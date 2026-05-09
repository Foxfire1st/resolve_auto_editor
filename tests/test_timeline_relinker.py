from __future__ import annotations

import json
from pathlib import Path

from gateway.resolve.timeline_relinker import TimelineRelinker
from gateway.resolve.timeline_snapshot import ClipSnapshot, MediaSpec, TimelineSnapshot


class FakeMediaPoolItem:
    def __init__(self, path: str, name: str) -> None:
        self._path = path
        self._name = name
        self._properties = {"File Path": path, "Name": name}

    def GetClipProperty(self) -> dict[str, str]:
        return self._properties

    def GetName(self) -> str:
        return self._name


class FakeTimelineItem:
    def __init__(
        self,
        media_pool_item: FakeMediaPoolItem,
        *,
        track_type: str,
        track_index: int,
        record_frame: int,
        duration: int,
        source_start: int,
        source_end: int,
        clip_color: str | None = None,
    ) -> None:
        self.media_pool_item = media_pool_item
        self.track_type = track_type
        self.track_index = track_index
        self.record_frame = record_frame
        self.duration = duration
        self.source_start = source_start
        self.source_end = source_end
        self.clip_color = clip_color

    def GetMediaPoolItem(self) -> FakeMediaPoolItem:
        return self.media_pool_item

    def GetClipProperty(self) -> dict[str, str]:  # pragma: no cover - delegated to media item
        return self.media_pool_item.GetClipProperty()

    def GetStart(self, *_args) -> int:
        return self.record_frame

    def GetDuration(self, *_args) -> int:
        return self.duration

    def SetClipColor(self, color: str) -> None:
        self.clip_color = color

    def GetName(self) -> str:
        return self.media_pool_item.GetName()


class FakeTimeline:
    def __init__(self, name: str = "Test Timeline") -> None:
        self._name = name
        self.tracks: dict[str, dict[int, list[FakeTimelineItem]]] = {"video": {}, "audio": {}}
        self.linked_groups: list[list[str]] = []

    def add_item(self, item: FakeTimelineItem) -> None:
        self.tracks[item.track_type].setdefault(item.track_index, []).append(item)

    def GetName(self) -> str:
        return self._name

    def GetItemListInTrack(self, track_type: str, track_index: int) -> list[FakeTimelineItem]:
        return list(self.tracks.get(track_type, {}).get(track_index, []))

    def DeleteItem(self, item: FakeTimelineItem) -> None:
        items = self.tracks[item.track_type].get(item.track_index, [])
        if item in items:
            items.remove(item)

    def DeleteClips(self, items: list[FakeTimelineItem], ripple: bool = False) -> bool:
        for item in list(items):
            self.DeleteItem(item)
        return True

    def SetClipsLinked(self, items: list[FakeTimelineItem], linked: bool) -> None:
        if linked:
            self.linked_groups.append([clip.media_pool_item.GetName() for clip in items])


class FakeMediaPool:
    def __init__(self, timeline: FakeTimeline) -> None:
        self._timeline = timeline

    def AppendToTimeline(self, clip_infos: list[dict[str, object]]) -> list[FakeTimelineItem]:
        inserted: list[FakeTimelineItem] = []
        for info in clip_infos:
            media_item = info["mediaPoolItem"]
            track_type = "video" if info["mediaType"] == 1 else "audio"
            track_index = info["trackIndex"]
            record_frame = info["recordFrame"]
            start_frame = info["startFrame"]
            end_frame = info["endFrame"]
            clip = FakeTimelineItem(
                media_item,
                track_type=track_type,
                track_index=int(track_index),
                record_frame=int(record_frame),
                duration=int(end_frame) - int(start_frame),
                source_start=int(start_frame),
                source_end=int(end_frame),
            )
            self._timeline.add_item(clip)
            inserted.append(clip)
        return inserted


class FakeProject:
    def __init__(self, media_pool: FakeMediaPool) -> None:
        self._media_pool = media_pool

    def GetMediaPool(self) -> FakeMediaPool:
        return self._media_pool


def _build_snapshot() -> TimelineSnapshot:
    video_clip = ClipSnapshot(
        media_path="C:/clips/topaz.mp4",
        media_name="topaz.mp4",
        track_type="video",
        track_index=1,
        media_type="video",
        record_frame=100,
        source_start=200,
        source_end=300,
        duration=100,
        link_group="grp1",
        clip_color="Blue",
    )
    audio_clip = ClipSnapshot(
        media_path="C:/clips/topaz.mp4",
        media_name="topaz.mp4",
        track_type="audio",
        track_index=1,
        media_type="audio",
        record_frame=100,
        source_start=200,
        source_end=300,
        duration=100,
        link_group="grp1",
    )
    return TimelineSnapshot(
        name="Core Edit",
        frame_rate="24",
        start_timecode="01:00:00:00",
        resolution_width=1920,
        resolution_height=1080,
        audio_track_subtypes={1: "Stereo"},
        clips=[video_clip, audio_clip],
        media={video_clip.media_path: MediaSpec(path=video_clip.media_path, name=video_clip.media_name)},
        link_group_sizes={"grp1": 2},
    )


def test_relinker_rebuilds_clips_and_writes_report(tmp_path: Path) -> None:
    snapshot = _build_snapshot()
    media_item = FakeMediaPoolItem(snapshot.clips[0].media_path, snapshot.clips[0].media_name)
    video_item = FakeTimelineItem(
        media_item,
        track_type="video",
        track_index=1,
        record_frame=100,
        duration=100,
        source_start=200,
        source_end=300,
        clip_color="Blue",
    )
    audio_item = FakeTimelineItem(
        media_item,
        track_type="audio",
        track_index=1,
        record_frame=100,
        duration=100,
        source_start=200,
        source_end=300,
    )
    timeline = FakeTimeline()
    timeline.add_item(video_item)
    timeline.add_item(audio_item)

    media_pool = FakeMediaPool(timeline)
    project = FakeProject(media_pool)

    notes_path = tmp_path / "notes.json"
    notes_payload = {
        "entries": [
            {
                "clip_asset_id": snapshot.clips[0].media_path,
                "clip_range": {"start": 0, "duration": 4},
                "visual_summary": "Sample",
                "vo_excerpt": "Line",
                "vo_range": {"start": 0, "duration": 1},
            }
        ]
    }
    notes_path.write_text(json.dumps(notes_payload), encoding="utf-8")

    report_path = tmp_path / "report.json"
    relinker = TimelineRelinker(project, timeline)
    result = relinker.relink(notes_path=notes_path, report_path=report_path, snapshot=snapshot)
    summary = result.summary()

    assert summary["processed"] == 2
    assert summary["status_counts"]["relinked"] == 2
    assert report_path.exists()

    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert len(report["entries"]) == 2
    assert report["entries"][0]["note"]["visual_summary"] == "Sample"
    assert timeline.linked_groups  # ensure re-linking attempted