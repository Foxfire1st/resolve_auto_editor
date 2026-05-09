from __future__ import annotations

from pathlib import Path

from gateway.resolve.timeline_snapshot import ClipSnapshot, TimelineSnapshot
from src.orchestrator.assembly.feedback import compare_proposal_to_snapshot, run_resolve_feedback
from src.orchestrator.assembly.models import (
    AssemblyCandidate,
    AssemblyProposal,
    AssemblySlot,
)
from src.orchestrator.models import SourceRange


def _snapshot(*, record_frame: int = 0, media_name: str = "clip.mp4") -> TimelineSnapshot:
    return TimelineSnapshot(
        name="Demo",
        frame_rate="30",
        start_timecode="00:00:00:00",
        resolution_width=1920,
        resolution_height=1080,
        audio_track_subtypes={},
        clips=[
            ClipSnapshot(
                media_path=f"C:/media/{media_name}",
                media_name=media_name,
                track_type="video",
                track_index=1,
                media_type="video",
                record_frame=record_frame,
                source_start=0,
                source_end=90,
                duration=90,
            )
        ],
        media={},
        link_group_sizes={},
    )


def test_compare_proposal_to_snapshot_accepts_matching_clip() -> None:
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
                    asset_id="clip.mp4",
                    source_range=SourceRange(start=0.0, duration=3.0),
                ),
            )
        ],
    )

    report = compare_proposal_to_snapshot(proposal, _snapshot())

    assert report.notes == []


def test_compare_proposal_to_snapshot_matches_asset_stem_to_resolve_media_name() -> None:
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
                    asset_id="clip",
                    source_range=SourceRange(start=0.0, duration=3.0),
                ),
            )
        ],
    )

    report = compare_proposal_to_snapshot(proposal, _snapshot(media_name="clip.mp4"))

    assert report.notes == []


def test_compare_proposal_to_snapshot_flags_missing_clip() -> None:
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
                    asset_id="missing.mp4",
                    source_range=SourceRange(start=0.0, duration=3.0),
                ),
            )
        ],
    )

    report = compare_proposal_to_snapshot(proposal, _snapshot(media_name="other.mp4"))

    assert report.notes[0].severity == "warning"
    assert "not found" in report.notes[0].message


def test_run_resolve_feedback_uses_mocked_resolve_tools(tmp_path: Path) -> None:
    snapshot_path = tmp_path / "snapshot.json"
    _snapshot().to_json(snapshot_path)
    calls: list[str] = []

    class FakeResolveTools:
        def import_fcpxml(self, path, timeline_name=None):
            calls.append(f"import:{Path(path).name}:{timeline_name}")

        def capture_timeline_snapshot(self, destination=None):
            calls.append("snapshot")
            return {"snapshot_path": str(snapshot_path)}

        def render(self, destination):
            calls.append("render")
            return {"path": str(destination)}

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
                    asset_id="clip.mp4",
                    source_range=SourceRange(start=0.0, duration=3.0),
                ),
            )
        ],
    )

    report = run_resolve_feedback(
        proposal,
        resolve_tools=FakeResolveTools(),
        fcpxml_path=tmp_path / "edit.fcpxml",
        snapshot_path=snapshot_path,
        timeline_name="Assembly Live",
        render_proxy=True,
        render_path=tmp_path / "proxy.mp4",
    )

    assert calls == ["import:edit.fcpxml:Assembly Live", "snapshot", "render"]
    assert report.notes == []
    assert report.render_path is not None
