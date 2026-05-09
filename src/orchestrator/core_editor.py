"""
Core Edit Generator for Auto-Editor.

This module implements the "Phase 1" logic:
1. Reads the "Beat Map" (Director's Plan).
2. Reads the "Segment Index" (Timekeeper).
3. Searches the Vector DB for matching footage for each beat.
4. Assembles a SemanticEdit model for the OTIO builder.
"""

import json
import logging
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from src.orchestrator import models
from src.orchestrator.clip_chooser import (
    ClipChooser,
    ClipSliceDecision,
    _desired_visual_mode,
    _classify_visual_mode,
    _matches_any_constraint,
)
from src.orchestrator.clip_slices import ClipSlice, generate_candidate_slices
from src.orchestrator.director_notes import DirectorNotesBuilder
from src.orchestrator.retrieval.constraints import (
    extract_shot_constraints,
    filter_candidates_by_constraints,
)
from src.orchestrator.shot_planner import ShotPlanner
from src.orchestrator.tool_bridge import retrieval_tools

LOGGER = logging.getLogger(__name__)
_SELECTION_RULES_VERSION = "explicit-constraints-v1"

def load_beat_map(path: Path) -> models.BeatMap:
    """Loads the Beat Map JSON."""
    with open(path, 'r', encoding='utf-8') as f:
        data = json.load(f)
    return models.BeatMap(**data)


def load_segment_index(path: Path) -> Dict[str, Dict[str, Any]]:
    """Loads the segment index and returns a lookup dict by ID."""
    with open(path, 'r', encoding='utf-8') as f:
        data = json.load(f)

    lookup: Dict[str, Dict[str, Any]] = {}
    
    # Support both "segments" (simple) and "aligned" (full) formats
    source_list = data.get("segments") or data.get("aligned") or []
    
    for seg in source_list:
        lookup[seg["id"]] = seg
    return lookup


@dataclass
class ManualClipPlacement:
    clip: models.ClipSpec
    visual_summary: str
    coverage_note: Optional[str] = None
    reasoning: str = "Manual selection"


@dataclass
class CandidatePoolResolution:
    search_mode: str
    candidates: List[ClipSlice]
    debug: Dict[str, Any]
    failure_reason: Optional[str] = None


@dataclass
class SelectionResult:
    clips: List[models.ClipSpec]
    slices: List[ClipSlice]
    decisions: List[ClipSliceDecision]
    failure_reason: Optional[str] = None
    retried_with_used_assets: bool = False
    selection_limit_reason: Optional[str] = None

