"""Unit tests for session-pack persistence + extraction (docs/specs/session-pack.md v0.1)."""

from __future__ import annotations

import io
import json
import shutil
import tarfile
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from transcribbler import library, pack
from transcribbler.ir import build_live_ir

PROF = SimpleNamespace(
    asr=SimpleNamespace(engine="whisper", backend="cpp"),
    diar=SimpleNamespace(engine="pyannote", backend="community-1"),
)
STARTED = datetime(2026, 7, 7, 14, 12, 5, tzinfo=timezone.utc)


def _isolate(tmp_path, monkeypatch):
    # paths.* read $XDG_DATA_HOME at call time → redirects sessions/ and library/
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))


def _enrollment_ir(name: str, secs: float = 4.0) -> dict:
    return build_live_ir(
        [(0.0, secs, name, "the quick brown fox")],
        PROF,
        duration_s=secs,
        operator_label=name,
        diarized=True,
    )


# ── pure helpers ────────────────────────────────────────────────────────────


def test_blob_name_grammar():
    name = pack.blob_name(STARTED, start_s=0, length_s=42, uid="7b1e04")
    assert name == "2026-07-07-141205-0-42-7b1e04-blob.tar.gz"


def test_frontmatter_emits_scalars_and_lists():
    fm = pack._frontmatter(
        {"id": "203346", "start": 0, "tags": ["enrollment", "training"], "empty": [], "skip": None}
    )
    assert fm.startswith("---\n") and fm.rstrip().endswith("---")
    assert 'id: "203346"' in fm  # string stays a string (not parsed as int 203346)
    assert "start: 0" in fm  # ints emit bare
    assert 'tags:\n  - "enrollment"\n  - "training"' in fm
    assert "empty: []" in fm
    assert "skip" not in fm  # None values are dropped


def test_frontmatter_quotes_yaml_special_values():
    # a name with a colon-space would emit malformed YAML if unquoted (finding #6)
    fm = pack._frontmatter({"title": "Dr: Priya", "note": 'a "quoted" # hash'})
    assert 'title: "Dr: Priya"' in fm
    assert 'note: "a \\"quoted\\" # hash"' in fm


def test_slug():
    assert pack.slug("Priya Sharma!") == "priya-sharma"
    assert pack.slug("  ") == "untitled"


# ── write → extract round-trip ────────────────────────────────────────────────


