"""Production Core Edit builder with optional time-range filtering."""

from __future__ import annotations

import argparse
import json
from importlib import import_module
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from dotenv import load_dotenv
from gateway.otio_fcpxml import builder
from orchestrator import core_editor, models
from orchestrator.assembly.candidate_matrix import (
    CandidateMatrixBuilder,
    build_slots_from_beat_map,
)
from orchestrator.assembly.export import proposal_to_semantic_edit
from orchestrator.assembly.feedback import run_resolve_feedback
from orchestrator.assembly.review import (
    audit_assembly_quality,
    gemini_review_proposal,
    review_proposal,
    write_quality_report,
)
from orchestrator.core_editor import ManualClipPlacement
from orchestrator.retrieval import footage_manifest
from orchestrator.tool_bridge.resolve_tools import ResolveTools

DEFAULT_CONFIG_PATH = Path("profiles/default_core_edit.json")


def _load_default_config() -> Dict[str, str]:
    if not DEFAULT_CONFIG_PATH.exists():
        return {}
    return json.loads(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))


_DEFAULTS = _load_default_config()
DEFAULT_PROJECT_ROOT = Path(_DEFAULTS.get("project_root", "samples/default edit"))
DEFAULT_BEAT_MAP = Path(_DEFAULTS.get("beat_map", str(DEFAULT_PROJECT_ROOT / "script_breakdown.json")))
DEFAULT_SEGMENT_INDEX = Path(_DEFAULTS.get("segment_index", str(DEFAULT_PROJECT_ROOT / "alignment_segments.json")))
DEFAULT_VO_AUDIO = Path(_DEFAULTS.get("vo_audio", str(DEFAULT_PROJECT_ROOT / "audio/voice_over.wav")))
DEFAULT_OUTPUT_DIR = Path(_DEFAULTS.get("output_dir", str(DEFAULT_PROJECT_ROOT / "outputs")))
DEFAULT_OUTPUT_FCPXML = Path(_DEFAULTS.get("output_fcpxml", str(DEFAULT_OUTPUT_DIR / "core_edit_focus.fcpxml")))
DEFAULT_NOTES = Path(_DEFAULTS.get("director_notes", str(DEFAULT_OUTPUT_DIR / "director_notes_focus.json")))
DEFAULT_MANIFEST_PATH = Path(_DEFAULTS.get("manifest_path", str(DEFAULT_OUTPUT_DIR / "footage_manifest.ndjson")))
DEFAULT_MANIFEST_HANDLE = Path(_DEFAULTS.get("manifest_handle", str(DEFAULT_OUTPUT_DIR / "footage_manifest.handle.json")))
DEFAULT_PROJECT_NAME = _DEFAULTS.get("project_name", "Auto-Edit")


def _parse_hhmmss(token: str) -> float:
    parts = token.strip().split(":")
    if len(parts) != 3:
        raise argparse.ArgumentTypeError(
            f"Invalid timecode '{token}'. Expected HH:MM:SS format."
        )
    hours, minutes, seconds = parts
    try:
        h = int(hours)
        m = int(minutes)
        s = float(seconds)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"Invalid timecode '{token}'. Components must be numeric."
        ) from exc
    return h * 3600 + m * 60 + s


