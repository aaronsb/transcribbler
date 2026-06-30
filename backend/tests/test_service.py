"""Tests for the HTTP service (ADR-0018), CI-safe: the pipeline and profiles are
mocked, so no engines/binaries/audio are needed. Covers the job lifecycle over
the wire, server-side render, the queued/progress/done SSE stream, cancel, and
the ownership leak guard.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import transcribbler.service.jobs as jobs_mod
from transcribbler import profiles
from transcribbler.profiles import Profile, StageConfig
from transcribbler.progress import ProgressEvent
from transcribbler.service.app import create_app
from transcribbler.service.jobs import Job, JobStatus, JobStore, new_job_id

_IR = {
    "schema_version": "0.1",
    "source": {"kind": "batch", "uri": "file:///x.wav", "sha256": "0" * 64, "duration_s": 1.0},
    "backend": {"kind": "modular", "asr": "whisper.cpp:vulkan"},
    "speakers": [{"id": "S1", "source": "fallback"}],
    "turns": [{"speaker_id": "S1", "start": 0.0, "end": 1.0, "text": "hi"}],
}


def _fake_profile() -> Profile:
    return Profile(
        name="test",
        asr=StageConfig(engine="whisper.cpp", backend="vulkan"),
        diar=StageConfig(engine="none"),
        llm=StageConfig(engine="none"),
    )


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(profiles, "available", lambda: ["test"])
    monkeypatch.setattr(profiles, "resolve", lambda arg=None: Path("test.toml"))
    monkeypatch.setattr(profiles, "load", lambda path: _fake_profile())

    def fake_run(audio, profile, *, asr_progress=None, **kw):
        # touch the progress sink so the SSE `progress` path is exercised
        if asr_progress is not None:
            asr_progress(ProgressEvent(stage="asr", completed=50, total=100))
        return _IR

    monkeypatch.setattr(jobs_mod, "run_pipeline", fake_run)
    with TestClient(create_app(principal="uid:test")) as c:
        yield c


def _submit(client, **data) -> str:
    r = client.post("/v1/jobs", files={"file": ("a.wav", b"audio-bytes")}, data={"profile": "test", **data})
    assert r.status_code == 202, r.text
    return r.json()["id"]


def _wait(client, job_id, target, tries=100) -> dict:
    for _ in range(tries):
        body = client.get(f"/v1/jobs/{job_id}").json()
        if body["status"] == target:
            return body
        time.sleep(0.01)
    raise AssertionError(f"job never reached {target!r}: {body}")


# --- store unit tests (no event loop needed) ------------------------------------


def _job(owner="a") -> Job:
    return Job(id=new_job_id(), owner=owner, profile=_fake_profile(), audio_path=Path("/tmp/none"))


def test_ownership_is_the_leak_guard():
    store = JobStore()
    job = _job(owner="a")
    store.submit(job)
    assert store.get(job.id, "a") is job
    assert store.get(job.id, "b") is None  # other principal can't see it
    assert store.get("deadbeef", "a") is None  # unknown id


def test_cancel_queued_emits_canceled_and_renumbers():
    store = JobStore()
    j1, j2 = _job(), _job()
    store.submit(j1)
    store.submit(j2)
    store.cancel(j1, by="a")
    assert j1.status == JobStatus.canceled
    assert any(e["event"] == "canceled" for e in j1.events)
    assert j1 not in store.pending
    # j2 moved up to position 1
    last_queued = [e for e in j2.events if e["event"] == "queued"][-1]
    assert last_queued["position"] == 1 and last_queued["ahead"] == 0


def test_cancel_queued_removes_temp_upload(tmp_path):
    # A job canceled while still queued never enters _run, so its cleanup must
    # happen in cancel() — otherwise the (large) upload leaks.
    store = JobStore()
    audio = tmp_path / "upload.wav"
    audio.write_bytes(b"audio")
    job = Job(id=new_job_id(), owner="a", profile=_fake_profile(), audio_path=audio)
    store.submit(job)
    store.cancel(job, by="a")
    assert not audio.exists()


# --- endpoint tests -------------------------------------------------------------


def test_healthz_version_profiles(client):
    assert client.get("/v1/healthz").json() == {"status": "ok"}
    v = client.get("/v1/version").json()
    assert v["wire_version"] == "1" and v["ir_schema_version"] == "0.1"
    assert "md" in v["capabilities"]["formats"]
    profs = client.get("/v1/profiles").json()
    assert profs == [{"name": "test", "asr": "whisper.cpp:vulkan", "diar": None, "llm": None}]


def test_unknown_profile_is_rejected(client):
    r = client.post("/v1/jobs", files={"file": ("a.wav", b"x")}, data={"profile": "nope"})
    assert r.status_code == 400 and r.json()["detail"]["code"] == "bad_input"


def test_empty_upload_is_rejected(client):
    r = client.post("/v1/jobs", files={"file": ("a.wav", b"")}, data={"profile": "test"})
    assert r.status_code == 400 and r.json()["detail"]["code"] == "bad_input"


def test_job_lifecycle_and_render(client):
    job_id = _submit(client, diarize="false", canon="false")
    done = _wait(client, job_id, "done")
    assert done["ir"]["turns"][0]["text"] == "hi"
    md = client.get(f"/v1/jobs/{job_id}/result", params={"format": "md"})
    assert md.status_code == 200 and "hi" in md.text
    assert client.get(f"/v1/jobs/{job_id}/result", params={"format": "json"}).status_code == 200


def test_result_before_done_is_409(client):
    # craft a still-running job directly in the store
    job = _job(owner="uid:test")
    job.status = JobStatus.running
    client.app.state.store.jobs[job.id] = job
    r = client.get(f"/v1/jobs/{job.id}/result")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "not_ready"


def test_unknown_job_is_404(client):
    assert client.get("/v1/jobs/deadbeef").status_code == 404


def test_sse_replays_queued_progress_done(client):
    job_id = _submit(client, diarize="false", canon="false")
    _wait(client, job_id, "done")  # let it finish, then replay the stream deterministically
    events = []
    with client.stream("GET", f"/v1/jobs/{job_id}/events") as r:
        for line in r.iter_lines():
            if line.startswith("event:"):
                events.append(line.split(":", 1)[1].strip())
            if line.strip() == "" and "done" in events:
                break
    assert events[0] == "queued"
    assert "progress" in events
    assert events[-1] == "done"


def test_cancel_endpoint(client):
    # cancel a job we placed as queued so it resolves deterministically to canceled
    job = _job(owner="uid:test")
    client.app.state.store.jobs[job.id] = job
    client.app.state.store.pending.append(job)
    r = client.delete(f"/v1/jobs/{job.id}")
    assert r.status_code == 200 and r.json()["status"] == "canceled"


def test_cancel_running_job_signals_intent(client):
    # A running job can't be killed mid-flight; DELETE records the request and
    # the response reflects it so the client isn't left guessing.
    job = _job(owner="uid:test")
    job.status = JobStatus.running
    client.app.state.store.jobs[job.id] = job
    body = client.delete(f"/v1/jobs/{job.id}").json()
    assert body["status"] == "running" and body["cancel_requested"] is True
