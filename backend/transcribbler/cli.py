"""transcribbler backend CLI.

First slice: `probe` (what GPUs are here) and `transcribe` (run the profile's ASR
core on a file → validated Canonical IR). HTTP serving, diarization, and the
canonicalization LLM come in later stages (ADR-0008).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__, probe, profiles
from .cores import asr_core
from .ir import build_ir


def _cmd_probe(args: argparse.Namespace) -> int:
    caps = probe.detect()
    print(f"cuda   : {caps.cuda or '-'}")
    print(f"rocm   : {caps.rocm or '-'}")
    print(f"vulkan : {caps.vulkan or '-'}")
    print(f"cpu    : yes")
    print(f"recommended backend: {caps.recommend()}")
    return 0


def _cmd_transcribe(args: argparse.Namespace) -> int:
    audio = Path(args.audio)
    if not audio.exists():
        print(f"error: audio not found: {audio}", file=sys.stderr)
        return 2
    profile = profiles.load(args.profile)
    if not profile.asr.enabled:
        print(f"error: profile {profile.name!r} has no ASR stage", file=sys.stderr)
        return 2

    core = asr_core(profile.asr)
    print(f"[{profile.name}] {core.name} ({profile.asr.backend}) → {audio.name}", file=sys.stderr)
    segments = core.transcribe(audio)
    ir = build_ir(segments, profile, audio)

    out = json.dumps(ir, indent=2, ensure_ascii=False)
    if args.output:
        Path(args.output).write_text(out + "\n")
        print(f"wrote {args.output} ({len(ir['turns'])} turns, {ir['source']['duration_s']}s)", file=sys.stderr)
    else:
        print(out)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="transcribbler", description="transcribbler compute backend")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("probe", help="detect available GPU backends").set_defaults(func=_cmd_probe)

    t = sub.add_parser("transcribe", help="transcribe a file to Canonical IR")
    t.add_argument("audio", help="audio/video file")
    t.add_argument("-p", "--profile", required=True, help="path to a compute profile .toml")
    t.add_argument("-o", "--output", help="write IR JSON here (default: stdout)")
    t.set_defaults(func=_cmd_transcribe)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
