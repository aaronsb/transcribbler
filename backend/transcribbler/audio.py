"""Audio normalization (ADR-0005).

One place for "normalize to 16 kHz mono WAV" — the form both whisper.cpp and
pyannote expect. Shared so cores don't each reimplement it.
"""
from __future__ import annotations

import subprocess
from pathlib import Path


def normalize_wav(src: Path, dst: Path) -> Path:
    """ffmpeg → 16 kHz mono WAV. Returns dst."""
    cmd = ["ffmpeg", "-y", "-i", str(src), "-vn", "-ar", "16000", "-ac", "1", str(dst)]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg normalize failed: {proc.stderr[-500:]}")
    return dst
