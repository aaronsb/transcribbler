"""Tests for IR renderers (md/vtt). Pure views over the IR."""

from __future__ import annotations

import pytest

from transcribbler.render import _ts, render, to_markdown, to_vtt

IR = {
    "schema_version": "0.1",
    "source": {"kind": "session", "uri": "file:///x.wav", "duration_s": 90.0},
    "backend": {"kind": "modular", "asr": "whisper.cpp:vulkan", "diarizer": "pyannote:rocm"},
    "speakers": [
        {"id": "S1", "display_name": "Alan", "role": "host", "source": "manual"},
        {"id": "S2", "source": "fallback"},
    ],
    "turns": [
        {"speaker_id": "S1", "start": 0.0, "end": 2.0, "text": "hello"},
        {"speaker_id": "S1", "start": 2.0, "end": 4.0, "text": "again"},
        {"speaker_id": "S2", "start": 4.0, "end": 6.5, "text": "hi there"},
    ],
}


def test_ts_formats_hms_millis():
    assert _ts(0) == "00:00:00.000"
    assert _ts(3661.5) == "01:01:01.500"


def test_markdown_uses_name_or_id_and_groups_consecutive():
    md = to_markdown(IR)
    assert "**Alan** [00:00:00.000]" in md  # named speaker resolved
    assert "hello again" in md  # consecutive S1 turns merged
    assert "**S2** [00:00:04.000]" in md  # unnamed falls back to id
    assert "Alan (host)" in md  # role shown in header


def test_vtt_has_header_and_voice_tags():
    vtt = to_vtt(IR)
    assert vtt.startswith("WEBVTT")
    assert "00:00:04.000 --> 00:00:06.500" in vtt
    assert "<v Alan>hello" in vtt and "<v S2>hi there" in vtt


def test_render_dispatch_and_unknown():
    assert render(IR, "json").strip().startswith("{")
    assert render(IR, "md").startswith("# Transcript")
    assert render(IR, "vtt").startswith("WEBVTT")
    with pytest.raises(ValueError):
        render(IR, "pdf")
