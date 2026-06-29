"""Core adapter interface (ADR-0015).

Deliberately tiny so engines stay swappable. An ASR core turns audio into
time-stamped text segments; it knows nothing about diarization, the IR, or HTTP.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class Segment:
    start: float  # seconds, absolute within the audio
    end: float
    text: str


class ASRCore(Protocol):
    name: str

    def transcribe(self, audio_path: Path) -> list[Segment]:
        """Transcribe a normalized audio file into chronological segments."""
        ...
