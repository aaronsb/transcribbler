"""Live capture MVP — rolling two-channel transcription to disk.

A local spike, in the direction of ADR-0009 (capture cadence) and ADR-0022 (live
audio ingest). NOT the sessionized HTTP service those describe: this proves the
live path end-to-end by capturing the operator's mic and the meeting output as
two separate channels, transcribing rolling chunks, and appending a live
transcript to disk.

Why two channels: the mic is a *deterministic* discriminator for the local
operator (empirically no meeting bleed on a headset / echo-cancelled source), so
"you" are identified by channel — no diarization, consistent across every chunk.
Only the meeting channel needs diarization, and only to separate the *remote*
speakers. Per-chunk wall-time is logged: the pyannote cold-start is the known
bottleneck, and this spike is how we measure whether it needs a persistent daemon.

Path detection matches the *activated* audio routes (which sink the meeting app is
actually playing into, which source it captures from) rather than trusting the
system defaults — the meeting is frequently not on the default sink.
"""

from __future__ import annotations

import array
import json
import math
import re
import shutil
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from . import capture_persist
from .attribution import Attributed, is_repeat, split_segment_by_turns
from .cores import asr_core
from .diarizer_daemon import DiarizerDaemon
from .profiles import Profile
from .session_gallery import SessionGallery

Log = Callable[[str], None]


class SourceError(RuntimeError):
    """No usable audio source could be resolved (e.g. nothing is playing yet)."""


# ---- audio-path detection (follow active routing, not defaults) -------------


@dataclass(frozen=True)
class Paths:
    mic: str  # PulseAudio/PipeWire source: the operator's (echo-cancelled) mic
    meeting: tuple[str, ...]  # monitor source(s) the meeting is routed into; mixed to one channel


def _pactl(*args: str) -> str:
    return subprocess.run(["pactl", *args], capture_output=True, text=True, check=True).stdout


def _short_map(kind: str) -> dict[str, str]:
    """index -> name for `pactl list short {sinks|sources}`."""
    out = _pactl("list", "short", kind)
    m: dict[str, str] = {}
    for line in out.splitlines():
        cols = line.split("\t")
        if len(cols) >= 2:
            m[cols[0]] = cols[1]
    return m


def _blocks(kind: str) -> list[dict[str, str]]:
    """Parse `pactl list {sink-inputs|source-outputs}` into per-stream dicts.

    Each dict carries the routing index ("route") plus the application.name.
    """
    text = _pactl("list", kind)
    blocks: list[dict[str, str]] = []
    cur: dict[str, str] | None = None
    route_key = "Sink:" if kind == "sink-inputs" else "Source:"
    for raw in text.splitlines():
        line = raw.strip()
        if re.match(r"(Sink Input|Source Output) #\d+", line):
            cur = {}
            blocks.append(cur)
        elif cur is not None:
            if line.startswith(route_key):
                cur["route"] = line.split(":", 1)[1].strip()
            elif line.startswith("application.name"):
                cur["app"] = line.split("=", 1)[1].strip().strip('"')
    return [b for b in blocks if "route" in b]


def _mean_volume_db(source: str, secs: float = 1.5) -> float:
    """Mean volume (dBFS) of a short capture of `source`; -120.0 if silent/absent.

    A wedged device (capture hangs past the timeout) folds into the same silent sentinel so a
    single stalled source degrades to an empty bar in the meter rather than killing the loop.
    """
    try:
        proc = subprocess.run(
            ["ffmpeg", "-hide_banner", "-f", "pulse", "-i", source, "-t", f"{secs}",
             "-af", "volumedetect", "-f", "null", "/dev/null"],
            capture_output=True, text=True, timeout=secs + 6,
        )
    except subprocess.TimeoutExpired:
        return -120.0
    m = re.search(r"mean_volume:\s*(-?\d+(?:\.\d+)?)\s*dB", proc.stderr)
    return float(m.group(1)) if m else -120.0


def _pw_dump() -> list[dict]:
    """Parse ``pw-dump`` — the live PipeWire object graph; ``[]`` if unavailable."""
    try:
        out = subprocess.run(["pw-dump"], capture_output=True, text=True, timeout=10).stdout
        return json.loads(out)
    except (OSError, subprocess.SubprocessError, ValueError):
        return []