def _parse_time_range(value: Optional[str]) -> Optional[Tuple[float, float]]:
    if value is None:
        return None
    if "-" not in value:
        raise argparse.ArgumentTypeError(
            "--time-range must be formatted as HH:MM:SS-HH:MM:SS"
        )
    start_raw, end_raw = value.split("-", 1)
    start = _parse_hhmmss(start_raw)
    end = _parse_hhmmss(end_raw)
    if start >= end:
        raise argparse.ArgumentTypeError("time-range start must be before end")
    return start, end


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build a Core Edit timeline using production planners."
    )
    parser.add_argument("--beat-map", type=Path, default=DEFAULT_BEAT_MAP)
    parser.add_argument("--segment-index", type=Path, default=DEFAULT_SEGMENT_INDEX)
    parser.add_argument("--vo-audio", type=Path, default=DEFAULT_VO_AUDIO)
    parser.add_argument("--output-fcpxml", type=Path, default=DEFAULT_OUTPUT_FCPXML)
    parser.add_argument("--director-notes", type=Path, default=DEFAULT_NOTES)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="Directory used for default outputs if explicit paths are omitted.",
    )
    parser.add_argument("--project-name", default=DEFAULT_PROJECT_NAME)
    parser.add_argument("--timeline-name", default="Core Edit V1")
    parser.add_argument(
        "--planner-mode",
        choices=("legacy", "assembly"),
        default="legacy",
        help="Use the legacy shot planner or the metadata-first rough-cut assembler.",
    )
    parser.add_argument(
        "--target-beat",
        action="append",
        dest="target_beats",
        help="Beat ID to include (use multiple times for more than one).",
    )
    parser.add_argument(
        "--time-range",
        type=str,
        help="Limit narration to HH:MM:SS-HH:MM:SS (timeline seconds).",
    )
    parser.add_argument(
        "--manual-plan",
        type=str,
        help="Optional import path MODULE:FUNC that returns manual overrides when given the segment lookup.",
    )
    parser.add_argument(
        "--manifest-path",
        type=Path,
        default=DEFAULT_MANIFEST_PATH,
        help="Path to the required footage manifest (NDJSON).",
    )
    parser.add_argument(
        "--manifest-handle",
        type=Path,
        default=DEFAULT_MANIFEST_HANDLE,
        help="Path to the saved Gemini Files manifest handle (optional).",
    )
    parser.add_argument(
        "--shot-plans-dir",
        type=Path,
        help="Directory to persist per-beat shot plans (defaults to <output_dir>/shot_plans).",
    )
    parser.add_argument(
        "--shot-debug-dir",
        type=Path,
        help="Directory to store retrieval/selection traces (defaults to <output_dir>/shot_debug).",
    )
    parser.add_argument("--profile-name", help="Event metadata profile for assembly mode.")
    parser.add_argument("--database-path", type=Path, help="Event metadata SQLite path for assembly mode.")
    parser.add_argument("--limit-per-slot", type=int, default=8)
    parser.add_argument("--proposal-path", type=Path)
    parser.add_argument("--semantic-edit-path", type=Path)
    parser.add_argument("--review-report-path", type=Path)
    parser.add_argument("--live-gemini-review", action="store_true")
    parser.add_argument("--live-resolve-feedback", action="store_true")
    parser.add_argument("--resolve-project-name")
    parser.add_argument("--resolve-timeline-name")
    parser.add_argument("--resolve-feedback-path", type=Path)
    parser.add_argument("--resolve-snapshot-path", type=Path)
    parser.add_argument("--resolve-render-path", type=Path)
    parser.add_argument("--render-proxy", action="store_true")
    return parser


