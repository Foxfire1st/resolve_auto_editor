"""Utilities for deriving precise clip slices from retrieval results."""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Iterable, List, Sequence

from pydantic import BaseModel, Field

from src.orchestrator import models
from src.orchestrator.retrieval.event_parser import ClipEvent
from src.orchestrator.tool_bridge.retrieval_tools import SearchResult

try:  # Prefer optional ffmpeg-python when available for asset-bound probing.
    import ffmpeg
except Exception:  # pragma: no cover - optional dependency
    ffmpeg = None  # type: ignore


class ClipSlice(BaseModel):
    """Pre-trimmed clip segment ready for LLM ranking."""

    asset_id: str
    source_path: str | None = None
    description: str | None = None
    slice_start: float
    slice_end: float
    window_start: float
    window_end: float
    event_summary: str
    event_tags: List[str] = Field(default_factory=list)
    event_offset: float
    score: float
    reasoning: str
    variant: str = "matched"

    @property
    def duration(self) -> float:
        return max(self.slice_end - self.slice_start, 0.0)


_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9']+")
_VARIANT_BONUS = {"tight": 0.0, "medium": 0.25, "wide": 0.15}


def _collect_keywords(*sources: Iterable[str]) -> List[str]:
    tokens: List[str] = []
    for source in sources:
        for text in source:
            if not text:
                continue
            tokens.extend(token.lower() for token in _TOKEN_PATTERN.findall(text))
    return tokens


def _keyword_overlap(keywords: Sequence[str], summary: str, tags: Sequence[str]) -> int:
    if not keywords:
        return 0
    haystack = " ".join([summary, " ".join(tags)]).lower()
    return sum(1 for keyword in keywords if keyword and keyword in haystack)


def _bounded_slice(bounds_start: float, bounds_end: float, anchor: float, duration: float) -> tuple[float, float]:
    available_duration = max(bounds_end - bounds_start, 0.1)
    effective_duration = min(max(duration, 0.2), available_duration)
    clamped_anchor = max(bounds_start, min(anchor, bounds_end))
    slice_start = max(bounds_start, clamped_anchor - effective_duration / 2)
    slice_end = slice_start + effective_duration
    if slice_end > bounds_end:
        slice_start = max(bounds_start, bounds_end - effective_duration)
        slice_end = bounds_end
    slice_start = round(slice_start, 4)
    slice_end = round(slice_end, 4)
    return slice_start, slice_end


def _window_slice(window_start: float, window_end: float, anchor: float, duration: float) -> tuple[float, float]:
    return _bounded_slice(window_start, window_end, anchor, duration)


def _duration_fit_score(actual_duration: float, target_duration: float) -> float:
    if target_duration <= 0.0:
        return 0.0
    return max(0.0, 1.0 - abs(actual_duration - target_duration) / target_duration)


def _variant_durations(window_duration: float, asset_duration: float, target_duration: float) -> list[tuple[str, float, bool]]:
    tight_target = min(window_duration, max(0.75, target_duration * 0.5))
    medium_target = min(asset_duration, max(tight_target, target_duration))
    wide_target = min(asset_duration, max(medium_target + 1.0, target_duration * 1.35))
    return [
        ("tight", tight_target, False),
        ("medium", medium_target, True),
        ("wide", wide_target, True),
    ]


@lru_cache(maxsize=128)
def _get_source_asset_duration(source_path: str | None) -> float | None:
    if not source_path or ffmpeg is None:
        return None
    try:
        probe = ffmpeg.probe(source_path)
        return float(probe["format"]["duration"])
    except Exception:  # pragma: no cover - probe fallback is best-effort only
        return None


@lru_cache(maxsize=128)
def _get_metadata_time_extent(source_path: str | None) -> float | None:
    if not source_path:
        return None
    media_path = Path(source_path)
    metadata_path = media_path.parent / "meta" / f"{media_path.stem}_gemini.json"
    try:
        payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None

    max_time = 0.0

    def visit(value: object) -> None:
        nonlocal max_time
        if isinstance(value, dict):
            for key, child in value.items():
                if key in {"start_s", "end_s", "time_s"}:
                    numeric = _optional_float(child)
                    if numeric is not None:
                        max_time = max(max_time, numeric)
                elif isinstance(child, (dict, list)):
                    visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(payload.get("events") if isinstance(payload, dict) else payload)
    return max_time or None


def _optional_float(value: object) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _metadata_time_scale(source_path: str | None, asset_duration: float | None) -> float | None:
    if not asset_duration or asset_duration <= 0:
        return None
    metadata_extent = _get_metadata_time_extent(source_path)
    if not metadata_extent or metadata_extent <= 0:
        return None
    if metadata_extent <= asset_duration * 1.05:
        return None
    return asset_duration / metadata_extent


def _fit_window_to_asset(start: float, end: float, asset_duration: float | None) -> tuple[float, float]:
    if asset_duration is None or asset_duration <= 0:
        return start, end
    window_duration = max(end - start, 0.1)
    if window_duration >= asset_duration:
        return 0.0, asset_duration
    if end > asset_duration:
        end = asset_duration
        start = max(0.0, end - window_duration)
    if start < 0.0:
        start = 0.0
        end = min(asset_duration, start + window_duration)
    return start, end