def generate_core_edit(
    beat_map_path: Path,
    segment_index_path: Path,
    project_name: str = "Auto-Edit",
    timeline_name: str = "Core Edit",
    *,
    target_beats: Optional[Set[str]] = None,
    time_range: Optional[Tuple[float, float]] = None,
    vo_audio_path: Optional[Path] = None,
    director_notes_path: Optional[Path] = None,
    shot_planner: Optional[ShotPlanner] = None,
    manual_clip_overrides: Optional[Dict[str, List[ManualClipPlacement]]] = None,
    manifest_path: Optional[Path] = None,
    manifest_handle_path: Optional[Path] = None,
    shot_plans_dir: Optional[Path] = None,
    shot_debug_dir: Optional[Path] = None,
) -> models.SemanticEdit:
    """Build a semantic edit focusing on the requested beats."""

    print(f"[CoreEditor] Loading Beat Map from {beat_map_path.name}...")
    beat_map = load_beat_map(beat_map_path)

    print(f"[CoreEditor] Loading Segment Index from {segment_index_path.name}...")
    segment_lookup = load_segment_index(segment_index_path)

    planner = shot_planner or ShotPlanner(
        manifest_path=manifest_path,
        manifest_handle_path=manifest_handle_path,
        shot_plans_dir=shot_plans_dir,
    )
    clip_chooser = ClipChooser()
    notes_builder = DirectorNotesBuilder(project_name=project_name, timeline_name=timeline_name)

    edit_segments: List[models.EditSegment] = []
    used_assets: Set[str] = set()

    allowed_segments: Optional[Set[str]] = None
    if time_range is not None:
        start, end = time_range
        if start >= end:
            raise ValueError("time_range start must be before end")
        allowed_segments = set()
        for seg_id, seg in segment_lookup.items():
            seg_start = float(seg.get("t0", 0.0))
            seg_end = float(seg.get("t1", seg_start))
            if max(seg_start, start) < min(seg_end, end):
                allowed_segments.add(seg_id)

        print(
            f"[CoreEditor] Time-range filter active: start={start:.2f}s end={end:.2f}s | "
            f"segments kept={len(allowed_segments)}"
        )

    def _segment_allowed(seg_id: str) -> bool:
        return allowed_segments is None or seg_id in allowed_segments

    beats_iter = beat_map.beats
    if target_beats:
        beats_iter = [beat for beat in beat_map.beats if beat.id in target_beats]

    total_beats = len(beats_iter)
    print(f"[CoreEditor] Generating {total_beats} beat(s) -> timeline '{timeline_name}'")

    for beat_idx, beat in enumerate(beats_iter, start=1):
        LOGGER.info(
            "[CoreEditor] Beat %d/%d %s — %s",
            beat_idx,
            total_beats,
            beat.id,
            beat.visual_intent or "(no intent)",
        )
        segment_context = [
            segment_lookup[sid]
            for sid in beat.segment_ids
            if sid in segment_lookup and _segment_allowed(sid)
        ]

        if not segment_context:
            LOGGER.warning("Beat %s has no segment context after filtering", beat.id)
            continue

        shot_plan = planner.plan_for_beat(beat=beat, segment_context=segment_context)

        for shot_idx, shot in enumerate(shot_plan.shot_instructions, start=1):
            LOGGER.info(
                "[CoreEditor] Beat %s: shot %d/%d (%s) covering %s",
                beat.id,
                shot_idx,
                len(shot_plan.shot_instructions),
                shot.shot_id,
                ",".join(shot.narration_ids),
            )
            filtered_ids = [sid for sid in shot.narration_ids if sid in segment_lookup and _segment_allowed(sid)]
            if not filtered_ids:
                LOGGER.warning("Shot %s lost all narration IDs after filtering", shot.shot_id)
                continue

            segs = [segment_lookup[sid] for sid in filtered_ids]
            if not segs:
                LOGGER.warning("Shot %s references missing narration IDs", shot.shot_id)
                continue

            if filtered_ids != shot.narration_ids:
                try:
                    shot = shot.model_copy(update={"narration_ids": filtered_ids})  # type: ignore[attr-defined]
                except AttributeError:  # pragma: no cover - pydantic v1 fallback
                    shot = shot.copy(update={"narration_ids": filtered_ids})

            t0 = min(seg["t0"] for seg in segs)
            t1 = max(seg["t1"] for seg in segs)
            duration = max(t1 - t0, 0.1)
            vo_excerpt = " ".join(seg.get("text", "") for seg in segs)
            vo_range = models.TimeSpan(start=t0, duration=duration)

            narration_audio = None
            if vo_audio_path:
                narration_audio = models.AudioClipSpec(
                    asset_id=str(vo_audio_path.resolve()),
                    source_range=models.SourceRange(start=t0, duration=duration),
                )

            video_clips = _resolve_shot_clips(
                beat=beat,
                shot=shot,
                used_assets=used_assets,
                vo_excerpt=vo_excerpt,
                vo_range=vo_range,
                notes_builder=notes_builder,
                clip_chooser=clip_chooser,
                manual_overrides=manual_clip_overrides,
                manifest_path=manifest_path,
                debug_dir=shot_debug_dir,
            )

            segment = models.EditSegment(
                id=shot.shot_id,
                shot_id=shot.shot_id,
                narration_span=vo_range,
                narration_audio=narration_audio,
                video_clips=video_clips,
            )

            if video_clips:
                segment.primary_clip = video_clips[0]

            edit_segments.append(segment)

    semantic_edit = models.SemanticEdit(
        project_name=project_name,
        fps=30.0,
        segments=edit_segments,
    )

    if director_notes_path:
        notes_builder.write_json(director_notes_path)
        md_path = director_notes_path.with_suffix(".md")
        notes_builder.write_markdown(md_path)

    return semantic_edit


