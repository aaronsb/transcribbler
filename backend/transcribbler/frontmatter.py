"""Minimal YAML frontmatter emit/parse for a small, closed set of flat fields.

Both the session pack (``pack.py``) and the voiceprint library (``library.py``) persist
OKF documents — YAML frontmatter over a human body — but the backend deliberately carries no
YAML dependency (and ``library.py`` can't import ``pack``, which imports it). This is the shared,
hand-rolled emitter/parser for the *closed* schema those docs use: flat scalars (quoted strings,
bare ints/bools) and lists of scalars. It round-trips what it writes; it is **not** a general
YAML implementation, and only needs to read documents this module produced.
"""

from __future__ import annotations

import json


def _scalar(value: object) -> str:
    """Render one scalar, preserving its YAML type.

    Ints/bools emit bare; every other value is emitted as a **JSON string** — which is a valid
    YAML double-quoted scalar — so a reader sees the intended *string* (``id: 203346`` stays a
    string, not an int; ``spec_version: 0.1`` not a float; ``timestamp: …Z`` not a date) and *all*
    special characters are escaped, including quotes, backslashes, and newlines (a raw newline
    would otherwise split the value across physical lines and break the round-trip).
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    return json.dumps(str(value))


def emit(meta: dict) -> str:
    """Serialize a flat mapping to a ``---``-fenced YAML frontmatter block (trailing newline).

    Values: scalars emit ``key: <scalar>``; lists emit a block sequence (``[]`` when empty);
    ``None`` values are dropped. Deterministic and stable/diffable (insertion order preserved).
    """
    lines = ["---"]
    for key, value in meta.items():
        if value is None:
            continue
        if isinstance(value, list):
            if not value:
                lines.append(f"{key}: []")
            else:
                lines.append(f"{key}:")
                lines.extend(f"  - {_scalar(item)}" for item in value)
        else:
            lines.append(f"{key}: {_scalar(value)}")
    lines.append("---")
    return "\n".join(lines) + "\n"


def _unscalar(token: str) -> object:
    if len(token) >= 2 and token[0] == '"' and token[-1] == '"':
        try:
            return json.loads(token)  # inverse of _scalar's json.dumps
        except json.JSONDecodeError:
            return token[1:-1]  # tolerate a hand-edited/damaged quoted value
    if token in ("true", "false"):
        return token == "true"
    try:
        return int(token)
    except ValueError:
        return token


def parse(text: str) -> dict:
    """Read a frontmatter block this module emitted → a flat mapping (inverse of :func:`emit`).

    Reads only the leading ``---``-fenced block; any Markdown body after it is ignored. Returns
    ``{}`` if the text has no frontmatter. Understands exactly what :func:`emit` writes: quoted
    strings (type-preserved), bare ints/bools, ``[]``, and ``- item`` block sequences.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    block: list[str] = []
    closed = False
    for line in lines[1:]:
        if line.strip() == "---":
            closed = True
            break
        block.append(line)
    if not closed:  # no closing fence → not a real frontmatter block; don't parse the body
        return {}

    meta: dict = {}
    i = 0
    while i < len(block):
        line = block[i]
        if not line.strip():
            i += 1
            continue
        key, _, rest = line.partition(":")
        key, rest = key.strip(), rest.strip()
        if rest == "":  # a block sequence follows
            items: list[object] = []
            j = i + 1
            while j < len(block) and block[j].lstrip().startswith("- "):
                items.append(_unscalar(block[j].lstrip()[2:].strip()))
                j += 1
            meta[key] = items
            i = j
        elif rest == "[]":
            meta[key] = []
            i += 1
        else:
            meta[key] = _unscalar(rest)
            i += 1
    return meta
