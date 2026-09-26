"""Occlusion-aware subtitle placement (top / bottom regions).

Pure Python (no web dependencies) so it can be unit-tested in isolation.

The request carries *occlusion intervals* on the video timeline, each
blocking one region (``top`` or ``bottom``) of the frame.  Whenever a
subtitle's time span intersects an occlusion interval with positive
length, that region is unavailable for the subtitle; every subtitle
needs at least one region that stays available for its whole duration
(touching an occlusion's endpoint is fine — a zero-length intersection
does not block a region).  Occlusion intervals may be given in any
order and may overlap; a moment where both regions are occluded simply
cannot be spanned by any subtitle.

Segmentation and placement are optimised jointly: the usual character,
duration, two-line typesetting and natural-boundary rules and costs
apply unchanged, and each pair of adjacent subtitles placed in
*different* regions adds :data:`REGION_SWITCH_PENALTY` (1 point) to the
total cost.  The lowest total cost wins; ties are broken in favour of
fewer subtitles, then by comparing the position sequence
lexicographically with ``bottom`` preferred over ``top``.  When no
segmentation can give every subtitle an available region the caller
gets a :class:`SegmentationError` (HTTP 422).

The dynamic program extends the plain segmenter with the region of the
current subtitle: ``dp[i][r]`` is the best ``(cost, count, positions)``
for covering words ``i..n-1`` with the first subtitle placed in region
``r``.  All candidates inside one state share the leading region ``r``,
so position-sequence comparisons can be done on the successor states'
sequences directly.
"""

from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass
from typing import Protocol, Sequence

from .config import CROSSED_BOUNDARY_PENALTY, REGION_SWITCH_PENALTY
from .segmenter import (
    SegmentResult,
    SegmentationError,
    WordLike,
    _best_layout,
    _needs_space,
    ends_with_sentence_punctuation,
    join_word_texts,
)

# Region indices; ``bottom`` sorts before ``top`` so plain tuple comparison
# implements the "bottom preferred over top" position-sequence tie-break.
REGIONS = ("bottom", "top")
BOTTOM, TOP = 0, 1


class OcclusionLike(Protocol):
    start_ms: int
    end_ms: int
    region: str


@dataclass
class PlacedSegment(SegmentResult):
    position: str  # "bottom" or "top"


@dataclass
class PlacementResult:
    segments: list[PlacedSegment]
    total_cost: float


def _validate_occlusions(
    occlusions: Sequence[OcclusionLike],
) -> tuple[tuple[int, int, str], ...]:
    """Check the occlusion intervals, return ``(start, end, region)`` triples.

    Each interval must satisfy ``start < end`` and name a known region.
    Unlike deletion intervals, occlusions need not be sorted and may
    overlap.  Raises :class:`SegmentationError` on illegal input.
    """
    checked: list[tuple[int, int, str]] = []
    for occ in occlusions:
        if occ.region not in REGIONS:
            raise SegmentationError(
                f"occlusion region {occ.region!r} is not supported; region "
                f"must be one of {list(REGIONS)}"
            )
        if occ.end_ms <= occ.start_ms:
            raise SegmentationError(
                f"occlusion interval [{occ.start_ms}, {occ.end_ms}] must "
                "satisfy start_ms < end_ms"
            )
        checked.append((occ.start_ms, occ.end_ms, occ.region))
    return tuple(checked)


class _RegionTimeline:
    """One region's occlusion intervals, answering availability queries.

    Intervals are sorted by start; ``_max_end[k]`` is the furthest end
    among the first ``k + 1`` intervals, so "does any interval intersect
    ``[start, end)`` with positive length" is a single bisect.
    """

    def __init__(self, intervals: list[tuple[int, int]]) -> None:
        intervals.sort()
        self._starts = [s for s, _ in intervals]
        self._max_end: list[int] = []
        furthest = 0
        for _, e in intervals:
            furthest = max(furthest, e)
            self._max_end.append(furthest)

    def is_available(self, start_ms: int, end_ms: int) -> bool:
        """True when no occluded interval intersects ``[start_ms, end_ms)``.

        Intersection must have positive length: an occlusion ending
        exactly at ``start_ms`` or starting exactly at ``end_ms`` does
        not block the region.
        """
        idx = bisect_left(self._starts, end_ms)  # intervals with start < end
        return idx == 0 or self._max_end[idx - 1] <= start_ms


