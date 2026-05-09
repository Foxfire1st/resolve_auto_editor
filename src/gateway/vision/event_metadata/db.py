"""SQLite helpers for event metadata storage."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .config import get_settings


def connect(database_path: Path | str | None = None, *, profile_name: str | None = None) -> sqlite3.Connection:
    """Open a configured SQLite connection."""

    settings = get_settings(profile_name=profile_name, database_path=database_path)
    path = settings.database_path
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(path))
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


@contextmanager
def connection_scope(
    database_path: Path | str | None = None,
    *,
    profile_name: str | None = None,
) -> Iterator[sqlite3.Connection]:
    """Provide a transactional SQLite connection scope."""

    connection = connect(database_path, profile_name=profile_name)
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def init_db(connection: sqlite3.Connection) -> None:
    """Create event metadata tables and indexes if missing."""

    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS event_metadata_clips (
            profile TEXT NOT NULL,
            video_id TEXT NOT NULL,
            source_path TEXT,
            duration_s REAL,
            title TEXT,
            description TEXT,
            tags_json TEXT NOT NULL DEFAULT '[]',
            summary_json TEXT NOT NULL DEFAULT '{}',
            updated_at TEXT NOT NULL,
            PRIMARY KEY (profile, video_id)
        );

        CREATE TABLE IF NOT EXISTS event_metadata_events (
            id TEXT PRIMARY KEY,
            profile TEXT NOT NULL,
            video_id TEXT NOT NULL,
            source_path TEXT,
            start_s REAL NOT NULL,
            end_s REAL NOT NULL,
            anchor_s REAL NOT NULL,
            actor TEXT,
            actor_type TEXT,
            action_type TEXT,
            ability_name TEXT,
            details TEXT NOT NULL,
            labels_json TEXT NOT NULL DEFAULT '[]',
            targets_json TEXT NOT NULL DEFAULT '[]',
            observations_json TEXT NOT NULL DEFAULT '[]',
            boundary_evidence_json TEXT NOT NULL DEFAULT '{}',
            confidence REAL,
            needs_boundary_review INTEGER NOT NULL DEFAULT 0,
            boundary_confidence REAL NOT NULL DEFAULT 1.0,
            tags_json TEXT NOT NULL DEFAULT '[]',
            raw_json TEXT NOT NULL DEFAULT '{}',
            updated_at TEXT NOT NULL,
            FOREIGN KEY (profile, video_id)
                REFERENCES event_metadata_clips(profile, video_id)
                ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_event_metadata_events_profile_video
            ON event_metadata_events(profile, video_id);
        CREATE INDEX IF NOT EXISTS idx_event_metadata_events_profile_actor
            ON event_metadata_events(profile, actor);
        CREATE INDEX IF NOT EXISTS idx_event_metadata_events_profile_action
            ON event_metadata_events(profile, action_type);
        CREATE INDEX IF NOT EXISTS idx_event_metadata_events_review
            ON event_metadata_events(profile, needs_boundary_review);
        """
    )
    _ensure_fts(connection)


def _ensure_fts(connection: sqlite3.Connection) -> None:
    try:
        connection.execute(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS event_metadata_fts USING fts5(
                event_id UNINDEXED,
                profile UNINDEXED,
                video_id UNINDEXED,
                actor,
                action_type,
                ability_name,
                labels,
                tags,
                details,
                observations
            )
            """
        )
    except sqlite3.OperationalError:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS event_metadata_fts (
                event_id TEXT PRIMARY KEY,
                profile TEXT,
                video_id TEXT,
                actor TEXT,
                action_type TEXT,
                ability_name TEXT,
                labels TEXT,
                tags TEXT,
                details TEXT,
                observations TEXT
            )
            """
        )


def has_fts5(connection: sqlite3.Connection) -> bool:
    """Return whether the current metadata table supports FTS5 MATCH queries."""

    try:
        connection.execute("SELECT rowid FROM event_metadata_fts WHERE event_metadata_fts MATCH ?", ("__unlikely__",))
    except sqlite3.OperationalError:
        return False
    return True
