"""CLI helper to inspect beat planning/debug artifacts."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict, List

from orchestrator import core_editor

DEFAULT_PROJECT_ROOT = Path("samples/feixiao test edit")
DEFAULT_BEAT_MAP = DEFAULT_PROJECT_ROOT / "script_breakdown.json"
DEFAULT_SEGMENT_INDEX = DEFAULT_PROJECT_ROOT / "alignment_segments.json"
DEFAULT_OUTPUT_DIR = DEFAULT_PROJECT_ROOT / "outputs"
DEFAULT_NOTES = DEFAULT_OUTPUT_DIR / "director_notes_focus.json"


def _load_json(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _fmt_seconds(value: float) -> str:
    minutes, seconds = divmod(value, 60)
    hours, minutes = divmod(int(minutes), 60)
    if hours:
        return f"{hours:d}:{minutes:02d}:{seconds:05.2f}"
    return f"{minutes:d}:{seconds:05.2f}"


def _print_header(title: str) -> None:
    print("")
    print(f"=== {title} ===")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Inspect a single beat's planning/debug artifacts.")
    parser.add_argument("--beat-id", required=True, help="Beat identifier to inspect (e.g., B003)")
    parser.add_argument("--beat-map", type=Path, default=DEFAULT_BEAT_MAP)
    parser.add_argument("--segment-index", type=Path, default=DEFAULT_SEGMENT_INDEX)
    parser.add_argument("--director-notes", type=Path, default=DEFAULT_NOTES)
    parser.add_argument("--shot-plans-dir", type=Path, help="Directory containing per-beat shot plans")
    parser.add_argument("--shot-debug-dir", type=Path, help="Directory containing per-shot debug traces")
    return parser


def _print_beat_summary(beat: Dict[str, Any]) -> None:
    _print_header(f"Beat {beat['id']} Summary")
    print(f"Visual intent: {beat.get('visual_intent')}")
    print(f"Pacing: {beat.get('pacing')}")
    queries = beat.get('search_queries') or []
    if queries:
        print("Search queries:")
        for query in queries:
            print(f"  - {query}")
    segments = beat.get('segment_ids') or []
    print(f"Narration segments: {', '.join(segments)}")


def _print_narration_segments(beat: Dict[str, Any], segment_index: Dict[str, Dict[str, Any]]) -> None:
    _print_header("Narration Lines")
    for seg_id in beat.get("segment_ids", []):
        segment = segment_index.get(seg_id)
        if not segment:
            print(f"  - {seg_id}: (missing)")
            continue
        start = _fmt_seconds(float(segment.get("t0", 0.0)))
        end = _fmt_seconds(float(segment.get("t1", segment.get("t0", 0.0))))
        text = segment.get("text", "").strip()
        print(f"  - {seg_id} [{start} – {end}]: {text}")


def _load_shot_plan(beat_id: str, shot_plans_dir: Path | None) -> Dict[str, Any] | None:
    if not shot_plans_dir:
        return None
    path = shot_plans_dir / f"{beat_id}.json"
    if not path.exists():
        return None
    return _load_json(path)


def _load_debug_payload(debug_dir: Path | None, beat_id: str, shot_id: str) -> Dict[str, Any] | None:
    if not debug_dir:
        return None
    path = debug_dir / beat_id / f"{shot_id}.json"
    if not path.exists():
        return None
    return _load_json(path)


def _print_shot_plan(beat_id: str, plan_data: Dict[str, Any] | None, debug_dir: Path | None) -> None:
    if not plan_data:
        _print_header("Shot Plan")
        print("(no shot plan on disk)")
        return
    _print_header("Shot Plan")
    instructions: List[Dict[str, Any]] = plan_data.get("shot_instructions", [])
    for shot in instructions:
        shot_id = shot.get("shot_id")
        narration_ids = ", ".join(shot.get("narration_ids", []))
        print(f"- {shot_id}: {shot.get('visual_goal')}")
        print(f"    Narration IDs: {narration_ids}")
        excerpt = (shot.get("vo_excerpt") or "").strip()
        if excerpt:
            print(f"    VO excerpt: {excerpt}")
        preferred = shot.get("preferred_assets") or []
        if preferred:
            print(f"    Preferred assets: {', '.join(preferred)}")
        debug_payload = _load_debug_payload(debug_dir, beat_id, shot_id) if shot_id else None
        if debug_payload:
            query = debug_payload.get("query")
            if query:
                print(f"    Retrieval query: {query}")
            decision = debug_payload.get("decision")
            if decision:
                start = decision.get("start")
                end = decision.get("end")
                asset = decision.get("asset_id")
                if start is not None and end is not None:
                    window = f"[{start:.2f}s – {end:.2f}s]"
                else:
                    window = "[timestamps unavailable]"
                reasoning = decision.get("reasoning", "no reasoning provided")
                status = debug_payload.get("status")
                print(f"    Decision: {asset} {window} ({reasoning}), status={status}")
            candidates = debug_payload.get("candidate_slices") or []
            if candidates:
                print("    Candidate slices (top 3):")
                for slice_info in candidates[:3]:
                    print(
                        f"      * {slice_info['asset_id']} [{slice_info['slice_start']:.2f}s – {slice_info['slice_end']:.2f}s] score={slice_info['score']:.3f}"
                    )
        else:
            print("    (no debug trace found)")


def _print_director_notes(beat_id: str, notes_path: Path) -> None:
    if not notes_path.exists():
        return
    notes = _load_json(notes_path)
    entries = [entry for entry in notes.get("entries", []) if entry.get("beat_id") == beat_id]
    if not entries:
        return
    _print_header("Director Notes")
    for entry in entries:
        clip = entry.get("clip_asset_id")
        clip_range = entry.get("clip_range", {})
        start = clip_range.get("start", 0.0)
        duration = clip_range.get("duration", 0.0)
        print(f"- {entry.get('shot_id')}: {clip} [{start:.2f}s – {start + duration:.2f}s]")
        print(f"    Coverage: {entry.get('coverage_note')}")
        print(f"    Visuals: {entry.get('visual_summary')}")


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    beat_map = _load_json(args.beat_map)
    beats = {beat["id"]: beat for beat in beat_map.get("beats", [])}
    beat = beats.get(args.beat_id)
    if not beat:
        raise SystemExit(f"Beat {args.beat_id} not found in {args.beat_map}")

    segment_index = core_editor.load_segment_index(args.segment_index)
    shot_plans_dir = args.shot_plans_dir or (DEFAULT_OUTPUT_DIR / "shot_plans")
    shot_debug_dir = args.shot_debug_dir or (DEFAULT_OUTPUT_DIR / "shot_debug")

    _print_beat_summary(beat)
    _print_narration_segments(beat, segment_index)
    shot_plan = _load_shot_plan(args.beat_id, shot_plans_dir)
    _print_shot_plan(args.beat_id, shot_plan, shot_debug_dir)
    if args.director_notes:
        _print_director_notes(args.beat_id, args.director_notes)

if __name__ == "__main__":
    main()