def _resolve_shot_clips(
    *,
    beat: models.Beat,
    shot: models.ShotInstruction,
    used_assets: Set[str],
    vo_excerpt: str,
    vo_range: models.TimeSpan,
    notes_builder: DirectorNotesBuilder,
    clip_chooser: ClipChooser,
    manual_overrides: Optional[Dict[str, List[ManualClipPlacement]]] = None,
    manifest_path: Optional[Path] = None,
    debug_dir: Optional[Path] = None,
) -> List[models.ClipSpec]:
    """Select clips for a shot and log them into the director notes."""

    override_entries: List[ManualClipPlacement] = []
    if manual_overrides:
        # Shot-specific overrides win but can be combined with per-line entries.
        shot_entries = manual_overrides.get(shot.shot_id, [])
        override_entries.extend(shot_entries)
        for seg_id in shot.narration_ids:
            seg_entries = manual_overrides.get(seg_id)
            if seg_entries:
                override_entries.extend(seg_entries)

    if override_entries:
        selections: List[models.ClipSpec] = []
        for entry in override_entries:
            clip_spec = entry.clip
            selections.append(clip_spec)
            notes_builder.add_entry(
                beat_id=beat.id,
                shot_id=shot.shot_id,
                narration_ids=shot.narration_ids,
                vo_excerpt=vo_excerpt,
                vo_range=vo_range,
                clip_spec=clip_spec,
                visual_summary=entry.visual_summary,
                coverage_note=entry.coverage_note or shot.coverage_note,
                reasoning=entry.reasoning,
            )
            _log_director_note(
                beat_id=beat.id,
                shot_id=shot.shot_id,
                vo_range=vo_range,
                clip_spec=clip_spec,
                visual_summary=entry.visual_summary,
                coverage_note=entry.coverage_note or shot.coverage_note,
                reasoning=entry.reasoning,
            )
        return selections

    query_parts: List[str] = [beat.visual_intent, shot.visual_goal]
    if shot.required_events:
        query_parts.append("Events: " + ", ".join(shot.required_events))
    if shot.keywords:
        query_parts.append("Keywords: " + ", ".join(shot.keywords))
    query = " | ".join(part for part in query_parts if part)
    constraints = extract_shot_constraints(
        visual_goal=shot.visual_goal,
        coverage_note=shot.coverage_note,
        beat_intent=beat.visual_intent,
        vo_excerpt=vo_excerpt,
        required_events=shot.required_events,
        keywords=shot.keywords,
    )

    debug_entry: Dict[str, Any] = {
        "beat_id": beat.id,
        "shot_id": shot.shot_id,
        "selection_rules_version": _SELECTION_RULES_VERSION,
        "query": query,
        "narration_ids": shot.narration_ids,
        "visual_goal": shot.visual_goal,
        "vo_excerpt": vo_excerpt,
        "vo_range": {"start": vo_range.start, "duration": vo_range.duration},
        "constraints": constraints.to_debug(),
        "retrieval_results": [],
        "candidate_slices": [],
        "status": "pending",
    }

    cached_clips = _load_cached_shot_resolution(
        debug_dir=debug_dir,
        beat=beat,
        shot=shot,
        vo_excerpt=vo_excerpt,
        vo_range=vo_range,
        notes_builder=notes_builder,
        used_assets=used_assets,
    )
    if cached_clips is not None:
        return cached_clips

    requested_clips = max(shot.min_clips, shot.max_clips, 1)
    search_limit = max(shot.max_clips * 6, 10)
    retrieval_attempts: List[Dict[str, Any]] = []
    debug_entry["retrieval_attempts"] = retrieval_attempts

    exact_resolution = _build_candidate_pool(
        beat=beat,
        shot=shot,
        vo_range=vo_range,
        vo_excerpt=vo_excerpt,
        query=query,
        constraints=constraints,
        manifest_path=manifest_path,
        search_limit=search_limit,
        requested_clips=requested_clips,
        search_mode="explicit",
    )
    retrieval_attempts.append(exact_resolution.debug)

    candidate_resolution = exact_resolution
    fallback_reason: Optional[str] = None
    selection_result: Optional[SelectionResult] = None

    if not candidate_resolution.candidates:
        fallback_reason = candidate_resolution.failure_reason
        candidate_resolution = _build_candidate_pool(
            beat=beat,
            shot=shot,
            vo_range=vo_range,
            vo_excerpt=vo_excerpt,
            query=query,
            constraints=constraints,
            manifest_path=manifest_path,
            search_limit=search_limit,
            requested_clips=requested_clips,
            search_mode="fuzzy",
        )
        retrieval_attempts.append(candidate_resolution.debug)
        if fallback_reason:
            debug_entry["fuzzy_fallback_reason"] = fallback_reason

    if candidate_resolution.candidates:
        _apply_pool_debug(debug_entry, candidate_resolution)
        selection_result = _select_clips_from_pool(
            beat=beat,
            shot=shot,
            used_assets=used_assets,
            vo_excerpt=vo_excerpt,
            vo_range=vo_range,
            clip_chooser=clip_chooser,
            candidate_pool=candidate_resolution.candidates,
            prefer_metadata=True,
        )

    if (
        candidate_resolution.search_mode == "explicit"
        and selection_result is not None
        and not selection_result.clips
    ):
        fallback_reason = selection_result.failure_reason or "Exact metadata selection produced no usable clip"
        candidate_resolution = _build_candidate_pool(
            beat=beat,
            shot=shot,
            vo_range=vo_range,
            vo_excerpt=vo_excerpt,
            query=query,
            constraints=constraints,
            manifest_path=manifest_path,
            search_limit=search_limit,
            requested_clips=requested_clips,
            search_mode="fuzzy",
        )
        retrieval_attempts.append(candidate_resolution.debug)
        debug_entry["fuzzy_fallback_reason"] = fallback_reason
        if candidate_resolution.candidates:
            _apply_pool_debug(debug_entry, candidate_resolution)
            selection_result = _select_clips_from_pool(
                beat=beat,
                shot=shot,
                used_assets=used_assets,
                vo_excerpt=vo_excerpt,
                vo_range=vo_range,
                clip_chooser=clip_chooser,
                candidate_pool=candidate_resolution.candidates,
                prefer_metadata=True,
            )

    if not candidate_resolution.candidates or selection_result is None or not selection_result.clips:
        failure_reason = (
            (selection_result.failure_reason if selection_result else None)
            or candidate_resolution.failure_reason
            or fallback_reason
            or "No usable clip was selected"
        )
        _record_unresolved_shot(
            debug_dir=debug_dir,
            debug_entry=debug_entry,
            beat_id=beat.id,
            shot=shot,
            vo_excerpt=vo_excerpt,
            vo_range=vo_range,
            notes_builder=notes_builder,
            failure_reason=failure_reason,
        )
        return []

    selected_clips = selection_result.clips
    selected_slices = selection_result.slices
    selected_decisions = selection_result.decisions
    if selection_result.retried_with_used_assets:
        debug_entry["retried_with_used_assets"] = True

    debug_entry["status"] = "selected" if len(selected_clips) == 1 else "selected-multi"
    debug_entry["decisions"] = []
    debug_entry["clip_specs"] = []
    if selection_result.selection_limit_reason:
        debug_entry["selection_limit_reason"] = selection_result.selection_limit_reason

    for clip_spec, selected_slice, decision in zip(selected_clips, selected_slices, selected_decisions):
        used_assets.add(decision.asset_id)
        reasoning = selected_slice.reasoning
        if decision.reasoning:
            reasoning = f"{reasoning}; chooser: {decision.reasoning}" if reasoning else f"chooser: {decision.reasoning}"

        debug_entry["decisions"].append(
            {
                "asset_id": decision.asset_id,
                "start": decision.start_time,
                "end": decision.end_time,
                "reasoning": decision.reasoning,
            }
        )
        debug_entry["clip_specs"].append(
            {
                "asset_id": clip_spec.asset_id,
                "start": clip_spec.source_range.start,
                "duration": clip_spec.source_range.duration,
            }
        )

        notes_builder.add_entry(
            beat_id=beat.id,
            shot_id=shot.shot_id,
            narration_ids=shot.narration_ids,
            vo_excerpt=vo_excerpt,
            vo_range=vo_range,
            clip_spec=clip_spec,
            visual_summary=selected_slice.event_summary,
            coverage_note=shot.coverage_note,
            reasoning=reasoning,
        )

        _log_director_note(
            beat_id=beat.id,
            shot_id=shot.shot_id,
            vo_range=vo_range,
            clip_spec=clip_spec,
            visual_summary=selected_slice.event_summary,
            coverage_note=shot.coverage_note,
            reasoning=reasoning,
        )

    if len(selected_clips) == 1:
        debug_entry["decision"] = debug_entry["decisions"][0]
        debug_entry["clip_spec"] = debug_entry["clip_specs"][0]

    _write_shot_debug(debug_dir, beat.id, shot.shot_id, debug_entry)
    return selected_clips


