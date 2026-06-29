"""Compute cores — swappable inference engine adapters (ADR-0003, ADR-0015).

Each core implements a narrow interface so engines and devices are config, not
code. New engine = new adapter; nothing else moves.
"""
from __future__ import annotations

from ..profiles import StageConfig
from .base import ASRCore, Segment
from .whisper_cpp import WhisperCppCore


def asr_core(cfg: StageConfig) -> ASRCore:
    """Resolve an ASR core from its stage config."""
    if cfg.engine == "whisper.cpp":
        return WhisperCppCore(cfg)
    raise ValueError(f"unknown ASR engine: {cfg.engine!r}")


__all__ = ["ASRCore", "Segment", "WhisperCppCore", "asr_core"]
