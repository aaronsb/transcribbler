"""Tests for canonicalization verify/apply (ADR-0006). The anti-hallucination core:
a proposed name is applied only if confident AND backed by the speaker's own turns."""
from __future__ import annotations

from transcribbler.canon import apply_canonicalization, build_evidence, evidence_supported


def _ir(*turns):
    speakers = sorted({t[0] for t in turns})
    return {
        "schema_version": "0.1",
        "source": {"kind": "session", "duration_s": 100},
        "backend": {"kind": "modular"},
        "speakers": [{"id": s, "source": "fallback"} for s in speakers],
        "turns": [{"speaker_id": s, "start": a, "end": b, "text": txt} for (s, a, b, txt) in turns],
    }


def test_evidence_supported_matches_words_in_speaker_text():
    assert evidence_supported("I am Alan Watts", "well I am Alan Watts and today")
    assert not evidence_supported("my name is Dolores", "the weather is nice today")


def test_supported_name_is_applied():
    ir = _ir(("S1", 0, 4, "hello I am Alan Watts"))
    data = {"speaker_map": [{"id": "S1", "display_name": "Alan Watts", "role": "host",
                             "confidence": 0.95, "evidence": "I am Alan Watts"}], "term_map": []}
    out = apply_canonicalization(ir, data)
    sp = out["speakers"][0]
    assert sp["display_name"] == "Alan Watts" and sp["source"] == "llm" and sp["role"] == "host"


def test_third_person_naming_is_accepted():
    # S1 is named by S2 addressing them; the quote lives in S2's turn (the transcript),
    # not S1's own — B-with-guards accepts this, the common discussion case.
    ir = _ir(("S1", 0, 4, "yes I can take that one"), ("S2", 4, 7, "thanks Bob go ahead"))
    data = {"speaker_map": [{"id": "S1", "display_name": "Bob", "confidence": 0.8,
                             "evidence": "thanks Bob"}], "term_map": []}
    out = apply_canonicalization(ir, data)
    assert out["speakers"][0]["display_name"] == "Bob" and out["speakers"][0]["source"] == "llm"


def test_unsupported_evidence_is_rejected():
    # The model claims a name but cites evidence NOT in the speaker's turns -> reject.
    ir = _ir(("S1", 0, 4, "today we talk about the weather"))
    data = {"speaker_map": [{"id": "S1", "display_name": "Dolores", "confidence": 0.99,
                             "evidence": "my name is Dolores"}], "term_map": []}
    out = apply_canonicalization(ir, data)
    assert out["speakers"][0] == {"id": "S1", "source": "fallback"}  # unchanged


def test_low_confidence_is_rejected():
    ir = _ir(("S1", 0, 4, "hello I am Alan Watts"))
    data = {"speaker_map": [{"id": "S1", "display_name": "Alan Watts", "confidence": 0.2,
                             "evidence": "I am Alan Watts"}], "term_map": []}
    assert out_unchanged(apply_canonicalization(ir, data))


def test_empty_name_leaves_speaker_as_fallback():
    ir = _ir(("S1", 0, 4, "some content"))
    data = {"speaker_map": [{"id": "S1", "display_name": "", "confidence": 0.0, "evidence": "x"}],
            "term_map": []}
    assert out_unchanged(apply_canonicalization(ir, data))


def out_unchanged(out):
    return out["speakers"][0] == {"id": "S1", "source": "fallback"}


def test_build_evidence_has_one_block_per_speaker():
    ir = _ir(("S1", 0, 4, "alpha"), ("S2", 5, 9, "beta"))
    ev = build_evidence(ir)
    assert "[S1]" in ev and "[S2]" in ev and "term_map" in ev
