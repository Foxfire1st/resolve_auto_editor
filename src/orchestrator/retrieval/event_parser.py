"""Parsers for structured metadata embedded in retrieval captions."""

from __future__ import annotations

import re
from typing import List

from pydantic import BaseModel, Field


class ClipEvent(BaseModel):
    """Structured representation of a single event mentioned in a caption."""

    offset: float = Field(..., description="Seconds from the start of the clip window")
    summary: str = Field(..., description="Natural language description of the event")
    tags: List[str] = Field(default_factory=list, description="Optional tags associated with the event")
    raw_text: str = Field(..., description="Original text block for auditing")


_EVENT_BLOCK_PATTERN = re.compile(
    r"\[(?P<minutes>\d+):(?P<seconds>\d+(?:\.\d+)?)\]\s*(?P<body>.*?)(?=\n?\[(?:\d+):(?:\d+(?:\.\d+)?)\]|$)",
    re.DOTALL,
)

_TAG_SPLIT_PATTERN = re.compile(r"\s*,\s*")


def parse_events_from_caption(caption: str | None) -> List[ClipEvent]:
    """Extract timestamped events from the retrieval caption text."""

    if not caption:
        return []

    events: List[ClipEvent] = []
    for match in _EVENT_BLOCK_PATTERN.finditer(caption):
        minutes = int(match.group("minutes"))
        seconds = float(match.group("seconds"))
        body = match.group("body").strip()
        if not body:
            continue

        tags: List[str] = []
        summary_text = body
        tag_marker = " - Tags:"
        if tag_marker in body:
            summary_text, tag_text = body.split(tag_marker, 1)
            tag_text = tag_text.strip()
            if tag_text:
                tags = [tag.strip() for tag in _TAG_SPLIT_PATTERN.split(tag_text) if tag.strip()]
        summary = summary_text.strip(" -\n")
        offset = minutes * 60 + seconds
        events.append(
            ClipEvent(
                offset=offset,
                summary=summary or "(untitled event)",
                tags=tags,
                raw_text=body.strip(),
            )
        )

    return events
