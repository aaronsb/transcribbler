# ADR-0010: Operator awareness, control & not-covert

- **Status**: Accepted
- **Date**: 2026-06-28
- **Deciders**: Aaron
- **Split from**: [ADR-0009](0009-capture-cadence.md) (originally bundled).

## Context

transcribber includes an always-on capture daemon ([ADR-0008](0008-build-order.md) stage 4)
that buffers audio (with pre-roll) and can transcribe ambient conversation. An
always-listening tool must never capture silently: the operator needs continuous awareness
that audio is being captured/transcribed, and first-class control to stop it. This is a
trust and privacy requirement, distinct from the segmentation concern in
[ADR-0009](0009-capture-cadence.md).

## Decision

### Always-visible active indicator (not covert)

Whenever capture or transcription is live, a visible indicator reflects state. This is a
**hard non-goal to violate**: capture is never silent or hidden.

A single **capture-state contract** is shared across all client surfaces (so each client
renders the same states rather than reinventing them):

| State | Meaning |
|---|---|
| `off` | disabled by operator; not capturing |
| `idle` | armed, watching heuristics, not yet buffering |
| `listening` | buffering / pre-roll, VAD watching for speech |
| `transcribing` | an epoch is in flight |

Surfaces: KDE tray icon state for the GUI client; a status line for the CLI/headless
daemon. The indicator must reflect *real* capture state and never misrepresent it.

### Explicit operator control — two distinct actions

The operator can start and stop at any time. "Stop" is split into two first-class actions
to separate data-safety from privacy:

- **Stop = finalize.** Ends the session and **finalizes the already-consented in-flight
  epoch** (don't lose a long meeting to a misclick). Equivalent to the `SessionEnd →
  finalize` boundary in [ADR-0009](0009-capture-cadence.md).
- **Cancel / Discard = drop.** Ends the session and **discards** the in-flight buffer
  without transcribing it (privacy: "don't keep that").
- **Un-accepted pre-roll** (still in `listening`, no keep-prompt answered) is **discarded**
  on stop — keeping it would undercut prompt-to-keep and the not-covert posture.

Operator stop/cancel is a first-class boundary event, equal to the silence timeout.

## Consequences

### Good
- Awareness and consent are designed in, not bolted on — addresses the core privacy risk
  of an always-listening tool and builds operator trust (goal 1).
- One shared capture-state contract avoids each client reinventing indicator logic.
- Finalize vs discard cleanly separates "don't lose my data" from "don't keep that."

### Bad / costs
- Every client surface (tray + CLI) must implement the indicator honestly.
- Two stop semantics add a little UX surface (two affordances, clearly labelled).

## Scope / deferred
- This ADR covers **operator** awareness only. It does **not** cover (a) **retention and
  deletion** of ring buffers, pre-roll, stored audio, and the transcript store, or (b)
  **consent of other parties** on a call (the legally/ethically significant case for a
  meeting recorder). Awareness ≠ consent of the recorded. These are deferred to
  [ADR-0013](0013-retention-and-consent.md).

## Alternatives considered
- **Silent capture with retroactive consent only** — rejected; violates *not covert*.
  Pre-roll buffering is allowed, but the active indicator is always shown.
- **Single "stop" action** — rejected; conflates data-safety (finalize) with privacy
  (discard).
- **Per-client ad-hoc indicators** — rejected; defined once as a shared state contract.

## Related
- ADR-0009 (cadence; the lifecycle the indicator reflects), ADR-0001 (indicator lives in
  each thin client), ADR-0013 (retention & third-party consent).
