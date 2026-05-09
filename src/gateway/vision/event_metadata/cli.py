"""Command-line interface for event metadata ingest and search."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

from . import db
from .config import get_settings
from .ingest import ingest_gemini_json
from .search import EventSearchFilters, search_events

DEFAULT_FOLDERS = ("regular", "slowdown")
META_SUFFIX = "_gemini.json"


@dataclass(frozen=True)
class BatchTarget:
    folder: str
    video_path: Path
    gemini_json: Path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="event-metadata")
    subparsers = parser.add_subparsers(dest="command", required=True)

    ingest_parser = subparsers.add_parser("ingest", help="Ingest one Gemini JSON artifact into event metadata")
    ingest_parser.add_argument("--video-path", type=Path, required=True)
    ingest_parser.add_argument("--gemini-json", type=Path, required=True)
    ingest_parser.add_argument("--profile", default=None)
    ingest_parser.add_argument("--database", type=Path, default=None)
    ingest_parser.add_argument("--clip-duration-s", type=float, default=None)

    batch_parser = subparsers.add_parser(
        "batch-ingest",
        help="Ingest video/Gemini JSON pairs from footage folders",
    )
    batch_parser.add_argument(
        "--footage-root",
        type=Path,
        required=True,
        help="Root directory containing footage folders, each with a meta/ subfolder.",
    )
    batch_parser.add_argument(
        "--folders",
        nargs="+",
        default=list(DEFAULT_FOLDERS),
        help="Footage subfolders to ingest (default: regular slowdown).",
    )
    batch_parser.add_argument("--profile", default=None)
    batch_parser.add_argument("--database", type=Path, default=None)
    batch_parser.add_argument("--clip-duration-s", type=float, default=None)
    batch_parser.add_argument("--dry-run", action="store_true")

    search_parser = subparsers.add_parser("search", help="Search trusted event metadata spans")
    search_parser.add_argument("query", nargs="?", default="")
    search_parser.add_argument("--profile", default=None)
    search_parser.add_argument("--database", type=Path, default=None)
    search_parser.add_argument("--actor")
    search_parser.add_argument("--action-type")
    search_parser.add_argument("--ability-name")
    search_parser.add_argument("--video-id", action="append", dest="video_ids")
    search_parser.add_argument("--video-id-contains")
    search_parser.add_argument("--required-tag", action="append", dest="required_tags", default=[])
    search_parser.add_argument("--include-review", action="store_true")
    search_parser.add_argument("--limit", type=int, default=10)

    describe_parser = subparsers.add_parser(
        "describe",
        help="Summarize stored event metadata for videos",
    )
    describe_parser.add_argument("--profile", default=None)
    describe_parser.add_argument("--database", type=Path, default=None)
    describe_parser.add_argument("--videos", nargs="+")
    describe_parser.add_argument("--contains")
    describe_parser.add_argument("--limit", type=int, default=20)
    describe_parser.add_argument("--sample-size", type=int, default=2)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.command == "ingest":
        with db.connection_scope(args.database, profile_name=args.profile) as connection:
            rows = ingest_gemini_json(
                args.video_path,
                args.gemini_json,
                profile_name=args.profile,
                database_path=args.database,
                clip_duration_s=args.clip_duration_s,
                connection=connection,
            )
        print(f"Ingested {len(rows)} event metadata rows")
        return 0

    if args.command == "batch-ingest":
        targets = _discover_batch_targets(args.footage_root, args.folders)
        print(f"Discovered {len(targets)} video/Gemini JSON pairs")
        ingested_count = 0
        with db.connection_scope(args.database, profile_name=args.profile) as connection:
            for target in targets:
                label = f"[{target.folder}] ingest {target.video_path.name}"
                print(label)
                if args.dry_run:
                    print(f"  DRY RUN: {target.gemini_json}")
                    continue
                rows = ingest_gemini_json(
                    target.video_path,
                    target.gemini_json,
                    profile_name=args.profile,
                    database_path=args.database,
                    clip_duration_s=args.clip_duration_s,
                    connection=connection,
                )
                ingested_count += len(rows)
        print(f"Ingested {ingested_count} event metadata rows")
        return 0

    if args.command == "search":
        filters = EventSearchFilters(
            actor=args.actor,
            action_type=args.action_type,
            ability_name=args.ability_name,
            video_ids=args.video_ids,
            required_tags=args.required_tags,
        )
        results = search_events(
            args.query,
            profile_name=args.profile,
            database_path=args.database,
            filters=filters,
            include_review=args.include_review,
            limit=max(args.limit * 5, args.limit) if args.video_id_contains else args.limit,
        )
        if args.video_id_contains:
            needle = args.video_id_contains.lower()
            results = [result for result in results if needle in result.video_id.lower()]
        results = results[: args.limit]
        for result in results:
            review = " review" if result.needs_boundary_review else ""
            print(
                f"{result.video_id} {result.start_s:.2f}-{result.end_s:.2f}s "
                f"score={result.score:.3f}{review} {result.summary}"
            )
        return 0

    if args.command == "describe":
        summaries = _describe_videos(
            profile_name=args.profile,
            database_path=args.database,
            video_ids=args.videos,
            video_id_contains=args.contains,
            limit=args.limit,
            sample_size=args.sample_size,
        )
        if not summaries:
            target = args.videos or f"contains {args.contains!r}"
            print(f"No event metadata found for {target}")
            return 0
        for summary in summaries:
            print(_format_summary(summary))
        return 0

    parser.error(f"Unknown command {args.command!r}")
    return 2


def _discover_batch_targets(footage_root: Path, folders: Iterable[str]) -> list[BatchTarget]:
    targets: list[BatchTarget] = []
    missing_meta: dict[str, list[str]] = {}

    for folder in folders:
        video_dir = footage_root / folder
        meta_dir = video_dir / "meta"
        if not video_dir.exists():
            raise FileNotFoundError(f"Missing footage folder: {video_dir}")
        if not meta_dir.exists():
            raise FileNotFoundError(f"Missing meta folder: {meta_dir}")

        missing_meta[folder] = []
        for video_path in sorted(video_dir.glob("*.mp4")):
            gemini_json = meta_dir / f"{video_path.stem}{META_SUFFIX}"
            if not gemini_json.exists():
                missing_meta[folder].append(video_path.name)
                continue
            if not _has_gemini_events(gemini_json):
                print(
                    f"Skipping {video_path.name}: no Gemini events in {gemini_json}",
                    file=sys.stderr,
                )
                continue
            targets.append(BatchTarget(folder=folder, video_path=video_path, gemini_json=gemini_json))

    dangling = {folder: names for folder, names in missing_meta.items() if names}
    if dangling:
        details = "\n".join(
            f"  {folder}: {', '.join(names)}" for folder, names in dangling.items()
        )
        raise SystemExit("Found videos without matching Gemini metadata files:\n" + details)
    return targets


def _has_gemini_events(path: Path) -> bool:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return False
    events = payload.get("events") if isinstance(payload, dict) else None
    return isinstance(events, list) and len(events) > 0


def _describe_videos(
    *,
    profile_name: str | None,
    database_path: Path | None,
    video_ids: Sequence[str] | None,
    video_id_contains: str | None,
    limit: int,
    sample_size: int,
) -> list[dict[str, object]]:
    connection = db.connect(database_path, profile_name=profile_name)
    try:
        settings = get_settings(profile_name=profile_name, database_path=database_path)
        db.init_db(connection)
        clauses = ["c.profile = ?"]
        params: list[object] = [settings.profile]
        if video_ids:
            placeholders = ", ".join("?" for _ in video_ids)
            clauses.append(f"c.video_id IN ({placeholders})")
            params.extend(video_ids)
        if video_id_contains:
            clauses.append("lower(c.video_id) LIKE ?")
            params.append(f"%{video_id_contains.lower()}%")
        where = " AND ".join(clauses)
        rows = connection.execute(
            f"""
            SELECT c.video_id, c.source_path, c.duration_s, c.tags_json,
                   MIN(e.start_s) AS start_s, MAX(e.end_s) AS end_s,
                   COUNT(e.id) AS event_count
            FROM event_metadata_clips c
            LEFT JOIN event_metadata_events e
                ON e.profile = c.profile AND e.video_id = c.video_id
            WHERE {where}
            GROUP BY c.video_id, c.source_path, c.duration_s, c.tags_json
            ORDER BY c.video_id
            LIMIT ?
            """,
            (*params, limit),
        ).fetchall()
        summaries: list[dict[str, object]] = []
        for row in rows:
            samples = connection.execute(
                """
                SELECT details
                FROM event_metadata_events
                WHERE profile = ? AND video_id = ?
                ORDER BY start_s
                LIMIT ?
                """,
                (settings.profile, row["video_id"], sample_size),
            ).fetchall()
            summaries.append(
                {
                    "video_id": row["video_id"],
                    "source_path": row["source_path"],
                    "duration_s": row["duration_s"],
                    "start_s": row["start_s"],
                    "end_s": row["end_s"],
                    "event_count": row["event_count"],
                    "tags": _json_list(row["tags_json"]),
                    "samples": [sample["details"] for sample in samples],
                }
            )
        return summaries
    finally:
        connection.close()


def _format_summary(summary: dict[str, object]) -> str:
    if summary["start_s"] is not None and summary["end_s"] is not None:
        duration = f"{float(summary['start_s']):.2f}s-{float(summary['end_s']):.2f}s"
    else:
        duration = "n/a"
    tags = ", ".join(str(tag) for tag in summary["tags"]) if summary["tags"] else "(no tags)"
    samples = "; ".join(str(sample) for sample in summary["samples"]) if summary["samples"] else "(no events)"
    return (
        f"video={summary['video_id']} events={summary['event_count']} duration={duration} "
        f"source={summary['source_path'] or 'n/a'} tags={tags}\n  samples: {samples}"
    )


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


if __name__ == "__main__":
    raise SystemExit(main())
