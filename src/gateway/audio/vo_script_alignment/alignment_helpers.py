"""Utility helpers for working with alignment JSON outputs.

These helpers keep everything streaming-friendly so the orchestrator can
progress through long alignments without loading the entire file into an LLM
context at once.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator, Sequence

AlignmentDict = dict[str, object]


@dataclass(frozen=True)
class AlignmentSpan:
    """Convenience representation for a single aligned span."""

    id: str
    text: str
    t0: float
    t1: float
    source: str
    words: Sequence[dict[str, object]]


def load_alignment(source: str | Path | AlignmentDict) -> AlignmentDict:
    """Load an alignment JSON file or pass-through an existing dict."""

    if isinstance(source, dict):
        return source
    path = Path(source)
    if not path.exists():
        raise FileNotFoundError(f"Alignment file not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def iter_spans(source: str | Path | AlignmentDict) -> Iterator[AlignmentSpan]:
    """Yield spans sequentially from an alignment payload."""

    payload = load_alignment(source)
    for raw in payload.get("aligned", []):
        yield _to_span(raw)


def find_span_by_id(
    source: str | Path | AlignmentDict,
    span_id: str,
) -> AlignmentSpan | None:
    """Return a single span matching ``span_id`` if present."""

    payload = load_alignment(source)
    for raw in payload.get("aligned", []):
        if raw.get("id") == span_id:
            return _to_span(raw)
    return None


def spans_in_window(
    source: str | Path | AlignmentDict,
    start_time: float,
    end_time: float,
    *,
    include_partial: bool = True,
) -> list[AlignmentSpan]:
    """Collect spans whose time range overlaps the given window."""

    if end_time <= start_time:
        raise ValueError("end_time must be greater than start_time")
    payload = load_alignment(source)
    matches: list[AlignmentSpan] = []
    for raw in payload.get("aligned", []):
        span = _to_span(raw)
        if _window_overlap(span.t0, span.t1, start_time, end_time, include_partial):
            matches.append(span)
    return matches


def iter_time_windows(
    source: str | Path | AlignmentDict,
    window_seconds: float,
    *,
    stride_seconds: float | None = None,
    include_partial: bool = True,
) -> Iterator[tuple[float, float, list[AlignmentSpan]]]:
    """Slide a window across the alignment and yield spans per chunk."""

    if window_seconds <= 0:
        raise ValueError("window_seconds must be > 0")
    payload = load_alignment(source)
    spans = [
        _to_span(raw)
        for raw in payload.get("aligned", [])
    ]
    if not spans:
        return
    stride = stride_seconds if stride_seconds is not None else window_seconds
    if stride <= 0:
        raise ValueError("stride_seconds must be > 0")
    total_duration = spans[-1].t1 if spans else 0.0
    cursor = 0.0
    while cursor < total_duration:
        window_end = cursor + window_seconds
        window_spans = [
            span
            for span in spans
            if _window_overlap(span.t0, span.t1, cursor, window_end, include_partial)
        ]
        if window_spans:
            yield cursor, min(window_end, total_duration), window_spans
        cursor += stride


def _window_overlap(
    span_start: float,
    span_end: float,
    window_start: float,
    window_end: float,
    include_partial: bool,
) -> bool:
    if include_partial:
        return span_start < window_end and span_end > window_start
    return span_start >= window_start and span_end <= window_end


def _to_span(raw: dict[str, object]) -> AlignmentSpan:
    return AlignmentSpan(
        id=str(raw.get("id")),
        text=str(raw.get("text", "")),
        t0=float(raw.get("t0", 0.0)),
        t1=float(raw.get("t1", 0.0)),
        source=str(raw.get("source", "script")),
        words=tuple(raw.get("words", []) or []),
    )