def main() -> None:
    load_dotenv(Path(__file__).resolve().parents[3] / ".env")
    parser = build_parser()
    args = parser.parse_args()

    time_range = _parse_time_range(args.time_range)

    if args.output_dir:
        args.output_dir.mkdir(parents=True, exist_ok=True)
    if args.output_fcpxml:
        args.output_fcpxml.parent.mkdir(parents=True, exist_ok=True)
    if args.director_notes:
        args.director_notes.parent.mkdir(parents=True, exist_ok=True)

    target_beats = set(args.target_beats) if args.target_beats else None

    if args.planner_mode == "assembly":
        _run_assembly_mode(args, target_beats=target_beats, time_range=time_range)
        return

    manual_plan: Optional[Dict[str, List[ManualClipPlacement]]] = None
    if args.manual_plan:
        if ":" not in args.manual_plan:
            raise argparse.ArgumentTypeError("--manual-plan must be formatted as module:function")
        module_name, func_name = args.manual_plan.split(":", 1)
        module = import_module(module_name)
        factory: Callable[[Dict[str, Dict[str, float]]], Dict[str, List[ManualClipPlacement]]] = getattr(module, func_name)
        segment_lookup = core_editor.load_segment_index(args.segment_index)
        manual_plan = factory(segment_lookup)

    try:
        manifest_entries = footage_manifest.read_manifest(args.manifest_path)
    except FileNotFoundError:
        raise SystemExit(
            f"[build_core_edit] Manifest missing. Run build_footage_manifest first: {args.manifest_path}"
        )
    except ValueError as exc:
        raise SystemExit(f"[build_core_edit] Manifest invalid: {exc}")

    print(
        f"[build_core_edit] Loaded manifest with {len(manifest_entries)} entries from {args.manifest_path}"
    )
    print(footage_manifest.format_manifest_preview(manifest_entries))

    manifest_handle_path: Optional[Path] = None
    if args.manifest_handle:
        manifest_handle_path = args.manifest_handle
        if not args.manifest_handle.exists():
            print(
                f"[build_core_edit] Manifest handle not found at {args.manifest_handle}; a new handle will be saved."
            )

    shot_plans_dir: Optional[Path] = args.shot_plans_dir or (args.output_dir / "shot_plans" if args.output_dir else None)
    shot_debug_dir: Optional[Path] = args.shot_debug_dir or (args.output_dir / "shot_debug" if args.output_dir else None)

    semantic_edit = core_editor.generate_core_edit(
        beat_map_path=args.beat_map,
        segment_index_path=args.segment_index,
        project_name=args.project_name,
        timeline_name=args.timeline_name,
        target_beats=target_beats,
        time_range=time_range,
        vo_audio_path=args.vo_audio,
        director_notes_path=args.director_notes,
        manual_clip_overrides=manual_plan,
        manifest_path=args.manifest_path,
        manifest_handle_path=manifest_handle_path,
        shot_plans_dir=shot_plans_dir,
        shot_debug_dir=shot_debug_dir,
    )

    timeline = builder.build_otio_timeline_from_model(semantic_edit)
    fcpxml_path = builder.export_fcpxml(timeline, args.output_fcpxml)

    print(
        "[build_core_edit] Exported FCPXML to",
        fcpxml_path,
    )
    print("[build_core_edit] Director notes at", args.director_notes)


