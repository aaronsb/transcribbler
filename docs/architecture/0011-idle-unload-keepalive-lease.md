# ADR-0011: Idle-unload keep-alive lease during live capture

- **Status**: Proposed (stub — to decide before the capture daemon, stage 4/6)
- **Date**: 2026-06-28
- **Deciders**: Aaron

> **Note (2026-06-29):** [ADR-0019](0019-job-scheduling.md) allows multiple
> simultaneous live sessions, so the lease is **multi-holder / reference-counted** —
> each live session takes a hold; the models unload only when the count reaches zero
> (no live session remains). `DELETE` of one session releases *its* hold, not the
> whole lease. To be finalized here with the daemon.

## Context

Goal 4 / [ADR-0004](0004-llama-cpp-native-backend.md) free the GPU by unloading models
after an idle TTL. This conflicts with eager always-on capture
([ADR-0009](0009-capture-cadence.md)): short turn epochs (~1 s) would either keep models
pinned permanently (defeating "GPU free for games") or pay a cold-load every turn (thrash).
The PyTorch pyannote sidecar makes cold loads especially costly (slow init, HF-gated
weights).

## Decision (to be finalized)

Likely: **a live capture session holds a keep-alive lease for its duration** — models stay
resident while a session is active; **idle-unload applies between sessions, not between
turns/epochs.** The lease is acquired on session start and released on session
finalize/cancel ([ADR-0010](0010-operator-awareness-and-control.md)). Open: lease across
the network from a desktop client to the cube backend; lease granularity per engine
(ASR vs diarizer vs canonicalization LLM).

## Related
- ADR-0004 (idle unload), ADR-0009 (cadence), ADR-0010 (session boundaries), ADR-0002.
