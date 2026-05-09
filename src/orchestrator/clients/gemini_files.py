"""Wrapper utilities around the Gemini Files API."""

from __future__ import annotations

import hashlib
import json
import os
import mimetypes
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from google import genai
from google.genai import types


def _resolve_api_key() -> str:
    api_key = (
        os.environ.get("gemini_api_key")
        or os.environ.get("GEMINI_API_KEY")
        or os.environ.get("GOOGLE_API_KEY")
    )
    if not api_key:
        raise RuntimeError("Gemini API key not configured")
    return api_key


def _parse_datetime(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _format_datetime(value: Optional[datetime]) -> Optional[str]:
    if not value:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def _sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


@dataclass
class UploadedFileHandle:
    """Persistent metadata describing an uploaded Gemini File."""

    local_path: str
    gemini_name: str
    size_bytes: int
    modified_time_ns: int
    uploaded_at: str
    gemini_uri: Optional[str] = None
    sha256: Optional[str] = None
    expire_time: Optional[str] = None

    @property
    def expires_at(self) -> Optional[datetime]:
        return _parse_datetime(self.expire_time)

    def is_expired(self) -> bool:
        expires = self.expires_at
        if not expires:
            return False
        return expires <= datetime.now(timezone.utc)


class GeminiFilesClient:
    """Minimal helper to upload files and persist handles."""

    def __init__(self, api_key: Optional[str] = None) -> None:
        self.client = genai.Client(api_key=api_key or _resolve_api_key())

    def upload(self, file_path: Path, *, mime_type: Optional[str] = None) -> UploadedFileHandle:
        if not file_path.exists():
            raise FileNotFoundError(file_path)

        guessed_mime = mimetypes.guess_type(file_path.name)[0]
        if not guessed_mime and file_path.suffix.lower() == ".ndjson":
            guessed_mime = "text/plain"
        detected_mime = mime_type or guessed_mime or "text/plain"
        file_obj = self.client.files.upload(
            file=file_path,
            config=types.UploadFileConfig(mime_type=detected_mime),
        )
        stat = file_path.stat()
        expire_time = getattr(file_obj, "expire_time", None)
        uri = getattr(file_obj, "uri", None)
        uploaded_at = datetime.now(timezone.utc)
        digest = _sha256(file_path)

        return UploadedFileHandle(
            local_path=str(file_path),
            gemini_name=file_obj.name,
            gemini_uri=uri,
            size_bytes=stat.st_size,
            modified_time_ns=stat.st_mtime_ns,
            sha256=digest,
            uploaded_at=_format_datetime(uploaded_at) or uploaded_at.isoformat(),
            expire_time=expire_time,
        )

    def get(self, name: str):
        return self.client.files.get(name=name)


def save_handle(handle: UploadedFileHandle, storage_path: Path) -> None:
    storage_path.parent.mkdir(parents=True, exist_ok=True)
    with open(storage_path, "w", encoding="utf-8") as fh:
        json.dump(asdict(handle), fh, indent=2)


def load_handle(storage_path: Path) -> Optional[UploadedFileHandle]:
    if not storage_path or not storage_path.exists():
        return None
    with open(storage_path, "r", encoding="utf-8") as fh:
        payload: dict[str, Any] = json.load(fh)
    try:
        return UploadedFileHandle(**payload)
    except TypeError:
        return None


def handle_matches_file(handle: UploadedFileHandle, file_path: Path) -> bool:
    if not file_path.exists():
        return False
    if Path(handle.local_path).resolve() != file_path.resolve():
        return False
    stat = file_path.stat()
    if handle.size_bytes != stat.st_size:
        return False
    if handle.modified_time_ns != stat.st_mtime_ns:
        return False
    if handle.sha256:
        current_digest = _sha256(file_path)
        if handle.sha256 != current_digest:
            return False
    return not handle.is_expired()
