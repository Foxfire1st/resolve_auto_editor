from __future__ import annotations

from pathlib import Path

import pytest

from gateway.vision.gemini_ingest import pipeline


def test_normalize_event_timestamps_handles_lists_and_slowdown():
    payload = [
        {
            "summary": "Demo",
            "events": [
                {"time_s": 120.0, "timecode": "00:01"},
                {"time_s": "240", "timecode": "00:02"},
            ],
        }
    ]
    pipeline._normalize_event_timestamps(payload, slowdown_factor=5.0)
    events = payload[0]["events"]
    assert events[0]["time_s"] == pytest.approx(0.2)
    assert events[0]["timecode"] == "00:00.20"
    assert events[1]["time_s"] == pytest.approx(0.4)
    assert events[1]["timecode"] == "00:00.40"


def test_normalize_event_timestamps_fills_missing_timecode():
    payload = {
        "events": [
            {"time_s": 65.0, "details": "Action without timecode"},
        ]
    }
    pipeline._normalize_event_timestamps(payload, slowdown_factor=1.0)
    assert payload["events"][0]["timecode"] == "01:05"


def test_normalize_event_timestamps_handles_spans_and_observations():
    payload = {
        "events": [
            {
                "start_s": 50.0,
                "end_timecode": "01:00",
                "anchor_s": "55",
                "observations": [
                    {"time_s": 52.5, "text": "label appears"},
                    {"start_timecode": "00:54", "end_s": 58.0, "text": "hit sequence"},
                ],
            },
        ]
    }

    pipeline._normalize_event_timestamps(payload, slowdown_factor=5.0)

    event = payload["events"][0]
    assert event["start_s"] == pytest.approx(10.0)
    assert event["start_timecode"] == "00:10"
    assert event["end_s"] == pytest.approx(12.0)
    assert event["end_timecode"] == "00:12"
    assert event["anchor_s"] == pytest.approx(11.0)
    assert event["anchor_timecode"] == "00:11"
    assert event["observations"][0]["time_s"] == pytest.approx(10.5)
    assert event["observations"][0]["timecode"] == "00:10.50"
    assert event["observations"][1]["start_s"] == pytest.approx(10.8)
    assert event["observations"][1]["start_timecode"] == "00:10.80"
    assert event["observations"][1]["end_s"] == pytest.approx(11.6)
    assert event["observations"][1]["end_timecode"] == "00:11.60"


def test_collect_batch_requests_discovers_videos_and_skips_existing(tmp_path: Path):
    footage_root = tmp_path / "footage"
    regular = footage_root / "regular"
    regular_meta = regular / "meta"
    slowdown = footage_root / "slowdown"
    slowdown_meta = slowdown / "meta"
    regular_meta.mkdir(parents=True)
    slowdown_meta.mkdir(parents=True)

    clip_a = regular / "clip_a.mp4"
    clip_a.write_bytes(b"0")
    clip_skip = regular / "clip_skip.mp4"
    clip_skip.write_bytes(b"0")
    (regular_meta / "clip_skip_gemini.json").write_text("{}", encoding="utf-8")
    (regular / "notes.txt").write_text("ignore", encoding="utf-8")

    combat_clip = slowdown / "combat01.mkv"
    combat_clip.write_bytes(b"0")

    tasks = pipeline.collect_batch_requests(
        footage_root,
        folder_slow_map={"regular": 1.0, "slowdown": 5.0},
        allowed_extensions=(".mp4", ".mkv"),
        overwrite=False,
    )

    assert [task.video_path.name for task in tasks] == ["clip_a.mp4", "combat01.mkv"]
    assert tasks[0].output_path == regular_meta / "clip_a_gemini.json"
    assert tasks[0].slowdown == 1.0
    assert tasks[1].output_path == slowdown_meta / "combat01_gemini.json"
    assert tasks[1].slowdown == 5.0


def test_collect_batch_requests_overwrite_requeues_existing(tmp_path: Path):
    footage_root = tmp_path / "footage"
    regular = footage_root / "regular"
    meta = regular / "meta"
    meta.mkdir(parents=True)

    clip = regular / "redo.mp4"
    clip.write_bytes(b"0")
    (meta / "redo_gemini.json").write_text("old", encoding="utf-8")

    tasks = pipeline.collect_batch_requests(
        footage_root,
        folder_slow_map={"regular": 0.5},
        overwrite=True,
    )

    assert len(tasks) == 1
    assert tasks[0].output_path == meta / "redo_gemini.json"
    # slowdown factor is coerced to at least 1.0 even if value < 1
    assert tasks[0].slowdown == 1.0


def test_collect_batch_requests_clip_filter(tmp_path: Path):
    root = tmp_path / "footage"
    reg = root / "regular"
    slow = root / "slowdown"
    (reg / "meta").mkdir(parents=True)
    (slow / "meta").mkdir(parents=True)

    (reg / "clip_a.mp4").write_bytes(b"0")
    (reg / "clip_b.mp4").write_bytes(b"0")
    (slow / "fight.mov").write_bytes(b"0")

    tasks = pipeline.collect_batch_requests(
        root,
        folder_slow_map={"regular": 1.0, "slowdown": 5.0},
        clip_name_filter=["clip_b", "slowdown/fight.mov"],
    )

    names = [task.video_path.name for task in tasks]
    assert names == ["clip_b.mp4", "fight.mov"]


def test_attempt_repair_json_handles_truncated_payload():
    broken = '{"events": [{"timecode": "00:01", "labels": ["CRIT Hit"\n'
    repaired = pipeline._attempt_repair_json(broken)
    assert isinstance(repaired, dict)
    assert repaired["events"][0]["labels"] == ["CRIT Hit"]
