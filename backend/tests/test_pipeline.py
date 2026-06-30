"""Tests for the extracted pipeline orchestration (CI-safe: fake cores).

These pin the behavior the CLI used to inline: stage ordering, progress-sink
threading, the diarize/canon gates, and the human log lines.
"""

from __future__ import annotations

import transcribbler.pipeline as pipeline
from transcribbler.profiles import Profile, StageConfig


def _profile(*, diar: bool, llm: bool) -> Profile:
    return Profile(
        name="test",
        asr=StageConfig(engine="whisper.cpp", backend="vulkan"),
        diar=StageConfig(engine="pyannote" if diar else "none", backend="rocm"),
        llm=StageConfig(engine="llama.cpp" if llm else "none", backend="vulkan"),
    )


def _patch_cores(monkeypatch, calls):
    class FakeASR:
        name = "fake-asr"

        def transcribe(self, audio, *, progress=None, prompt=None):
            calls.append(("asr", progress, prompt))
            return ["seg"]

    class FakeDiar:
        name = "fake-diar"

        def diarize(self, audio, *, progress=None):
            calls.append(("diar", progress))
            return ["turn"]

    class FakeCanon:
        name = "fake-canon"

        def canonicalize(self, evidence):
            calls.append(("canon", evidence))
            return {"speaker_map": [], "term_map": []}

    monkeypatch.setattr(pipeline, "asr_core", lambda cfg: FakeASR())
    monkeypatch.setattr(pipeline, "diarizer_core", lambda cfg: FakeDiar())
    monkeypatch.setattr(pipeline, "canonicalizer_core", lambda cfg: FakeCanon())
    monkeypatch.setattr(
        pipeline, "build_ir", lambda segs, prof, audio, diar_turns=None: {"segs": segs, "turns": diar_turns}
    )


def test_orders_stages_and_threads_progress(monkeypatch, tmp_path):
    calls: list = []
    _patch_cores(monkeypatch, calls)
    logs: list[str] = []
    ir = pipeline.run_pipeline(
        tmp_path / "a.wav",
        _profile(diar=True, llm=False),
        prompt="names",
        asr_progress="ASINK",
        diar_progress="DSINK",
        log=logs.append,
    )
    assert ir == {"segs": ["seg"], "turns": ["turn"]}
    assert calls == [("asr", "ASINK", "names"), ("diar", "DSINK")]
    assert any("fake-asr" in m for m in logs)
    assert any("speaker turns" in m for m in logs)


def test_no_diarize_flag_skips_diar(monkeypatch, tmp_path):
    calls: list = []
    _patch_cores(monkeypatch, calls)
    ir = pipeline.run_pipeline(tmp_path / "a.wav", _profile(diar=True, llm=False), diarize=False)
    assert [c[0] for c in calls] == ["asr"]
    assert ir["turns"] is None


def test_profile_without_diar_skips_diar(monkeypatch, tmp_path):
    calls: list = []
    _patch_cores(monkeypatch, calls)
    pipeline.run_pipeline(tmp_path / "a.wav", _profile(diar=False, llm=False))
    assert [c[0] for c in calls] == ["asr"]


def test_canon_runs_when_enabled(monkeypatch, tmp_path):
    calls: list = []
    _patch_cores(monkeypatch, calls)
    # apply_canonicalization/validate_ir need a real IR shape; stub them to isolate ordering.
    monkeypatch.setattr(pipeline, "build_evidence", lambda ir: "evidence")
    monkeypatch.setattr(pipeline, "apply_canonicalization", lambda ir, data: {**ir, "speakers": []})
    monkeypatch.setattr(pipeline, "validate_ir", lambda ir: None)
    pipeline.run_pipeline(tmp_path / "a.wav", _profile(diar=False, llm=True))
    assert [c[0] for c in calls] == ["asr", "canon"]


def test_no_canon_flag_skips_canon(monkeypatch, tmp_path):
    calls: list = []
    _patch_cores(monkeypatch, calls)
    pipeline.run_pipeline(tmp_path / "a.wav", _profile(diar=False, llm=True), canon=False)
    assert [c[0] for c in calls] == ["asr"]
