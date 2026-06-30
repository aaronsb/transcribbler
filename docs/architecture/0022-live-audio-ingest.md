# ADR-0022: Live audio ingest — sessionized chunk submission over the wire

- **Status**: Proposed
- **Date**: 2026-06-30
- **Deciders**: Aaron

## Context

[ADR-0018](0018-client-facing-wire.md) defines the client-facing wire for **batch**
work (submit a file → job → IR) and explicitly **defers the live path**: "a live
audio ingest path for the capture daemon (streaming body / WebSocket upgrade — also
transport-agnostic), deferred to the capture-daemon ADR." This is that ADR.

Several accepted decisions already constrain the shape, and the gap is the *mechanism*
that joins them:

- **[ADR-0009](0009-capture-cadence.md)** — the capture daemon segments the stream
  into **epochs** client-side (VAD + pre-roll + hangover). *Session* cadence yields the
  canonical transcript; *turn* cadence yields eager, **non-canonical** preview.
- **[ADR-0019](0019-job-scheduling.md)** — live audio "arrives as **small,
  silence-bounded chunks**"; **capture is decoupled from transcription** (the capture
  host spools, transcription runs as capacity allows, "never drops audio"); the
  canonical re-run is **live-class, incremental per finalized segment**.
- **[ADR-0012](0012-capture-store-and-forward.md)** — the **client-side** spool holds
  finalized epochs and drains them to the backend with retry/backoff.
- **[ADR-0011](0011-idle-unload-keepalive-lease.md)** — a live session holds a
  multi-holder keep-alive lease so models stay resident across turns.
- **[ADR-0005](0005-diarization-flow.md)** — the canonical tier diarizes the **whole**
  window globally for stable cross-speaker IDs; the preview tier is fast/low-fidelity.
- **[ADR-0006](0006-canonical-ir-contract.md)** — the IR already models a live capture
  as `source.kind = "session"` with `uri`/`sha256` optional (ephemeral).

So the daemon produces silence-bounded chunks, must never drop audio, and the canonical
transcript needs the whole session. What's undecided is: **how does a chunk get from the
capture host to the backend, how is a session framed on the wire, and how do the chunks
become one canonical session IR** — without inventing a second protocol.

## Decision

**Live ingest is a *session* of silence-bounded **chunk jobs** carried over the existing
ADR-0018 HTTP contract — not a new streaming/socket protocol. The capture host owns
segmentation and spooling; the backend owns reassembly into one `kind:"session"`
Canonical IR.**

### Transport — reuse the job wire; the chunk is the unit

A finalized epoch chunk ([ADR-0009](0009-capture-cadence.md)) is submitted exactly like a
batch job — multipart audio over UDS/TCP — but **live-class** and bound to a session.
This reuses ADR-0018 wholesale: chunks are **server-owned, survive disconnect**, and flow
through the same admission/queue/SSE machinery ([ADR-0019](0019-job-scheduling.md)).

We **reject a raw continuous stream (WebSocket / chunked request body) for the durable
path**, because:

- Segmentation is **client-side** by ADR-0009 (VAD + pre-roll on the capture host); a raw
  stream would push framing to the server and duplicate that logic.
- The durable unit is **already chunk-granular and disconnect-survivable** (ADR-0018/0019);
  a long-lived bidirectional socket re-introduces the connection-liveness coupling
  ADR-0018 rejected, and a multi-hour streaming request body is proxy-hostile.
- One contract, not two — the same reason ADR-0018 chose HTTP+SSE over gRPC.

A low-latency **preview** channel (a WebSocket feeding the CUDA-only Sortformer tier,
ADR-0005/0019) **may** be added later as an advertised `capability` (ADR-0018), *above*
this durable path — not in place of it. Deferred until the preview tier is built
([ADR-0008](0008-build-order.md) stage 6).

### Session framing — a thin resource over the job machinery

A **live session** groups its chunks and carries the lease + live-mode signal:

- `POST /v1/sessions` — open a session (compute-profile **name**, cadence). Acquires the
  ADR-0011 keep-alive lease, enters ADR-0019 **live mode**. Returns a session id (an
  unguessable UUID, owned by the caller-principal — same leak guard as jobs, ADR-0018).
- `POST /v1/sessions/{id}/chunks` — submit one finalized chunk (multipart audio +
  `seq` + `offset_s` + the ADR-0009 pre-roll lead-in). Live-class; admitted/queued per
  ADR-0019. Returns a chunk job id.
- `GET /v1/sessions/{id}/events` — **SSE** multiplexing the session: per-chunk
  `progress`, eager **preview** fragments (non-canonical), and lifecycle, ending in
  `finalized {ir_ref}`.
- `POST /v1/sessions/{id}/finalize` — no more chunks; run the global-diarization
  canonical pass over the accumulated audio → the session IR. Releases the lease.
- `DELETE /v1/sessions/{id}` — operator stop/discard ([ADR-0010](0010-operator-awareness-and-control.md));
  releases the lease, drops buffered audio per retention ([ADR-0013](0013-retention-and-consent.md)).

