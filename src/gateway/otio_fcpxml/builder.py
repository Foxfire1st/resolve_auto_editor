"""OTIO timeline construction helpers for the Resolve roundtrip MVP.

The goal is to convert a semantic plan (SemanticEdit)
into an ``otio.schema.Timeline`` that DaVinci Resolve can import via FCPXML.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Iterable, Mapping, Optional, Sequence, Union, List, Tuple
import xml.etree.ElementTree as ET

import opentimelineio as otio
from opentimelineio import exceptions as otio_exceptions
from src.orchestrator.models import SemanticEdit, EditSegment, ClipSpec, AudioClipSpec, SourceRange

try:  # Ensure the FCPXML adapter is registered
    import opentimelineio.contrib.adapters.fcpxml  # type: ignore  # noqa: F401
except Exception:  # pragma: no cover - optional dependency
    pass

try:  # Some environments expose contrib adapters under a different namespace
    import opentimelineio_contrib.adapters.fcpxml  # type: ignore  # noqa: F401
except Exception:  # pragma: no cover - optional dependency
    pass

try:  # Prefer optional ffmpeg-python for duration probing when available
    import ffmpeg
except Exception:  # pragma: no cover - probing will fall back to ``None``
    ffmpeg = None  # type: ignore


def _probe_duration(media_path: Path) -> float:
    """Probe media duration using ffmpeg/ffprobe."""
    if ffmpeg is None:
        raise RuntimeError(f"ffmpeg-python unavailable; cannot probe {media_path}")
    
    try:
        probe = ffmpeg.probe(str(media_path))
        return float(probe["format"]["duration"])
    except (ffmpeg.Error, KeyError, ValueError) as exc:
        # Fallback: try stream duration
        try:
            probe = ffmpeg.probe(str(media_path))
            streams = probe.get("streams", [])
            return float(streams[0]["duration"])
        except Exception:
            raise RuntimeError(f"Could not determine duration for {media_path}") from exc

def _make_rational_time(seconds: float, fps: float) -> otio.opentime.RationalTime:
    return otio.opentime.from_seconds(float(seconds), float(fps))

def _make_time_range(start: float, duration: float, fps: float) -> otio.opentime.TimeRange:
    return otio.opentime.TimeRange(
        start_time=_make_rational_time(start, fps),
        duration=_make_rational_time(duration, fps)
    )


def _fit_source_range_to_media(
    start: float,
    duration: float,
    media_duration: float | None,
    fps: float,
) -> tuple[float, float]:
    start = max(float(start), 0.0)
    duration = max(float(duration), 0.1)
    if media_duration is None or media_duration <= 0.0:
        return start, duration

    # Resolve rejects XML source ranges that round past the real media duration.
    frame_guard = 1.0 / max(float(fps), 1.0)
    safe_media_duration = max(float(media_duration) - frame_guard, frame_guard)
    duration = min(duration, safe_media_duration)
    if start + duration > safe_media_duration:
        start = max(0.0, safe_media_duration - duration)
    return start, duration

def _resolve_media_path(asset_id: str, media_map: Optional[Mapping[str, Path]] = None) -> str:
    """
    Resolve asset_id to an absolute file path.
    """
    # 0. Check media_map
    if media_map and asset_id in media_map:
        return str(media_map[asset_id].resolve())

    # 1. Check if absolute
    path = Path(asset_id)
    if path.is_absolute() and path.exists():
        return str(path)
    
    # 2. Check common folders
    # This is a bit hacky for the MVP, ideally this comes from a Catalog service
    search_paths = [
        Path("samples/feixiao test edit/footage/regular"),
        Path("samples/feixiao test edit/footage/slowdown"),
        Path("samples/feixiao test edit/audio"),
        Path("samples/feixiao test edit/music"),
        Path("C:/Footage"), # Legacy fallback
    ]
    
    for base in search_paths:
        candidate = base / asset_id
        if candidate.exists():
            return str(candidate.resolve())
        # Try adding extension if missing
        if not candidate.suffix:
            for ext in [".mp4", ".wav", ".mov", ".mp3"]:
                candidate_ext = base / (asset_id + ext)
                if candidate_ext.exists():
                    return str(candidate_ext.resolve())

    # 3. Return as is (OTIO might fail or Resolve might show "Media Offline")
    return asset_id

def _create_clip(
    spec: Union[ClipSpec, AudioClipSpec], 
    fps: float, 
    track_kind: str = "Video",
    media_map: Optional[Mapping[str, Path]] = None
) -> otio.schema.Clip:
    """Create an OTIO Clip from a ClipSpec."""
    media_path = _resolve_media_path(spec.asset_id, media_map)
    media_url = Path(media_path).as_uri()
    
    # Determine source range
    media_duration = None
    try:
        media_duration = _probe_duration(Path(media_path))
    except Exception:
        media_duration = None

    if spec.source_range:
        src_start = spec.source_range.start
        src_duration = spec.source_range.duration
    else:
        # Default to 0 start using probed (or fallback) duration
        src_start = 0.0
        if media_duration is None:
            src_duration = 5.0
        else:
            src_duration = media_duration

    src_start, src_duration = _fit_source_range_to_media(src_start, src_duration, media_duration, fps)

    if media_duration is None:
        available_duration = src_start + src_duration
    else:
        available_duration = media_duration
            
    # Apply speed if it's a video clip
    speed = getattr(spec, "speed", 1.0)
    # OTIO handles speed via TimeTransform on the Clip, but FCPXML export support varies.
    # For simple FCPXML 1.10, we might just adjust the duration and let Resolve handle it?
    # Actually, OTIO's FCPXML adapter supports speed.
    
    # Create Media Reference
    ref = otio.schema.ExternalReference(
        target_url=media_url,
        available_range=_make_time_range(0, available_duration, fps)
    )
    
    clip = otio.schema.Clip(
        name=Path(media_path).stem,
        media_reference=ref,
        source_range=_make_time_range(src_start, src_duration, fps)
    )
    
    # TODO: Handle speed (requires LinearTimeWarp effect or similar in OTIO)
    
    return clip

def build_otio_timeline_from_model(
    edit: SemanticEdit, 
    media_map: Optional[Mapping[str, Path]] = None
) -> otio.schema.Timeline:
    """Convert a SemanticEdit model into an OTIO Timeline with absolute placement."""

    timeline = otio.schema.Timeline(name=edit.project_name)
    fps = edit.fps

    v1 = otio.schema.Track(name="Primary Video", kind=otio.schema.TrackKind.Video)
    v2 = otio.schema.Track(name="B-Roll", kind=otio.schema.TrackKind.Video)
    a1 = otio.schema.Track(name="Clip Audio", kind=otio.schema.TrackKind.Audio)
    a2 = otio.schema.Track(name="Narration", kind=otio.schema.TrackKind.Audio)
    a3 = otio.schema.Track(name="Music", kind=otio.schema.TrackKind.Audio)

    segments = sorted(
        edit.segments,
        key=lambda seg: seg.narration_span.start if seg.narration_span else 0.0,
    )

    cursor_v1 = 0.0
    cursor_a1 = 0.0
    cursor_a2 = 0.0

    def _append_gap(track: otio.schema.Track, cursor: float, target: float) -> float:
        gap = max(target - cursor, 0.0)
        if gap > 0.0005:
            track.append(otio.schema.Gap(duration=_make_rational_time(gap, fps)))
            cursor += gap
        return cursor

    for segment in segments:
        if not segment.narration_span:
            continue

        seg_start = segment.narration_span.start
        seg_end = seg_start + segment.narration_span.duration

        cursor_v1 = _append_gap(v1, cursor_v1, seg_start)
        cursor_a1 = _append_gap(a1, cursor_a1, seg_start)

        clip_specs: List[ClipSpec] = list(segment.video_clips)
        if not clip_specs and segment.primary_clip:
            clip_specs = [segment.primary_clip]

        clip_cursor = seg_start
        for clip_spec in clip_specs:
            clip = _create_clip(clip_spec, fps, media_map=media_map)
            clip_duration = clip.source_range.duration.to_seconds()

            cursor_v1 = _append_gap(v1, cursor_v1, clip_cursor)
            v1.append(clip)
            cursor_v1 += clip_duration

            cursor_a1 = _append_gap(a1, cursor_a1, clip_cursor)
            a1.append(_create_clip(clip_spec, fps, media_map=media_map))
            cursor_a1 += clip_duration

            clip_cursor += clip_duration

        cursor_v1 = _append_gap(v1, cursor_v1, seg_end)
        cursor_a1 = _append_gap(a1, cursor_a1, seg_end)

        # B-roll placement mirrors old behavior (segment-aligned)
        v2_duration_rt = v2.duration()
        v2_duration = v2_duration_rt.to_seconds() if v2_duration_rt else 0.0
        for broll in segment.broll:
            gap_needed = seg_start - v2_duration
            if gap_needed > 0:
                v2.append(otio.schema.Gap(duration=_make_rational_time(gap_needed, fps)))
            clip = _create_clip(broll, fps, media_map=media_map)
            v2.append(clip)
            v2_duration = v2.duration().to_seconds()

    # Narration master track (Audio 2)
    narration_specs = [seg.narration_audio for seg in segments if seg.narration_audio]
    if narration_specs:
        asset_ids = {spec.asset_id for spec in narration_specs}
        if len(asset_ids) == 1:
            asset_id = narration_specs[0].asset_id
            starts: List[float] = []
            ends: List[float] = []
            for spec in narration_specs:
                if spec.source_range:
                    starts.append(spec.source_range.start)
                    ends.append(spec.source_range.start + spec.source_range.duration)
            if starts and ends:
                vo_start = min(starts)
                vo_end = max(ends)
                cursor_a2 = _append_gap(a2, cursor_a2, vo_start)
                full_spec = AudioClipSpec(
                    asset_id=asset_id,
                    source_range=SourceRange(start=vo_start, duration=vo_end - vo_start),
                )
                a2.append(_create_clip(full_spec, fps, media_map=media_map))
                cursor_a2 += vo_end - vo_start
        else:
            for spec in narration_specs:
                if not spec.source_range:
                    continue
                start = spec.source_range.start
                cursor_a2 = _append_gap(a2, cursor_a2, start)
                a2.append(_create_clip(spec, fps, media_map=media_map))
                cursor_a2 += spec.source_range.duration

    for music in edit.background_music:
        a3.append(_create_clip(music, fps, media_map=media_map))

    stack = otio.schema.Stack(name="Main")
    stack.extend([v1, v2, a1, a2, a3])
    timeline.tracks = stack
    return timeline


def export_fcpxml(
    timeline: otio.schema.Timeline, output_path: Path, adapter_name: str = "fcp_xml"
) -> Path:
    """Write the timeline to FCPXML using the contrib adapter (default: fcp_xml)."""

    output_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        otio.adapters.write_to_file(timeline, str(output_path), adapter_name=adapter_name)
    except otio_exceptions.NotSupportedError as exc:
        fallback = "otio_json"
        fallback_path = output_path.with_suffix(".otio")
        print(
            f"[builder] Adapter '{adapter_name}' unavailable ({exc}); "
            f"falling back to '{fallback}' at {fallback_path}."
        )
        otio.adapters.write_to_file(timeline, str(fallback_path), adapter_name=fallback)
        return fallback_path
    _normalize_xmeml_file_durations(output_path)
    return output_path


def _normalize_xmeml_file_durations(path: Path) -> None:
    """Ensure Resolve does not reject XMEML clips whose file duration exported as zero."""

    try:
        tree = ET.parse(path)
    except ET.ParseError:
        return
    root = tree.getroot()
    if root.tag != "xmeml":
        return

    changed = False
    removed_clipitems = _remove_empty_xmeml_clipitems(root)
    removed_tracks = _remove_empty_xmeml_tracks(root)
    changed = bool(removed_clipitems or removed_tracks)
    for clipitem in root.findall(".//clipitem"):
        file_node = clipitem.find("file")
        if file_node is None:
            continue
        duration_node = file_node.find("duration")
        if duration_node is None:
            duration_node = ET.SubElement(file_node, "duration")
        current_duration = _parse_int(duration_node.text)
        required_duration = max(
            _parse_int(clipitem.findtext("in")),
            _parse_int(clipitem.findtext("out")),
            _parse_int(clipitem.findtext("duration")),
            0,
        )
        if required_duration > current_duration:
            duration_node.text = str(required_duration)
            changed = True

    if changed:
        tree.write(path, encoding="utf-8", xml_declaration=True)


def _remove_empty_xmeml_clipitems(root: ET.Element) -> int:
    removed = 0
    for track in root.findall(".//sequence/media/video/track") + root.findall(".//sequence/media/audio/track"):
        for clipitem in list(track.findall("./clipitem")):
            if len(list(clipitem)) == 0:
                track.remove(clipitem)
                removed += 1
    return removed


def _remove_empty_xmeml_tracks(root: ET.Element) -> int:
    removed = 0
    for parent in root.findall(".//sequence/media/video"):
        for track in list(parent.findall("./track")):
            has_real_clip = any(clip.find("file") is not None for clip in track.findall("./clipitem"))
            if not has_real_clip:
                parent.remove(track)
                removed += 1
    return removed


def _parse_int(value: object) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return 0

def build_fcpxml_from_plan(
    edit: SemanticEdit, 
    output_path: str,
    media_map: Optional[Mapping[str, str]] = None
) -> None:
    """
    Convenience function to build OTIO timeline and export to FCPXML.
    """
    # Convert media_map values to Path if they are strings
    path_map = None
    if media_map:
        path_map = {k: Path(v) for k, v in media_map.items()}
        
    timeline = build_otio_timeline_from_model(edit, media_map=path_map)
    export_fcpxml(timeline, Path(output_path))

