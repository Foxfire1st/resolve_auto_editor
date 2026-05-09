"""Resolve feedback comparison helpers for assembly proposals."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel

from gateway.resolve.timeline_snapshot import TimelineSnapshot
from src.orchestrator.assembly.models import AssemblyProposal, AssemblySlot


class ResolveFeedbackNote(BaseModel):
    """One comparison note between an assembly proposal and a Resolve snapshot."""

    slot_id: str
    severity: str
    message: str


class ResolveFeedbackReport(BaseModel):
    """Summary of Resolve feedback for a proposal."""

    timeline_name: str
    notes: list[ResolveFeedbackNote]
    render_path: str | None = None


def compare_proposal_to_snapshot(
    proposal: AssemblyProposal,
    snapshot: TimelineSnapshot | dict[str, Any],
    *,
    frame_tolerance: int = 2,
) -> ResolveFeedbackReport:
    """Compare selected proposal slots with a captured Resolve timeline snapshot."""

    resolved_snapshot = _coerce_snapshot(snapshot)
    fps = float(resolved_snapshot.frame_rate or 24)
    video_clips = [clip for clip in resolved_snapshot.clips if clip.track_type == "video"]
    notes: list[ResolveFeedbackNote] = []
    for slot in proposal.slots:
        selected_assets = _slot_asset_ids(slot)
        if not selected_assets:
            notes.append(
                ResolveFeedbackNote(
                    slot_id=slot.slot_id,
                    severity="info",
                    message=slot.unresolved_reason or "Slot unresolved before Resolve import.",
                )
            )
            continue
        expected_frame = round(slot.timeline_start * fps)
        matching = [
            clip
            for clip in video_clips
            if _clip_matches_any_asset(clip.media_path, clip.media_name, selected_assets)
        ]
        if not matching:
            notes.append(
                ResolveFeedbackNote(
                    slot_id=slot.slot_id,
                    severity="warning",
                    message=f"Selected asset not found in Resolve snapshot: {', '.join(selected_assets)}.",
                )
            )
            continue
        nearest = min(matching, key=lambda clip: abs(clip.record_frame - expected_frame))
        delta = abs(nearest.record_frame - expected_frame)
        if delta > frame_tolerance:
            notes.append(
                ResolveFeedbackNote(
                    slot_id=slot.slot_id,
                    severity="warning",
                    message=(
                        f"Selected asset appears {delta} frame(s) from expected start "
                        f"{expected_frame}."
                    ),
                )
            )
    return ResolveFeedbackReport(timeline_name=resolved_snapshot.name, notes=notes)


def run_resolve_feedback(
    proposal: AssemblyProposal,
    *,
    resolve_tools: Any,
    fcpxml_path: Path,
    snapshot_path: Path | None = None,
    timeline_name: str | None = None,
    render_proxy: bool = False,
    render_path: Path | None = None,
) -> ResolveFeedbackReport:
    """Import FCPXML through a Resolve tool object, capture a snapshot, and compare it."""

    resolve_tools.import_fcpxml(fcpxml_path, timeline_name=timeline_name)
    snapshot_result = resolve_tools.capture_timeline_snapshot(destination=snapshot_path)
    if isinstance(snapshot_result, dict) and "snapshot_path" in snapshot_result:
        snapshot = TimelineSnapshot.from_json(Path(snapshot_result["snapshot_path"]))
    else:
        snapshot = _coerce_snapshot(snapshot_result)

    report = compare_proposal_to_snapshot(proposal, snapshot)
    if render_proxy and hasattr(resolve_tools, "render"):
        render_result = resolve_tools.render(render_path or fcpxml_path.with_suffix(".mp4"))
        if isinstance(render_result, dict) and render_result.get("path"):
            report.render_path = str(render_result["path"])
        elif render_result is not None:
            report.render_path = str(render_result)
    return report


def _coerce_snapshot(snapshot: TimelineSnapshot | dict[str, Any]) -> TimelineSnapshot:
    if isinstance(snapshot, TimelineSnapshot):
        return snapshot
    return TimelineSnapshot.from_dict(snapshot)


def _slot_asset_ids(slot: AssemblySlot) -> list[str]:
    if slot.selected_clips:
        return [candidate.asset_id for candidate in slot.selected_clips]
    if slot.selected is not None:
        return [slot.selected.asset_id]
    return []


def _clip_matches_any_asset(media_path: str, media_name: str, asset_ids: list[str]) -> bool:
    path_name = Path(media_path).name
    path_stem = Path(media_path).stem
    media_stem = Path(media_name).stem
    for asset_id in asset_ids:
        asset_name = Path(asset_id).name
        asset_stem = Path(asset_id).stem
        if asset_id in {media_path, media_name, path_name}:
            return True
        if asset_name in {media_name, path_name}:
            return True
        if asset_stem in {media_stem, path_stem}:
            return True
    return False
