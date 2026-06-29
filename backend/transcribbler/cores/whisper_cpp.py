"""whisper.cpp ASR core (ADR-0004).

Shells out to the whisper.cpp `whisper-cli` binary. The device (Vulkan/CUDA/ROCm)
is chosen at *build* time of whisper.cpp; this adapter just runs whichever binary
the profile points at. Input audio is normalized to 16 kHz mono WAV first
(ADR-0005), which is what whisper.cpp expects.
"""
from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

from ..profiles import StageConfig
from .base import Segment


class WhisperCppCore:
    name = "whisper.cpp"

    def __init__(self, cfg: StageConfig):
        if not cfg.binary or not Path(cfg.binary).exists():
            raise FileNotFoundError(f"whisper.cpp binary not found: {cfg.binary}")
        if not cfg.model or not Path(cfg.model).exists():
            raise FileNotFoundError(f"whisper.cpp model not found: {cfg.model}")
        self.binary = cfg.binary
        self.model = cfg.model

    def transcribe(self, audio_path: Path) -> list[Segment]:
        with tempfile.TemporaryDirectory(prefix="transcribbler_") as tmp:
            wav = _normalize(audio_path, Path(tmp) / "norm.wav")
            out_prefix = Path(tmp) / "out"
            self._run(wav, out_prefix)
            return _parse(out_prefix.with_suffix(".json"))

    def _run(self, wav: Path, out_prefix: Path) -> None:
        cmd = [
            self.binary,
            "-m", self.model,
            "-f", str(wav),
            "-oj",                      # JSON output
            "-of", str(out_prefix),     # output file prefix
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(f"whisper-cli failed ({proc.returncode}): {proc.stderr[-500:]}")


def _normalize(src: Path, dst: Path) -> Path:
    """ffmpeg → 16 kHz mono WAV (ADR-0005 normalization)."""
    cmd = ["ffmpeg", "-y", "-i", str(src), "-vn", "-ar", "16000", "-ac", "1", str(dst)]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg normalize failed: {proc.stderr[-500:]}")
    return dst


def _parse(json_path: Path) -> list[Segment]:
    data = json.loads(json_path.read_text())
    segments = []
    for item in data.get("transcription", []):
        offsets = item.get("offsets", {})
        text = item.get("text", "").strip()
        if not text:
            continue
        segments.append(
            Segment(
                start=offsets.get("from", 0) / 1000.0,
                end=offsets.get("to", 0) / 1000.0,
                text=text,
            )
        )
    return segments
