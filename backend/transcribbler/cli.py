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


def _cmd_meter(args: argparse.Namespace) -> int:
    from .meter import run_meter

    return run_meter(args.app, sample_s=args.sample)


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
    from .capture import SourceError, run_capture

    try:
        profile_path = profiles.resolve(args.profile)
    except profiles.ProfileError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    profile = profiles.load(profile_path)
    if not profile.asr.enabled:
        print(f"error: profile {profile.name!r} has no ASR stage", file=sys.stderr)
        return 2
    try:
        run_capture(
            profile,
            Path(args.output),
            app=args.app,
            mic=args.mic,
            meeting=args.meeting,
            segment_s=args.segment,
            diarize=not args.no_diarize,
            threshold=args.threshold,
            bleed_reject_db=args.bleed_reject_db,
            denoise=args.denoise,
            retain_audio=not args.no_audio,
            log=lambda m: print(m, file=sys.stderr),
        )
    except SourceError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
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
    """Single-key controls: space pauses, e flags the current speaker, q quits. cbreak mode."""
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
        elif ch in ("e", "E"):
            label = controls.flag_current()
            if label is not None:
                say(f"  ⭐ flagged {label} to remember — you'll name them when you finish")
            else:
                say("  (no remote speaker heard yet to flag)")
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

    from . import pack, paths
    from .capture import Controls, SourceError, run_capture

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
    result: pack.PackResult | None = None

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
            say(f"listening → {out_path}    [space] pause    [e] remember speaker    "
                f"[q] quit (finalizes the tail)")
        else:
            say(f"listening → {out_path}    (Ctrl-C to stop)")
        result = run_capture(
            profile,
            out_path,
            app=args.app,
            mic=args.mic,
            meeting=args.meeting,
            segment_s=args.segment,
            diarize=not args.no_diarize,
            threshold=args.threshold,
            bleed_reject_db=args.bleed_reject_db,
            denoise=args.denoise,
            retain_audio=not args.no_audio,
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
    except SourceError as e:
        say(f"error: {e}")
        # nothing was captured — don't leave the empty session/scratch dirs behind.
        # rmdir removes only if empty, so a real session's dirs are never touched.
        for d in (workdir, out_path.parent if not args.output else None):
            if d is not None:
                try:
                    d.rmdir()
                except OSError:
                    pass
        return 2
    finally:
        controls.stop()
        console.done()
        if old_termios is not None:
            import termios

            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, old_termios)
    say(f"saved → {out_path}")
    # Offer the guided naming walk for any speakers flagged live with [e]. Runs only now —
    # after the terminal is back in cooked mode and the key thread has stopped — so input()
    # behaves, and only when interactive (a piped run just leaves them pending on the pack).
    if result is not None and sys.stdin.isatty() and sys.stdout.isatty():
        pending = pack.load_pack(result.uid).meta.get("pending_enrollment") or []
        if pending:
            try:
                ans = input(f"\nyou flagged {len(pending)} speaker(s) to remember — "
                            f"name them now? [Y/n] ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                ans = "n"
            if ans in ("", "y", "yes"):
                _cmd_pack_enroll(argparse.Namespace(uid=result.uid, speaker=[]))
            else:
                print(f"  later:  transcribbler pack enroll {result.uid}")
    return 0


# A phonetically rich passage (the Rainbow Passage) — reading it gives a fuller,
# more speaker-representative voiceprint than a few offhand words.
_ENROLL_PASSAGE = (
    "When the sunlight strikes raindrops in the air, they act as a prism and form a "
    "rainbow. The rainbow is a division of white light into many beautiful colors. "
    "These take the shape of a long round arch, with its path high above, and its two "
    "ends apparently beyond the horizon."
)


def _dominant_embedding(res: dict) -> list[float] | None:
    """Embedding of the speaker with the most speech in a diarize result (the reader)."""
    dur: dict[str, float] = {}
    for t in res.get("turns", []):
        dur[t["label"]] = dur.get(t["label"], 0.0) + (t["end"] - t["start"])
    emb = {s["label"]: s["embedding"] for s in res.get("speakers", []) if s.get("embedding")}
    ranked = sorted((lbl for lbl in emb if lbl in dur), key=lambda lbl: dur[lbl], reverse=True)
    if ranked:
        return emb[ranked[0]]
    return next(iter(emb.values()), None)  # short clip with no turns → any usable one


def _cmd_enroll(args: argparse.Namespace) -> int:
    import subprocess

    from . import library, pack, paths
    from .capture import detect_paths
    from .diarizer_daemon import DiarizerDaemon
    from .ir import build_live_ir
    from .session_gallery import cosine

    try:
        profile = profiles.load(profiles.resolve(args.profile))
    except profiles.ProfileError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if not profile.diar.enabled:
        print(f"error: profile {profile.name!r} has no diarizer to compute embeddings", file=sys.stderr)
        return 2
    mic = args.mic or detect_paths(args.app).mic

    prior = library.find_by_name(args.name)
    where = f"updating existing, {prior.samples} sample(s)" if prior else "new"
    print(f"\nEnrolling a voiceprint for {args.name!r} ({where}).")
    print("\nRead the following aloud, clearly and at a normal pace:\n")
    body = f"\033[36m{_ENROLL_PASSAGE}\033[0m" if sys.stdout.isatty() else _ENROLL_PASSAGE
    print(f"  {body}\n")
    try:
        input(f"Press Enter when ready — recording runs for {args.seconds}s… ")
    except (EOFError, KeyboardInterrupt):
        print("\ncancelled.", file=sys.stderr)
        return 1
    print("● recording — read now…", flush=True)

    work = paths.ensure(paths.state_dir() / "enroll")
    wav = work / "enroll.wav"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "pulse", "-i", mic,
         "-t", str(args.seconds), "-ar", "16000", "-ac", "1", str(wav)],
        check=True,
    )
    print("✓ captured — computing voiceprint…")
    daemon = DiarizerDaemon(profile.diar.model, work)
    daemon.start()
    try:
        res = daemon.diarize(wav)
    finally:
        daemon.close()

    emb = _dominant_embedding(res)
    if emb is None:
        print("error: no usable voice captured (too quiet or no speech). Try again.", file=sys.stderr)
        return 2
    if prior is not None:
        sim = cosine(emb, prior.centroid)
        print(f"  match to your existing print: cosine {sim:.3f}  (1.0 = identical, >0.5 = same voice)")

    # An enrollment is not a special format — it's a single-speaker session pack tagged for
    # training (session-pack spec §1). Build the IR (the read passage as one turn), write a
    # real pack (record.ir.json + audio/ clip + embedding sidecar), then extract the
    # voiceprint FROM the pack — the same universal path any capture will use.
    ir = build_live_ir(
        [(0.0, float(args.seconds), args.name, _ENROLL_PASSAGE)],
        profile,
        duration_s=float(args.seconds),
        operator_label=args.name,
        diarized=True,
    )
    sid = ir["speakers"][0]["id"]
    result = pack.write_pack(
        ir,
        title=f"{args.name} enrollment",
        tags=["enrollment", "training"],
        embeddings={sid: emb},
        audio={sid: wav},
    )
    vp = pack.extract(result.blob_path)[0]
    print(f"\n✓ enrolled {vp.name!r} — voiceprint {vp.uid}, {vp.samples} sample(s)")
    print(f"  pack → {result.md_path}")
    print(f"  blob → {result.blob_path.name}\n")
    return 0


