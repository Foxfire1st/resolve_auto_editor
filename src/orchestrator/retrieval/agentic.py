"""Compatibility wrapper around deterministic event metadata retrieval."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence

from pydantic import BaseModel, Field

from gateway.vision.event_metadata.models import EventSearchResult
from gateway.vision.event_metadata.search import EventSearchFilters, search_events


class RetrievalCandidate(BaseModel):
    """Window metadata surfaced for ranking and downstream slicing."""

    segment_id: str
    video_id: str
    source_path: Optional[str]
    vector_score: float
    start_s: float
    end_s: float
    caption: str
    tags: List[str] = Field(default_factory=list)
    is_window: bool = False
    segment_ids: List[str] = Field(default_factory=list)
    segment_count: int = 1


class SelectedClip(BaseModel):
    found_match: bool = Field(..., description="Set to False if no suitable clip was found.")
    video_id: Optional[str] = None
    start_time: Optional[float] = None
    end_time: Optional[float] = None
    reasoning: str
    confidence: float
    segment_ids: List[str] = Field(
        default_factory=list,
        description="If provided, these ids map directly back to stored event metadata.",
    )


@dataclass
class AgenticSelection:
    clip: SelectedClip
    candidate: RetrievalCandidate


@dataclass
class AgenticRetrievalResult:
    selection: Optional[AgenticSelection]
    candidates: List[RetrievalCandidate]
    raw_segments: List[EventSearchResult]


class AgenticRetriever:
    """Retrieve clips from event metadata while preserving the legacy API name."""

    def __init__(self, *, model_name: Optional[str] = None) -> None:
        self.model_name = model_name

    def retrieve(
        self,
        query: str,
        *,
        max_candidates: int = 10,
        context_window: float = 15.0,
        search_kwargs: Optional[Dict[str, Any]] = None,
    ) -> AgenticRetrievalResult:
        """Return the top deterministic event metadata match and candidates."""

        del context_window
        search_kwargs = search_kwargs or {}
        filters = EventSearchFilters(
            actor=search_kwargs.get("actor"),
            action_type=search_kwargs.get("action_type"),
            ability_name=search_kwargs.get("ability_name"),
            video_ids=search_kwargs.get("video_ids"),
            required_tags=search_kwargs.get("required_tags") or (),
        )
        raw_results = search_events(
            query,
            profile_name=search_kwargs.get("profile_name") or search_kwargs.get("profile"),
            database_path=search_kwargs.get("database_path"),
            filters=filters,
            include_review=bool(search_kwargs.get("include_review", False)),
            limit=max_candidates,
        )

        candidates = [_candidate_from_event(hit) for hit in raw_results]
        if not candidates:
            return AgenticRetrievalResult(selection=None, candidates=[], raw_segments=raw_results)

        best = candidates[0]
        selection = AgenticSelection(
            clip=SelectedClip(
                found_match=True,
                video_id=best.video_id,
                start_time=best.start_s,
                end_time=best.end_s,
                reasoning="Selected by deterministic event metadata ranking.",
                confidence=min(max(best.vector_score, 0.0), 1.0),
                segment_ids=best.segment_ids,
            ),
            candidate=best,
        )
        return AgenticRetrievalResult(selection=selection, candidates=candidates, raw_segments=raw_results)


def _candidate_from_event(hit: EventSearchResult) -> RetrievalCandidate:
    tags = _unique([*hit.tags, *hit.labels])
    return RetrievalCandidate(
        segment_id=hit.id,
        video_id=hit.video_id,
        source_path=hit.source_path,
        vector_score=hit.score,
        start_s=hit.start_s,
        end_s=hit.end_s,
        caption=hit.summary,
        tags=tags,
        is_window=False,
        segment_ids=[hit.id],
        segment_count=1,
    )


def _match_candidate(candidates: Sequence[RetrievalCandidate], clip: SelectedClip) -> Optional[RetrievalCandidate]:
    for cand in candidates:
        if clip.video_id != cand.video_id:
            continue
        if clip.start_time is None or clip.end_time is None:
            return cand
        if abs(cand.start_s - clip.start_time) < 1.0 and abs(cand.end_s - clip.end_time) < 1.0:
            return cand
    return next((cand for cand in candidates if cand.video_id == clip.video_id), None)


def _unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        normalized = value.strip()
        if not normalized or normalized.lower() in seen:
            continue
        seen.add(normalized.lower())
        result.append(normalized)
    return result
