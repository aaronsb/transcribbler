# Architecture Decision Records

These ADRs record the decisions behind transcribbler and *why* they were made, so a
future contributor (human or agent) can see the reasoning, not just the result.

Format: lightweight [MADR](https://adr.github.io/madr/)-style. One decision per file.

| ADR | Decision | Status |
|---|---|---|
| [0001](0001-composable-daemon-and-thin-clients.md) | Composable daemon + thin clients (reject the monolith) | Accepted |
| [0002](0002-hardware-topology.md) | Two-machine topology: CUDA backend on cube, ROCm desktop for clients | Accepted |
| [0003](0003-reuse-engines-build-the-gap.md) | Reuse inference engines; build the gap (clients, capture, canonicalization) | Accepted |
| [0004](0004-llama-cpp-native-backend.md) | llama.cpp-native backend with idle GPU unload | Accepted |
| [0005](0005-diarization-flow.md) | Diarization flow: global diarize, chunk ASR, overlap-align | Accepted |
| [0006](0006-canonical-ir-contract.md) | Canonical IR as the backend-agnostic pipeline contract | Accepted |
| [0007](0007-supervisor-agnostic-packaging.md) | Supervisor-agnostic single process; systemd + container wrappers | Accepted |
| [0008](0008-build-order.md) | Build order: CLI daemon first | Accepted |
| [0009](0009-capture-cadence.md) | Capture cadence: the epoch model (session vs turn) | Accepted |
| [0010](0010-operator-awareness-and-control.md) | Operator awareness, control & not-covert | Accepted |
| [0011](0011-idle-unload-keepalive-lease.md) | Idle-unload keep-alive lease during live capture | Proposed |
| [0012](0012-capture-store-and-forward.md) | Capture store-and-forward spool | Proposed |
| [0013](0013-retention-and-consent.md) | Retention, deletion & third-party consent | Proposed |
| [0014](0014-ir-epoch-session-identity.md) | IR epoch/session identity & deterministic merge | Deferred |
| [0015](0015-pluggable-compute-backends.md) | Pluggable compute backends — swapping GPUs via wire seam + compute profiles | Accepted |

## Conventions

- **Status**: Proposed → Accepted → (later) Deprecated / Superseded by ADR-NNNN.
- New ADRs get the next number; never renumber.
- An ADR is immutable once Accepted — to change a decision, write a new ADR that
  supersedes it and update the old one's status.
