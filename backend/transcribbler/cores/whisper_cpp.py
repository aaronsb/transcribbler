"""whisper.cpp ASR core (ADR-0004).

Shells out to the whisper.cpp `whisper-cli` binary. The device (Vulkan/CUDA/ROCm)
is chosen at *build* time of whisper.cpp; this adapter just runs whichever binary
the profile points at. Input audio is normalized to 16 kHz mono WAV first
(ADR-0005), which is what whisper.cpp expects.
"""

from __future__ import annotations

import json
import re
import tempfile
from collections.abc import Callable
from pathlib import Path

from ..audio import normalize_wav
from ..profiles import StageConfig
from .base import Segment
from .proc import run_streamed

_PROGRESS_RE = re.compile(r"progress\s*=\s*(\d+)%")


def _progress_renderer() -> Callable[[str], str | None]:
    """Turn whisper's `...progress = N%` stderr lines into a live `ASR N%`."""
    last = -1

    def render(line: str) -> str | None:
        nonlocal last
        m = _PROGRESS_RE.search(line)
        if not m:
            return None
        pct = int(m.group(1))
        if pct == last:
            return None
        last = pct
        return f"\r  ASR {pct:3d}%" + ("\n" if pct >= 100 else "")

    return render


class WhisperCppCore:
    name = "whisper.cpp"

    def __init__(self, cfg: StageConfig):
        if not cfg.binary or not Path(cfg.binary).exists():
            raise FileNotFoundError(f"whisper.cpp binary not found: {cfg.binary}")
        if not cfg.model or not Path(cfg.model).exists():
            raise FileNotFoundError(f"whisper.cpp model not found: {cfg.model}")
        self.binary = cfg.binary
        self.model = cfg.model
        # Profile-level default initial prompt (biases spelling of names/jargon);
        # a per-call prompt overrides it.
        self.prompt = cfg.options.get("prompt")

    def transcribe(
        self, audio_path: Path, *, progress: bool = False, prompt: str | None = None
    ) -> list[Segment]:
        with tempfile.TemporaryDirectory(prefix="transcribbler_") as tmp:
            wav = normalize_wav(audio_path, Path(tmp) / "norm.wav")
            out_prefix = Path(tmp) / "out"
            self._run(wav, out_prefix, progress=progress, prompt=prompt or self.prompt)
            return _parse(out_prefix.with_suffix(".json"))

    def _run(self, wav: Path, out_prefix: Path, *, progress: bool, prompt: str | None) -> None:
        cmd = [
            self.binary,
            "-m",
            self.model,
            "-f",
            str(wav),
            "-pp",  # emit `progress = N%` on stderr (rendered live when streaming)
            "-oj",  # JSON output
            "-of",
            str(out_prefix),  # output file prefix
        ]
        if prompt:
            # carry it across whisper's internal 30s windows so the bias holds
            # over the whole recording, not just the first window.
            cmd += ["--prompt", prompt, "--carry-initial-prompt"]
        rc, _out, tail = run_streamed(cmd, stream=progress, on_line=_progress_renderer())
        if rc != 0:
            raise RuntimeError(f"whisper-cli failed ({rc}): {tail[-500:]}")


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
