"""Pack lifecycle: discover, inspect, relabel, and the capture→named-voiceprint loop.

These exercise the operations that make a session pack *operable* (spec §7–§9): finding packs,
reading their speakers/audio, renaming an anonymous diarized speaker, and folding the result into
the durable voiceprint library with the session→voiceprint graph edge closed.
"""

from __future__ import annotations

import subprocess
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
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))  # redirects sessions/ + library/


def _silent_wav(path: Path, secs: float = 1.0) -> Path:
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
         "-i", "anullsrc=r=16000:cl=mono", "-t", str(secs), str(path)],
        check=True,
    )
    return path


def _two_speaker_pack(tmp_path, *, uid="abc123", title="Team standup", started=STARTED):
    """A capture-shaped pack: operator You(S0) + an anonymous remote(S1), both with audio."""
    ir = build_live_ir(
        [(0.0, 2.0, "You", "morning"), (2.0, 5.0, "Remote", "hello everyone")],
        PROF, duration_s=5.0, operator_label="You", diarized=True,
    )
    ids = [s["id"] for s in ir["speakers"]]  # -> ["S0", "S1"]
    clip = _silent_wav(tmp_path / "clip.wav")
    return pack.write_pack(
        ir, title=title, tags=["meeting"],
        embeddings={ids[0]: [1.0, 0.0, 0.0], ids[1]: [0.0, 1.0, 0.0]},
        audio={sid: clip for sid in ids}, started=started, uid=uid,
    )


# ── discovery ────────────────────────────────────────────────────────────────


