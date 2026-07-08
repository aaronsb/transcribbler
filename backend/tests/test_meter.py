"""Unit tests for the audio-source meter's pure helpers (meter.py)."""

from __future__ import annotations

from transcribbler import capture, meter
from transcribbler.capture import Paths


def test_bar_maps_db_to_fill():
    assert meter._bar(0.0, width=24) == "█" * 24  # loud → full
    assert meter._bar(-60.0, width=24) == "░" * 24  # at floor → empty
    assert meter._bar(-120.0, width=24) == "░" * 24  # below floor → clamped empty
    half = meter._bar(-30.0, width=24)
    assert len(half) == 24 and half.count("█") == 12  # midpoint → half


def test_short_trims_monitor_suffix_and_truncates():
    assert meter._short("alsa_output.x.monitor") == "alsa_output.x"
    long = "a" * 60
    assert meter._short(long).startswith("…") and len(meter._short(long)) == 40


def test_recommend_picks_loudest_per_role():
    srcs = [
        meter.Source("mon_a.monitor", "meeting", auto=True),
        meter.Source("mon_b.monitor", "meeting", auto=False),
        meter.Source("mic_a", "mic", auto=True),
    ]
    peak = {"mon_a.monitor": -40.0, "mon_b.monitor": -12.0, "mic_a": -20.0}
    rec = meter._recommend(srcs, peak)
    assert rec["meeting"] == "mon_b.monitor"  # loudest meeting by peak, not the auto pick
    assert rec["mic"] == "mic_a"


def test_recommend_omits_roles_never_heard():
    # nothing rose above the floor → no confident route to suggest (review LOW)
    srcs = [meter.Source("m.monitor", "meeting", auto=True), meter.Source("mic_a", "mic", auto=True)]
    silent = {"m.monitor": -120.0, "mic_a": -80.0}  # both below _FLOOR_DB (-60)
    assert meter._recommend(srcs, silent) == {}


def test_candidates_flags_detection_pick(monkeypatch):
    short = {"sinks": {"0": "sinkA", "1": "sinkB"}, "sources": {"0": "micA"}}
    blocks = {
        "sink-inputs": [{"route": "0", "app": "Google Chrome"}],
        "source-outputs": [{"route": "0", "app": "Google Chrome"}],
    }
    defaults = {"get-default-sink": "sinkB\n", "get-default-source": "micA\n"}
    monkeypatch.setattr(capture, "_short_map", lambda kind: short[kind])
    monkeypatch.setattr(capture, "_blocks", lambda kind: blocks[kind])
    monkeypatch.setattr(capture, "_pactl", lambda *a: defaults[a[0]])
    monkeypatch.setattr(capture, "detect_paths", lambda app: Paths(mic="micA", meeting=("sinkA.monitor",)))

    srcs = meter.candidates("Google Chrome")
    by_name = {s.name: s for s in srcs}
    assert by_name["sinkA.monitor"].role == "meeting" and by_name["sinkA.monitor"].auto  # detection's pick
    assert by_name["sinkB.monitor"].role == "meeting" and not by_name["sinkB.monitor"].auto  # default sink
    assert by_name["micA"].role == "mic" and by_name["micA"].auto
