from __future__ import annotations

import json
from src.orchestrator.assembly.export import proposal_to_semantic_edit
from src.orchestrator.assembly.models import (
    AssemblyCandidate,
    AssemblyProposal,
    AssemblySlot,
)
from src.orchestrator.models import SourceRange


def test_proposal_to_semantic_edit_skips_unresolved_slots() -> None:
    proposal = AssemblyProposal(
        project_name="Demo",
        profile_name="hsr",
        slots=[
            AssemblySlot(
                slot_id="B001",
                narration_ids=["L001"],
                timeline_start=0.0,
                timeline_duration=4.0,
                visual_requirement="Show combat.",
                selected=AssemblyCandidate(
                    asset_id="combat_clip",
                    source_range=SourceRange(start=2.0, duration=4.0),
                    event_ids=["event-1"],
                    reasons=["event metadata score 1.5"],
                    debug_summary="event event-1: combat clip summary; source=2.00+4.00s; score=1.500; boundary=1.000",
                ),
            ),
            AssemblySlot(
                slot_id="B002",
                narration_ids=["L002"],
                timeline_start=4.0,
                timeline_duration=3.0,
                visual_requirement="Show missing menu.",
                unresolved_reason="No metadata candidate found for slot.",
            ),
        ],
    )

    edit = proposal_to_semantic_edit(proposal)

    assert edit.project_name == "Demo"
    assert len(edit.segments) == 1
    assert edit.segments[0].id == "B001"
    assert edit.segments[0].video_clips[0].asset_id == "combat_clip"
    assert (
        edit.segments[0].video_clips[0].description
        == "event event-1: combat clip summary; source=2.00+4.00s; score=1.500; boundary=1.000"
    )
    assert edit.segments[0].narration_span is not None
    assert edit.segments[0].narration_span.duration == 4.0


def test_proposal_to_semantic_edit_exports_multi_clip_slot() -> None:
    proposal = AssemblyProposal(
        project_name="Demo",
        profile_name="hsr",
        slots=[
            AssemblySlot(
                slot_id="B001",
                narration_ids=["L001"],
                timeline_start=0.0,
                timeline_duration=6.0,
                visual_requirement="Show setup and payoff.",
                selected_clips=[
                    AssemblyCandidate(
                        asset_id="setup_clip",
                        source_range=SourceRange(start=1.0, duration=3.0),
                        reasons=["manifest keyword match"],
                    ),
                    AssemblyCandidate(
                        asset_id="payoff_clip",
                        source_range=SourceRange(start=9.0, duration=3.0),
                        reasons=["event metadata match"],
                    ),
                ],
            )
        ],
    )

    edit = proposal_to_semantic_edit(proposal)

    assert len(edit.segments) == 1
    assert edit.segments[0].primary_clip is not None
    assert edit.segments[0].primary_clip.asset_id == "setup_clip"
    assert [clip.asset_id for clip in edit.segments[0].video_clips] == [
        "setup_clip",
        "payoff_clip",
    ]


def test_proposal_to_semantic_edit_repeats_underfilled_source_duration() -> None:
    proposal = AssemblyProposal(
        project_name="Demo",
        profile_name="hsr",
        slots=[
            AssemblySlot(
                slot_id="B001",
                narration_ids=["L001"],
                timeline_start=0.0,
                timeline_duration=8.0,
                visual_requirement="Show one short matching moment.",
                selected=AssemblyCandidate(
                    asset_id="short_clip",
                    source_range=SourceRange(start=4.0, duration=2.0),
                    reasons=["only exact metadata match"],
                    warnings=["underfills narration span"],
                ),
            )
        ],
    )

    edit = proposal_to_semantic_edit(proposal)

    assert edit.segments[0].narration_span is not None
    assert edit.segments[0].narration_span.duration == 8.0
    assert [clip.source_range.duration for clip in edit.segments[0].video_clips if clip.source_range] == [
        2.0,
        2.0,
        2.0,
        2.0,
    ]


def test_proposal_to_semantic_edit_writes_unresolved_director_notes(tmp_path) -> None:
    notes_path = tmp_path / "assembly_director_notes.json"
    proposal = AssemblyProposal(
        project_name="Demo",
        profile_name="hsr",
        slots=[
            AssemblySlot(
                slot_id="B001",
                beat_id="B001",
                narration_ids=["L001"],
                timeline_start=0.0,
                timeline_duration=3.0,
                visual_requirement="Show missing menu.",
                vo_excerpt="The build menu matters here.",
                unresolved_reason="No candidate satisfied the menu constraint.",
            )
        ],
    )

    edit = proposal_to_semantic_edit(proposal, director_notes_path=notes_path)

    assert edit.segments == []
    payload = json.loads(notes_path.read_text(encoding="utf-8"))
    assert payload["entries"][0]["clip_asset_id"] == "__UNRESOLVED__"
    assert payload["entries"][0]["reasoning"] == "No candidate satisfied the menu constraint."
    assert notes_path.with_suffix(".md").exists()
