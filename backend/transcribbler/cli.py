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
import threading
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


_SPK_PALETTE = ["36", "35", "33", "34", "32", "31"]  # cyan, magenta, yellow, blue, green, red


def _speaker_color(label: str) -> str:
    if label == "You":
        return "1;32"  # bold green — the operator
    if label == "Remote":
        return "2"  # dim — unattributed remote
    if label.startswith("S") and label[1:].isdigit():
        return _SPK_PALETTE[(int(label[1:]) - 1) % len(_SPK_PALETTE)]
    return "0"


def _fmt_turn(turn, color: bool) -> str:
    m, s = divmod(int(turn.start), 60)
    ts = f"{m:02d}:{s:02d}"
    if not color:
        return f"[{ts}] {turn.speaker}: {turn.text}"
    return f"\033[2m[{ts}]\033[0m \033[{_speaker_color(turn.speaker)}m{turn.speaker}\033[0m: {turn.text}"


def _key_listener(controls, say) -> None:
    """Single-key controls: space toggles pause, q quits. Caller sets cbreak mode."""
    import select

    while not controls.stopped:
        r, _, _ = select.select([sys.stdin], [], [], 0.2)
        if not r:
            continue
        try:
            ch = sys.stdin.read(1)
        except (OSError, ValueError):
            break
        if ch == " ":
            controls.toggle_pause()
            say("  ⏸  paused (listening off)" if controls.paused else "  ▶  resumed")
        elif ch in ("q", "Q"):
            say("  quitting… (finalizing the tail)")
            controls.stop()
            break


class _LiveConsole:
    """Scrolling transcript above a single in-place status footer (fancy=TTY only).

    Transcript, announces, and notices all scroll via ``line()``; the per-chunk
    status is pinned to one line (``status()``) that overwrites in place, so the
    view stays a continuous timestamped session. In fancy mode this is the *single*
    point through which terminal output passes — routing notices through it too is
    what keeps the footer from being corrupted by a competing stderr write. A lock
    guards draws because the key-listener thread also emits notices. All writes are
    guarded: a closed stdout (`listen | head`) latches drawing off instead of raising.
    """

    def __init__(self, fancy: bool):
        self._fancy = fancy
        self._footer = ""
        self._dead = False
        self._lock = threading.Lock()

    def _write(self, s: str) -> None:  # caller holds _lock
        if self._dead:
            return
        try:
            sys.stdout.write(s)
            sys.stdout.flush()
        except BrokenPipeError:
            self._dead = True  # stdout closed downstream — stop drawing, keep recording

    def line(self, text: str) -> None:
        with self._lock:
            self._write(("\r\033[K" if self._footer else "") + text + "\n" + self._footer)

    def status(self, text: str) -> None:
        if not self._fancy:
            return
        with self._lock:
            self._footer = text
            self._write("\r\033[K" + text)

    def done(self) -> None:
        with self._lock:
            if self._fancy and self._footer:
                self._write("\n")
                self._footer = ""


def _cmd_listen(args: argparse.Namespace) -> int:
    from datetime import datetime

    from . import paths
    from .capture import Controls, run_capture

    try:
        profile_path = profiles.resolve(args.profile)
    except profiles.ProfileError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    profile = profiles.load(profile_path)
    if not profile.asr.enabled:
        print(f"error: profile {profile.name!r} has no ASR stage", file=sys.stderr)
        return 2

    session_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    if args.output:
        out_path = Path(args.output)
    else:  # default: a per-session pack dir under XDG data, scratch under XDG state
        out_path = paths.ensure(paths.sessions_dir() / session_id) / "transcript.md"
    workdir = paths.ensure(paths.capture_dir() / session_id)
    color = sys.stdout.isatty()
    console = _LiveConsole(fancy=color)
    controls = Controls()

    def say(m: str) -> None:
        # In fancy mode route notices through the console so they scroll above the
        # pinned footer instead of colliding with it on stderr; piped mode keeps
        # notices on stderr so stdout stays a clean transcript.
        if color:
            console.line(m)
        else:
            print(m, file=sys.stderr, flush=True)

    def on_turn(turn) -> None:
        console.line(_fmt_turn(turn, color))

    def on_new_speaker(sid: str) -> None:
        badge = f"\033[{_speaker_color(sid)}m{sid}\033[0m" if color else sid
        console.line(f"  🆕 new speaker {badge}")

    def on_chunk(n: int, n_turns: int, dt: float, keeps_up: bool) -> None:
        if not color:
            return
        console.status(
            f"\033[2m· chunk {n:05d} · {n_turns} turns · {dt:.1f}s · "
            f"{'keeps up' if keeps_up else 'LAGS'} vs {args.segment}s\033[0m"
        )

    old_termios = None
    try:
        # cbreak setup lives inside the try so the finally always restores the terminal.
        if sys.stdin.isatty():
            import termios
            import tty

            fd = sys.stdin.fileno()
            old_termios = termios.tcgetattr(fd)
            tty.setcbreak(fd)  # ISIG stays on → Ctrl-C still works
            threading.Thread(target=_key_listener, args=(controls, say), daemon=True).start()
            say(f"listening → {out_path}    [space] pause/resume    [q] quit (finalizes the tail)")
        else:
            say(f"listening → {out_path}    (Ctrl-C to stop)")
        run_capture(
            profile,
            out_path,
            app=args.app,
            mic=args.mic,
            meeting=args.meeting,
            segment_s=args.segment,
            diarize=not args.no_diarize,
            threshold=args.threshold,
            on_turn=on_turn,
            on_chunk=on_chunk,
            on_new_speaker=on_new_speaker,
            controls=controls,
            banner=False,
            workdir=workdir,
            log=say,
        )
    except KeyboardInterrupt:
        pass
    finally:
        controls.stop()
        console.done()
        if old_termios is not None:
            import termios

            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, old_termios)
    say(f"saved → {out_path}")
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

    ls = sub.add_parser(
        "listen", help="live console: transcribe what's playing, print it live, [space]/[q] controls"
    )
    ls.add_argument(
        "-o", "--output",
        help="transcript file (default: a per-session pack under XDG data, "
             "e.g. ~/.local/share/transcribbler/sessions/<id>/transcript.md)",
    )
    ls.add_argument("-p", "--profile", help="compute profile (auto-selected if omitted)")
    ls.add_argument("--app", default="Google Chrome", help="meeting app to match for path detection")
    ls.add_argument("--mic", help="override: PipeWire source for the operator mic")
    ls.add_argument("--meeting", help="override: PipeWire monitor source for meeting audio")
    ls.add_argument("--segment", type=int, default=20, help="chunk length in seconds (default: 20)")
    ls.add_argument("--no-diarize", action="store_true", help="skip remote-speaker diarization")
    ls.add_argument(
        "--threshold", type=float, default=0.5, help="voiceprint match threshold (default: 0.5)"
    )
    ls.set_defaults(func=_cmd_listen)

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
