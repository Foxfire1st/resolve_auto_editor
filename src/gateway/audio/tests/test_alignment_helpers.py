from pathlib import Path

import pytest

from gateway.audio.vo_script_alignment.alignment_helpers import (
    AlignmentSpan,
    find_span_by_id,
    iter_spans,
    iter_time_windows,
    load_alignment,
    spans_in_window,
)


@pytest.fixture()
def sample_alignment(tmp_path: Path) -> Path:
    payload = {
        "aligned": [
            {
                "id": "L001",
                "text": "Intro",
                "t0": 0.0,
                "t1": 5.0,
                "source": "script",
                "words": [],
            },
            {
                "id": "L002",
                "text": "Action starts",
                "t0": 5.0,
                "t1": 10.0,
                "source": "script",
                "words": [],
            },
            {
                "id": "L003",
                "text": "Outro",
                "t0": 10.0,
                "t1": 12.5,
                "source": "audio",
                "words": [],
            },
        ]
    }
    path = tmp_path / "alignment.json"
    path.write_text(__import__("json").dumps(payload), encoding="utf-8")
    return path


def test_iter_spans_preserves_order(sample_alignment: Path) -> None:
    spans = list(iter_spans(sample_alignment))
    assert [span.id for span in spans] == ["L001", "L002", "L003"]
    assert isinstance(spans[0], AlignmentSpan)


def test_find_span_by_id(sample_alignment: Path) -> None:
    span = find_span_by_id(sample_alignment, "L002")
    assert span is not None and span.text == "Action starts"
    assert find_span_by_id(sample_alignment, "missing") is None


def test_spans_in_window_inclusive(sample_alignment: Path) -> None:
    spans = spans_in_window(sample_alignment, 4.0, 11.0)
    assert [span.id for span in spans] == ["L001", "L002", "L003"]
    strict = spans_in_window(sample_alignment, 4.0, 11.0, include_partial=False)
    assert [span.id for span in strict] == ["L002"]


def test_iter_time_windows(sample_alignment: Path) -> None:
    windows = list(iter_time_windows(sample_alignment, window_seconds=6.0, stride_seconds=5.0))
    assert len(windows) >= 2
    first_start, first_end, first_spans = windows[0]
    assert pytest.approx(first_start, rel=1e-6) == 0.0
    assert pytest.approx(first_end, rel=1e-6) == 6.0
    assert [span.id for span in first_spans] == ["L001", "L002"]


def test_load_alignment_round_trip(sample_alignment: Path) -> None:
    payload = load_alignment(sample_alignment)
    assert "aligned" in payload
