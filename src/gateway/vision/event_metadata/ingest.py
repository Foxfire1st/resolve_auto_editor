"""Ingest Gemini JSON artifacts into event metadata storage."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import sqlite3

from gateway.vision.timecode import timecode_to_seconds

from . import db
from .config import get_settings
from .models import StoredEvent
from .normalize import normalize_event_metadata

TAG_PATTERN = re.compile(r"[^a-z0-9]+")


def ingest_gemini_json(
    video_path: Path,
    gemini_json: Path,
    *,
    profile_name: str | None = None,
    database_path: Path | str | None = None,
    clip_duration_s: float | None = None,
    connection: sqlite3.Connection | None = None,
) -> list[StoredEvent]:
    """Persist normalized event metadata from one Gemini JSON artifact."""

    video_path = video_path.expanduser()
    gemini_json = gemini_json.expanduser()
    if not gemini_json.exists():
        raise FileNotFoundError(gemini_json)

    settings = get_settings(profile_name=profile_name, database_path=database_path)
    payload = _read_json(gemini_json)
    summary = _summary_payload(payload)
    raw_events = list(_iter_raw_events(payload))
    if not raw_events:
        raise ValueError(f"No Gemini events found in {gemini_json}")

    own_connection = connection is None
    connection = connection or db.connect(settings.database_path)
    try:
        db.init_db(connection)
        video_id = video_path.stem
        source_path = str(video_path)
        clip_tags = _summary_tags(summary)
        _replace_clip(connection, settings.profile, video_id, source_path, clip_duration_s, summary, clip_tags)
        _delete_clip_events(connection, settings.profile, video_id)

        starts = [_event_start_seconds(event) for event in raw_events]
        stored: list[StoredEvent] = []
        for index, raw_event in enumerate(raw_events):
            next_start = next((start for start in starts[index + 1 :] if start is not None), None)
            try:
                metadata = normalize_event_metadata(
                    raw_event,
                    clip_duration_s=clip_duration_s,
                    next_event_start_s=next_start,
                )
            except ValueError:
                continue
            event_tags = sorted({*clip_tags, *_event_tags(metadata)})
            event_id = _event_id(settings.profile, video_id, metadata.start_s, metadata.end_s, index, metadata.details)
            row = StoredEvent(
                id=event_id,
                profile=settings.profile,
                video_id=video_id,
                source_path=source_path,
                start_s=metadata.start_s,
                end_s=metadata.end_s,
                anchor_s=metadata.anchor_s,
                actor=metadata.actor,
                actor_type=metadata.actor_type,
                action_type=metadata.action_type,
                ability_name=metadata.ability_name,
                details=metadata.details,
                labels=metadata.labels,
                targets=metadata.targets,
                observations=metadata.observations,
                boundary_evidence=metadata.boundary_evidence,
                confidence=metadata.confidence,
                needs_boundary_review=metadata.needs_boundary_review,
                boundary_confidence=metadata.boundary_confidence,
                tags=event_tags,
            )
            _insert_event(connection, row, raw_event)
            stored.append(row)
        if own_connection:
            connection.commit()
        return stored
    finally:
        if own_connection:
            connection.close()


def _read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _iter_raw_events(payload: Any) -> Iterable[dict[str, Any]]:
    if isinstance(payload, dict):
        events = payload.get("events")
        if isinstance(events, list):
            yield from (event for event in events if isinstance(event, dict))
    elif isinstance(payload, list):
        yield from (event for event in payload if isinstance(event, dict))


def _summary_payload(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    candidate = payload.get("clip_summary") or payload.get("summary")
    return candidate if isinstance(candidate, dict) else payload


def _event_start_seconds(raw_event: dict[str, Any]) -> float | None:
    for seconds_key, timecode_key in (("start_s", "start_timecode"), ("time_s", "timecode")):
        value = raw_event.get(seconds_key)
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value)
            except ValueError:
                pass
        timecode = raw_event.get(timecode_key)
        if isinstance(timecode, str):
            seconds = timecode_to_seconds(timecode)
            if seconds is not None:
                return seconds
    return None


def _replace_clip(
    connection: sqlite3.Connection,
    profile: str,
    video_id: str,
    source_path: str,
    duration_s: float | None,
    summary: dict[str, Any],
    tags: list[str],
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    connection.execute(
        """
        INSERT INTO event_metadata_clips (
            profile, video_id, source_path, duration_s, title, description,
            tags_json, summary_json, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(profile, video_id) DO UPDATE SET
            source_path=excluded.source_path,
            duration_s=excluded.duration_s,
            title=excluded.title,
            description=excluded.description,
            tags_json=excluded.tags_json,
            summary_json=excluded.summary_json,
            updated_at=excluded.updated_at
        """,
        (
            profile,
            video_id,
            source_path,
            duration_s,
            _optional_str(summary.get("title") or summary.get("summary")),
            _optional_str(summary.get("description") or summary.get("details")),
            _json(tags),
            _json(summary),
            now,
        ),
    )


def _delete_clip_events(connection: sqlite3.Connection, profile: str, video_id: str) -> None:
    event_ids = [
        row["id"]
        for row in connection.execute(
            "SELECT id FROM event_metadata_events WHERE profile = ? AND video_id = ?",
            (profile, video_id),
        )
    ]
    for event_id in event_ids:
        connection.execute("DELETE FROM event_metadata_fts WHERE event_id = ?", (event_id,))
    connection.execute(
        "DELETE FROM event_metadata_events WHERE profile = ? AND video_id = ?",
        (profile, video_id),
    )


def _insert_event(connection: sqlite3.Connection, row: StoredEvent, raw_event: dict[str, Any]) -> None:
    now = datetime.now(timezone.utc).isoformat()
    connection.execute(
        """
        INSERT INTO event_metadata_events (
            id, profile, video_id, source_path, start_s, end_s, anchor_s,
            actor, actor_type, action_type, ability_name, details,
            labels_json, targets_json, observations_json, boundary_evidence_json,
            confidence, needs_boundary_review, boundary_confidence, tags_json, raw_json, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            row.id,
            row.profile,
            row.video_id,
            row.source_path,
            row.start_s,
            row.end_s,
            row.anchor_s,
            row.actor,
            row.actor_type,
            row.action_type,
            row.ability_name,
            row.details,
            _json(row.labels),
            _json(row.targets),
            _json([observation.model_dump() for observation in row.observations]),
            _json(row.boundary_evidence.model_dump()),
            row.confidence,
            int(row.needs_boundary_review),
            row.boundary_confidence,
            _json(row.tags),
            _json(raw_event),
            now,
        ),
    )
    connection.execute("DELETE FROM event_metadata_fts WHERE event_id = ?", (row.id,))
    connection.execute(
        """
        INSERT INTO event_metadata_fts (
            event_id, profile, video_id, actor, action_type, ability_name,
            labels, tags, details, observations
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            row.id,
            row.profile,
            row.video_id,
            row.actor or "",
            row.action_type or "",
            row.ability_name or "",
            " ".join(row.labels),
            " ".join(row.tags),
            row.details,
            _observations_text(row),
        ),
    )


def _event_id(profile: str, video_id: str, start_s: float, end_s: float, index: int, details: str) -> str:
    text = f"{profile}\0{video_id}\0{start_s:.3f}\0{end_s:.3f}\0{index}\0{details}"
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _summary_tags(summary: dict[str, Any]) -> list[str]:
    tags: set[str] = {"#gemini"}
    for key in ("tags", "party", "enemies"):
        value = summary.get(key)
        if isinstance(value, list):
            tags.update(tag for item in value if (tag := _normalize_tag(str(item))))
    for key in ("mode", "stage_name"):
        if tag := _normalize_tag(_optional_str(summary.get(key))):
            tags.add(tag)
    if summary.get("is_combat"):
        tags.add("#combat")
    return sorted(tags)


def _event_tags(metadata: Any) -> list[str]:
    tags: set[str] = set()
    for value in (metadata.actor, metadata.actor_type, metadata.action_type, metadata.ability_name):
        if tag := _normalize_tag(value):
            tags.add(tag)
    for collection in (metadata.labels, metadata.targets):
        tags.update(tag for item in collection if (tag := _normalize_tag(item)))
    return sorted(tags)


def _normalize_tag(value: str | None) -> str | None:
    if not value:
        return None
    trimmed = value.strip()
    if not trimmed:
        return None
    if trimmed.startswith("#"):
        trimmed = trimmed[1:]
    slug = TAG_PATTERN.sub("", trimmed.lower())
    return f"#{slug}" if slug else None


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _observations_text(row: StoredEvent) -> str:
    parts: list[str] = []
    for observation in row.observations:
        parts.extend(observation.labels)
        for value in (observation.kind, observation.text, observation.details):
            if value:
                parts.append(value)
    evidence = row.boundary_evidence
    for value in (evidence.start, evidence.end, evidence.anchor, evidence.notes):
        if value:
            parts.append(value)
    return " ".join(parts)