def test_write_pack_lands_two_loose_artifacts(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    ir = _enrollment_ir("Priya")
    sid = ir["speakers"][0]["id"]
    result = pack.write_pack(
        ir,
        title="Priya enrollment",
        tags=["enrollment", "training"],
        embeddings={sid: [1.0, 0.0, 0.0]},
        audio={},  # no ffmpeg needed: embedding fold path doesn't require the clip
        started=STARTED,
    )
    assert result.md_path.exists() and result.md_path.name == "2026-07-07-priya-enrollment.md"
    assert result.blob_path.exists() and result.blob_path.name.endswith("-blob.tar.gz")
    fm = result.md_path.read_text()
    assert 'type: "session_pack"' in fm
    assert f'blob: "{result.blob_path.name}"' in fm
    assert 'tags:\n  - "enrollment"\n  - "training"' in fm


def test_blob_is_self_describing(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    ir = _enrollment_ir("Priya")
    sid = ir["speakers"][0]["id"]
    result = pack.write_pack(
        ir, title="Priya enrollment", tags=["training"],
        embeddings={sid: [1.0, 0.0, 0.0]}, audio={}, started=STARTED,
    )
    with tarfile.open(result.blob_path, "r:gz") as tar:
        members = tar.getnames()
        record = json.loads(tar.extractfile("record.ir.json").read())
        embed = json.loads(tar.extractfile("embeddings.json").read())
    assert "record.ir.json" in members  # source of truth
    assert "session.md" in members  # self-describing copy of the sidecar
    assert record["source"]["kind"] == "session"
    assert embed["pack_uid"] == result.uid
    assert embed["vectors"][sid] == [1.0, 0.0, 0.0]


def test_extract_folds_voiceprint_from_pack(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    ir = _enrollment_ir("Priya")
    sid = ir["speakers"][0]["id"]
    result = pack.write_pack(
        ir, title="Priya enrollment", tags=["training"],
        embeddings={sid: [1.0, 0.0, 0.0]}, audio={}, started=STARTED,
    )
    vps = pack.extract(result.blob_path)
    assert len(vps) == 1
    vp = vps[0]
    assert vp.name == "Priya"
    assert vp.uid == f"{result.uid}-priya"  # spec §8.2 <pack_uid>-<name>
    assert vp.samples == 1
    assert vp.sources == [f"../sessions/{result.blob_path.name}"]  # graph back-reference
    assert library.find_by_name("Priya").uid == vp.uid  # landed in the durable library


def test_second_pack_compounds_same_voiceprint(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    # two separate enrollment packs of the same person → one compounding voiceprint
    first = pack.write_pack(
        _enrollment_ir("Priya"), title="Priya enrollment", tags=["training"],
        embeddings={"S0": [1.0, 0.0, 0.0]}, audio={}, started=STARTED, uid="aaaaaa",
    )
    second = pack.write_pack(
        _enrollment_ir("Priya"), title="Priya enrollment", tags=["training"],
        embeddings={"S0": [0.0, 1.0, 0.0]}, audio={}, started=STARTED, uid="bbbbbb",
    )
    pack.extract(first.blob_path)
    vp = pack.extract(second.blob_path)[0]

    assert len(library.load_all()) == 1  # not a duplicate
    assert vp.uid == "aaaaaa-priya"  # stable: seeded by the FIRST pack, not the second
    assert vp.samples == 2  # compounded
    assert abs(vp.centroid[0] - 0.5) < 1e-9 and abs(vp.centroid[1] - 0.5) < 1e-9
    assert vp.sources == [  # both packs recorded as sources
        f"../sessions/{first.blob_path.name}",
        f"../sessions/{second.blob_path.name}",
    ]
    # same-day + same-title enrollments must not overwrite each other's sidecar (finding #4)
    assert first.md_path != second.md_path
    assert first.md_path.exists() and second.md_path.exists()


def test_extract_is_idempotent(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    ir = _enrollment_ir("Priya")
    sid = ir["speakers"][0]["id"]
    result = pack.write_pack(
        ir, title="Priya enrollment", tags=["training"],
        embeddings={sid: [1.0, 0.0, 0.0]}, audio={}, started=STARTED,
    )
    pack.extract(result.blob_path)
    vp = pack.extract(result.blob_path)[0]  # re-extracting the same pack is a no-op
    assert vp.samples == 1  # not inflated
    assert vp.sources == [f"../sessions/{result.blob_path.name}"]  # not duplicated


def test_extract_rejects_pack_without_embeddings(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    bogus = tmp_path / "2026-07-07-000000-0-1-nope00-blob.tar.gz"
    with tarfile.open(bogus, "w:gz") as tar:
        data = b'{"speakers": []}'
        info = tarfile.TarInfo("record.ir.json")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    with pytest.raises(ValueError, match="not an extractable pack"):
        pack.extract(bogus)


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg required to transcode opus clips")
def test_active_pack_carries_opus_audio(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    import subprocess

    wav = tmp_path / "clip.wav"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
         "-i", "anullsrc=r=16000:cl=mono", "-t", "1", str(wav)],
        check=True,
    )
    ir = _enrollment_ir("Priya")
    sid = ir["speakers"][0]["id"]
    result = pack.write_pack(
        ir, title="Priya enrollment", tags=["training"],
        embeddings={sid: [1.0, 0.0, 0.0]}, audio={sid: wav}, started=STARTED,
    )
    with tarfile.open(result.blob_path, "r:gz") as tar:
        members = tar.getnames()
    assert f"audio/{sid}.opus" in members  # clip keyed by collision-free canonical id


def test_audio_falls_back_to_source_when_opus_unavailable(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    monkeypatch.setattr(pack, "_to_opus", lambda src, dst: False)  # simulate no libopus
    wav = tmp_path / "clip.wav"
    wav.write_bytes(b"RIFF....WAVEfake")  # opaque bytes: fallback just archives them as-is
    ir = _enrollment_ir("Priya")
    sid = ir["speakers"][0]["id"]
    result = pack.write_pack(
        ir, title="Priya enrollment", tags=["training"],
        embeddings={sid: [1.0, 0.0, 0.0]}, audio={sid: wav}, started=STARTED,
    )
    with tarfile.open(result.blob_path, "r:gz") as tar:
        members = tar.getnames()
    assert f"audio/{sid}.wav" in members  # source clip kept, pack + voiceprint not lost
    assert pack.extract(result.blob_path)[0].name == "Priya"


def test_extract_rejects_corrupt_json(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    bogus = tmp_path / "2026-07-07-000000-0-1-bad000-blob.tar.gz"
    with tarfile.open(bogus, "w:gz") as tar:
        for member, data in [("record.ir.json", b"{not json"), ("embeddings.json", b"{}")]:
            info = tarfile.TarInfo(member)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    with pytest.raises(ValueError, match="not an extractable pack"):
        pack.extract(bogus)


def test_write_pack_honours_explicit_md_path(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    ir = _enrollment_ir("Priya")
    sid = ir["speakers"][0]["id"]
    out = tmp_path / "my-meeting.md"  # the operator's -o transcript path
    result = pack.write_pack(
        ir, title="Team standup", tags=["meeting"], embeddings={sid: [1.0, 0.0, 0.0]},
        audio={}, started=STARTED, dest_dir=tmp_path, md_path=out,
    )
    assert result.md_path == out and out.exists()  # sidecar pinned to -o
    assert result.blob_path.parent == tmp_path and result.blob_path.exists()  # blob beside it


def _ffprobe_dur(wav: Path) -> float:
    import subprocess
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of",
         "default=nk=1:nw=1", str(wav)],
        capture_output=True, text=True, check=True,
    )
    return float(out.stdout.strip())


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg required to slice audio")
def test_build_speaker_clips_isolates_by_channel_and_spans(tmp_path):
    import subprocess

    # a 10s stereo wav: distinct tone per channel so a mis-channelled slice would be audible
    stereo = tmp_path / "session.wav"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "sine=frequency=200:duration=10",
         "-f", "lavfi", "-i", "sine=frequency=600:duration=10",
         "-filter_complex", "[0:a][1:a]join=inputs=2:channel_layout=stereo[o]",
         "-map", "[o]", "-ar", "16000", str(stereo)],
        check=True,
    )
    clips = pack.build_speaker_clips(
        stereo,
        spans_by_id={"S0": [(0.0, 2.0), (4.0, 5.0)], "S1": [(6.0, 9.0)], "S2": []},
        channel_by_id={"S0": 0, "S1": 1},  # S0=mic, S1=meeting
        dst_dir=tmp_path,
    )
    assert set(clips) == {"S0", "S1"}  # S2 (no spans) skipped
    assert abs(_ffprobe_dur(clips["S0"]) - 3.0) < 0.2  # 2s + 1s of its spans, concatenated
    assert abs(_ffprobe_dur(clips["S1"]) - 3.0) < 0.2  # its single 3s span
