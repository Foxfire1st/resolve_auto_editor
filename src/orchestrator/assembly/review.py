"""Optional critique pass for metadata-first rough-cut proposals."""

from __future__ import annotations

import json
import os
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

from google import genai
from google.genai import types
from pydantic import BaseModel, Field

from src.orchestrator.assembly.models import AssemblyCandidate, AssemblyProposal, AssemblySlot


ReviewAction = Literal["keep", "swap", "flag"]


class ProposalReviewSuggestion(BaseModel):
    """One advisory review note for a proposal slot."""

    slot_id: str
    action: ReviewAction
    reason: str
    candidate_asset_id: str | None = None
    confidence: float = 1.0


class ProposalReviewReport(BaseModel):
    """Structured output from an optional proposal reviewer."""

    summary: str
    suggestions: list[ProposalReviewSuggestion] = Field(default_factory=list)


class AssemblyQualityReport(BaseModel):
    """Deterministic audit summary for editorial usefulness."""

    slot_count: int
    resolved_slot_count: int
    slots_per_minute: float = 0.0
    long_single_clip_slots: list[str] = Field(default_factory=list)
    manifest_selected: int = 0
    event_selected: int = 0
    repeated_asset_ids: list[str] = Field(default_factory=list)
    zero_start_selected: int = 0
    issues: list[str] = Field(default_factory=list)

    @property
    def low_editorial_usefulness(self) -> bool:
        return bool(self.issues)


Reviewer = Callable[[dict[str, Any]], ProposalReviewReport | dict[str, Any]]


def summarize_proposal_for_review(
    proposal: AssemblyProposal,
    *,
    max_alternatives: int = 3,
) -> dict[str, Any]:
    """Build a compact prompt payload from selected clips, alternatives, and warnings."""

    return {
        "project_name": proposal.project_name,
        "profile_name": proposal.profile_name,
        "global_notes": proposal.global_notes,
        "slots": [_summarize_slot(slot, max_alternatives=max_alternatives) for slot in proposal.slots],
    }


def build_review_prompt(proposal: AssemblyProposal) -> str:
    """Create a compact human/LLM prompt for proposal critique."""

    lines = [
        "Review this rough-cut proposal.",
        "Suggest swaps only when an alternative is clearly better.",
        "Flag weak or unresolved slots, but keep deterministic scoring as the default.",
        "",
    ]
    for slot in proposal.slots:
        selected = slot.selected.asset_id if slot.selected else None
        if slot.selected_clips:
            selected = ", ".join(candidate.asset_id for candidate in slot.selected_clips)
        alternatives = ", ".join(candidate.asset_id for candidate in slot.alternatives[:3]) or "none"
        lines.append(
            f"{slot.slot_id}: {slot.visual_requirement} | selected={selected or 'unresolved'} "
            f"| alternatives={alternatives}"
        )
        if slot.warnings:
            lines.append(f"warnings: {'; '.join(slot.warnings)}")
        if slot.unresolved_reason:
            lines.append(f"unresolved: {slot.unresolved_reason}")
    return "\n".join(lines)


def review_proposal(
    proposal: AssemblyProposal,
    *,
    reviewer: Reviewer | None = None,
) -> ProposalReviewReport:
    """Run an optional reviewer, falling back to deterministic weak-slot flags."""

    payload = summarize_proposal_for_review(proposal)
    if reviewer is not None:
        result = reviewer(payload)
        if isinstance(result, ProposalReviewReport):
            return result
        return ProposalReviewReport.model_validate(result)
    return _deterministic_review(proposal)


