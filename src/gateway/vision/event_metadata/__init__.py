"""Structured event metadata contracts for footage retrieval."""

from .ingest import ingest_gemini_json
from .normalize import normalize_event_metadata
from .search import EventSearchFilters, search_events
from .schema import BoundaryEvidence, EventMetadata, EventObservation

__all__ = [
    "BoundaryEvidence",
    "EventMetadata",
    "EventSearchFilters",
    "EventObservation",
    "ingest_gemini_json",
    "normalize_event_metadata",
    "search_events",
]
