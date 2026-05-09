"""Retrieval tools for the Orchestrator."""

import json
import re
from pathlib import Path
from typing import Any, List, Optional

from pydantic import BaseModel, Field

from gateway.vision.event_metadata import db
from gateway.vision.event_metadata.config import get_settings
from gateway.vision.event_metadata.models import EventSearchResult
from gateway.vision.event_metadata.search import EventSearchFilters, search_events
from src.orchestrator.retrieval import footage_manifest
from src.orchestrator.retrieval.event_parser import ClipEvent


class SearchResult(BaseModel):
    asset_id: str
    start: float
    end: float
    description: str
    score: float
    source_path: Optional[str] = None
    tags: List[str] = Field(default_factory=list)
    is_window: bool = False
    segment_ids: List[str] = Field(default_factory=list)
    segment_count: int = 1
    events: List[ClipEvent] = Field(default_factory=list)


def find_clips(
    query: str,
    limit: int = 5,
    constraints: Optional[dict[str, Any]] = None,
) -> List[SearchResult]:
    """Search for footage via deterministic event metadata."""

    constraints = constraints or {}
    search_mode = _normalize_search_mode(constraints.get("search_mode", "auto"))
    profile_name = constraints.get("profile") or constraints.get("profile_name")
    filters = EventSearchFilters(
        actor=constraints.get("actor"),
        action_type=constraints.get("action_type"),
        ability_name=constraints.get("ability_name"),
        video_ids=constraints.get("video_ids"),
        required_tags=constraints.get("required_tags") or (),
    )
    search_kwargs = {
        "profile_name": profile_name,
        "database_path": constraints.get("database_path"),
        "filters": filters,
        "include_review": bool(constraints.get("include_review", False)),
    }

    explicit_terms = [
        str(term).strip()
        for term in constraints.get("explicit_terms", ())
        if str(term).strip()
    ]
    if search_mode in {"auto", "explicit"}:
        explicit_results = _find_explicit_candidates(
            explicit_terms,
            manifest_path=constraints.get("manifest_path"),
            profile_name=profile_name,
            database_path=constraints.get("database_path"),
            limit=max(limit, 5),
        )
        if explicit_results or search_mode == "explicit":
            return _dedupe_results(explicit_results)[:limit]

    component_queries = _decompose_query(query)
    per_query_limit = max(limit, 5) if len(component_queries) == 1 else max(3, min(limit, 6))
    hit_groups: list[list[EventSearchResult]] = []
    for component_query in component_queries:
        hits = search_events(component_query, limit=per_query_limit, **search_kwargs)
        if hits:
            hit_groups.append(hits)

    merged_hits: list[EventSearchResult] = []
    seen_ids: set[str] = set()
    max_group_length = max((len(group) for group in hit_groups), default=0)
    for index in range(max_group_length):
        for group in hit_groups:
            if index >= len(group):
                continue
            hit = group[index]
            if hit.id in seen_ids:
                continue
            seen_ids.add(hit.id)
            merged_hits.append(hit)
            if len(merged_hits) >= limit:
                break
        if len(merged_hits) >= limit:
            break

    return _dedupe_results([_search_result_from_event(hit) for hit in merged_hits])[:limit]


def _find_explicit_candidates(
    terms: list[str],
    *,
    manifest_path: Any,
    profile_name: str | None,
    database_path: Any,
    limit: int,
) -> list[SearchResult]:
    """Search exact/named terms with review rows enabled and static spans expanded."""

    if not terms:
        return []

    results = _find_explicit_manifest_candidates(terms, manifest_path, limit=limit)
    if len(results) >= limit:
        return results
    settings = get_settings(profile_name=profile_name, database_path=database_path)
    connection = db.connect(settings.database_path, profile_name=settings.profile)
    try:
        db.init_db(connection)
        seen_ids: set[str] = set()
        for term in terms:
            hits = _search_explicit_metadata(connection, settings.profile, term, limit=max(limit, 5))
            durations = _clip_durations(connection, settings.profile, [hit.video_id for hit in hits])
            for hit in hits:
                if hit.id in seen_ids:
                    continue
                seen_ids.add(hit.id)
                end_override = None
                if hit.end_s <= hit.start_s:
                    duration = durations.get(hit.video_id)
                    if duration and duration > hit.start_s:
                        end_override = duration
                results.append(
                    _search_result_from_event(
                        hit,
                        end_override=end_override,
                        score_bonus=2.0 if _term_is_strong_match(term, hit) else 1.0,
                        provenance_tag="#explicit-review" if hit.needs_boundary_review else "#explicit",
                    )
                )
                if len(results) >= limit:
                    return results
        return results
    finally:
        connection.close()


