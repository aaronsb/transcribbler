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

import re
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .cores import asr_core
from .diarizer_daemon import DiarizerDaemon
from .profiles import Profile
from .session_gallery import SessionGallery

Log = Callable[[str], None]


# ---- audio-path detection (match activated routes, not defaults) ------------


@dataclass(frozen=True)
class Paths:
    mic: str  # PulseAudio/PipeWire source: the operator's (echo-cancelled) mic
    meeting: str  # monitor source: where the meeting audio is actually played


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
    """Mean volume (dBFS) of a short capture of `source`; -120.0 if silent/absent."""
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-f", "pulse", "-i", source, "-t", f"{secs}",
         "-af", "volumedetect", "-f", "null", "/dev/null"],
        capture_output=True, text=True, timeout=secs + 6,
    )
    m = re.search(r"mean_volume:\s*(-?\d+(?:\.\d+)?)\s*dB", proc.stderr)
    return float(m.group(1)) if m else -120.0


def detect_paths(app: str = "Google Chrome", log: Log | None = None) -> Paths:
    """Resolve the meeting app's *activated* mic source and output monitor.

    Meeting output: the app may drive several sinks; probe each candidate's
    monitor and pick the one with real energy (the default sink is often silent).
    Mic: the source the app's input stream captures from, else the system default.
    """
    say = log or (lambda _m: None)
    sinks, sources = _short_map("sinks"), _short_map("sources")

    # meeting output — candidate monitors from the app's playback streams
    cand: list[str] = []
    for b in _blocks("sink-inputs"):
        if app.lower() in b.get("app", "").lower():
            name = sinks.get(b["route"])
            if name and f"{name}.monitor" not in cand:
                cand.append(f"{name}.monitor")
    if not cand:  # app not playing anywhere we can see — fall back to default sink
        cand = [f"{_pactl('get-default-sink').strip()}.monitor"]

    if len(cand) == 1:
        meeting = cand[0]
    else:
        scored = sorted(((c, _mean_volume_db(c)) for c in cand), key=lambda x: x[1], reverse=True)
        say("  meeting-path probe: " + ", ".join(f"{c.split('.monitor')[0]}={v:.0f}dB" for c, v in scored))
        meeting = scored[0][0]

    # operator mic — the source the app's *input* stream uses
    mic = None
    for b in _blocks("source-outputs"):
        if app.lower() in b.get("app", "").lower():
            mic = sources.get(b["route"])
            break
    if not mic:
        mic = _pactl("get-default-source").strip()

    say(f"  mic     : {mic}")
    say(f"  meeting : {meeting}")
    return Paths(mic=mic, meeting=meeting)


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


def _ffmpeg_segmenter(paths: Paths, workdir: Path, segment_s: int, stderr) -> subprocess.Popen:
    """Start ffmpeg capturing mic(L)+meeting(R) into rolling stereo 16k chunks."""
    fc = (
        "[0:a]aresample=16000,pan=mono|c0=c0[m];"
        "[1:a]aresample=16000,pan=mono|c0=c0[s];"
        "[m][s]join=inputs=2:channel_layout=stereo[o]"
    )
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-f", "pulse", "-i", paths.mic,
        "-f", "pulse", "-i", paths.meeting,
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


def _overlap(a0: float, a1: float, b0: float, b1: float) -> float:
    return max(0.0, min(a1, b1) - max(a0, b0))