`/v1/jobs` stays the batch shape; sessions/chunks are the live shape — distinct on the
wire, sharing the job engine underneath.

### Assembly — one session IR by deterministic merge + finalize re-diarization

A session produces a single `source.kind = "session"` Canonical IR (ADR-0006; `uri`/
`sha256` omitted for ephemeral live). It is assembled by the **deterministic merge of
[ADR-0014](0014-ir-epoch-session-identity.md)** — which this ADR **activates from
Deferred**, since reassembling per-chunk results into one session document is exactly the
case it was written for. Chunks carry `seq` + `offset_s`, and ASR provenance carries true
offsets so stitching can't drift (ADR-0006).

Reconciling the two tiers (ADR-0005/0019):

- **During the session**, finalized chunks yield **provisional** canonical results
  incrementally (speaker ids may be chunk-local) plus, where available, fast preview
  fragments. These are shown, not authoritative.
- **On finalize**, a **global diarization pass** over the whole session reconciles
  speaker identities end-to-end (the ADR-0005 keystone), producing the authoritative
  session IR. This supersedes the provisional results.

This is the concrete form of ADR-0019's "canonical re-run is live-class, incremental per
finalized segment, reconciled" and ADR-0009's "preview now + canonical on finalize."

### Never drop audio

Capture is decoupled from transcription (ADR-0019): the capture host writes finalized
chunks to its **spool** (ADR-0012) and drains them as live-class chunk submissions with
retry/backoff. Because chunks are server-owned and the session id persists, a daemon that
loses the connection **reconnects and resumes** the same session; under backend
contention text lags but **audio is never lost**.

```mermaid
sequenceDiagram
    participant D as Capture daemon (VAD + spool)
    participant B as Backend (live mode)
    D->>B: POST /v1/sessions  (profile, cadence)
    B-->>D: session id  (lease acquired, live mode)
    loop per silence-bounded epoch
        D->>B: POST /sessions/{id}/chunks (audio, seq, offset)
        B-->>D: SSE: progress / preview (non-canonical)
    end
    D->>B: POST /sessions/{id}/finalize
    B->>B: global re-diarize accumulated audio (ADR-0005)
    B-->>D: SSE: finalized {ir_ref}   (session IR; lease released)
```

## Consequences

### Good

- **One contract.** Live reuses ADR-0018's job machinery (ownership, disconnect
  survival, SSE, admission) instead of a second protocol — less to build, secure, and test.
- **No dropped audio by construction** — client spool + server-owned chunks + resumable
  session, matching ADR-0019's guarantee.
- **Canonical fidelity preserved** — the finalize re-diarization keeps the global-window
  speaker stability ADR-0005 exists to protect; provisional results give immediacy without
  compromising the source of truth.
- **Cadence maps cleanly** — preview fragments (turn) vs the session IR (session) fall out
  of ADR-0009 with no new IR hierarchy beyond ADR-0014's merge.

### Bad / costs

- **Activates ADR-0014** (epoch/session identity + deterministic merge), previously
  deferred — real work in the IR assembly layer.
- **Per-chunk job overhead** — many small submissions per session; mitigated by
  connection reuse and ADR-0019's "not-too-small" chunk sizing, but heavier than a raw
  stream would be.
- **No true low-latency preview here** — the sub-second streaming tier (Sortformer) is
  deferred; portable hosts get chunk-cadence near-real-time only (already ADR-0019's
  position).
- **Finalize is a heavy step** — a whole-session global diarization at the end; bounded by
  session length and admitted as live-class.

## Alternatives considered

- **WebSocket / chunked-body raw audio stream** — rejected for the durable path: pushes
  segmentation server-side (vs ADR-0009), re-couples to connection liveness (vs ADR-0018),
  needs a second protocol. Reserved as a future *preview* capability, not the canonical
  carrier.
- **Each chunk is a standalone canonical IR (no session)** — rejected: reintroduces the
  cross-chunk speaker-ID inconsistency ADR-0005/0009 kill, and leaves the operator with N
  fragments instead of one transcript.
- **Server-side VAD/segmentation** — rejected: duplicates the capture daemon's job, and the
  client must segment anyway to spool and to drive pre-roll/operator awareness
  (ADR-0009/0010).
- **A bespoke binary live protocol** — rejected for the same reason ADR-0018 rejected gRPC:
  toolchain weight and a second client codebase for no benefit over chunk-jobs + SSE.

## Related

- ADR-0018 (the wire this extends; resolves its deferred live path),
  ADR-0019 (live-class scheduling of these chunks; partial-singleton live mode),
  ADR-0009 (client-side epochs; session→canonical, turn→preview),
  ADR-0005 (two tiers; global diarize on finalize),
  ADR-0006 (`kind:"session"` IR), ADR-0014 (deterministic merge — activated here),
  ADR-0011 (lease held for the session), ADR-0012 (client spool drains chunks),
  ADR-0010 (operator stop/discard), ADR-0013 (retention of buffered live audio),
  ADR-0008 (capture daemon is the consumer of this path; preview tier is stage 6).
