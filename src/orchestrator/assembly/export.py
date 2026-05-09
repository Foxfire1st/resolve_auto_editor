"""Export rough-cut assembly proposals to the existing semantic edit schema."""

from __future__ import annotations

from pathlib import Path

from src.orchestrator import models
from src.orchestrator.assembly.models import AssemblyCandidate, AssemblyProposal, AssemblySlot
from src.orchestrator.director_notes import DirectorNotesBuilder
from src.orchestrator.models import SourceRange


def _selected_candidates(slot: AssemblySlot) -> list[AssemblyCandidate]:
    if slot.selected_clips:
        return slot.selected_clips
    if slot.selected is not None:
        return [slot.selected]
    return []


def _candidate_to_clip(
    candidate: AssemblyCandidate,
    *,
    duration: float | None = None,
) -> models.ClipSpec:
    source_range = candidate.source_range
    if duration is not None:
        source_range = SourceRange(start=source_range.start, duration=duration)
    return models.ClipSpec(
        asset_id=candidate.asset_id,
        source_range=source_range,
        description=candidate.debug_summary or "; ".join(candidate.reasons),
    )


def _candidate_reasoning(candidate: AssemblyCandidate) -> str:
    parts: list[str] = []
    if candidate.debug_summary:
        parts.append(candidate.debug_summary)
    if candidate.reasons:
        parts.append("; ".join(candidate.reasons))
    return " | ".join(parts)


def _slot_video_clips(slot: AssemblySlot, selected: list[AssemblyCandidate]) -> list[models.ClipSpec]:
    if not selected:
        return []
    clips: list[models.ClipSpec] = []
    remaining = max(slot.timeline_duration, 0.1)
    for candidate in selected:
        if remaining <= 0.001:
            break
        duration = min(max(candidate.source_range.duration, 0.1), remaining)
        clips.append(_candidate_to_clip(candidate, duration=duration))
        remaining -= duration

    filler = selected[-1]
    filler_duration = max(filler.source_range.duration, 0.1)
    while remaining > 0.001:
        duration = min(filler_duration, remaining)
        clips.append(_candidate_to_clip(filler, duration=duration))
        remaining -= duration
    return clips


def proposal_to_director_notes(
    proposal: AssemblyProposal,
    *,
    timeline_name: str | None = None,
) -> models.DirectorNotesDocument:
    """Build human-review notes for every proposal slot, including unresolved ones."""

    notes = DirectorNotesBuilder(
        project_name=proposal.project_name,
        timeline_name=timeline_name or proposal.project_name,
    )
    for slot in proposal.slots:
        vo_range = models.TimeSpan(
            start=slot.timeline_start,
            duration=slot.timeline_duration,
        )
        beat_id = slot.beat_id or slot.slot_id
        selected = _selected_candidates(slot)
        if not selected:
            notes.add_unresolved_entry(
                beat_id=beat_id,
                shot_id=slot.slot_id,
                narration_ids=slot.narration_ids,
                vo_excerpt=slot.vo_excerpt,
                vo_range=vo_range,
                coverage_note=slot.visual_requirement,
                reasoning=slot.unresolved_reason or "No selected assembly candidate.",
            )
            continue

        for index, candidate in enumerate(selected, start=1):
            clip_spec = _candidate_to_clip(candidate)
            note_suffix = "" if len(selected) == 1 else f" [{index}/{len(selected)}]"
            notes.add_entry(
                beat_id=beat_id,
                shot_id=slot.slot_id,
                narration_ids=slot.narration_ids,
                vo_excerpt=slot.vo_excerpt,
                vo_range=vo_range,
                clip_spec=clip_spec,
                visual_summary=(candidate.source_path or candidate.asset_id) + note_suffix,
                coverage_note=slot.visual_requirement,
                reasoning=_candidate_reasoning(candidate),
            )
    return notes.document


def write_proposal_director_notes(
    proposal: AssemblyProposal,
    path: Path,
    *,
    timeline_name: str | None = None,
) -> models.DirectorNotesDocument:
    """Write proposal director notes in the existing JSON and Markdown formats."""

    notes = DirectorNotesBuilder(
        project_name=proposal.project_name,
        timeline_name=timeline_name or proposal.project_name,
    )
    document = proposal_to_director_notes(proposal, timeline_name=timeline_name)
    notes.document = document
    notes.write_json(path)
    notes.write_markdown(path.with_suffix(".md"))
    return document


def proposal_to_semantic_edit(
    proposal: AssemblyProposal,
    *,
    vo_audio_path: Path | None = None,
    fps: float = 30.0,
    director_notes_path: Path | None = None,
    timeline_name: str | None = None,
) -> models.SemanticEdit:
    """Convert selected proposal slots into the existing timeline export DTO."""

    if director_notes_path is not None:
        write_proposal_director_notes(
            proposal,
            director_notes_path,
            timeline_name=timeline_name,
        )

    segments: list[models.EditSegment] = []
    for slot in proposal.slots:
        selected = _selected_candidates(slot)
        if not selected:
            continue
        narration_span = models.TimeSpan(
            start=slot.timeline_start,
            duration=slot.timeline_duration,
        )
        narration_audio = None
        if vo_audio_path is not None:
            narration_audio = models.AudioClipSpec(
                asset_id=str(vo_audio_path.resolve()),
                source_range=models.SourceRange(
                    start=slot.timeline_start,
                    duration=slot.timeline_duration,
                ),
            )
        clips = _slot_video_clips(slot, selected)
        segments.append(
            models.EditSegment(
                id=slot.slot_id,
                shot_id=slot.slot_id,
                narration_span=narration_span,
                narration_audio=narration_audio,
                primary_clip=clips[0],
                video_clips=clips,
            )
        )
    return models.SemanticEdit(
        project_name=proposal.project_name,
        fps=fps,
        segments=segments,
    )