def _routed_sources(app: str, dump: list[dict] | None = None) -> tuple[str | None, list[str]]:
    """Follow the app's *active* PipeWire routing to its capturable sources.

    The active link graph is ground truth: a one-shot guess or a default-sink
    fallback strands capture on a monitor nothing plays into (the silent-capture
    bug). Instead we read where the app is really wired *right now* —

    - **meeting**: every sink the app's audio-output node is linked into, captured
      at that sink's ``.monitor``. The app may split across several sinks (an effects
      sink plus a real device); all are returned so the caller can mix them, since
      which one carries audible audio drifts and only the live signal knows.
    - **mic**: the source the app's audio-input node is linked *from*.

    Returns ``(mic | None, [meeting monitors])``. Matching is loose (``app`` as a
    substring of ``node.name``/``application.name``) so "Google Chrome" also finds
    the "Google Chrome input" capture stream.
    """
    dump = _pw_dump() if dump is None else dump
    nodes: dict[int, dict] = {}
    links: list[tuple[int | None, int | None]] = []
    for o in dump:
        t = o.get("type")
        if t == "PipeWire:Interface:Node":
            nid = o.get("id")
            if nid is not None:  # stay defensive: a malformed dump must not raise
                nodes[nid] = (o.get("info", {}) or {}).get("props", {}) or {}
        elif t == "PipeWire:Interface:Link":
            info = o.get("info", {}) or {}
            links.append((info.get("output-node-id"), info.get("input-node-id")))

    a = app.lower()

    def matches(p: dict) -> bool:
        hay = ((p.get("node.name") or "") + " " + (p.get("application.name") or "")).lower()
        return a in hay

    out_nodes = {i for i, p in nodes.items()
                 if p.get("media.class") == "Stream/Output/Audio" and matches(p)}
    in_nodes = {i for i, p in nodes.items()
                if p.get("media.class") == "Stream/Input/Audio" and matches(p)}

    meeting: list[str] = []
    for o_id, i_id in links:
        if o_id in out_nodes:
            sink = nodes.get(i_id, {})
            if sink.get("media.class") == "Audio/Sink":
                name = sink.get("node.name")
                if name and f"{name}.monitor" not in meeting:
                    meeting.append(f"{name}.monitor")

    mic: str | None = None
    for o_id, i_id in links:
        if i_id in in_nodes:
            mic = nodes.get(o_id, {}).get("node.name")
            if mic:
                break

    return mic, meeting


def detect_paths(app: str = "Google Chrome", log: Log | None = None) -> Paths:
    """Resolve the meeting app's mic source and meeting monitor(s) from live routing.

    Meeting: every sink the app is *actively routed into*, captured at its monitor —
    no default-sink fallback, because a monitor nothing is routed to is silent, which
    is exactly the dead-capture failure this avoids. An empty result is honest: the
    caller reports "nothing is playing yet" rather than recording silence.
    Mic: the source the app captures from (graph → pactl input stream → default).
    """
    say = log or (lambda _m: None)
    mic, meeting = _routed_sources(app)

    if not mic:  # app isn't capturing — its pactl input stream, else the default source
        sources = _short_map("sources")
        for b in _blocks("source-outputs"):
            if app.lower() in b.get("app", "").lower():
                mic = sources.get(b["route"])
                break
        if not mic:
            mic = _pactl("get-default-source").strip()

    if meeting:
        for m in meeting:
            say(f"  meeting : {m}")
    else:
        say(f"  meeting : (none — nothing is routed from {app!r} yet)")
    say(f"  mic     : {mic}")
    return Paths(mic=mic, meeting=tuple(meeting))


# ---- capture + transcribe loop ----------------------------------------------


@dataclass
class Turn:
    start: float  # absolute session seconds
    end: float
    speaker: str
    text: str


class Controls:
    """Thread-safe run controls for the interactive console: pause + stop.

    A keyboard-listener thread flips these; the capture loop polls them. Pausing
    discards incoming chunks (listening off), it does not pause the ffmpeg capture.
    """

    def __init__(self) -> None:
        self._paused = threading.Event()
        self._stopped = threading.Event()

    def toggle_pause(self) -> None:
        self._paused.clear() if self._paused.is_set() else self._paused.set()

    @property
    def paused(self) -> bool:
        return self._paused.is_set()

    def stop(self) -> None:
        self._stopped.set()

    @property
    def stopped(self) -> bool:
        return self._stopped.is_set()


