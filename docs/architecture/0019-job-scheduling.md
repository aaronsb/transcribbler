# ADR-0019: Job scheduling — admission, concurrency, and live priority

- **Status**: Proposed
- **Date**: 2026-06-29
- **Deciders**: Aaron

## Context

[ADR-0018](0018-client-facing-wire.md) defines *one* job's lifecycle (server-owned,
async, survives disconnect). But a backend has finite VRAM and one (or few) GPUs,
and real use contends: a bulk directory of files, a YouTube backlog, and an ad-hoc
live session all want the GPU at once — sometimes while a *game* already holds it.

Without a scheduler the choices are all bad: OOM-fail when VRAM is short, block the
whole service on one long file, or let a latency-critical live session lose to a
batch job. We need **admission** (does it fit right now?), **concurrency** (how many
at once?), **priority** (live vs batch), and **queueing** (defer, don't fail, when
busy).

Two facts shape the design:

- **ASR is already chunked** ([ADR-0005](0005-diarization-flow.md): global diarize,
  chunk ASR). So a batch job has a natural **work-unit (a chunk)** — a safe boundary
  to suspend at and resume from. (`whisper-cli` can't be checkpointed mid-call, but
  it *can* be stopped between chunks.)
- **The spool already persists work** ([ADR-0012](0012-capture-store-and-forward.md)).
  The queue is not new infrastructure — it *is* the spool, given ordering and
  admission.

## Decision

A **server-side scheduler** mediates all jobs on a backend, extending ADR-0018's
lifecycle with admission, a queue, and priority.

### Two job classes

- **Live.** Latency-critical, **top priority**, holds the
  [ADR-0011](0011-idle-unload-keepalive-lease.md) keep-alive lease while active;
  not preemptable by batch. **Multiple live sessions may run at once** (several
  accounts / devices). The backend cannot *guarantee* real-time for all of them on one
  GPU — but it **never drops audio**: each live session captures to its own spool
  ([ADR-0012](0012-capture-store-and-forward.md)), and transcription runs as capacity
  allows, degrading gracefully from real-time to catch-up under contention. **Capture
  is decoupled from transcription** — the thing that must never fail (recording the
  audio) is independent of the thing that can lag (turning it into text).
- **Batch.** Submit many; they queue, run, and can be **deferred and suspended**.
  Preemptable at a work-unit (chunk) boundary.

### Admission control — by VRAM budget

A job is **admitted only if its profile's model set fits the device's free VRAM**
(whisper + maybe pyannote + maybe llama). Capacity is **read from the device**, not a
constant — 16GB on cube, 24GB on the desktop, and *less* when a game is resident.
A job that doesn't fit **queues** (persisted via the spool) rather than OOM-failing;
it is admitted later when VRAM frees.

### Concurrency — serial + pipelined by default

One heavy GPU job at a time, but **overlap stages** (ASR of the next chunk/job while
diarization of the current runs) and **overlap CPU/IO** (ffmpeg normalize, `yt-dlp`
downloads) with GPU work. True N-way GPU parallelism is **opt-in, only with measured
headroom** (small models, or the 24GB card running ASR+diarize concurrently) —
because on a single GPU naive parallelism usually adds VRAM and overhead without
improving wall-clock.

### Ownership & fair queueing

**Every job is owned by the principal that submitted it** (the account each transport
authenticates — [ADR-0020](0020-remote-access-security.md)). The queue is therefore
**per-requester aware**, not a single anonymous line:

- A principal can list/cancel **its own** jobs; it does not see others' content.
- Scheduling within the batch class is **fair across requesters** (round-robin over
  owners), so one account's 400-file backlog can't starve another's single file.
- The client is told its **position** — the wire (ADR-0018) emits a `queued
  {position, ahead}` event, updated as the line moves: *"3 jobs ahead of you" → 2 → 1
  → running*. This is the same SSE stream that later carries progress.

This is what makes a single backend usable by several trusted people/devices at once
(multi-user, not multi-tenant — ADR-0020).