def _cmd_library(args: argparse.Namespace) -> int:
    from . import library

    vps = library.load_all()
    if not vps:
        print("(no voiceprints enrolled — run: transcribbler enroll --name <name>)")
        return 0
    print(f"{'uid':14}{'name':20}{'samples':>8}  updated")
    for vp in vps:
        print(f"{vp.uid:14}{vp.name:20}{vp.samples:>8}  {vp.updated}")
    return 0


def _cmd_pack_list(args: argparse.Namespace) -> int:
    from . import pack

    packs = pack.find_packs()
    if not packs:
        print("(no packs yet — run `transcribbler listen` or `enroll` to create one)")
        return 0
    print(f"{'uid':10}{'date':12}{'state':10}{'len':>6}  {'title':24} participants")
    for pk in packs:
        m = pk.meta
        date = str(m.get("timestamp", ""))[:10]
        length = m.get("length") or m.get("duration") or ""
        title = str(m.get("title", "(untitled)"))[:23]
        parts = ", ".join(m.get("participants") or [])
        print(f"{pk.uid:10}{date:12}{str(m.get('state', '?')):10}{str(length):>6}  {title:24} {parts}")
    return 0


def _cmd_pack_show(args: argparse.Namespace) -> int:
    from . import pack

    try:
        pk = pack.load_pack(args.uid)
        d = pack.pack_details(pk)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    m = d["meta"]
    print(f"pack {pk.uid}  —  {m.get('title', '(untitled)')}")
    print(f"  blob   : {pk.blob_path.name}")
    print(f"  md     : {pk.md_path.name if pk.md_path else '(none — embedded session.md only)'}")
    print(f"  when   : {m.get('timestamp', '?')}   length {d['duration_s']:.0f}s   state {m.get('state', '?')}")
    print(f"  tags   : {', '.join(m.get('tags') or []) or '-'}")
    print(f"  audio  : {'present (auditionable / re-processable)' if d['has_audio'] else 'stripped (finalized)'}")
    if m.get("voiceprints"):
        print(f"  linked : {', '.join(m['voiceprints'])}")
    if m.get("pending_enrollment"):
        print(f"  flagged: {', '.join(m['pending_enrollment'])}  (name them: pack enroll {pk.uid})")
    print(f"\n  {'speaker':10}{'name':20}{'turns':>6}{'speech':>9}  clip")
    for s in d["speakers"]:
        print(f"  {s['id']:10}{s['name']:20}{s['turns']:>6}{s['speech_s']:>8.0f}s  {'yes' if s['clip'] else '-'}")
    return 0