def gemini_review_proposal(
    proposal: AssemblyProposal,
    *,
    model_name: str | None = None,
) -> ProposalReviewReport:
    """Run the optional proposal review pass against the configured Gemini API."""

    api_key = (
        os.environ.get("gemini_api_key")
        or os.environ.get("GEMINI_API_KEY")
        or os.environ.get("GOOGLE_API_KEY")
    )
    if not api_key:
        raise RuntimeError("Gemini API key not found.")

    payload = summarize_proposal_for_review(proposal)
    prompt = (
        build_review_prompt(proposal)
        + "\n\nReturn strict JSON with this shape: "
        + '{"summary": string, "suggestions": ['
        + '{"slot_id": string, "action": "keep|swap|flag", '
        + '"reason": string, "candidate_asset_id": string|null, "confidence": number}'
        + "]}\n\nProposal payload:\n"
        + json.dumps(payload, ensure_ascii=False)
    )
    client = genai.Client(api_key=api_key)
    response = client.models.generate_content(
        model=model_name or os.environ.get("GEMINI_MODEL_EDITOR", "gemini-2.5-flash"),
        contents=[prompt],
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            temperature=0.0,
        ),
    )
    try:
        raw = json.loads(response.text or "{}")
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Gemini proposal review returned invalid JSON: {response.text}") from exc
    return ProposalReviewReport.model_validate(raw)


def audit_assembly_quality(proposal: AssemblyProposal) -> AssemblyQualityReport:
    selected_by_slot = {slot.slot_id: _selected_candidates(slot) for slot in proposal.slots}
    selected = [candidate for candidates in selected_by_slot.values() for candidate in candidates]
    resolved_slot_count = sum(1 for candidates in selected_by_slot.values() if candidates)
    total_duration = max((slot.timeline_start + slot.timeline_duration for slot in proposal.slots), default=0.0)
    slots_per_minute = (resolved_slot_count / (total_duration / 60.0)) if total_duration > 0 else 0.0
    long_single_clip_slots = [
        slot.slot_id
        for slot in proposal.slots
        if slot.timeline_duration > 20.0 and len(selected_by_slot[slot.slot_id]) <= 1
    ]
    manifest_selected = sum(1 for candidate in selected if candidate.provenance == ["manifest"])
    event_selected = sum(1 for candidate in selected if "event_metadata" in candidate.provenance)
    zero_start_selected = sum(
        1 for candidate in selected if abs(candidate.source_range.start) <= 0.001
    )
    repeated_asset_ids = sorted(
        asset_id
        for asset_id, count in Counter(candidate.asset_id for candidate in selected).items()
        if count > 1
    )
    issues: list[str] = []
    if long_single_clip_slots:
        issues.append(f"{len(long_single_clip_slots)} long slot(s) rely on a single clip.")
    if selected and manifest_selected == len(selected):
        issues.append("All selected clips come from manifest candidates.")
    if selected and zero_start_selected == len(selected):
        issues.append("All selected clips start at source time 0.0.")
    if repeated_asset_ids:
        issues.append("Repeated asset use reduces clip diversity.")
    return AssemblyQualityReport(
        slot_count=len(proposal.slots),
        resolved_slot_count=resolved_slot_count,
        slots_per_minute=round(slots_per_minute, 3),
        long_single_clip_slots=long_single_clip_slots,
        manifest_selected=manifest_selected,
        event_selected=event_selected,
        repeated_asset_ids=repeated_asset_ids,
        zero_start_selected=zero_start_selected,
        issues=issues,
    )

