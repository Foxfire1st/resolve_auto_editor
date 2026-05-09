from __future__ import annotations

from pathlib import Path

from gateway.resolve.timeline_snapshot import TimelineSnapshot


class FakeMediaPoolItem:
    def __init__(self, path: str, name: str, props: dict[str, str] | None = None, media_id: str | None = None) -> None:
        self._path = path
        self._name = name
        base_props = {"File Path": path, "Source File Path": path, "Name": name}
        base_props.update(props or {})
        self._props = base_props
        self._media_id = media_id or f"media-{name}"

    def GetName(self) -> str:
        return self._name

    def GetClipProperty(self) -> dict[str, str]:
        return self._props

    def GetMediaId(self) -> str:
        return self._media_id


class FakeTimelineItem:
    def __init__(
        self,
        media_item: FakeMediaPoolItem,
        *,
        track_type: str,
        track_index: int,
        record_frame: int,
        source_start: int,
        source_end: int,
        duration: int,
        clip_color: str | None = None,
    ) -> None:
        self._media_item = media_item
        self._track_type = track_type
        self._track_index = track_index
        self._record_frame = record_frame
        self._source_start = source_start
        self._source_end = source_end
        self._duration = duration
        self._clip_color = clip_color
        self._linked_items: list[FakeTimelineItem] = []

    def link_with(self, *others: "FakeTimelineItem") -> None:
        self._linked_items = list(others)

    def GetMediaPoolItem(self) -> FakeMediaPoolItem:
        return self._media_item

    def GetStart(self, *_args, **_kwargs) -> int:
        return self._record_frame

    def GetSourceStartFrame(self) -> int:
        return self._source_start

    def GetSourceEndFrame(self) -> int:
        return self._source_end

    def GetDuration(self, *_args, **_kwargs) -> int:
        return self._duration

    def GetClipColor(self) -> str | None:
        return self._clip_color

    def GetLinkedItems(self) -> list["FakeTimelineItem"]:
        return self._linked_items

    def GetTrackTypeAndIndex(self) -> tuple[str, int]:
        return self._track_type, self._track_index


class FakeCaptureTimeline:
    def __init__(self, name: str, start_timecode: str, tracks: dict[str, dict[int, list[FakeTimelineItem]]]) -> None:
        self._name = name
        self._start_timecode = start_timecode
        self._tracks = tracks
        self._audio_subtypes: dict[int, str] = {1: "Stereo"}

    def GetName(self) -> str:
        return self._name

    def GetStartTimecode(self) -> str:
        return self._start_timecode

    def GetTrackCount(self, track_type: str) -> int:
        return len(self._tracks.get(track_type, {}))

    def GetItemListInTrack(self, track_type: str, index: int) -> list[FakeTimelineItem]:
        return list(self._tracks.get(track_type, {}).get(index, []))

    def GetTrackSubType(self, track_type: str, index: int) -> str | None:
        if track_type != "audio":
            return None
        return self._audio_subtypes.get(index)

    def GetSetting(self, _setting: str) -> str | None:
        return None


class FakeCaptureProject:
    def __init__(self) -> None:
        self._settings = {
            "timelineFrameRate": "24",
            "timelineResolutionWidth": "1920",
            "timelineResolutionHeight": "1080",
        }

    def GetSetting(self, key: str) -> str | None:
        return self._settings.get(key)


class FakeInsertedClip:
    def __init__(self, clip_info: dict[str, object]) -> None:
        self.clip_info = clip_info
        self.color: str | None = None

    def SetClipColor(self, color: str) -> None:
        self.color = color


class FakeRebuildTimeline:
    def __init__(self, name: str) -> None:
        self._name = name
        self.settings: dict[str, str] = {}
        self.video_tracks = 0
        self.audio_tracks = 0
        self.linked_groups: list[list[str]] = []
        self.added_audio_subtypes: list[str | None] = []

    def GetName(self) -> str:
        return self._name

    def SetSetting(self, key: str, value: str) -> None:
        self.settings[key] = value

    def GetTrackCount(self, track_type: str) -> int:
        return self.video_tracks if track_type == "video" else self.audio_tracks

    def AddTrack(self, track_type: str, subtype: str | None) -> bool:
        if track_type == "video":
            self.video_tracks += 1
        else:
            self.audio_tracks += 1
            self.added_audio_subtypes.append(subtype)
        return True

    def SetClipsLinked(self, items: list[FakeInsertedClip], linked: bool) -> None:
        if linked:
            self.linked_groups.append([clip.clip_info["mediaPoolItem"].GetName() for clip in items])


