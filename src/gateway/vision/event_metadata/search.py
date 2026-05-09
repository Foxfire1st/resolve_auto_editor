"""Deterministic SQLite search for event metadata."""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from . import db
from .config import get_settings
from .models import EventSearchResult

TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_+#']+")
STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "by",
    "can",
    "clear",
    "clearly",
    "clip",
    "clips",
    "could",
    "details",
    "display",
    "do",
    "doing",
    "for",
    "from",
    "has",
    "have",
    "in",
    "into",
    "is",
    "it",
    "its",
    "menu",
    "of",
    "on",
    "or",
    "possibly",
    "regardless",
    "screen",
    "show",
    "showing",
    "the",
    "then",
    "this",
    "to",
    "type",
    "using",
    "where",
    "with",
}


@dataclass(frozen=True)
class EventSearchFilters:
    """Exact filters applied around text search."""

    actor: str | None = None
    action_type: str | None = None
    ability_name: str | None = None
    video_ids: Sequence[str] | None = None
    required_tags: Sequence[str] = field(default_factory=tuple)


def search_events(
    query: str = "",
    *,
    profile_name: str | None = None,
    database_path: Path | str | None = None,
    filters: EventSearchFilters | None = None,
    include_review: bool = False,
    limit: int = 20,
    connection: sqlite3.Connection | None = None,
) -> list[EventSearchResult]:
    """Search stored event spans using exact fields plus SQLite FTS when available."""

    settings = get_settings(profile_name=profile_name, database_path=database_path)
    filters = filters or EventSearchFilters()
    own_connection = connection is None
    connection = connection or db.connect(settings.database_path)
    try:
        db.init_db(connection)
        if query.strip() and db.has_fts5(connection):
            rows = _search_fts(connection, query, settings.profile, filters, include_review, limit)
            if not rows:
                rows = _search_weighted_scan(connection, query, settings.profile, filters, include_review, limit)
        else:
            rows = _search_scan(connection, query, settings.profile, filters, include_review, limit)
        results = [_row_to_result(row) for row in rows]
        if filters.required_tags:
            required = {_normalize_tag(tag) for tag in filters.required_tags}
            required.discard(None)
            results = [result for result in results if required.issubset({_normalize_tag(tag) for tag in result.tags})]
        return results[:limit]
    finally:
        if own_connection:
            connection.close()


def _search_fts(
    connection: sqlite3.Connection,
    query: str,
    profile: str,
    filters: EventSearchFilters,
    include_review: bool,
    limit: int,
) -> list[sqlite3.Row]:
    where, params = _where_clause(profile, filters, include_review)
    fts_query = _fts_query(query)
    if not fts_query:
        return _search_scan(connection, query, profile, filters, include_review, limit)
    sql = f"""
        SELECT e.*, bm25(event_metadata_fts) AS bm25_score
        FROM event_metadata_fts
        JOIN event_metadata_events e ON e.id = event_metadata_fts.event_id
        WHERE event_metadata_fts MATCH ?
          AND {where}
        ORDER BY bm25_score ASC, e.boundary_confidence DESC, e.start_s ASC
        LIMIT ?
    """
    return list(connection.execute(sql, (fts_query, *params, max(limit * 5, limit))))


def _search_scan(
    connection: sqlite3.Connection,
    query: str,
    profile: str,
    filters: EventSearchFilters,
    include_review: bool,
    limit: int,
) -> list[sqlite3.Row]:
    where, params = _where_clause(profile, filters, include_review)
    query_text = query.strip().lower()
    if query_text:
        where = f"{where} AND (lower(coalesce(e.actor, '') || ' ' || coalesce(e.action_type, '') || ' ' || coalesce(e.ability_name, '') || ' ' || e.details || ' ' || e.labels_json || ' ' || e.tags_json || ' ' || e.observations_json) LIKE ?)"
        params.append(f"%{query_text}%")
    sql = f"""
        SELECT e.*, NULL AS bm25_score
        FROM event_metadata_events e
        WHERE {where}
        ORDER BY e.boundary_confidence DESC, e.start_s ASC
        LIMIT ?
    """
    return list(connection.execute(sql, (*params, max(limit * 5, limit))))


