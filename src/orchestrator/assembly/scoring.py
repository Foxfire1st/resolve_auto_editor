"""Global scoring helpers for metadata-first rough-cut proposals."""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field
from typing import Mapping, Sequence

from src.orchestrator.assembly.models import AssemblyCandidate, AssemblySlot
from src.orchestrator.retrieval.constraints import extract_shot_constraints, validate_candidate

REPEATED_ASSET_PENALTY = 0.75
SCARCE_FUTURE_ASSET_PENALTY = 1.5
SHORT_MANIFEST_FALLBACK_DURATION = 3.0


@dataclass
class AssemblyState:
    """Mutable state used while assigning candidates across slots."""

    used_assets: set[str] = field(default_factory=set)
    future_scarce_assets: set[str] = field(default_factory=set)


@dataclass(frozen=True)
class _ConstraintCandidateView:
    asset_id: str
    event_summary: str
    description: str | None
    event_tags: list[str]


def assemble_slots(
    slots: Sequence[AssemblySlot],
    matrix: Mapping[str, Sequence[AssemblyCandidate]],
) -> list[AssemblySlot]:
    """Assign candidates with light lookahead for repeated or scarce assets."""

    ordered_slots = sorted(slots, key=lambda slot: (slot.timeline_start, slot.slot_id))
    assigned: list[AssemblySlot] = []
    state = AssemblyState()
    for index, slot in enumerate(ordered_slots):
        remaining_slots = ordered_slots[index + 1 :]
        state.future_scarce_assets = _future_scarce_assets(remaining_slots, matrix)
        candidates = list(matrix.get(slot.slot_id, ()))
        accepted, rejected = _filter_candidates_by_constraints(slot, candidates)
        scored = [score_candidate(slot, candidate, state) for candidate in accepted]
        scored.sort(key=lambda candidate: candidate.score, reverse=True)
        selected_clips = _select_slot_sequence(slot, scored)
        selected = selected_clips[0] if selected_clips else None
        if selected_clips:
            state.used_assets.update(candidate.asset_id for candidate in selected_clips)
        assigned.append(
            slot.model_copy(
                update={
                    "selected": selected,
                    "selected_clips": selected_clips if len(selected_clips) > 1 else [],
                    "alternatives": [candidate for candidate in scored if candidate not in selected_clips],
                    "unresolved_reason": _unresolved_reason(selected, candidates, rejected),
                }
            )
        )
    return assigned


def score_candidate(
    slot: AssemblySlot,
    candidate: AssemblyCandidate,
    state: AssemblyState,
) -> AssemblyCandidate:
    """Return a candidate copy with global assignment penalties applied."""

    del slot
    score = candidate.score
    reasons = list(candidate.reasons)
    if candidate.asset_id in state.used_assets:
        score -= REPEATED_ASSET_PENALTY
        reasons.append("asset repetition penalty")
    if candidate.asset_id in state.future_scarce_assets:
        score -= SCARCE_FUTURE_ASSET_PENALTY
        reasons.append("reserved for more constrained later slot")
    return candidate.model_copy(update={"score": score, "reasons": reasons})


def _future_scarce_assets(
    slots: Sequence[AssemblySlot],
    matrix: Mapping[str, Sequence[AssemblyCandidate]],
) -> set[str]:
    """Find assets that are uniquely useful to future unresolved slots."""

    counts: Counter[str] = Counter()
    for slot in slots:
        asset_ids = {candidate.asset_id for candidate in matrix.get(slot.slot_id, ())}
        if len(asset_ids) == 1:
            counts.update(asset_ids)
    return set(counts)


def _filter_candidates_by_constraints(
    slot: AssemblySlot,
    candidates: Sequence[AssemblyCandidate],
) -> tuple[list[AssemblyCandidate], list[str]]:
    constraints = extract_shot_constraints(
        visual_goal=slot.visual_requirement,
        vo_excerpt=slot.vo_excerpt,
        keywords=slot.keywords,
    )
    if not constraints.has_hard_constraints:
        return list(candidates), []

    accepted: list[AssemblyCandidate] = []
    rejected: list[str] = []
    for candidate in candidates:
        validation = validate_candidate(_constraint_view(candidate), constraints)
        if validation.accepted:
            accepted.append(candidate)
            continue
        rejected.append(f"{candidate.asset_id}: {', '.join(validation.reasons)}")
    accepted, weak_rejections = _reject_weak_manifest_matches(slot, accepted)
    return accepted, [*rejected, *weak_rejections]


def _constraint_view(candidate: AssemblyCandidate) -> _ConstraintCandidateView:
    debug_details = candidate.debug_details
    summary = str(
        debug_details.get("summary")
        or debug_details.get("manifest_summary")
        or candidate.debug_summary
        or candidate.source_path
        or candidate.asset_id
    )
    tags = [*candidate.provenance]
    for key in ("labels", "tags", "matched_terms", "query_terms"):
        value = debug_details.get(key)
        if isinstance(value, list):
            tags.extend(str(item) for item in value if str(item).strip())
    return _ConstraintCandidateView(
        asset_id=candidate.asset_id,
        event_summary=summary,
        description=candidate.debug_summary,
        event_tags=tags,
    )


def _unresolved_reason(
    selected: AssemblyCandidate | None,
    candidates: Sequence[AssemblyCandidate],
    rejected: Sequence[str],
) -> str | None:
    if selected is not None:
        return None
    if rejected:
        return "No candidate satisfied hard visual constraints: " + "; ".join(rejected[:3])
    if candidates:
        return "No metadata candidate found for slot."
    return "No metadata candidate found for slot."


def _reject_weak_manifest_matches(
    slot: AssemblySlot,
    candidates: Sequence[AssemblyCandidate],
) -> tuple[list[AssemblyCandidate], list[str]]:
    accepted: list[AssemblyCandidate] = []
    rejected: list[str] = []
    has_better_fallback = any(not _is_weak_manifest_candidate(candidate) for candidate in candidates)
    for candidate in candidates:
        if not _is_weak_manifest_candidate(candidate):
            accepted.append(candidate)
            continue
        if slot.timeline_duration <= SHORT_MANIFEST_FALLBACK_DURATION and not has_better_fallback:
            accepted.append(candidate)
            continue
        rejected.append(f"{candidate.asset_id}: weak one-term manifest match")
    return accepted, rejected


def _is_weak_manifest_candidate(candidate: AssemblyCandidate) -> bool:
    if candidate.provenance != ["manifest"]:
        return False
    match_count = candidate.debug_details.get("match_count")
    try:
        return int(match_count) <= 1
    except (TypeError, ValueError):
        return False


def _select_slot_sequence(
    slot: AssemblySlot,
    candidates: Sequence[AssemblyCandidate],
) -> list[AssemblyCandidate]:
    if not candidates:
        return []
    desired = _desired_clip_count(slot)
    selected: list[AssemblyCandidate] = []
    used_assets: set[str] = set()
    for candidate in candidates:
        if candidate.asset_id in used_assets:
            continue
        selected.append(candidate)
        used_assets.add(candidate.asset_id)
        if len(selected) >= desired:
            break
    return selected or [candidates[0]]


def _desired_clip_count(slot: AssemblySlot) -> int:
    if slot.max_clips <= 1:
        return 1
    duration_target = math.ceil(max(slot.timeline_duration, 0.1) / 6.0)
    return max(slot.min_clips, min(slot.max_clips, max(1, duration_target)))
