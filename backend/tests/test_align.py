"""Tests for overlap alignment (ADR-0005). Pure, deterministic — golden behavior."""
from __future__ import annotations

from transcribbler.align import SECONDARY_MIN_OVERLAP, align, canonical_speaker_map
from transcribbler.cores.base import Segment, SpeakerTurn


def _seg(start, end, text="x"):
    return Segment(start=start, end=end, text=text)


def _turn(start, end, label):
    return SpeakerTurn(start=start, end=end, label=label)


def test_canonical_map_is_first_appearance_in_time():
    # Label "1" appears earlier in time than "0" -> it must become S1.
    turns = [_turn(5, 6, "0"), _turn(0, 1, "1")]
    assert canonical_speaker_map(turns) == {"1": "S1", "0": "S2"}


def test_single_speaker_attributes_everything_to_s1():
    turns = [_turn(0, 30, "A")]
    segs = [_seg(0, 5), _seg(5, 10)]
    ids, aligned = align(segs, turns)
    assert ids == ["S1"]
    assert [a.speaker_id for a in aligned] == ["S1", "S1"]
    assert all(not a.secondary_speakers for a in aligned)


def test_two_speakers_clean_segments():
    turns = [_turn(0, 10, "0"), _turn(10, 20, "1")]
    segs = [_seg(1, 4), _seg(12, 16)]
    ids, aligned = align(segs, turns)
    assert ids == ["S1", "S2"]
    assert [a.speaker_id for a in aligned] == ["S1", "S2"]


def test_segment_spanning_boundary_picks_majority_and_flags_secondary():
    # Segment 8..14: 2s with speaker0 (8..10), 4s with speaker1 (10..14) -> primary S2.
    turns = [_turn(0, 10, "0"), _turn(10, 20, "1")]
    segs = [_seg(8, 14)]
    _, aligned = align(segs, turns)
    a = aligned[0]
    assert a.speaker_id == "S2"          # speaker "1" has the majority overlap
    assert a.secondary_speakers == ["S1"]  # speaker "0" covers 2/6 = 33% >= threshold


def test_minor_boundary_spill_is_not_secondary():
    # Segment 9.5..14: only 0.5s of speaker0 -> 0.5/4.5 ~= 11% < threshold, not secondary.
    assert SECONDARY_MIN_OVERLAP > 0.11
    turns = [_turn(0, 10, "0"), _turn(10, 20, "1")]
    segs = [_seg(9.5, 14)]
    _, aligned = align(segs, turns)
    assert aligned[0].speaker_id == "S2"
    assert aligned[0].secondary_speakers == []


def test_segment_in_gap_attributes_to_nearest_turn():
    # No turn covers 11..12; nearest by midpoint is the 0..10 turn (mid 5 vs 20).
    turns = [_turn(0, 10, "0"), _turn(18, 22, "1")]
    segs = [_seg(11, 12)]
    _, aligned = align(segs, turns)
    assert aligned[0].speaker_id == "S1"


def test_text_and_timing_are_preserved():
    turns = [_turn(0, 10, "0")]
    segs = [_seg(1.234, 4.567, "hello world")]
    _, aligned = align(segs, turns)
    assert aligned[0].text == "hello world"
    assert (aligned[0].start, aligned[0].end) == (1.234, 4.567)
