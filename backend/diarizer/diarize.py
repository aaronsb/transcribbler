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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("audio", help="normalized audio file (16 kHz mono wav)")
    ap.add_argument("--model", default="pyannote/speaker-diarization-community-1")
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
    output = pipeline({"waveform": waveform, "sample_rate": sample_rate})
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
