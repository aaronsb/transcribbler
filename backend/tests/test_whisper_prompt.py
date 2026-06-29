"""Tests for whisper.cpp initial-prompt wiring (CI-safe: no real binary/audio)."""

from __future__ import annotations

import transcribbler.cores.whisper_cpp as wc


def _core(prompt=None):
    """Build a core without __init__'s binary/model existence checks."""
    core = wc.WhisperCppCore.__new__(wc.WhisperCppCore)
    core.binary = "/usr/bin/true"
    core.model = "/usr/bin/true"
    core.prompt = prompt
    return core


def _capture_cmd(monkeypatch):
    captured: dict = {}

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        return (0, "", "")

    monkeypatch.setattr(wc, "run_streamed", fake_run)
    monkeypatch.setattr(wc, "normalize_wav", lambda src, dst: dst)
    monkeypatch.setattr(wc, "_parse", lambda path: [])
    return captured


def test_prompt_passed_and_carried(monkeypatch, tmp_path):
    captured = _capture_cmd(monkeypatch)
    _core().transcribe(tmp_path / "a.wav", prompt="Aaron Bockelie, pyannote")
    cmd = captured["cmd"]
    assert "--prompt" in cmd
    assert cmd[cmd.index("--prompt") + 1] == "Aaron Bockelie, pyannote"
    assert "--carry-initial-prompt" in cmd


def test_no_prompt_no_flags(monkeypatch, tmp_path):
    captured = _capture_cmd(monkeypatch)
    _core().transcribe(tmp_path / "a.wav")
    assert "--prompt" not in captured["cmd"]
    assert "--carry-initial-prompt" not in captured["cmd"]


def test_per_call_prompt_overrides_profile_default(monkeypatch, tmp_path):
    captured = _capture_cmd(monkeypatch)
    _core(prompt="from-profile").transcribe(tmp_path / "a.wav", prompt="from-cli")
    cmd = captured["cmd"]
    assert cmd[cmd.index("--prompt") + 1] == "from-cli"


def test_profile_default_used_when_no_call_prompt(monkeypatch, tmp_path):
    captured = _capture_cmd(monkeypatch)
    _core(prompt="from-profile").transcribe(tmp_path / "a.wav")
    cmd = captured["cmd"]
    assert cmd[cmd.index("--prompt") + 1] == "from-profile"