### Priority — drain-and-suspend

When **any** live session starts while batch is running:

1. Stop **admitting** new batch work.
2. Let the in-flight batch unit **finish its current chunk** (bounded wait — one
   chunk, not one whole file, thanks to ADR-0005), then **suspend** the batch job
   (its progress is spooled).
3. Free the VRAM and give the GPU to live.

No work is killed; live waits at most a chunk's worth of drain. When live ends, the
suspended batch job **resumes** from its next chunk. Among **multiple** live sessions
that can't all stay real-time, the GPU is **fair-shared** and each session's audio
keeps spooling — slower text, never lost audio.

### Lifecycle (extends ADR-0018)

`queued` (admitted-pending-resources) → `running` → optional `paused`
(batch yielded to live) → `done` / `failed` / `canceled`. Live always outranks batch;
within the batch class, ordering is FIFO **fair across requesters** (no single owner
starves the rest). Empty queue **and** no live session → idle TTL → unload
(ADR-0011), freeing the GPU for games/other work — the whole point.

### Scope

Per-backend scheduling only. **Cross-host distribution** (routing jobs across desktop
+ cube, [ADR-0002](0002-hardware-topology.md)) is explicitly future work — each
backend owns its own queue first.

## Consequences

### Good

- Busy/again-later "just works": jobs queue instead of failing when VRAM is short
  (game running, card full).
- Live is never starved by batch, and batch never loses committed work — drain, don't
  kill.
- Honest about single-GPU reality; no false promise of parallel speedup, but real
  pipelining/IO-overlap wins are taken.
- The queue reuses the spool; idle-unload still reclaims the GPU when truly idle.
- **Multiple live sessions never lose audio** — capture is decoupled from
  transcription, so contention costs latency, not data.
- **Several trusted accounts share one backend fairly** — per-requester ownership +
  fair queueing + visible "N ahead of you," with no multi-tenant machinery.

### Bad / costs

- A real scheduler is meaningful backend complexity: a queue, admission, suspend/
  resume, VRAM accounting.
- **VRAM cost estimation is approximate** — model footprints vary with backend
  (Vulkan/ROCm/CUDA) and settings; admission needs a margin and may mis-estimate.
- Live start has a small, bounded latency (one chunk's drain) unless VRAM is reserved
  (a tunable, not the default).
- Suspend/resume requires batch jobs to be genuinely chunk-resumable end to end
  (ASR chunks + global diarize re-run or cached) — a constraint on the pipeline.
- Multiple concurrent live sessions multiply VRAM pressure (each holds a model set
  loaded); the spool keeps audio safe, but sustained real-time for *many* live streams
  on one GPU is not guaranteed.

## Alternatives considered

- **Resource-greedy parallel (default)** — rejected: little wall-clock gain on one
  GPU, more VRAM pressure and contention; kept only as an opt-in with headroom.
- **Hard preempt (kill + requeue the batch unit)** — rejected: throws away partial
  compute and complicates resumability for marginally faster live start.
- **Reserve VRAM for live always** — rejected as the *default* (starves batch
  throughput, painful on 16GB cube); available as a config knob for latency-critical
  setups.
- **No scheduler — fail when busy** — rejected: hostile UX; the game-is-running case
  is normal, not exceptional.
- **External queue (Celery/Redis/RQ)** — rejected: heavy dependency for a
  single-node backend; the in-process scheduler + spool is sufficient and keeps the
  low-overhead property (ADR-0007).

## Related

- ADR-0018 (the job this schedules; carries `queued {position, ahead}`),
  [ADR-0020](0020-remote-access-security.md) (the principal that owns each job),
  ADR-0011 (lease held by live; idle unload), ADR-0012 (spool = the persistent queue
  *and* the live audio buffer that prevents data loss), ADR-0005 (chunked ASR = the
  work-unit / suspend boundary), ADR-0009 (live cadence: session vs turn epochs),
  ADR-0002 (per-host capacity; future cross-host distribution),
  ADR-0007 (in-process, low-overhead — no external broker).
