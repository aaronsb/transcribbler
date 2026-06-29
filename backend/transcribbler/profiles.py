"""Compute profiles (ADR-0015).

A profile is declarative data describing one runnable backend on one host: per
stage (asr / diar / llm) the engine, device backend, binary, and model. Selecting
a profile selects the GPU path; adding a GPU is adding a profile, not editing code.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

VALID_BACKENDS = {"cuda", "rocm", "vulkan", "cpu"}

# Bundled, host-specific profiles shipped in the repo (repo_root/profiles).
# parents: [0]=transcribbler [1]=backend [2]=repo root.
BUNDLED_DIR = Path(__file__).resolve().parents[2] / "profiles"


@dataclass(frozen=True)
class StageConfig:
    engine: str  # e.g. "whisper.cpp", "pyannote", "llama.cpp", "none"
    backend: str = "cpu"  # cuda | rocm | vulkan | cpu
    binary: str | None = None
    model: str | None = None
    options: dict = field(default_factory=dict)

    @property
    def enabled(self) -> bool:
        return self.engine not in ("", "none")


@dataclass(frozen=True)
class Profile:
    name: str
    asr: StageConfig
    diar: StageConfig
    llm: StageConfig
    idle_ttl_s: int = 300


def _stage(raw: dict) -> StageConfig:
    backend = raw.get("backend", "cpu")
    if backend not in VALID_BACKENDS:
        raise ValueError(f"backend must be one of {sorted(VALID_BACKENDS)}, got {backend!r}")
    known = {"engine", "backend", "binary", "model"}
    return StageConfig(
        engine=raw.get("engine", "none"),
        backend=backend,
        binary=raw.get("binary"),
        model=raw.get("model"),
        options={k: v for k, v in raw.items() if k not in known},
    )


def load(path: str | Path) -> Profile:
    """Load and validate a profile from a TOML file."""
    path = Path(path)
    data = tomllib.loads(path.read_text())
    name = data.get("name") or path.stem
    return Profile(
        name=name,
        asr=_stage(data.get("asr", {})),
        diar=_stage(data.get("diar", {})),
        llm=_stage(data.get("llm", {})),
        idle_ttl_s=int(data.get("idle_ttl_s", 300)),
    )


# --- profile discovery (so the CLI feels like one binary you just run) ---


class ProfileError(Exception):
    """Could not resolve a profile to load."""


def config_dir() -> Path:
    """XDG config home for transcribbler (host-local profiles live here)."""
    base = os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")
    return Path(base) / "transcribbler"


def search_dirs() -> list[Path]:
    """Where bare profile names are resolved, in priority order."""
    return [config_dir() / "profiles", BUNDLED_DIR]


def available() -> list[str]:
    """Profile names discoverable by `-p <name>`, deduped, priority order."""
    names: list[str] = []
    for d in search_dirs():
        if d.is_dir():
            for f in sorted(d.glob("*.toml")):
                if f.stem not in names:
                    names.append(f.stem)
    return names


def _looks_like_path(arg: str) -> bool:
    return os.sep in arg or arg.endswith(".toml") or Path(arg).expanduser().exists()


def resolve(arg: str | None) -> Path:
    """Resolve a profile selector to a concrete .toml path.

    Order: explicit ``arg`` (a path, or a bare name searched on the path) →
    ``$TRANSCRIBBLER_PROFILE`` → auto-select by probed backend. Raises
    ``ProfileError`` (with the available names) when nothing matches.
    """
    selector = arg or os.environ.get("TRANSCRIBBLER_PROFILE")
    if selector:
        if _looks_like_path(selector):
            p = Path(selector).expanduser()
            if not p.exists():
                raise ProfileError(f"profile file not found: {p}")
            return p
        for d in search_dirs():
            cand = d / f"{selector}.toml"
            if cand.exists():
                return cand
        raise ProfileError(
            f"unknown profile {selector!r}; available: {', '.join(available()) or '(none)'}"
        )
    return auto_select()


def auto_select() -> Path:
    """Pick the bundled/host profile whose ASR backend matches the probed GPU."""
    from . import probe  # local import: probe has no profiles dependency

    backend = probe.detect().recommend()
    for d in search_dirs():
        if not d.is_dir():
            continue
        for f in sorted(d.glob("*.toml")):
            try:
                prof = load(f)
            except (OSError, ValueError, tomllib.TOMLDecodeError):
                continue
            if prof.asr.enabled and prof.asr.backend == backend:
                return f
    raise ProfileError(
        f"no profile matches the detected backend {backend!r}; "
        f"pass -p <name|path> (available: {', '.join(available()) or '(none)'})"
    )
