"""Unit tests for the durable voiceprint store (library.py)."""

from __future__ import annotations

import json

import pytest

from transcribbler import frontmatter, library, paths

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


def test_reenroll_rejects_dimensionality_mismatch(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    library.enroll("Aaron", E1)  # 3-d
    with pytest.raises(ValueError, match="dim"):
        library.enroll("Aaron", [0.1, 0.2, 0.3, 0.4])  # a 4-d embedding must not truncate


def test_save_writes_md_record_and_sibling_vector(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    library.enroll("Priya", [0.1, 0.2, 0.3], uid="aaaaaa-priya", source="../s/x.tar.gz")
    lib = paths.library_dir()
    md, vec, legacy = lib / "aaaaaa-priya.md", lib / "aaaaaa-priya.vec.json", lib / "aaaaaa-priya.json"
    assert md.exists() and vec.exists() and not legacy.exists()  # OKF md + sibling vector, no json
    m = frontmatter.parse(md.read_text())
    assert m["type"] == "voiceprint" and m["uid"] == "aaaaaa-priya" and m["samples"] == 1
    assert m["sources"] == ["../s/x.tar.gz"]  # graph edge in the frontmatter
    assert json.loads(vec.read_text()) == [0.1, 0.2, 0.3]  # vector out of the YAML


def test_reads_and_migrates_a_legacy_json_record(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    paths.ensure(paths.library_dir())
    legacy = paths.library_dir() / "olduid.json"  # pre-md record, no `sources` key
    legacy.write_text(json.dumps(
        {"uid": "olduid", "name": "Legacy", "centroid": E1, "samples": 5,
         "updated": "2026-01-01T00:00:00+00:00"}
    ))
    vp = library.load("olduid")  # read-both: legacy still loads
    assert vp is not None and vp.name == "Legacy" and vp.samples == 5 and vp.sources == []
    assert library.find_by_name("legacy").uid == "olduid"  # load_all sees legacy too

    library.enroll("Legacy", E2)  # re-enroll folds → save → migrates the record
    assert not legacy.exists() and (paths.library_dir() / "olduid.md").exists()
    assert len(library.load_all()) == 1  # migrated, not duplicated across formats


def test_corrupt_record_is_skipped_not_crash(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    library.enroll("Good", E1, uid="good")
    # a truncated sibling vector must not take down the whole library (review finding #1)
    (paths.library_dir() / "bad.md").write_text(
        frontmatter.emit({"uid": "bad", "type": "voiceprint", "name": "Bad", "samples": 1})
    )
    (paths.library_dir() / "bad.vec.json").write_text("[1.0, 0.0,")  # truncated JSON
    names = {vp.name for vp in library.load_all()}  # must not raise
    assert names == {"Good"}  # the good record survives; the corrupt one is skipped


def test_incomplete_md_does_not_shadow_legacy(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    lib = paths.ensure(paths.library_dir())
    # simulate a crashed migration: a .md exists WITHOUT its .vec, and the legacy .json survives
    (lib / "olduid.json").write_text(json.dumps(
        {"uid": "olduid", "name": "Legacy", "centroid": E1, "samples": 3, "updated": "x"}
    ))
    (lib / "olduid.md").write_text(frontmatter.emit({"uid": "olduid", "name": "Legacy"}))
    vps = library.load_all()  # incomplete md must not hide the still-valid legacy record
    assert [vp.name for vp in vps] == ["Legacy"] and vps[0].samples == 3


def test_scalar_sources_is_wrapped_not_splatted(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    lib = paths.ensure(paths.library_dir())
    (lib / "u.md").write_text(frontmatter.emit(
        {"uid": "u", "name": "N", "samples": 1, "sources": "../s/x.tar.gz"}  # scalar, not a list
    ))
    (lib / "u.vec.json").write_text(json.dumps(E1))
    vp = library.load("u")
    assert vp.sources == ["../s/x.tar.gz"]  # not ['.', '.', '/', 's', ...] (finding #6)


def test_best_match_picks_nearest_over_threshold(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    library.enroll("Aaron", E1)
    library.enroll("Clayton", E2)
    m = library.best_match([0.95, 0.05, 0.0], threshold=0.5)
    assert m is not None and m[0].name == "Aaron"
    assert library.best_match([0.0, 0.0, 1.0], threshold=0.5) is None  # orthogonal → no match
