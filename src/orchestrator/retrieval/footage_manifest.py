"""Utilities for building Gemini-ready footage manifests."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, Iterable, List, Sequence

from src.orchestrator import beat_manager

LOGGER = logging.getLogger(__name__)


def _normalize_roots(roots: Iterable[Path | str]) -> List[Path]:
    normalized: List[Path] = []
    for entry in roots:
        path = Path(entry)
        try:
            normalized.append(path.resolve())
        except OSError:
            normalized.append(path)
    return normalized


def gather_entries(
    footage_roots: Sequence[Path | str],
) -> List[Dict[str, Any]]:
    """Return normalized manifest entries harvested from ingest summaries."""

    if not footage_roots:
        return []

    summaries = beat_manager.collect_footage_summaries(
        _normalize_roots(footage_roots),
    )
    entries: List[Dict[str, Any]] = []
    for item in summaries:
        entry = {
            "asset_id": item.get("asset_id"),
            "summary": item.get("summary"),
            "tags": item.get("tags", []),
            "mode": item.get("mode"),
            "stage_name": item.get("stage_name"),
            "duration": item.get("duration"),
            "source_path": item.get("source_path"),
            "meta_path": item.get("meta_path"),
        }
        if entry["asset_id"] and entry["summary"]:
            entries.append(entry)
    return entries


def write_manifest(entries: Sequence[Dict[str, Any]], output_path: Path) -> None:
    """Write manifest entries to disk as newline-delimited JSON."""

    if not entries:
        raise ValueError("No manifest entries supplied")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as handle:
        for entry in entries:
            handle.write(json.dumps(entry, ensure_ascii=True))
            handle.write("\n")
    LOGGER.info("Wrote %s entries to %s", len(entries), output_path)


def build_manifest(
    *,
    footage_roots: Sequence[Path | str],
    output_path: Path,
) -> Path:
    """Gather entries and write them to ``output_path``."""

    entries = gather_entries(footage_roots)
    if not entries:
        raise RuntimeError("No footage summaries discovered for manifest build")
    write_manifest(entries, output_path)
    return output_path


def read_manifest(path: Path) -> List[Dict[str, Any]]:
    """Load a manifest file from disk."""

    if not path.exists():
        raise FileNotFoundError(f"Manifest file not found: {path}")

    entries: List[Dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid manifest line in {path}: {line}") from exc
    return entries


def format_manifest_preview(entries: Sequence[Dict[str, Any]], *, limit: int = 5) -> str:
    if not entries:
        return "(manifest empty)"

    preview: List[str] = []
    for entry in entries[:limit]:
        asset = entry.get("asset_id") or entry.get("source_path") or "(unknown asset)"
        summary = (entry.get("summary") or "").strip()
        if len(summary) > 160:
            summary = summary[:160].rstrip() + "…"
        tags = entry.get("tags") or []
        tag_block = ", ".join(str(tag) for tag in tags[:5]) if tags else "no tags"
        preview.append(f"- {asset}: {summary} (tags: {tag_block})")

    remainder = len(entries) - min(len(entries), limit)
    if remainder > 0:
        preview.append(f"- …and {remainder} more entries in manifest")

    return "\n".join(preview)