def _build_candidate_pool(
    *,
    beat: models.Beat,
    shot: models.ShotInstruction,
    vo_range: models.TimeSpan,
    vo_excerpt: str,
    query: str,
    constraints: Any,
    manifest_path: Optional[Path],
    search_limit: int,
    requested_clips: int,
    search_mode: str,
) -> CandidatePoolResolution:
    mode_label = "exact" if search_mode == "explicit" else search_mode
    attempt_debug: Dict[str, Any] = {
        "search_mode": search_mode,
        "retrieval_results": [],
        "candidate_slices": [],
        "status": "pending",
    }
    LOGGER.info(
        "[CoreEditor] Shot %s: querying %s footage candidates (limit=%d)",
        shot.shot_id,
        mode_label,
        search_limit,
    )
    try:
        results = retrieval_tools.find_clips(
            query,
            limit=search_limit,
            constraints={
                "profile": constraints.profile_name,
                "explicit_terms": constraints.search_terms,
                "manifest_path": str(manifest_path) if manifest_path else None,
                "search_mode": search_mode,
            },
        )
    except Exception as exc:  # pragma: no cover - defensive fallback for local runs
        LOGGER.error(
            "Retrieval failed for shot %s (mode=%s query=%s): %s",
            shot.shot_id,
            search_mode,
            query,
            exc,
        )
        failure_reason = f"{mode_label} retrieval failed: {exc}"
        attempt_debug["status"] = "retrieval-error"
        attempt_debug["failure_reason"] = failure_reason
        return CandidatePoolResolution(search_mode=search_mode, candidates=[], debug=attempt_debug, failure_reason=failure_reason)

    LOGGER.info(
        "[CoreEditor] Shot %s: %s retrieval returned %d candidate window(s)",
        shot.shot_id,
        mode_label,
        len(results),
    )
    attempt_debug["retrieval_results"] = _debug_search_results(results)

    if not results:
        failure_reason = f"No {mode_label} retrieval hits"
        attempt_debug["status"] = "no-results"
        attempt_debug["failure_reason"] = failure_reason
        return CandidatePoolResolution(search_mode=search_mode, candidates=[], debug=attempt_debug, failure_reason=failure_reason)

    candidate_slices = generate_candidate_slices(
        beat=beat,
        shot=shot,
        vo_range=vo_range,
        search_results=results,
        max_candidates=max(search_limit, requested_clips * 4, 6),
    )
    if not candidate_slices:
        failure_reason = f"{mode_label} retrieval produced no candidate slices"
        LOGGER.warning("Shot %s produced no %s candidate slices", shot.shot_id, mode_label)
        attempt_debug["status"] = "no-candidates"
        attempt_debug["failure_reason"] = failure_reason
        return CandidatePoolResolution(search_mode=search_mode, candidates=[], debug=attempt_debug, failure_reason=failure_reason)

    LOGGER.info(
        "[CoreEditor] Shot %s: prepared %d %s slice candidate(s)",
        shot.shot_id,
        len(candidate_slices),
        mode_label,
    )
    attempt_debug["candidate_slices"] = _debug_candidate_slices(candidate_slices)

    candidate_pool = candidate_slices
    if shot.preferred_assets:
        preferred = [c for c in candidate_pool if c.asset_id in shot.preferred_assets]
        if preferred:
            candidate_pool = preferred
            attempt_debug["preferred_assets_applied"] = True

    candidate_pool, prefilter_notes = _apply_candidate_prefilters(
        candidates=candidate_pool,
        shot=shot,
        beat_intent=beat.visual_intent,
        vo_excerpt=vo_excerpt,
    )
    if prefilter_notes:
        attempt_debug["prefilters"] = prefilter_notes

    constrained_pool, constraint_log = filter_candidates_by_constraints(candidate_pool, constraints)
    if constraint_log:
        attempt_debug["constraint_filter"] = constraint_log
    if constrained_pool:
        attempt_debug["constraint_filter_status"] = "applied"
        attempt_debug["status"] = "usable"
        return CandidatePoolResolution(search_mode=search_mode, candidates=constrained_pool, debug=attempt_debug)
    if constraints.has_hard_constraints:
        failure_reason = "No candidates satisfied hard visual constraints"
        rejected = [
            f"{entry['asset_id']}: {', '.join(entry['reasons'])}"
            for entry in constraint_log[:5]
            if entry.get("reasons")
        ]
        if rejected:
            failure_reason = f"{failure_reason}; " + "; ".join(rejected)
        attempt_debug["status"] = "constraint-rejected"
        attempt_debug["failure_reason"] = failure_reason
        return CandidatePoolResolution(search_mode=search_mode, candidates=[], debug=attempt_debug, failure_reason=failure_reason)

    attempt_debug["status"] = "usable"
    return CandidatePoolResolution(search_mode=search_mode, candidates=candidate_pool, debug=attempt_debug)


