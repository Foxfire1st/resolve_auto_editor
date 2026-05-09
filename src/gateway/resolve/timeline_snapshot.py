"""Utilities for capturing and recreating Resolve timelines via the scripting API."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal


TrackType = Literal["video", "audio"]
MediaType = Literal["video", "audio"]


@dataclass
class MediaSpec:
    """Represents a single media asset Resolve should re-import."""

    path: str
    name: str
    properties: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"path": self.path, "name": self.name, "properties": self.properties}

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "MediaSpec":
        return cls(path=payload["path"], name=payload.get("name", ""), properties=payload.get("properties", {}))


@dataclass
class ClipSnapshot:
    """Minimal description of a clip's placement on the timeline."""

    media_path: str
    media_name: str
    track_type: TrackType
    track_index: int
    media_type: MediaType
    record_frame: int
    source_start: int
    source_end: int
    duration: int
    link_group: str | None = None
    clip_color: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        return payload

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ClipSnapshot":
        return cls(
            media_path=payload["media_path"],
            media_name=payload.get("media_name", ""),
            track_type=payload["track_type"],
            track_index=int(payload["track_index"]),
            media_type=payload["media_type"],
            record_frame=int(payload["record_frame"]),
            source_start=int(payload["source_start"]),
            source_end=int(payload["source_end"]),
            duration=int(payload["duration"]),
            link_group=payload.get("link_group"),
            clip_color=payload.get("clip_color"),
        )


