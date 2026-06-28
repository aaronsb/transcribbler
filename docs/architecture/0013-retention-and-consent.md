# ADR-0013: Retention, deletion & third-party consent

- **Status**: Proposed (stub — privacy posture; decide before ambient capture ships)
- **Date**: 2026-06-28
- **Deciders**: Aaron

## Context

[ADR-0010](0010-operator-awareness-and-control.md) covers *operator* awareness, but goal 1
(local-first & private) needs more. Two gaps remain:

1. **Retention/deletion** of all captured artifacts — the PipeWire ring buffer, accepted
   pre-roll, stored audio, and the transcript store ([ADR-0005](0005-diarization-flow.md)).
   How long is anything kept? What is deleted, when, and how (including the spool in
   [ADR-0012](0012-capture-store-and-forward.md))?
2. **Consent of other parties** on a call. For a meeting recorder this is the
   legally/ethically significant case, and is jurisdiction-dependent (one- vs all-party
   consent). Operator awareness ≠ consent of the recorded.

## Decision (to be finalized)

To decide: a default retention policy (e.g. keep transcripts, auto-expire raw audio after
N days; easy purge), where it's configured, and what the tool does about third-party
consent — at minimum documenting operator responsibility, possibly an announce/disclaimer
affordance. Until decided, ambient capture of multi-party calls is **operator-responsibility,
documented as such.**

## Related
- ADR-0010 (operator awareness), ADR-0012 (spool retention), ADR-0005 (transcript store),
  goal 1 (privacy).
