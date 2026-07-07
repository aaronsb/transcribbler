"""Unit tests for the durable voiceprint store (library.py)."""

from __future__ import annotations

from transcribbler import library

E1 = [1.0, 0.0, 0.0]
E2 = [0.0, 1.0, 0.0]


def _isolate(tmp_path, monkeypatch):
    # paths.* read $XDG_DATA_HOME at call time, so this redirects the whole library
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))


def test_enroll_creates_and_reloads(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    vp = library.enroll("Aaron", E1)
    assert vp.name == "Aaron" and vp.samples == 1
    again = library.find_by_name("aaron")  # case-insensitive
    assert again is not None and again.uid == vp.uid


def test_reenroll_compounds_the_centroid(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    library.enroll("Aaron", E1)
    vp = library.enroll("Aaron", E2)  # folds → running mean of E1, E2
    assert vp.samples == 2
    assert abs(vp.centroid[0] - 0.5) < 1e-9
    assert abs(vp.centroid[1] - 0.5) < 1e-9
    assert len(library.load_all()) == 1  # same person, not a duplicate


def test_best_match_picks_nearest_over_threshold(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    library.enroll("Aaron", E1)
    library.enroll("Clayton", E2)
    m = library.best_match([0.95, 0.05, 0.0], threshold=0.5)
    assert m is not None and m[0].name == "Aaron"
    assert library.best_match([0.0, 0.0, 1.0], threshold=0.5) is None  # orthogonal → no match