_FFMPEG_OP_TIMEOUT = 60  # seconds; a per-chunk ffmpeg op hanging longer is dropped
_MAX_BACKLOG = 8         # unprocessed chunks tolerated before dropping the oldest (disk guard)


def _capture_filter(n_meeting: int) -> str:
    """Build the ffmpeg ``-filter_complex`` for mic(L) + a mix of ``n_meeting`` monitors(R).

    Input 0 is the mic; inputs 1..n are the meeting monitors. Each is downmixed to mono
    16k; the meeting monitors are *summed* (``amix normalize=0``) so the loud route stays
    at full level regardless of how many silent routes ride alongside it (averaging would
    attenuate it by the count of dead siblings). The result is joined mic→L, meeting→R.
    """
    if n_meeting < 1:
        raise SourceError("no meeting sources to capture")  # guarded upstream; belt-and-suspenders
    mic_part = "[0:a]aresample=16000,pan=mono|c0=c0[m]"
    if n_meeting == 1:
        mtg_parts = ["[1:a]aresample=16000,pan=mono|c0=c0[s]"]
    else:
        pre = [f"[{i}:a]aresample=16000,pan=mono|c0=c0[s{i}]" for i in range(1, n_meeting + 1)]
        mix = "".join(f"[s{i}]" for i in range(1, n_meeting + 1)) + f"amix=inputs={n_meeting}:normalize=0[s]"
        mtg_parts = [*pre, mix]
    return ";".join([mic_part, *mtg_parts, "[m][s]join=inputs=2:channel_layout=stereo[o]"])


def _ffmpeg_segmenter(paths: Paths, workdir: Path, segment_s: int, stderr) -> subprocess.Popen:
    """Start ffmpeg capturing mic(L) + a mix of the meeting monitor(s)(R) into 16k chunks."""
    fc = _capture_filter(len(paths.meeting))
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "pulse", "-i", paths.mic]
    for m in paths.meeting:
        cmd += ["-f", "pulse", "-i", m]
    cmd += [
        "-filter_complex", fc, "-map", "[o]",
        "-f", "segment", "-segment_time", str(segment_s), "-reset_timestamps", "1",
        str(workdir / "chunk_%05d.wav"),
    ]
    # stderr goes to a file the caller drains, not a PIPE nobody reads: an unread
    # PIPE fills the OS buffer (~64KB) on a long capture and blocks ffmpeg silently.
    return subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=stderr)


def _split_channel(chunk: Path, dst: Path, channel: int) -> Path:
    """Extract one channel (0=mic, 1=meeting) of a stereo chunk to a mono wav."""
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(chunk),
         "-af", f"pan=mono|c0=c{channel}", "-ar", "16000", "-ac", "1", str(dst)],
        check=True, timeout=_FFMPEG_OP_TIMEOUT,
    )
    return dst


def _mean_db_file(wav: Path) -> float:
    try:
        proc = subprocess.run(
            ["ffmpeg", "-hide_banner", "-i", str(wav), "-af", "volumedetect", "-f", "null", "/dev/null"],
            capture_output=True, text=True, timeout=_FFMPEG_OP_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return -120.0  # treat a hung probe as silence → chunk gets skipped
    m = re.search(r"mean_volume:\s*(-?\d+(?:\.\d+)?)\s*dB", proc.stderr)
    return float(m.group(1)) if m else -120.0


def _energy_envelope(wav: Path, *, frame_s: float = 0.05, decim: int = 4) -> list[float]:
    """Per-frame RMS (dBFS) loudness curve of a mono 16k wav.

    Reads the PCM once and decimates for speed (the backend has no numpy); each entry
    is one ``frame_s``-second frame. Lets the operator and meeting channels be compared
    span-by-span for bleed rejection.
    """
    try:
        proc = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(wav),
             "-ar", "16000", "-ac", "1", "-f", "s16le", "-"],
            capture_output=True, timeout=_FFMPEG_OP_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return []
    a = array.array("h")
    a.frombytes(proc.stdout[: len(proc.stdout) // 2 * 2])
    if decim > 1:
        a = a[::decim]
    win = max(1, int((16000 // decim) * frame_s))
    env: list[float] = []
    for i in range(0, len(a), win):
        seg = a[i : i + win]
        if not seg:
            break
        rms = math.sqrt(sum(x * x for x in seg) / len(seg))
        env.append(20 * math.log10(rms / 32768) if rms > 0 else -120.0)
    return env


def _mean_db(env: list[float], s0: float, s1: float, frame_s: float = 0.05) -> float:
    """Mean loudness (dBFS) of ``env`` over ``[s0, s1]`` seconds, averaged in power."""
    if not env:
        return -120.0
    lo = max(0, int(s0 / frame_s))
    hi = min(len(env), int(s1 / frame_s) + 1)
    frames = env[lo:hi] or [env[min(lo, len(env) - 1)]]
    lin = [10 ** (d / 10) for d in frames]
    return 10 * math.log10(sum(lin) / len(lin))


def _denoise(src: Path, dst: Path) -> Path:
    """Spectral denoise (ffmpeg afftdn) — no model/dependency; for the meeting channel."""
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src),
         "-af", "afftdn=nr=12", "-ar", "16000", "-ac", "1", str(dst)],
        check=True, timeout=_FFMPEG_OP_TIMEOUT,
    )
    return dst


def _concat(chunks: list[Path], dst: Path) -> Path:
    """Concatenate same-format stereo chunks into one wav (pass a single through)."""
    if len(chunks) == 1:
        return chunks[0]
    inputs: list[str] = []
    for p in chunks:
        inputs += ["-i", str(p)]
    streams = "".join(f"[{i}:a]" for i in range(len(chunks)))
    fc = f"{streams}concat=n={len(chunks)}:v=0:a=1[o]"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *inputs,
         "-filter_complex", fc, "-map", "[o]", str(dst)],
        check=True, timeout=_FFMPEG_OP_TIMEOUT,
    )
    return dst


