"""Pydantic models for metadata-first rough-cut proposals."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from src.orchestrator.models import SourceRange


class AssemblyCandidate(BaseModel):
    """A metadata-backed candidate visual for one rough-cut slot."""

    asset_id: str
    source_path: str | None = None
    source_range: SourceRange
    event_ids: list[str] = Field(default_factory=list)
    score: float = 0.0
    reasons: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    provenance: list[str] = Field(default_factory=list)
    debug_summary: str | None = None
    debug_details: dict[str, Any] = Field(default_factory=dict)


class AssemblySlot(BaseModel):
    """A narration-timed slot that needs visual coverage."""

    slot_id: str
    narration_ids: list[str]
    timeline_start: float
    timeline_duration: float
    visual_requirement: str
    beat_id: str | None = None
    vo_excerpt: str = ""
    keywords: list[str] = Field(default_factory=list)
    preferred_assets: list[str] = Field(default_factory=list)
    min_clips: int = 1
    max_clips: int = 1
    selected: AssemblyCandidate | None = None
    selected_clips: list[AssemblyCandidate] = Field(default_factory=list)
    alternatives: list[AssemblyCandidate] = Field(default_factory=list)
    unresolved_reason: str | None = None
    warnings: list[str] = Field(default_factory=list)


class AssemblyProposal(BaseModel):
    """Inspectable rough-cut proposal created before SemanticEdit export."""

    project_name: str
    profile_name: str
    slots: list[AssemblySlot] = Field(default_factory=list)
    global_notes: list[str] = Field(default_factory=list)
