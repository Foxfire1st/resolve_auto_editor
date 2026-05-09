"""Persistence helpers for Gemini attachment metadata."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

from src.orchestrator.clients import gemini_files

DEFAULT_STATE_FILENAME = "gemini_attachments.json"


def load_state(path: Path) -> Dict[str, Any]:
    if not path or not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def save_state(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)


def record_attachment(
    *,
    state_path: Path,
    attachment_name: str,
    handle: gemini_files.UploadedFileHandle,
    handle_path: Path,
) -> None:
    data = load_state(state_path)
    data[attachment_name] = {
        "handle_path": str(handle_path),
        "gemini_name": handle.gemini_name,
        "local_path": handle.local_path,
        "sha256": handle.sha256,
        "size_bytes": handle.size_bytes,
        "modified_time_ns": handle.modified_time_ns,
        "uploaded_at": handle.uploaded_at,
        "expire_time": handle.expire_time,
    }
    save_state(state_path, data)


def list_attachments(state_path: Path) -> Dict[str, Any]:
    return load_state(state_path)


def attachment_status(entry: Dict[str, Any]) -> str:
    handle_path = Path(entry.get("handle_path", ""))
    handle = gemini_files.load_handle(handle_path)
    if not handle:
        return "missing"
    file_path = Path(handle.local_path)
    if not gemini_files.handle_matches_file(handle, file_path):
        return "stale"
    return "valid"


def resolve_handle(entry: Dict[str, Any], client: Optional[gemini_files.GeminiFilesClient] = None):
    if client is None:
        client = gemini_files.GeminiFilesClient()
    return client.get(entry["gemini_name"])