def _select_clips_from_pool(
    *,
    beat: models.Beat,
    shot: models.ShotInstruction,
    used_assets: Set[str],
    vo_excerpt: str,
    vo_range: models.TimeSpan,
    clip_chooser: ClipChooser,
    candidate_pool: List[ClipSlice],
    prefer_metadata: bool,
) -> SelectionResult:
    full_candidate_pool = list(candidate_pool)
    unused_candidates = [c for c in candidate_pool if c.asset_id not in used_assets]
    using_unused_subset = bool(unused_candidates and len(unused_candidates) < len(candidate_pool))
    if unused_candidates:
        candidate_pool = unused_candidates

    LOGGER.info(
        "[CoreEditor] Shot %s: sending %d candidate slice(s) to clip chooser",
        shot.shot_id,
        len(candidate_pool),
    )
    selection_target = _target_clip_count(shot=shot, vo_duration=vo_range.duration)
    per_clip_duration = max(vo_range.duration / selection_target, 0.5)
    remaining_candidates = list(candidate_pool)
    selected_clips: List[models.ClipSpec] = []
    selected_slices: List[ClipSlice] = []
    selected_decisions: List[ClipSliceDecision] = []
    selection_limit_reason: Optional[str] = None
    retried_with_used_assets = False

    while remaining_candidates and len(selected_clips) < selection_target:
        decision = clip_chooser.choose_clip(
            vo_excerpt=vo_excerpt,
            vo_duration=per_clip_duration,
            coverage_note=shot.coverage_note,
            beat_intent=beat.visual_intent,
            visual_goal=shot.visual_goal,
            required_events=shot.required_events,
            keywords=shot.keywords,
            candidates=remaining_candidates,
            prefer_metadata=prefer_metadata,
        )
        if decision is None:
            selection_limit_reason = clip_chooser.last_failure_reason or "No acceptable clip was selected"
            if (
                not selected_clips
                and using_unused_subset
                and not retried_with_used_assets
                and full_candidate_pool
            ):
                LOGGER.info(
                    "[CoreEditor] Shot %s: unused candidates failed; retrying with used assets allowed",
                    shot.shot_id,
                )
                remaining_candidates = list(full_candidate_pool)
                using_unused_subset = False
                retried_with_used_assets = True
                continue
            if not selected_clips or len(selected_clips) < shot.min_clips:
                return SelectionResult(
                    clips=[],
                    slices=[],
                    decisions=[],
                    failure_reason=selection_limit_reason,
                    retried_with_used_assets=retried_with_used_assets,
                    selection_limit_reason=selection_limit_reason,
                )
            break

        selected_slice = next((c for c in remaining_candidates if c.asset_id == decision.asset_id), None)
        if selected_slice is None:
            selection_limit_reason = f"Chooser selected unknown asset {decision.asset_id}"
            if not selected_clips or len(selected_clips) < shot.min_clips:
                return SelectionResult(
                    clips=[],
                    slices=[],
                    decisions=[],
                    failure_reason=selection_limit_reason,
                    retried_with_used_assets=retried_with_used_assets,
                    selection_limit_reason=selection_limit_reason,
                )
            break

        LOGGER.info(
            "[CoreEditor] Shot %s: chooser selected %s %.2f-%.2fs",
            shot.shot_id,
            decision.asset_id,
            decision.start_time,
            decision.end_time,
        )

        clip_duration = max(decision.end_time - decision.start_time, 0.1)
        clip_spec = models.ClipSpec(
            asset_id=decision.asset_id,
            source_range=models.SourceRange(start=decision.start_time, duration=clip_duration),
            description=selected_slice.event_summary,
        )
        selected_clips.append(clip_spec)
        selected_slices.append(selected_slice)
        selected_decisions.append(decision)
        remaining_candidates = [c for c in remaining_candidates if c.asset_id != decision.asset_id]

    if len(selected_clips) < shot.min_clips:
        failure_reason = selection_limit_reason or (
            f"Only found {len(selected_clips)} acceptable clip(s) but shot requires at least {shot.min_clips}"
        )
        return SelectionResult(
            clips=[],
            slices=[],
            decisions=[],
            failure_reason=failure_reason,
            retried_with_used_assets=retried_with_used_assets,
            selection_limit_reason=selection_limit_reason,
        )

    return SelectionResult(
        clips=selected_clips,
        slices=selected_slices,
        decisions=selected_decisions,
        retried_with_used_assets=retried_with_used_assets,
        selection_limit_reason=selection_limit_reason,
    )


