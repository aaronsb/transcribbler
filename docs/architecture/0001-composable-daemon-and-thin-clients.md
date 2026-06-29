# ADR-0001: Composable daemon + thin clients (reject the monolith)

- **Status**: Accepted (client wire refined by [ADR-0017](0017-rust-clients-python-service.md)/[ADR-0018](0018-client-facing-wire.md))
- **Date**: 2026-06-28
- **Deciders**: Aaron

> **Note (2026-06-29):** This ADR describes the client↔backend protocol as
> "OpenAI-compatible HTTP." [ADR-0017](0017-rust-clients-python-service.md) refines
> that: the *clients* speak a distinct **client-facing contract**
> ([ADR-0018](0018-client-facing-wire.md)); OpenAI-compatible HTTP is the backend's
> *internal engine* wire ([ADR-0004](0004-llama-cpp-native-backend.md)). The split +
> thin-client decision here stands; only the wire layering is clarified.

## Context

The driving requirement for transcribbler is captured negatively: avoid
*"all-in-one applications that are heavy and I have to remember to have running all the
time, or act 'special' and not unix-like."*

A survey of the 2025–2026 ecosystem (see [prior-art](../design/prior-art.md)) shows a
clean split:

- **Backends with no awareness** — `speaches`, `whisper.cpp server`,
  `wyoming-faster-whisper`, LocalAI. Clean daemons, but they don't capture anything.
- **Capture UX welded into monoliths** — Hyprnote, anarlog, Handy, Vibe. They have the
  always-listening behavior we want, but as Electron/Tauri desktop apps with the engine
  embedded — exactly the babysat monolith we reject.

The ambient pre-roll / "voice detected, keep this?" behavior exists **only** inside the
monoliths. No Unix-like, daemon-friendly tool combines a transcription+diarization
backend, idle GPU release, and a thin always-on capture client.

## Decision

Build transcribbler as a **single long-running backend service** plus a family of
**thin, independent clients**, following the Unix philosophy:

- The backend does transcription + diarization + canonicalization and exposes a clean
  wire protocol (OpenAI-compatible HTTP; see [ADR-0004](0004-llama-cpp-native-backend.md)).
- Each of {CLI, KDE tray, always-on capture daemon} is a **separate client process**
  that talks to the backend. Clients are replaceable independently and never embed the
  engine.
- Every component is headless-capable, file/stream/pipe friendly, and supervisable by
  `systemd`. The desktop GUI pieces are optional sugar, not the product.

The architectural reference for "many thin capture clients → one backend" is
`wyoming-satellite`.

## Consequences

### Good
- No babysat monolith; nothing "acts special."
- Clients and backend evolve and deploy independently.
- A backend on a remote host (cube) serves all desktop clients transparently.
- Naturally testable: each part has a wire contract.

### Bad / costs
- More moving parts than a single app; requires a defined protocol and versioning.
- Cross-process latency (negligible for batch; mitigated for live by the two-tier
  design in [ADR-0005](0005-diarization-flow.md)).

## Alternatives considered

- **Adopt a monolith (Hyprnote et al.)** — rejected; violates the core requirement.
- **Library-only (in-process) like nerd-dictation** — rejected; no client/server split,
  can't share one GPU-resident backend across machines and clients.

## Related

- ADR-0002 (where each piece runs), ADR-0003 (reuse vs build), ADR-0007 (packaging).
