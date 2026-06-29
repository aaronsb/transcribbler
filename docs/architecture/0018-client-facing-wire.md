# ADR-0018: Client-facing wire — one HTTP contract over Unix-socket (local) and TCP (remote)

- **Status**: Proposed
- **Date**: 2026-06-29
- **Deciders**: Aaron

## Context

[ADR-0017](0017-rust-clients-python-service.md) draws the boundary (Rust clients,
Python service) and names a **client-facing contract** distinct from ADR-0004's
internal *engine* wire — but leaves its shape open. This ADR fixes that shape.

The forces:

- **Local is the common case, and it shouldn't need ports or auth.** A CLI on the
  same box as the service should not open a network port, consume a TCP port, or
  require a token. The OS already has an authorization mechanism for same-host IPC:
  filesystem permissions on a **Unix domain socket** (UDS).
- **Remote is the case that crosses a trust boundary.** Desktop → cube
  ([ADR-0002](0002-hardware-topology.md)) goes over the network and therefore *does*
  need identity (auth), not just transport encryption.
- **One protocol, not two.** HTTP/1.1 speaks fine over a UDS, so we do not need a
  separate socket protocol and an HTTP protocol — we need **one HTTP contract that
  binds to either transport**. uvicorn binds `--uds` or `--host/--port` with the same
  ASGI app; Rust `hyper`/`reqwest` do HTTP-over-UDS (hyperlocal).
- **Work is long-running and must survive client disconnect.** A 57-min file or an
  ambient capture session outlives a single request; the spool
  ([ADR-0012](0012-capture-store-and-forward.md)) and idle-unload lease
  ([ADR-0011](0011-idle-unload-keepalive-lease.md)) already assume server-owned,
  resumable jobs.

## Decision

**A single versioned HTTP contract, carrying the [ADR-0006](0006-canonical-ir-contract.md)
IR, bound to a Unix socket locally and a TCP port remotely. The transport encodes
the trust boundary, and auth follows from it.**

### Transport & auth (the core of this ADR)

| | **Unix socket — local default** | **TCP — remote** |
|---|---|---|
| Bind | `$XDG_RUNTIME_DIR/transcribbler.sock` | `host:port` |
| Network surface | none | a port, firewalled |
| Authorization | **filesystem perms** (per-user runtime dir, 0700); optional `SO_PEERCRED` uid check | **bearer token** over **TLS** (traefik terminates, per ADR-0007) |
| Autostart | **systemd socket activation** (first connect spawns the service) | service must be running |
| Reach | same host only | desktop → cube |

- **Local needs no app-layer auth** — the kernel already enforced it. This is the
  default and the path the one-shot CLI takes.
- **Auth exists only where a request crosses machines.** A TCP listener requires a
  bearer token and refuses cleartext; token lifecycle/rotation and the threat model
  are big enough to warrant a dedicated **security ADR** (intersects consent/retention,
  [ADR-0013](0013-retention-and-consent.md)).
- **The client selects transport by base URL**: a socket path (default) or
  `https://cube:port`. No code path differs beyond the connector.
- **Locality, not headlessness, is the axis.** A headless service on the same box
  still uses the UDS; only cross-host use takes the TCP+auth path.

### Contract shape — server-owned async jobs

Because work outlives a request, the model is **jobs**, not a single blocking call:

- `POST /v1/jobs` — submit audio (multipart: metadata + file) with a **profile name**
  (resolved server-side against the ADR-0017 allowlist — never a path), and flags
  (`diarize`, `canon`, `prompt`). Returns `202` + a job id.