def _window_plan(
    k: int, *, contiguous: bool, terminal: bool, segment_s: int
) -> tuple[list[int], float, float, float, tuple[float, float]]:
    """Chunk indices, window start, emit region and shared span for window ``k``.

    An interior window spans chunks ``[k-1, k]`` and is *centered* on the chunk
    boundary ``k*segment_s`` — so that boundary is interior to the window and words
    across it decode with context on both sides. Its emit region is the middle
    ``segment_s`` seconds ``[k*seg - half, k*seg + half)``; adjacent windows' emit
    regions tile exactly. A non-contiguous window (cold start or after a gap) has no
    left neighbour, so it is a single chunk emitting from its own start. A terminal
    window (final drain) extends its emit region to the end of its audio.
    """
    half = segment_s / 2
    if contiguous:
        chunks_idx = [k - 1, k]
        win_start = (k - 1) * segment_s
        emit_lo = k * segment_s - half
        shared = (float(win_start), float(win_start + segment_s))  # chunk k-1, shared with prev
    else:
        chunks_idx = [k]
        win_start = k * segment_s
        emit_lo = float(win_start)
        shared = (0.0, 0.0)
    emit_hi = float((k + 1) * segment_s) if terminal else k * segment_s + half
    return chunks_idx, float(win_start), emit_lo, emit_hi, shared


