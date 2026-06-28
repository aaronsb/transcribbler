# ADR-0014: IR epoch/session identity & deterministic merge

- **Status**: Deferred (only needed if nested/composable cadence is ever adopted)
- **Date**: 2026-06-28
- **Deciders**: Aaron

## Context

[ADR-0009](0009-capture-cadence.md) deliberately makes cadence profiles **exclusive** and
keeps the Canonical IR ([ADR-0006](0006-canonical-ir-contract.md)) ignorant of cadence:
only session epochs produce canonical IR; turn epochs are non-canonical preview fragments.
This avoids leaking epoch/session structure into the contract.

*If* a future need arises to emit turn epochs as peer canonical documents that roll up into
a session (true composable cadence), the IR would need **epoch/session identifiers**, a
parent/child relation, and a **deterministic cross-epoch merge** — including reconciling
speaker IDs across epochs (the cross-chunk identity problem [ADR-0005](0005-diarization-flow.md)
exists to avoid). That is a non-trivial schema + algorithm decision.

## Decision

**Deferred.** Do not add epoch/session identity to the IR unless composable cadence is
actually adopted. Recorded here so the requirement and its cost are not forgotten.

## Related
- ADR-0006 (IR contract), ADR-0009 (why it's deferred), ADR-0005 (speaker-ID reconciliation).
