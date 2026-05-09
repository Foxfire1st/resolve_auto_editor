from __future__ import annotations

from fractions import Fraction
from pathlib import Path

import pytest

from src.orchestrator import models
from gateway.otio_fcpxml import builder


def test_build_plan_from_dict(tmp_path: Path):
    clip_dir = tmp_path / "clips"
    clip_dir.mkdir()
    dummy_clip = clip_dir / "a.mov"
    dummy_clip.write_text("placeholder")

    plan = builder.build_plan(
        name="Demo",
        fps=30,
        video_segments=[{"clip_id": "A", "media_path": dummy_clip, "src_out": 1}]
    )

    assert plan.name == "Demo"
    assert plan.fps == Fraction(30, 1)
    assert plan.video_segments[0].clip_id == "A"


def test_build_plan_requires_segments():
    with pytest.raises(ValueError):
        builder.build_plan(name="Empty", fps=24, video_segments=[])


def test_normalize_xmeml_file_durations_extends_zero_file_duration(tmp_path: Path):
    xml_path = tmp_path / "timeline.fcpxml"
    xml_path.write_text(
        """<?xml version="1.0"?>
<xmeml version="4">
  <sequence>
    <media>
      <video>
        <track>
          <clipitem id="clipitem-1">
            <file id="file-1"><duration>0</duration></file>
            <duration>30</duration>
            <in>10</in>
            <out>40</out>
          </clipitem>
        </track>
      </video>
    </media>
  </sequence>
</xmeml>
""",
        encoding="utf-8",
    )

    builder._normalize_xmeml_file_durations(xml_path)

    assert "<duration>40</duration>" in xml_path.read_text(encoding="utf-8")


def test_normalize_xmeml_file_durations_removes_empty_clipitems_and_tracks(tmp_path: Path):
    xml_path = tmp_path / "timeline.fcpxml"
    xml_path.write_text(
        """<?xml version="1.0"?>
<xmeml version="4">
  <sequence>
    <media>
      <video>
        <track>
          <clipitem id="clipitem-1">
            <file id="file-1"><duration>30</duration></file>
            <duration>30</duration><start>0</start><end>30</end><in>0</in><out>30</out>
          </clipitem>
          <clipitem id="clipitem-1" />
        </track>
        <track />
      </video>
      <audio>
        <track><clipitem id="clipitem-1" /></track>
      </audio>
    </media>
  </sequence>
</xmeml>
""",
        encoding="utf-8",
    )

    builder._normalize_xmeml_file_durations(xml_path)

    text = xml_path.read_text(encoding="utf-8")
    assert '<clipitem id="clipitem-1" />' not in text
    assert "<audio>\n        <track />\n      </audio>" in text


def test_create_clip_shifts_out_of_bounds_source_range_inside_media(tmp_path: Path, monkeypatch) -> None:
    media = tmp_path / "clip.mp4"
    media.write_text("placeholder")
    monkeypatch.setattr(builder, "_probe_duration", lambda path: 10.0)

    clip = builder._create_clip(
        models.ClipSpec(
            asset_id=str(media),
            source_range=models.SourceRange(start=20.0, duration=3.0),
        ),
        fps=30.0,
    )

    start = clip.source_range.start_time.to_seconds()
    duration = clip.source_range.duration.to_seconds()
    assert start == pytest.approx(10.0 - (1.0 / 30.0) - 3.0)
    assert duration == pytest.approx(3.0)
    assert start + duration < 10.0