def segment_with_placement(
    words: Sequence[WordLike],
    occlusions: Sequence[OcclusionLike],
    *,
    max_chars: int,
    min_duration_ms: int,
    max_duration_ms: int,
    target_duration_ms: int,
    pause_threshold_ms: int,
    max_line_chars: int | None = None,
) -> PlacementResult:
    """Segment the transcript and place every subtitle top or bottom.

    ``words`` is the time-ordered, non-overlapping word-level transcript;
    ``occlusions`` holds the occlusion intervals on the same timeline.
    Returns the jointly optimal segmentation + placement (see the module
    docstring for the cost and tie-break rules).  Raises
    :class:`SegmentationError` on illegal input or when no segmentation
    can keep one region available for every subtitle.
    """
    n = len(words)
    if n == 0:
        raise SegmentationError("words must contain at least one word")
    occ = _validate_occlusions(occlusions)

    # ---- input validation (same rules as the plain segmenter) -------------
    for idx, w in enumerate(words):
        if not w.text or not w.text.strip():
            raise SegmentationError(f"word {idx} has empty text")
        if w.end_ms <= w.start_ms:
            raise SegmentationError(
                f"word {idx} ({w.text!r}) must satisfy end_ms > start_ms "
                f"(got {w.start_ms}..{w.end_ms})"
            )
        if len(w.text) > max_chars:
            raise SegmentationError(
                f"word {idx} ({w.text!r}) is {len(w.text)} characters and can "
                f"never fit within max_chars={max_chars}"
            )
        if max_line_chars is not None and len(w.text) > max_line_chars:
            raise SegmentationError(
                f"word {idx} ({w.text!r}) is {len(w.text)} characters and can "
                f"never fit within max_line_chars={max_line_chars}"
            )
        word_dur = w.end_ms - w.start_ms
        if word_dur > max_duration_ms:
            raise SegmentationError(
                f"word {idx} ({w.text!r}) lasts {word_dur} ms and can never "
                f"fit within max_duration_ms={max_duration_ms}"
            )
    for i in range(1, n):
        if words[i].start_ms < words[i - 1].end_ms:
            raise SegmentationError(
                f"words must be sorted by time and non-overlapping: word {i} "
                f"starts at {words[i].start_ms} ms but word {i - 1} ends at "
                f"{words[i - 1].end_ms} ms"
            )

    # ---- natural break points, joining spaces, cumulative lengths ---------
    natural = [False] * max(n - 1, 0)
    for k in range(n - 1):
        gap = words[k + 1].start_ms - words[k].end_ms
        natural[k] = (
            gap >= pause_threshold_ms
            or ends_with_sentence_punctuation(words[k].text)
        )
    # nat_prefix[k] = number of natural boundaries among indices 0..k-1, so
    # the count crossed by a segment spanning words i..j is prefix[j]-prefix[i].
    nat_prefix = [0] * n
    for k in range(n - 1):
        nat_prefix[k + 1] = nat_prefix[k] + (1 if natural[k] else 0)

    space_before = [0] * n
    for k in range(1, n):
        space_before[k] = 1 if _needs_space(words[k - 1].text, words[k].text) else 0

    # cum_len[k+1] = length of the joined words 0..k with each word's
    # joining space stored in front of it (same layout as the segmenter).
    cum_len = [0] * (n + 1)
    for k in range(n):
        cum_len[k + 1] = cum_len[k] + len(words[k].text) + space_before[k]

    # ---- occlusion availability per region --------------------------------
    timelines = (
        _RegionTimeline([(s, e) for s, e, r in occ if r == "bottom"]),
        _RegionTimeline([(s, e) for s, e, r in occ if r == "top"]),
    )

    # ---- dynamic programming over (word position, region) -----------------
    INF = float("inf")
    EPS = 1e-9
    dp_cost = [[INF] * (n + 1) for _ in REGIONS]
    dp_count = [[0] * (n + 1) for _ in REGIONS]
    # dp_seq[r][i] = position sequence (region indices) of the best plan
    # covering words i..n-1 with the first subtitle in region r.
    dp_seq: list[list[tuple[int, ...]]] = [[()] * (n + 1) for _ in REGIONS]
    nxt_end = [[-1] * (n + 1) for _ in REGIONS]
    nxt_break = [[-1] * (n + 1) for _ in REGIONS]
    nxt_region = [[-1] * (n + 1) for _ in REGIONS]
    dp_cost[BOTTOM][n] = dp_cost[TOP][n] = 0.0
    target_s = target_duration_ms / 1000.0

    for i in range(n - 1, -1, -1):
        # Best candidate per region for the subtitle starting at word i;
        # kept in locals during the scan so the position-sequence tuple is
        # concatenated exactly once per state when the scan finishes.
        cand_cost = [INF, INF]
        cand_count = [0, 0]
        cand_succ_seq: list[tuple[int, ...]] = [(), ()]
        cand_end = [-1, -1]
        cand_break = [-1, -1]
        cand_succ = [-1, -1]
        chars = 0
        for j in range(i, n):
            chars += len(words[j].text) + (space_before[j] if j > i else 0)
            if chars > max_chars:
                break  # only grows as j grows
            start_ms = words[i].start_ms
            end_ms = words[j].end_ms
            dur_ms = end_ms - start_ms
            if dur_ms > max_duration_ms:
                break  # only grows as j grows
            if max_line_chars is not None:
                layout = _best_layout(i, j, cum_len, space_before, max_line_chars)
                if layout is None:
                    break  # wider spans starting at i stay unlayoutable
                break_word, line_cost = layout
            else:
                break_word, line_cost = -1, 0.0
            if dur_ms < min_duration_ms:
                continue
            crossed = nat_prefix[j] - nat_prefix[i]
            dur_s = dur_ms / 1000.0
            seg_cost = (
                (dur_s - target_s) ** 2
                + CROSSED_BOUNDARY_PENALTY * crossed
                + line_cost
            )
            for region in (BOTTOM, TOP):
                if not timelines[region].is_available(start_ms, end_ms):
                    continue
                for succ in (BOTTOM, TOP):
                    if dp_cost[succ][j + 1] == INF:
                        continue
                    cost = seg_cost + dp_cost[succ][j + 1]
                    if succ != region:
                        cost += REGION_SWITCH_PENALTY
                    count = 1 + dp_count[succ][j + 1]
                    succ_seq = dp_seq[succ][j + 1]
                    if cost < cand_cost[region] - EPS or (
                        abs(cost - cand_cost[region]) <= EPS
                        and (count, succ_seq)
                        < (cand_count[region], cand_succ_seq[region])
                    ):
                        cand_cost[region] = cost
                        cand_count[region] = count
                        cand_succ_seq[region] = succ_seq
                        cand_end[region] = j
                        cand_break[region] = break_word
                        cand_succ[region] = succ
        for region in (BOTTOM, TOP):
            if cand_end[region] < 0:
                continue  # no legal subtitle starting at i in this region
            dp_cost[region][i] = cand_cost[region]
            dp_count[region][i] = cand_count[region]
            dp_seq[region][i] = (region,) + cand_succ_seq[region]
            nxt_end[region][i] = cand_end[region]
            nxt_break[region][i] = cand_break[region]
            nxt_region[region][i] = cand_succ[region]

    # ---- pick the starting region (same cost/count/sequence order) --------
    start_region = BOTTOM
    if dp_cost[TOP][0] < dp_cost[BOTTOM][0] - EPS or (
        abs(dp_cost[TOP][0] - dp_cost[BOTTOM][0]) <= EPS
        and (dp_count[TOP][0], dp_seq[TOP][0])
        < (dp_count[BOTTOM][0], dp_seq[BOTTOM][0])
    ):
        start_region = TOP

    if dp_cost[start_region][0] == INF:
        layout_note = (
            f", max_line_chars={max_line_chars}" if max_line_chars is not None else ""
        )
        raise SegmentationError(
            "no legal segmentation gives every subtitle an available region "
            f"under the given constraints (max_chars={max_chars}"
            f"{layout_note}, min_duration_ms={min_duration_ms}, "
            f"max_duration_ms={max_duration_ms}); every subtitle — including "
            "the last one — must satisfy the duration limits and keep one "
            "region (top or bottom) free of occlusions for its whole time "
            "span, so try relaxing min_duration_ms or the other limits, or "
            "narrowing the occlusion intervals"
        )

    # ---- reconstruct the optimal path --------------------------------------
    segments: list[PlacedSegment] = []
    i = 0
    region = start_region
    while i < n:
        j = nxt_end[region][i]
        texts = [w.text for w in words[i : j + 1]]
        bw = nxt_break[region][i]
        if bw > 0:
            text = (
                join_word_texts(texts[: bw - i])
                + "\n"
                + join_word_texts(texts[bw - i :])
            )
        else:
            text = join_word_texts(texts)
        segments.append(
            PlacedSegment(
                index=len(segments),
                text=text,
                start_ms=words[i].start_ms,
                end_ms=words[j].end_ms,
                duration_ms=words[j].end_ms - words[i].start_ms,
                word_start=i,
                word_end=j,
                crossed_boundaries=nat_prefix[j] - nat_prefix[i],
                position=REGIONS[region],
            )
        )
        region = nxt_region[region][i]
        i = j + 1

    return PlacementResult(segments=segments, total_cost=dp_cost[start_region][0])
