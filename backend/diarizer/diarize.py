#!/usr/bin/env python3
"""pyannote diarization sidecar.

Runs in its own torch-ROCm env (see pyproject.toml) so torch stays out of the
main backend. Reads a normalized audio file, runs the pyannote pipeline on the
GPU, and writes speaker turns as JSON to stdout:

    {"turns": [{"start": s, "end": s, "label": "SPEAKER_00"}, ...], "device": "cuda"}

The HF token is read from the HF_TOKEN env var (the parent passes it through).
Diagnostics go to stderr so stdout stays pure JSON.
"""
from __future__ import annotations

import argparse
import json
import os
import sys


def log(*a):
    print(*a, file=sys.stderr, flush=True)


class _ProgressToStderr:
    """pyannote hook → machine-readable progress lines on stderr.

    Emits ``@@P@@<TAB>step<TAB>completed<TAB>total`` (newline-terminated so the
    parent can read it line-by-line and render it live). The parent recognizes
    the sentinel and reformats; everything else stays human log output. Deduped
    on integer percent so a long step doesn't flood the pipe.
    """

    def __enter__(self):
        self._last: dict[str, int] = {}
        return self

    def __exit__(self, *a):
        return

    def __call__(self, step_name, step_artifact, file=None, total=None, completed=None):
        if completed is None:
            completed = total = 1
        try:
            pct = int(100 * completed / total) if total else 0
        except (TypeError, ZeroDivisionError):
            return
        if self._last.get(step_name) == pct:
            return
        self._last[step_name] = pct
        print(f"@@P@@\t{step_name}\t{completed}\t{total}", file=sys.stderr, flush=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("audio", help="normalized audio file (16 kHz mono wav)")
    ap.add_argument("--model", default="pyannote/speaker-diarization-community-1")
    ap.add_argument("--progress", action="store_true", help="emit @@P@@ progress lines on stderr")
    args = ap.parse_args()

    token = os.environ.get("HF_TOKEN")
    if not token:
        log("error: HF_TOKEN not set")
        return 2

    import soundfile as sf
    import torch
    from pyannote.audio import Pipeline
    from pyannote.audio.telemetry import set_telemetry_metrics

    set_telemetry_metrics(False)  # don't phone home about a private audio pipeline

    pipeline = Pipeline.from_pretrained(args.model, token=token)
    if pipeline is None:
        log(f"error: could not load pipeline {args.model!r} (gated model not accepted?)")
        return 3

    # ROCm builds of torch report as CUDA (HIP masquerades as the cuda API).
    device = "cuda" if torch.cuda.is_available() else "cpu"
    pipeline.to(torch.device(device))
    log(f"diarizing on {device} with {args.model}")

    # Load the (already 16 kHz mono) wav ourselves and pass an in-memory waveform.
    # Avoids pyannote 4.x's torchcodec-based file decoder entirely.
    data, sample_rate = sf.read(args.audio, dtype="float32", always_2d=False)
    waveform = torch.from_numpy(data)
    waveform = waveform.unsqueeze(0) if waveform.ndim == 1 else waveform.T  # (channel, time)
    inputs = {"waveform": waveform, "sample_rate": sample_rate}
    if args.progress:
        with _ProgressToStderr() as hook:
            output = pipeline(inputs, hook=hook)
    else:
        output = pipeline(inputs)
    # pyannote 4.x returns DiarizeOutput; .speaker_diarization is the full Annotation
    # (keeps overlapping turns, so our alignment can flag secondary speakers).
    annotation = output.speaker_diarization if hasattr(output, "speaker_diarization") else output
    turns = [
        {"start": round(segment.start, 3), "end": round(segment.end, 3), "label": str(speaker)}
        for segment, _, speaker in annotation.itertracks(yield_label=True)
    ]
    json.dump({"turns": turns, "device": device}, sys.stdout)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
