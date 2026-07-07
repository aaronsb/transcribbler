"""Unit tests for the capture drain-and-persist seam (capture_persist.py)."""

from __future__ import annotations

import json
import shutil
import subprocess
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from transcribbler import capture_persist, pack

PROF = SimpleNamespace(
    asr=SimpleNamespace(engine="whisper", backend="cpp"),
    diar=SimpleNamespace(engine="pyannote", backend="community-1"),
)


def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))


def _stereo(dst: Path, secs: int = 10) -> Path:
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", f"sine=frequency=200:duration={secs}",
         "-f", "lavfi", "-i", f"sine=frequency=600:duration={secs}",
         "-filter_complex", "[0:a][1:a]join=inputs=2:channel_layout=stereo[o]",
         "-map", "[o]", "-ar", "16000", "-c:a", "pcm_s16le", str(dst)],
        check=True,
    )
    return dst


def _dur(wav: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nk=1:nw=1", str(wav)],
        capture_output=True, text=True, check=True,
    )
    return float(out.stdout.strip())


# operator "You" + one diarized remote S1
TURNS = [(0.0, 2.0, "You", "morning"), (2.0, 5.0, "S1", "hi there"), (6.0, 9.0, "S1", "more")]


def test_persist_without_audio_still_packs_transcript_and_embeddings(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    out = tmp_path / "standup.md"
    result = capture_persist.persist_session_pack(
        TURNS, PROF, operator_label="You", diarized=True,
        centroids={"S1": [0.1] * 256},  # gallery seeded S1; operator has no centroid
        session_wav=None, out_path=out,
    )
    assert out.exists()  # -o path is the sidecar
    assert result.blob_path.parent == tmp_path
    with tarfile.open(result.blob_path, "r:gz") as tar:
        members = tar.getnames()
        embed = json.loads(tar.extractfile("embeddings.json").read())
    assert not any(m.startswith("audio/") for m in members)  # no retained audio
    assert "S1" in embed["vectors"] and "S0" not in embed["vectors"]  # only diarized remote
    assert pack.extract(result.blob_path)  # embeddings alone prime the library


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg required to slice audio")
def test_persist_with_audio_cuts_speaker_isolated_clips(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    session = _stereo(tmp_path / "session.wav")
    out = tmp_path / "standup.md"
    result = capture_persist.persist_session_pack(
        TURNS, PROF, operator_label="You", diarized=True,
        centroids={"S1": [0.1] * 256}, session_wav=session, out_path=out,
    )
    with tarfile.open(result.blob_path, "r:gz") as tar:
        members = tar.getnames()
    # You → S0 (mic channel), remote → S1 (meeting channel); clips keyed by canonical id
    assert "audio/S0.opus" in members
    assert "audio/S1.opus" in members


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg required to assemble audio")
def test_assemble_session_gap_fills_missing_chunks(tmp_path):
    retain = tmp_path / "retain"
    retain.mkdir()
    for i in (0, 2):  # chunk 1 is missing (dropped/paused) — must be silence-filled
        _stereo(retain / f"chunk_{i:05d}.wav", secs=2)
    session = capture_persist.assemble_session(retain, 2, tmp_path / "session.wav")
    assert session is not None
    assert abs(_dur(session) - 6.0) < 0.3  # 2s + 2s silence + 2s, alignment preserved


def test_assemble_session_returns_none_when_nothing_retained(tmp_path):
    retain = tmp_path / "retain"
    retain.mkdir()
    assert capture_persist.assemble_session(retain, 2, tmp_path / "session.wav") is None
