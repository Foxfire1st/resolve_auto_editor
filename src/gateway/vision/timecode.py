"""Utility helpers for converting between seconds and HH:MM:SS-style timecodes."""

from __future__ import annotations

DEFAULT_TIMECODE_FPS = 60.0


def timecode_to_seconds(
    timecode: str | None,
    *,
    frame_rate: float = DEFAULT_TIMECODE_FPS,
) -> float | None:
    """Convert a HH:MM:SS or HH:MM:SS:FF timecode into seconds."""
    if not timecode:
        return None
    text = str(timecode).strip()
    if not text:
        return None
    parts = text.split(":")
    try:
        values = [float(part) for part in parts]
    except ValueError:
        return None

    if len(values) == 4:
        hours, minutes, seconds, frames = values
        fps = frame_rate if frame_rate > 0 else DEFAULT_TIMECODE_FPS
        return hours * 3600 + minutes * 60 + seconds + frames / fps
    if len(values) == 3:
        hours, minutes, seconds = values
        return hours * 3600 + minutes * 60 + seconds
    if len(values) == 2:
        minutes, seconds = values
        return minutes * 60 + seconds
    if len(values) == 1:
        return values[0]
    # Unsupported format
    return None


def seconds_to_timecode(
    seconds: float,
    *,
    drop_leading_hours: bool = True,
) -> str:
    """Convert seconds into a HH:MM:SS style string."""
    seconds = max(float(seconds), 0.0)
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = seconds % 60
    fractional = abs(secs - round(secs)) > 1e-3
    if hours == 0 and drop_leading_hours:
        if fractional:
            return f"{minutes:02d}:{secs:05.2f}"
        return f"{minutes:02d}:{int(round(secs)):02d}"
    if fractional:
        return f"{hours:02d}:{minutes:02d}:{secs:05.2f}"
    return f"{hours:02d}:{minutes:02d}:{int(round(secs)):02d}"