def _run_assembly_mode(
    args: argparse.Namespace,
    *,
    target_beats: set[str] | None,
    time_range: tuple[float, float] | None,
) -> None:
    proposal_path = args.proposal_path or args.output_dir / "assembly_proposal.json"
    semantic_edit_path = args.semantic_edit_path or args.output_dir / "assembly_semantic_edit.json"
    review_report_path = args.review_report_path or args.output_dir / "assembly_review_report.json"
    quality_report_path = args.output_dir / "assembly_quality_report.json"
    resolve_feedback_path = args.resolve_feedback_path or args.output_dir / "assembly_resolve_feedback.json"
    resolve_snapshot_path = args.resolve_snapshot_path or args.output_dir / "assembly_resolve_snapshot.json"
    resolve_render_path = args.resolve_render_path or args.output_dir / "assembly_resolve_proxy.mp4"

    for path in (
        proposal_path,
        semantic_edit_path,
        review_report_path,
        quality_report_path,
        args.output_fcpxml,
        args.director_notes,
        resolve_feedback_path,
        resolve_snapshot_path,
        resolve_render_path,
    ):
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)

    beat_map = models.BeatMap.model_validate_json(args.beat_map.read_text(encoding="utf-8"))
    segment_lookup = _load_segment_lookup(args.segment_index)
    shot_plans_dir: Optional[Path] = args.shot_plans_dir or (args.output_dir / "shot_plans")
    shot_plans = _load_shot_plan_lookup(beat_map, shot_plans_dir)
    slots = build_slots_from_beat_map(beat_map, segment_lookup, shot_plans=shot_plans)
    slots = _filter_slots(slots, target_beats=target_beats, time_range=time_range)

    with CandidateMatrixBuilder(
        profile_name=args.profile_name,
        manifest_path=args.manifest_path,
        database_path=args.database_path,
    ) as matrix_builder:
        proposal = matrix_builder.build_proposal(
            project_name=args.project_name,
            slots=slots,
            limit_per_slot=args.limit_per_slot,
        )

    proposal_path.write_text(proposal.model_dump_json(indent=2), encoding="utf-8")
    print(f"[build_core_edit] Assembly proposal at {proposal_path}")
    quality_report = audit_assembly_quality(proposal)
    write_quality_report(quality_report, quality_report_path)
    print(f"[build_core_edit] Assembly quality report at {quality_report_path}")

    review_report = gemini_review_proposal(proposal) if args.live_gemini_review else review_proposal(proposal)
    review_report_path.write_text(review_report.model_dump_json(indent=2), encoding="utf-8")
    review_mode = "Gemini" if args.live_gemini_review else "deterministic"
    print(f"[build_core_edit] Assembly {review_mode} review at {review_report_path}")

    semantic_edit = proposal_to_semantic_edit(
        proposal,
        vo_audio_path=args.vo_audio,
        director_notes_path=args.director_notes,
        timeline_name=args.timeline_name,
    )
    semantic_edit_path.write_text(semantic_edit.model_dump_json(indent=2), encoding="utf-8")
    print(f"[build_core_edit] Assembly SemanticEdit at {semantic_edit_path}")

    timeline = builder.build_otio_timeline_from_model(semantic_edit)
    fcpxml_path = builder.export_fcpxml(timeline, args.output_fcpxml)
    print("[build_core_edit] Exported FCPXML to", fcpxml_path)
    print("[build_core_edit] Director notes at", args.director_notes)

    if args.live_resolve_feedback:
        resolve_project = args.resolve_project_name or args.project_name
        feedback = run_resolve_feedback(
            proposal,
            resolve_tools=ResolveTools(project_name=resolve_project),
            fcpxml_path=fcpxml_path,
            snapshot_path=resolve_snapshot_path,
            timeline_name=args.resolve_timeline_name or args.timeline_name,
            render_proxy=args.render_proxy,
            render_path=resolve_render_path,
        )
        resolve_feedback_path.write_text(feedback.model_dump_json(indent=2), encoding="utf-8")
        print(f"[build_core_edit] Resolve feedback at {resolve_feedback_path}")


def _load_segment_lookup(path: Path) -> dict[str, dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    raw_segments = payload.get("segments", payload if isinstance(payload, list) else [])
    return {str(segment["id"]): segment for segment in raw_segments if "id" in segment}


def _load_shot_plan_lookup(
    beat_map: models.BeatMap,
    shot_plans_dir: Optional[Path],
) -> dict[str, models.ShotPlan]:
    if shot_plans_dir is None or not shot_plans_dir.exists():
        return {}

    shot_plans: dict[str, models.ShotPlan] = {}
    for beat in beat_map.beats:
        path = shot_plans_dir / f"{beat.id}.json"
        if not path.exists():
            continue
        try:
            shot_plan = models.ShotPlan.model_validate_json(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if shot_plan.beat_id != beat.id or not shot_plan.shot_instructions:
            continue
        shot_plans[beat.id] = shot_plan
    return shot_plans


def _filter_slots(
    slots,
    *,
    target_beats: set[str] | None,
    time_range: tuple[float, float] | None,
):
    filtered = list(slots)
    if target_beats:
        filtered = [
            slot
            for slot in filtered
            if slot.beat_id in target_beats or slot.slot_id in target_beats
        ]
    if time_range is not None:
        start, end = time_range
        filtered = [
            slot
            for slot in filtered
            if slot.timeline_start < end and slot.timeline_start + slot.timeline_duration > start
        ]
    return filtered


if __name__ == "__main__":
    main()
