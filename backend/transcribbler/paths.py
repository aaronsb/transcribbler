"""XDG-based application paths (user-scoped), first cut.

transcribbler stores its artifacts under the XDG base directories rather than the
working directory, so sessions and the voiceprint library accumulate in one known
place and never clutter the repo:

- **config** (`$XDG_CONFIG_HOME/transcribbler`) — user config + secrets (HF token).
- **data**   (`$XDG_DATA_HOME/transcribbler`)   — `sessions/<id>/` packs, `library/` corpus.
- **state**  (`$XDG_STATE_HOME/transcribbler`)  — `capture/<id>/` scratch workdirs, logs.
- **cache**  (`$XDG_CACHE_HOME/transcribbler`)  — regenerable model caches.

`$XDG_*_HOME` overrides are honored; otherwise the spec defaults apply. This is a
first effort — the layout is meant to be used and revised (feeds ADR-0028's realized
storage decision), not treated as settled.
"""

from __future__ import annotations

import os
from pathlib import Path

_APP = "transcribbler"


def _base(env_var: str, default: str) -> Path:
    override = os.environ.get(env_var)
    return (Path(override) if override else Path.home() / default) / _APP


def config_dir() -> Path:
    return _base("XDG_CONFIG_HOME", ".config")


def data_dir() -> Path:
    return _base("XDG_DATA_HOME", ".local/share")


def state_dir() -> Path:
    return _base("XDG_STATE_HOME", ".local/state")


def cache_dir() -> Path:
    return _base("XDG_CACHE_HOME", ".cache")


def sessions_dir() -> Path:
    """Durable per-session packs: `<data>/sessions/<id>/`."""
    return data_dir() / "sessions"


def library_dir() -> Path:
    """The voiceprint corpus / identity store: `<data>/library/`."""
    return data_dir() / "library"


def capture_dir() -> Path:
    """Ephemeral capture scratch workdirs: `<state>/capture/<id>/`."""
    return state_dir() / "capture"


def ensure(path: Path) -> Path:
    """Create ``path`` (and parents) if absent; return it."""
    path.mkdir(parents=True, exist_ok=True)
    return path
