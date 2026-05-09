"""Build and optionally live-validate a metadata-first rough-cut assembly proposal."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from gateway.otio_fcpxml import builder as otio_builder
from orchestrator import models
from orchestrator.assembly.candidate_matrix import CandidateMatrixBuilder, build_slots_from_beat_map
from orchestrator.assembly.export import proposal_to_semantic_edit
from orchestrator.assembly.feedback import run_resolve_feedback
from orchestrator.assembly.review import gemini_review_proposal, review_proposal
from orchestrator.tool_bridge.resolve_tools import ResolveTools

DEFAULT_CONFIG_PATH = Path("profiles/default_core_edit.json")


def _load_default_config() -> dict[str, str]:
    if not DEFAULT_CONFIG_PATH.exists():
        return {}
    return json.loads(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))


_DEFAULTS = _load_default_config()
DEFAULT_PROJECT_ROOT = Path(_DEFAULTS.get("project_root", "samples/default edit"))
DEFAULT_BEAT_MAP = Path(_DEFAULTS.get("beat_map", str(DEFAULT_PROJECT_ROOT / "script_breakdown.json")))
DEFAULT_SEGMENT_INDEX = Path(_DEFAULTS.get("segment_index", str(DEFAULT_PROJECT_ROOT / "alignment_segments.json")))
DEFAULT_MANIFEST_PATH = Path(_DEFAULTS.get("manifest_path", str(DEFAULT_PROJECT_ROOT / "outputs/footage_manifest.ndjson")))
DEFAULT_OUTPUT_DIR = Path(_DEFAULTS.get("output_dir", str(DEFAULT_PROJECT_ROOT / "outputs")))
DEFAULT_VO_AUDIO = Path(_DEFAULTS.get("vo_audio", str(DEFAULT_PROJECT_ROOT / "audio/voice_over.wav")))
DEFAULT_PROJECT_NAME = _DEFAULTS.get("project_name", "Auto-Edit")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build a metadata-first rough-cut assembly proposal.")
    parser.add_argument("--beat-map", type=Path, default=DEFAULT_BEAT_MAP)
    parser.add_argument("--segment-index", type=Path, default=DEFAULT_SEGMENT_INDEX)
    parser.add_argument("--manifest-path", type=Path, default=DEFAULT_MANIFEST_PATH)
    parser.add_argument("--database-path", type=Path)
    parser.add_argument("--profile-name")
    parser.add_argument("--project-name", default=DEFAULT_PROJECT_NAME)
    parser.add_argument("--vo-audio", type=Path, default=DEFAULT_VO_AUDIO)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--proposal-path", type=Path)
    parser.add_argument("--semantic-edit-path", type=Path)
    parser.add_argument("--director-notes-path", type=Path)
    parser.add_argument("--review-report-path", type=Path)
    parser.add_argument("--fcpxml-path", type=Path)
    parser.add_argument("--resolve-feedback-path", type=Path)
    parser.add_argument("--resolve-snapshot-path", type=Path)
    parser.add_argument("--resolve-render-path", type=Path)
    parser.add_argument("--resolve-project-name")
    parser.add_argument("--resolve-timeline-name")
    parser.add_argument("--limit-per-slot", type=int, default=8)
    parser.add_argument("--target-beat", action="append", dest="target_beats")
    parser.add_argument("--time-range", help="Limit narration to HH:MM:SS-HH:MM:SS.")
    parser.add_argument("--live-gemini", action="store_true")
    parser.add_argument("--live-resolve", action="store_true")
    parser.add_argument("--render-proxy", action="store_true")
    return parser


def main() -> None:
    repo_root = Path(__file__).resolve().parents[3]
    load_dotenv(repo_root / ".env")
    args = build_parser().parse_args()

    args.output_dir.mkdir(parents=True, exist_ok=True)
    proposal_path = args.proposal_path or args.output_dir / "assembly_proposal.json"
    semantic_edit_path = args.semantic_edit_path or args.output_dir / "assembly_semantic_edit.json"
    director_notes_path = args.director_notes_path or args.output_dir / "assembly_director_notes.json"
    review_report_path = args.review_report_path or args.output_dir / "assembly_review_report.json"
    fcpxml_path = args.fcpxml_path or args.output_dir / "assembly_rough_cut.fcpxml"
    resolve_feedback_path = args.resolve_feedback_path or args.output_dir / "assembly_resolve_feedback.json"
    resolve_snapshot_path = args.resolve_snapshot_path or args.output_dir / "assembly_resolve_snapshot.json"
    resolve_render_path = args.resolve_render_path or args.output_dir / "assembly_resolve_proxy.mp4"

    beat_map = models.BeatMap.model_validate_json(args.beat_map.read_text(encoding="utf-8"))
    segment_lookup = _load_segment_lookup(args.segment_index)
    slots = build_slots_from_beat_map(beat_map, segment_lookup)
    slots = _filter_slots(slots, target_beats=set(args.target_beats or []), time_range=_parse_time_range(args.time_range))

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

    proposal_path.parent.mkdir(parents=True, exist_ok=True)
    proposal_path.write_text(proposal.model_dump_json(indent=2), encoding="utf-8")
    print(f"[assembly] Proposal at {proposal_path}")

    review_report = gemini_review_proposal(proposal) if args.live_gemini else review_proposal(proposal)
    review_report_path.parent.mkdir(parents=True, exist_ok=True)
    review_report_path.write_text(review_report.model_dump_json(indent=2), encoding="utf-8")
    review_mode = "Gemini" if args.live_gemini else "deterministic"
    print(f"[assembly] {review_mode} review at {review_report_path}")

    semantic_edit = proposal_to_semantic_edit(
        proposal,
        vo_audio_path=args.vo_audio,
        director_notes_path=director_notes_path,
    )
    semantic_edit_path.write_text(semantic_edit.model_dump_json(indent=2), encoding="utf-8")
    print(f"[assembly] SemanticEdit at {semantic_edit_path}")
    timeline = otio_builder.build_otio_timeline_from_model(semantic_edit)
    exported_path = otio_builder.export_fcpxml(timeline, fcpxml_path)
    print(f"[assembly] FCPXML at {exported_path}")

    if args.live_resolve:
        resolve_project = args.resolve_project_name or args.project_name
        feedback = run_resolve_feedback(
            proposal,
            resolve_tools=ResolveTools(project_name=resolve_project),
            fcpxml_path=exported_path,
            snapshot_path=resolve_snapshot_path,
            timeline_name=args.resolve_timeline_name,
            render_proxy=args.render_proxy,
            render_path=resolve_render_path,
        )
        resolve_feedback_path.write_text(feedback.model_dump_json(indent=2), encoding="utf-8")
        print(f"[assembly] Resolve feedback at {resolve_feedback_path}")


def _load_segment_lookup(path: Path) -> dict[str, dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    raw_segments = payload.get("segments", payload if isinstance(payload, list) else [])
    return {str(segment["id"]): segment for segment in raw_segments if "id" in segment}


def _filter_slots(slots, *, target_beats: set[str], time_range: tuple[float, float] | None):
    filtered = list(slots)
    if target_beats:
        filtered = [slot for slot in filtered if slot.beat_id in target_beats or slot.slot_id in target_beats]
    if time_range is not None:
        start, end = time_range
        filtered = [
            slot
            for slot in filtered
            if slot.timeline_start < end and slot.timeline_start + slot.timeline_duration > start
        ]
    return filtered


def _parse_time_range(value: str | None) -> tuple[float, float] | None:
    if value is None:
        return None
    if "-" not in value:
        raise argparse.ArgumentTypeError("--time-range must be formatted as HH:MM:SS-HH:MM:SS")
    start_raw, end_raw = value.split("-", 1)
    start = _parse_hhmmss(start_raw)
    end = _parse_hhmmss(end_raw)
    if start >= end:
        raise argparse.ArgumentTypeError("time-range start must be before end")
    return start, end


def _parse_hhmmss(token: str) -> float:
    parts = token.strip().split(":")
    if len(parts) != 3:
        raise argparse.ArgumentTypeError(f"Invalid timecode '{token}'.")
    hours, minutes, seconds = parts
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


if __name__ == "__main__":
    main()
