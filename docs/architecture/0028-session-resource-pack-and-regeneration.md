# ADR-0028: Session resource-pack & regeneration lifecycle — the structured record is the source of truth, transcripts are renders

- **Status**: Draft (pre-build sketch)
- **Date**: 2026-07-07
- **Deciders**: Aaron

> **Note (build-first):** this is a design sketch to guide a spike, not a settled decision.
> We don't yet know the real shape of live-capture-through-the-IR, the pack boundary, or the
> retention/finalize mechanics until we build them. This ADR will be rewritten to match what
> the spike actually teaches us, then moved to Proposed. Forward references to ADR-0029/0030
> are placeholders for consumers not yet written.

## Context

The batch pipeline already treats a **structured record as authoritative and human-readable
transcripts as views over it**: `transcribe` runs `run_pipeline` → a schema-validated
**Canonical IR** ([ADR-0006](0006-canonical-ir-contract.md)), and `render(ir, fmt)` derives
`md`/`vtt`/`json` from it — `render.py` states it outright ("the IR is the source of truth;
renderers are pure views"). The **live** path does not. `capture.py` writes markdown lines
straight to disk (`out.write(f"[{ts}] {speaker}: {text}")`), never building an IR, and its
`_process` finally-block **unlinks every audio chunk after transcribing it** (plus backlog-drop
and terminal-drain deletes). The sole surviving artifact is a flat transcript, and the substrate
is destroyed — exactly what [ADR-0027](0027-robust-online-speaker-attribution.md) noted when it
observed "chunks are unlinked and unlinkable after processing," so a meeting cannot be replayed.

This is the wrong outcome for a system whose identity layer is designed to **compound**:
[ADR-0024](0024-live-speaker-identification.md) makes speaker identity provisional-then-refined
and every naming/merge/split reversible, and [ADR-0027](0027-robust-online-speaker-attribution.md)
explicitly defers the authoritative attribution to an offline reconciliation pass. If the live
path keeps only rendered markdown and throws the audio away, none of that refinement can reach a
transcript already written — a person named or a merge corrected next month cannot improve last
month's meeting, because the meeting kept no re-labelable structure and no re-processable audio.

The forces:

- **Refinement must be able to reach back.** Attribution improves over time; the transcript that
  was produced when identity was worst is the one that most needs the later, better identity.
- **Two different costs of "regenerate."** Re-styling a transcript (md ↔ srt ↔ speaker-grouped)
  is a pure function of the record. Re-deriving the record itself (better ASR/diarization/
  attribution) needs the *audio* back. Collapsing these into one word hides that one is free and
  the other is expensive and only sometimes possible.
- **Audio is the most sensitive artifact and the scarcest to keep.** Retaining listenable
  third-party voice — even compressed — is precisely the biometric-consent case
  [ADR-0013](0013-retention-and-consent.md) defers and [ADR-0024](0024-live-speaker-identification.md)
  flags as load-bearing. Retention is therefore a *bounded* privilege, not a default.
- **A session's artifacts are currently scattered** (a markdown file here, gallery writes there,
  clips deleted) with no single unit to move, encrypt, replicate, or expire — which the locality
  and replication decisions ([ADR-0025](0025-deployment-topology-and-data-locality.md),
  [ADR-0026](0026-shared-speaker-identity-store.md)) assume exists.

## Decision

A **per-session resource pack is the unit of persistence**, its **structured record is the
Canonical IR extended**, and **regeneration has two tiers** gated by whether audio is still
retained. This is a **draft / debate** artifact: each decision lays out the fork and lands a
recommendation, for review before commit.

### Decision 1 — the structured record is the source of truth; transcripts are renders

The session's authoritative artifact is a **structured record**, and it is the **Canonical IR
([ADR-0006](0006-canonical-ir-contract.md)), extended** — *not* a new parallel format. The batch
path already does this; live capture is brought onto the same contract instead of writing
markdown directly. `.md`/`.srt`/`.vtt`/speaker-grouped/etc. are **generated views** produced by
`render`, never stored as the truth.

