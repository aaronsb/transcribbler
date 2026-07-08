"""Live audio-source meter — *see* which source carries the meeting vs the mic.

Audio routing drifts between runs (a meeting tab hops sinks; the default sink is often silent),
so ``capture.detect_paths`` can guess the wrong monitor. This meter shows a live dB bar per
candidate source so the operator pins the routes from what they *see*, not what detection guessed:
play the meeting → the loud **MEETING** row is ``--meeting``; speak → the loud **MIC** row is
``--mic``. On exit it prints the flags to paste. Reuses the PulseAudio/ffmpeg helpers in
``capture`` (which is already over the size limit — this lives on its own).
"""

from __future__ import annotations

import concurrent.futures as cf
import sys
from dataclasses import dataclass

from . import capture

_FLOOR_DB = -60.0  # bottom of the bar; louder than this fills it


@dataclass
class Source:
    name: str  # the PulseAudio source (a *.monitor for meeting, an input source for mic)
    role: str  # "meeting" | "mic"
    auto: bool  # whether capture.detect_paths would pick this one


def candidates(app: str) -> list[Source]:
    """Assemble the meeting-monitor and mic-source candidates, flagging detection's pick.

    Meeting candidates are the monitors of the sinks the app plays into, plus the default sink's
    monitor; mic candidates are the sources the app captures from, plus the default source.
    """
    sinks, sources = capture._short_map("sinks"), capture._short_map("sources")
    picked = capture.detect_paths(app)

    meeting: list[str] = []
    for b in capture._blocks("sink-inputs"):
        if app.lower() in b.get("app", "").lower():
            name = sinks.get(b["route"])
            if name and f"{name}.monitor" not in meeting:
                meeting.append(f"{name}.monitor")
    default_monitor = f"{capture._pactl('get-default-sink').strip()}.monitor"
    if default_monitor not in meeting:
        meeting.append(default_monitor)

    mics: list[str] = []
    for b in capture._blocks("source-outputs"):
        if app.lower() in b.get("app", "").lower():
            name = sources.get(b["route"])
            if name and name not in mics:
                mics.append(name)
    default_source = capture._pactl("get-default-source").strip()
    if default_source not in mics:
        mics.append(default_source)

    return (
        [Source(n, "meeting", n == picked.meeting) for n in meeting]
        + [Source(n, "mic", n == picked.mic) for n in mics]
    )


def _bar(db: float, width: int = 24) -> str:
    frac = max(0.0, min(1.0, (db - _FLOOR_DB) / (0.0 - _FLOOR_DB)))
    filled = round(frac * width)
    return "█" * filled + "░" * (width - filled)


def _recommend(sources: list[Source], peak: dict[str, float]) -> dict[str, str]:
    """Loudest source (by session peak) in each role — the meter's exit suggestion."""
    out: dict[str, str] = {}
    for role in ("mic", "meeting"):
        in_role = [s for s in sources if s.role == role]
        if in_role:
            out[role] = max(in_role, key=lambda s: peak[s.name]).name
    return out


def _short(name: str) -> str:
    """Trim a monitor/source name to something readable in a fixed column."""
    n = name[: -len(".monitor")] if name.endswith(".monitor") else name
    return n if len(n) <= 40 else "…" + n[-39:]


def _frame(sources: list[Source], levels: dict[str, float], peak: dict[str, float]) -> list[str]:
    loudest = {role: (max((s for s in sources if s.role == role),
                          key=lambda s: levels[s.name], default=None)) for role in ("meeting", "mic")}
    lines: list[str] = []
    for role, title in (("meeting", "MEETING (monitors)"), ("mic", "MIC (inputs)")):
        lines.append(f"  {title}")
        for s in sources:
            if s.role != role:
                continue
            db = levels[s.name]
            hot = "►" if loudest[role] is s and db > _FLOOR_DB + 6 else " "
            tags = " ".join(t for t in (("auto" if s.auto else ""),) if t)
            lines.append(f"  {hot} {_bar(db)} {db:6.0f} dB  {_short(s.name):<40} {tags}")
    return lines


def run_meter(app: str = "Google Chrome", *, sample_s: float = 0.4) -> int:
    """Loop: sample every candidate source concurrently, redraw bars, until Ctrl-C."""
    try:
        sources = candidates(app)
    except Exception as e:  # pactl/ffmpeg not available, or no PulseAudio
        print(f"error: could not enumerate audio sources ({e})", file=sys.stderr)
        return 2
    if not sources:
        print("no audio sources found (is PulseAudio/PipeWire running?)", file=sys.stderr)
        return 1

    names = [s.name for s in sources]
    peak = {n: -120.0 for n in names}
    tty = sys.stdout.isatty()
    print(f"audio meter — app match {app!r}, Ctrl-C to stop\n"
          "play the meeting to light a MEETING row; speak to light a MIC row (► = loudest now)\n")
    drawn = 0
    try:
        while True:
            with cf.ThreadPoolExecutor(max_workers=len(names)) as ex:
                levels = dict(zip(names, ex.map(lambda n: capture._mean_volume_db(n, sample_s), names)))
            for n, v in levels.items():
                peak[n] = max(peak[n], v)
            frame = _frame(sources, levels, peak)
            if tty and drawn:
                sys.stdout.write(f"\033[{drawn}A")
            sys.stdout.write("\n".join(frame) + "\n")
            sys.stdout.flush()
            drawn = len(frame)
    except KeyboardInterrupt:
        rec = _recommend(sources, peak)
        print("\n\nloudest this session — paste to pin the routes:")
        flags = " ".join(f"--{'mic' if r == 'mic' else 'meeting'} {rec[r]!r}" for r in ("mic", "meeting") if r in rec)
        print(f"  transcribbler listen {flags}\n")
        return 0
