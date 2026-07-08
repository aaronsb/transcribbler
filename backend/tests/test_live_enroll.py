"""Live enroll — flag a speaker mid-meeting, then name them into a voiceprint (ADR-0024).

Covers the whole Increment-1 seam: the ``Controls`` flag-to-remember gesture, the durable
``pending_enrollment`` it lands on the pack (through capture's persist path), the ``pack.set_pending``
/ ``extract(only=…)`` surgical primitives, and the guided ``pack enroll`` CLI walk that turns a
flagged anonymous remote into a named library voiceprint and clears the flag.
"""

from __future__ import annotations

import argparse
import builtins
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from transcribbler import cli, library, pack
from transcribbler.capture import Controls
from transcribbler.ir import build_live_ir

PROF = SimpleNamespace(
    asr=SimpleNamespace(engine="whisper", backend="cpp"),
    diar=SimpleNamespace(engine="pyannote", backend="community-1"),
)
STARTED = datetime(2026, 7, 8, 14, 12, 5, tzinfo=timezone.utc)


def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))  # redirects sessions/ + library/


def _silent_wav(path: Path, secs: float = 1.0) -> Path:
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
         "-i", "anullsrc=r=16000:cl=mono", "-t", str(secs), str(path)],
        check=True,
    )
    return path


def _pack_with_pending(tmp_path, *, pending=None, uid="abc123"):
    """A capture-shaped pack: operator You(S0) + anonymous remote(S1), both with a clip."""
    ir = build_live_ir(
        [(0.0, 2.0, "You", "morning"), (2.0, 5.0, "Remote", "hello everyone")],
        PROF, duration_s=5.0, operator_label="You", diarized=True,
    )
    ids = [s["id"] for s in ir["speakers"]]  # -> ["S0", "S1"]
    clip = _silent_wav(tmp_path / "clip.wav")
    return pack.write_pack(
        ir, title="Team standup", tags=["meeting"],
        embeddings={ids[0]: [1.0, 0.0, 0.0], ids[1]: [0.0, 1.0, 0.0]},
        audio={sid: clip for sid in ids}, started=STARTED, uid=uid, pending=pending,
    )


# ── Controls: the flag-to-remember gesture ────────────────────────────────────


def test_controls_flag_tracks_current_remote():
    c = Controls()
    assert c.flag_current() is None  # nobody has spoken yet — nothing to flag
    assert c.flagged == set()
    c.note_speaker("S2")
    assert c.flag_current() == "S2"  # flags whoever is talking now
    c.note_speaker("S3")
    c.flag_current()
    assert c.flagged == {"S2", "S3"}  # each keypress accretes the then-current speaker


def test_controls_flag_is_idempotent_per_speaker():
    c = Controls()
    c.note_speaker("S2")
    c.flag_current()
    c.flag_current()  # same speaker still current — a double-tap doesn't duplicate
    assert c.flagged == {"S2"}


# ── pending_enrollment on the pack ─────────────────────────────────────────────