- `GET /v1/jobs/{id}/events` — **SSE** stream: `queued {position, ahead}` while it
  waits in line (updated as the queue moves — [ADR-0019](0019-job-scheduling.md)) →
  `progress {stage, completed, total}` once running → terminal `done {ir_ref}` or
  `error {code, message}`. This is the wire form of the transport-agnostic progress
  sink from ADR-0017 (PR #13 #4).
- `GET /v1/jobs/{id}` — job state + the Canonical IR when complete.
- `GET /v1/jobs/{id}/result?format=md|vtt|json` — **server-side render**, so a
  `curl`/non-Rust client gets a readable view without reimplementing render.
- `DELETE /v1/jobs/{id}` — **cancel**; releases the ADR-0011 keep-alive lease and is
  the deletion hook for ADR-0013.
- `GET /v1/profiles` — names + capabilities (server-side allowlist).
- `GET /v1/healthz` (liveness) and `GET /v1/version` (see versioning).

A one-shot CLI is sugar over this (submit → stream events → fetch result/render),
hidden behind the client so trivial use is one command.

**Client disconnect does not cancel a job** — the job persists (spool, ADR-0012);
the client can reconnect to `/events` or poll `/jobs/{id}`. Only an explicit
`DELETE` cancels and frees the lease.

### Persistence ownership (resolves an ADR-0017 open question)

The **server owns the canonical store** — job state and the IR — because async +
disconnect-survival + the spool require it (ADR-0012/0013/0014). **Render is a view,
offered both server-side (`?format=`) and client-side (Rust)**; either is cheap and
stateless. Clients may also save a rendered file locally (`-o`), but the IR is the
source of truth and lives on the backend.

### Versioning

- **Major version in the path** (`/v1`); a client refuses an unknown major.
- `GET /v1/version` returns `{wire_version, capabilities}` so a client and a
  backend deployed independently across hosts (ADR-0002) detect mismatch and degrade
  gracefully. Additive (minor) changes stay backward compatible.

## Consequences

### Good

- Local use opens **no port, needs no token** — auth is the OS's job; the headline
  "Unix-like, no babysitting" property holds at the wire level.
- Socket activation + idle-unload makes a one-shot feel **in-process** without a
  resident daemon — connect, spawn, unload after TTL.
- One contract, two transports → no protocol duplication; the same Rust connector
  and the same ASGI app serve both.
- Server-owned jobs survive disconnect and power the capture daemon, the spool, and
  resumable long jobs for free.
- Server-side render keeps the contract usable from `curl` and future non-Rust clients.

### Bad / costs

- Two auth regimes to implement and test (UDS perms path + TCP token path), though
  the second is confined to the cross-host case.
- The job/SSE model is more moving parts than a blocking call (job store, event
  stream, cancellation, lifecycle) — justified by long-running, disconnect-prone work.
- A real cross-host security model (tokens, TLS, rotation) is still owed — deferred
  to a security ADR, not solved here.

## Alternatives considered

- **TCP/HTTP everywhere (even local)** — rejected as the default: consumes a port,
  exposes a network surface, and forces auth onto the common local case the OS could
  have secured for free.
- **A bespoke framed socket protocol (non-HTTP) for local** — rejected: forces a
  second protocol and a second client codebase; HTTP-over-UDS gives the same
  no-port/no-auth benefit while keeping one contract.
- **Blocking synchronous request (no jobs)** — rejected: a dropped connection loses
  a 57-min transcription; can't serve the capture daemon or the spool; ties GPU work
  to socket liveness.
- **gRPC** — rejected: heavier toolchain on both sides; HTTP+SSE covers our
  request/stream needs, and HTTP-over-UDS is simpler to operate than gRPC-over-UDS.

## Related

- ADR-0017 (the split this contract serves; resolves its wire/persistence/autostart
  open questions), [ADR-0019](0019-job-scheduling.md) (how submitted jobs are
  admitted/queued/prioritized), ADR-0004 (the internal engine wire this sits in front of),
  ADR-0006 (the IR carried), ADR-0007 (supervision / TLS termination),
  ADR-0011 (lease released on cancel), ADR-0012 (server-owned spool/jobs),
  ADR-0013/0014 (retention, persistence, deletion), ADR-0002 (independent host deploys).
- [ADR-0020](0020-remote-access-security.md) (remote access tiers + auth that secure
  the TCP transport defined here); forthcoming: a **live audio ingest** path for the
  capture daemon (streaming body / WebSocket upgrade — also transport-agnostic),
  deferred to the capture-daemon ADR.
