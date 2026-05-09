"""Core utilities for uploading clips and requesting Gemini analyses."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from google import genai
from google.genai import types

from gateway.profiles import default_profile_name, get_manifest_node, load_profile_function, load_profile_manifest, load_prompt_text
from gateway.vision.timecode import seconds_to_timecode, timecode_to_seconds
from services.encoding import EncodingServiceError, FfmpegEncodingService

from .config import get_settings
from .prompts import DEFAULT_ROSTER_PROMPT, DEFAULT_TIMELINE_PROMPT
from .schema import EVENT_METADATA_RESPONSE_SCHEMA


DEFAULT_VIDEO_EXTENSIONS: tuple[str, ...] = (".mp4", ".mov", ".mkv", ".avi", ".m4v", ".webm")
DEFAULT_FOLDER_SLOW_MAP: dict[str, float] = {"regular": 1.0, "slowdown": 5.0}


class GeminiResponseDecodeError(RuntimeError):
    """Raised when Gemini streaming output cannot be parsed as JSON."""

    def __init__(self, message: str, response_text: str, original: json.JSONDecodeError):
        super().__init__(message)
        self.response_text = response_text
        self.original = original


@dataclass(frozen=True)
class BatchRequest:
    """Describes a single clip slated for Gemini analysis."""

    video_path: Path
    output_path: Path
    slowdown: float


@dataclass(frozen=True)
class AbilityLoaderHooks:
    profile_name: str
    character_loader: Callable[[Iterable[str]], Any] | None
    enemy_loader: Callable[[Iterable[str]], Any] | None


@dataclass(frozen=True)
class MediaTypeDefinition:
    name: str
    prompt_keys: tuple[str, ...]
    hooks: tuple[str, ...]
    slowdown: float | None = None
    model: str | None = None


@dataclass(frozen=True)
class ProjectMediaMapping:
    base_dir: Path
    video_map: dict[str, tuple[Path, ...]]


def _load_ability_loader(profile_name: str | None) -> AbilityLoaderHooks | None:
    profile = profile_name or default_profile_name()
    if not profile:
        return None
    hook = get_manifest_node(profile, "hooks", "gemini_ingest", "ability_loader")
    if not isinstance(hook, dict):
        return None
    if hook.get("enabled") is False:
        return None
    script = hook.get("script")
    if not script:
        return None
    char_name = hook.get("character_function")
    enemy_name = hook.get("enemy_function")
    if not char_name and not enemy_name:
        return None
    character_loader = load_profile_function(profile, script, char_name) if char_name else None
    enemy_loader = load_profile_function(profile, script, enemy_name) if enemy_name else None
    if not character_loader and not enemy_loader:
        return None
    return AbilityLoaderHooks(profile_name=profile, character_loader=character_loader, enemy_loader=enemy_loader)


def _invoke_loader(loader: Callable[[Iterable[str]], Any] | None, names: Iterable[str], profile_name: str) -> dict:
    if not loader:
        return {}
    try:
        result = loader(names, profile_name=profile_name)
    except TypeError:
        result = loader(names)
    if isinstance(result, dict):
        return result
    return {}


@lru_cache(maxsize=32)
def _get_media_type_definitions(profile_name: str | None) -> dict[str, MediaTypeDefinition]:
    profile = profile_name or default_profile_name()
    if not profile:
        return {}
    manifest = load_profile_manifest(profile)
    video_entries = (manifest.get("media_types") or {}).get("video") or []
    definitions: dict[str, MediaTypeDefinition] = {}
    for entry in video_entries:
        if not isinstance(entry, dict):
            continue
        type_name = str(entry.get("type") or "").strip()
        if not type_name:
            continue
        prompt_keys = tuple(str(key).strip() for key in entry.get("prompts") or [] if str(key).strip())
        hook_keys = tuple(str(key).strip() for key in entry.get("hooks") or [] if str(key).strip())
        slowdown_value = entry.get("slowdown")
        slowdown_factor: float | None = None
        if slowdown_value is not None:
            try:
                slowdown_factor = max(float(slowdown_value), 1.0)
            except (TypeError, ValueError):
                slowdown_factor = None
        model_override = entry.get("model")
        if isinstance(model_override, str):
            model_override = model_override.strip() or None
        else:
            model_override = None
        definitions[type_name] = MediaTypeDefinition(
            name=type_name,
            prompt_keys=prompt_keys,
            hooks=hook_keys,
            slowdown=slowdown_factor,
            model=model_override,
        )
    return definitions


@lru_cache(maxsize=64)
def _find_project_settings(start_path: str) -> ProjectMediaMapping | None:
    current = Path(start_path).resolve()
    for candidate in (current,) + tuple(current.parents):
        settings_path = candidate / "project.settings.json"
        if not settings_path.exists():
            continue
        try:
            with settings_path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Invalid JSON in {settings_path}: {exc}") from exc
        media_types = (data.get("media_types") or {}).get("video") or {}
        video_map: dict[str, tuple[Path, ...]] = {}
        base_dir = settings_path.parent.resolve()
        for type_name, paths in media_types.items():
            normalized: list[Path] = []
            for entry in paths or []:
                if not isinstance(entry, str) or not entry.strip():
                    continue
                configured = Path(entry)
                if not configured.is_absolute():
                    configured = (base_dir / configured).resolve()
                else:
                    configured = configured.resolve()
                normalized.append(configured)
            if normalized:
                video_map[str(type_name)] = tuple(normalized)
        return ProjectMediaMapping(base_dir=base_dir, video_map=video_map)
    return None


def _resolve_video_media_type(video_path: Path) -> tuple[str | None, ProjectMediaMapping | None]:
    mapping = _find_project_settings(str(video_path.parent))
    if not mapping:
        return None, None
    for type_name, folders in mapping.video_map.items():
        for folder in folders:
            try:
                if video_path.resolve().is_relative_to(folder):
                    return type_name, mapping
            except ValueError:
                continue
    return None, mapping


def _compose_prompt_from_profile(profile_name: str | None, prompt_keys: tuple[str, ...]) -> str | None:
    if not profile_name or not prompt_keys:
        return None
    sections: list[str] = []
    for key in prompt_keys:
        text = load_prompt_text(profile_name, key)
        if text:
            sections.append(text)
    if sections:
        return "\n\n".join(sections).strip()
    return None


def collect_batch_requests(
    footage_root: Path,
    *,
    folder_slow_map: dict[str, float] | None = None,
    meta_folder_name: str = "meta",
    allowed_extensions: Iterable[str] | None = None,
    overwrite: bool = False,
    clip_name_filter: Iterable[str] | None = None,
) -> list[BatchRequest]:
    """Return the list of Gemini jobs for the provided footage directory.

    Each key in ``folder_slow_map`` represents a subdirectory inside ``footage_root``
    and maps to the slowdown factor that should be applied before uploading the
    clips. JSON files are written into ``<subdir>/<meta_folder_name>``.
    """

    footage_root = footage_root.expanduser().resolve()
    if not footage_root.exists():
        raise FileNotFoundError(f"Footage root not found: {footage_root}")

    folder_map = dict(folder_slow_map or DEFAULT_FOLDER_SLOW_MAP)
    if not folder_map:
        raise ValueError("folder_slow_map must contain at least one folder definition.")

    extensions = allowed_extensions or DEFAULT_VIDEO_EXTENSIONS
    normalized_exts: set[str] = set()
    for entry in extensions:
        normalized = str(entry).strip().lower()
        if not normalized:
            continue
        if not normalized.startswith("."):
            normalized = f".{normalized}"
        normalized_exts.add(normalized)
    if not normalized_exts:
        raise ValueError("allowed_extensions cannot be empty.")

    normalized_filter: set[str] | None = None
    if clip_name_filter:
        normalized_filter = set()
        for value in clip_name_filter:
            normalized = str(value).strip().lower()
            if not normalized:
                continue
            normalized_filter.add(normalized)

    batch_requests: list[BatchRequest] = []
    for folder_name, slowdown_factor in folder_map.items():
        subdir = footage_root / folder_name
        if not subdir.exists():
            print(f"[Gemini][batch] Skipping missing folder: {subdir}")
            continue
        meta_dir = subdir / meta_folder_name
        meta_dir.mkdir(parents=True, exist_ok=True)
        slowdown_value = max(float(slowdown_factor), 1.0)
        for candidate in sorted(subdir.iterdir()):
            if candidate.is_dir():
                continue
            if candidate.suffix.lower() not in normalized_exts:
                continue
            if normalized_filter:
                rel_name = str(candidate.relative_to(footage_root)).replace("\\", "/").lower()
                name = candidate.name.lower()
                stem = candidate.stem.lower()
                if (
                    rel_name not in normalized_filter
                    and name not in normalized_filter
                    and stem not in normalized_filter
                ):
                    continue
            output_path = meta_dir / f"{candidate.stem}_gemini.json"
            if not overwrite and output_path.exists():
                print(f"[Gemini][batch] Skipping existing analysis: {output_path}")
                continue
            batch_requests.append(BatchRequest(video_path=candidate, output_path=output_path, slowdown=slowdown_value))

    return batch_requests


def _wait_for_file_active(client: genai.Client, file_name: str, timeout: float = 300.0) -> None:
    """Poll until the Gemini Files API marks the upload as ACTIVE."""
    start = time.time()
    while True:
        file_state = client.files.get(name=file_name)
        state_name = getattr(getattr(file_state, "state", None), "name", None)
        if state_name == "ACTIVE" or state_name is None:
            return
        if state_name == "FAILED":
            raise RuntimeError(f"Gemini file {file_name} failed to process.")
        if time.time() - start > timeout:
            raise TimeoutError(f"Gemini file {file_name} did not become active within {timeout} seconds.")
        time.sleep(1.0)


_ENCODING_SERVICE: FfmpegEncodingService | None = None


def _get_encoding_service() -> FfmpegEncodingService:
    global _ENCODING_SERVICE
    if _ENCODING_SERVICE is None:
        _ENCODING_SERVICE = FfmpegEncodingService()
    return _ENCODING_SERVICE


def _parse_media_resolution(value: str | None) -> types.MediaResolution | None:
    if not value:
        return None
    normalized = value.strip().upper()
    if not normalized.startswith("MEDIA_RESOLUTION_"):
        normalized = f"MEDIA_RESOLUTION_{normalized}"
    try:
        return types.MediaResolution[normalized]
    except KeyError as exc:
        valid = ", ".join(
            name.replace("MEDIA_RESOLUTION_", "").lower() for name in types.MediaResolution.__members__.keys()
        )
        raise ValueError(f"Invalid media resolution '{value}'. Choose from: {valid}.") from exc


def _parse_thinking_level(value: str | None) -> types.ThinkingLevel | None:
    if not value:
        return None
    normalized = value.strip().upper()
    if normalized in {"", "DEFAULT", "NONE"}:
        return None
    if normalized not in types.ThinkingLevel.__members__:
        valid = ", ".join(name.lower() for name in types.ThinkingLevel.__members__.keys())
        raise ValueError(f"Invalid thinking level '{value}'. Choose from: {valid}.")
    return types.ThinkingLevel[normalized]


def _slowdown_video(source: Path, factor: float) -> Path:
    """Proxy that leverages the shared encoding service for slowdown requests."""
    service = _get_encoding_service()
    try:
        return service.slowdown_video(source, factor=factor)
    except EncodingServiceError as exc:
        raise RuntimeError(f"ffmpeg slowdown failed: {exc}") from exc


def _stream_json_response(
    client: genai.Client,
    model_name: str,
    contents: list[dict[str, Any]],
    *,
    config: dict[str, Any],
    log_prefix: str,
) -> dict[str, Any]:
    chunks = client.models.generate_content_stream(
        model=model_name,
        contents=contents,
        config=config,
    )
    collected: list[str] = []
    for chunk in chunks:
        text = getattr(chunk, "text", None)
        if not text and getattr(chunk, "candidates", None):
            for candidate in chunk.candidates:
                content = getattr(candidate, "content", None)
                parts = getattr(content, "parts", None) if content else None
                if parts:
                    for part in parts:
                        if getattr(part, "text", None):
                            text = part.text
                            break
                if text:
                    break
        if text:
            print(f"{log_prefix} >> {text}")
            collected.append(text)
    response_text = "".join(collected).strip()
    if not response_text:
        raise RuntimeError("Gemini response was empty or streaming failed.")
    try:
        return json.loads(response_text)
    except json.JSONDecodeError as exc:
        repaired = _attempt_repair_json(response_text)
        if repaired is not None:
            print("[Gemini] Detected truncated JSON; repaired payload with synthetic closings.")
            return repaired
        raise GeminiResponseDecodeError("Gemini returned malformed JSON", response_text, exc) from exc


def _attempt_repair_json(response_text: str) -> Optional[dict[str, Any]]:
    """Best-effort fixer for truncated Gemini JSON blobs."""

    trimmed = response_text.rstrip()
    if not trimmed:
        return None
    stack: list[str] = []
    in_string = False
    escape = False
    for ch in trimmed:
        if in_string:
            if escape:
                escape = False
                continue
            if ch == "\\":
                escape = True
                continue
            if ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            stack.append("}")
        elif ch == "[":
            stack.append("]")
        elif ch in "}]":
            if not stack or stack[-1] != ch:
                return None
            stack.pop()
    suffix: list[str] = []
    if in_string:
        suffix.append('"')
    while stack:
        suffix.append(stack.pop())
    if not suffix:
        return None
    repaired_text = trimmed + "".join(suffix)
    try:
        return json.loads(repaired_text)
    except json.JSONDecodeError:
        return None


def _identify_roster(
    client: genai.Client,
    model_name: str,
    video_part: dict[str, Any],
    *,
    prompt_text: str,
    tools: Optional[list[types.Tool]] = None,
    temperature: Optional[float] = None,
    thinking_enum: Optional[types.ThinkingLevel] = None,
) -> tuple[list[str], list[str]]:
    config: dict[str, Any] = {"response_mime_type": "application/json"}
    if tools:
        config["tools"] = tools
    if temperature is not None:
        config["temperature"] = temperature
    if thinking_enum:
        config["thinking_config"] = types.ThinkingConfig(
            thinking_level=thinking_enum,
            include_thoughts=False,
        )
    contents = [
        {
            "role": "user",
            "parts": [
                video_part,
                types.Part(text=prompt_text),
            ],
        }
    ]
    data = _stream_json_response(
        client,
        model_name,
        contents,
        config=config,
        log_prefix="[Gemini][roster]",
    )
    def _collect(key: str) -> list[str]:
        values = data.get(key)
        if isinstance(values, list):
            return [str(name).strip() for name in values if isinstance(name, str) and name.strip()]
        return []

    roster = _collect("roster")
    enemies = _collect("enemies")
    if roster:
        print(f"[Gemini] Roster detected: {', '.join(roster)}")
    if enemies:
        print(f"[Gemini] Visible enemies detected: {', '.join(enemies)}")
    return roster, enemies


def _augment_prompt_with_abilities(
    prompt_text: str,
    player_hints: dict[str, dict[str, dict[str, str]]],
    enemy_hints: dict[str, dict[str, object]],
) -> str:
    sections: list[str] = [prompt_text]

    if player_hints:
        roster_block = json.dumps([{name: details} for name, details in player_hints.items()], indent=2, ensure_ascii=False)
        print(f"{roster_block}\n")
        sections.append(
            "Known roster abilities (use these canonical action types when describing the clip):\n"
            f"{roster_block}"
        )

    if enemy_hints:
        enemy_block = json.dumps([{name: details} for name, details in enemy_hints.items()], indent=2, ensure_ascii=False)
        print(f"{enemy_block}\n")
        sections.append(
            "Known enemy abilities and weaknesses:\n"
            f"{enemy_block}"
        )

    return "\n\n".join(sections)


def _iter_gemini_events(node: Any):
    """Yield every event dictionary nested inside a Gemini response."""
    if isinstance(node, dict):
        events = node.get("events")
        if isinstance(events, list):
            for event in events:
                if isinstance(event, dict):
                    yield event
        for key, value in node.items():
            if key == "events":
                continue
            if isinstance(value, (dict, list)):
                yield from _iter_gemini_events(value)
    elif isinstance(node, list):
        for item in node:
            if isinstance(item, (dict, list)):
                yield from _iter_gemini_events(item)


_TIMESTAMP_FIELD_PAIRS: tuple[tuple[str, str], ...] = (
    ("time_s", "timecode"),
    ("start_s", "start_timecode"),
    ("end_s", "end_timecode"),
    ("anchor_s", "anchor_timecode"),
)


def _iter_event_timestamp_nodes(node: Any):
    """Yield an event dictionary and nested dictionaries that may carry timestamp evidence."""
    if isinstance(node, dict):
        yield node
        for key, value in node.items():
            if key == "events":
                continue
            if isinstance(value, (dict, list)):
                yield from _iter_event_timestamp_nodes(value)
    elif isinstance(node, list):
        for item in node:
            if isinstance(item, (dict, list)):
                yield from _iter_event_timestamp_nodes(item)


def _coerce_seconds(seconds_value: Any, timecode_value: Any) -> float | None:
    seconds = None
    if isinstance(timecode_value, str):
        seconds = timecode_to_seconds(timecode_value)
    if seconds is None and isinstance(seconds_value, (int, float)):
        seconds = float(seconds_value)
    elif seconds is None and isinstance(seconds_value, str):
        try:
            seconds = float(seconds_value)
        except ValueError:
            seconds = None
    return seconds


def _normalize_event_timestamps(payload: Any, slowdown_factor: float) -> None:
    """Normalize event timestamp pairs and counteract slowdown stretching."""
    for event in _iter_gemini_events(payload):
        for node in _iter_event_timestamp_nodes(event):
            for seconds_key, timecode_key in _TIMESTAMP_FIELD_PAIRS:
                seconds = _coerce_seconds(node.get(seconds_key), node.get(timecode_key))
                if seconds is None:
                    continue
                if slowdown_factor > 1.0:
                    seconds /= slowdown_factor
                node[seconds_key] = round(seconds, 3)
                node[timecode_key] = seconds_to_timecode(seconds)


def analyze_clip(
    video_path: Path,
    *,
    prompt: Optional[str] = None,
    output_path: Optional[Path] = None,
    model: Optional[str] = None,
    slowdown: Optional[float] = None,
    media_resolution: Optional[str] = None,
    skip_roster_pass: bool = False,
    enable_google_search: bool = False,
    temperature: Optional[float] = None,
    thinking_level: Optional[str] = None,
    enable_structured_output: bool = False,
    profile_name: Optional[str] = None,
    keep_upload: bool = False,
) -> tuple[dict[str, Any], Path]:
    """Upload a clip to Gemini and return the parsed JSON analysis."""
    video_path = video_path.expanduser().resolve()
    if not video_path.exists():
        raise FileNotFoundError(f"Video not found: {video_path}")

    settings = get_settings()
    profile_name = profile_name or default_profile_name()
    media_type_name, _ = _resolve_video_media_type(video_path)
    media_definitions = _get_media_type_definitions(profile_name)
    media_definition = media_definitions.get(media_type_name) if media_type_name else None
    if media_type_name:
        print(f"[Gemini] Media type: {media_type_name}")
        if media_definition is None:
            print(f"[Gemini] Warning: no manifest entry for media type '{media_type_name}'. Falling back to defaults.")
    prompt_from_profile = None
    if media_definition:
        prompt_from_profile = _compose_prompt_from_profile(profile_name, media_definition.prompt_keys)
    client = genai.Client(api_key=settings.api_key)
    prompt_text = prompt or prompt_from_profile or DEFAULT_TIMELINE_PROMPT
    media_model = media_definition.model if media_definition else None
    model_name = model or media_model or settings.model
    if not model and media_model:
        print(
            f"[Gemini] Media-type model override: using {media_model} instead of {settings.model}."
        )
    slowdown_override = media_definition.slowdown if media_definition else None
    slowdown_factor: float
    if slowdown_override is not None:
        if slowdown is not None and slowdown_override != slowdown:
            print(
                f"[Gemini] Media-type slowdown {slowdown_override}x overrides requested slowdown {slowdown}x."
            )
        slowdown_factor = slowdown_override
    elif slowdown is not None:
        slowdown_factor = max(float(slowdown), 1.0)
    else:
        slowdown_factor = max(settings.default_slowdown_factor, 1.0)
    resolution_choice = media_resolution or settings.media_resolution
    try:
        resolution_enum = _parse_media_resolution(resolution_choice)
    except ValueError as exc:
        raise RuntimeError(str(exc)) from exc
    temp_value = temperature if temperature is not None else settings.temperature
    thinking_choice = thinking_level or settings.thinking_level
    try:
        thinking_enum = _parse_thinking_level(thinking_choice)
    except ValueError as exc:
        raise RuntimeError(str(exc)) from exc
    use_google_search = settings.enable_google_search or enable_google_search
    use_structured_output = settings.enable_structured_output or enable_structured_output
    skip_roster_pass = skip_roster_pass or settings.skip_roster_pass
    tools_list: list[types.Tool] | None = None
    if use_google_search:
        tools_list = [types.Tool(google_search=types.GoogleSearch())]
    temp_files: list[Path] = []

    upload_source = video_path
    if slowdown_factor > 1.0:
        print(f"[Gemini] Slowing clip by {slowdown_factor}x to approximate {slowdown_factor} fps sampling.")
        upload_source = _slowdown_video(video_path, slowdown_factor)
        temp_files.append(upload_source)
        print(f"[Gemini] Slowdown output: {upload_source}")

    print("[Gemini] Uploading via Files API (native sampling ~1 fps).")
    upload = client.files.upload(file=str(upload_source))
    if getattr(upload, "name", None):
        _wait_for_file_active(client, upload.name)
    video_part = {
        "file_data": {
            "file_uri": upload.uri,
            "mime_type": upload.mime_type,
        },
        "video_metadata": {"fps": 1.0},
    }

    roster_names: list[str] = []
    enemy_names: list[str] = []
    player_hints: dict[str, dict] = {}
    enemy_hints: dict[str, dict] = {}
    wants_ability_hook = media_definition is None or (
        media_definition and "ability_loader" in media_definition.hooks
    )
    ability_loader = _load_ability_loader(profile_name) if wants_ability_hook else None
    if not skip_roster_pass:
        try:
            roster_names, enemy_names = _identify_roster(
                client,
                model_name,
                video_part,
                prompt_text=load_prompt_text(profile_name, "roster") or DEFAULT_ROSTER_PROMPT,
                tools=tools_list,
                temperature=temp_value,
                thinking_enum=thinking_enum,
            )
        except Exception as exc:
            print(f"[Gemini] Roster pass failed: {exc}")
            roster_names, enemy_names = [], []

        if roster_names and ability_loader and ability_loader.character_loader:
            try:
                player_hints = _invoke_loader(ability_loader.character_loader, roster_names, ability_loader.profile_name)
            except Exception as exc:  # pylint: disable=broad-except
                print(f"[Gemini] Ability lookup failed: {exc}")
                player_hints = {}
            if player_hints:
                print("[Gemini] Loaded ability reference for: " + ", ".join(player_hints.keys()))
        if enemy_names and ability_loader and ability_loader.enemy_loader:
            try:
                enemy_hints = _invoke_loader(ability_loader.enemy_loader, enemy_names, ability_loader.profile_name)
            except Exception as exc:  # pylint: disable=broad-except
                print(f"[Gemini] Enemy lookup failed: {exc}")
                enemy_hints = {}
            if enemy_hints:
                print("[Gemini] Loaded enemy reference for: " + ", ".join(enemy_hints.keys()))

        if player_hints or enemy_hints:
            prompt_text = _augment_prompt_with_abilities(prompt_text, player_hints, enemy_hints)

    contents = [
        {
            "role": "user",
            "parts": [
                video_part,
                types.Part(text=prompt_text),
            ],
        }
    ]

    request_config: dict[str, Any] = {"response_mime_type": "application/json"}
    if resolution_enum:
        request_config["media_resolution"] = resolution_enum
    if temp_value is not None:
        request_config["temperature"] = temp_value
    if tools_list:
        request_config["tools"] = tools_list
    if thinking_enum:
        request_config["thinking_config"] = types.ThinkingConfig(
            thinking_level=thinking_enum,
            include_thoughts=False,
        )
    if use_structured_output:
        request_config["response_schema"] = EVENT_METADATA_RESPONSE_SCHEMA

    output_dir = output_path.parent if output_path else settings.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    final_output = output_path or (output_dir / f"{video_path.stem}_gemini.json")

    upload_name = getattr(upload, "name", None)
    try:
        max_attempts = settings.response_retry_attempts
        data: dict[str, Any] | None = None
        for attempt in range(1, max_attempts + 1):
            print(f"[Gemini] Requesting analysis from {model_name} (slowdown factor {slowdown_factor}) ... [attempt {attempt}/{max_attempts}]")
            try:
                data = _stream_json_response(
                    client,
                    model_name,
                    contents,
                    config=request_config,
                    log_prefix="[Gemini]",
                )
                break
            except GeminiResponseDecodeError as exc:
                partial_path = final_output.with_name(f"{final_output.stem}.attempt{attempt}.partial.json")
                partial_path.write_text(exc.response_text, encoding="utf-8", errors="ignore")
                print(
                    f"[Gemini] JSON decoding failed for {video_path.name} (attempt {attempt}). "
                    f"Raw response saved to {partial_path}."
                )
                if attempt < max_attempts:
                    print("[Gemini] Retrying analysis...")
                    time.sleep(2.0)
                    continue
                raise RuntimeError(
                    f"Gemini response for {video_path.name} remained malformed after {max_attempts} attempts. "
                    f"See {partial_path} for the raw payload."
                ) from exc

        if data is None:
            raise RuntimeError("Gemini analysis did not return any data.")

        _normalize_event_timestamps(data, slowdown_factor)

        with final_output.open("w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)

        print(f"[Gemini] Analysis saved to {final_output}")
        return data, final_output
    finally:
        for temp in temp_files:
            temp.unlink(missing_ok=True)
        if upload_name:
            if keep_upload:
                print(f"[Gemini] Keeping file {upload_name} in storage (requested by user)")
            else:
                try:
                    client.files.delete(name=upload_name)
                    print(f"[Gemini] Deleted file {upload_name} from storage")
                except Exception as exc:
                    print(f"[Gemini] Failed to delete file {upload_name}: {exc}")
