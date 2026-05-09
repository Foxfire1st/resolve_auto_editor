from __future__ import annotations

from pathlib import Path

from src.orchestrator.assembly.models import (
    AssemblyCandidate,
    AssemblyProposal,
    AssemblySlot,
)
from src.orchestrator.assembly.review import (
    audit_assembly_quality,
    build_review_prompt,
    review_proposal,
    write_quality_report,
)
from src.orchestrator.models import SourceRange


def test_review_proposal_flags_unresolved_and_low_score_slots() -> None:
    proposal = AssemblyProposal(
        project_name="Demo",
        profile_name="hsr",
        slots=[
            AssemblySlot(
                slot_id="B001",
                narration_ids=["L001"],
                timeline_start=0.0,
                timeline_duration=3.0,
                visual_requirement="Show combat.",
                selected=AssemblyCandidate(
                    asset_id="weak_clip",
                    source_range=SourceRange(start=0.0, duration=3.0),
                    score=0.1,
                ),
            ),
            AssemblySlot(
                slot_id="B002",
                narration_ids=["L002"],
                timeline_start=3.0,
                timeline_duration=2.0,
                visual_requirement="Show menu.",
                unresolved_reason="No menu candidate found.",
            ),
        ],
    )

    report = review_proposal(proposal)

    assert len(report.suggestions) == 2
    assert {suggestion.slot_id for suggestion in report.suggestions} == {"B001", "B002"}
    assert all(suggestion.action == "flag" for suggestion in report.suggestions)


def test_review_proposal_accepts_injected_reviewer() -> None:
    proposal = AssemblyProposal(
        project_name="Demo",
        profile_name="hsr",
        slots=[
            AssemblySlot(
                slot_id="B001",
                narration_ids=["L001"],
                timeline_start=0.0,
                timeline_duration=3.0,
                visual_requirement="Show combat.",
            )
        ],
    )

    def fake_reviewer(payload):
        assert payload["slots"][0]["slot_id"] == "B001"
        return {
            "summary": "Swap suggested.",
            "suggestions": [
                {
                    "slot_id": "B001",
                    "action": "swap",
                    "candidate_asset_id": "better_clip",
                    "reason": "Alternative better matches the VO.",
                    "confidence": 0.8,
                }
            ],
        }

    report = review_proposal(proposal, reviewer=fake_reviewer)

    assert report.suggestions[0].action == "swap"
    assert report.suggestions[0].candidate_asset_id == "better_clip"


def test_build_review_prompt_includes_selected_and_alternatives() -> None:
    proposal = AssemblyProposal(
        project_name="Demo",
        profile_name="hsr",
        slots=[
            AssemblySlot(
                slot_id="B001",
                narration_ids=["L001"],
                timeline_start=0.0,
                timeline_duration=3.0,
                visual_requirement="Show combat.",
                selected=AssemblyCandidate(
                    asset_id="selected_clip",
                    source_range=SourceRange(start=0.0, duration=3.0),
                ),
                alternatives=[
                    AssemblyCandidate(
                        asset_id="alt_clip",
                        source_range=SourceRange(start=4.0, duration=3.0),
                    )
                ],
            )
        ],
    )

    prompt = build_review_prompt(proposal)

    assert "selected_clip" in prompt
    assert "alt_clip" in prompt


def test_audit_assembly_quality_flags_manifest_dump_failure_modes() -> None:
    proposal = AssemblyProposal(
        project_name="Demo",
        profile_name="hsr",
        slots=[
            AssemblySlot(
                slot_id="B001",
                narration_ids=["L001"],
                timeline_start=0.0,
                timeline_duration=24.0,
                visual_requirement="Show combat.",
                selected=AssemblyCandidate(
                    asset_id="ultimate",
                    source_range=SourceRange(start=0.0, duration=24.0),
                    score=1.0,
                    provenance=["manifest"],
                ),
            ),
            AssemblySlot(
                slot_id="B002",
                narration_ids=["L002"],
                timeline_start=24.0,
                timeline_duration=30.0,
                visual_requirement="Show combat.",
                selected=AssemblyCandidate(
                    asset_id="ultimate",
                    source_range=SourceRange(start=0.0, duration=30.0),
                    score=1.0,
                    provenance=["manifest"],
                ),
            ),
        ],
    )

    report = audit_assembly_quality(proposal)

    assert report.long_single_clip_slots == ["B001", "B002"]
    assert report.manifest_selected == 2
    assert report.event_selected == 0
    assert report.zero_start_selected == 2
    assert report.repeated_asset_ids == ["ultimate"]
    assert report.low_editorial_usefulness is True


def test_review_proposal_flags_manifest_dump_failure_modes() -> None:
    proposal = AssemblyProposal(
        project_name="Demo",
        profile_name="hsr",
        slots=[
            AssemblySlot(
                slot_id="B001",
                narration_ids=["L001"],
                timeline_start=0.0,
                timeline_duration=24.0,
                visual_requirement="Show combat.",
                selected=AssemblyCandidate(
                    asset_id="ultimate",
                    source_range=SourceRange(start=0.0, duration=24.0),
                    score=1.0,
                    provenance=["manifest"],
                ),
            ),
            AssemblySlot(
                slot_id="B002",
                narration_ids=["L002"],
                timeline_start=24.0,
                timeline_duration=30.0,
                visual_requirement="Show combat.",
                selected=AssemblyCandidate(
                    asset_id="ultimate",
                    source_range=SourceRange(start=0.0, duration=30.0),
                    score=1.0,
                    provenance=["manifest"],
                ),
            ),
        ],
    )

    report = review_proposal(proposal)

    reasons = [suggestion.reason for suggestion in report.suggestions]
    assert any("Long single-clip coverage" in reason for reason in reasons)
    assert any("Manifest-only selection" in reason for reason in reasons)
    assert any("start at 0.0" in reason for reason in reasons)
    assert any("repeated across multiple slots" in reason for reason in reasons)


def test_write_quality_report_emits_json_and_markdown(tmp_path: Path) -> None:
    proposal = AssemblyProposal(
        project_name="Demo",
        profile_name="hsr",
        slots=[
            AssemblySlot(
                slot_id="B001",
                narration_ids=["L001"],
                timeline_start=0.0,
                timeline_duration=24.0,
                visual_requirement="Show combat.",
                selected=AssemblyCandidate(
                    asset_id="ultimate",
                    source_range=SourceRange(start=0.0, duration=24.0),
                    score=1.0,
                    provenance=["manifest"],
                ),
            )
        ],
    )
    report = audit_assembly_quality(proposal)
    path = tmp_path / "assembly_quality_report.json"

    write_quality_report(report, path)

    assert path.exists()
    assert path.with_suffix(".md").exists()
    assert '"manifest_selected": 1' in path.read_text(encoding="utf-8")
    assert "# Assembly Quality Report" in path.with_suffix(".md").read_text(encoding="utf-8")