def _find_explicit_manifest_candidates(
    terms: list[str],
    manifest_path: Any,
    *,
    limit: int,
) -> list[SearchResult]:
    if not manifest_path:
        return []
    try:
        entries = footage_manifest.read_manifest(Path(manifest_path))
    except (OSError, ValueError):
        return []

    results: list[SearchResult] = []
    for term in terms:
        normalized = term.lower().strip()
        if not normalized:
            continue
        for entry in entries:
            haystack = _manifest_entry_text(entry)
            if normalized not in haystack.lower():
                continue
            asset_id = str(entry.get("asset_id") or Path(str(entry.get("source_path", ""))).stem)
            if not asset_id:
                continue
            duration = _optional_float(entry.get("duration")) or 0.0
            summary = str(entry.get("summary") or "").strip() or f"Manifest match for {term}"
            tags = _unique([*(str(tag) for tag in entry.get("tags") or []), "#explicit-manifest"])
            term_id = re.sub(r"[^a-z0-9]+", "-", normalized).strip("-") or "term"
            segment_id = f"manifest:{asset_id}:{term_id}"
            results.append(
                SearchResult(
                    asset_id=asset_id,
                    start=0.0,
                    end=duration,
                    description=summary,
                    score=3.0,
                    source_path=str(entry.get("source_path") or "") or None,
                    tags=tags,
                    segment_ids=[segment_id],
                    events=[
                        ClipEvent(
                            offset=0.0,
                            summary=summary,
                            tags=tags,
                            raw_text=segment_id,
                        )
                    ],
                )
            )
            if len(results) >= limit:
                return results
    return results


def _search_explicit_metadata(
    connection: Any,
    profile: str,
    term: str,
    *,
    limit: int,
) -> list[EventSearchResult]:
    """Scan stored full metadata fields for exact named terms."""

    normalized = term.strip().lower()
    if not normalized:
        return []
    searchable_text = """
        lower(
            coalesce(e.video_id, '') || ' ' ||
            coalesce(e.actor, '') || ' ' ||
            coalesce(e.action_type, '') || ' ' ||
            coalesce(e.ability_name, '') || ' ' ||
            e.details || ' ' ||
            e.labels_json || ' ' ||
            e.tags_json || ' ' ||
            e.observations_json || ' ' ||
            e.raw_json || ' ' ||
            coalesce(c.title, '') || ' ' ||
            coalesce(c.description, '') || ' ' ||
            coalesce(c.tags_json, '') || ' ' ||
            coalesce(c.summary_json, '')
        )
    """
    rows = connection.execute(
        f"""
        SELECT
            e.*,
            c.title AS clip_title,
            c.description AS clip_description,
            c.tags_json AS clip_tags_json,
            c.summary_json AS clip_summary_json
        FROM event_metadata_events e
        LEFT JOIN event_metadata_clips c
            ON c.profile = e.profile AND c.video_id = e.video_id
        WHERE e.profile = ?
          AND {searchable_text} LIKE ?
        ORDER BY
            CASE
                WHEN lower(e.video_id) = ? THEN 0
                WHEN lower(e.video_id) LIKE ? THEN 1
                WHEN lower(coalesce(c.title, '')) LIKE ? THEN 2
                WHEN lower(coalesce(c.description, '')) LIKE ? THEN 3
                ELSE 4
            END,
            e.boundary_confidence DESC,
            e.start_s ASC
        LIMIT ?
        """,
        (
            profile,
            f"%{normalized}%",
            normalized,
            f"%{normalized}%",
            f"%{normalized}%",
            f"%{normalized}%",
            max(limit * 2, limit),
        ),
    )
    return [_event_search_result_from_row(row, exact_term=term) for row in rows]


