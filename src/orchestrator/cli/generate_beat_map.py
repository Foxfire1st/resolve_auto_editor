"""Command-line utility to (re)generate beat maps for narration scripts."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from src.orchestrator import beat_manager
from src.orchestrator.clients import gemini_files
from src.orchestrator.retrieval import attachment_state, footage_manifest

DEFAULT_CONFIG_PATH = Path("profiles/default_beat_map.json")


def _load_default_config() -> dict[str, str]:
    if not DEFAULT_CONFIG_PATH.exists():
        return {}
    return json.loads(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))


_DEFAULTS = _load_default_config()
DEFAULT_PROJECT_ROOT = Path(_DEFAULTS.get("project_root", "samples/default edit"))
DEFAULT_SEGMENT_INDEX = Path(_DEFAULTS.get("segment_index", str(DEFAULT_PROJECT_ROOT / "alignment_segments.json")))
DEFAULT_SCRIPT = Path(_DEFAULTS.get("script", str(DEFAULT_PROJECT_ROOT / "script.md")))
DEFAULT_PROFILE_TEXT = Path(_DEFAULTS.get("profile_text", "profiles/default/prompts/guide_analysis_prompt.md"))
DEFAULT_OUTPUT = Path(_DEFAULTS.get("output", str(DEFAULT_PROJECT_ROOT / "script_breakdown.json")))
DEFAULT_MANIFEST_PATH = Path(_DEFAULTS.get("manifest_path", str(DEFAULT_PROJECT_ROOT / "outputs" / "footage_manifest.ndjson")))
DEFAULT_HANDLE_PATH = Path(_DEFAULTS.get("manifest_handle", str(DEFAULT_MANIFEST_PATH.with_suffix(".handle.json"))))
DEFAULT_STATE_PATH = Path(
    _DEFAULTS.get("attachment_state", str(DEFAULT_PROJECT_ROOT / "outputs" / attachment_state.DEFAULT_STATE_FILENAME))
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate a beat map JSON by calling Gemini or using a fallback heuristic.",
    )
    parser.add_argument("--segments", type=Path, default=DEFAULT_SEGMENT_INDEX)
    parser.add_argument("--script", type=Path, default=DEFAULT_SCRIPT)
    parser.add_argument("--profile-text", type=Path, default=DEFAULT_PROFILE_TEXT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--model-name", type=str, default=None)
    parser.add_argument("--profile", type=str, default=None, help="Vector profile name (default from env)")
    parser.add_argument(
        "--manifest-path",
        type=Path,
        default=DEFAULT_MANIFEST_PATH,
        help="Where to write the footage manifest (NDJSON).",
    )
    parser.add_argument(
        "--manifest-handle",
        type=Path,
        default=DEFAULT_HANDLE_PATH,
        help="Path to persist Gemini Files handle metadata.",
    )
    parser.add_argument(
        "--attachment-state",
        type=Path,
        default=DEFAULT_STATE_PATH,
        help="Path to the shared attachment state file (records handle metadata).",
    )
    return parser


def _ensure_manifest_handle(
    manifest_path: Path, handle_path: Path
) -> tuple[gemini_files.UploadedFileHandle, Any]:
    if not manifest_path.exists():
        raise FileNotFoundError(f"Manifest file not found: {manifest_path}")

    client = gemini_files.GeminiFilesClient()
    cached = gemini_files.load_handle(handle_path)

    def _upload_new_handle() -> gemini_files.UploadedFileHandle:
        handle = client.upload(manifest_path)
        gemini_files.save_handle(handle, handle_path)
        print(
            f"[generate_beat_map] Uploaded manifest ({handle.size_bytes} bytes) -> {handle.gemini_name}"
        )
        return handle

    handle = None
    if cached and gemini_files.handle_matches_file(cached, manifest_path):
        handle = cached
        print(
            f"[generate_beat_map] Reusing Gemini handle {cached.gemini_name} "
            f"(expires {cached.expire_time or '≈48h'})"
        )
        try:
            file_ref = client.get(cached.gemini_name)
            return cached, file_ref
        except Exception as exc:
            print(
                f"[generate_beat_map] Cached handle {cached.gemini_name} unavailable ({exc}); re-uploading manifest."
            )
            handle = None

    handle = handle or _upload_new_handle()
    file_ref = client.get(handle.gemini_name)
    return handle, file_ref


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    load_dotenv(Path(__file__).resolve().parents[2] / ".env")

    segments = beat_manager.load_segments(args.segments)
    script_text = beat_manager.read_text_file(args.script)
    profile_text = beat_manager.read_text_file(args.profile_text)

    try:
        manifest_entries = footage_manifest.read_manifest(args.manifest_path)
    except FileNotFoundError:
        print(
            "[generate_beat_map] Manifest missing. Run build_footage_manifest before this step: "
            f"{args.manifest_path}"
        )
        sys.exit(1)
    except ValueError as exc:
        print(f"[generate_beat_map] Manifest is invalid: {exc}")
        sys.exit(1)

    print(
        f"[generate_beat_map] Loaded manifest with {len(manifest_entries)} entries from {args.manifest_path}"
    )
    print(footage_manifest.format_manifest_preview(manifest_entries))

    try:
        handle, file_ref = _ensure_manifest_handle(args.manifest_path, args.manifest_handle)
    except Exception as exc:  # pragma: no cover - network failure
        print(f"[generate_beat_map] Failed to upload manifest: {exc}")
        sys.exit(1)

    manifest_hint = (
        f"Handle {handle.gemini_name} | size={handle.size_bytes} bytes | "
        f"expires {handle.expire_time or '≈48h from upload'}"
    )

    if args.attachment_state:
        attachment_state.record_attachment(
            state_path=args.attachment_state,
            attachment_name="footage_manifest",
            handle=handle,
            handle_path=args.manifest_handle,
        )

    beat_map = beat_manager.generate_beat_map(
        segments,
        script_text=script_text,
        style_profile_text=profile_text,
        model_name=args.model_name,
        profile_name=args.profile,
        file_attachments=[file_ref],
        manifest_hint=manifest_hint,
    )

    if not args.output.parent.exists():
        args.output.parent.mkdir(parents=True, exist_ok=True)

    with open(args.output, "w", encoding="utf-8") as f:
        f.write(beat_map.model_dump_json(indent=2))

    beat_count = len(beat_map.beats)
    segment_total = sum(len(beat.segment_ids) for beat in beat_map.beats)
    print(
        f"[generate_beat_map] Wrote {beat_count} beat(s) covering "
        f"{segment_total} narration lines -> {args.output}"
    )


if __name__ == "__main__":
    main()
