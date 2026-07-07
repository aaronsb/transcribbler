"""Turn-aware attribution (ADR-0027, Decision 2a).

Failure B: a single ASR segment can span a diarization speaker change, and
whole-segment attribution buckets it under one speaker (e.g. the 06:46 case where
one line was Jonathan then Val). Given a segment and the speaker turns overlapping
it, split the segment's text at the turn boundaries.

The ASR core exposes only segment-level times, not word times (ADR-0015), so the
text is divided between pieces by **word count in proportion to each piece's
duration** — approximate, but it stops the gross whole-segment misattribution.
Precise word-level splitting is the deferred 2b upgrade (word timestamps).

Pure functions, no I/O: unit-testable without audio.
"""

from __future__ import annotations

from dataclasses import dataclass

# Ignore incidental sub-second overlaps when deciding a segment is multi-speaker;
# a turn must own at least this many seconds of the segment to claim a slice.
_MIN_SLICE_S = 0.30


@dataclass(frozen=True)
class Attributed:
    start: float
    end: float
    speaker: str
    text: str


def _overlap(a0: float, a1: float, b0: float, b1: float) -> float:
    return max(0.0, min(a1, b1) - max(a0, b0))


def _cover(
    seg_start: float, seg_end: float, turns: list[tuple[float, float, str]]
) -> list[tuple[float, float, str]]:
    """Partition ``[seg_start, seg_end]`` into contiguous single-speaker runs.

    Each elementary interval between boundary points is attributed to the turn
    with the most overlap on it; intervals covered by no turn inherit the nearest
    neighbour's speaker; adjacent same-speaker runs are merged. Returns runs in
    order, together covering the whole segment. Empty if no turn overlaps at all.
    """
    slices = [
        (max(seg_start, t0), min(seg_end, t1), spk)
        for t0, t1, spk in turns
        if min(seg_end, t1) - max(seg_start, t0) > 0.0
    ]
    if not slices:
        return []

    points = sorted({seg_start, seg_end, *(p for lo, hi, _ in slices for p in (lo, hi))})
    cells: list[tuple[float, float, str | None]] = []
    for lo, hi in zip(points, points[1:]):
        if hi <= lo:
            continue
        best_spk, best_ov = None, 0.0
        for s0, s1, spk in slices:
            ov = _overlap(lo, hi, s0, s1)
            if ov > best_ov:
                best_spk, best_ov = spk, ov
        cells.append((lo, hi, best_spk))

    # fill gaps (speaker None) with the nearest attributed neighbour
    for i, (lo, hi, spk) in enumerate(cells):
        if spk is not None:
            continue
        left = next((cells[j][2] for j in range(i - 1, -1, -1) if cells[j][2]), None)
        right = next((cells[j][2] for j in range(i + 1, len(cells)) if cells[j][2]), None)
        cells[i] = (lo, hi, left or right)

    runs: list[tuple[float, float, str]] = []
    for lo, hi, spk in cells:
        if spk is None:
            continue
        if runs and runs[-1][2] == spk:
            runs[-1] = (runs[-1][0], hi, spk)
        else:
            runs.append((lo, hi, spk))
    return runs


def split_segment_by_turns(
    seg_start: float,
    seg_end: float,
    text: str,
    turns: list[tuple[float, float, str]],
    *,
    default: str,
) -> list[Attributed]:
    """Split one ASR segment into single-speaker pieces along turn boundaries.

    ``turns`` is ``[(start, end, speaker)]`` in absolute seconds, any order. If the
    segment is dominated by one speaker (or overlaps none — then ``default``), it is
    returned whole. Otherwise the text is divided between the speaker runs by word
    count proportional to each run's duration; runs allotted no words are dropped.
    """
    words = text.split()
    if not words:
        return []

    runs = _cover(seg_start, seg_end, turns)
    if not runs:
        return [Attributed(seg_start, seg_end, default, text)]

    # collapse runs shorter than the slice floor into the whole-segment case if
    # they leave a single meaningful speaker; otherwise keep the multi-run split
    long_runs = [r for r in runs if (r[1] - r[0]) >= _MIN_SLICE_S]
    speakers = {r[2] for r in long_runs} or {runs[0][2]}
    if len(speakers) <= 1:
        return [Attributed(seg_start, seg_end, next(iter(speakers)), text)]

    total = seg_end - seg_start
    n = len(words)
    # proportional word counts, largest-remainder so the counts sum to n exactly
    raw = [((hi - lo) / total * n, i) for i, (lo, hi, _) in enumerate(runs)]
    counts = [int(x) for x, _ in raw]
    leftover = n - sum(counts)
    for _, i in sorted(raw, key=lambda r: r[0] - int(r[0]), reverse=True)[:leftover]:
        counts[i] += 1

    out: list[Attributed] = []
    cur = 0
    for (lo, hi, spk), c in zip(runs, counts):
        if c <= 0:
            continue
        piece = " ".join(words[cur : cur + c])
        cur += c
        out.append(Attributed(lo, hi, spk, piece))
    return out
