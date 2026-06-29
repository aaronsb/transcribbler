"""Compute cores — swappable inference engine adapters (ADR-0003, ADR-0015).

Each core implements a narrow interface so engines and devices are config, not
code. New engine = new adapter; nothing else moves.
"""

from __future__ import annotations

from ..profiles import StageConfig
from .base import ASRCore, DiarizerCore, Segment, SpeakerTurn
from .llama_canon import LlamaCanonicalizer
from .pyannote import PyannoteCore
from .whisper_cpp import WhisperCppCore


def asr_core(cfg: StageConfig) -> ASRCore:
    """Resolve an ASR core from its stage config."""
    if cfg.engine == "whisper.cpp":
        return WhisperCppCore(cfg)
    raise ValueError(f"unknown ASR engine: {cfg.engine!r}")


def diarizer_core(cfg: StageConfig) -> DiarizerCore:
    """Resolve a diarizer core from its stage config."""
    if cfg.engine == "pyannote":
        return PyannoteCore(cfg)
    raise ValueError(f"unknown diarizer engine: {cfg.engine!r}")


def canonicalizer_core(cfg: StageConfig) -> LlamaCanonicalizer:
    """Resolve a canonicalizer core from its stage config."""
    if cfg.engine in ("llama.cpp", "llama-server"):
        return LlamaCanonicalizer(cfg)
    raise ValueError(f"unknown canonicalizer engine: {cfg.engine!r}")


__all__ = [
    "ASRCore",
    "DiarizerCore",
    "Segment",
    "SpeakerTurn",
    "WhisperCppCore",
    "PyannoteCore",
    "LlamaCanonicalizer",
    "asr_core",
    "diarizer_core",
    "canonicalizer_core",
]
