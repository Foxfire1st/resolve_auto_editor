from __future__ import annotations

import json
from pathlib import Path

from gateway.vision.event_metadata.ingest import ingest_gemini_json
from src.orchestrator.assembly.candidate_matrix import (
    CandidateMatrixBuilder,
    build_slots_from_beat_map,
)
from src.orchestrator.models import Beat, BeatMap, ShotInstruction, ShotPlan


def _ingest_payload(tmp_path: Path, database: Path, stem: str, payload: dict, duration_s: float = 20.0) -> None:
    video = tmp_path / f"{stem}.mp4"
    video.write_bytes(b"demo")
    gemini_json = tmp_path / f"{stem}_gemini.json"
    gemini_json.write_text(json.dumps(payload), encoding="utf-8")
    ingest_gemini_json(video, gemini_json, database_path=database, profile_name="hsr", clip_duration_s=duration_s)


def test_build_slots_from_beat_map_falls_back_to_segment_slots() -> None:
    beat_map = BeatMap(
        beats=[
            Beat(
                id="B001",
                segment_ids=["L001", "L002"],
                visual_intent="Show Feixiao combat.",
                search_queries=["Feixiao", "combat"],
            )
        ]
    )
    segment_lookup = {
        "L001": {"id": "L001", "t0": 1.0, "t1": 2.0, "text": "First line."},
        "L002": {"id": "L002", "t0": 2.0, "t1": 4.5, "text": "Second line."},
    }

    slots = build_slots_from_beat_map(beat_map, segment_lookup)

    assert len(slots) == 2
    assert slots[0].slot_id == "B001-S01"
    assert slots[0].timeline_start == 1.0
    assert slots[0].timeline_duration == 1.0
    assert slots[0].vo_excerpt == "First line."
    assert slots[1].slot_id == "B001-S02"
    assert slots[1].timeline_start == 2.0
    assert slots[1].timeline_duration == 2.5
    assert slots[1].vo_excerpt == "Second line."


def test_build_slots_from_beat_map_uses_shot_plan_when_available() -> None:
    beat_map = BeatMap(
        beats=[
            Beat(
                id="B001",
                segment_ids=["L001", "L002"],
                visual_intent="Show Feixiao combat.",
                search_queries=["Feixiao", "combat"],
            )
        ]
    )
    segment_lookup = {
        "L001": {"id": "L001", "t0": 1.0, "t1": 2.0, "text": "First line."},
        "L002": {"id": "L002", "t0": 2.0, "t1": 4.5, "text": "Second line."},
    }
    shot_plan = ShotPlan(
        beat_id="B001",
        shot_instructions=[
            ShotInstruction(
                shot_id="B001-SHOT-01",
                narration_ids=["L001", "L002"],
                vo_excerpt="Planned excerpt.",
                visual_goal="Show Feixiao using Terrasplit in combat.",
                keywords=["Terrasplit", "combat"],
                required_events=["Terrasplit"],
                preferred_assets=["combat_clip"],
                min_clips=1,
                max_clips=2,
                coverage_note="Planned coverage note.",
            )
        ],
    )

    slots = build_slots_from_beat_map(beat_map, segment_lookup, shot_plans={"B001": shot_plan})

    assert len(slots) == 1
    assert slots[0].slot_id == "B001-SHOT-01"
    assert slots[0].timeline_start == 1.0
    assert slots[0].timeline_duration == 3.5
    assert slots[0].vo_excerpt == "First line. Second line."
    assert slots[0].visual_requirement == "Show Feixiao using Terrasplit in combat."
    assert slots[0].preferred_assets == ["combat_clip"]
    assert slots[0].min_clips == 1
    assert slots[0].max_clips == 2


def test_candidate_matrix_uses_manifest_and_shared_event_connection(tmp_path: Path) -> None:
    database = tmp_path / "event_metadata.db"
    manifest = tmp_path / "manifest.ndjson"
    manifest_video = tmp_path / "formshift.mp4"
    manifest_video.write_bytes(b"demo")
    manifest.write_text(
        json.dumps(
            {
                "asset_id": "manifest_formshift",
                "summary": "Feixiao Formshift trace details in the Traces menu.",
                "tags": ["trace", "ui"],
                "duration": 8.0,
                "source_path": str(manifest_video),
            }
        )
        + "\n",
        encoding="utf-8",
    )
    _ingest_payload(
        tmp_path,
        database,
        "combat_clip",
        {
            "clip_summary": {"title": "Feixiao combat", "party": ["Feixiao"], "is_combat": True},
            "events": [
                {
                    "start_s": 3.0,
                    "end_s": 9.0,
                    "anchor_s": 5.0,
                    "actor": "Feixiao",
                    "action_type": "Ultimate",
                    "ability_name": "Terrasplit",
                    "details": "Feixiao uses Terrasplit in combat.",
                    "labels": ["Feixiao", "Terrasplit"],
                    "confidence": 0.95,
                }
            ],
        },
    )
    slot = build_slots_from_beat_map(
        BeatMap(
            beats=[
                Beat(
                    id="B001",
                    segment_ids=["L001"],
                    visual_intent="Show Feixiao using Terrasplit in combat.",
                    search_queries=["Formshift", "Terrasplit", "combat"],
                )
            ]
        ),
        {"L001": {"id": "L001", "t0": 0.0, "t1": 5.0, "text": "Use Terrasplit in combat."}},
    )[0]

    with CandidateMatrixBuilder(profile_name="hsr", manifest_path=manifest, database_path=database) as builder:
        matrix = builder.build_matrix([slot], limit_per_slot=5)
        proposal = builder.build_proposal(project_name="Demo", slots=[slot], limit_per_slot=5)

    candidates = matrix[slot.slot_id]
    asset_ids = {candidate.asset_id for candidate in candidates}
    manifest_candidate = next(candidate for candidate in candidates if candidate.asset_id == "manifest_formshift")
    event_candidate = next(candidate for candidate in candidates if candidate.asset_id == "combat_clip")

    assert "manifest_formshift" in asset_ids
    assert "combat_clip" in asset_ids
    assert manifest_candidate.debug_summary is not None
    assert "formshift" in manifest_candidate.debug_summary.lower()
    assert "formshift" in manifest_candidate.debug_details["matched_terms"]
    assert event_candidate.debug_summary is not None
    assert event_candidate.debug_summary.startswith("event ")
    assert "Terrasplit" in event_candidate.debug_summary
    assert event_candidate.debug_details["event_id"] in event_candidate.debug_summary
    assert proposal.slots[0].selected is not None
    assert proposal.slots[0].selected.asset_id == "combat_clip"
    assert proposal.slots[0].selected.provenance == ["event_metadata"]
    assert proposal.global_notes[0] == "1 of 1 slots resolved from metadata candidates."
