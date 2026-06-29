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

from . import __version__, env, probe, profiles
from .canon import apply_canonicalization, build_evidence
from .cores import asr_core, canonicalizer_core, diarizer_core
from .ir import build_ir, validate_ir
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
        print(f"profile: {profile_path.stem} (auto; -p to override)", file=sys.stderr)
    profile = profiles.load(profile_path)
    if not profile.asr.enabled:
        print(f"error: profile {profile.name!r} has no ASR stage", file=sys.stderr)
        return 2

    progress = args.progress if args.progress is not None else sys.stderr.isatty()

    core = asr_core(profile.asr)
    print(f"[{profile.name}] {core.name} ({profile.asr.backend}) → {audio.name}", file=sys.stderr)
    segments = core.transcribe(audio, progress=progress, prompt=args.prompt)

    diar_turns = None
    if profile.diar.enabled and not args.no_diarize:
        diarizer = diarizer_core(profile.diar)
        print(f"  diarizing: {diarizer.name} ({profile.diar.backend})", file=sys.stderr)
        diar_turns = diarizer.diarize(audio, progress=progress)
        print(f"  {len(diar_turns)} speaker turns", file=sys.stderr)

    ir = build_ir(segments, profile, audio, diar_turns=diar_turns)

    if profile.llm.enabled and not args.no_canon:
        canon = canonicalizer_core(profile.llm)
        print(f"  canonicalizing: {canon.name} ({profile.llm.backend})", file=sys.stderr)
        data = canon.canonicalize(build_evidence(ir))
        ir = apply_canonicalization(ir, data)
        validate_ir(ir)
        named = [s for s in ir["speakers"] if s.get("display_name")]
        print(f"  named {len(named)}/{len(ir['speakers'])} speakers", file=sys.stderr)

    return _emit(render(ir, args.format), args.output, ir)


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
