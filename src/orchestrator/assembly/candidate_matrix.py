"""Build metadata-backed rough-cut candidates for narration slots."""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any, Mapping, Sequence, SupportsFloat, SupportsIndex

try:
    import ffmpeg
except Exception:  # pragma: no cover - optional duration probing dependency
    ffmpeg = None

from gateway.vision.event_metadata import db
from gateway.vision.event_metadata.config import get_settings
from gateway.vision.event_metadata.models import EventSearchResult
from gateway.vision.event_metadata.search import EventSearchFilters, search_events
from src.orchestrator import models
from src.orchestrator.assembly.models import (
    AssemblyCandidate,
    AssemblyProposal,
    AssemblySlot,
)
from src.orchestrator.assembly.scoring import assemble_slots
from src.orchestrator.models import SourceRange
from src.orchestrator.retrieval.constraints import extract_shot_constraints
from src.orchestrator.retrieval.footage_manifest import read_manifest


def build_slots_from_beat_map(
    beat_map: models.BeatMap,
    segment_lookup: Mapping[str, Mapping[str, Any]],
    shot_plans: Mapping[str, models.ShotPlan] | None = None,
) -> list[AssemblySlot]:
    """Create narration-timed assembly slots from shot plans or narration subspans."""

    slots: list[AssemblySlot] = []
    for beat in beat_map.beats:
        shot_plan = shot_plans.get(beat.id) if shot_plans else None
        if shot_plan and shot_plan.shot_instructions:
            slots.extend(build_slots_from_shot_plan(beat, shot_plan, segment_lookup))
            continue
        slots.extend(_build_slots_from_segments(beat, segment_lookup))
    return slots


def build_slots_from_shot_plan(
    beat: models.Beat,
    shot_plan: models.ShotPlan,
    segment_lookup: Mapping[str, Mapping[str, Any]],
) -> list[AssemblySlot]:
    """Create assembly slots from explicit shot-planner directives."""

    slots: list[AssemblySlot] = []
    for shot in shot_plan.shot_instructions:
        segment_pairs = [
            (segment_id, segment_lookup[segment_id])
            for segment_id in shot.narration_ids
            if segment_id in segment_lookup
        ]
        if not segment_pairs:
            continue
        narration_ids = [str(segment.get("id", segment_id)) for segment_id, segment in segment_pairs]
        segments = [segment for _, segment in segment_pairs]
        start = min(float(segment.get("t0", 0.0)) for segment in segments)
        end = max(float(segment.get("t1", start)) for segment in segments)
        vo_excerpt = " ".join(str(segment.get("text", "")).strip() for segment in segments).strip()
        slots.append(
            AssemblySlot(
                slot_id=shot.shot_id,
                beat_id=beat.id,
                narration_ids=narration_ids,
                timeline_start=start,
                timeline_duration=max(end - start, 0.1),
                visual_requirement=shot.visual_goal,
                vo_excerpt=vo_excerpt or shot.vo_excerpt,
                keywords=list(shot.keywords or beat.search_queries),
                preferred_assets=list(shot.preferred_assets),
                min_clips=shot.min_clips,
                max_clips=shot.max_clips,
            )
        )
    return slots


def _build_slots_from_segments(
    beat: models.Beat,
    segment_lookup: Mapping[str, Mapping[str, Any]],
) -> list[AssemblySlot]:
    slots: list[AssemblySlot] = []
    segment_pairs = [
        (index, segment_id, segment_lookup[segment_id])
        for index, segment_id in enumerate(beat.segment_ids, start=1)
        if segment_id in segment_lookup
    ]
    for index, segment_id, segment in segment_pairs:
        start = float(segment.get("t0", 0.0))
        end = float(segment.get("t1", start))
        narration_id = str(segment.get("id", segment_id))
        slots.append(
            AssemblySlot(
                slot_id=f"{beat.id}-S{index:02d}",
                beat_id=beat.id,
                narration_ids=[narration_id],
                timeline_start=start,
                timeline_duration=max(end - start, 0.1),
                visual_requirement=beat.visual_intent,
                vo_excerpt=str(segment.get("text", "")).strip(),
                keywords=list(beat.search_queries),
            )
        )
    return slots