class FakeMediaPool:
    def __init__(self) -> None:
        self.current_timeline: FakeRebuildTimeline | None = None
        self.appended: list[FakeInsertedClip] = []

    def CreateEmptyTimeline(self, name: str) -> FakeRebuildTimeline:
        timeline = FakeRebuildTimeline(name)
        self.current_timeline = timeline
        return timeline

    def AppendToTimeline(self, clips: list[dict[str, object]]) -> list[FakeInsertedClip]:
        inserted = [FakeInsertedClip(info) for info in clips]
        self.appended.extend(inserted)
        return inserted

    def DeleteClips(self, _clips: list[object]) -> None:  # pragma: no cover - provided for completeness
        pass

    def DeleteFolders(self, _folders: list[object]) -> None:  # pragma: no cover - provided for completeness
        pass


class FakeProjectRebuild:
    def __init__(self, media_pool: FakeMediaPool) -> None:
        self._media_pool = media_pool
        self.current_timeline: FakeRebuildTimeline | None = None

    def GetMediaPool(self) -> FakeMediaPool:
        return self._media_pool

    def SetCurrentTimeline(self, timeline: FakeRebuildTimeline) -> None:
        self.current_timeline = timeline


class FakeMediaStorage:
    def __init__(self) -> None:
        self.imported_paths: list[str] = []

    def AddItemListToMediaPool(self, paths: list[str]) -> list[FakeMediaPoolItem]:
        self.imported_paths.extend(paths)
        return [FakeMediaPoolItem(path, Path(path).name) for path in paths]


class FakeResolve:
    def __init__(self, media_storage: FakeMediaStorage) -> None:
        self._media_storage = media_storage

    def GetMediaStorage(self) -> FakeMediaStorage:
        return self._media_storage


def _build_sample_capture_state() -> tuple[FakeCaptureProject, FakeCaptureTimeline]:
    media_item = FakeMediaPoolItem("/tmp/clip.mov", "clip.mov")
    video_item = FakeTimelineItem(
        media_item,
        track_type="video",
        track_index=1,
        record_frame=100,
        source_start=25,
        source_end=125,
        duration=100,
        clip_color="Blue",
    )
    audio_item = FakeTimelineItem(
        media_item,
        track_type="audio",
        track_index=1,
        record_frame=100,
        source_start=25,
        source_end=125,
        duration=100,
    )
    video_item.link_with(audio_item)
    audio_item.link_with(video_item)

    project = FakeCaptureProject()
    tracks = {
        "video": {1: [video_item]},
        "audio": {1: [audio_item]},
    }
    timeline = FakeCaptureTimeline("Original", "01:00:00:00", tracks)
    return project, timeline


def test_timeline_snapshot_round_trip(tmp_path) -> None:
    project, timeline = _build_sample_capture_state()
    snapshot = TimelineSnapshot.capture(project, timeline)
    assert snapshot.name == "Original"
    assert snapshot.audio_track_subtypes == {1: "Stereo"}
    assert len(snapshot.media) == 1
    assert len(snapshot.link_group_sizes) == 1

    snapshot_path = tmp_path / "snapshot.json"
    snapshot.to_json(snapshot_path)
    reloaded = TimelineSnapshot.from_json(snapshot_path)
    assert reloaded.clips[0].media_name == "clip.mov"

    media_pool = FakeMediaPool()
    project_rebuild = FakeProjectRebuild(media_pool)
    media_storage = FakeMediaStorage()
    resolve = FakeResolve(media_storage)
    rebuilt_timeline = reloaded.rebuild(project_rebuild, resolve)

    assert project_rebuild.current_timeline is rebuilt_timeline
    assert rebuilt_timeline.settings["timelineFrameRate"] == "24"
    assert rebuilt_timeline.settings["timelineStartTimecode"] == "01:00:00:00"
    assert rebuilt_timeline.video_tracks == 1
    assert rebuilt_timeline.audio_tracks == 1
    assert rebuilt_timeline.added_audio_subtypes == ["Stereo"]
    assert rebuilt_timeline.linked_groups and len(rebuilt_timeline.linked_groups[0]) == 2
    assert len(media_pool.appended) == 2
    assert any(clip.color == "Blue" for clip in media_pool.appended)