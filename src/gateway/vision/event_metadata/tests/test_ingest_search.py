from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from gateway.vision.event_metadata.ingest import ingest_gemini_json
from gateway.vision.event_metadata.search import EventSearchFilters, search_events


def _write_sample_payload(path: Path) -> None:
    payload = {
        "clip_summary": {
            "title": "Combat clip",
            "description": "Feixiao and enemies in battle.",
            "mode": "moc",
            "party": ["Feixiao"],
            "enemies": ["Automaton"],
            "is_combat": True,
            "tags": ["showcase"],
        },
        "events": [
            {
                "start_s": 10.0,
                "end_s": 18.5,
                "anchor_s": 12.0,
                "actor": "Feixiao",
                "actor_type": "Ally",
                "action_type": "Ultimate",
                "ability_name": "Terrasplit",
                "labels": ["Terrasplit", "Boltsunder Blitz"],
                "details": "Feixiao performs her ultimate.",
                "boundary_evidence": {
                    "start": "ultimate label appears",
                    "end": "final damage resolves",
                },
                "confidence": 0.92,
            },
            {
                "start_s": 21.0,
                "actor": "Automaton",
                "actor_type": "Enemy",
                "action_type": "Enemy Skill",
                "labels": ["Enemy Strike"],
                "details": "Enemy attack with missing explicit end.",
                "observations": [
                    {"time_s": 22.25, "kind": "damage", "text": "damage lands"},
                ],
            },
        ],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_ingest_gemini_json_is_idempotent(tmp_path: Path) -> None:
    video = tmp_path / "combat_clip.mp4"
    video.write_bytes(b"demo")
    gemini_json = tmp_path / "combat_clip_gemini.json"
    database = tmp_path / "event_metadata.db"
    _write_sample_payload(gemini_json)

    first = ingest_gemini_json(video, gemini_json, database_path=database, profile_name="hsr", clip_duration_s=30.0)
    second = ingest_gemini_json(video, gemini_json, database_path=database, profile_name="hsr", clip_duration_s=30.0)

    assert len(first) == 2
    assert len(second) == 2
    with sqlite3.connect(database) as connection:
        event_count = connection.execute("SELECT count(*) FROM event_metadata_events").fetchone()[0]
        fts_count = connection.execute("SELECT count(*) FROM event_metadata_fts").fetchone()[0]
    assert event_count == 2
    assert fts_count == 2


def test_search_events_excludes_boundary_review_by_default(tmp_path: Path) -> None:
    video = tmp_path / "combat_clip.mp4"
    video.write_bytes(b"demo")
    gemini_json = tmp_path / "combat_clip_gemini.json"
    database = tmp_path / "event_metadata.db"
    _write_sample_payload(gemini_json)
    ingest_gemini_json(video, gemini_json, database_path=database, profile_name="hsr", clip_duration_s=30.0)

    trusted = search_events("Enemy Strike", database_path=database, profile_name="hsr")
    reviewed = search_events("Enemy Strike", database_path=database, profile_name="hsr", include_review=True)

    assert trusted == []
    assert len(reviewed) == 1
    assert reviewed[0].needs_boundary_review is True
    assert reviewed[0].start_s == 21.0
    assert reviewed[0].end_s == 22.25


def test_search_events_returns_trusted_span_shape(tmp_path: Path) -> None:
    video = tmp_path / "combat_clip.mp4"
    video.write_bytes(b"demo")
    gemini_json = tmp_path / "combat_clip_gemini.json"
    database = tmp_path / "event_metadata.db"
    _write_sample_payload(gemini_json)
    ingest_gemini_json(video, gemini_json, database_path=database, profile_name="hsr", clip_duration_s=30.0)

    results = search_events(
        "Terrasplit",
        database_path=database,
        profile_name="hsr",
        filters=EventSearchFilters(actor="Feixiao", action_type="Ultimate", required_tags=["#combat"]),
    )

    assert len(results) == 1
    result = results[0]
    assert result.video_id == "combat_clip"
    assert result.start_s == 10.0
    assert result.end_s == 18.5
    assert result.event_offset == 2.0
    assert result.source_path == str(video)
    assert "Terrasplit" in result.labels
    assert result.needs_boundary_review is False
