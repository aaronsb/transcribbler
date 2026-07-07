# ADR-0025: Deployment topology — compute placement and data locality across machines

- **Status**: Draft
- **Date**: 2026-07-06
- **Deciders**: Aaron

## Context

Several ADRs assume a client/service split without ever stating *where the service runs
relative to the operator*: the wire is UDS **or** TCP ([ADR-0018](0018-client-facing-wire.md)),
compute profiles imply both an on-box GPU (`desktop-vulkan`) and a separate host (`cube-cuda`),
a cloud worker exists ([ADR-0021](0021-internet-cloud-worker.md)), and the voiceprint store +
matcher were placed "server-side" ([ADR-0023](0023-voiceprint-lifecycle.md)) without locating
"server." [ADR-0024](0024-live-speaker-identification.md) made the first concrete locality
ruling (audio clips stay local) and flagged that the general boundary was unmapped. This ADR
maps it.

The actual driver is **spreading compute to where the horsepower is** — not ideology about
local vs cloud. Whether that split is acceptable is governed by one physical fact: **can this
machine reliably reach that horsepower?** Which reduces to whether the machine moves.

- A **non-portable** workstation and a **non-portable** compute host (`cube`) on the same
  network *all the time* can split freely — the network is effectively always present.
- A **portable** machine that sometimes leaves that network cannot depend on the split. It must
  be able to run **fully self-contained**, or to **reach back** to the same compute host over
  whatever network it is on — and it must not simply break when the host is unreachable.

The mechanisms to do this already exist and are only being assembled here: pluggable compute
backends ([ADR-0015](0015-pluggable-compute-backends.md)) make compute relocatable; the
UDS-or-TCP wire ([ADR-0018](0018-client-facing-wire.md)) already spans co-located and networked;
capture store-and-forward ([ADR-0012](0012-capture-store-and-forward.md)) buffers when compute
is unreachable; remote-access security ([ADR-0020](0020-remote-access-security.md)) and the
cloud worker ([ADR-0021](0021-internet-cloud-worker.md)) secure reach-back.

## Decision

Compute placement is a **per-deployment choice driven by two variables — where the horsepower
is, and whether this machine can reliably reach it** — expressed as three operating modes the
*same software* supports. The client always owns capture and operator adjudication; only the
compute stage (ASR / diarization / embedding / matching) relocates.

### The three modes

- **Self-contained.** Capture, compute, store, and adjudication all on one machine. Required
  whenever the machine is portable and may be offline. Models are sized to that machine's own
  hardware. No network boundary exists, so no data leaves.
- **Trusted-LAN split.** A fixed client machine captures and adjudicates; a fixed compute host
  runs the heavy stages. Both are permanently on the same network. Larger models and higher
  throughput than the client could manage alone; depends on a LAN that, by assumption, is
  always present.
- **Reach-back.** A portable machine connects to its home compute host over an untrusted network
  when away ([ADR-0020](0020-remote-access-security.md)). Gives portability *and* the big
  compute host — but only while connected, and every crossing is over the internet.

### Graceful degradation is mandatory, not optional

A machine that can lose its compute host **must not break**. It must either (a) carry local
models and fall back to **self-contained**, or (b) **store-and-forward** captured audio
([ADR-0012](0012-capture-store-and-forward.md)) until reach-back is restored, then drain the
backlog. Which fallback a deployment uses is a configuration of that deployment, but *some*
graceful path is required for any machine that is not permanently co-located with its compute.

### Data-locality overlay

Layered on the modes, per artifact (this generalizes [ADR-0024](0024-live-speaker-identification.md)'s
clip-local rule and locates [ADR-0023](0023-voiceprint-lifecycle.md)'s "server-side" store):

| Artifact | Self-contained | Trusted-LAN split | Reach-back |
|---|---|---|---|
| **Raw audio / clips** | never leaves | crosses LAN (bounded, accepted) | crosses internet → strong transport required |
| **Embeddings / centroids** | local | with the matcher (on host) | with the matcher (on host) |
| **Transcript / IR** | local | returned to client | returned to client |
| **UID + name label** | local | synced | synced |

The rule of thumb: the more sensitive the artifact and the less trusted the network, the more
locality is forced. Raw audio is the most sensitive and is the artifact a deployment is most
likely to keep pinned; embeddings and IR travel more freely because they are less reversible or
less sensitive.

## Consequences

### Good

- "Where does the service run" has one answer per deployment, and the whole system inherits it
  instead of each ADR re-deciding locality ad hoc.
- Portability and heavy compute stop being mutually exclusive: reach-back and self-contained
  fallback give a laptop both, at different times.
- Data egress becomes an explicit, per-mode decision rather than an emergent accident of where
  a process happened to run.

### Bad / costs

- The software must genuinely run in all three modes, including carrying local models for
  self-contained fallback — real packaging and testing surface, not just config.
- Reach-back adds transport-security and connection-lifecycle burden
  ([ADR-0020](0020-remote-access-security.md)), and store-and-forward adds backlog-drain logic.
- Model parity across modes is imperfect: a laptop's self-contained models are smaller than the
  cube's, so quality shifts with mode — the operator must understand this drawback.

### Neutral

- Deployment mode becomes a first-class operational concept the operator selects/understands,
  surfaced in awareness/control ([ADR-0010](0010-operator-awareness-and-control.md)).
- Existing mechanisms (0012, 0015, 0018, 0020, 0021) are unified under one model rather than
  extended.

## Alternatives considered

- **Always-local (self-contained only).** Rejected: wastes a capable compute host that a fixed
  workstation could always use, and caps quality at whatever the client machine can run.
- **Always-remote (service always networked).** Rejected: a portable machine off-network would
  simply stop working, and forces audio egress even when both machines are the same box.
- **Leave locality emergent (status quo).** Rejected: it is exactly the ambiguity that made
  "what's local vs remote" unclear, and it lets sensitive audio cross boundaries no one decided.

## Related

- [ADR-0024](0024-live-speaker-identification.md) — clip-local rule that this ADR generalizes
- [ADR-0023](0023-voiceprint-lifecycle.md) — locates its "server-side" voiceprint store
- [ADR-0015](0015-pluggable-compute-backends.md) — the mechanism that makes compute relocatable
- [ADR-0018](0018-client-facing-wire.md) — UDS-or-TCP wire spanning co-located and networked
- [ADR-0012](0012-capture-store-and-forward.md) — buffering when compute is unreachable
- [ADR-0020](0020-remote-access-security.md) — securing reach-back over untrusted networks
- [ADR-0021](0021-internet-cloud-worker.md) — remote compute over the internet
- [ADR-0011](0011-idle-unload-keepalive-lease.md) — a shared compute host serving intermittently
