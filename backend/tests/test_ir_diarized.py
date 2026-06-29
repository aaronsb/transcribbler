"""build_ir produces schema-valid IR in both ASR-only and diarized modes."""

from __future__ import annotations

from transcribbler.cores.base import Segment, SpeakerTurn
from transcribbler.ir import build_ir
from transcribbler.profiles import Profile, StageConfig


def _profile() -> Profile:
    return Profile(
        name="test",
        asr=StageConfig(engine="whisper.cpp", backend="vulkan"),
        diar=StageConfig(engine="pyannote", backend="rocm"),
        llm=StageConfig(engine="none"),
    )


def _audio(tmp_path):
    p = tmp_path / "a.wav"
    p.write_bytes(b"RIFF....fake")  # content only needs to exist for sha256/uri
    return p


def test_asr_only_ir_validates(tmp_path):
    segs = [Segment(0, 5, "hello"), Segment(5, 9, "there")]
    ir = build_ir(segs, _profile(), _audio(tmp_path))  # no diar_turns -> fallback
    assert ir["speakers"] == [{"id": "S1", "source": "fallback"}]
    assert "diarizer" not in ir["backend"]
    assert {t["speaker_id"] for t in ir["turns"]} == {"S1"}


def test_diarized_ir_validates_and_attributes_two_speakers(tmp_path):
    segs = [Segment(1, 4, "hi"), Segment(12, 16, "bye")]
    turns = [SpeakerTurn(0, 10, "0"), SpeakerTurn(10, 20, "1")]
    ir = build_ir(segs, _profile(), _audio(tmp_path), diar_turns=turns)  # validates internally
    assert {s["id"] for s in ir["speakers"]} == {"S1", "S2"}
    assert ir["backend"]["diarizer"] == "pyannote:rocm"
    assert [t["speaker_id"] for t in ir["turns"]] == ["S1", "S2"]


def test_diarized_ir_emits_secondary_speakers_on_overlap(tmp_path):
    segs = [Segment(8, 14, "overlap zone")]
    turns = [SpeakerTurn(0, 10, "0"), SpeakerTurn(10, 20, "1")]
    ir = build_ir(segs, _profile(), _audio(tmp_path), diar_turns=turns)
    turn = ir["turns"][0]
    assert turn["speaker_id"] == "S2"
    assert turn["secondary_speakers"] == ["S1"]
