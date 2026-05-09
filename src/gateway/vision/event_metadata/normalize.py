"""Normalize Gemini event spans into event metadata contracts."""

from __future__ import annotations

from typing import Any

from gateway.vision.timecode import timecode_to_seconds

from .schema import BoundaryEvidence, EventMetadata, EventObservation


def _coerce_seconds(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return timecode_to_seconds(value)
    return None


def _read_seconds(data: dict[str, Any], seconds_key: str, timecode_key: str) -> float | None:
    seconds = _coerce_seconds(data.get(seconds_key))
    if seconds is not None:
        return seconds
    value = data.get(timecode_key)
    if isinstance(value, str):
        return timecode_to_seconds(value)
    return None


def _clamp(value: float, lower: float, upper: float | None) -> float:
    value = max(value, lower)
    if upper is not None:
        value = min(value, upper)
    return value


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _normalize_observation(raw: dict[str, Any]) -> EventObservation:
    return EventObservation(
        time_s=_read_seconds(raw, "time_s", "timecode"),
        timecode=raw.get("timecode") if isinstance(raw.get("timecode"), str) else None,
        start_s=_read_seconds(raw, "start_s", "start_timecode"),
        end_s=_read_seconds(raw, "end_s", "end_timecode"),
        start_timecode=raw.get("start_timecode") if isinstance(raw.get("start_timecode"), str) else None,
        end_timecode=raw.get("end_timecode") if isinstance(raw.get("end_timecode"), str) else None,
        kind=str(raw["kind"]).strip() if raw.get("kind") else None,
        text=str(raw["text"]).strip() if raw.get("text") else None,
        details=str(raw["details"]).strip() if raw.get("details") else None,
        labels=_string_list(raw.get("labels")),
        confidence=_coerce_seconds(raw.get("confidence")),
    )


def _latest_observation_time_after(start_s: float, observations: list[EventObservation]) -> float | None:
    candidates: list[float] = []
    for observation in observations:
        for value in (observation.end_s, observation.start_s, observation.time_s):
            if value is not None and value > start_s:
                candidates.append(value)
    if not candidates:
        return None
    return max(candidates)


def _boundary_evidence(raw_event: dict[str, Any]) -> BoundaryEvidence:
    raw = raw_event.get("boundary_evidence")
    if not isinstance(raw, dict):
        return BoundaryEvidence()
    return BoundaryEvidence(
        start=str(raw["start"]).strip() if raw.get("start") else None,
        end=str(raw["end"]).strip() if raw.get("end") else None,
        anchor=str(raw["anchor"]).strip() if raw.get("anchor") else None,
        notes=str(raw["notes"]).strip() if raw.get("notes") else None,
    )


def normalize_event_metadata(
    raw_event: dict[str, Any],
    *,
    clip_duration_s: float | None = None,
    next_event_start_s: float | None = None,
) -> EventMetadata:
    """Convert a raw Gemini event into a normalized event span.

    Missing or invalid end boundaries are only repaired from observed timestamps or
    neighboring event starts. Repaired boundaries stay flagged for review so search
    can later default to explicit, trusted spans.
    """

    start = _read_seconds(raw_event, "start_s", "start_timecode")
    if start is None:
        start = _read_seconds(raw_event, "time_s", "timecode")
    if start is None:
        raise ValueError("event metadata requires start_s/start_timecode or legacy time_s/timecode")

    start = _clamp(start, 0.0, clip_duration_s)
    anchor = _read_seconds(raw_event, "anchor_s", "anchor_timecode")
    if anchor is None:
        anchor = _read_seconds(raw_event, "time_s", "timecode")
    if anchor is None:
        anchor = start
    anchor = _clamp(anchor, start, clip_duration_s)

    observations = [
        _normalize_observation(item)
        for item in raw_event.get("observations", [])
        if isinstance(item, dict)
    ]

    end = _read_seconds(raw_event, "end_s", "end_timecode")
    repaired_boundary = False
    if end is None or end <= start:
        end = _latest_observation_time_after(start, observations)
        repaired_boundary = end is not None
    if (end is None or end <= start) and next_event_start_s is not None:
        end = next_event_start_s
        repaired_boundary = True

    needs_boundary_review = repaired_boundary or end is None or end <= start
    if end is None or end <= start:
        end = start
    end = _clamp(end, start, clip_duration_s)

    confidence = _coerce_seconds(raw_event.get("confidence"))
    explicit_boundary = not repaired_boundary and end > start
    if needs_boundary_review:
        boundary_confidence = 0.4 if end > start else 0.0
    elif explicit_boundary:
        boundary_confidence = confidence if confidence is not None else 1.0
    else:
        boundary_confidence = 0.0

    details = raw_event.get("details", raw_event.get("description", ""))
    return EventMetadata(
        start_s=round(start, 3),
        end_s=round(end, 3),
        anchor_s=round(anchor, 3),
        actor=str(raw_event["actor"]).strip() if raw_event.get("actor") else None,
        actor_type=str(raw_event["actor_type"]).strip() if raw_event.get("actor_type") else None,
        action_type=str(raw_event["action_type"]).strip() if raw_event.get("action_type") else None,
        ability_name=str(raw_event["ability_name"]).strip() if raw_event.get("ability_name") else None,
        labels=_string_list(raw_event.get("labels")),
        targets=_string_list(raw_event.get("targets")),
        details=str(details).strip(),
        observations=observations,
        boundary_evidence=_boundary_evidence(raw_event),
        confidence=confidence,
        needs_boundary_review=needs_boundary_review,
        boundary_confidence=round(max(min(boundary_confidence, 1.0), 0.0), 3),
    )
