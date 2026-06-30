"""The ASGI app + ``/v1`` routes (ADR-0018).

One app, bound to either a Unix socket or TCP by ``serve.py``. Routes are thin:
validate input, own the job, hand off to :class:`~.jobs.JobStore`; the pipeline
and render are reused unchanged from the backend.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import Response, StreamingResponse

from .. import profiles
from ..render import render
from .jobs import TERMINAL_EVENTS, Job, JobStatus, JobStore, new_job_id
from .models import (
    WIRE_VERSION,
    ErrorInfo,
    IR_SCHEMA_VERSION,
    JobCreated,
    JobState,
    ProfileInfo,
    VersionInfo,
)
from .principal import get_principal, local_principal

_MEDIA = {"md": "text/markdown; charset=utf-8", "vtt": "text/vtt; charset=utf-8", "json": "application/json"}


def _bad_input(message: str) -> HTTPException:
    return HTTPException(status_code=400, detail={"code": "bad_input", "message": message})


def _not_found() -> HTTPException:
    # Missing and not-owned look identical on purpose (no id enumeration).
    return HTTPException(status_code=404, detail={"code": "not_found", "message": "no such job"})


def _save_upload(upload: UploadFile) -> Path:
    suffix = Path(upload.filename or "audio").suffix
    fd, tmp = tempfile.mkstemp(prefix="transcribbler_job_", suffix=suffix)
    with os.fdopen(fd, "wb") as f:
        shutil.copyfileobj(upload.file, f)
    return Path(tmp)


def _resolve_profile(name: str) -> profiles.Profile:
    """Resolve a client-supplied profile *name* against the server allowlist.

    A client may send a bare name (must be in ``available()``) or omit it for
    server-side auto-select. It may never send a path — that would let a remote
    caller point the engine at an arbitrary file (ADR-0017/0018).
    """
    name = (name or "").strip()
    if name and name not in profiles.available():
        raise _bad_input(f"unknown profile {name!r}; available: {', '.join(profiles.available()) or '(none)'}")
    try:
        profile = profiles.load(profiles.resolve(name or None))
    except profiles.ProfileError as e:
        raise _bad_input(str(e))
    if not profile.asr.enabled:
        raise _bad_input(f"profile {profile.name!r} has no ASR stage")
    return profile


def _sse(event: dict) -> str:
    payload = {k: v for k, v in event.items() if k != "event"}
    return f"event: {event['event']}\ndata: {json.dumps(payload)}\n\n"


def _job_state(job: Job) -> JobState:
    err = None
    if job.status == JobStatus.error:
        err = ErrorInfo(code=job.error_code or "internal", message=job.error_message or "")
    return JobState(
        id=job.id,
        status=job.status.value,
        profile=job.profile.name,
        diarize=job.diarize,
        canon=job.canon,
        error=err,
        ir=job.ir if job.status == JobStatus.done else None,
    )


def create_app(*, principal: str | None = None) -> FastAPI:
    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        store = JobStore()
        app.state.store = store
        worker = asyncio.create_task(store.run_forever())
        try:
            yield
        finally:
            worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await worker

    app = FastAPI(title="transcribbler", version=WIRE_VERSION, lifespan=lifespan)
    app.state.principal = principal or local_principal()

    def store() -> JobStore:
        return app.state.store

    @app.get("/v1/healthz")
    async def healthz() -> dict:
        return {"status": "ok"}

    @app.get("/v1/version", response_model=VersionInfo)
    async def version() -> VersionInfo:
        return VersionInfo(
            wire_version=WIRE_VERSION,
            ir_schema_version=IR_SCHEMA_VERSION,
            capabilities={"formats": list(_MEDIA), "diarize": True, "canon": True},
        )

    @app.get("/v1/profiles", response_model=list[ProfileInfo])
    async def list_profiles() -> list[ProfileInfo]:
        out: list[ProfileInfo] = []
        for name in profiles.available():
            try:
                p = profiles.load(profiles.resolve(name))
            except Exception:
                continue  # a broken profile shouldn't blank the whole list
            stage = lambda s: f"{s.engine}:{s.backend}" if s.enabled else None  # noqa: E731
            out.append(ProfileInfo(name=p.name, asr=stage(p.asr), diar=stage(p.diar), llm=stage(p.llm)))
        return out

    @app.post("/v1/jobs", response_model=JobCreated, status_code=202)
    async def submit_job(
        file: UploadFile = File(...),
        profile: str = Form(""),
        diarize: bool = Form(True),
        canon: bool = Form(True),
        prompt: str | None = Form(None),
        principal: str = Depends(get_principal),
    ) -> JobCreated:
        resolved = _resolve_profile(profile)
        audio = _save_upload(file)
        if audio.stat().st_size == 0:
            audio.unlink(missing_ok=True)
            raise _bad_input("empty audio upload")
        job = Job(
            id=new_job_id(),
            owner=principal,
            profile=resolved,
            audio_path=audio,
            diarize=diarize,
            canon=canon,
            prompt=prompt or None,
        )
        store().submit(job)
        return JobCreated(id=job.id, status=job.status.value)

    @app.get("/v1/jobs/{job_id}", response_model=JobState)
    async def get_job(job_id: str, principal: str = Depends(get_principal)) -> JobState:
        job = store().get(job_id, principal)
        if job is None:
            raise _not_found()
        return _job_state(job)

    @app.get("/v1/jobs/{job_id}/events")
    async def job_events(job_id: str, principal: str = Depends(get_principal)) -> StreamingResponse:
        job = store().get(job_id, principal)
        if job is None:
            raise _not_found()

        async def stream() -> AsyncIterator[str]:
            cursor = 0
            while True:
                while cursor < len(job.events):
                    ev = job.events[cursor]
                    cursor += 1
                    yield _sse(ev)
                    if ev["event"] in TERMINAL_EVENTS:
                        return
                await job.wait_for_event()

        return StreamingResponse(stream(), media_type="text/event-stream")

    @app.get("/v1/jobs/{job_id}/result")
    async def job_result(
        job_id: str, format: str = "json", principal: str = Depends(get_principal)
    ) -> Response:
        if format not in _MEDIA:
            raise _bad_input(f"unknown format {format!r}; one of {', '.join(_MEDIA)}")
        job = store().get(job_id, principal)
        if job is None:
            raise _not_found()
        if job.status != JobStatus.done or job.ir is None:
            raise HTTPException(
                status_code=409,
                detail={"code": "not_ready", "message": f"job is {job.status.value}, not done"},
            )
        return Response(content=render(job.ir, format), media_type=_MEDIA[format])

    @app.delete("/v1/jobs/{job_id}", response_model=JobState)
    async def cancel_job(job_id: str, principal: str = Depends(get_principal)) -> JobState:
        job = store().get(job_id, principal)
        if job is None:
            raise _not_found()
        store().cancel(job, principal)
        return _job_state(job)

    return app
