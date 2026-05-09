"""Configuration helpers for event metadata storage."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

PROFILE_ENV = "EVENT_METADATA_PROFILE"
DATABASE_PATH_ENV = "EVENT_METADATA_DB_PATH"

BASE_DIR = Path(__file__).resolve().parent
try:
    REPO_ROOT = BASE_DIR.parents[3]
except IndexError:
    REPO_ROOT = BASE_DIR
DEFAULT_PROFILE_FILE = REPO_ROOT / "profiles" / "default_profile.txt"


@dataclass(frozen=True)
class EventMetadataSettings:
    """Runtime settings for event metadata operations."""

    profile: str
    database_path: Path


def get_settings(profile_name: str | None = None, database_path: Path | str | None = None) -> EventMetadataSettings:
    """Return event metadata settings for a profile."""

    profile = profile_name or os.getenv(PROFILE_ENV) or os.getenv("VIDEO_EMBED_PROFILE") or _default_profile()
    configured_path = database_path or os.getenv(DATABASE_PATH_ENV)
    if configured_path:
        path = Path(configured_path).expanduser()
        if not path.is_absolute():
            path = (REPO_ROOT / path).resolve()
    else:
        path = REPO_ROOT / "profiles" / profile / "data" / "event_metadata.db"
    return EventMetadataSettings(profile=profile, database_path=path)


def _default_profile() -> str:
    if DEFAULT_PROFILE_FILE.exists():
        profile = DEFAULT_PROFILE_FILE.read_text(encoding="utf-8").strip()
        if profile:
            return profile
    raise RuntimeError(
        "No event metadata profile configured. Pass --profile, set "
        f"{PROFILE_ENV}, or create profiles/default_profile.txt."
    )
