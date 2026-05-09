"""Utilities for constructing director notes alongside the semantic timeline."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable

from src.orchestrator import models


class DirectorNotesBuilder:
    """Accumulates clip-level metadata for QA before Resolve import."""

    def __init__(self, *, project_name: str, timeline_name: str) -> None:
        self.document = models.DirectorNotesDocument(
            project_name=project_name,
            timeline_name=timeline_name,
            entries=[],
        )

    def add_entry(
        self,
        *,
        beat_id: str,
        shot_id: str,
        narration_ids: Iterable[str],
        vo_excerpt: str,
        vo_range: models.TimeSpan,
        clip_spec: models.ClipSpec,
        visual_summary: str,
        coverage_note: str,
        reasoning: str,
    ) -> None:
        source_range = clip_spec.source_range
        if source_range is None:
            source_range = models.SourceRange(start=0.0, duration=0.0)
        entry = models.DirectorNoteEntry(
            beat_id=beat_id,
            shot_id=shot_id,
            narration_ids=list(narration_ids),
            vo_excerpt=vo_excerpt,
            vo_range=vo_range,
            clip_asset_id=clip_spec.asset_id,
            clip_range=source_range,
            visual_summary=visual_summary,
            coverage_note=coverage_note,
            reasoning=reasoning,
        )
        self.document.entries.append(entry)

    def add_unresolved_entry(
        self,
        *,
        beat_id: str,
        shot_id: str,
        narration_ids: Iterable[str],
        vo_excerpt: str,
        vo_range: models.TimeSpan,
        coverage_note: str,
        reasoning: str,
    ) -> None:
        entry = models.DirectorNoteEntry(
            beat_id=beat_id,
            shot_id=shot_id,
            narration_ids=list(narration_ids),
            vo_excerpt=vo_excerpt,
            vo_range=vo_range,
            clip_asset_id="__UNRESOLVED__",
            clip_range=models.SourceRange(start=0.0, duration=0.0),
            visual_summary="No acceptable clip selected",
            coverage_note=coverage_note,
            reasoning=reasoning,
        )
        self.document.entries.append(entry)

    def to_dict(self) -> dict:
        return self.document.model_dump()

    def write_json(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as handle:
            json.dump(self.to_dict(), handle, indent=2)

    def write_markdown(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = [f"# Director Notes — {self.document.timeline_name}", ""]
        for entry in self.document.entries:
            lines.append(f"## {entry.shot_id} ({entry.beat_id})")
            lines.append(f"Narration IDs: {', '.join(entry.narration_ids)}")
            vo_start = entry.vo_range.start
            vo_end = entry.vo_range.start + entry.vo_range.duration
            lines.append(f"VO ({vo_start:.2f}s – {vo_end:.2f}s): {entry.vo_excerpt}")
            lines.append(
                f"Clip: {entry.clip_asset_id} [{entry.clip_range.start:.2f}s – "
                f"{entry.clip_range.start + entry.clip_range.duration:.2f}s]"
            )
            lines.append(f"Visuals: {entry.visual_summary}")
            lines.append(f"Coverage: {entry.coverage_note}")
            lines.append(f"Reasoning: {entry.reasoning}")
            lines.append("")
        with path.open("w", encoding="utf-8") as handle:
            handle.write("\n".join(lines).strip() + "\n")