@dataclass
class TimelineSnapshot:
    """Serialized representation of a Resolve timeline."""

    name: str
    frame_rate: str
    start_timecode: str
    resolution_width: int
    resolution_height: int
    audio_track_subtypes: dict[int, str]
    clips: list[ClipSnapshot]
    media: dict[str, MediaSpec]
    link_group_sizes: dict[str, int]

    # ---------------------------------------------------------------------
    # Capture helpers
    # ---------------------------------------------------------------------
    @classmethod
    def capture(cls, project: Any, timeline: Any) -> "TimelineSnapshot":
        """Snapshot the currently active Resolve timeline."""

        if timeline is None:
            raise RuntimeError("No active timeline to snapshot.")

        name = timeline.GetName()
        frame_rate = project.GetSetting("timelineFrameRate") or timeline.GetSetting("timelineFrameRate") or "24"
        start_timecode = timeline.GetStartTimecode() or "00:00:00:00"
        resolution_width = int(project.GetSetting("timelineResolutionWidth") or 1920)
        resolution_height = int(project.GetSetting("timelineResolutionHeight") or 1080)

        audio_track_count = timeline.GetTrackCount("audio") or 0
        audio_track_subtypes: dict[int, str] = {}
        for idx in range(1, audio_track_count + 1):
            subtype = timeline.GetTrackSubType("audio", idx)
            audio_track_subtypes[idx] = subtype or "mono"

        media_specs: dict[str, MediaSpec] = {}
        clips: list[ClipSnapshot] = []
        link_group_sizes: dict[str, int] = {}

        for track_type in ("video", "audio"):
            track_count = timeline.GetTrackCount(track_type) or 0
            for track_index in range(1, track_count + 1):
                items = timeline.GetItemListInTrack(track_type, track_index) or []
                for item in items:
                    media_pool_item = item.GetMediaPoolItem()
                    if media_pool_item is None:
                        continue
                    clip_props = media_pool_item.GetClipProperty() or {}
                    media_path = clip_props.get("File Path") or clip_props.get("Source File Path")
                    if not media_path:
                        continue
                    media_path = str(media_path)
                    if media_path not in media_specs:
                        media_specs[media_path] = MediaSpec(
                            path=media_path,
                            name=media_pool_item.GetName() or Path(media_path).name,
                            properties={
                                key: value
                                for key, value in clip_props.items()
                                if isinstance(key, str) and isinstance(value, str)
                            },
                        )

                    record_frame = int(item.GetStart(False))
                    source_start = int(item.GetSourceStartFrame())
                    source_end = int(item.GetSourceEndFrame())
                    duration = int(item.GetDuration(False))
                    media_type: MediaType = "video" if track_type == "video" else "audio"
                    clip_color = item.GetClipColor() if hasattr(item, "GetClipColor") else None

                    link_group = cls._compute_link_group(item)
                    if link_group:
                        link_group_sizes[link_group] = link_group_sizes.get(link_group, 0) + 1

                    clips.append(
                        ClipSnapshot(
                            media_path=media_path,
                            media_name=media_pool_item.GetName() or Path(media_path).name,
                            track_type=track_type,
                            track_index=track_index,
                            media_type=media_type,
                            record_frame=record_frame,
                            source_start=source_start,
                            source_end=source_end,
                            duration=duration,
                            link_group=link_group,
                            clip_color=clip_color,
                        )
                    )

        clips.sort(key=lambda c: (c.record_frame, 0 if c.track_type == "video" else 1, c.track_index))
        return cls(
            name=name,
            frame_rate=str(frame_rate),
            start_timecode=start_timecode,
            resolution_width=resolution_width,
            resolution_height=resolution_height,
            audio_track_subtypes=audio_track_subtypes,
            clips=clips,
            media=media_specs,
            link_group_sizes=link_group_sizes,
        )

    @staticmethod
    def _compute_link_group(item: Any) -> str | None:
        linked_items = item.GetLinkedItems() or []
        members = [item] + [linked for linked in linked_items if linked is not None]
        if len(members) <= 1:
            return None

        signatures: list[str] = []
        for member in members:
            media_pool_item = member.GetMediaPoolItem()
            if media_pool_item is None:
                continue
            media_id = getattr(media_pool_item, "GetMediaId", lambda: None)()
            member_start = int(member.GetStart(False))
            track_type, track_index = member.GetTrackTypeAndIndex()
            signatures.append(f"{track_type}:{track_index}:{member_start}:{media_id}")

        if not signatures:
            return None
        signatures.sort()
        return "|".join(signatures)

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "frame_rate": self.frame_rate,
            "start_timecode": self.start_timecode,
            "resolution_width": self.resolution_width,
            "resolution_height": self.resolution_height,
            "audio_track_subtypes": {str(k): v for k, v in self.audio_track_subtypes.items()},
            "clips": [clip.to_dict() for clip in self.clips],
            "media": {path: spec.to_dict() for path, spec in self.media.items()},
            "link_group_sizes": self.link_group_sizes,
        }

    def to_json(self, destination: Path) -> Path:
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("w", encoding="utf-8") as fp:
            json.dump(self.to_dict(), fp, indent=2)
        return destination

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "TimelineSnapshot":
        return cls(
            name=payload["name"],
            frame_rate=str(payload.get("frame_rate", "24")),
            start_timecode=payload.get("start_timecode", "00:00:00:00"),
            resolution_width=int(payload.get("resolution_width", 1920)),
            resolution_height=int(payload.get("resolution_height", 1080)),
            audio_track_subtypes={int(k): v for k, v in payload.get("audio_track_subtypes", {}).items()},
            clips=[ClipSnapshot.from_dict(entry) for entry in payload.get("clips", [])],
            media={path: MediaSpec.from_dict(spec) for path, spec in payload.get("media", {}).items()},
            link_group_sizes={key: int(value) for key, value in payload.get("link_group_sizes", {}).items()},
        )

    @classmethod
    def from_json(cls, source: Path) -> "TimelineSnapshot":
        with source.open("r", encoding="utf-8") as fp:
            payload = json.load(fp)
        return cls.from_dict(payload)

    # ------------------------------------------------------------------
    # Timeline reconstruction
    # ------------------------------------------------------------------
    def rebuild(self, project: Any, resolve: Any) -> Any:
        """Recreate the captured timeline inside the given Resolve project."""

        media_pool = project.GetMediaPool()
        if media_pool is None:
            raise RuntimeError("Unable to access Resolve media pool.")

        media_storage = resolve.GetMediaStorage()
        if media_storage is None:
            raise RuntimeError("Resolve media storage is unavailable.")

        unique_paths = list(self.media.keys())
        imported_items = media_storage.AddItemListToMediaPool(unique_paths)
        if not imported_items:
            raise RuntimeError("Resolve did not import any media for the snapshot rebuild.")

        path_to_item = {}
        for path, media_pool_item in zip(unique_paths, imported_items):
            if media_pool_item is not None:
                path_to_item[path] = media_pool_item

        missing = [path for path in unique_paths if path not in path_to_item]
        if missing:
            raise RuntimeError(f"Failed to import media files: {missing}")

        timeline = media_pool.CreateEmptyTimeline(self.name)
        if timeline is None:
            raise RuntimeError("Failed to create a new timeline for rebuild.")
        project.SetCurrentTimeline(timeline)

        # Apply basic settings
        timeline.SetSetting("timelineFrameRate", str(self.frame_rate))
        timeline.SetSetting("timelineStartTimecode", self.start_timecode)
        timeline.SetSetting("timelineResolutionWidth", str(self.resolution_width))
        timeline.SetSetting("timelineResolutionHeight", str(self.resolution_height))

        # Ensure enough tracks exist
        self._ensure_track_counts(timeline)

        pending_links: dict[str, list[Any]] = {}
        for clip in self.clips:
            media_pool_item = path_to_item.get(clip.media_path)
            if media_pool_item is None:
                raise RuntimeError(f"Media not available for clip {clip.media_name}: {clip.media_path}")

            clip_info = {
                "mediaPoolItem": media_pool_item,
                "startFrame": clip.source_start,
                "endFrame": clip.source_end,
                "trackIndex": clip.track_index,
                "recordFrame": clip.record_frame,
                "mediaType": 1 if clip.media_type == "video" else 2,
            }
            inserted_items = media_pool.AppendToTimeline([clip_info]) or []
            inserted = inserted_items[0] if inserted_items else None
            if inserted and clip.clip_color:
                inserted.SetClipColor(clip.clip_color)

            if clip.link_group and inserted:
                group_items = pending_links.setdefault(clip.link_group, [])
                group_items.append(inserted)
                expected = self.link_group_sizes.get(clip.link_group)
                if expected and len(group_items) >= expected:
                    timeline.SetClipsLinked(group_items, True)
                    pending_links.pop(clip.link_group, None)

        return timeline

    def _ensure_track_counts(self, timeline: Any) -> None:
        video_needed = 0
        audio_needed = 0
        for clip in self.clips:
            if clip.track_type == "video":
                video_needed = max(video_needed, clip.track_index)
            elif clip.track_type == "audio":
                audio_needed = max(audio_needed, clip.track_index)

        while timeline.GetTrackCount("video") < video_needed:
            timeline.AddTrack("video", None)

        while timeline.GetTrackCount("audio") < audio_needed:
            next_index = timeline.GetTrackCount("audio") + 1
            subtype = self.audio_track_subtypes.get(next_index)
            if subtype:
                timeline.AddTrack("audio", subtype)
            else:
                timeline.AddTrack("audio", None)
