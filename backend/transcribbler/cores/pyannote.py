"""pyannote diarizer core (ADR-0004, ADR-0005).

Runs the diarizer as a subprocess in its own torch-ROCm env (backend/diarizer)
so torch never enters the main backend. Normalizes audio to 16 kHz mono first,
invokes the sidecar, and parses its JSON into SpeakerTurns. The HF token is
passed through the environment.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path

from ..audio import normalize_wav
from ..profiles import StageConfig
from .base import SpeakerTurn

# backend/transcribbler/cores/pyannote.py -> backend/diarizer
_SIDECAR_DIR = Path(__file__).resolve().parents[2] / "diarizer"
_SIDECAR_SCRIPT = _SIDECAR_DIR / "diarize.py"


class PyannoteCore:
    name = "pyannote"

    def __init__(self, cfg: StageConfig):
        if not _SIDECAR_SCRIPT.exists():
            raise FileNotFoundError(f"diarizer sidecar not found: {_SIDECAR_SCRIPT}")
        if not os.environ.get("HF_TOKEN"):
            raise RuntimeError("HF_TOKEN not set (needed for the gated pyannote model)")
        self.model = cfg.model or "pyannote/speaker-diarization-community-1"

    def diarize(self, audio_path: Path) -> list[SpeakerTurn]:
        with tempfile.TemporaryDirectory(prefix="transcribbler_diar_") as tmp:
            wav = normalize_wav(audio_path, Path(tmp) / "norm.wav")
            payload = self._run_sidecar(wav)
        return [
            SpeakerTurn(start=t["start"], end=t["end"], label=t["label"]) for t in payload.get("turns", [])
        ]

    def _run_sidecar(self, wav: Path) -> dict:
        cmd = [
            "uv",
            "run",
            "--project",
            str(_SIDECAR_DIR),
            "python",
            str(_SIDECAR_SCRIPT),
            str(wav),
            "--model",
            self.model,
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, env=os.environ.copy())
        if proc.returncode != 0:
            raise RuntimeError(f"diarizer sidecar failed ({proc.returncode}): {proc.stderr[-800:]}")
        try:
            return json.loads(proc.stdout)
        except json.JSONDecodeError as e:
            raise RuntimeError(f"diarizer produced invalid JSON: {e}; stderr: {proc.stderr[-400:]}")
