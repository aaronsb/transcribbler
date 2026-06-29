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

    import torch
    from pyannote.audio import Pipeline

    pipeline = Pipeline.from_pretrained(args.model, token=token)
    if pipeline is None:
        log(f"error: could not load pipeline {args.model!r} (gated model not accepted?)")
        return 3

    # ROCm builds of torch report as CUDA (HIP masquerades as the cuda API).
    device = "cuda" if torch.cuda.is_available() else "cpu"
    pipeline.to(torch.device(device))
    log(f"diarizing on {device} with {args.model}")

    diarization = pipeline(args.audio)
    turns = [
        {"start": round(segment.start, 3), "end": round(segment.end, 3), "label": str(speaker)}
        for segment, _, speaker in diarization.itertracks(yield_label=True)
    ]
    json.dump({"turns": turns, "device": device}, sys.stdout)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