def _apply_pool_debug(debug_entry: Dict[str, Any], resolution: CandidatePoolResolution) -> None:
    for key in (
        "retrieval_results",
        "candidate_slices",
        "prefilters",
        "constraint_filter",
        "constraint_filter_status",
        "preferred_assets_applied",
    ):
        debug_entry.pop(key, None)
    debug_entry["retrieval_mode"] = resolution.search_mode
    for key, value in resolution.debug.items():
        if key in {"search_mode", "status", "failure_reason"}:
            continue
        debug_entry[key] = value


def _debug_search_results(results: List[retrieval_tools.SearchResult]) -> List[Dict[str, Any]]:
    return [
        {
            "asset_id": res.asset_id,
            "start": res.start,
            "end": res.end,
            "score": res.score,
            "tags": res.tags,
            "description": res.description,
            "segment_ids": res.segment_ids,
        }
        for res in results
    ]


def _debug_candidate_slices(candidate_slices: List[ClipSlice]) -> List[Dict[str, Any]]:
    return [
        {
            "asset_id": slice_.asset_id,
            "slice_start": slice_.slice_start,
            "slice_end": slice_.slice_end,
            "window_start": slice_.window_start,
            "window_end": slice_.window_end,
            "score": slice_.score,
            "reasoning": slice_.reasoning,
            "event_summary": slice_.event_summary,
        }
        for slice_ in candidate_slices
    ]


