"""ASR confidence signal: whisper token probs → Segment → IR turn → flagged render (#11)."""

from __future__ import annotations

import json
from types import SimpleNamespace

from transcribbler.align import align
from transcribbler.cores.base import Segment, SpeakerTurn
from transcribbler.cores.whisper_cpp import _parse, _segment_confidence
from transcribbler.ir import build_ir
from transcribbler.render import LOW_CONFIDENCE, to_markdown

PROF = SimpleNamespace(
    asr=SimpleNamespace(engine="whisper", backend="cpp"),
    diar=SimpleNamespace(engine="pyannote", backend="community-1"),
)


def _wav(tmp_path):
    p = tmp_path / "a.wav"
    p.write_bytes(b"RIFF....WAVEfake")  # build_ir only needs a file to hash
    return p


# ── the signal: mean word-token probability ───────────────────────────────────


def test_segment_confidence_means_word_tokens_only():
    tokens = [{"text": "[_BEG_]", "p": 0.01}, {"text": "Hello", "p": 0.9}, {"text": " world", "p": 0.8}]
    assert _segment_confidence(tokens) == 0.85  # special token excluded; mean of 0.9, 0.8
    assert _segment_confidence([]) is None
    assert _segment_confidence([{"text": "x"}]) is None  # no probabilities → None, stays optional


def test_parse_reads_confidence_from_full_json(tmp_path):
    j = tmp_path / "o.json"
    j.write_text(json.dumps({"transcription": [
        {"offsets": {"from": 0, "to": 1000}, "text": "hi", "tokens": [{"text": "hi", "p": 0.95}]},
        {"offsets": {"from": 1000, "to": 2000}, "text": "mmf", "tokens": [{"text": "mmf", "p": 0.30}]},
    ]}))
    segs = _parse(j)
    assert [s.confidence for s in segs] == [0.95, 0.3]


# ── carried into the IR (both attribution paths) ───────────────────────────────


def test_build_ir_plain_path_carries_confidence(tmp_path):
    segs = [Segment(0.0, 1.0, "clear", confidence=0.95), Segment(1.0, 2.0, "garbled", confidence=0.2)]
    ir = build_ir(segs, PROF, _wav(tmp_path))  # validates against the schema (accepts confidence)
    assert [t.get("confidence") for t in ir["turns"]] == [0.95, 0.2]


def test_align_carries_segment_confidence():
    _, aligned = align([Segment(0.0, 1.0, "hi", confidence=0.42)], [SpeakerTurn(0.0, 1.0, "0")])
    assert aligned[0].confidence == 0.42


def test_build_ir_omits_confidence_when_absent(tmp_path):
    ir = build_ir([Segment(0.0, 1.0, "hi")], PROF, _wav(tmp_path))
    assert "confidence" not in ir["turns"][0]  # optional — never emitted as null


# ── surfaced in the render ─────────────────────────────────────────────────────


def test_render_flags_only_low_confidence_blocks(tmp_path):
    segs = [Segment(0.0, 1.0, "clear speech", confidence=0.95),
            Segment(1.0, 2.0, "garbled bit", confidence=0.2)]
    md = to_markdown(build_ir(segs, PROF, _wav(tmp_path)))
    # both are the one fallback speaker → merged block; a block is as weak as its weakest turn
    assert "⚠ low confidence (0.20)" in md
    assert md.count("⚠") == 1


def test_render_unflagged_without_confidence(tmp_path):
    md = to_markdown(build_ir([Segment(0.0, 1.0, "hello there")], PROF, _wav(tmp_path)))
    assert "⚠" not in md


def test_low_confidence_threshold_is_sane():
    assert 0.0 < LOW_CONFIDENCE < 1.0
