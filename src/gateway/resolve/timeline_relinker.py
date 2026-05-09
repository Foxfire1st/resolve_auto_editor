"""Utilities for re-linking timeline clips in-place using the Resolve API."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from gateway.resolve.timeline_snapshot import ClipSnapshot, TimelineSnapshot

TrackType = str


def _frame_to_timecode(frame: int, fps: float) -> str:
    if fps <= 0:
        fps = 24.0
    total_seconds = frame / fps
    hours = int(total_seconds // 3600)
    minutes = int((total_seconds % 3600) // 60)
    seconds = int(total_seconds % 60)
    frames = int(round((total_seconds - int(total_seconds)) * fps))
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}:{frames:02d}"


class NotesIndex:
    """Helper to consume editor notes sequentially for each media asset."""

    def __init__(self, mapping: dict[str, list[dict[str, Any]]]) -> None:
        self._mapping = mapping
        self._cursor: dict[str, int] = {key: 0 for key in mapping}

    @staticmethod
    def _normalize_path(path: str | Path | None) -> str | None:
        if not path:
            return None
        try:
            normalized = Path(path)
        except Exception:
            return str(path).lower()
        return normalized.as_posix().lower()

    @classmethod
    def from_path(cls, notes_path: Path | None) -> "NotesIndex":
        if not notes_path:
            return cls({})
        path = Path(notes_path)
        if not path.exists():
            raise FileNotFoundError(f"Editor notes file not found: {path}")
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        entries = data.get("entries", [])
        mapping: dict[str, list[dict[str, Any]]] = {}
        for entry in entries:
            clip_asset_id = entry.get("clip_asset_id")
            key = cls._normalize_path(clip_asset_id)
            if not key:
                continue
            mapping.setdefault(key, []).append(entry)
        for queue in mapping.values():
            queue.sort(key=lambda item: (item.get("clip_range", {}).get("start", 0), item.get("vo_range", {}).get("start", 0)))
        return cls(mapping)

    def consume(self, media_path: str | Path | None) -> dict[str, Any] | None:
        key = self._normalize_path(media_path)
        if not key or key not in self._mapping:
            return None
        idx = self._cursor.get(key, 0)
        queue = self._mapping[key]
        if idx >= len(queue):
            return None
        self._cursor[key] = idx + 1
        return queue[idx]


@dataclass
class ClipLogEntry:
    """Structured record describing a processed clip."""

    clip: ClipSnapshot
    status: str
    clip_properties: dict[str, Any]
    timeline_timecode: str
    note: dict[str, Any] | None = None
    inserted: bool = False

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "track_type": self.clip.track_type,
            "track_index": self.clip.track_index,
            "record_frame": self.clip.record_frame,
            "timeline_timecode": self.timeline_timecode,
            "duration_frames": self.clip.duration,
            "media_name": self.clip.media_name,
            "media_path": self.clip.media_path,
            "source_start": self.clip.source_start,
            "source_end": self.clip.source_end,
            "clip_color": self.clip.clip_color,
            "status": self.status,
            "clip_properties": self.clip_properties,
            "note": self.note,
            "inserted": self.inserted,
        }
        return payload


@dataclass
class RelinkResult:
    """Return payload summarizing a relink run."""

    timeline_name: str
    entries: list[ClipLogEntry] = field(default_factory=list)
    report_path: Path | None = None

    def summary(self) -> dict[str, Any]:
        status_counts: dict[str, int] = {}
        for entry in self.entries:
            status_counts[entry.status] = status_counts.get(entry.status, 0) + 1
        return {
            "timeline": self.timeline_name,
            "processed": len(self.entries),
            "status_counts": status_counts,
            "report_path": str(self.report_path) if self.report_path else None,
        }

    def write_report(self, destination: Path) -> None:
        destination = destination.resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        report = {
            "timeline": self.timeline_name,
            "entries": [entry.to_dict() for entry in self.entries],
            "status_counts": self.summary()["status_counts"],
        }
        with destination.open("w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2)
        self.report_path = destination


class TimelineRelinker:
    """Coordinates clip-by-clip relinking inside the current Resolve timeline."""

    def __init__(self, project: Any, timeline: Any) -> None:
        self._project = project
        self._timeline = timeline

    def relink(
        self,
        *,
        notes_path: Path | None = None,
        report_path: Path | None = None,
        snapshot: TimelineSnapshot | None = None,
    ) -> RelinkResult:
        media_pool = self._project.GetMediaPool()
        if media_pool is None:
            raise RuntimeError("Unable to access Resolve media pool.")
        if snapshot is None:
            snapshot = TimelineSnapshot.capture(self._project, self._timeline)
        note_index = NotesIndex.from_path(notes_path)
        fps = float(snapshot.frame_rate or 24)
        pending_links: dict[str, list[Any]] = {}
        result = RelinkResult(timeline_name=snapshot.name)

        grouped_clips = self._group_clips(snapshot.clips)
        for group in grouped_clips:
            captured = self._capture_group_items(group)
            if not captured:
                continue
            # Delete existing items before re-adding
            self._delete_items([record.timeline_item for record in captured if record.timeline_item is not None])
            for record in captured:
                log_entry = self._build_log_entry(record.clip, record.clip_properties, note_index.consume(record.clip.media_path), fps)
                if record.media_pool_item is None:
                    log_entry.status = "missing-media"
                    result.entries.append(log_entry)
                    continue
                inserted = self._append_clip(
                    media_pool,
                    record.clip,
                    record.media_pool_item,
                    pending_links,
                    snapshot.link_group_sizes,
                )
                log_entry.inserted = inserted is not None
                log_entry.status = "relinked" if inserted else "append-failed"
                result.entries.append(log_entry)
        if report_path:
            result.write_report(Path(report_path))
        return result

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    @dataclass
    class _CapturedClip:
        clip: ClipSnapshot
        timeline_item: Any | None
        media_pool_item: Any | None
        clip_properties: dict[str, Any]

    def _capture_group_items(self, clips: list[ClipSnapshot]) -> list[_CapturedClip]:
        captured: list[TimelineRelinker._CapturedClip] = []
        for clip in clips:
            item = self._find_timeline_item(clip)
            clip_props = self._safe_clip_props(item)
            media_pool_item = item.GetMediaPoolItem() if item else None
            captured.append(self._CapturedClip(clip=clip, timeline_item=item, media_pool_item=media_pool_item, clip_properties=clip_props))
        return captured

    def _find_timeline_item(self, clip: ClipSnapshot) -> Any | None:
        items = self._timeline.GetItemListInTrack(clip.track_type, clip.track_index) or []
        for item in items:
            try:
                start = int(item.GetStart(False))
            except TypeError:
                start = int(item.GetStart())
            duration = int(item.GetDuration(False))
            if start != clip.record_frame or duration != clip.duration:
                continue
            media_pool_item = item.GetMediaPoolItem()
            media_path = None
            if media_pool_item:
                clip_props = media_pool_item.GetClipProperty() or {}
                media_path = clip_props.get("File Path") or clip_props.get("Source File Path")
            if media_path:
                try:
                    candidate = Path(media_path).as_posix().lower()
                except Exception:
                    candidate = str(media_path).lower()
                try:
                    target = Path(clip.media_path).as_posix().lower()
                except Exception:
                    target = str(clip.media_path).lower()
                if candidate == target:
                    return item
            if not media_path:
                return item
        return None

    def _delete_items(self, items: list[Any]) -> None:
        targets = [item for item in items if item is not None]
        if not targets:
            return

        delete_clips = getattr(self._timeline, "DeleteClips", None)
        if callable(delete_clips):
            try:
                success = delete_clips(targets, False)
            except TypeError:
                success = delete_clips(targets)
            if success is not False:
                return

        delete_item = getattr(self._timeline, "DeleteItem", None)
        if callable(delete_item):
            for item in targets:
                delete_item(item)
            return

        delete_items = getattr(self._timeline, "DeleteItems", None)
        if callable(delete_items):
            delete_items(targets)
            return

        for item in targets:
            delete_method = getattr(item, "Delete", None)
            if callable(delete_method):
                delete_method()
            else:
                raise RuntimeError("Timeline API does not expose any deletion method for clips")

    def _append_clip(
        self,
        media_pool: Any,
        clip: ClipSnapshot,
        media_pool_item: Any,
        pending_links: dict[str, list[Any]],
        link_group_sizes: dict[str, int],
    ) -> Any | None:
        clip_info = {
            "mediaPoolItem": media_pool_item,
            "startFrame": clip.source_start,
            "endFrame": clip.source_end,
            "recordFrame": clip.record_frame,
            "trackIndex": clip.track_index,
            "mediaType": 1 if clip.track_type == "video" else 2,
        }
        inserted_items = media_pool.AppendToTimeline([clip_info]) or []
        inserted = inserted_items[0] if inserted_items else None
        if inserted and clip.clip_color and hasattr(inserted, "SetClipColor"):
            try:
                inserted.SetClipColor(clip.clip_color)
            except Exception:
                pass
        if clip.link_group and inserted:
            members = pending_links.setdefault(clip.link_group, [])
            members.append(inserted)
            expected = link_group_sizes.get(clip.link_group)
            if expected and len(members) >= expected:
                try:
                    self._timeline.SetClipsLinked(members, True)
                except Exception:
                    pass
                pending_links.pop(clip.link_group, None)
        return inserted

    def _build_log_entry(
        self,
        clip: ClipSnapshot,
        clip_properties: dict[str, Any],
        note: dict[str, Any] | None,
        fps: float,
    ) -> ClipLogEntry:
        timecode = _frame_to_timecode(clip.record_frame, fps)
        return ClipLogEntry(
            clip=clip,
            clip_properties=clip_properties,
            note=note,
            status="pending",
            timeline_timecode=timecode,
        )

    def _safe_clip_props(self, timeline_item: Any | None) -> dict[str, Any]:
        if timeline_item is None:
            return {}
        media_pool_item = timeline_item.GetMediaPoolItem()
        if media_pool_item is None:
            return {}
        props = media_pool_item.GetClipProperty()
        if isinstance(props, dict):
            return props
        return {}

    def _group_clips(self, clips: list[ClipSnapshot]) -> list[list[ClipSnapshot]]:
        groups: dict[str, list[ClipSnapshot]] = {}
        for clip in clips:
            key = clip.link_group or f"{clip.track_type}:{clip.track_index}:{clip.record_frame}"
            groups.setdefault(key, []).append(clip)
        sorted_groups = sorted(
            groups.values(),
            key=lambda group: (
                min(c.record_frame for c in group),
                0 if group[0].track_type == "video" else 1,
            ),
        )
        return sorted_groups
