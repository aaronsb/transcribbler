# ADR-0017: Rust single-binary clients over a Python backend service

- **Status**: Proposed
- **Date**: 2026-06-29
- **Deciders**: Aaron

## Context

[ADR-0001](0001-composable-daemon-and-thin-clients.md) decided the shape — one
long-running backend service plus a family of thin, independent clients (CLI, KDE
tray, capture daemon) over an OpenAI-compatible HTTP wire. [ADR-0004](0004-llama-cpp-native-backend.md)
fixes that wire, [ADR-0006](0006-canonical-ir-contract.md) the Canonical IR it
carries, and [ADR-0007](0007-supervisor-agnostic-packaging.md) how the service is
supervised (systemd `--user` natively, PID 1 in a container on cube).

Two things were left open, and the current code has drifted from the decided
shape:

1. **No client language was pinned.** ADR-0001 names the clients but not what they
   are written in.
2. **The boundary does not exist yet.** Today the `transcribbler` CLI is a *fused
   monolith*: `cli.py` calls `asr_core()` / `diarizer_core()` in-process; nothing
   exposes an HTTP API (the only HTTP anywhere is the canon core acting as a
   *client* of a llama-server). The "frontend" and "backend" are one process.

The next tranche of work — voiceprint enrollment, YouTube/batch ingest, the
PipeWire capture daemon — would otherwise weld onto this monolith, each feature
landing on the wrong side of a boundary that isn't there. We want the boundary
*first* so each feature lands cleanly: capture/UX on the client, compute/IR on the
service.

Forces on the client language:

- The predecessor `whisper-client` was a **Rust single binary** — one static
  artifact, no runtime to provision, ~zero idle overhead. That is exactly what an
  **always-on capture daemon** and a **tray** want (long-lived, low footprint,
  native desktop integration).
- The backend effectively **must stay Python**: pyannote *is* a PyTorch library,
  and whisper.cpp / llama.cpp are already external binaries the Python layer only
  shells to. Python here is thin glue over native GPU kernels — rewriting it buys
  no performance and discards the entire ML ecosystem (out of scope; see the
  rejected option below).

## Decision

**The clients are Rust single binaries; the backend is the existing Python
service. They meet only at the ADR-0006 IR over the ADR-0004 HTTP wire.** This
refines ADR-0001 (pins the open language question); it does not supersede it.

**Boundary — what lives where:**

- **Backend (Python service):** the heavy compute and everything that needs it —
  the cores (whisper.cpp / pyannote / llama), `align`, `ir` assembly, optional
  canon, and **profile selection**. Exposes audio-in → IR-out over
  OpenAI-compatible HTTP, supervised per ADR-0007. Progress is **streamed over the
  wire** (chunked / SSE), not just to a local stderr.
- **Client (Rust):** HTTP + JSON IR + **render** (md/vtt/json — no GPU, so it is
  pure client-side) + UX (argument parsing, the live progress *display*, the tray
  surface, PipeWire capture). The CLI-ergonomics behavior already built in Python
  (auto-profile, `--progress`, `--prompt`) is the **behavioral spec the Rust CLI
  mirrors**.

**Wire / contract details (folding in PR #13 review findings):**

- `schemas/` (JSON Schema, already language-agnostic) stays the single source of
  truth for the IR; the Rust client derives or hand-writes types from it.
- **Profiles are server-side.** A client names a profile (`desktop-vulkan`); it
  **never** supplies a binary/model path. The server resolves names against an
  **allowlist** — a client-supplied path must not be able to pick an executed
  binary (review finding #7).
- Progress streaming is made **transport-agnostic**: the `run_streamed` sink
  generalizes so the same progress events feed a local stderr *or* an HTTP
  response body (review finding #4).

**Local single-box UX:** the CLI speaks HTTP to a localhost backend; a `systemd
--user` unit (ADR-0007) provides it, with on-demand autostart so a one-off
`transcribbler transcribe x.m4a` "just works" without the user hand-starting a
daemon. During the transition the **existing Python CLI is retained** as the
dev/reference and in-process path, and is retired (or kept as a debug entry) once
the Rust CLI reaches parity.

**Repo layout:** `backend/` keeps its role (Python service + cores); a new
`clients/cli/` holds the Rust binary (cargo). The contract between them is
`schemas/`.

**Build sequencing (refines [ADR-0008](0008-build-order.md)):**

1. Extract the backend **HTTP service** around the existing cores (audio → IR,
   streamed progress) — no behavior change, just expose the boundary.
2. Build the **Rust CLI** client to parity with today's Python CLI over that wire.
3. Resume feature work (voiceprint, batch/YouTube, capture daemon) on the correct
   side of the boundary; the capture daemon and tray are further Rust clients.

## Consequences

### Good

- Each upcoming feature lands on the right side of a real boundary instead of
  thickening a monolith.
- Always-on clients (daemon, tray) get the single-binary, low-idle-overhead model
  they want; distribution is "drop a binary," matching the `whisper-client` lineage.
- The backend stays where the ML ecosystem is; no pointless rewrite.
- The wire is exercised by a *different-language* client — the cleanest possible
  proof that the IR/HTTP contract is genuinely backend-agnostic (reinforces
  ADR-0015's swap seams).

### Bad / costs

- **Two languages** in the repo (Python service + Rust clients): two toolchains,
  two CI lanes, IR types maintained on both sides of the schema.
- A network hop for what used to be an in-process call; negligible for batch, and
  localhost for the common single-box case. The two-tier live design (ADR-0005)
  already anticipates this.
- Transitional duplication: the Python CLI and the Rust CLI coexist until parity.
- Render logic exists in Rust (client) and remains available in Python — minor
  duplication, bounded by the IR being simple.

## Alternatives considered

- **Python thin client** — reuses `render.py`, the schema, env handling; fastest
  path and one language. Rejected: not a single static binary, and a Python
  runtime is the wrong footprint for an always-on daemon/tray; packaging
  (pyinstaller/uv tool) is heavier than shipping one Rust binary.
- **Go single binary** — also one static binary, simpler than Rust. Rejected: does
  not match the `whisper-client` Rust lineage and adds a third language with no
  offsetting advantage over Rust for our case.
- **Rewrite the backend (pyannote/whisper) in Rust for performance** — rejected
  and out of scope: the hot path is GPU tensor kernels already running native; the
  bottleneck is not Python. A port would mean reimplementing trained models in a
  Rust ML stack — research-grade effort for no speedup.
- **Keep the fused monolith and just add features** — rejected: it is the very
  "babysat monolith with the engine welded in" ADR-0001 exists to avoid, and it
  forces capture/UX and compute to share a process.

## Related

- ADR-0001 (split + thin clients — this pins its open client-language question),
  ADR-0004 (HTTP wire), ADR-0006 (IR contract), ADR-0007 (service packaging),
  ADR-0008 (build order — resequenced here), ADR-0015 (swap seams).
- Prior art: `whisper-client` (Rust CLI), `whisper-service` (Python backend).