def _transcribe_window(
    window_wav: Path, win_start: float, emit_lo: float, emit_hi: float,
    profile: Profile, workdir: Path,
    *, daemon: DiarizerDaemon | None, gallery: SessionGallery | None,
    prev_sid_turns: list[tuple[float, float, str]], shared_span: tuple[float, float],
    gate_db: float, operator_label: str, bleed_reject_db: float, denoise: bool, say: Log,
) -> tuple[list[Turn], list[tuple[float, float, str]]]:
    """Transcribe one overlapping window; emit only turns in ``[emit_lo, emit_hi)``.

    ASR runs on the whole window so words near the emit edges have context on both
    sides, but a segment is emitted only when its start falls in the emit region —
    the overlap with neighbouring windows is decoding context, not output, so nothing
    double-emits. The meeting channel is diarized over the whole window; labels are
    linked to session ids by ``gallery.assign_window`` (temporal-first, ADR-0027) and
    each emitted segment is split at speaker-change boundaries (Decision 2a).

    Returns ``(emitted turns, this window's meeting sid-turns)`` — the sid-turns
    (absolute seconds) become the next window's ``prev_sid_turns``.
    """
    asr = asr_core(profile.asr)
    turns: list[Turn] = []

    def _owned(seg) -> bool:
        return emit_lo <= win_start + seg.start < emit_hi

    mic_wav = _split_channel(window_wav, workdir / "mic.wav", 0)
    mtg_wav = _split_channel(window_wav, workdir / "meeting.wav", 1)
    # loudness curves to reject far-end bleed from the operator channel
    mic_env = _energy_envelope(mic_wav)
    mtg_env = _energy_envelope(mtg_wav)

    # operator channel (mic = channel 0): identified by channel, no diarization. A
    # segment is kept only if the mic is *near-end dominant* over its span — if the
    # meeting channel is louder there by more than bleed_reject_db, it's the far end
    # coming back through the mic (speaker bleed), not the operator.
    if _mean_db_file(mic_wav) >= gate_db:
        for s in asr.transcribe(mic_wav):
            if not (s.text.strip() and _owned(s)):
                continue
            if _mean_db(mtg_env, s.start, s.end) - _mean_db(mic_env, s.start, s.end) > bleed_reject_db:
                continue  # meeting dominates this span → bleed, not the operator
            turns.append(Turn(win_start + s.start, win_start + s.end, operator_label, s.text.strip()))

    # meeting channel (channel 1): ASR (optionally denoised), diarize the RAW audio
    # (embeddings from the unfiltered signal), link, split-attribute
    sid_turns: list[tuple[float, float, str]] = []
    if _mean_db_file(mtg_wav) >= gate_db:
        asr_wav = _denoise(mtg_wav, workdir / "meeting_dn.wav") if denoise else mtg_wav
        segs = [s for s in asr.transcribe(asr_wav) if s.text.strip()]
        if daemon is not None and gallery is not None and segs:
            try:
                res = daemon.diarize(mtg_wav)
                local_turns = [
                    (win_start + t["start"], win_start + t["end"], t["label"])
                    for t in res.get("turns", [])
                ]
                mapping = gallery.assign_window(
                    res.get("speakers", []), local_turns, prev_sid_turns, shared_span
                )
                sid_turns = [(a0, a1, mapping.get(lbl, "Remote")) for a0, a1, lbl in local_turns]
            except Exception as e:  # daemon flaked — degrade to one remote bucket
                say(f"  (diarize failed, remote→one speaker: {e})")
                sid_turns = []
        for s in segs:
            if not _owned(s):
                continue  # context region — owned by a neighbouring window
            a0, a1 = win_start + s.start, win_start + s.end
            text = s.text.strip()
            pieces = (
                split_segment_by_turns(a0, a1, text, sid_turns, default="Remote")
                if sid_turns
                else [Attributed(a0, a1, "Remote", text)]
            )
            for p in pieces:
                turns.append(Turn(p.start, p.end, p.speaker, p.text))

    turns.sort(key=lambda t: t.start)
    return turns, sid_turns


def _fmt_ts(sec: float) -> str:
    m, s = divmod(int(sec), 60)
    return f"{m:02d}:{s:02d}"