The extension carries what live attribution needs and the batch IR does not yet hold: per-turn
**speaker UID + confidence**, per-segment **embedding references** (into the evidence store, not
inline biometrics — [ADR-0024](0024-live-speaker-identification.md)), and session-scoped
**timing/provenance**. This is the concrete instance of the epoch/session identity that
[ADR-0014](0014-ir-epoch-session-identity.md) deferred until a live session actually needed it;
the record makes a live session a first-class Canonical IR document.

Retroactive refinement — **relabel / merge / split** — edits the *record*, then re-renders. This
composes directly with [ADR-0024](0024-live-speaker-identification.md)'s UID/label split: turns
reference opaque **UIDs**, names are mutable labels resolved at render time, so naming, merging,
and splitting **never rewrite turn references** — they update the speaker table and the record's
UID bindings, and every view regenerates cleanly. Correcting a meeting's identities is a cheap
edit-plus-re-render, not a re-transcription.

- **1a — keep the live path writing markdown directly (status quo).** *Reject.* It is the root of
  the problem: the only artifact is a lossy view, unre-labelable and unre-renderable.
- **1b — invent a live-specific session format alongside the IR.** *Reject.* Two source-of-truth
  formats to schema, version, render, and reconcile, for no gain — the IR's `speakers[]` /
  `turns[]` shape already fits, needing only additive fields (the same additive discipline
  [ADR-0023](0023-voiceprint-lifecycle.md) used for `source:"enrolled"`).
- **1c — the record IS the extended Canonical IR; live capture routes through it (RECOMMEND).**
  One contract, one renderer, one schema-versioning story; batch and live converge; refinement is
  an IR edit. **Recommendation: 1c.**

### Decision 2 — the session pack is one portable container

A session persists as **one pack** (a single tar.gz / object), bundling:

- the **structured record** (the extended IR of Decision 1);
- the session's **per-speaker voiceprint embeddings** (the refinement written back per
  [ADR-0024](0024-live-speaker-identification.md));
- **compressed, speaker-isolated audio clips** (Decision 4) — the re-process substrate;
- **session metadata** — participant count, reconciliation answers
  ([ADR-0030](0030-reconciliation-questionnaire.md), consumer), and quality/discrimination
  metrics.

One pack is **one thing to move, seal, replicate, or expire.** It is the natural
**replication/reconciliation unit** for [ADR-0026](0026-shared-speaker-identity-store.md) and
slots into [ADR-0025](0025-deployment-topology-and-data-locality.md)'s per-artifact locality
overlay wholesale (the record and embeddings may travel within the trust domain; the clips are
the pinned-most artifact). The alternative — leaving artifacts scattered across a markdown file,
gallery writebacks, and a clip store with no common boundary — is what makes locality, encryption,
and expiry each an ad-hoc per-artifact chore; the pack gives them one seam.

### Decision 3 — two-tier regeneration

- **(a) Re-render — cheap, always available.** A pure function of the structured record → any
  transcript style. Needs no audio; available for the pack's whole life, including after finalize.
  This is `render` generalized: relabel/merge/split then re-render is the everyday refinement loop.
- **(b) Re-process — expensive, audio-gated.** Re-run ASR / diarization / attribution over the
  **retained audio** with an improved corpus/models to regenerate **the record itself** — better
  text *and* better attribution, not just a restyle. This is how a maturing gallery
  ([ADR-0024](0024-live-speaker-identification.md)) actually reaches an old meeting, and it is the
  offline-reconciliation pass of [ADR-0027](0027-robust-online-speaker-attribution.md) made
  repeatable rather than one-shot. **Re-process is available only while the pack retains audio**
  (Decision 5). Once audio is stripped, only re-render remains.

The two tiers are why the substrate matters: without retained audio the system can restyle a
transcript forever but can never *improve* it beyond a re-labeling of what the first pass heard.

### Decision 4 — retain compressed audio, not raw WAV

