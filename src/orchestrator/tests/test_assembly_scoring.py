from __future__ import annotations

from src.orchestrator.assembly.models import AssemblyCandidate, AssemblySlot
from src.orchestrator.assembly.scoring import assemble_slots
from src.orchestrator.models import SourceRange


def _slot(slot_id: str, start: float) -> AssemblySlot:
    return AssemblySlot(
        slot_id=slot_id,
        narration_ids=[slot_id.replace("B", "L")],
        timeline_start=start,
        timeline_duration=3.0,
        visual_requirement=f"Requirement {slot_id}",
    )


def _candidate(
    asset_id: str,
    score: float,
    *,
    debug_summary: str | None = None,
    debug_details: dict[str, object] | None = None,
    provenance: list[str] | None = None,
) -> AssemblyCandidate:
    return AssemblyCandidate(
        asset_id=asset_id,
        source_range=SourceRange(start=0.0, duration=3.0),
        score=score,
        reasons=[f"base score {score}"],
        debug_summary=debug_summary,
        debug_details=debug_details or {},
        provenance=provenance or [],
    )


def test_assemble_slots_preserves_scarce_candidate_for_later_slot() -> None:
    slots = [_slot("B001", 0.0), _slot("B002", 3.0)]
    matrix = {
        "B001": [_candidate("scarce_clip", 10.0), _candidate("backup_clip", 9.0)],
        "B002": [_candidate("scarce_clip", 5.0)],
    }

    assigned = assemble_slots(slots, matrix)

    assert assigned[0].selected is not None
    assert assigned[0].selected.asset_id == "backup_clip"
    assert assigned[1].selected is not None
    assert assigned[1].selected.asset_id == "scarce_clip"
    assert "reserved for more constrained later slot" in assigned[0].alternatives[0].reasons


def test_assemble_slots_rejects_wrong_mode_before_ranking() -> None:
    slot = AssemblySlot(
        slot_id="B010",
        narration_ids=["L010"],
        timeline_start=0.0,
        timeline_duration=3.0,
        visual_requirement="Show the Feixiao Light Cone menu screen.",
        vo_excerpt="Show the menu details.",
        keywords=["Feixiao Light Cone menu"],
    )
    matrix = {
        "B010": [
            _candidate(
                "combat_clip",
                5.0,
                debug_summary="event evt-1: Feixiao uses her ultimate in combat; source=4.00+3.00s; score=5.000; boundary=1.000",
                debug_details={"summary": "Feixiao uses her ultimate in combat.", "tags": ["combat", "ultimate"]},
                provenance=["event_metadata"],
            ),
            _candidate(
                "light_cone_menu",
                0.4,
                debug_summary="manifest light_cone_menu: matched [feixiao, light cone, menu]; summary=Feixiao light cone menu screen details.; source=0.00+3.00s",
                debug_details={"manifest_summary": "Feixiao light cone menu screen details.", "matched_terms": ["feixiao", "light cone", "menu"]},
                provenance=["manifest"],
            ),
        ]
    }

    assigned = assemble_slots([slot], matrix)

    assert assigned[0].selected is not None
    assert assigned[0].selected.asset_id == "light_cone_menu"


def test_assemble_slots_marks_slot_unresolved_when_all_candidates_fail_constraints() -> None:
    slot = AssemblySlot(
        slot_id="B011",
        narration_ids=["L011"],
        timeline_start=0.0,
        timeline_duration=3.0,
        visual_requirement="Show the Feixiao Light Cone menu screen.",
        vo_excerpt="Show the menu details.",
        keywords=["Feixiao Light Cone menu"],
    )
    matrix = {
        "B011": [
            _candidate(
                "combat_clip",
                5.0,
                debug_summary="event evt-1: Feixiao uses her ultimate in combat; source=4.00+3.00s; score=5.000; boundary=1.000",
                debug_details={"summary": "Feixiao uses her ultimate in combat.", "tags": ["combat", "ultimate"]},
                provenance=["event_metadata"],
            )
        ]
    }

    assigned = assemble_slots([slot], matrix)

    assert assigned[0].selected is None
    assert assigned[0].unresolved_reason is not None
    assert "hard visual constraints" in assigned[0].unresolved_reason
    assert "wrong visual mode (combat)" in assigned[0].unresolved_reason


def test_assemble_slots_rejects_weak_manifest_match_for_long_slot() -> None:
    slot = AssemblySlot(
        slot_id="B012",
        narration_ids=["L012"],
        timeline_start=0.0,
        timeline_duration=6.0,
        visual_requirement="Show Feixiao combat footage.",
        vo_excerpt="Show the combat moment.",
        keywords=["Feixiao", "combat"],
    )
    matrix = {
        "B012": [
            _candidate(
                "weak_manifest",
                0.5,
                debug_summary="manifest weak_manifest: matched [feixiao]; summary=Feixiao footage.; source=0.00+6.00s",
                debug_details={"match_count": 1, "manifest_summary": "Feixiao footage."},
                provenance=["manifest"],
            )
        ]
    }

    assigned = assemble_slots([slot], matrix)

    assert assigned[0].selected is None
    assert assigned[0].unresolved_reason is not None
    assert "weak one-term manifest match" in assigned[0].unresolved_reason


def test_assemble_slots_keeps_weak_manifest_match_for_short_slot_without_fallback() -> None:
    slot = AssemblySlot(
        slot_id="B013",
        narration_ids=["L013"],
        timeline_start=0.0,
        timeline_duration=2.0,
        visual_requirement="Show Feixiao combat footage.",
        vo_excerpt="Quick combat insert.",
        keywords=["Feixiao", "combat"],
    )
    matrix = {
        "B013": [
            _candidate(
                "weak_manifest",
                0.5,
                debug_summary="manifest weak_manifest: matched [feixiao]; summary=Feixiao footage.; source=0.00+2.00s",
                debug_details={"match_count": 1, "manifest_summary": "Feixiao footage."},
                provenance=["manifest"],
            )
        ]
    }

    assigned = assemble_slots([slot], matrix)

    assert assigned[0].selected is not None
    assert assigned[0].selected.asset_id == "weak_manifest"


def test_assemble_slots_selects_multiple_clips_for_multi_clip_slot() -> None:
    slot = AssemblySlot(
        slot_id="B014",
        narration_ids=["L014"],
        timeline_start=0.0,
        timeline_duration=12.0,
        visual_requirement="Show setup and payoff.",
        min_clips=2,
        max_clips=3,
    )
    matrix = {
        "B014": [
            _candidate("setup_clip", 5.0),
            _candidate("payoff_clip", 4.5),
            _candidate("third_clip", 4.0),
        ]
    }

    assigned = assemble_slots([slot], matrix)

    assert assigned[0].selected is not None
    assert assigned[0].selected.asset_id == "setup_clip"
    assert [candidate.asset_id for candidate in assigned[0].selected_clips] == [
        "setup_clip",
        "payoff_clip",
    ]
    assert assigned[0].alternatives[0].asset_id == "third_clip"
