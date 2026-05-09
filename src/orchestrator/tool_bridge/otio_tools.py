"""High-level OTIO helpers exposed as orchestrator tools."""

from __future__ import annotations

from pathlib import Path
from typing import Mapping, Sequence

from gateway.otio_fcpxml import builder
from orchestrator.models import AudioClipSpec, ClipSpec, EditSegment, SemanticEdit, SourceRange, TimeSpan

SegmentSpec = Mapping[str, object]


def build_and_export_semantic_edit(
    edit: SemanticEdit,
    destination: Path,
    adapter: str = "fcp_xml",
) -> Path:
    """Build an OTIO timeline from a SemanticEdit model and export to FCPXML."""
    timeline = builder.build_otio_timeline_from_model(edit)
    return builder.export_fcpxml(timeline, destination, adapter_name=adapter)


def build_and_export_fcpxml(
    *,
    name: str,
    fps: int,
    video_segments: Sequence[SegmentSpec],
    destination: Path,
    voice_over: SegmentSpec | None = None,
    base_dir: Path | None = None,
    adapter: str = "fcp_xml",
) -> Path:
    plan = build_timeline_plan(
        name=name,
        fps=fps,
        video_segments=video_segments,
        voice_over=voice_over,
        base_dir=base_dir,
    )
    return export_fcpxml_from_plan(plan, adapter=adapter, destination=destination)


def build_timeline_plan(
    *,
    name: str,
    fps: int,
    video_segments: Sequence[SegmentSpec],
    voice_over: SegmentSpec | None = None,
    base_dir: Path | None = None,
) -> SemanticEdit:
    """Convert JSON-friendly segment specs into the shared SemanticEdit model."""

    segments: list[EditSegment] = []
    cursor = 0.0
    for index, spec in enumerate(video_segments, start=1):
        source_range = _source_range_from_spec(spec)
        timeline_start = _float_value(spec, "timeline_start", "start", default=cursor)
        clip = ClipSpec(
            asset_id=_asset_id_from_spec(spec, base_dir=base_dir),
            source_range=source_range,
            description=str(spec.get("description") or ""),
        )
        narration_audio = None
        if voice_over is not None:
            narration_audio = AudioClipSpec(
                asset_id=_asset_id_from_spec(voice_over, base_dir=base_dir),
                source_range=SourceRange(
                    start=timeline_start,
                    duration=source_range.duration,
                ),
            )
        segment_id = str(spec.get("id") or spec.get("clip_id") or f"segment-{index:03d}")
        segments.append(
            EditSegment(
                id=segment_id,
                shot_id=segment_id,
                narration_span=TimeSpan(
                    start=timeline_start,
                    duration=source_range.duration,
                ),
                narration_audio=narration_audio,
                primary_clip=clip,
                video_clips=[clip],
            )
        )
        cursor = timeline_start + source_range.duration
    return SemanticEdit(project_name=name, fps=float(fps), segments=segments)


def export_fcpxml_from_plan(
    plan: SemanticEdit,
    *,
    adapter: str = "fcp_xml",
    destination: Path,
) -> Path:
    """Export a SemanticEdit plan through the existing OTIO/FCPXML builder."""

    timeline = builder.build_otio_timeline_from_model(plan)
    return builder.export_fcpxml(timeline, destination, adapter_name=adapter)


def _source_range_from_spec(spec: SegmentSpec) -> SourceRange:
    src_in = _float_value(spec, "src_in", "source_start", default=0.0)
    if "src_out" in spec:
        duration = max(_float_value(spec, "src_out", default=src_in) - src_in, 0.1)
    else:
        duration = _float_value(spec, "duration", "source_duration", default=5.0)
    return SourceRange(start=src_in, duration=duration)


def _asset_id_from_spec(spec: SegmentSpec, *, base_dir: Path | None = None) -> str:
    value = spec.get("media_path") or spec.get("asset_id") or spec.get("clip_id")
    if value is None:
        raise ValueError("Segment spec must include media_path, asset_id, or clip_id.")
    path = Path(str(value))
    if base_dir is not None and not path.is_absolute() and "media_path" in spec:
        path = base_dir / path
    return str(path.resolve()) if path.exists() else str(path)


def _float_value(spec: SegmentSpec, *keys: str, default: float) -> float:
    for key in keys:
        if key not in spec:
            continue
        value = spec[key]
        if value is None:
            continue
        return float(value)
    return default
