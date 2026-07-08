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
from pathlib import Path

from ..audio import normalize_wav
from ..profiles import StageConfig
from ..progress import ProgressEvent, ProgressSink, line_tap
from .base import Segment
from .proc import run_streamed

_PROGRESS_RE = re.compile(r"progress\s*=\s*(\d+)%")


def _parse_progress(line: str) -> ProgressEvent | None:
    """Parse whisper's `...progress = N%` stderr lines into a ProgressEvent."""
    m = _PROGRESS_RE.search(line)
    if not m:
        return None
    return ProgressEvent(stage="asr", completed=float(m.group(1)), total=100.0)


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
        self, audio_path: Path, *, progress: ProgressSink | None = None, prompt: str | None = None
    ) -> list[Segment]:
        with tempfile.TemporaryDirectory(prefix="transcribbler_") as tmp:
            wav = normalize_wav(audio_path, Path(tmp) / "norm.wav")
            out_prefix = Path(tmp) / "out"
            self._run(wav, out_prefix, progress=progress, prompt=prompt or self.prompt)
            return _parse(out_prefix.with_suffix(".json"))

    def _run(self, wav: Path, out_prefix: Path, *, progress: ProgressSink | None, prompt: str | None) -> None:
        cmd = [
            self.binary,
            "-m",
            self.model,
            "-f",
            str(wav),
            "-pp",  # emit `progress = N%` on stderr (rendered live when streaming)
            "-ojf",  # full JSON output — includes per-token probabilities (for confidence)
            "-of",
            str(out_prefix),  # output file prefix
        ]
        if prompt:
            # carry it across whisper's internal 30s windows so the bias holds
            # over the whole recording, not just the first window.
            cmd += ["--prompt", prompt, "--carry-initial-prompt"]
        on_line = line_tap(_parse_progress, progress) if progress is not None else None
        rc, _out, tail = run_streamed(cmd, stream=progress is not None, on_line=on_line)
        if rc != 0:
            raise RuntimeError(f"whisper-cli failed ({rc}): {tail[-500:]}")


def _segment_confidence(tokens: list[dict]) -> float | None:
    """Mean probability of a segment's *word* tokens (whisper's per-token ``p``, full JSON).

    Special tokens — timestamps and markers like ``[_BEG_]`` — are excluded so the score
    reflects the actual words. Returns None when no usable probabilities are present (e.g. the
    binary emitted basic JSON), which keeps ``confidence`` optional all the way to the IR.
    """
    ps = [
        t["p"]
        for t in tokens
        if isinstance(t.get("p"), (int, float)) and not t.get("text", "").strip().startswith("[_")
    ]
    return round(sum(ps) / len(ps), 3) if ps else None


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
                confidence=_segment_confidence(item.get("tokens", [])),
            )
        )
    return segments
