# ADR-0019: Job scheduling — admission, concurrency, and live priority

- **Status**: Accepted
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
- **Durable buffering is already a theme.** This ADR's **server-side job queue + live
  audio buffer** lives on the *backend*. It is a sibling of — **not** the same thing as
  — the *client-side* store-and-forward spool ([ADR-0012](0012-capture-store-and-forward.md)),
  which buffers on the *capture host* and drains to the backend. Same idea (durable
  buffer so nothing is lost), two locations; this ADR owns the server-side one.

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
(whisper + maybe pyannote + maybe llama). Throughout this ADR "profile" means the
**compute profile** ([ADR-0015](0015-pluggable-compute-backends.md): which engines/
models on which device) — not the *cadence* profile (ADR-0009: session vs turn epoch),
which is a separate axis. Capacity is **read from the device**, not a constant —
16GB on cube, 24GB on the desktop, and *less* when a game is resident.
A job that doesn't fit **queues** (persisted via the spool) rather than OOM-failing;
it is admitted later when VRAM frees.

### Concurrency — serial + pipelined by default (batch mode)

In **batch mode**, one heavy GPU job at a time, but **overlap stages** (ASR of the next chunk/job while
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
  (Enforcement: the wire verifies `caller-principal == job-owner` on every
  `/jobs/{id}` op and uses unguessable ids — ADR-0018.)
- Batch ordering is **FIFO within an owner, round-robin across owners**: each owner's
  jobs run in submission order, but the scheduler rotates between owners so one
  account's 400-file backlog can't starve another's single file.
- The client is told its place via the wire's `queued {position, ahead}` event
  (ADR-0018), updated as the line moves: *"3 jobs ahead of you" → 2 → 1 → running*.
  `ahead` is the **effective** count given round-robin fairness (how many jobs will
  actually run before yours), which is why it is reported rather than left for the
  client to infer from a raw position.

This is what makes a single backend usable by several trusted people/devices at once
(multi-user, not multi-tenant — ADR-0020). **This shared-backend, many-principal
scenario is the *remote* case** — a backend (typically cube) serving SSH-tunnelled /
token clients. The *local* UDS tier is single-user by construction (a per-user socket
under `$XDG_RUNTIME_DIR`, one backend per OS user via socket activation — ADR-0018),
so its principal is simply that user; fair-share across principals doesn't arise there.

### Modes — a "partial singleton": live preempts batch wholesale

The scheduler is in one of three modes: **idle** → **batch** (draining the queue) →
**live** (one or more live sessions active). Entering live mode *is* the priority rule.

**Why live mode can host several sessions at once.** Live audio arrives as **small,
silence-bounded chunks** (sentence/paragraph-sized — not so small that overhead
dominates, not so large that latency suffers). Each chunk transcribes in a *fraction*
of its own duration, so one live session leaves the GPU **idle between chunks**. That
slack is the budget for a *second* and *third* live session: the scheduler **"nices"**
live work, fair-sharing the GPU across whichever session's chunk is ready, and they
interleave in each other's gaps. Same-profile live sessions also **share one resident
model set** (load once, multiplex chunks) — a new live participant costs *throughput*,
not another copy of the models in VRAM.

**Batch is frozen for the whole duration of live mode.** When the first live session
starts, in-flight batch **drains to its next chunk boundary and suspends** (work-unit
= ADR-0005 chunk — bounded wait), and the batch queue is **not drained again until the
last live session closes**. Batch is deliberately *not* interleaved into live's slack:
a batch work-unit (a 30s ASR window, a whole-file diarize) is too **coarse** to fit
between live chunks without blowing live's latency budget, so it yields wholesale
rather than risk live responsiveness. When the last live session ends, the server
leaves live mode and resumes draining batch (or goes idle → unload).

The "partial singleton": the server holds a single *mode*, but live mode admits
multiple participants. Live always wins; batch always waits. No work is killed; under
live oversubscription each session's audio keeps **spooling** — slower text, never
lost audio.

### Live mode and the two-tier model (ADR-0005)

[ADR-0005](0005-diarization-flow.md) splits live into **two tiers**: a fast streaming
**preview** (low-fidelity, e.g. Streaming Sortformer / WhisperLiveKit) and an offline
**canonical re-run** (pyannote + whisper) on segment finalize that produces the
source-of-truth IR. The scheduler treats them as follows:

- **The "resident model set" is the host's live engine.** Where a fast preview engine
  exists (Sortformer is **CUDA-only**, so cube), that is the shared, in-slack set.
  Portable hosts (the Vulkan/ROCm desktop — ADR-0015/0002) have **no Sortformer**, so
  "live" there is **chunked near-real-time canonical** at the turn-epoch cadence
  ([ADR-0009](0009-capture-cadence.md)) rather than a separate preview tier. Live
  preview is thus a backend *capability*, advertised via the wire's `capabilities`
  (ADR-0018), not a guarantee.
- **The canonical re-run is live-class work, not third-party batch.** It is part of
  the live session's own output, so it runs *within* live mode (in the slack, below
  preview but above frozen batch). Consequence: a long live session yields canonical
  IR **incrementally per finalized segment**, not only when the meeting ends — it is
  not subject to the "batch frozen for the whole session" rule.
- **Admission budgets both tiers.** The VRAM/throughput budget must account for the
  preview set *and* the canonical re-run, not preview alone — otherwise a live session
  is admitted that can't actually keep up with its own re-runs.

### Lifecycle (extends ADR-0018)

`queued` (admitted-pending-resources) → `running` → optional `paused`
(batch yielded to live) → `done` / `error` / `canceled`. Each state has a wire event
(ADR-0018), including `paused`/`resumed` — a job can sit `paused` for the length of a
live session, so that state is observable, not silent. Terminal naming matches the
wire: `done` / `error` / `canceled`. Live always outranks batch; batch ordering is
**FIFO within an owner, round-robin across owners** (above). Empty queue **and** no
live session → idle TTL → unload (ADR-0011), freeing the GPU for games/other work.

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
- Multiple **same-profile** live sessions share the resident model set (cheap), but
  **different-profile** live sessions each need their own models loaded (VRAM cost),
  and sustained real-time for *many* live streams on one GPU is throughput-bounded —
  the spool keeps audio safe, but text can lag.
- **A long-running live session defers all batch for its entire duration** (an all-day
  meeting freezes the batch queue). Accepted: batch is "whenever," live is interactive.
  A future safety valve could lend live's idle slack to *small* batch units, but coarse
  batch work-units make that risky for live latency — so the simple rule is "no batch
  during live mode."

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
