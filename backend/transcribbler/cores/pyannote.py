"""pyannote diarizer core (ADR-0004, ADR-0005).

Runs the diarizer as a subprocess in its own torch-ROCm env (backend/diarizer)
so torch never enters the main backend. Normalizes audio to 16 kHz mono first,
invokes the sidecar, and parses its JSON into SpeakerTurns. The HF token is
passed through the environment.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from ..audio import normalize_wav
from ..profiles import StageConfig
from ..progress import ProgressEvent, ProgressSink, line_tap
from .base import SpeakerTurn
from .proc import run_streamed

# backend/transcribbler/cores/pyannote.py -> backend/diarizer
_SIDECAR_DIR = Path(__file__).resolve().parents[2] / "diarizer"
_SIDECAR_SCRIPT = _SIDECAR_DIR / "diarize.py"


def _parse_progress(line: str) -> ProgressEvent | None:
    """Parse the sidecar's `@@P@@\tstep\tcompleted\ttotal` lines into a ProgressEvent."""
    if not line.startswith("@@P@@\t"):
        return None  # human log line: kept in the error tail, not echoed
    parts = line.rstrip("\n").split("\t")
    if len(parts) != 4:
        return None
    _, step, completed, total = parts
    try:
        return ProgressEvent(stage="diar", step=step, completed=float(completed), total=float(total))
    except ValueError:
        return None


class PyannoteCore:
    name = "pyannote"

    def __init__(self, cfg: StageConfig):
        if not _SIDECAR_SCRIPT.exists():
            raise FileNotFoundError(f"diarizer sidecar not found: {_SIDECAR_SCRIPT}")
        if not os.environ.get("HF_TOKEN"):
            raise RuntimeError("HF_TOKEN not set (needed for the gated pyannote model)")
        self.model = cfg.model or "pyannote/speaker-diarization-community-1"

    def diarize(self, audio_path: Path, *, progress: ProgressSink | None = None) -> list[SpeakerTurn]:
        with tempfile.TemporaryDirectory(prefix="transcribbler_diar_") as tmp:
            wav = normalize_wav(audio_path, Path(tmp) / "norm.wav")
            payload = self._run_sidecar(wav, progress=progress)
        return [
            SpeakerTurn(start=t["start"], end=t["end"], label=t["label"]) for t in payload.get("turns", [])
        ]

    def _run_sidecar(self, wav: Path, *, progress: ProgressSink | None) -> dict:
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
        if progress is not None:
            cmd.append("--progress")
        on_line = line_tap(_parse_progress, progress) if progress is not None else None
        rc, out, tail = run_streamed(
            cmd, stream=progress is not None, env=os.environ.copy(), on_line=on_line
        )
        if rc != 0:
            raise RuntimeError(f"diarizer sidecar failed ({rc}): {tail[-800:]}")
        try:
            return json.loads(out)
        except json.JSONDecodeError as e:
            raise RuntimeError(f"diarizer produced invalid JSON: {e}; stderr: {tail[-400:]}")
