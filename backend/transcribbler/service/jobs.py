"""Server-owned async jobs (ADR-0018).

Work outlives a request, so a submission becomes a :class:`Job` that the client
observes over SSE and which survives disconnect. Jobs run **FIFO single-flight**:
one worker drains a pending list one job at a time. This is deliberately *not*
the ADR-0019 scheduler (no VRAM admission, no live-mode priority) — just an
honest queue that produces real ``queued {position, ahead}`` events.

Threading note (flagged in the PR #16 review): a progress sink is stateful and
is invoked from ``run_streamed``'s pump thread, so each job builds its own sinks
and marshals every event back onto the event loop via ``call_soon_threadsafe``.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from ..pipeline import run_pipeline
from ..profiles import Profile
from ..progress import ProgressEvent, ProgressSink

TERMINAL_EVENTS = ("done", "error", "canceled")


class JobStatus(str, Enum):
    queued = "queued"
    running = "running"
    done = "done"
    error = "error"
    canceled = "canceled"


_TERMINAL = {JobStatus.done, JobStatus.error, JobStatus.canceled}


@dataclass
class Job:
    id: str
    owner: str
    profile: Profile
    audio_path: Path  # server-side temp copy of the upload; removed on terminal
    diarize: bool = True
    canon: bool = True
    prompt: str | None = None
    status: JobStatus = JobStatus.queued
    ir: dict | None = None
    error_code: str | None = None
    error_message: str | None = None
    canceled_by: str | None = None
    events: list[dict] = field(default_factory=list)
    cancel_requested: bool = False
    _waiters: list[asyncio.Future] = field(default_factory=list)

    def emit(self, event: dict) -> None:
        """Append an event and wake every SSE subscriber. Loop-thread only."""
        self.events.append(event)
        for w in self._waiters:
            if not w.done():
                w.set_result(None)
        self._waiters.clear()

    async def wait_for_event(self) -> None:
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._waiters.append(fut)
        try:
            await fut
        finally:  # a client disconnecting mid-await shouldn't leave its future parked
            with contextlib.suppress(ValueError):
                self._waiters.remove(fut)


def _discard_audio(job: Job) -> None:
    """Remove a job's server-side temp upload. Safe to call more than once."""
    with contextlib.suppress(OSError):
        job.audio_path.unlink()


def _classify(exc: Exception) -> tuple[str, str]:
    """Map a pipeline exception to an ADR-0018 error code + message.

    Best-effort heuristics — the engines don't expose typed errors yet, so we
    sniff the message. Unknown failures are ``internal`` so a client retries
    rather than giving up.
    """
    msg = str(exc)
    low = msg.lower()
    if "hf_token" in low or "gated" in low:
        return "auth", msg
    if "out of memory" in low or "oom" in low or "vk_error_out_of_device_memory" in low:
        return "oom", msg
    if isinstance(exc, FileNotFoundError) or "ffmpeg" in low or "could not normalize" in low:
        return "bad_input", msg
    return "internal", msg


class JobStore:
    """Holds jobs, the pending FIFO, and the single worker that drains it."""

    def __init__(self) -> None:
        # TODO(ADR-0013/0014): `jobs` grows unbounded — terminal jobs + their IR
        # stay resident for the life of the process. Add eviction/TTL (or the
        # durable store) when retention lands; fine for the single-user UDS default.
        self.jobs: dict[str, Job] = {}
        self.pending: list[Job] = []
        self._wake = asyncio.Event()

    def submit(self, job: Job) -> None:
        self.jobs[job.id] = job
        self.pending.append(job)
        pos = len(self.pending)
        job.emit({"event": "queued", "position": pos, "ahead": pos - 1})
        self._wake.set()

    def get(self, job_id: str, owner: str) -> Job | None:
        """Return the job iff it exists *and* the caller owns it.

        Ownership + unguessable ids are the ADR-0018 leak guard: a principal can
        only ever see its own jobs, and a missing-vs-forbidden answer is the same
        (caller can't probe for ids it doesn't own).
        """
        job = self.jobs.get(job_id)
        if job is None or job.owner != owner:
            return None
        return job

    def cancel(self, job: Job, by: str) -> None:
        if job.status in _TERMINAL:
            return
        job.canceled_by = by
        if job.status == JobStatus.queued:
            if job in self.pending:
                self.pending.remove(job)
            job.status = JobStatus.canceled
            job.emit({"event": "canceled", "by": by})
            _discard_audio(job)  # _run never runs for a queued-canceled job
            self._renumber()
        else:
            # Running: best-effort. We can't interrupt the in-flight subprocess
            # yet (real cancel + lease release is ADR-0011); the worker emits
            # `canceled` and discards the IR when the current run returns.
            job.cancel_requested = True

    def _renumber(self) -> None:
        for i, job in enumerate(self.pending):
            job.emit({"event": "queued", "position": i + 1, "ahead": i})

    async def _next(self) -> Job:
        while True:
            while self.pending:
                job = self.pending.pop(0)
                if job.status == JobStatus.canceled:
                    _discard_audio(job)  # canceled between pop and here
                    continue
                self._renumber()
                return job
            self._wake.clear()
            await self._wake.wait()

    async def run_forever(self) -> None:
        """The worker loop. One job at a time (single-flight)."""
        while True:
            job = await self._next()
            try:
                await self._run(job)
            except Exception:  # one job must never wedge the queue; CancelledError still propagates
                job.error_code, job.error_message = "internal", "worker fault"
                job.status = JobStatus.error
                job.emit({"event": "error", "code": "internal", "message": "worker fault"})
                _discard_audio(job)

    async def _run(self, job: Job) -> None:
        loop = asyncio.get_running_loop()

        def make_sink(stage: str) -> ProgressSink:
            def sink(ev: ProgressEvent) -> None:
                loop.call_soon_threadsafe(
                    job.emit,
                    {
                        "event": "progress",
                        "stage": ev.stage,
                        "step": ev.step,
                        "completed": ev.completed,
                        "total": ev.total,
                        "pct": ev.pct,
                    },
                )
                return None

            return sink

        job.status = JobStatus.running
        try:
            ir = await asyncio.to_thread(
                run_pipeline,
                job.audio_path,
                job.profile,
                diarize=job.diarize,
                canon=job.canon,
                prompt=job.prompt,
                asr_progress=make_sink("asr"),
                diar_progress=make_sink("diar"),
            )
            if job.cancel_requested:
                job.status = JobStatus.canceled
                job.emit({"event": "canceled", "by": job.canceled_by or job.owner})
            else:
                job.ir = ir
                job.status = JobStatus.done
                job.emit({"event": "done", "ir_ref": f"/v1/jobs/{job.id}"})
        except Exception as exc:  # boundary: translate to a wire error code
            job.error_code, job.error_message = _classify(exc)
            job.status = JobStatus.error
            job.emit({"event": "error", "code": job.error_code, "message": job.error_message})
        finally:
            _discard_audio(job)


def new_job_id() -> str:
    return uuid.uuid4().hex
