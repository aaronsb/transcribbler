"""Transport-agnostic progress (ADR-0017 / ADR-0018).

A core parses its engine's progress output into a :class:`ProgressEvent` and
feeds it to a :data:`ProgressSink`. The sink decides what to do with it: the CLI
renders a live bar to the terminal; the HTTP service turns it into an SSE
``progress`` event. The core never knows the transport.

The sink returns an optional string so it can drive the terminal path through
``proc.run_streamed`` (which owns the ``\\r``/dangling-line handling): the CLI
sink returns the text to display, the service sink returns ``None`` and emits an
event as a side effect.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class ProgressEvent:
    """One progress reading from a stage.

    ``stage`` is the coarse pipeline stage (``"asr"`` / ``"diar"``). ``step`` is
    an optional finer sub-step within the stage (the diarizer reports
    ``segmentation``/``embeddings``/...); ASR has none.
    """

    stage: str
    completed: float
    total: float
    step: str | None = None

    @property
    def pct(self) -> int:
        return min(100, int(100 * self.completed / self.total)) if self.total else 0


# Given a structured event, optionally return a terminal string to write to
# stderr (the CLI path); return None to display nothing (the service path emits
# SSE as a side effect). Returning the string keeps run_streamed's \r/dangling
# handling intact for the terminal.
ProgressSink = Callable[[ProgressEvent], "str | None"]


def line_tap(
    parse: Callable[[str], ProgressEvent | None], sink: ProgressSink
) -> Callable[[str], str | None]:
    """Adapt a per-line ``parse`` + a ``sink`` into a ``run_streamed`` on_line.

    Non-progress lines (``parse`` returns None) are passed through as None so
    ``run_streamed`` suppresses them; progress lines flow to the sink.
    """

    def on_line(line: str) -> str | None:
        ev = parse(line)
        return None if ev is None else sink(ev)

    return on_line


def stderr_sink() -> ProgressSink:
    """A live terminal renderer, fresh per stage (matches the pre-refactor CLI).

    ASR shows a single ``ASR N%`` bar; the diarizer shows a per-step bar and
    starts a new line when the step changes so each finished step stays visible.
    Identical readings are deduped; the line is newline-terminated at 100%.
    """
    last_step: str | None = None
    last_pct = -1

    def render(ev: ProgressEvent) -> str | None:
        nonlocal last_step, last_pct
        if ev.step is None:  # ASR: single bar
            if ev.pct == last_pct:
                return None
            last_pct = ev.pct
            return f"\r  ASR {ev.pct:3d}%" + ("\n" if ev.pct >= 100 else "")
        # diarizer: per-step bar; newline on step change keeps finished steps visible
        if ev.step == last_step and ev.pct == last_pct:
            return None
        prefix = "\n" if last_step not in (None, ev.step) else "\r"
        last_step, last_pct = ev.step, ev.pct
        return f"{prefix}  diar {ev.step:<13s} {ev.pct:3d}%" + ("\n" if ev.pct >= 100 else "")

    return render