def _cmd_pack_extract(args: argparse.Namespace) -> int:
    from . import pack

    try:
        pk = pack.load_pack(args.uid)
        vps = pack.extract(pk.blob_path)
        pack.link_voiceprints(pk, vps)  # close the session→voiceprint graph edge (spec §9)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    print(f"extracted {len(vps)} voiceprint(s) from pack {pk.uid}:")
    for vp in vps:
        print(f"  {vp.uid:24}{vp.name:20}{vp.samples:>4} sample(s)")
    return 0


def _play_clip(clip: Path) -> str | None:
    """Play a clip through the first available player; ``None`` on success, else an error message.

    Tries ffplay → paplay → aplay, skipping any that isn't installed and continuing past one that
    errors, so a missing/degraded player never blocks the (audio-optional) audition or enroll flow.
    """
    import subprocess

    failed = []
    for player in (["ffplay", "-autoexit", "-nodisp", "-loglevel", "error"], ["paplay"], ["aplay", "-q"]):
        try:
            subprocess.run([*player, str(clip)], check=True)
            return None
        except FileNotFoundError:
            continue  # this player isn't installed — try the next
        except KeyboardInterrupt:
            return None  # operator stopped playback; that's fine
        except subprocess.CalledProcessError as e:
            failed.append(f"{player[0]} ({e})")  # present but errored — try the next player
    if failed:
        return f"playback failed: {'; '.join(failed)}"
    return "no audio player found (tried ffplay, paplay, aplay)"


def _cmd_pack_audition(args: argparse.Namespace) -> int:
    import tempfile

    from . import pack

    try:
        pk = pack.load_pack(args.uid)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    with tempfile.TemporaryDirectory() as tmp:
        try:
            clip = pack.read_clip(pk, args.speaker, Path(tmp) / "clip")
        except ValueError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        print(f"auditioning {args.speaker} of pack {pk.uid} — Ctrl-C to stop")
        err = _play_clip(clip)
    if err is not None:
        print(f"error: {err}", file=sys.stderr)
        return 2
    return 0


