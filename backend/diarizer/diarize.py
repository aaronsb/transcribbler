#!/usr/bin/env python3
"""pyannote diarization sidecar.

Runs in its own torch-ROCm env (see pyproject.toml) so torch stays out of the
main backend. Two modes:

- **one-shot** (default): ``diarize.py AUDIO --model M`` loads the pipeline, runs
  it on one file, writes JSON to stdout, exits. Used by the batch pipeline.
- **serve** (``--serve``): loads the pipeline **once**, then reads one audio path
  per line from stdin and writes one JSON response per line to stdout, staying
  warm across requests. Used by live capture, where per-chunk model reload (~9-13s)
  is the bottleneck (ADR-0024).

Response JSON (both modes):

    {"turns":     [{"start": s, "end": s, "label": "SPEAKER_00"}, ...],
     "speakers":  [{"label": "SPEAKER_00", "embedding": [float, ...]}, ...],
     "device":    "cuda"}

``speakers[].embedding`` is pyannote's per-speaker voiceprint (community-1 returns
these by default; the caller uses them to stitch chunk-local speakers into
session-stable identities). Absent/unusable embeddings are emitted as null.

The HF token is read from HF_TOKEN. Diagnostics go to stderr so stdout stays pure JSON.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import math
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


def load_pipeline(model: str, token: str):
    """Load the pyannote pipeline once and move it to the best device."""
    import torch
    from pyannote.audio import Pipeline
    from pyannote.audio.telemetry import set_telemetry_metrics

    set_telemetry_metrics(False)  # don't phone home about a private audio pipeline

    # Keep model-load chatter off stdout so it can't corrupt the JSON protocol.
    with contextlib.redirect_stdout(sys.stderr):
        pipeline = Pipeline.from_pretrained(model, token=token)
        if pipeline is None:
            raise RuntimeError(f"could not load pipeline {model!r} (gated model not accepted?)")
        # ROCm builds of torch report as CUDA (HIP masquerades as the cuda API).
        device = "cuda" if torch.cuda.is_available() else "cpu"
        pipeline.to(torch.device(device))
    return pipeline, device


def _embedding_row(speaker_embeddings, s: int) -> list[float] | None:
    """Serialize speaker row `s` to a JSON-safe list, or None if unusable."""
    if speaker_embeddings is None or s >= len(speaker_embeddings):
        return None
    row = speaker_embeddings[s]
    vals = [float(x) for x in row]
    if not vals or any(math.isnan(v) or math.isinf(v) for v in vals):
        return None
    if not any(v != 0.0 for v in vals):
        return None  # pyannote zero-pads "extra" speakers — a zero vector isn't a voiceprint
    return [round(v, 6) for v in vals]


def diarize_file(pipeline, device: str, audio_path: str, *, progress: bool = False) -> dict:
    """Run the warm pipeline on one 16 kHz mono wav → turns + per-speaker embeddings."""
    import soundfile as sf
    import torch

    # Load the (already 16 kHz mono) wav ourselves and pass an in-memory waveform.
    # Avoids pyannote 4.x's torchcodec-based file decoder entirely.
    data, sample_rate = sf.read(audio_path, dtype="float32", always_2d=False)
    waveform = torch.from_numpy(data)
    waveform = waveform.unsqueeze(0) if waveform.ndim == 1 else waveform.T  # (channel, time)
    inputs = {"waveform": waveform, "sample_rate": sample_rate}

    # Redirect stdout→stderr during inference: a stray library print to stdout would
    # corrupt the JSON-lines protocol the parent reads.
    with contextlib.redirect_stdout(sys.stderr):
        if progress:
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

    # pyannote re-orders speaker_embeddings so row s matches speaker_diarization.labels()[s]
    # ("re-order centroids so that they match the order given by diarization.labels()" in
    # speaker_diarization.py), so enumerate(labels()) is the correct, guaranteed pairing.
    speaker_embeddings = getattr(output, "speaker_embeddings", None)
    speakers = [
        {"label": str(speaker), "embedding": _embedding_row(speaker_embeddings, s)}
        for s, speaker in enumerate(annotation.labels())
    ]
    return {"turns": turns, "speakers": speakers, "device": device}


def _serve(pipeline, device: str) -> int:
    """Read one audio path per line from stdin; write one JSON response per line."""
    log("ready")  # handshake: the parent waits for this before sending work
    for line in sys.stdin:
        path = line.strip()
        if not path:
            continue
        if path in ("__quit__", "__exit__"):
            break
        try:
            result = diarize_file(pipeline, device, path)
        except Exception as e:  # one bad chunk must not kill the daemon
            result = {"error": f"{type(e).__name__}: {e}"}
        try:
            json.dump(result, sys.stdout)
            sys.stdout.write("\n")
            sys.stdout.flush()
        except BrokenPipeError:  # parent went away mid-response — shut down quietly
            break
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("audio", nargs="?", help="normalized audio file (16 kHz mono wav)")
    ap.add_argument("--model", default="pyannote/speaker-diarization-community-1")
    ap.add_argument("--serve", action="store_true", help="persistent mode: load once, read paths from stdin")
    ap.add_argument("--progress", action="store_true", help="emit @@P@@ progress lines on stderr (one-shot)")
    args = ap.parse_args()

    token = os.environ.get("HF_TOKEN")
    if not token:
        log("error: HF_TOKEN not set")
        return 2

    try:
        pipeline, device = load_pipeline(args.model, token)
    except RuntimeError as e:
        log(f"error: {e}")
        return 3
    log(f"diarizing on {device} with {args.model}")

    if args.serve:
        return _serve(pipeline, device)

    if not args.audio:
        log("error: audio path required in one-shot mode")
        return 2
    result = diarize_file(pipeline, device, args.audio, progress=args.progress)
    json.dump(result, sys.stdout)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
