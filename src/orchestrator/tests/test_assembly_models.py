from __future__ import annotations

from src.orchestrator.assembly.models import (
    AssemblyCandidate,
    AssemblyProposal,
    AssemblySlot,
)
from src.orchestrator.models import SourceRange


def test_assembly_proposal_models_are_json_friendly() -> None:
    candidate = AssemblyCandidate(
        asset_id="clip_a",
        source_path="/tmp/clip_a.mp4",
        source_range=SourceRange(start=1.0, duration=2.5),
        event_ids=["event-1"],
        score=4.2,
        reasons=["metadata match"],
        provenance=["event_metadata"],
    )
    proposal = AssemblyProposal(
        project_name="Demo",
        profile_name="default",
        slots=[
            AssemblySlot(
                slot_id="B001",
                narration_ids=["L001"],
                timeline_start=0.0,
                timeline_duration=2.5,
                visual_requirement="Show clip A",
                selected=candidate,
            )
        ],
    )

    payload = proposal.model_dump()

    assert payload["slots"][0]["selected"]["source_range"]["start"] == 1.0
    assert payload["slots"][0]["selected"]["provenance"] == ["event_metadata"]
