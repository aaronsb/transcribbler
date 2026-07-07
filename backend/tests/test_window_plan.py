"""Unit tests for overlapping-window tiling math (ADR-0027, capture._window_plan)."""

from __future__ import annotations

from transcribbler.capture import _window_plan

SEG = 20  # half = 10


def _plan(k, *, contiguous, terminal=False):
    return _window_plan(k, contiguous=contiguous, terminal=terminal, segment_s=SEG)


def test_cold_start_window_is_single_chunk_from_zero():
    chunks, win_start, emit_lo, emit_hi, shared = _plan(0, contiguous=False)
    assert chunks == [0]
    assert (win_start, emit_lo, emit_hi) == (0.0, 0.0, 10.0)
    assert shared == (0.0, 0.0)


def test_interior_window_spans_two_chunks_and_centers_on_the_boundary():
    chunks, win_start, emit_lo, emit_hi, shared = _plan(2, contiguous=True)
    assert chunks == [1, 2]
    assert win_start == 20.0
    # emit region is the middle seg, centered on the k*seg=40 boundary
    assert (emit_lo, emit_hi) == (30.0, 50.0)
    assert (emit_lo + emit_hi) / 2 == 40.0
    # shared span with the previous window is chunk k-1
    assert shared == (20.0, 40.0)


def test_emit_regions_tile_without_gap_or_overlap():
    # cold window 0, then contiguous 1,2,3 — emit_hi of each == emit_lo of the next
    _, _, lo0, hi0, _ = _plan(0, contiguous=False)
    _, _, lo1, hi1, _ = _plan(1, contiguous=True)
    _, _, lo2, hi2, _ = _plan(2, contiguous=True)
    _, _, lo3, hi3, _ = _plan(3, contiguous=True)
    assert lo0 == 0.0
    assert hi0 == lo1
    assert hi1 == lo2
    assert hi2 == lo3
    # each interior emit region is exactly one segment wide
    assert hi1 - lo1 == SEG
    assert hi2 - lo2 == SEG


def test_terminal_window_extends_emit_to_end_of_audio():
    chunks, win_start, emit_lo, emit_hi, _ = _plan(2, contiguous=True, terminal=True)
    assert chunks == [1, 2]
    assert emit_lo == 30.0
    assert emit_hi == 60.0  # (k+1)*seg — to the end of chunk k's audio


def test_fresh_window_after_a_gap_starts_at_its_own_chunk():
    chunks, win_start, emit_lo, emit_hi, shared = _plan(5, contiguous=False)
    assert chunks == [5]
    assert (win_start, emit_lo, emit_hi) == (100.0, 100.0, 110.0)
    assert shared == (0.0, 0.0)