def run_capture(
    profile: Profile,
    out_path: Path,
    *,
    app: str = "Google Chrome",
    mic: str | None = None,
    meeting: str | None = None,
    segment_s: int = 45,
    diarize: bool = True,
    threshold: float = 0.5,
    gate_db: float = -55.0,
    bleed_reject_db: float = 6.0,
    denoise: bool = False,
    operator_label: str = "You",
    retain_audio: bool = True,
    on_turn: Callable[[Turn], None] | None = None,
    on_chunk: Callable[[int, int, float, bool], None] | None = None,
    on_new_speaker: Callable[[str], None] | None = None,
    controls: Controls | None = None,
    banner: bool = True,
    workdir: Path | None = None,
    log: Log | None = None,
) -> None:
    """Capture live audio and append a rolling transcript to ``out_path``.

    Runs until interrupted (Ctrl-C). Window N (chunks N-1..N) is transcribed once
    chunk N+1 opens; overlapping windows give ASR context and stable speaker
    linking (ADR-0027).
    """
    say = log or (lambda _m: None)

    # Resolve sources first — before creating any dirs — so an early "nothing is
    # routed yet" failure raises cleanly instead of stranding empty session/scratch
    # dirs. CLI overrides win; otherwise follow the app's live routing (a --meeting
    # override pins a single source; detection may return several to mix).
    detected = detect_paths(app, log=say) if not (mic and meeting) else None
    mic_final = mic or (detected.mic if detected else None)
    if meeting:
        meeting_final: tuple[str, ...] = (meeting,)
    else:
        meeting_final = detected.meeting if detected else ()
    if not meeting_final:
        raise SourceError(
            f"no meeting audio: nothing is routed from {app!r} yet. Start the meeting "
            f"playing first, run `transcribbler meter` to see live source levels, or "
            f"pass --meeting <source>."
        )
    if not mic_final:
        raise SourceError("no mic source resolved; pass --mic <source>.")
    paths = Paths(mic=mic_final, meeting=meeting_final)

    work = workdir or (out_path.parent / f".{out_path.stem}.capture")
    work.mkdir(parents=True, exist_ok=True)
    retain = work / "retain"  # processed chunks kept here for the session pack's audio
    if retain_audio:
        retain.mkdir(exist_ok=True)

    def _retire(idx: int) -> None:
        """A processed chunk is done streaming — keep it for the pack, or drop it."""
        src = work / f"chunk_{idx:05d}.wav"
        if retain_audio and src.exists():
            src.replace(retain / f"chunk_{idx:05d}.wav")
        else:
            src.unlink(missing_ok=True)

    use_diar = diarize and profile.diar.enabled
    gallery = SessionGallery(threshold, on_new_speaker=on_new_speaker) if use_diar else None
    daemon = DiarizerDaemon(profile.diar.model, work, log=say) if use_diar else None

    if banner:
        say(f"capturing → {out_path} (Ctrl-C to stop)")
    out = None
    proc = None
    ff_log = None
    processed: set[int] = set()
    prev_sid_turns: list[tuple[float, float, str]] = []
    recent_emitted: list[Turn] = []  # previous window's turns, to dedup the shared seam
    session_turns: list[Turn] = []  # every emitted turn, for the session record (IR)
    last_k = -2  # last window index emitted; -2 so window 0 reads as non-contiguous

    def _chunk_indices() -> list[int]:
        idx = []
        for p in work.glob("chunk_*.wav"):
            try:
                idx.append(int(p.stem.split("_")[1]))
            except (IndexError, ValueError):
                pass  # ignore anything that isn't chunk_NNNNN.wav
        return sorted(idx)

    def _process(k: int, *, terminal: bool = False) -> None:
        nonlocal daemon, prev_sid_turns, recent_emitted, session_turns, last_k
        if daemon is not None and not daemon.is_alive():
            say("  diarizer stopped — remaining audio → single 'Remote' speaker")
            daemon.close()
            daemon = None  # latch off: stop retrying + logging every window

        contiguous = k == last_k + 1 and k >= 1
        chunks_idx, win_start, emit_lo, emit_hi, shared = _window_plan(
            k, contiguous=contiguous, terminal=terminal, segment_s=segment_s
        )
        chunk_paths = [work / f"chunk_{i:05d}.wav" for i in chunks_idx]
        if not all(p.exists() for p in chunk_paths):  # a neighbour was dropped — go fresh
            contiguous = False
            chunks_idx, win_start, emit_lo, emit_hi, shared = _window_plan(
                k, contiguous=False, terminal=terminal, segment_s=segment_s
            )
            chunk_paths = [work / f"chunk_{k:05d}.wav"]
        prev = prev_sid_turns if contiguous else []

        t0 = time.monotonic()
        try:
            window_wav = _concat(chunk_paths, work / "window.wav")
            turns, sid_turns = _transcribe_window(
                window_wav, win_start, emit_lo, emit_hi, profile, work,
                daemon=daemon, gallery=gallery, prev_sid_turns=prev, shared_span=shared,
                gate_db=gate_db, operator_label=operator_label,
                bleed_reject_db=bleed_reject_db, denoise=denoise, say=say,
            )
        except Exception as e:  # one bad window must not kill the whole session
            say(f"  window {k:05d}: skipped ({type(e).__name__}: {e})")
            # keep the geometry contiguous so chunk k (still retained) is emitted by
            # window k+1; only the linking context is lost → fall back to embeddings
            prev_sid_turns = []
            last_k = k
        else:
            dt = time.monotonic() - t0
            written: list[Turn] = []
            for t in turns:
                # drop a turn the overlapping neighbour already emitted (shared-seam
                # duplicate); the time-overlap guard keeps genuine later repeats
                if any(is_repeat(t.start, t.end, t.text, r.start, r.end, r.text) for r in recent_emitted):
                    continue
                out.write(f"[{_fmt_ts(t.start)}] {t.speaker}: {t.text}\n")
                if on_turn is not None:
                    on_turn(t)
                written.append(t)
            out.flush()
            if on_chunk is not None:
                on_chunk(k, len(written), dt, dt < segment_s)
            else:
                say(f"  window {k:05d}: {len(written)} turns in {dt:.1f}s "
                    f"({'keeps up' if dt < segment_s else 'LAGS'} vs {segment_s}s)")
            recent_emitted = written
            session_turns.extend(written)  # accumulate for the session record (IR)
            # sid_turns feeds the next window's temporal link; it is empty when this
            # window's meeting channel was silent or diarization failed, which forces
            # the next window onto the embedding fallback (known lag, ADR-0027)
            prev_sid_turns = sid_turns
            last_k = k
        finally:
            processed.add(k)
            _retire(k - 1)  # left chunk is done streaming → retain for the pack (or drop)
            if terminal:
                _retire(k)

    try:
        out = out_path.open("a")
        out.write(f"# transcript — {profile.name} — segment {segment_s}s\n\n")
        out.flush()
        ff_log = (work / "ffmpeg.log").open("wb")
        proc = _ffmpeg_segmenter(paths, work, segment_s, ff_log)
        if daemon is not None:
            daemon.start()  # one-time model load; ffmpeg is already capturing
        while not (controls is not None and controls.stopped):
            if proc.poll() is not None:
                tail = (work / "ffmpeg.log").read_bytes()[-500:].decode(errors="replace")
                raise RuntimeError(f"ffmpeg capture exited ({proc.returncode}): {tail}")
            ready = _chunk_indices()
            # backlog guard: if transcription can't keep up, drop the oldest
            # unprocessed chunks (loudly) rather than filling the disk.
            backlog = [n for n in ready if n not in processed]
            if len(backlog) > _MAX_BACKLOG:
                for n in backlog[:-_MAX_BACKLOG]:
                    (work / f"chunk_{n:05d}.wav").unlink(missing_ok=True)
                    processed.add(n)
                say(f"  backlog > {_MAX_BACKLOG}: dropped {len(backlog) - _MAX_BACKLOG} old chunk(s)")
                ready = _chunk_indices()
            # a chunk is complete once the *next* one exists (ffmpeg finalizes N's
            # header before opening N+1)
            for n in [n for n in ready if (n + 1) in ready and n not in processed]:
                if controls is not None and controls.paused:
                    (work / f"chunk_{n:05d}.wav").unlink(missing_ok=True)
                    processed.add(n)  # listening off: discard rather than transcribe
                    continue
                _process(n)
            time.sleep(1.0)
    except KeyboardInterrupt:
        say("\nstopping…")
    finally:
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        if ff_log is not None:
            ff_log.close()
        # ffmpeg has exited, so every remaining chunk on disk is finalized — drain
        # the tail as windows; the last one is terminal (emits to the end of audio).
        if out is not None:
            remaining = [n for n in _chunk_indices() if n not in processed]
            for pos, n in enumerate(remaining):
                if controls is not None and controls.paused:
                    (work / f"chunk_{n:05d}.wav").unlink(missing_ok=True)
                    processed.add(n)  # quit-while-paused: discard the muted tail
                else:
                    _process(n, terminal=(pos == len(remaining) - 1))  # drain uses the daemon
            out.close()
            # persist the session as a real pack: the blob's record.ir.json is the schema-
            # validated source of truth, out_path becomes its .md sidecar, and speaker-isolated
            # clips + the gallery's centroids seed the durable voiceprint library (ADR-0028).
            if session_turns:
                try:
                    session_wav = (
                        capture_persist.assemble_session(retain, segment_s, retain / "session.wav")
                        if retain_audio else None
                    )
                    result = capture_persist.persist_session_pack(
                        [(t.start, t.end, t.speaker, t.text) for t in session_turns],
                        profile,
                        operator_label=operator_label,
                        diarized=use_diar,
                        centroids=gallery.centroids() if gallery is not None else {},
                        session_wav=session_wav,
                        out_path=out_path,
                    )
                    say(f"  session pack → {result.md_path.name} + {result.blob_path.name}")
                    shutil.rmtree(retain, ignore_errors=True)  # packed → free the raw chunks
                except Exception as e:
                    # keep retain/ on failure so the session's audio is recoverable, not lost
                    say(f"  (session pack failed, kept raw transcript; audio at {retain}: {e})")
            else:
                # nothing transcribed → no pack; free any retained chunks so disk isn't leaked
                shutil.rmtree(retain, ignore_errors=True)
        if daemon is not None:
            daemon.close()
