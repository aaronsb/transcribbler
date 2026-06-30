"""Core adapter interface (ADR-0015).

Deliberately tiny so engines stay swappable. An ASR core turns audio into
time-stamped text segments; it knows nothing about diarization, the IR, or HTTP.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ..progress import ProgressSink


@dataclass(frozen=True)
class Segment:
    start: float  # seconds, absolute within the audio
    end: float
    text: str


@dataclass(frozen=True)
class SpeakerTurn:
    """A diarizer's verdict: speaker `label` is active over [start, end].

    `label` is the engine's *local* speaker id (e.g. "0", "SPEAKER_01"); it is
    mapped to a canonical, recording-wide id (S1, S2, ...) during alignment.
    """

    start: float
    end: float
    label: str


class ASRCore(Protocol):
    name: str

    def transcribe(
        self, audio_path: Path, *, progress: ProgressSink | None = None, prompt: str | None = None
    ) -> list[Segment]:
        """Transcribe a normalized audio file into chronological segments.

        ``progress``, if given, receives :class:`~transcribbler.progress.ProgressEvent`s
        as decoding proceeds. ``prompt`` is an optional initial prompt biasing
        decoding (names/jargon).
        """
        ...


class DiarizerCore(Protocol):
    name: str

    def diarize(self, audio_path: Path, *, progress: ProgressSink | None = None) -> list[SpeakerTurn]:
        """Diarize a normalized audio file into speaker turns over the whole file.

        ``progress``, if given, receives :class:`~transcribbler.progress.ProgressEvent`s.
        """
        ...
