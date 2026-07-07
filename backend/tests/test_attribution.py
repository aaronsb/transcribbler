"""Unit tests for turn-aware attribution (ADR-0027 Decision 2a)."""

from __future__ import annotations

from transcribbler.attribution import split_segment_by_turns


def _texts(pieces):
    return [(p.speaker, p.text) for p in pieces]


def test_no_turns_falls_back_to_default():
    pieces = split_segment_by_turns(0.0, 4.0, "one two three four", [], default="Remote")
    assert _texts(pieces) == [("Remote", "one two three four")]


def test_single_speaker_returns_whole_segment():
    turns = [(0.0, 4.0, "S1")]
    pieces = split_segment_by_turns(0.0, 4.0, "one two three four", turns, default="Remote")
    assert _texts(pieces) == [("S1", "one two three four")]
    assert (pieces[0].start, pieces[0].end) == (0.0, 4.0)


def test_even_split_two_speakers():
    turns = [(0.0, 2.0, "S1"), (2.0, 4.0, "S2")]
    pieces = split_segment_by_turns(0.0, 4.0, "one two three four", turns, default="Remote")
    assert _texts(pieces) == [("S1", "one two"), ("S2", "three four")]


def test_change_at_quarter_splits_words_proportionally():
    # the 06:46 case: mostly the second speaker, a short lead-in from the first
    turns = [(0.0, 1.0, "S1"), (1.0, 4.0, "S6")]
    pieces = split_segment_by_turns(0.0, 4.0, "aa bb cc dd", turns, default="Remote")
    assert _texts(pieces) == [("S1", "aa"), ("S6", "bb cc dd")]


def test_incidental_overlap_is_not_a_split():
    # a 0.1s sliver of S2 at the tail is below the slice floor -> stays one speaker
    turns = [(0.0, 3.9, "S1"), (3.9, 4.0, "S2")]
    pieces = split_segment_by_turns(0.0, 4.0, "one two three four", turns, default="Remote")
    assert _texts(pieces) == [("S1", "one two three four")]


def test_uncovered_gap_inherits_nearest_neighbour():
    # gap [1,3] has no turn; it should fold into the left speaker, giving 3:1
    turns = [(0.0, 1.0, "S1"), (3.0, 4.0, "S2")]
    pieces = split_segment_by_turns(0.0, 4.0, "aa bb cc dd", turns, default="Remote")
    assert _texts(pieces) == [("S1", "aa bb cc"), ("S2", "dd")]


def test_word_counts_are_conserved():
    turns = [(0.0, 1.3, "S1"), (1.3, 4.0, "S2")]
    text = "a b c d e f g"
    pieces = split_segment_by_turns(0.0, 4.0, text, turns, default="Remote")
    assert sum(len(p.text.split()) for p in pieces) == len(text.split())


def test_empty_text_yields_nothing():
    assert split_segment_by_turns(0.0, 4.0, "   ", [(0.0, 4.0, "S1")], default="Remote") == []
