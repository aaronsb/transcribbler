"""Unit tests for the live-session Canonical IR builder (ADR-0028 spike)."""

from __future__ import annotations

from types import SimpleNamespace

from transcribbler.ir import build_live_ir
from transcribbler.render import to_markdown

# build_live_ir only reads profile.asr.{engine,backend} and profile.diar.{engine,backend}
PROF = SimpleNamespace(
    asr=SimpleNamespace(engine="whisper", backend="cpp"),
    diar=SimpleNamespace(engine="pyannote", backend="community-1"),
)


def test_live_ir_is_session_kind_without_source_file():
    ir = build_live_ir([(0.0, 2.0, "S1", "hello")], PROF, duration_s=2.0)
    assert ir["source"]["kind"] == "session"
    assert "uri" not in ir["source"]  # schema permits this for kind=session
    assert "sha256" not in ir["source"]


def test_live_ir_maps_specials_and_keeps_gallery_ids():
    turns = [(0.0, 2.0, "You", "hi"), (2.0, 4.0, "S1", "hello"), (4.0, 6.0, "Remote", "noise")]
    ir = build_live_ir(turns, PROF, duration_s=6.0)  # validate=True by default
    by_name = {s.get("display_name") or s["id"]: s for s in ir["speakers"]}
    assert by_name["S1"]["id"] == "S1"  # gallery id preserved
    assert by_name["You"]["id"] == "S0"  # operator takes the free S0
    assert by_name["Remote"]["id"] not in ("S0", "S1")  # no collision
    ids = [s["id"] for s in ir["speakers"]]
    assert len(ids) == len(set(ids))


def test_live_ir_operator_tagged_manual_others_fallback():
    ir = build_live_ir([(0.0, 1.0, "You", "hi"), (1.0, 2.0, "S1", "yo")], PROF, duration_s=2.0)
    src = {s.get("display_name") or s["id"]: s["source"] for s in ir["speakers"]}
    assert src["You"] == "manual"
    assert src["S1"] == "fallback"


def test_live_ir_renders_to_markdown():
    turns = [(0.0, 2.0, "You", "morning"), (2.0, 4.0, "S1", "hello there")]
    md = to_markdown(build_live_ir(turns, PROF, duration_s=4.0))
    assert "hello there" in md
    assert "You" in md  # display_name resolved by the renderer


def test_live_ir_undiarized_omits_diarizer_backend():
    ir = build_live_ir([(0.0, 1.0, "Remote", "x")], PROF, duration_s=1.0, diarized=False)
    assert "diarizer" not in ir["backend"]
