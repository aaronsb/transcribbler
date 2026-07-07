"""transcribbler backend CLI.

First slice: `probe` (what GPUs are here) and `transcribe` (run the profile's ASR
core on a file → validated Canonical IR). HTTP serving, diarization, and the
canonicalization LLM come in later stages (ADR-0008).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import __version__, env, probe, profiles
from .pipeline import run_pipeline
from .progress import stderr_sink
from .render import render


def _cmd_probe(args: argparse.Namespace) -> int:
    caps = probe.detect()
    print(f"cuda   : {caps.cuda or '-'}")
    print(f"rocm   : {caps.rocm or '-'}")
    print(f"vulkan : {caps.vulkan or '-'}")
    print("cpu    : yes")
    print(f"recommended backend: {caps.recommend()}")
    return 0


def _cmd_transcribe(args: argparse.Namespace) -> int:
    audio = Path(args.audio)
    if not audio.exists():
        print(f"error: audio not found: {audio}", file=sys.stderr)
        return 2
    try:
        profile_path = profiles.resolve(args.profile)
    except profiles.ProfileError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if not args.profile:
        src = "$TRANSCRIBBLER_PROFILE" if os.environ.get("TRANSCRIBBLER_PROFILE") else "auto"
        print(f"profile: {profile_path.stem} ({src}; -p to override)", file=sys.stderr)
    profile = profiles.load(profile_path)
    if not profile.asr.enabled:
        print(f"error: profile {profile.name!r} has no ASR stage", file=sys.stderr)
        return 2

    show = args.progress if args.progress is not None else sys.stderr.isatty()
    ir = run_pipeline(
        audio,
        profile,
        diarize=not args.no_diarize,
        canon=not args.no_canon,
        prompt=args.prompt,
        asr_progress=stderr_sink() if show else None,
        diar_progress=stderr_sink() if show else None,
        log=lambda m: print(m, file=sys.stderr),
    )
    return _emit(render(ir, args.format), args.output, ir)


def _cmd_capture(args: argparse.Namespace) -> int:
    from .capture import run_capture

    try:
        profile_path = profiles.resolve(args.profile)
    except profiles.ProfileError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    profile = profiles.load(profile_path)
    if not profile.asr.enabled:
        print(f"error: profile {profile.name!r} has no ASR stage", file=sys.stderr)
        return 2
    run_capture(
        profile,
        Path(args.output),
        app=args.app,
        mic=args.mic,
        meeting=args.meeting,
        segment_s=args.segment,
        diarize=not args.no_diarize,
        threshold=args.threshold,
        log=lambda m: print(m, file=sys.stderr),
    )
    return 0


def _cmd_render(args: argparse.Namespace) -> int:
    path = Path(args.ir)
    if not path.exists():
        print(f"error: IR not found: {path}", file=sys.stderr)
        return 2
    ir = json.loads(path.read_text())
    return _emit(render(ir, args.format), args.output, ir)


def _emit(text: str, output: str | None, ir: dict) -> int:
    if output:
        Path(output).write_text(text)
        print(f"wrote {output} ({len(ir['turns'])} turns, {ir['source']['duration_s']}s)", file=sys.stderr)
    else:
        sys.stdout.write(text)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="transcribbler", description="transcribbler compute backend")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("probe", help="detect available GPU backends").set_defaults(func=_cmd_probe)

    t = sub.add_parser("transcribe", help="transcribe a file to Canonical IR")
    t.add_argument("audio", help="audio/video file")
    t.add_argument(
        "-p",
        "--profile",
        help="compute profile: a name (e.g. desktop-vulkan), a .toml path, or "
        "$TRANSCRIBBLER_PROFILE; auto-selected by detected GPU if omitted",
    )
    t.add_argument("-o", "--output", help="write here (default: stdout)")
    t.add_argument(
        "-f", "--format", choices=["json", "md", "vtt"], default="json", help="output format (default: json)"
    )
    t.add_argument(
        "--no-diarize", action="store_true", help="skip diarization even if the profile enables it"
    )
    t.add_argument(
        "--no-canon", action="store_true", help="skip LLM speaker naming even if the profile enables it"
    )
    t.add_argument(
        "--progress",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="stream live ASR/diarization progress to stderr (default: on when stderr is a TTY)",
    )
    t.add_argument(
        "--prompt",
        help="initial prompt to bias ASR spelling (names, jargon); overrides the profile's [asr] prompt",
    )
    t.set_defaults(func=_cmd_transcribe)

    c = sub.add_parser("capture", help="live-capture mic+meeting → rolling transcript on disk")
    c.add_argument("-o", "--output", required=True, help="transcript file to append to")
    c.add_argument("-p", "--profile", help="compute profile (auto-selected if omitted)")
    c.add_argument("--app", default="Google Chrome", help="meeting app to match for path detection")
    c.add_argument("--mic", help="override: PipeWire source for the operator mic")
    c.add_argument("--meeting", help="override: PipeWire monitor source for meeting audio")
    c.add_argument("--segment", type=int, default=45, help="chunk length in seconds (default: 45)")
    c.add_argument("--no-diarize", action="store_true", help="skip remote-speaker diarization")
    c.add_argument(
        "--threshold", type=float, default=0.5,
        help="voiceprint cosine match threshold for session-stable speakers (default: 0.5)",
    )
    c.set_defaults(func=_cmd_capture)

    r = sub.add_parser("render", help="render an existing Canonical IR to md/vtt/json")
    r.add_argument("ir", help="path to a Canonical IR .json")
    r.add_argument(
        "-f", "--format", choices=["json", "md", "vtt"], default="md", help="output format (default: md)"
    )
    r.add_argument("-o", "--output", help="write here (default: stdout)")
    r.set_defaults(func=_cmd_render)

    env.load_env_file()  # make HF_TOKEN etc. available to cores
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):  # bare `transcribbler` → help, not a traceback
        parser.print_help()
        return 0
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
