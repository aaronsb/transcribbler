# ADR-0012: Capture store-and-forward spool

- **Status**: Proposed (stub — to decide before the capture daemon, stage 4)
- **Date**: 2026-06-28
- **Deciders**: Aaron

## Context

The capture daemon runs 24/7 on the desktop, but its backend lives on cube
([ADR-0002](0002-hardware-topology.md)), which may be unreachable, rebooting, or
idle-unloaded. An always-on capturer with a network-dependent backend must not drop audio
when the backend is briefly unavailable. ADR-0002's "local fallback backend" mitigates
*compute*, but there is no decision yet about a **durable buffer** for finalized epochs
awaiting transcription.

## Decision (to be finalized)

Likely: **a durable on-disk spool** of finalized epochs (audio + metadata) on the capture
host; the daemon enqueues epochs and a worker drains the queue to the backend with retry/
backoff, surviving daemon restarts. Open: spool format and retention (ties to
[ADR-0013](0013-retention-and-consent.md)), ordering guarantees, max spool size /
backpressure, and whether the local fallback backend drains the spool when cube is down.

## Related
- ADR-0002 (network dependency / fallback), ADR-0009 (epochs), ADR-0013 (retention).