def _log_director_note(
    *,
    beat_id: str,
    shot_id: str,
    vo_range: models.TimeSpan,
    clip_spec: models.ClipSpec,
    visual_summary: str,
    coverage_note: str,
    reasoning: str,
) -> None:
    """Emit a concise console summary mirroring the director notes entry."""

    clip_range = clip_spec.source_range
    clip_start = clip_range.start if clip_range else 0.0
    clip_end = (clip_range.start + clip_range.duration) if clip_range else clip_start
    LOGGER.info(
        "[DirectorNotes] %s/%s | VO %.2f-%.2f -> %s %.2f-%.2f | Visuals: %s | Coverage: %s | Reason: %s",
        beat_id,
        shot_id,
        vo_range.start,
        vo_range.end,
        clip_spec.asset_id,
        clip_start,
        clip_end,
        visual_summary,
        coverage_note,
        reasoning,
    )


def _load_cached_shot_resolution(
    *,
    debug_dir: Optional[Path],
    beat: models.Beat,
    shot: models.ShotInstruction,
    vo_excerpt: str,
    vo_range: models.TimeSpan,
    notes_builder: DirectorNotesBuilder,
    used_assets: Set[str],
) -> Optional[List[models.ClipSpec]]:
    if not debug_dir:
        return None
    path = debug_dir / beat.id / f"{shot.shot_id}.json"
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError, TypeError) as exc:
        LOGGER.warning("Failed to read cached shot debug %s: %s", path, exc)
        return None

    if payload.get("beat_id") != beat.id or payload.get("shot_id") != shot.shot_id:
        return None
    if payload.get("selection_rules_version") != _SELECTION_RULES_VERSION:
        return None
    cached_narration_ids = payload.get("narration_ids")
    if cached_narration_ids and list(cached_narration_ids) != list(shot.narration_ids):
        return None

    status = payload.get("status")
    if status in {"no-selection", "no-candidates"}:
        failure_reason = payload.get("failure_reason") or status
        LOGGER.info("[CoreEditor] Shot %s: reusing cached unresolved result", shot.shot_id)
        notes_builder.add_unresolved_entry(
            beat_id=beat.id,
            shot_id=shot.shot_id,
            narration_ids=shot.narration_ids,
            vo_excerpt=vo_excerpt,
            vo_range=vo_range,
            coverage_note=shot.coverage_note,
            reasoning=failure_reason,
        )
        _log_unresolved_note(
            beat_id=beat.id,
            shot_id=shot.shot_id,
            vo_range=vo_range,
            coverage_note=shot.coverage_note,
            reasoning=failure_reason,
        )
        return []

    if status not in {"selected", "selected-multi"}:
        return None

    clip_specs_raw = payload.get("clip_specs") or []
    if not clip_specs_raw:
        return None

    candidate_by_asset = {
        candidate.get("asset_id"): candidate
        for candidate in payload.get("candidate_slices") or []
        if candidate.get("asset_id")
    }
    decision_by_asset = {
        decision.get("asset_id"): decision
        for decision in payload.get("decisions") or []
        if decision.get("asset_id")
    }

    clips: List[models.ClipSpec] = []
    for raw in clip_specs_raw:
        asset_id = raw.get("asset_id")
        if not asset_id:
            return None
        try:
            start = float(raw.get("start", 0.0))
            duration = float(raw.get("duration"))
        except (TypeError, ValueError):
            return None

        candidate = candidate_by_asset.get(asset_id, {})
        decision = decision_by_asset.get(asset_id, {})
        visual_summary = candidate.get("event_summary") or shot.visual_goal
        reasoning = candidate.get("reasoning") or "Cached clip selection"
        if decision.get("reasoning"):
            reasoning = f"{reasoning}; chooser: {decision['reasoning']}" if reasoning else f"chooser: {decision['reasoning']}"

        clip_spec = models.ClipSpec(
            asset_id=asset_id,
            source_range=models.SourceRange(start=start, duration=duration),
            description=visual_summary,
        )
        clips.append(clip_spec)
        used_assets.add(asset_id)
        notes_builder.add_entry(
            beat_id=beat.id,
            shot_id=shot.shot_id,
            narration_ids=shot.narration_ids,
            vo_excerpt=vo_excerpt,
            vo_range=vo_range,
            clip_spec=clip_spec,
            visual_summary=visual_summary,
            coverage_note=shot.coverage_note,
            reasoning=reasoning,
        )
        _log_director_note(
            beat_id=beat.id,
            shot_id=shot.shot_id,
            vo_range=vo_range,
            clip_spec=clip_spec,
            visual_summary=visual_summary,
            coverage_note=shot.coverage_note,
            reasoning=reasoning,
        )

    LOGGER.info("[CoreEditor] Shot %s: reused %d cached clip(s)", shot.shot_id, len(clips))
    return clips


