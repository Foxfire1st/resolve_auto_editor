"""Pydantic contracts for editor-usable video event metadata."""

from __future__ import annotations

from pydantic import BaseModel, Field


class BoundaryEvidence(BaseModel):
    """Visible evidence used to justify event span boundaries."""

    start: str | None = None
    end: str | None = None
    anchor: str | None = None
    notes: str | None = None


class EventObservation(BaseModel):
    """A timestamped fact observed inside a parent visual action span."""

    time_s: float | None = None
    timecode: str | None = None
    start_s: float | None = None
    end_s: float | None = None
    start_timecode: str | None = None
    end_timecode: str | None = None
    kind: str | None = None
    text: str | None = None
    details: str | None = None
    labels: list[str] = Field(default_factory=list)
    confidence: float | None = None


class EventMetadata(BaseModel):
    """Normalized event span ready for metadata storage and later retrieval."""

    start_s: float
    end_s: float
    anchor_s: float
    actor: str | None = None
    actor_type: str | None = None
    action_type: str | None = None
    ability_name: str | None = None
    labels: list[str] = Field(default_factory=list)
    targets: list[str] = Field(default_factory=list)
    details: str = ""
    observations: list[EventObservation] = Field(default_factory=list)
    boundary_evidence: BoundaryEvidence = Field(default_factory=BoundaryEvidence)
    confidence: float | None = None
    needs_boundary_review: bool = False
    boundary_confidence: float = 1.0
