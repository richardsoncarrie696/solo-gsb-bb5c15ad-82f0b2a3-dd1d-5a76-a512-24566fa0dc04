"""WebVTT export with cue line positions for top/bottom placement."""

from __future__ import annotations

from typing import Sequence

from .place import PlacedSegment

# Cue ``line`` setting that parks the cue at the very top / bottom of the
# frame: with a percentage, 0% aligns the cue's first line with the top of
# the video and 100% its last line with the bottom.
_LINE_SETTING = {"top": "0%", "bottom": "100%"}


def format_vtt_timestamp(ms: int) -> str:
    """Milliseconds -> ``HH:MM:SS.mmm``."""
    ms = max(0, int(ms))
    hours, rem = divmod(ms, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    seconds, millis = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{millis:03d}"


def to_vtt(segments: Sequence[PlacedSegment]) -> str:
    blocks = []
    for seg in segments:
        blocks.append(
            f"{format_vtt_timestamp(seg.start_ms)} --> "
            f"{format_vtt_timestamp(seg.end_ms)} "
            f"line:{_LINE_SETTING[seg.position]}\n"
            f"{seg.text}"
        )
    return "WEBVTT\n\n" + "\n\n".join(blocks) + "\n"
