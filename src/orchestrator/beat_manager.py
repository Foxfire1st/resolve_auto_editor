import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

from dotenv import load_dotenv
from google import genai
from google.genai import types

from src.orchestrator import models

LOGGER = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(REPO_ROOT / ".env")

VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".avi"}


def collect_footage_summaries(
    footage_roots: Optional[Iterable[Path] | Path],
) -> List[Dict[str, Any]]:
    """Harvest footage summaries from gemini_ingest outputs under multiple roots."""

    if footage_roots is None:
        return []

    if isinstance(footage_roots, (str, os.PathLike, Path)):
        root_list = [Path(footage_roots)]
    else:
        root_list = [Path(root) for root in footage_roots]

    meta_paths: List[Path] = []
    for root in root_list:
        if not root or not root.exists():
            continue
        meta_paths.extend(root.glob("**/meta/*.json"))

    if not meta_paths:
        return []

    meta_paths.sort()
    summaries: List[Dict[str, Any]] = []

    def _derive_asset_id(meta_path: Path) -> str:
        name = meta_path.name
        # Strip suffix starting at "_gemini" if present
        marker = "_gemini"
        if marker in name:
            return name.split(marker, 1)[0]
        return meta_path.stem

    def _find_source_path(meta_path: Path, asset_id: str) -> Optional[str]:
        parent = meta_path.parent.parent
        if not parent.exists():
            return None
        for candidate in sorted(parent.glob(f"{asset_id}.*")):
            if candidate.suffix.lower() in VIDEO_EXTENSIONS:
                return str(candidate)
        return None

    seen_assets: set[str] = set()

    for meta_path in meta_paths:
        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as exc:  # pragma: no cover - defensive against corrupt files
            LOGGER.debug("Skipping %s: %s", meta_path, exc)
            continue

        meta = data.get("meta") or {}
        summary_text = meta.get("summary")
        if not summary_text:
            continue

        asset_id = _derive_asset_id(meta_path)
        asset_id = _derive_asset_id(meta_path)
        dedupe_key = f"{asset_id}:{meta_path.parent.as_posix()}"
        if dedupe_key in seen_assets:
            continue
        seen_assets.add(dedupe_key)

        tags: List[str] = []
        for key in ("party", "enemies", "characters", "items"):
            values = meta.get(key)
            if isinstance(values, list):
                tags.extend(str(v) for v in values)

        for key in ("stage_name", "mode"):
            value = meta.get(key)
            if value:
                tags.append(str(value))

        if meta.get("is_combat"):
            tags.append("combat")

        entry = {
            "asset_id": asset_id,
            "summary": summary_text.strip(),
            "mode": meta.get("mode"),
            "stage_name": meta.get("stage_name"),
            "tags": sorted({t for t in tags if t}),
            "source_path": _find_source_path(meta_path, asset_id),
            "duration": meta.get("duration") or meta.get("length_s"),
            "meta_path": str(meta_path),
        }
        summaries.append(entry)

    return summaries