def _cmd_pack_enroll(args: argparse.Namespace) -> int:
    """Guided walk: name each flagged (or given) anonymous speaker → named voiceprint.

    The end of the capture→named-voiceprint loop (ADR-0024/0029): for each speaker the operator
    flagged live (``pending_enrollment``), or each id passed explicitly, audition the clip on
    request, take a name, then relabel + extract *just that speaker* into the library. Named
    speakers drop out of ``pending``; skipped ones stay, so the walk resumes next time.
    """
    import tempfile

    from . import pack

    try:
        pk = pack.load_pack(args.uid)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    raw = args.speaker if args.speaker else (pk.meta.get("pending_enrollment") or [])
    targets = list(dict.fromkeys(raw))  # de-dupe, keep order (a repeated id would double-remove)
    if not targets:
        print(f"pack {pk.uid} has no speakers flagged to remember — flag them live with [e] "
              f"during `listen`, or name one now: transcribbler pack enroll {pk.uid} S1")
        return 0

    by_id = {s["id"]: s for s in pack.pack_details(pk)["speakers"]}
    remaining = list(targets)  # what stays pending after this walk
    print(f"naming {len(targets)} flagged speaker(s) in pack {pk.uid} — a name enrolls, blank skips\n")
    for sid in targets:
        st = by_id.get(sid)
        if st is None:  # flagged id no longer in the record (e.g. re-processed) — drop it
            print(f"  {sid}: not in this pack — dropping from the flagged list")
            remaining.remove(sid)
            continue
        print(f"  {sid}: {st['turns']} turn(s), {st['speech_s']:.0f}s of speech"
              f"{'' if st['clip'] else '  (no isolated clip)'}")
        try:
            while st["clip"] and input(f"    [p] play clip, or Enter to name {sid}: ").strip().lower() == "p":
                with tempfile.TemporaryDirectory() as tmp:
                    clip = pack.read_clip(pk, sid, Path(tmp) / "clip")
                    err = _play_clip(clip)
                if err is not None:
                    print(f"    ({err})")
            name = input(f"    name for {sid} (blank = skip): ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\ncancelled.", file=sys.stderr)
            break
        if not name:
            print(f"    skipped {sid}\n")
            continue
        try:
            pk = pack.relabel(pk, sid, name)
            vps = pack.extract(pk.blob_path, only=[sid])
            pk = pack.link_voiceprints(pk, vps)
        except ValueError as e:
            print(f"    error: {e}", file=sys.stderr)
            continue
        remaining.remove(sid)
        vp = next((v for v in vps if v.uid == f"{pk.uid}-{pack.slug(name)}"), None)
        detail = f"  (voiceprint {vp.uid}, {vp.samples} sample(s))" if vp else ""
        print(f"    ✓ {sid} → {name!r}{detail}\n")

    pk = pack.set_pending(pk, remaining)
    if remaining:
        print(f"{len(remaining)} still flagged — resume with: transcribbler pack enroll {pk.uid}")
    else:
        print("all flagged speakers handled.")
    return 0


def _cmd_pack_relabel(args: argparse.Namespace) -> int:
    from . import pack

    try:
        pk = pack.load_pack(args.uid)
        pk = pack.relabel(pk, args.speaker, args.name)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    print(f"relabeled {args.speaker} → {args.name!r} in pack {pk.uid}")
    print(f"  fold it into the library:  transcribbler pack extract {pk.uid}")
    return 0