def test_write_pack_stamps_pending_enrollment(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    _pack_with_pending(tmp_path, pending=["S1"])
    pk = pack.load_pack("abc123")
    assert pk.meta["pending_enrollment"] == ["S1"]


def test_write_pack_omits_pending_key_when_none(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    _pack_with_pending(tmp_path, pending=None)
    pk = pack.load_pack("abc123")
    assert "pending_enrollment" not in pk.meta


def test_persist_resolves_flagged_labels_to_ids_and_drops_absent(tmp_path, monkeypatch):
    # The capture→pack seam: flagged live *labels* become canonical pack *ids*, and a label
    # that never reached the transcript (so has no IR speaker) is dropped, not mis-attributed.
    _isolate(tmp_path, monkeypatch)
    from transcribbler import capture_persist

    turns = [(0.0, 2.0, "You", "hi"), (2.0, 4.0, "S1", "hello"), (4.0, 6.0, "S2", "hey")]
    result = capture_persist.persist_session_pack(
        turns, PROF, operator_label="You", diarized=True, centroids={},
        session_wav=None, out_path=tmp_path / "t.md",
        flagged={"S1", "S2", "S9-never-spoke"},
    )
    pk = pack.load_pack(str(result.blob_path))  # lands beside out_path, not in the sessions store
    # S1/S2 are gallery labels → kept as ids; the phantom label is silently dropped
    assert pk.meta["pending_enrollment"] == ["S1", "S2"]


# ── set_pending: durable walk progress ─────────────────────────────────────────


def test_set_pending_updates_and_clears(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    _pack_with_pending(tmp_path, pending=["S1"])
    pk = pack.load_pack("abc123")

    pk = pack.set_pending(pk, [])  # naming the last speaker clears the key entirely
    assert "pending_enrollment" not in pack.load_pack("abc123").meta
    assert "pending_enrollment" not in pk.meta

    pk = pack.set_pending(pk, ["S1"])  # and it can be set back on
    assert pack.load_pack("abc123").meta["pending_enrollment"] == ["S1"]


def test_set_pending_noop_when_unchanged(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    _pack_with_pending(tmp_path, pending=["S1"])
    pk = pack.load_pack("abc123")
    same = pack.set_pending(pk, ["S1"])  # same list → skip the O(pack) rewrite
    assert same is pk


# ── extract(only=…): fold just the named speaker ───────────────────────────────


def test_extract_only_folds_the_named_speaker_alone(tmp_path, monkeypatch):
    # Naming one flagged remote must not drag the un-named operator/others into the library.
    _isolate(tmp_path, monkeypatch)
    pk = pack.load_pack(_pack_with_pending(tmp_path).uid)
    pk = pack.relabel(pk, "S1", "Priya")

    vps = pack.extract(pk.blob_path, only=["S1"])
    assert {vp.name for vp in vps} == {"Priya"}
    assert library.find_by_name("Priya") is not None
    assert library.find_by_name("You") is None  # the operator was flagged nothing → not folded


# ── the guided pack enroll walk (CLI) ──────────────────────────────────────────


def _script_input(monkeypatch, answers):
    """Feed ``answers`` to successive ``input()`` calls inside the walk."""
    it = iter(answers)
    monkeypatch.setattr(builtins, "input", lambda *_a, **_k: next(it))


def test_pack_enroll_walk_names_flagged_speaker_and_clears_pending(tmp_path, monkeypatch, capsys):
    _isolate(tmp_path, monkeypatch)
    _pack_with_pending(tmp_path, pending=["S1"])
    # Enter (skip audition) → "Priya" (name it)
    _script_input(monkeypatch, ["", "Priya"])

    rc = cli._cmd_pack_enroll(argparse.Namespace(uid="abc123", speaker=[]))
    assert rc == 0

    assert library.find_by_name("Priya") is not None            # folded into the library
    reloaded = pack.load_pack("abc123")
    assert "pending_enrollment" not in reloaded.meta            # flag cleared
    s1 = next(s for s in pack.read_record(reloaded.blob_path)["speakers"] if s["id"] == "S1")
    assert s1["display_name"] == "Priya"                        # relabel persisted


def test_pack_enroll_walk_skip_keeps_speaker_pending(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    _pack_with_pending(tmp_path, pending=["S1"])
    _script_input(monkeypatch, ["", ""])  # Enter (skip audition), blank name → skip

    cli._cmd_pack_enroll(argparse.Namespace(uid="abc123", speaker=[]))
    assert pack.load_pack("abc123").meta["pending_enrollment"] == ["S1"]  # still flagged
    assert library.find_by_name("Priya") is None


def test_pack_enroll_explicit_speaker_overrides_pending(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    _pack_with_pending(tmp_path, pending=[])  # nothing flagged
    _script_input(monkeypatch, ["", "Sam"])

    rc = cli._cmd_pack_enroll(argparse.Namespace(uid="abc123", speaker=["S1"]))
    assert rc == 0
    assert library.find_by_name("Sam") is not None


def test_pack_enroll_no_targets_is_a_noop(tmp_path, monkeypatch, capsys):
    _isolate(tmp_path, monkeypatch)
    _pack_with_pending(tmp_path, pending=[])
    rc = cli._cmd_pack_enroll(argparse.Namespace(uid="abc123", speaker=[]))
    assert rc == 0
    assert "no speakers flagged" in capsys.readouterr().out