def _event_search_result_from_row(row: Any, *, exact_term: str | None = None) -> EventSearchResult:
    labels = _json_list(row["labels_json"])
    tags = _json_list(row["tags_json"])
    summary = _summary_text(row, labels)
    if exact_term:
        snippet = _metadata_snippet(row, exact_term)
        if snippet and snippet.lower() not in summary.lower():
            summary = f"{summary} Metadata: {snippet}"
    return EventSearchResult(
        id=row["id"],
        video_id=row["video_id"],
        start_s=float(row["start_s"]),
        end_s=float(row["end_s"]),
        anchor_s=float(row["anchor_s"]),
        summary=summary,
        score=round(float(row["boundary_confidence"]), 6),
        source_path=row["source_path"],
        actor=row["actor"],
        action_type=row["action_type"],
        ability_name=row["ability_name"],
        tags=tags,
        labels=labels,
        needs_boundary_review=bool(row["needs_boundary_review"]),
        boundary_confidence=float(row["boundary_confidence"]),
    )


def _search_result_from_event(
    hit: EventSearchResult,
    *,
    end_override: float | None = None,
    score_bonus: float = 0.0,
    provenance_tag: str | None = None,
) -> SearchResult:
    tags = _unique([*hit.tags, *hit.labels])
    if provenance_tag:
        tags = _unique([*tags, provenance_tag])
    end = end_override if end_override is not None else hit.end_s
    return SearchResult(
        asset_id=hit.video_id,
        start=hit.start_s,
        end=end,
        description=hit.summary,
        score=hit.score + score_bonus,
        source_path=hit.source_path,
        tags=tags,
        is_window=False,
        segment_ids=[hit.id],
        segment_count=1,
        events=[
            ClipEvent(
                offset=hit.event_offset,
                summary=hit.summary,
                tags=tags,
                raw_text=hit.id,
            )
        ],
    )


def _clip_durations(connection: Any, profile: str, video_ids: list[str]) -> dict[str, float]:
    if not video_ids:
        return {}
    unique_ids = _unique(video_ids)
    placeholders = ", ".join("?" for _ in unique_ids)
    rows = connection.execute(
        f"""
        SELECT video_id, duration_s
        FROM event_metadata_clips
        WHERE profile = ? AND video_id IN ({placeholders})
        """,
        (profile, *unique_ids),
    )
    durations: dict[str, float] = {}
    for row in rows:
        value = row["duration_s"]
        if value is not None:
            durations[row["video_id"]] = float(value)
    return durations


def _term_is_strong_match(term: str, hit: EventSearchResult) -> bool:
    haystack = " ".join([hit.video_id, hit.summary, " ".join(hit.tags), " ".join(hit.labels)]).lower()
    normalized = term.lower().strip()
    return normalized in haystack or all(token in haystack for token in re.findall(r"[a-z0-9']+", normalized))


def _summary_text(row: Any, labels: list[str]) -> str:
    parts = []
    if row["actor"]:
        parts.append(str(row["actor"]))
    if row["action_type"]:
        parts.append(str(row["action_type"]))
    if row["ability_name"]:
        parts.append(str(row["ability_name"]))
    if row["details"]:
        parts.append(str(row["details"]))
    clip_title = _row_value(row, "clip_title")
    clip_description = _row_value(row, "clip_description")
    if clip_title:
        parts.append(f"Clip: {clip_title}")
    if clip_description:
        parts.append(str(clip_description))
    if labels:
        parts.append("Labels: " + ", ".join(labels))
    return " - ".join(parts) or "(untitled event)"


def _metadata_snippet(row: Any, term: str) -> str:
    haystack = " ".join(
        str(value)
        for value in (
            _row_value(row, "clip_title"),
            _row_value(row, "clip_description"),
            _row_value(row, "clip_tags_json"),
            _row_value(row, "clip_summary_json"),
            _row_value(row, "raw_json"),
        )
        if value
    )
    normalized = term.lower().strip()
    index = haystack.lower().find(normalized)
    if index < 0:
        return ""
    start = max(index - 80, 0)
    end = min(index + len(term) + 160, len(haystack))
    prefix = "..." if start else ""
    suffix = "..." if end < len(haystack) else ""
    return f"{prefix}{haystack[start:end].strip()}{suffix}"


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


