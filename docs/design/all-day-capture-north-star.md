# Design note: continuous all-day capture (north star)

**Status: exploratory — a direction, not a commitment.** Nothing here is scheduled or
decided; it records where the live-capture work could grow so that near-term decisions
stay pointed the right way. Any step toward it must go through the normal ADR process.

## The idea

Run the live-capture loop **continuously through the day** rather than per-meeting, producing
one timestamped, speaker-attributed record of everything the operator hears and says — a
navigable log of the real world that an assistant can query ("what did I commit to today?",
"when did X first come up?", "who was in the 2pm conversation?").

The [live-capture MVP](../architecture/0024-live-speaker-identification.md) is the atom of
this: a timestamped, speaker-attributed transcript written to disk, with the operator
identified deterministically by channel. "All day" is the same loop, always-on and segmented.

## Why the foundation already points here

Most of what all-day capture needs already exists as decisions, not new invention:

- **Always-on without pinning the GPU** — idle-unload / keep-alive lease
  ([ADR-0011](../architecture/0011-idle-unload-keepalive-lease.md)) and capture
  store-and-forward ([ADR-0012](../architecture/0012-capture-store-and-forward.md)) exist so
  capture can run all day and survive gaps without losing audio.
- **A navigable day, not one undifferentiated wall** — session/epoch identity
  ([ADR-0014](../architecture/0014-ir-epoch-session-identity.md)) turns hours of transcript
  into addressable segments.
- **People, not `S1`/`S2`** — the persistent voiceprint identity graph
  ([ADR-0024](../architecture/0024-live-speaker-identification.md),
  [ADR-0026](../architecture/0026-shared-speaker-identity-store.md)) is what makes a day read
  as "talked with Priya, then standup, then a call with…". Over a full day you re-encounter the
  same people across contexts; the gallery names them once and everywhere. All-day capture is
  the use case that most rewards that graph.
- **The query layer** — "the assistant interacts with it" is a retrieval/interface concern
  built *on top of* the Canonical IR the MVP already emits, not a change to capture.

## The dominant constraint: consent and retention

Going from "record a meeting" to "record a life" makes consent and retention
([ADR-0013](../architecture/0013-retention-and-consent.md)) the **governing** concern, not a
footnote. All day, the operator records **third parties who did not opt in**, into a queryable
log of their whole life — the most sensitive artifact the system could hold. This is exactly
why the postures already chosen matter *more* here, not less:

- **Trust-domain locality** ([ADR-0025](../architecture/0025-deployment-topology-and-data-locality.md))
  — raw audio never leaves the operator's trust domain.
- **Crypto-erase lifecycle & encryption at rest**
  ([ADR-0023](../architecture/0023-voiceprint-lifecycle.md)) — retention limits and expunge are
  load-bearing, not optional.
- **Operator awareness & control**
  ([ADR-0010](../architecture/0010-operator-awareness-and-control.md)) — always-on capture must
  be visible and controllable, and third-party consent handled honestly.

The capability is compelling precisely to the degree it is honest about this weight. If all-day
capture is ever pursued, the consent/retention design is the first ADR, not the last.

## Open questions (for whenever this is picked up)

- What is the consent model for ambient third parties captured all day?
- Default retention window and what "expunge my day" means end to end.
- How the day is segmented into sessions/epochs in practice (silence? location? calendar?).
- The query interface: what an assistant is allowed to surface, to whom, and how it is audited.
