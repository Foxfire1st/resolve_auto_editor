"""Dataclasses used by the event metadata storage and search layer."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .schema import BoundaryEvidence, EventObservation


@dataclass(frozen=True)
class ClipRecord:
    """Stored clip-level metadata."""

    profile: str
    video_id: str
    source_path: str | None
    duration_s: float | None = None
    title: str | None = None
    description: str | None = None
    tags: list[str] = field(default_factory=list)
    updated_at: datetime | None = None


@dataclass(frozen=True)
class StoredEvent:
    """Event row persisted in the metadata database."""

    id: str
    profile: str
    video_id: str
    source_path: str | None
    start_s: float
    end_s: float
    anchor_s: float
    actor: str | None
    actor_type: str | None
    action_type: str | None
    ability_name: str | None
    details: str
    labels: list[str]
    targets: list[str]
    observations: list[EventObservation]
    boundary_evidence: BoundaryEvidence
    confidence: float | None
    needs_boundary_review: bool
    boundary_confidence: float
    tags: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class EventSearchResult:
    """Search result returned by deterministic event metadata lookup."""

    id: str
    video_id: str
    start_s: float
    end_s: float
    anchor_s: float
    summary: str
    score: float
    source_path: str | None = None
    actor: str | None = None
    action_type: str | None = None
    ability_name: str | None = None
    tags: list[str] = field(default_factory=list)
    labels: list[str] = field(default_factory=list)
    needs_boundary_review: bool = False
    boundary_confidence: float = 1.0

    @property
    def event_offset(self) -> float:
        """Return the anchor offset inside this event span."""

        return max(self.anchor_s - self.start_s, 0.0)