def _row_value(row: Any, key: str) -> Any:
    return row[key] if key in row.keys() else None


def _manifest_entry_text(entry: dict[str, Any]) -> str:
    return " ".join(
        str(value)
        for value in (
            entry.get("asset_id"),
            entry.get("summary"),
            " ".join(str(tag) for tag in entry.get("tags") or []),
            entry.get("mode"),
            entry.get("stage_name"),
            entry.get("source_path"),
            entry.get("meta_path"),
        )
        if value
    )


def _optional_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _result_signature(result: SearchResult) -> tuple[str, float, float, str]:
    segment_id = result.segment_ids[0] if result.segment_ids else ""
    return (result.asset_id, round(result.start, 3), round(result.end, 3), segment_id)


def _dedupe_results(results: list[SearchResult]) -> list[SearchResult]:
    seen_signatures: set[tuple[str, float, float, str]] = set()
    deduped: list[SearchResult] = []
    for result in results:
        signature = _result_signature(result)
        if signature in seen_signatures:
            continue
        seen_signatures.add(signature)
        deduped.append(result)
    return deduped


def _normalize_search_mode(value: Any) -> str:
    normalized = str(value or "auto").strip().lower()
    aliases = {
        "explicit": "explicit",
        "exact": "explicit",
        "structured": "explicit",
        "explicit-only": "explicit",
        "fuzzy": "fuzzy",
        "broad": "fuzzy",
        "fallback": "fuzzy",
        "auto": "auto",
    }
    if normalized not in aliases:
        raise ValueError(f"Unsupported retrieval search_mode: {value!r}")
    return aliases[normalized]


def _unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        normalized = value.strip()
        if not normalized or normalized.lower() in seen:
            continue
        seen.add(normalized.lower())
        result.append(normalized)
    return result


def _decompose_query(query: str) -> list[str]:
    """Split multi-intent shot prompts into searchable components."""

    cleaned = query.strip()
    if not cleaned:
        return [cleaned]

    raw_parts: list[str] = []
    keyword_terms: list[str] = []
    for part in re.split(r"\s*\|\s*", cleaned):
        part = _strip_query_prefix(part)
        if not part:
            continue
        if part.lower().startswith("keywords:"):
            keyword_terms = [
                keyword.strip()
                for keyword in part.split(":", 1)[1].split(",")
                if keyword.strip()
            ]
            raw_parts.extend(keyword_terms)
            raw_parts.append(" ".join(keyword_terms))
            continue
        raw_parts.extend(_split_narrative_part(part))

    priority_parts = [" ".join(keyword_terms)] if keyword_terms else []
    components = [_normalize_component(part) for part in [*priority_parts, *raw_parts]]
    components = [part for part in components if _has_search_value(part)]
    if not components:
        components = [cleaned]
    elif len(components) > 1:
        components.append(cleaned)
    return _unique(components)


def _strip_query_prefix(part: str) -> str:
    stripped = part.strip()
    if stripped.lower().startswith("query:"):
        return stripped.split(":", 1)[1].strip()
    return stripped


def _split_narrative_part(part: str) -> list[str]:
    split_pattern = re.compile(
        r"\b(?:and\s+then|then|followed\s+by|cut\s+to|transition\s+to|possibly\s+with)\b",
        flags=re.IGNORECASE,
    )
    return [piece.strip(" .,:;\t\r\n") for piece in split_pattern.split(part) if piece.strip(" .,:;\t\r\n")]


def _normalize_component(part: str) -> str:
    return re.sub(r"\s+", " ", part).strip(" .,:;\t\r\n")


def _has_search_value(part: str) -> bool:
    tokens = re.findall(r"[A-Za-z0-9_+#']+", part)
    meaningful = [token for token in tokens if len(token.strip("'")) >= 3]
    return len(meaningful) >= 2
