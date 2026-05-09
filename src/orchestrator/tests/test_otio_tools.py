from __future__ import annotations

from pathlib import Path

from orchestrator.tool_bridge import otio_tools


def test_build_and_export_fcpxml(tmp_path: Path):
    clips_dir = tmp_path / "media"
    clips_dir.mkdir()
    video = clips_dir / "clip.mov"
    video.write_text("mock")

    destination = tmp_path / "out" / "timeline.fcpxml"
    path = otio_tools.build_and_export_fcpxml(
        name="Sample",
        fps=60,
        video_segments=[{"clip_id": "clip", "media_path": video, "src_out": 1}],
        destination=destination,
    )
    assert path == destination
    assert destination.exists()