def _cmd_pack_delete(args: argparse.Namespace) -> int:
    from . import pack

    try:
        pk = pack.load_pack(args.uid)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    linked = pk.meta.get("voiceprints") or []
    if not args.yes:
        print(f"delete pack {pk.uid} — {pk.meta.get('title', '(untitled)')} ({pk.blob_path.name})?")
        if linked:
            print(f"  note: {len(linked)} voiceprint(s) extracted from it stay in the library.")
        try:
            resp = input("  confirm delete [y/N]: ")
        except (EOFError, KeyboardInterrupt):
            print("\ncancelled.", file=sys.stderr)
            return 1
        if resp.strip().lower() not in ("y", "yes"):
            print("cancelled.")
            return 1
    pack.delete_pack(pk)
    print(f"deleted pack {pk.uid}")
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

    mt = sub.add_parser(
        "meter", help="live dB meter of candidate audio sources — see which is mic vs meeting"
    )
    mt.add_argument("--app", default="Google Chrome", help="meeting app to match for candidates")
    mt.add_argument("--sample", type=float, default=0.4, help="per-source sample seconds (default: 0.4)")
    mt.set_defaults(func=_cmd_meter)

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
    c.add_argument(
        "--bleed-reject-db", type=float, default=6.0,
        help="reject a mic segment as speaker bleed when the meeting channel is louder than the "
             "mic by more than this many dB over its span (default: 6.0; 120 effectively disables)",
    )
    c.add_argument(
        "--denoise", action="store_true",
        help="spectral-denoise the meeting channel before ASR (experimental; may not help)",
    )
    c.add_argument(
        "--no-audio", action="store_true",
        help="don't retain session audio in the pack (smaller; loses re-extract/adjudication "
             "substrate — the transcript, IR record, and voiceprint embeddings are still packed)",
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
    ls.add_argument(
        "--bleed-reject-db", type=float, default=6.0,
        help="reject a mic segment as speaker bleed when the meeting channel is louder than the "
             "mic by more than this many dB over its span (default: 6.0; raise to reject less, "
             "120 effectively disables)",
    )
    ls.add_argument(
        "--denoise", action="store_true",
        help="spectral-denoise the meeting channel before ASR (experimental; may not help)",
    )
    ls.add_argument(
        "--no-audio", action="store_true",
        help="don't retain session audio in the pack (smaller; loses re-extract/adjudication "
             "substrate — the transcript, IR record, and voiceprint embeddings are still packed)",
    )
    ls.set_defaults(func=_cmd_listen)

    e = sub.add_parser("enroll", help="record a read-aloud sample → build/update a named voiceprint")
    e.add_argument("--name", required=True, help="speaker name for the voiceprint")
    e.add_argument("--seconds", type=int, default=30, help="recording length (default: 30)")
    e.add_argument("--mic", help="override: PipeWire source to record from")
    e.add_argument("--app", default="Google Chrome", help="app to match for mic path detection")
    e.add_argument("-p", "--profile", help="compute profile (auto-selected if omitted)")
    e.set_defaults(func=_cmd_enroll)

    lib = sub.add_parser("library", help="list enrolled voiceprints")
    lib.set_defaults(func=_cmd_library)

    pk = sub.add_parser("pack", help="inspect and curate session packs (list/show/extract/audition/relabel)")
    pk.set_defaults(func=lambda _a: (pk.print_help() or 0))  # bare `pack` → its own help
    pk_sub = pk.add_subparsers(dest="pack_cmd")
    pk_sub.add_parser("list", help="list session packs").set_defaults(func=_cmd_pack_list)
    ps = pk_sub.add_parser("show", help="show a pack's speakers, audio, and graph edges")
    ps.add_argument("uid", help="pack uid (or a unique prefix)")
    ps.set_defaults(func=_cmd_pack_show)
    pe = pk_sub.add_parser("extract", help="fold a pack's speaker embeddings into the voiceprint library")
    pe.add_argument("uid", help="pack uid (or a unique prefix)")
    pe.set_defaults(func=_cmd_pack_extract)
    pa = pk_sub.add_parser("audition", help="play a speaker's isolated clip to identify who they are")
    pa.add_argument("uid", help="pack uid (or a unique prefix)")
    pa.add_argument("speaker", help="canonical speaker id (e.g. S1)")
    pa.set_defaults(func=_cmd_pack_audition)
    pr = pk_sub.add_parser("relabel", help="name a speaker (e.g. S1 → Priya) and re-pack the bundle")
    pr.add_argument("uid", help="pack uid (or a unique prefix)")
    pr.add_argument("speaker", help="canonical speaker id (e.g. S1)")
    pr.add_argument("name", help="display name to assign")
    pr.set_defaults(func=_cmd_pack_relabel)
    pen = pk_sub.add_parser(
        "enroll", help="guided walk: name the speakers flagged live ([e]) → named voiceprints"
    )
    pen.add_argument("uid", help="pack uid (or a unique prefix)")
    pen.add_argument(
        "speaker", nargs="*",
        help="speaker id(s) to name (default: the pack's flagged pending_enrollment list)",
    )
    pen.set_defaults(func=_cmd_pack_enroll)
    pd = pk_sub.add_parser("delete", help="delete a pack (blob + sidecar); leaves derived voiceprints")
    pd.add_argument("uid", help="pack uid (or a unique prefix)")
    pd.add_argument("-y", "--yes", action="store_true", help="skip the confirmation prompt")
    pd.set_defaults(func=_cmd_pack_delete)

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
