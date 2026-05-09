from typing import Any, Dict, List

from gateway.vision.event_metadata import db
from gateway.vision.event_metadata.config import get_settings
from gateway.vision.event_metadata.search import EventSearchFilters, search_events
from src.orchestrator.tool_bridge.retrieval_tools import find_clips


def list_assets(
    profile: str,
    page: int = 1,
    page_size: int = 20,
    database_path: str | None = None,
) -> List[Dict[str, Any]]:
    """
    List available video assets from event metadata storage.
    """
    offset = max(page - 1, 0) * page_size

    with db.connection_scope(database_path=database_path, profile_name=profile) as connection:
        db.init_db(connection)
        rows = connection.execute(
            """
            SELECT
                c.video_id,
                c.source_path,
                COALESCE(c.duration_s, MAX(e.end_s) - MIN(e.start_s), 0) AS duration
            FROM event_metadata_clips c
            LEFT JOIN event_metadata_events e
                ON e.profile = c.profile AND e.video_id = c.video_id
            WHERE c.profile = ?
            GROUP BY c.video_id, c.source_path, c.duration_s
            ORDER BY c.video_id
            LIMIT ? OFFSET ?
            """,
            (profile, page_size, offset),
        )
        return [
            {
                "asset_id": row["video_id"],
                "duration": float(row["duration"] or 0.0),
                "source_path": row["source_path"],
            }
            for row in rows
        ]


def summarize_asset(
    asset_id: str,
    profile: str | None = None,
    database_path: str | None = None,
) -> Dict[str, Any]:
    """
    Summarize an asset by aggregating its stored event metadata.
    """
    settings = get_settings(profile_name=profile, database_path=database_path)
    with db.connection_scope(database_path=database_path, profile_name=settings.profile) as connection:
        db.init_db(connection)
        clip = connection.execute(
            """
            SELECT video_id, source_path, duration_s, tags_json
            FROM event_metadata_clips
            WHERE profile = ? AND video_id = ?
            """,
            (settings.profile, asset_id),
        ).fetchone()
        if clip is None:
            return {"error": "Asset not found"}

        events = connection.execute(
            """
            SELECT start_s, end_s, details, tags_json
            FROM event_metadata_events
            WHERE profile = ? AND video_id = ?
            ORDER BY start_s
            """,
            (settings.profile, asset_id),
        ).fetchall()

    all_tags: set[str] = set()
    captions: list[str] = []
    for row in events:
        captions.append(row["details"])
        all_tags.update(_json_list(row["tags_json"]))

    first_start = float(events[0]["start_s"]) if events else 0.0
    last_end = float(events[-1]["end_s"]) if events else 0.0
    duration = clip["duration_s"]
    return {
        "asset_id": asset_id,
        "duration": float(duration) if duration is not None else max(last_end - first_start, 0.0),
        "segment_count": len(events),
        "all_tags": sorted(all_tags),
        "captions_sample": captions[:5] + ["..."] if len(captions) > 5 else captions,
    }


def search_segments(query: str, constraints: Dict[str, Any] = None) -> Dict[str, Any]:
    """
    Search event metadata and return a token-optimized summary.
    """
    constraints = constraints or {}
    results = find_clips(query, limit=int(constraints.get("limit", 5)), constraints=constraints)

    output: Dict[str, Any] = {
        "query": query,
        "found_match": bool(results),
        "top_candidates": [],
    }
    if results:
        selected = results[0]
        output["selected_clip"] = {
            "found_match": True,
            "video_id": selected.asset_id,
            "start_time": selected.start,
            "end_time": selected.end,
            "reasoning": selected.description,
            "confidence": selected.score,
            "segment_ids": selected.segment_ids,
        }
        output["matched_candidate_summary"] = {
            "video_id": selected.asset_id,
            "window": f"{selected.start:.2f}-{selected.end:.2f}",
            "score": selected.score,
        }

    for result in results[:5]:
        output["top_candidates"].append(
            {
                "video_id": result.asset_id,
                "window": f"{result.start:.2f}-{result.end:.2f}",
                "score": f"{result.score:.3f}",
                "caption_snippet": (
                    result.description[:100] + "..."
                    if len(result.description) > 100
                    else result.description
                ),
            }
        )
    return output


def inspect_window(
    asset_id: str,
    start: float,
    end: float,
    profile: str | None = None,
    database_path: str | None = None,
) -> Dict[str, Any]:
    """
    Fetch event metadata for a specific window.
    """
    settings = get_settings(profile_name=profile, database_path=database_path)
    results = search_events(
        "",
        profile_name=settings.profile,
        database_path=database_path,
        filters=EventSearchFilters(video_ids=[asset_id]),
        include_review=True,
        limit=100,
    )
    overlapping = [
        result for result in results
        if result.start_s < end and result.end_s > start
    ]
    return {
        "asset_id": asset_id,
        "window": {"start": start, "end": end},
        "segments": [
            {
                "id": result.id,
                "start": result.start_s,
                "end": result.end_s,
                "caption": result.summary,
                "tags": result.tags,
            }
            for result in overlapping
        ],
    }


def _json_list(value: str | None) -> list[str]:
    if not value:
        return []
    import json

    try:
        loaded = json.loads(value)
    except json.JSONDecodeError:
        return []
    if not isinstance(loaded, list):
        return []
    return [str(item) for item in loaded]