def write_quality_report(report: AssemblyQualityReport, path: Path) -> AssemblyQualityReport:
    """Write a quality audit report as JSON plus a Markdown companion."""

    path.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    lines = [
        "# Assembly Quality Report",
        "",
        f"- Slot count: {report.slot_count}",
        f"- Resolved slots: {report.resolved_slot_count}",
        f"- Slots per minute: {report.slots_per_minute}",
        f"- Manifest selections: {report.manifest_selected}",
        f"- Event selections: {report.event_selected}",
        f"- Zero-start selections: {report.zero_start_selected}",
        f"- Repeated assets: {', '.join(report.repeated_asset_ids) or 'none'}",
        f"- Long single-clip slots: {', '.join(report.long_single_clip_slots) or 'none'}",
        "",
        "## Issues",
        "",
    ]
    if report.issues:
        lines.extend(f"- {issue}" for issue in report.issues)
    else:
        lines.append("- None")
    path.with_suffix(".md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report


def _deterministic_review(proposal: AssemblyProposal) -> ProposalReviewReport:
    suggestions: list[ProposalReviewSuggestion] = []
    quality = audit_assembly_quality(proposal)
    systemic_manifest_only = (
        quality.resolved_slot_count > 1
        and quality.manifest_selected == quality.resolved_slot_count
    )
    systemic_zero_start = (
        quality.resolved_slot_count > 1
        and quality.zero_start_selected == quality.resolved_slot_count
    )
    for slot in proposal.slots:
        selected_candidates = _selected_candidates(slot)
        selected = slot.selected
        if slot.unresolved_reason or selected is None and not slot.selected_clips:
            suggestions.append(
                ProposalReviewSuggestion(
                    slot_id=slot.slot_id,
                    action="flag",
                    reason=slot.unresolved_reason or "No selected candidate.",
                )
            )
            continue
        if selected is not None and selected.score < 0.25:
            suggestions.append(
                ProposalReviewSuggestion(
                    slot_id=slot.slot_id,
                    action="flag",
                    reason=f"Low selected score: {selected.score:.2f}.",
                    candidate_asset_id=selected.asset_id,
                )
            )
        if slot.slot_id in quality.long_single_clip_slots:
            suggestions.append(
                ProposalReviewSuggestion(
                    slot_id=slot.slot_id,
                    action="flag",
                    reason="Long single-clip coverage reduces editorial usefulness.",
                    candidate_asset_id=selected.asset_id if selected else None,
                )
            )
        if systemic_manifest_only and selected_candidates and all(
            candidate.provenance == ["manifest"] for candidate in selected_candidates
        ):
            suggestions.append(
                ProposalReviewSuggestion(
                    slot_id=slot.slot_id,
                    action="flag",
                    reason="Manifest-only selection should be treated as low-confidence editorial coverage.",
                    candidate_asset_id=selected.asset_id if selected else None,
                )
            )
        if systemic_zero_start and selected_candidates and all(
            abs(candidate.source_range.start) <= 0.001 for candidate in selected_candidates
        ):
            suggestions.append(
                ProposalReviewSuggestion(
                    slot_id=slot.slot_id,
                    action="flag",
                    reason="Selected source ranges all start at 0.0, suggesting coarse manifest coverage.",
                    candidate_asset_id=selected.asset_id if selected else None,
                )
            )
        if selected is not None and selected.asset_id in quality.repeated_asset_ids:
            suggestions.append(
                ProposalReviewSuggestion(
                    slot_id=slot.slot_id,
                    action="flag",
                    reason="Asset is repeated across multiple slots.",
                    candidate_asset_id=selected.asset_id,
                )
            )
        for warning in slot.warnings:
            suggestions.append(
                ProposalReviewSuggestion(
                    slot_id=slot.slot_id,
                    action="flag",
                    reason=warning,
                    candidate_asset_id=selected.asset_id if selected else None,
                )
            )
    return ProposalReviewReport(
        summary=f"Deterministic review found {len(suggestions)} issue(s).",
        suggestions=suggestions,
    )


def _selected_candidates(slot: AssemblySlot) -> list[AssemblyCandidate]:
    if slot.selected_clips:
        return list(slot.selected_clips)
    if slot.selected is not None:
        return [slot.selected]
    return []


def _summarize_slot(slot: AssemblySlot, *, max_alternatives: int) -> dict[str, Any]:
    selected = [candidate.model_dump() for candidate in slot.selected_clips]
    if not selected and slot.selected is not None:
        selected = [slot.selected.model_dump()]
    return {
        "slot_id": slot.slot_id,
        "beat_id": slot.beat_id,
        "narration_ids": slot.narration_ids,
        "timeline_start": slot.timeline_start,
        "timeline_duration": slot.timeline_duration,
        "visual_requirement": slot.visual_requirement,
        "vo_excerpt": slot.vo_excerpt,
        "selected": selected,
        "alternatives": [candidate.model_dump() for candidate in slot.alternatives[:max_alternatives]],
        "warnings": slot.warnings,
        "unresolved_reason": slot.unresolved_reason,
    }
