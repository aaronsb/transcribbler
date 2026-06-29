"""Compute profiles (ADR-0015).

A profile is declarative data describing one runnable backend on one host: per
stage (asr / diar / llm) the engine, device backend, binary, and model. Selecting
a profile selects the GPU path; adding a GPU is adding a profile, not editing code.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

VALID_BACKENDS = {"cuda", "rocm", "vulkan", "cpu"}


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
