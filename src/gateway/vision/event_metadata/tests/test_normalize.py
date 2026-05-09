from __future__ import annotations

import pytest

from gateway.vision.event_metadata import normalize_event_metadata


def test_normalize_event_metadata_accepts_explicit_span():
    event = normalize_event_metadata(
        {
            "start_s": 10.0,
            "end_s": 18.5,
            "anchor_s": 12.0,
            "actor": "Feixiao",
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
        clip_duration_s=30.0,
    )

    assert event.start_s == pytest.approx(10.0)
    assert event.end_s == pytest.approx(18.5)
    assert event.anchor_s == pytest.approx(12.0)
    assert event.needs_boundary_review is False
    assert event.boundary_confidence == pytest.approx(0.92)
    assert event.labels == ["Terrasplit", "Boltsunder Blitz"]


def test_normalize_event_metadata_flags_observation_repaired_boundary():
    event = normalize_event_metadata(
        {
            "start_s": 4.0,
            "actor": "Enemy",
            "action_type": "Enemy Skill",
            "details": "Enemy attack with missing explicit end.",
            "observations": [
                {"time_s": 4.5, "kind": "label", "text": "enemy label"},
                {"time_s": 6.25, "kind": "damage", "text": "damage lands"},
            ],
        },
        clip_duration_s=8.0,
    )

    assert event.start_s == pytest.approx(4.0)
    assert event.end_s == pytest.approx(6.25)
    assert event.needs_boundary_review is True
    assert event.boundary_confidence == pytest.approx(0.4)


def test_normalize_event_metadata_flags_point_only_event():
    event = normalize_event_metadata(
        {
            "timecode": "00:05",
            "actor": "Unknown",
            "action_type": "Other",
            "description": "Legacy point event.",
        },
        clip_duration_s=10.0,
    )

    assert event.start_s == pytest.approx(5.0)
    assert event.end_s == pytest.approx(5.0)
    assert event.anchor_s == pytest.approx(5.0)
    assert event.needs_boundary_review is True
    assert event.boundary_confidence == pytest.approx(0.0)


def test_normalize_event_metadata_can_use_next_event_start_as_weak_end():
    event = normalize_event_metadata(
        {
            "start_s": 2.0,
            "details": "Missing end but followed by another event.",
        },
        clip_duration_s=20.0,
        next_event_start_s=5.5,
    )

    assert event.end_s == pytest.approx(5.5)
    assert event.needs_boundary_review is True