def _transcribe_chunk(
    chunk: Path, offset_s: float, profile: Profile, workdir: Path,
    *, daemon: DiarizerDaemon | None, gallery: SessionGallery | None,
    gate_db: float, operator_label: str, say: Log,
) -> list[Turn]:
    """Two-channel transcription of one chunk → absolute-timed turns.

    Operator channel → ASR labeled by channel. Meeting channel → ASR; if a diarizer
    daemon is given, each ASR segment is attributed to the *session-stable* speaker
    id (via the gallery) whose diarized turn overlaps it most. Without a daemon, all
    remote speech collapses to a single ``Remote`` bucket.
    """
    asr = asr_core(profile.asr)
    turns: list[Turn] = []

    # operator channel: ASR only, gated so whisper doesn't hallucinate on silence
    mic_wav = _split_channel(chunk, workdir / "mic.wav", 0)
    if _mean_db_file(mic_wav) >= gate_db:
        for s in asr.transcribe(mic_wav):
            if s.text.strip():
                turns.append(Turn(offset_s + s.start, offset_s + s.end, operator_label, s.text.strip()))

    # meeting channel: ASR, then attribute each segment to a session-stable speaker
    mtg_wav = _split_channel(chunk, workdir / "meeting.wav", 1)
    if _mean_db_file(mtg_wav) >= gate_db:
        segs = [s for s in asr.transcribe(mtg_wav) if s.text.strip()]
        stitched: list[tuple[float, float, str]] | None = None
        if daemon is not None and gallery is not None and segs:
            try:
                res = daemon.diarize(mtg_wav)
                mapping = gallery.assign_chunk(res.get("speakers", []))
                stitched = [
                    (t["start"], t["end"], mapping.get(t["label"], "Remote"))
                    for t in res.get("turns", [])
                ]
            except Exception as e:  # daemon flaked — degrade to one remote bucket
                say(f"  (diarize failed, remote→one speaker: {e})")
                stitched = None
        for s in segs:
            label = "Remote"
            if stitched:
                best = max(stitched, key=lambda t: _overlap(s.start, s.end, t[0], t[1]), default=None)
                if best is not None and _overlap(s.start, s.end, best[0], best[1]) > 0.0:
                    label = best[2]
            turns.append(Turn(offset_s + s.start, offset_s + s.end, label, s.text.strip()))

    turns.sort(key=lambda t: t.start)
    return turns


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
    operator_label: str = "You",
    on_turn: Callable[[Turn], None] | None = None,
    controls: Controls | None = None,
    workdir: Path | None = None,
    log: Log | None = None,
) -> None:
    """Capture live audio and append a rolling transcript to ``out_path``.

    Runs until interrupted (Ctrl-C). Chunk N is transcribed once chunk N+1 opens.
    """
    say = log or (lambda _m: None)
    work = workdir or (out_path.parent / f".{out_path.stem}.capture")
    work.mkdir(parents=True, exist_ok=True)

    paths = (
        Paths(mic=mic, meeting=meeting)
        if mic and meeting
        else detect_paths(app, log=say)
    )
    if mic:
        paths = Paths(mic=mic, meeting=paths.meeting)
    if meeting:
        paths = Paths(mic=paths.mic, meeting=meeting)

    use_diar = diarize and profile.diar.enabled
    gallery = SessionGallery(threshold) if use_diar else None
    daemon = DiarizerDaemon(profile.diar.model, work, log=say) if use_diar else None

    say(f"capturing → {out_path} (Ctrl-C to stop)")
    out = None
    proc = None
    ff_log = None
    processed: set[int] = set()

    def _chunk_indices() -> list[int]:
        idx = []
        for p in work.glob("chunk_*.wav"):
            try:
                idx.append(int(p.stem.split("_")[1]))
            except (IndexError, ValueError):
                pass  # ignore anything that isn't chunk_NNNNN.wav
        return sorted(idx)

    def _process(n: int) -> None:
        nonlocal daemon
        if daemon is not None and not daemon.is_alive():
            say("  diarizer stopped — remaining audio → single 'Remote' speaker")
            daemon.close()
            daemon = None  # latch off: stop retrying + logging every chunk
        chunk = work / f"chunk_{n:05d}.wav"
        t0 = time.monotonic()
        try:
            turns = _transcribe_chunk(
                chunk, n * segment_s, profile, work,
                daemon=daemon, gallery=gallery, gate_db=gate_db,
                operator_label=operator_label, say=say,
            )
        except Exception as e:  # one bad chunk must not kill the whole session
            say(f"  chunk {n:05d}: skipped ({type(e).__name__}: {e})")
        else:
            dt = time.monotonic() - t0
            for t in turns:
                out.write(f"[{_fmt_ts(t.start)}] {t.speaker}: {t.text}\n")
                if on_turn is not None:
                    on_turn(t)
            out.flush()
            say(f"  chunk {n:05d}: {len(turns)} turns in {dt:.1f}s "
                f"({'keeps up' if dt < segment_s else 'LAGS'} vs {segment_s}s)")
        finally:
            processed.add(n)
            chunk.unlink(missing_ok=True)

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
        # the tail (incl. the last chunk, which never got an N+1 successor).
        if out is not None:
            for n in _chunk_indices():
                if n not in processed:
                    _process(n)  # drain uses the daemon, so close it after
            out.close()
        if daemon is not None:
            daemon.close()
