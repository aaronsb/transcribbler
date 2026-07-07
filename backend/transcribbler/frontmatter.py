"""Minimal YAML frontmatter emit/parse for a small, closed set of flat fields.

Both the session pack (``pack.py``) and the voiceprint library (``library.py``) persist
OKF documents — YAML frontmatter over a human body — but the backend deliberately carries no
YAML dependency (and ``library.py`` can't import ``pack``, which imports it). This is the shared,
hand-rolled emitter/parser for the *closed* schema those docs use: flat scalars (quoted strings,
bare ints/bools) and lists of scalars. It round-trips what it writes; it is **not** a general
YAML implementation, and only needs to read documents this module produced.
"""

from __future__ import annotations


def _scalar(value: object) -> str:
    """Render one scalar, preserving its YAML type.

    Ints/bools emit bare; everything else is double-quoted (``"``/``\\`` escaped) so a reader sees
    the intended *string* — otherwise ``id: 203346`` parses as an int, ``spec_version: 0.1`` as a
    float, ``timestamp: …Z`` as a date, and a value containing ``:`` or a leading ``#`` breaks.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


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


def _unquote(token: str) -> str:
    out: list[str] = []
    i = 0
    while i < len(token):
        if token[i] == "\\" and i + 1 < len(token):
            out.append(token[i + 1])
            i += 2
        else:
            out.append(token[i])
            i += 1
    return "".join(out)


def _unscalar(token: str) -> object:
    if len(token) >= 2 and token[0] == '"' and token[-1] == '"':
        return _unquote(token[1:-1])
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
    for line in lines[1:]:
        if line.strip() == "---":
            break
        block.append(line)

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
