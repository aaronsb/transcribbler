"""Client for the persistent diarizer daemon (ADR-0024).

Owns the pyannote sidecar running in `--serve` mode: starts it once, waits for the
`ready` handshake, then diarizes chunks over a stdin/stdout JSON-lines protocol —
no ~9-13s model reload per chunk. Each call returns turns + per-speaker embeddings
for session-stable stitching.

The daemon's stderr is drained to a log file (not an unread PIPE that would fill and
stall it — the same failure the capture loop avoids); stdout carries only the JSON
responses.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

# backend/transcribbler/diarizer_daemon.py -> backend/diarizer
_SIDECAR_DIR = Path(__file__).resolve().parents[1] / "diarizer"
_SIDECAR_SCRIPT = _SIDECAR_DIR / "diarize.py"
_DEFAULT_MODEL = "pyannote/speaker-diarization-community-1"


class DiarizerDaemon:
    """A warm pyannote diarizer: ``start()`` once, ``diarize(wav)`` per chunk, ``close()``."""

    def __init__(
        self,
        model: str | None,
        workdir: Path,
        *,
        log: Callable[[str], None] | None = None,
        ready_timeout: float = 180.0,
    ):
        self.model = model or _DEFAULT_MODEL
        self.log_path = workdir / "diarizer.log"
        self._say = log or (lambda _m: None)
        self._ready_timeout = ready_timeout
        self._proc: subprocess.Popen | None = None
        self._errf = None

    def start(self) -> None:
        if not _SIDECAR_SCRIPT.exists():
            raise FileNotFoundError(f"diarizer sidecar not found: {_SIDECAR_SCRIPT}")
        if not os.environ.get("HF_TOKEN"):
            raise RuntimeError("HF_TOKEN not set (needed for the gated pyannote model)")
        self._errf = self.log_path.open("wb")
        cmd = [
            "uv", "run", "--project", str(_SIDECAR_DIR),
            "python", str(_SIDECAR_SCRIPT), "--serve", "--model", self.model,
        ]
        self._say(f"  starting diarizer daemon ({self.model}) — one-time model load…")
        # start_new_session: a Ctrl-C/SIGINT to the capture process group must not
        # reach the daemon, so it survives to diarize the final drained chunks;
        # close() stops it explicitly.
        self._proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self._errf,
            text=True, env=os.environ.copy(), start_new_session=True,
        )
        deadline = time.monotonic() + self._ready_timeout
        while time.monotonic() < deadline:
            if self._proc.poll() is not None:
                raise RuntimeError(
                    f"diarizer daemon exited during startup ({self._proc.returncode}); see {self.log_path}"
                )
            if self.log_path.exists() and b"\nready\n" in b"\n" + self.log_path.read_bytes():
                self._say("  diarizer daemon ready")
                return
            time.sleep(0.5)
        raise RuntimeError(f"diarizer daemon did not signal ready within {self._ready_timeout:.0f}s")

    def diarize(self, wav: Path) -> dict:
        """Diarize one wav → ``{"turns": [...], "speakers": [{"label","embedding"}], ...}``."""
        if self._proc is None or self._proc.poll() is not None:
            raise RuntimeError("diarizer daemon is not running")
        self._proc.stdin.write(f"{wav}\n")
        self._proc.stdin.flush()
        line = self._proc.stdout.readline()
        if not line:
            raise RuntimeError(f"diarizer daemon closed the pipe; see {self.log_path}")
        result = json.loads(line)
        if "error" in result:
            raise RuntimeError(f"diarizer: {result['error']}")
        return result

    def close(self) -> None:
        if self._proc is not None:
            try:
                if self._proc.poll() is None:
                    self._proc.stdin.write("__quit__\n")
                    self._proc.stdin.flush()
                    self._proc.wait(timeout=5)
            except (BrokenPipeError, OSError, subprocess.TimeoutExpired):
                pass
            if self._proc.poll() is None:
                self._proc.terminate()
                try:
                    self._proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
        if self._errf is not None:
            self._errf.close()