Retain the re-process substrate as **compressed, speaker-isolated clips**, **not raw WAV**. The
one decision that matters is *compressed vs lossless*: raw WAV is ~10× the size for fidelity the
downstream ASR/diarization models do not consume, so it is not worth keeping at rest.[^codec]
Default to an **opus-class** codec; the **pack format keeps the codec swappable behind it** (the
record references clips by role/UID + offset, not by codec), so the exact codec is an
implementation detail, not an architectural fork. Clips are speaker-isolated so they double as the
[ADR-0024](0024-live-speaker-identification.md) audition/adjudication exemplars, joined by UID.

[^codec]: Sizes: lossless WAV ≈ 115 MB/hr; opus-class ≈ 10 MB/hr. The contrast justifies "not raw
WAV"; it is not a codec-selection deliberation.

### Decision 5 — the finalize lifecycle: active → finalized

A pack has two states, and **finalize is the privacy/storage transition**:

- **active** — audio retained → **both** regeneration tiers (re-render *and* re-process).
- **finalized** — audio **stripped** → **re-render only**. The durable structured record and the
  voiceprint embeddings are **kept** (they are the compounding asset,
  [ADR-0026](0026-shared-speaker-identity-store.md)); only the listenable substrate is removed.

Finalize bounds biometric retention without discarding the transcript or the identity graph.
Stripping audio (whether by explicit finalize, a retention **TTL**, or `DELETE`) is a
**crypto-erase**: shred the clips' per-record data key per [ADR-0023](0023-voiceprint-lifecycle.md),
not merely an index-row delete. A retention TTL and an **opt-in** posture govern how long a pack
may stay active; the default must not silently expand biometric retention beyond what
[ADR-0013](0013-retention-and-consent.md) permits.

### Cross-cutting obligations

- **Consent ([ADR-0013](0013-retention-and-consent.md), a stub).** Retaining third-party
  colleagues' voice audio — even compressed, speaker-isolated clips — is exactly the case 0013
  defers. This ADR names that as an honest dependency: the active-state retention TTL and opt-in
  posture **co-finalize with 0013** (the same pattern [ADR-0023](0023-voiceprint-lifecycle.md)
  used). This ADR sets the *mechanism* (finalize, TTL, crypto-erase); 0013 sets the *policy*.
- **Encryption at rest ([ADR-0023](0023-voiceprint-lifecycle.md)).** Retained clips and embeddings
  are sealed under the per-record data-key envelope; finalize and delete are crypto-erase by
  data-key shred.
- **Locality ([ADR-0025](0025-deployment-topology-and-data-locality.md),
  [ADR-0026](0026-shared-speaker-identity-store.md)).** Clips never leave the trust domain;
  embeddings, UIDs, and names may travel within it; the pack is the replication unit.

### Scope boundary

This ADR decides the **storage format + pack + regeneration/finalize lifecycle only.** It
**enables but does not define** its consumers, and must not absorb them:

- the supervised **teaching-mode UX** ([ADR-0029](0029-teaching-mode-ux.md), future);
- **corpus-priming** of live sessions from prior packs (an implementation + a
  [ADR-0024](0024-live-speaker-identification.md)/[ADR-0027](0027-robust-online-speaker-attribution.md)
  amendment, not this ADR);
- the end-of-session **reconciliation questionnaire / adaptive trigger**
  ([ADR-0030](0030-reconciliation-questionnaire.md), future) — this ADR only reserves a slot for
  its answers in pack metadata.

## Consequences

### Good

- Retroactive refinement becomes possible at all: a better gallery can reach an old meeting, which
  the markdown-only live path made impossible.
- One source-of-truth contract for batch *and* live — the IR — with transcripts as pure renders;
  no parallel live format to maintain.
- The two-tier split makes the cost model honest: re-render is free and always available;
  re-process is expensive and audio-gated, and the gate is visible in the pack's state.
- One portable pack is a single unit to move, seal, replicate, and expire — the shape
  [ADR-0025](0025-deployment-topology-and-data-locality.md)/[ADR-0026](0026-shared-speaker-identity-store.md)
  already assume.
- Finalize gives a clean privacy transition: bound biometric retention without losing the
  transcript or the compounding identity graph.

### Bad / costs