def _log_unresolved_note(
    *,
    beat_id: str,
    shot_id: str,
    vo_range: models.TimeSpan,
    coverage_note: str,
    reasoning: str,
) -> None:
    LOGGER.info(
        "[DirectorNotes] %s/%s | VO %.2f-%.2f -> UNRESOLVED | Coverage: %s | Reason: %s",
        beat_id,
        shot_id,
        vo_range.start,
        vo_range.end,
        coverage_note,
        reasoning,
    )


def _record_unresolved_shot(
    *,
    debug_dir: Optional[Path],
    debug_entry: Dict[str, Any],
    beat_id: str,
    shot: models.ShotInstruction,
    vo_excerpt: str,
    vo_range: models.TimeSpan,
    notes_builder: DirectorNotesBuilder,
    failure_reason: str,
) -> None:
    LOGGER.warning("Clip chooser could not pick a usable slice for shot %s: %s", shot.shot_id, failure_reason)
    _write_shot_debug(
        debug_dir,
        beat_id,
        shot.shot_id,
        {**debug_entry, "status": "no-selection", "failure_reason": failure_reason},
    )
    notes_builder.add_unresolved_entry(
        beat_id=beat_id,
        shot_id=shot.shot_id,
        narration_ids=shot.narration_ids,
        vo_excerpt=vo_excerpt,
        vo_range=vo_range,
        coverage_note=shot.coverage_note,
        reasoning=failure_reason,
    )
    _log_unresolved_note(
        beat_id=beat_id,
        shot_id=shot.shot_id,
        vo_range=vo_range,
        coverage_note=shot.coverage_note,
        reasoning=failure_reason,
    )


def _apply_candidate_prefilters(
    *,
    candidates: List[ClipSlice],
    shot: models.ShotInstruction,
    beat_intent: str,
    vo_excerpt: str,
) -> tuple[List[ClipSlice], List[str]]:
    filtered = list(candidates)
    reasons: List[str] = []
    desired_mode = _desired_visual_mode(
        visual_goal=shot.visual_goal,
        fallback_text=" ".join(filter(None, [vo_excerpt, shot.coverage_note])),
    )
    if desired_mode != "neutral":
        mode_matches = [
            candidate
            for candidate in filtered
            if _classify_visual_mode(
                " ".join(filter(None, [candidate.event_summary, candidate.description, " ".join(candidate.event_tags)]))
            )
            == desired_mode
        ]
        if mode_matches:
            filtered = mode_matches
            reasons.append(f"visual-mode:{desired_mode}")

    if shot.required_events:
        event_matches = [
            candidate
            for candidate in filtered
            if _matches_any_constraint(
                shot.required_events,
                " ".join(filter(None, [candidate.event_summary, candidate.description, " ".join(candidate.event_tags)])).lower(),
            )
        ]
        if event_matches:
            filtered = event_matches
            reasons.append("required-events")
    elif shot.keywords:
        keyword_matches = [
            candidate
            for candidate in filtered
            if _matches_any_constraint(
                shot.keywords,
                " ".join(filter(None, [candidate.event_summary, candidate.description, " ".join(candidate.event_tags)])).lower(),
            )
        ]
        if keyword_matches:
            filtered = keyword_matches
            reasons.append("keywords")

    return filtered, reasons


def _target_clip_count(*, shot: models.ShotInstruction, vo_duration: float) -> int:
    max_clips = max(shot.max_clips, shot.min_clips, 1)
    desired = max(shot.min_clips, 1)
    if max_clips > 1 and vo_duration > 6.0:
        desired = max(desired, min(max_clips, math.ceil(vo_duration / 5.0)))
    return min(max_clips, desired)


def _write_shot_debug(debug_dir: Optional[Path], beat_id: str, shot_id: str, payload: Dict[str, Any]) -> None:
    if debug_dir is None:
        return
    try:
        target_dir = debug_dir / beat_id
        target_dir.mkdir(parents=True, exist_ok=True)
        path = target_dir / f"{shot_id}.json"
        with path.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
    except OSError as exc:
        LOGGER.warning("Failed to write shot debug for %s/%s: %s", beat_id, shot_id, exc)