def generate_candidate_slices(
    *,
    beat: models.Beat,
    shot: models.ShotInstruction,
    vo_range: models.TimeSpan,
    search_results: Sequence[SearchResult],
    max_candidates: int = 3,
) -> List[ClipSlice]:
    """Turn retrieval results into VO-aligned clip slices."""

    if not search_results:
        return []

    target_duration = max(vo_range.duration, 0.5)
    keyword_bank = _collect_keywords(
        shot.required_events,
        shot.keywords,
        [beat.visual_intent or ""],
        [shot.visual_goal or ""],
    )

    provisional: List[ClipSlice] = []
    for result in search_results:
        raw_window_start = result.start
        raw_window_end = result.end
        asset_duration = _get_source_asset_duration(result.source_path)
        time_scale = _metadata_time_scale(result.source_path, asset_duration)
        if time_scale:
            window_start = raw_window_start * time_scale
            window_end = raw_window_end * time_scale
        else:
            window_start = raw_window_start
            window_end = raw_window_end
        window_start, window_end = _fit_window_to_asset(window_start, window_end, asset_duration)
        window_duration = max(window_end - window_start, 0.1)
        asset_start = 0.0
        asset_end = asset_duration if asset_duration is not None else window_end
        events: Sequence[ClipEvent] = result.events or []
        if not events:
            midpoint_offset = max((window_end - window_start) / 2.0, 0.0)
            events = [
                ClipEvent(
                    offset=midpoint_offset,
                    summary=result.description or "Window midpoint",
                    tags=result.tags,
                    raw_text="synthetic event",
                )
            ]

        for event in events:
            if time_scale:
                event_time = max(
                    window_start,
                    min((raw_window_start + event.offset) * time_scale, window_end),
                )
            else:
                event_time = max(window_start, min(window_start + event.offset, window_end))
            seen_ranges: set[tuple[float, float]] = set()
            for variant_name, preferred_duration, can_expand in _variant_durations(
                window_duration,
                max(asset_end - asset_start, window_duration),
                target_duration,
            ):
                if can_expand:
                    slice_start, slice_end = _bounded_slice(asset_start, asset_end, event_time, preferred_duration)
                else:
                    slice_start, slice_end = _window_slice(window_start, window_end, event_time, preferred_duration)
                signature = (slice_start, slice_end)
                if signature in seen_ranges:
                    continue
                seen_ranges.add(signature)

                actual_duration = max(slice_end - slice_start, 0.0)
                overlap = _keyword_overlap(keyword_bank, event.summary, event.tags)
                duration_fit = _duration_fit_score(actual_duration, target_duration)
                score = result.score + overlap * 0.5 + duration_fit * 0.75 + _VARIANT_BONUS[variant_name]
                reasoning_parts = [f"{variant_name} slice", f"duration fit {duration_fit:.2f}"]
                if time_scale:
                    reasoning_parts.append(f"metadata time scaled {time_scale:.2f}")
                if overlap:
                    reasoning_parts.append(f"keyword overlap x{overlap}")
                reasoning_parts.append(f"vector score {result.score:.3f}")
                provisional.append(
                    ClipSlice(
                        asset_id=result.asset_id,
                        source_path=result.source_path,
                        description=result.description,
                        slice_start=slice_start,
                        slice_end=slice_end,
                        window_start=window_start,
                        window_end=window_end,
                        event_summary=event.summary,
                        event_tags=event.tags,
                        event_offset=event.offset,
                        score=score,
                        reasoning=", ".join(reasoning_parts),
                        variant=variant_name,
                    )
                )

    provisional.sort(key=lambda slice_: (slice_.score, slice_.duration), reverse=True)
    selected: List[ClipSlice] = []
    used_signatures: set[tuple[str, float, float]] = set()
    per_asset_counts: dict[str, int] = {}
    per_asset_limit = 2 if max_candidates > 1 else 1
    for slice_candidate in provisional:
        signature = (slice_candidate.asset_id, slice_candidate.slice_start, slice_candidate.slice_end)
        if signature in used_signatures:
            continue
        if per_asset_counts.get(slice_candidate.asset_id, 0) >= per_asset_limit:
            continue
        selected.append(slice_candidate)
        used_signatures.add(signature)
        per_asset_counts[slice_candidate.asset_id] = per_asset_counts.get(slice_candidate.asset_id, 0) + 1
        if len(selected) >= max_candidates:
            break

    return selected


def make_slices(asset_id: str, windows: List[dict], desired_duration: float) -> List[ClipSlice]:
    """Propose slices for specific windows in an asset."""

    slices = []
    for w in windows:
        start = w["start"]
        end = w["end"]
        anchor = (start + end) / 2.0
        slice_start, slice_end = _window_slice(start, end, anchor, desired_duration)

        slices.append(
            ClipSlice(
                asset_id=asset_id,
                slice_start=slice_start,
                slice_end=slice_end,
                window_start=start,
                window_end=end,
                event_summary="Manual window",
                event_offset=anchor - start,
                score=1.0,
                reasoning="Manual slice request",
                variant="manual",
            )
        )

    return slices