def load_project_settings(settings_path: Optional[Path]) -> Dict[str, Any]:
    if not settings_path:
        return {}
    try:
        with open(settings_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        LOGGER.debug("Project settings not found at %s", settings_path)
    except Exception as exc:  # pragma: no cover - corrupt settings
        LOGGER.warning("Failed to read project settings %s: %s", settings_path, exc)
    return {}


def extract_footage_roots_from_settings(
    settings: Dict[str, Any],
    *,
    base_dir: Optional[Path] = None,
) -> List[Path]:
    media_types = settings.get("media_types") or {}
    video_section = media_types.get("video") if isinstance(media_types, dict) else {}
    if not isinstance(video_section, dict):
        return []

    roots: List[Path] = []
    for path_list in video_section.values():
        if not isinstance(path_list, list):
            continue
        for entry in path_list:
            if not isinstance(entry, str):
                continue
            path = Path(entry)
            if not path.is_absolute() and base_dir:
                path = (base_dir / path).resolve()
            roots.append(path)

    # Deduplicate while preserving order
    deduped: List[Path] = []
    seen: set[str] = set()
    for root in roots:
        key = str(root.resolve()) if root else str(root)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(root)
    return deduped

def create_segment_index(alignment_path: str, output_path: str) -> None:
    """
    Reads the full alignment JSON and creates a lightweight index 
    containing only segment-level timing (stripping word-level data).
    """
    with open(alignment_path, 'r', encoding='utf-8') as f:
        data = json.load(f)
    
    segments = []
    if "aligned" in data:
        source_segments = data["aligned"]
    else:
        # Handle case where it might be a list directly or different structure
        source_segments = data if isinstance(data, list) else []

    for seg in source_segments:
        # Create a copy without 'words'
        clean_seg = {
            "id": seg.get("id"),
            "text": seg.get("text"),
            "t0": seg.get("t0"),
            "t1": seg.get("t1"),
            "source": seg.get("source")
        }
        segments.append(clean_seg)
    
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump({"segments": segments}, f, indent=2)
    
    print(f"Created segment index at {output_path} with {len(segments)} segments.")

def generate_beat_map_prompt(
    script_text: str,
    profile_text: str,
    window_context: str = "",
    manifest_hint: str = "",
) -> str:
    """Generates the prompt for the LLM to create the Beat Map."""

    window_block = window_context.strip() or "- (no event metadata context available)"
    return f"""
You are a Video Director creating a "Beat Map" for a video edit.
Your goal is to group the narration script into logical "Beats".
A "Beat" is a sequence of sentences that share a common visual idea or topic.

INPUTS:
1. SCRIPT (with IDs):
{script_text}

2. STYLE PROFILE:
{profile_text}

3. RECENT EVENT METADATA WINDOWS (footage already indexed):
{window_block}

4. FOOTAGE MANIFEST (Gemini Files attachment):
{manifest_hint or "- (manifest missing; upload footage_manifest.ndjson before running this command)"}

INSTRUCTIONS:
- Group consecutive sentences (by ID) into Beats.
- Assign a 'visual_intent' to each beat describing what we should see.
- Assign 'search_queries' to find B-roll for this beat.
- Determine the 'pacing' (fast, slow, normal).
- ONLY promise visuals that can be sourced from the provided footage list or windows. Reference the relevant asset_id / tags when describing visual_intent or search_queries.
- When coverage is thin, merge or simplify beats instead of inventing imaginary shots.

OUTPUT FORMAT (JSON):
{{
  "beats": [
    {{
      "id": "B001",
      "segment_ids": ["L001", "L002"],
      "visual_intent": "Intro montage showing the character's power",
      "pacing": "fast",
      "search_queries": ["character ult", "explosion", "wind effects"]
    }},
    ...
  ]
}}
"""


def load_segments(path: Path) -> List[Dict[str, Any]]:
    """Load the lightweight alignment index created by create_segment_index."""
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data.get("segments", [])


def read_text_file(path: Optional[Path]) -> str:
    """Return the contents of a text file or an empty string when not provided."""
    if not path:
        return ""
    with open(path, "r", encoding="utf-8") as f:
        return f.read().strip()


def format_segments_for_prompt(segments: Sequence[Dict[str, Any]]) -> str:
    """Format alignment segments into a compact block for the LLM prompt."""
    lines: List[str] = []
    for seg in segments:
        seg_id = seg.get("id", "?")
        text = seg.get("text", "").strip()
        t0 = seg.get("t0")
        t1 = seg.get("t1")
        timing = ""
        if isinstance(t0, (int, float)) and isinstance(t1, (int, float)):
            timing = f" [{t0:.2f}s - {t1:.2f}s]"
        lines.append(f"{seg_id}: {text}{timing}")
    return "\n".join(lines)


def extract_headings(script_text: str) -> List[str]:
    """Return markdown headings (## / ###) from the narration script."""
    headings: List[str] = []
    for line in script_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("##"):
            headings.append(stripped.lstrip("# "))
    return headings


def _build_prompt_payload(
    segments: Sequence[Dict[str, Any]],
    script_text: str,
    profile_text: str,
    profile_name: Optional[str],
    manifest_hint: str = "",
) -> str:
    segment_block = format_segments_for_prompt(segments)
    headings = extract_headings(script_text)
    headings_text = "\n".join(f"- {title}" for title in headings) or "- (no headings supplied)"
    window_context = _vector_window_context(profile_name)
    return generate_beat_map_prompt(
        script_text=f"Headings:\n{headings_text}\n\nNarration:\n{segment_block}",
        profile_text=profile_text or "(no profile text provided)",
        window_context=window_context,
        manifest_hint=manifest_hint,
    )


def generate_beat_map(
    segments: Sequence[Dict[str, Any]],
    *,
    script_text: str = "",
    style_profile_text: str = "",
    model_name: Optional[str] = None,
    profile_name: Optional[str] = None,
    file_attachments: Optional[Sequence[Any]] = None,
    manifest_hint: str = "",
) -> models.BeatMap:
    """Generate a BeatMap via Gemini. Raises if no API key is available."""

    prompt = _build_prompt_payload(
        segments,
        script_text,
        style_profile_text,
        profile_name,
        manifest_hint=manifest_hint,
    )

    api_key = (
        os.environ.get("gemini_api_key")
        or os.environ.get("GEMINI_API_KEY")
        or os.environ.get("GOOGLE_API_KEY")
    )
    if not api_key:
        raise RuntimeError(
            "Gemini API key not found. Set GEMINI_API_KEY or GOOGLE_API_KEY in your .env."
        )

    model = model_name or os.environ.get(
        "GEMINI_MODEL_BEATS",
        os.environ.get("GEMINI_MODEL_EDITOR", "gemini-2.5-flash"),
    )
    client = genai.Client(api_key=api_key)

    contents: List[Any] = []
    if file_attachments:
        contents.extend(file_attachments)
    contents.append(prompt)

    response = client.models.generate_content(
        model=model,
        contents=contents,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=models.BeatMap,
            temperature=0.2,
        ),
    )

    if response.parsed:
        return response.parsed
    if response.text:
        try:
            return models.BeatMap.model_validate_json(response.text)
        except AttributeError:  # pragma: no cover - pydantic v1 fallback
            return models.BeatMap.parse_raw(response.text)

    raise RuntimeError("Gemini response did not include JSON content for the beat map.")


