"""Unit tests for SessionGallery stitching (ADR-0024 / ADR-0027)."""

from __future__ import annotations

from transcribbler.session_gallery import SessionGallery

E1 = [1.0, 0.0, 0.0]
E2 = [0.0, 1.0, 0.0]  # orthogonal to E1 -> cosine 0, well under threshold


def _spk(label, emb):
    return {"label": label, "embedding": emb}


def test_cold_start_mints_distinct_speakers_then_rematches():
    g = SessionGallery(threshold=0.5)
    first = g.assign_chunk([_spk("0", E1), _spk("1", E2)])
    assert first == {"0": "S1", "1": "S2"}
    # a later isolated chunk: E1 again should re-match S1, not mint a third id
    again = g.assign_chunk([_spk("0", E1)])
    assert again == {"0": "S1"}
    assert g.speaker_count == 2


def test_temporal_link_beats_a_misleading_embedding():
    g = SessionGallery(threshold=0.5)
    g.assign_chunk([_spk("a", E1), _spk("b", E2)])  # establishes S1(E1), S2(E2)
    prev = [(0.0, 10.0, "S1"), (10.0, 20.0, "S2")]
    # window labels carry embeddings that would cosine-match the WRONG speaker,
    # but their turns temporally coincide with the right one -> temporal wins
    speakers = [_spk("0", E2), _spk("1", E1)]
    local_turns = [(2.0, 9.0, "0"), (11.0, 19.0, "1")]
    mapping = g.assign_window(speakers, local_turns, prev, shared_span=(0.0, 20.0))
    assert mapping == {"0": "S1", "1": "S2"}


def test_speaker_absent_from_shared_span_uses_embedding_fallback():
    g = SessionGallery(threshold=0.5)
    g.assign_chunk([_spk("a", E1)])  # S1(E1)
    prev = [(0.0, 10.0, "S1")]
    # "0" overlaps the shared span -> temporal S1; "9" is entirely outside it and
    # its embedding matches nothing known -> mint S2 via fallback
    speakers = [_spk("0", E1), _spk("9", E2)]
    local_turns = [(2.0, 8.0, "0"), (30.0, 35.0, "9")]
    mapping = g.assign_window(speakers, local_turns, prev, shared_span=(0.0, 10.0))
    assert mapping["0"] == "S1"
    assert mapping["9"] == "S2"


def test_incidental_shared_overlap_does_not_link():
    g = SessionGallery(threshold=0.5)
    g.assign_chunk([_spk("a", E1)])  # S1(E1)
    prev = [(0.0, 10.0, "S1")]
    # only 0.3s of shared overlap (< _MIN_LINK_S) -> no temporal link; E2 embedding
    # doesn't match S1 either -> mints a new id rather than falsely linking
    speakers = [_spk("0", E2)]
    local_turns = [(9.7, 12.0, "0")]
    mapping = g.assign_window(speakers, local_turns, prev, shared_span=(0.0, 10.0))
    assert mapping["0"] == "S2"


def test_temporal_link_is_one_to_one():
    g = SessionGallery(threshold=0.5)
    g.assign_chunk([_spk("a", E1), _spk("b", E2)])  # S1, S2
    prev = [(0.0, 10.0, "S1"), (10.0, 20.0, "S2")]
    # two local labels both overlap S1's span; only the larger overlap may take S1
    speakers = [_spk("0", E1), _spk("1", E1)]
    local_turns = [(0.0, 8.0, "0"), (6.0, 10.0, "1")]  # "0" overlaps S1 more
    mapping = g.assign_window(speakers, local_turns, prev, shared_span=(0.0, 20.0))
    assert mapping["0"] == "S1"
    assert mapping["1"] != "S1"
