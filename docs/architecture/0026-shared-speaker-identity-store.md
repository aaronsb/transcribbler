# ADR-0026: Shared speaker-identity store — one identity graph across machines

- **Status**: Draft
- **Date**: 2026-07-06
- **Deciders**: Aaron

## Context

The speaker-identity graph — voiceprint embeddings, opaque UIDs, and the names bound to them
([ADR-0024](0024-live-speaker-identification.md)) — is a **primary durable outcome of the
system, second only to the transcripts themselves**. It accumulates and sharpens over months
and is expensive to rebuild.

Meetings are captured on more than one machine or substrate (a fixed workstation, a laptop,
possibly a container or cloud instance). If each machine builds its **own** gallery, the same
person is assigned a different UID on each one: the identity graph fragments, the same
voiceprint is duplicated N times, refinement is split across copies instead of compounding, and
a name applied on one machine is invisible on another. The most valuable asset degrades exactly
because it is replicated instead of shared.

This appears to collide with two prior decisions: [ADR-0024](0024-live-speaker-identification.md)
pinned **audio clips local**, and [ADR-0025](0025-deployment-topology-and-data-locality.md)
pinned locality per deployment mode. "One shared identity" and "clips stay local" are only
contradictory under the reading that *local* means *this physical box*. It does not — it means
**within the operator's trust domain**. Fixing that meaning dissolves the conflict and is part
of this decision.

## Decision

There is **one authoritative speaker-identity graph per operator**, shared across all their
machines rather than forked per machine. "Local" is redefined as **within the operator's trust
domain**, and artifacts are shared or pinned by sensitivity within that domain.

### One authoritative graph, hubbed on the compute host

The identity graph (`UID → embedding/centroid`, `UID → name`) has a single source of truth on
the operator's compute host — the same host that runs the matcher and store
([ADR-0023](0023-voiceprint-lifecycle.md), [ADR-0025](0025-deployment-topology-and-data-locality.md)).
In trusted-LAN and reach-back modes, machines read and write **that** graph directly; they do
not maintain independent identities.

### Offline machines hold a replica and reconcile

A machine operating self-contained/offline ([ADR-0025](0025-deployment-topology-and-data-locality.md))
works against a **replica** of the graph and **reconciles** with the hub when it rejoins —
pushing new UIDs, refinements, merges, splits, and name bindings made while offline, and pulling
those it missed. This is the identity-graph analogue of capture store-and-forward
([ADR-0012](0012-capture-store-and-forward.md)).

### Cross-machine duplication is resolved by the existing merge primitive

Two machines independently minting a UID for the same new person is the same problem as an
intra-session split, and uses the same fix: **merge** ([ADR-0024](0024-live-speaker-identification.md)).
On reconciliation the system compares embeddings across the replicas and **suggests merges**
for UIDs that are likely the same person, which the operator confirms. No new conflict machinery
is introduced — cross-machine dedup and within-session correction are one operation.

### Locality within the trust domain, by sensitivity

Redefining "local" as the trust domain (this generalizes
[ADR-0024](0024-live-speaker-identification.md)'s clip rule):

- **Embeddings + UID + name** replicate freely **within** the trust domain (hub ↔ machines) —
  they are the shared graph.
- **Raw audio clips** never leave the trust domain, but within it may live on the hub so *any*
  of the operator's machines can audition a UID's evidence. A machine with no connection holds
  its clips until it can sync them inward (or keeps them pinned locally if the deployment forbids
  clip movement at all).
- **Nothing identity-related egresses to third parties or an untrusted cloud.** Reach-back over
  the internet ([ADR-0020](0020-remote-access-security.md)) is a channel *into the operator's own
  trust domain*, not egress out of it.

### Conflict resolution

Names are operator-authoritative (an explicit human naming wins over an inferred one, latest
explicit wins on genuine conflict). UIDs union via merge. Refinement centroids combine by
weighted mean over their sample counts, so reconciling two refined copies yields the same result
as if all speech had been seen in one place.

## Consequences

### Good

- The identity graph compounds instead of fragmenting: every meeting on any machine sharpens one
  set of voiceprints and one set of names.
- Name a person once, anywhere, and they are named everywhere.
- Cross-machine dedup reuses the merge primitive — no separate distributed-identity mechanism.
- The trust-domain reframing makes "local" mean something precise and portable across the rest
  of the system.

### Bad / costs

- Replica reconciliation is real distributed-state work: sync protocol, ordering, and
  merge-suggestion at rejoin.
- Offline divergence is unavoidable — while a machine is disconnected, its identities can drift
  from the hub's until reconciled; the live view on that machine is provisional until then.
- Clip sync within the trust domain adds transfer and storage decisions the clip-strictly-pinned
  reading would avoid.

### Neutral

- The compute host becomes the identity hub as well as the matcher — a concentration that is fine
  within one operator's trust domain but would need rethinking for multi-operator/multi-tenant use
  (deferred, as in [ADR-0023](0023-voiceprint-lifecycle.md)).
- "Trust domain" becomes a named concept the deployment defines (which machines, which host, which
  network paths count as inside).

## Alternatives considered

- **Per-machine galleries (status quo of a naive multi-machine setup).** Rejected: fragments and
  duplicates the highest-value asset and splits refinement across copies.
- **Central cloud identity service.** Rejected: it would place third parties' biometric voiceprints
  and audio outside the operator's trust domain, violating the consent/retention posture
  ([ADR-0013](0013-retention-and-consent.md), [ADR-0024](0024-live-speaker-identification.md)).
- **Share embeddings but never clips across machines.** Viable and is the fallback for
  clip-movement-forbidden deployments, but rejected as the default because it breaks the
  "listen to identify" loop on any machine that did not capture the meeting.

## Related

- [ADR-0024](0024-live-speaker-identification.md) — the identity graph, UID/name split, and merge/split primitives
- [ADR-0025](0025-deployment-topology-and-data-locality.md) — deployment modes and the trust-domain locality model
- [ADR-0023](0023-voiceprint-lifecycle.md) — voiceprint store and matcher on the compute host
- [ADR-0012](0012-capture-store-and-forward.md) — the store-and-forward pattern reused for reconciliation
- [ADR-0020](0020-remote-access-security.md) — reach-back as a channel into the trust domain
- [ADR-0013](0013-retention-and-consent.md) — consent/retention posture for biometric data