def _vector_window_context(profile_name: Optional[str], limit: int = 8) -> str:
    try:
        from gateway.vision.event_metadata.config import get_settings
        from gateway.vision.event_metadata.search import search_events
    except Exception as exc:  # pragma: no cover - optional dependency at runtime
        LOGGER.debug("Event metadata context unavailable: %s", exc)
        return ""

    profile = profile_name or None
    if profile is None:
        try:
            profile = get_settings().profile
        except Exception as exc:  # pragma: no cover
            LOGGER.debug("Failed to resolve profile for window context: %s", exc)
            return ""

    try:
        windows = search_events("", profile_name=profile, limit=limit)
    except Exception as exc:  # pragma: no cover - DB missing etc.
        LOGGER.debug("Unable to list event metadata windows for beat prompt: %s", exc)
        return ""

    if not windows:
        return ""

    lines: List[str] = []
    for window in windows:
        tags = ", ".join(window.tags[:5]) if window.tags else "(no tags)"
        duration = max(window.end_s - window.start_s, 0.1)
        caption = (window.summary or "").strip()
        preview = caption[:140] + ("..." if len(caption) > 140 else "")
        lines.append(
            f"- {window.video_id} [{window.start_s:.1f}-{window.end_s:.1f}s | {duration:.1f}s]: "
            f"{tags} -> {preview}"
        )
    return "\n".join(lines)