def test_find_packs_lists_newest_first(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    _two_speaker_pack(tmp_path, uid="aaaaaa", title="older",
                      started=datetime(2026, 7, 1, 9, 0, 0, tzinfo=timezone.utc))
    _two_speaker_pack(tmp_path, uid="bbbbbb", title="newer",
                      started=datetime(2026, 7, 8, 9, 0, 0, tzinfo=timezone.utc))
    packs = pack.find_packs()
    assert [p.uid for p in packs] == ["bbbbbb", "aaaaaa"]  # blob names sort by date, newest first
    assert packs[0].meta["title"] == "newer"


def test_find_packs_falls_back_to_embedded_meta_when_sidecar_deleted(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    result = _two_speaker_pack(tmp_path)
    result.md_path.unlink()  # "I deleted my .md files" — blob is still authoritative (spec §6)
    packs = pack.find_packs()
    assert len(packs) == 1
    assert packs[0].md_path is None
    assert packs[0].meta["title"] == "Team standup"  # recovered from embedded session.md


def test_load_pack_by_prefix_and_errors(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    _two_speaker_pack(tmp_path, uid="abcdef")
    assert pack.load_pack("abc").uid == "abcdef"  # unique prefix
    with pytest.raises(ValueError, match="no pack matching"):
        pack.load_pack("zzz")


def test_load_pack_ambiguous_prefix_raises(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    _two_speaker_pack(tmp_path, uid="ab1111", title="one")
    _two_speaker_pack(tmp_path, uid="ab2222", title="two")
    with pytest.raises(ValueError, match="ambiguous"):
        pack.load_pack("ab")


# ── inspection ───────────────────────────────────────────────────────────────


def test_pack_details_reports_speakers_and_audio(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    pk = pack.load_pack(_two_speaker_pack(tmp_path).uid)
    d = pack.pack_details(pk)
    assert d["has_audio"] is True
    by_id = {s["id"]: s for s in d["speakers"]}
    assert by_id["S0"]["name"] == "You" and by_id["S1"]["name"] == "Remote"
    assert by_id["S1"]["turns"] == 1 and by_id["S1"]["speech_s"] == pytest.approx(3.0)
    assert by_id["S1"]["clip"] is True


def test_read_clip_extracts_and_missing_raises(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    pk = pack.load_pack(_two_speaker_pack(tmp_path).uid)
    out = pack.read_clip(pk, "S1", tmp_path / "aud")
    assert out.exists() and out.stat().st_size > 0
    with pytest.raises(ValueError, match="no audio clip"):
        pack.read_clip(pk, "S9", tmp_path / "aud")


# ── relabel + the capture→named-voiceprint loop ──────────────────────────────


def test_relabel_renames_speaker_in_blob_and_sidecar(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    pk = pack.load_pack(_two_speaker_pack(tmp_path).uid)
    pk = pack.relabel(pk, "S1", "Priya")

    # record.ir.json (source of truth) carries the new name
    ir = pack.read_record(pk.blob_path)
    s1 = next(s for s in ir["speakers"] if s["id"] == "S1")
    assert s1["display_name"] == "Priya"
    # participants mirror + loose sidecar both refreshed
    assert pk.meta["participants"] == ["You", "Priya"]
    assert "Priya" in pk.md_path.read_text()


def test_relabel_unknown_speaker_raises(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    pk = pack.load_pack(_two_speaker_pack(tmp_path).uid)
    with pytest.raises(ValueError, match="no speaker"):
        pack.relabel(pk, "S9", "Nobody")


def test_relabel_empty_name_raises(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    pk = pack.load_pack(_two_speaker_pack(tmp_path).uid)
    with pytest.raises(ValueError, match="cannot be empty"):
        pack.relabel(pk, "S1", "   ")


def test_relabel_preserves_audio_clips(tmp_path, monkeypatch):
    # The primary _rewrite_blob contract: mutating the record must not lose the isolated audio.
    _isolate(tmp_path, monkeypatch)
    pk = pack.load_pack(_two_speaker_pack(tmp_path).uid)
    before = pack.pack_details(pk)
    assert all(s["clip"] for s in before["speakers"])
    pk = pack.relabel(pk, "S1", "Priya")
    after = pack.pack_details(pk)
    assert after["has_audio"] and all(s["clip"] for s in after["speakers"])
    assert pack.read_clip(pk, "S1", tmp_path / "still").stat().st_size > 0


def test_relabel_then_extract_yields_named_voiceprint(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    pk = pack.load_pack(_two_speaker_pack(tmp_path, uid="pack01").uid)
    pk = pack.relabel(pk, "S1", "Priya")

    vps = pack.extract(pk.blob_path)
    names = {vp.name for vp in vps}
    assert "Priya" in names  # the once-anonymous remote is now a named voiceprint
    priya = next(vp for vp in vps if vp.name == "Priya")
    assert priya.uid == "pack01-priya"
    assert library.find_by_name("Priya") is not None  # landed in the durable library


def test_delete_removes_pack_files_but_not_voiceprints(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    pk = pack.load_pack(_two_speaker_pack(tmp_path).uid)
    pack.extract(pk.blob_path)  # teach a voiceprint first
    assert pk.blob_path.exists() and pk.md_path.exists()

    pack.delete_pack(pk)
    assert not pk.blob_path.exists() and not pk.md_path.exists()
    assert pack.find_packs() == []
    assert library.load_all()  # the identities it taught survive the pack's deletion


def test_delete_tidies_emptied_session_subdir(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    from transcribbler import paths

    sub = paths.ensure(paths.sessions_dir() / "20260708-100021")  # a listen-style per-session dir
    ir = build_live_ir([(0.0, 2.0, "You", "hi")], PROF, duration_s=2.0, operator_label="You")
    r = pack.write_pack(ir, title="s", tags=[], embeddings={"S0": [1.0]}, audio={},
                        dest_dir=sub, md_path=sub / "transcript.md")
    pack.delete_pack(pack.load_pack(r.uid))
    assert not sub.exists()  # emptied subdir removed…
    assert paths.sessions_dir().exists()  # …but never the store itself


def test_link_voiceprints_writes_back_edge(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    pk = pack.load_pack(_two_speaker_pack(tmp_path).uid)
    vps = pack.extract(pk.blob_path)
    pk = pack.link_voiceprints(pk, vps)

    edges = pk.meta["voiceprints"]
    assert edges and all(e.startswith("../library/") and e.endswith(".md") for e in edges)
    # idempotent: re-linking the same voiceprints doesn't grow the edge list
    again = pack.link_voiceprints(pk, vps)
    assert again.meta["voiceprints"] == edges


def test_link_edge_depth_for_nested_session_dir(tmp_path, monkeypatch):
    # The ADR's load-bearing case: a `listen` pack lives in sessions/<id>/, so the edge back to
    # library/ must climb two levels (../../library/), not one.
    _isolate(tmp_path, monkeypatch)
    from transcribbler import paths

    sub = paths.ensure(paths.sessions_dir() / "20260708-141042")
    ir = build_live_ir([(0.0, 3.0, "Remote", "hi there")], PROF, duration_s=3.0, operator_label="You")
    r = pack.write_pack(ir, title="s", tags=["meeting"], embeddings={"S0": [0.0, 1.0, 0.0]},
                        audio={}, dest_dir=sub, md_path=sub / "transcript.md")
    pk = pack.load_pack(r.uid)
    pk = pack.link_voiceprints(pk, pack.extract(pk.blob_path))
    assert all(e.startswith("../../library/") for e in pk.meta["voiceprints"])


def test_rewrite_blob_drop_audio_strips_clips_keeps_record(tmp_path, monkeypatch):
    # The finalize primitive's other branch: drop_audio removes audio/ but keeps the record +
    # embeddings seed (so a finalized pack still extracts).
    _isolate(tmp_path, monkeypatch)
    pk = pack.load_pack(_two_speaker_pack(tmp_path).uid)
    ir = pack.read_record(pk.blob_path)
    pack._rewrite_blob(pk.blob_path, new_ir=ir, new_sidecar="---\n---\n", drop_audio=True)
    assert pack.pack_details(pk)["has_audio"] is False
    assert pack.extract(pk.blob_path)  # embeddings.json seed survived → still extractable