class CandidateMatrixBuilder:
    """Session-scoped candidate lookup over manifest and event metadata."""

    def __init__(
        self,
        *,
        profile_name: str | None = None,
        manifest_path: Path | None = None,
        database_path: Path | str | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> None:
        self.settings = get_settings(profile_name=profile_name, database_path=database_path)
        self.profile_name = self.settings.profile
        self.manifest_path = manifest_path
        self.manifest_entries = read_manifest(manifest_path) if manifest_path else []
        self._own_connection = connection is None
        self.connection = connection or db.connect(self.settings.database_path)
        db.init_db(self.connection)

    def close(self) -> None:
        if self._own_connection:
            self.connection.close()

    def __enter__(self) -> "CandidateMatrixBuilder":
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    def build_matrix(
        self,
        slots: Sequence[AssemblySlot],
        *,
        limit_per_slot: int = 8,
    ) -> dict[str, list[AssemblyCandidate]]:
        """Return candidate lists keyed by slot id."""

        return {
            slot.slot_id: self.find_candidates(slot, limit=limit_per_slot)
            for slot in slots
        }

    def build_proposal(
        self,
        *,
        project_name: str,
        slots: Sequence[AssemblySlot],
        limit_per_slot: int = 8,
    ) -> AssemblyProposal:
        """Build a first-pass proposal with the top metadata candidate selected."""

        matrix = self.build_matrix(slots, limit_per_slot=limit_per_slot)
        proposal_slots = assemble_slots(slots, matrix)
        resolved = sum(1 for slot in proposal_slots if slot.selected is not None)
        return AssemblyProposal(
            project_name=project_name,
            profile_name=self.profile_name,
            slots=proposal_slots,
            global_notes=[
                f"{resolved} of {len(proposal_slots)} slots resolved from metadata candidates.",
                "First-pass proposal only; global scoring and review are not applied yet.",
            ],
        )

    def find_candidates(self, slot: AssemblySlot, *, limit: int = 8) -> list[AssemblyCandidate]:
        constraints = extract_shot_constraints(
            visual_goal=slot.visual_requirement,
            vo_excerpt=slot.vo_excerpt,
            required_events=(),
            keywords=slot.keywords,
            profile_name=self.profile_name,
        )
        query = _slot_query(slot)
        candidates = [
            *self._manifest_candidates(slot, constraints.search_terms),
            *self._event_candidates(
                slot,
                query=query,
                include_review=constraints.has_exact_named_target,
                limit=max(limit, 5),
            ),
        ]
        return _dedupe_and_rank(candidates)[:limit]

    def _manifest_candidates(
        self,
        slot: AssemblySlot,
        terms: Sequence[str],
    ) -> list[AssemblyCandidate]:
        if not self.manifest_entries:
            return []
        candidates: list[AssemblyCandidate] = []
        query_terms = [term.lower() for term in terms if term.strip()]
        fallback_terms = _salient_terms(_slot_query(slot))
        search_terms = query_terms or fallback_terms
        for entry in self.manifest_entries:
            haystack = _manifest_text(entry)
            if not haystack:
                continue
            matched_terms = [term for term in search_terms if term in haystack]
            match_count = len(matched_terms)
            if match_count <= 0:
                continue
            asset_id = str(entry.get("asset_id") or Path(str(entry.get("source_path", ""))).stem)
            if not asset_id:
                continue
            duration = _manifest_duration(entry) or slot.timeline_duration
            source_duration = max(min(duration, slot.timeline_duration), 0.1)
            manifest_summary = _manifest_summary(entry, asset_id)
            manifest_score = _manifest_candidate_score(match_count=match_count, term_count=len(search_terms))
            candidates.append(
                AssemblyCandidate(
                    asset_id=asset_id,
                    source_path=str(entry.get("source_path") or "") or None,
                    source_range=SourceRange(start=0.0, duration=source_duration),
                    event_ids=[f"manifest:{asset_id}"],
                    score=manifest_score,
                    reasons=[
                        f"manifest term coverage {manifest_score:.3f}",
                        f"matched {match_count} manifest term(s)",
                    ],
                    warnings=[] if duration else ["manifest duration missing; using slot duration"],
                    provenance=["manifest"],
                    debug_summary=_manifest_debug_summary(
                        asset_id=asset_id,
                        matched_terms=matched_terms,
                        manifest_summary=manifest_summary,
                        source_start=0.0,
                        source_duration=source_duration,
                    ),
                    debug_details={
                        "candidate_type": "manifest",
                        "match_count": match_count,
                        "query_term_count": len(search_terms),
                        "query_terms": list(search_terms),
                        "matched_terms": matched_terms,
                        "manifest_summary": manifest_summary,
                    },
                )
            )
        return candidates

    def _event_candidates(
        self,
        slot: AssemblySlot,
        *,
        query: str,
        include_review: bool,
        limit: int,
    ) -> list[AssemblyCandidate]:
        if not query.strip():
            return []
        hits = search_events(
            query,
            profile_name=self.profile_name,
            filters=EventSearchFilters(),
            include_review=include_review,
            limit=limit,
            connection=self.connection,
        )
        return [_candidate_from_event(slot, hit) for hit in hits]


def _candidate_from_event(slot: AssemblySlot, hit: EventSearchResult) -> AssemblyCandidate:
    duration = max(hit.end_s - hit.start_s, 0.0)
    warnings: list[str] = []
    if duration <= 0.0:
        duration = slot.timeline_duration
        warnings.append("event duration missing; using slot duration")
    duration = max(min(duration, slot.timeline_duration), 0.1)
    event_summary = hit.summary.strip() or hit.video_id
    reasons = [
        f"event score {hit.score:.3f}",
        f"boundary confidence {hit.boundary_confidence:.3f}",
    ]
    if hit.needs_boundary_review:
        warnings.append("event boundary needs review")
    return AssemblyCandidate(
        asset_id=hit.video_id,
        source_path=hit.source_path,
        source_range=SourceRange(start=hit.start_s, duration=duration),
        event_ids=[hit.id],
        score=hit.score,
        reasons=reasons,
        warnings=warnings,
        provenance=["event_metadata"],
        debug_summary=_event_debug_summary(hit, event_summary=event_summary, duration=duration),
        debug_details={
            "candidate_type": "event_metadata",
            "event_id": hit.id,
            "summary": event_summary,
            "actor": hit.actor,
            "action_type": hit.action_type,
            "ability_name": hit.ability_name,
            "labels": list(hit.labels),
            "tags": list(hit.tags),
        },
    )


def _slot_query(slot: AssemblySlot) -> str:
    parts = [slot.visual_requirement, slot.vo_excerpt, " ".join(slot.keywords)]
    return " | ".join(part for part in parts if part)


def _manifest_summary(entry: Mapping[str, Any], asset_id: str) -> str:
    summary = str(entry.get("summary") or "").strip()
    return summary or asset_id


def _manifest_debug_summary(
    *,
    asset_id: str,
    matched_terms: Sequence[str],
    manifest_summary: str,
    source_start: float,
    source_duration: float,
) -> str:
    terms = ", ".join(matched_terms[:6]) or "no matched terms"
    return (
        f"manifest {asset_id}: matched [{terms}]; "
        f"summary={_truncate_debug_text(manifest_summary)}; "
        f"source={source_start:.2f}+{source_duration:.2f}s"
    )


def _manifest_candidate_score(*, match_count: int, term_count: int) -> float:
    return match_count / max(term_count, 1)


def _event_debug_summary(
    hit: EventSearchResult,
    *,
    event_summary: str,
    duration: float,
) -> str:
    return (
        f"event {hit.id}: {_truncate_debug_text(event_summary)}; "
        f"source={hit.start_s:.2f}+{duration:.2f}s; "
        f"score={hit.score:.3f}; "
        f"boundary={hit.boundary_confidence:.3f}"
    )


def _truncate_debug_text(text: str, limit: int = 160) -> str:
    compact = " ".join(text.split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 3].rstrip() + "..."


def _manifest_text(entry: Mapping[str, Any]) -> str:
    values = [
        entry.get("asset_id"),
        entry.get("summary"),
        " ".join(str(tag) for tag in entry.get("tags") or []),
        entry.get("mode"),
        entry.get("stage_name"),
        entry.get("source_path"),
        entry.get("meta_path"),
    ]
    return " ".join(str(value) for value in values if value).lower()


def _salient_terms(text: str) -> list[str]:
    terms: list[str] = []
    seen: set[str] = set()
    for token in re.findall(r"[A-Za-z0-9_+#']+", text.lower()):
        normalized = token.strip("'")
        if len(normalized) < 3 or normalized in seen:
            continue
        seen.add(normalized)
        terms.append(normalized)
    return terms[:12]


def _positive_float(value: str | SupportsFloat | SupportsIndex | None) -> float | None:
    if value is None:
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    return numeric if numeric > 0 else None


def _manifest_duration(entry: Mapping[str, Any]) -> float | None:
    duration = _positive_float(entry.get("duration"))
    if duration is not None:
        return duration
    source_path = entry.get("source_path")
    if not source_path or ffmpeg is None:
        return None
    try:
        probe = ffmpeg.probe(str(source_path))
        return _positive_float(probe.get("format", {}).get("duration"))
    except Exception:
        return None


def _dedupe_and_rank(candidates: Sequence[AssemblyCandidate]) -> list[AssemblyCandidate]:
    best_by_key: dict[tuple[str, float, float], AssemblyCandidate] = {}
    for candidate in candidates:
        key = (
            candidate.asset_id,
            round(candidate.source_range.start, 3),
            round(candidate.source_range.duration, 3),
        )
        existing = best_by_key.get(key)
        if existing is None or candidate.score > existing.score:
            best_by_key[key] = candidate
    return sorted(best_by_key.values(), key=lambda item: item.score, reverse=True)
