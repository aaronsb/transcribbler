"""Unit tests for the shared hand-rolled frontmatter emit/parse (frontmatter.py)."""

from __future__ import annotations

from transcribbler import frontmatter


def test_round_trips_scalars_and_lists():
    meta = {
        "spec_version": "0.1",
        "uid": "aaaaaa-priya",
        "samples": 3,
        "flag": True,
        "sources": ["../a.tar.gz", "../b.tar.gz"],
        "empty": [],
    }
    assert frontmatter.parse(frontmatter.emit(meta)) == meta


def test_preserves_strings_that_look_numeric():
    parsed = frontmatter.parse(frontmatter.emit({"id": "203346", "ver": "0.1"}))
    assert parsed["id"] == "203346" and isinstance(parsed["id"], str)
    assert parsed["ver"] == "0.1" and isinstance(parsed["ver"], str)  # not the float 0.1


def test_round_trips_yaml_special_and_escaped_chars():
    meta = {"title": 'Dr: "Bob" # hash', "path": "a\\b"}
    assert frontmatter.parse(frontmatter.emit(meta)) == meta


def test_ignores_markdown_body_after_the_fence():
    text = frontmatter.emit({"name": "Priya"}) + "\n# Priya\n\nRecurring participant.\n"
    assert frontmatter.parse(text) == {"name": "Priya"}


def test_no_frontmatter_returns_empty():
    assert frontmatter.parse("# just a heading\n\nbody\n") == {}


def test_none_values_are_dropped():
    assert "gone" not in frontmatter.emit({"kept": "x", "gone": None})