- Retaining third-party voice audio (even compressed) raises the consent/retention bar; the TTL,
  opt-in posture, encryption, and crypto-erase are now load-bearing, and the policy is blocked on
  [ADR-0013](0013-retention-and-consent.md).
- Storage grows: even opus-class clips (~10 MB/hr) plus embeddings plus the record accumulate per
  active session — finalize/TTL is what bounds it, and must actually run.
- The Canonical IR schema gains session-scoped fields (UID/confidence/embedding-ref/timing) — an
  additive, versioned schema change ([ADR-0006](0006-canonical-ir-contract.md)'s
  `ir_schema_version` handshake, as [ADR-0023](0023-voiceprint-lifecycle.md) used for `source`).
- Re-process is only as good as the retained substrate: speaker-isolated compressed clips cap the
  fidelity a future pass can recover versus the original live audio.

### Neutral

- **`capture.py` is already 522 lines (>500 review threshold);** routing live capture through the
  record + retention must **split it at its natural seams — audio-path detection, the
  window-transcribe loop, and session drain-and-persist — not grow it.** The persist seam is
  precisely where the current markdown-write and chunk-unlink live, so it is the seam this ADR
  changes; extracting it is a prerequisite, not incidental cleanup.
- The live view stays provisional ([ADR-0027](0027-robust-online-speaker-attribution.md)); the
  pack is what lets its provisional attribution be superseded later rather than frozen in markdown.
- "Active vs finalized" becomes an operator-visible session state, surfaced in awareness/control
  ([ADR-0010](0010-operator-awareness-and-control.md)) alongside deployment mode.

## Alternatives considered

- **Markdown transcript as the only artifact (status quo live path).** Rejected: lossy, unre-
  labelable, unre-renderable, and it destroys the re-process substrate — the problem this ADR exists
  to fix (Decision 1a).
- **A separate live session format alongside the Canonical IR.** Rejected: two source-of-truth
  formats for no benefit; the IR needs only additive fields (Decision 1b).
- **Retain raw WAV for maximum re-process fidelity.** Rejected: ~10× the size at rest for fidelity
  the models do not consume; compressed speaker-isolated clips are ASR/diarization-adequate and
  double as adjudication exemplars (Decision 4).
- **Keep audio indefinitely (no finalize).** Rejected: unbounded biometric retention of third-party
  voice, contrary to [ADR-0013](0013-retention-and-consent.md)/[ADR-0024](0024-live-speaker-identification.md);
  finalize + TTL bound it while preserving the durable record and embeddings.
- **One regeneration path (re-transcribe on demand).** Rejected: conflates the free restyle with the
  expensive, audio-gated re-derivation, hiding both the cost and the fact that re-processing is
  impossible once audio is gone.

## Related

- [ADR-0006](0006-canonical-ir-contract.md) — the Canonical IR this record extends; source-of-truth-vs-renders is its principle, applied to live capture
- [ADR-0014](0014-ir-epoch-session-identity.md) — the deferred session/epoch identity the record now actualizes
- [ADR-0024](0024-live-speaker-identification.md) — UID/label split, merge/split, evidence store, clip-local rule the pack inherits
- [ADR-0027](0027-robust-online-speaker-attribution.md) — the spike that writes markdown + unlinks audio; its offline reconciliation is Decision 3's re-process, made repeatable
- [ADR-0023](0023-voiceprint-lifecycle.md) — envelope-at-rest + crypto-erase for retained clips/embeddings
- [ADR-0025](0025-deployment-topology-and-data-locality.md) — per-artifact locality the pack slots into
- [ADR-0026](0026-shared-speaker-identity-store.md) — the pack as replication/reconciliation unit
- [ADR-0013](0013-retention-and-consent.md) — biometric consent/retention this ADR's TTL + opt-in co-finalize with
- [ADR-0022](0022-live-audio-ingest.md) — the sessionized live-ingest service this record/retention feeds
- [ADR-0009](0009-capture-cadence.md) — session epoch = one pack = one IR document
- [ADR-0029](0029-teaching-mode-ux.md), [ADR-0030](0030-reconciliation-questionnaire.md) — consumers this ADR enables but does not define