def _search_weighted_scan(
    connection: sqlite3.Connection,
    query: str,
    profile: str,
    filters: EventSearchFilters,
    include_review: bool,
    limit: int,
) -> list[sqlite3.Row]:
    """Fallback search for narrative prompts that are too broad for strict FTS AND matching."""

    tokens = _salient_tokens(query)
    if not tokens:
        return _search_scan(connection, query, profile, filters, include_review, limit)

    where, params = _where_clause(profile, filters, include_review)
    searchable_text = """
        lower(
            coalesce(e.video_id, '') || ' ' ||
            coalesce(e.actor, '') || ' ' ||
            coalesce(e.action_type, '') || ' ' ||
            coalesce(e.ability_name, '') || ' ' ||
            e.details || ' ' ||
            e.labels_json || ' ' ||
            e.tags_json || ' ' ||
            e.observations_json
        )
    """
    match_expr = " + ".join("CASE WHEN searchable_text LIKE ? THEN 1 ELSE 0 END" for _ in tokens)
    any_match = " OR ".join("searchable_text LIKE ?" for _ in tokens)
    token_count = float(len(tokens))
    sql = f"""
        WITH candidates AS (
            SELECT e.*, {searchable_text} AS searchable_text
            FROM event_metadata_events e
            WHERE {where}
        )
        SELECT
            candidates.*,
            (({token_count} - ({match_expr})) / {token_count}) AS bm25_score,
            ({match_expr}) AS match_count
        FROM candidates
        WHERE {any_match}
        ORDER BY match_count DESC, boundary_confidence DESC, start_s ASC
        LIMIT ?
    """
    like_params = [f"%{token}%" for token in tokens]
    return list(
        connection.execute(
            sql,
            (
                *params,
                *like_params,
                *like_params,
                *like_params,
                max(limit * 5, limit),
            ),
        )
    )


def _where_clause(profile: str, filters: EventSearchFilters, include_review: bool) -> tuple[str, list[Any]]:
    clauses = ["e.profile = ?"]
    params: list[Any] = [profile]
    if not include_review:
        clauses.append("e.needs_boundary_review = 0")
    if filters.actor:
        clauses.append("lower(e.actor) = lower(?)")
        params.append(filters.actor)
    if filters.action_type:
        clauses.append("lower(e.action_type) = lower(?)")
        params.append(filters.action_type)
    if filters.ability_name:
        clauses.append("lower(e.ability_name) = lower(?)")
        params.append(filters.ability_name)
    if filters.video_ids:
        placeholders = ", ".join("?" for _ in filters.video_ids)
        clauses.append(f"e.video_id IN ({placeholders})")
        params.extend(filters.video_ids)
    return " AND ".join(clauses), params


def _fts_query(query: str) -> str:
    tokens = TOKEN_PATTERN.findall(query)
    escaped = [token.replace('"', "") for token in tokens if token.strip()]
    return " ".join(f'"{token}"' for token in escaped)


def _salient_tokens(query: str) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for token in TOKEN_PATTERN.findall(query):
        normalized = token.strip(" '\"\t\r\n").lower()
        if normalized.endswith("'s"):
            normalized = normalized[:-2]
        if len(normalized) < 3 or normalized in STOPWORDS:
            continue
        if normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
    return result[:16]


def _row_to_result(row: sqlite3.Row) -> EventSearchResult:
    labels = _json_list(row["labels_json"])
    tags = _json_list(row["tags_json"])
    summary = _summary_text(row, labels)
    bm25_score = row["bm25_score"]
    if bm25_score is None:
        score = float(row["boundary_confidence"])
    else:
        score = float(row["boundary_confidence"]) + (1.0 / (1.0 + abs(float(bm25_score))))
    return EventSearchResult(
        id=row["id"],
        video_id=row["video_id"],
        start_s=float(row["start_s"]),
        end_s=float(row["end_s"]),
        anchor_s=float(row["anchor_s"]),
        summary=summary,
        score=round(score, 6),
        source_path=row["source_path"],
        actor=row["actor"],
        action_type=row["action_type"],
        ability_name=row["ability_name"],
        tags=tags,
        labels=labels,
        needs_boundary_review=bool(row["needs_boundary_review"]),
        boundary_confidence=float(row["boundary_confidence"]),
    )


def _summary_text(row: sqlite3.Row, labels: list[str]) -> str:
    parts = []
    if row["actor"]:
        parts.append(str(row["actor"]))
    if row["action_type"]:
        parts.append(str(row["action_type"]))
    if row["ability_name"]:
        parts.append(str(row["ability_name"]))
    if row["details"]:
        parts.append(str(row["details"]))
    if labels:
        parts.append("Labels: " + ", ".join(labels))
    return " - ".join(parts) or "(untitled event)"


def _json_list(value: str | None) -> list[str]:
    if not value:
        return []
    try:
        loaded = json.loads(value)
    except json.JSONDecodeError:
        return []
    if not isinstance(loaded, list):
        return []
    return [str(item) for item in loaded if str(item).strip()]


def _normalize_tag(value: str | None) -> str | None:
    if not value:
        return None
    text = value.strip().lower()
    return text if text.startswith("#") else f"#{text}"
