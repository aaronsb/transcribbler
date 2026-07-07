# ADR-0028: The session pack — one open, self-describing unit of persistence, with two-tier regeneration and universal voiceprint extraction

- **Status**: Accepted
- **Date**: 2026-07-07
- **Accepted**: 2026-07-07 — realized by PR #30 (enroll → session pack + universal `extract`) and
  PR #31 (live capture/listen persist real packs with retained, time-sliced audio). See
  **[Realized (as built)](#realized-as-built--prs-30-31)** for the sub-decisions the implementation
  surfaced and what remains deferred.
- **Deciders**: Aaron

## Context

The batch pipeline already treats a **structured record as authoritative and human-readable
transcripts as views over it**: `transcribe` runs `run_pipeline` → a schema-validated
**Canonical IR** ([ADR-0006](0006-canonical-ir-contract.md)), and `render(ir, fmt)` derives
`md`/`vtt`/`json` from it (`render.py`: "the IR is the source of truth; renderers are pure
views"). The system's identity layer is designed to **compound** — [ADR-0024](0024-live-speaker-identification.md)
makes speaker identity provisional-then-refined and every naming/merge/split reversible, and
[ADR-0027](0027-robust-online-speaker-attribution.md) defers authoritative attribution to an
offline reconciliation pass. For that compounding to reach *back* into past sessions, each
session must persist something re-labelable and re-processable, under one boundary that can be
moved, sealed, replicated, and expired.

*(Motivating example: this crystallized in live use, where the same speaker fragmented across
audio conditions and the live path kept only lossy markdown — a transcript that could neither be
re-labeled nor re-processed once the audio was gone. The decision below is the durable principle,
not that episode.)*

The forces:

- **Refinement must reach back.** Attribution improves over time; the transcript produced when
  identity was worst is the one that most needs the later, better identity.
- **"Regenerate" hides two very different costs.** Re-styling a transcript (md ↔ srt ↔
  speaker-grouped) is a pure function of the record. Re-deriving the record itself (better
  ASR/diarization/attribution) needs the *audio* back. One word conflates the free operation with
  the expensive, sometimes-impossible one.
- **Audio is the most sensitive and scarcest artifact to keep.** Retaining listenable third-party
  voice — even compressed — is the biometric-consent case [ADR-0013](0013-retention-and-consent.md)
  defers and [ADR-0024](0024-live-speaker-identification.md) flags as load-bearing. Retention is a
  *bounded* privilege, not a default.
- **Enrollment and conversation are not different mechanisms.** Both are "capture a person's voice
  and keep the substrate." Treating them as two subsystems (a session store *and* a read-aloud
  enroll path) duplicates storage, extraction, and consent handling for one underlying thing.
- **A session's artifacts are currently scattered** (a markdown file, gallery writebacks, deleted
  clips) with no single unit for the locality and replication overlays
  ([ADR-0025](0025-deployment-topology-and-data-locality.md),
  [ADR-0026](0026-shared-speaker-identity-store.md)) to act on.

## Decision

**The session pack is the one unit of persistence in transcribbler.** It is an open,
self-describing bundle of files whose structured record is the Canonical IR extended; every
capture produces one; regeneration has two tiers gated by whether audio is still retained; and
voiceprint extraction is a single operation defined over any pack. The concrete on-disk contract
(naming grammar, frontmatter schema, tar layout, extraction signature) is the
**[session-pack format spec](../specs/session-pack.md)**; this ADR decides the model, the spec
carries the shape.

### 1 — One kind of artifact for every capture

Every capture — a conversation *or* an enrollment — produces the **same kind of artifact: a
session pack.** An enrollment is not a special mechanism; it is a session pack that happens to
contain a single speaker, tagged for training. This collapses what would otherwise be two stores
into one: the same persistence, extraction, finalize, and consent machinery serves both, and a
conversation and a deliberate voice sample differ only in their contents and tags, not in kind.

### 2 — A pack is OKF-aligned "just files" that survive their tooling

A pack is modeled on the **Open Knowledge Format** (Google Cloud,
[how the OKF can improve data sharing](https://cloud.google.com/blog/products/data-analytics/how-the-open-knowledge-format-can-improve-data-sharing)) —
knowledge as portable files, not rows in a proprietary store. Three OKF principles are adopted
directly:

- **Minimally opinionated.** Only a tiny required metadata core is fixed; producers extend the
  frontmatter and the bundle freely without a schema migration.
- **Producer/consumer independence.** The format *is* the contract. Capture (the producer) and
  rendering, extraction, and adjudication (the consumers) evolve and swap independently — nothing
  reaches across the format to a shared runtime.
- **Format, not platform.** A pack opens with `tar` plus stdlib parsers; no transcribbler process
  is required to read one. The value lives in the files, so it outlives any version of the tool.

And OKF's **reference-as-graph** idea: small human-readable documents carry typed references to
each other and to their heavier resources, forming a browsable graph rather than an opaque
database (see decisions 6 and 7).

### 3 — The record is the extended Canonical IR; transcripts are renders

The pack's authoritative artifact is a **structured record**, and it is the **Canonical IR
([ADR-0006](0006-canonical-ir-contract.md)), extended** — not a new parallel format. Live capture
is brought onto the same contract the batch path already uses; `.md`/`.srt`/`.vtt`/speaker-grouped
outputs are **generated views** produced by `render`, never stored as truth. The extension carries
what live attribution needs and the batch IR does not yet hold: per-turn speaker **UID +
confidence**, per-segment **embedding references** (into the evidence store, not inline
biometrics — [ADR-0024](0024-live-speaker-identification.md)), and session-scoped
**timing/provenance** — the concrete instance of the epoch/session identity
[ADR-0014](0014-ir-epoch-session-identity.md) deferred. Retroactive **relabel / merge / split**
edits the *record*, then re-renders; because turns reference opaque UIDs and names resolve at
render time, identity operations never rewrite turn references.

### 4 — Two loose artifacts plus a self-describing blob

A pack surfaces as **two loose files** plus one archive that can stand alone:

- a human-friendly **`.md`** — YAML frontmatter plus the rendered transcript — whose frontmatter
  *references* the archive (the OKF `resource:` pattern);
- a verbose-named **`.tar.gz` blob** holding the record, the compressed audio, and metadata.

The blob **always packs a copy of the `.md` at creation.** A pack is therefore self-describing:
lose the loose sidecar ("I deleted my `.md` files") and the embedded copy is authoritative. The
loose `.md` is a convenience view over a bundle that already contains everything needed to rebuild
it.

### 5 — Two-tier regeneration, gated by retained audio

- **Re-render — cheap, always available.** A pure function of the record → any transcript style.
  Needs no audio; available for the pack's whole life, including after finalize. Relabel/merge/split
  then re-render is the everyday refinement loop.
- **Re-process — expensive, audio-gated.** Re-run ASR / diarization / attribution over the
  **retained audio** with improved models/corpus to regenerate **the record itself** — better text
  *and* better attribution. This is [ADR-0027](0027-robust-online-speaker-attribution.md)'s offline
  reconciliation made repeatable, and how a maturing gallery reaches an old session. Available only
  while the pack retains audio (decision 8). Once audio is stripped, only re-render remains.

Audio is retained as **compressed, speaker-isolated clips, not raw WAV**: raw WAV is ~10× the size
for fidelity the ASR/diarization models do not consume.[^codec] Default to an **opus-class** codec;
the record references clips by role/UID + offset, so the codec stays swappable behind the format
and is an implementation detail, not an architectural fork. Speaker-isolated clips double as the
[ADR-0024](0024-live-speaker-identification.md) audition/adjudication exemplars, joined by UID.

[^codec]: Sizes: lossless WAV ≈ 115 MB/hr; opus-class ≈ 10 MB/hr — the contrast justifies "not raw
WAV," it is not a codec-selection deliberation.

### 6 — Universal voiceprint extraction

**Extraction is one operation, defined over any pack** — introspect the pack's audio → per-speaker
embeddings → the voiceprint library — regardless of whether the pack is a conversation or an
enrollment. This is what makes decision 1 pay off: there is a single extraction interface, not one
per capture kind. *Any* pack can improve a voiceprint incidentally; an **enrollment pack is
purpose-built ideal training data** — a single speaker, clean, controlled — but it feeds the very
same extraction. The store can therefore batch-re-extract by tag ("re-extract from all `meeting`
packs with the new model").

### 7 — The voiceprint library is OKF too — one provenance graph

The library is the same kind of thing as the sessions, not a separate database. **Each voiceprint
is a markdown + frontmatter document** that links to the session blobs its embeddings were
extracted from; each session `.md` links the voiceprints present in it. Sessions ↔ library form
**one browsable provenance graph** of small text files pointing at binary blobs — walkable with a
text editor and `tar`, no index server. This is decision 2's reference-as-graph applied to
identity: where a voiceprint came from, and which sessions a person appears in, are both answerable
by following links.

### 8 — Tags carry purpose; finalize bounds retention

**Tags** (already `tags[]` in the Canonical IR schema, mirrored in pack frontmatter) carry a pack's
purpose — `meeting` / `transcription` / `training` / `enrollment` … — and let the store filter and
batch operations. A pack has two lifecycle states:

- **active** — audio retained → both regeneration tiers.
- **finalized** — audio **stripped** → re-render only. The record and the extracted voiceprints
  are **kept** (the compounding asset); only the listenable substrate is removed.

Finalize bounds biometric retention without discarding the transcript or the identity graph.
Stripping audio (explicit finalize, a retention **TTL**, or `DELETE`) is a **crypto-erase** — shred
the clips' per-record data key ([ADR-0023](0023-voiceprint-lifecycle.md)), not an index-row delete.

### Cross-cutting obligations

- **Consent ([ADR-0013](0013-retention-and-consent.md), a stub).** Retaining third-party voice
  audio is exactly the case 0013 defers. This ADR sets the *mechanism* (finalize, TTL,
  crypto-erase); the active-state TTL and opt-in posture **co-finalize with 0013**, which sets the
  *policy*.
- **Encryption at rest ([ADR-0023](0023-voiceprint-lifecycle.md)).** Retained clips and embeddings
  are sealed under the per-record data-key envelope; finalize and delete are crypto-erase by
  data-key shred.
- **Locality ([ADR-0025](0025-deployment-topology-and-data-locality.md),
  [ADR-0026](0026-shared-speaker-identity-store.md)).** Clips never leave the trust domain;
  embeddings, UIDs, and names may travel within it; the pack is the replication unit.

### Scope boundary

This ADR decides the **session-pack format, the regeneration/finalize lifecycle, the universal
extraction interface, and enrollment-as-a-training-pack.** It **enables but does not define** its
consumers, and must not absorb them:

- the interactive **teaching / adjudication UX** ([ADR-0029](0029-teaching-mode-ux.md), future);
- the end-of-session **reconciliation questionnaire / adaptive trigger**
  ([ADR-0030](0030-reconciliation-questionnaire.md), future) — this ADR only reserves a slot for
  its answers in pack metadata;
- **corpus-priming** of live sessions from prior packs (an implementation plus a
  [ADR-0024](0024-live-speaker-identification.md)/[ADR-0027](0027-robust-online-speaker-attribution.md)
  amendment, not this ADR).

## Consequences

### Good

- One artifact kind for every capture: conversation and enrollment share persistence, extraction,
  finalize, and consent — no second store, no duplicate read-aloud path to maintain.
- Packs are open OKF "just files": openable with `tar` + stdlib, self-describing (the blob carries
  its own `.md`), and independent of any transcribbler runtime — the value outlives the tool.
- One source-of-truth contract for batch *and* live — the extended Canonical IR — with transcripts
  as pure renders; retroactive relabel/merge/split becomes a cheap edit-plus-re-render.
- The two-tier split makes the cost model honest and the gate visible: re-render is free and always
  available; re-process is expensive and audio-gated by the pack's state.
- Universal extraction turns *any* pack into voiceprint training data, and the tag filter lets the
  store re-extract a whole class of packs when a model improves.
- Sessions and library form one browsable provenance graph — where a voiceprint came from and which
  sessions a person appears in are answerable by following links, not querying a database.
- One portable pack is a single unit to move, seal, replicate, and expire — the shape
  [ADR-0025](0025-deployment-topology-and-data-locality.md)/[ADR-0026](0026-shared-speaker-identity-store.md)
  already assume.

### Bad / costs

- Retaining third-party voice audio (even compressed) **raises the consent/retention bar**; the
  TTL, opt-in posture, encryption, and crypto-erase are load-bearing, and the *policy* is blocked
  on [ADR-0013](0013-retention-and-consent.md), which must co-finalize.
- **Storage grows** — opus-class clips (~10 MB/hr) plus embeddings plus the record accumulate per
  active pack; growth is bounded only by finalize/TTL, which must actually run.
- The **Canonical IR gains session-scoped fields** (UID/confidence/embedding-ref/timing, `tags[]`
  purpose) — an additive, versioned schema change under [ADR-0006](0006-canonical-ir-contract.md)'s
  `ir_schema_version` handshake, as [ADR-0023](0023-voiceprint-lifecycle.md) did for `source`.
- **`capture.py` is 612 lines (>500 review threshold).** When retention lands, routing capture
  through the pack must **split it at its natural seams** — audio-path detection, the
  window-transcribe loop, and session drain-and-persist — not grow it. The persist seam is exactly
  where the current markdown-write and chunk-unlink live, so extracting it is a prerequisite, not
  incidental cleanup.
- **Re-process fidelity is capped by the retained substrate:** speaker-isolated compressed clips
  bound what a future pass can recover versus the original live audio.

### Neutral

- **Current build state (as built, PRs #30/#31):** live capture and `enroll` now write a **real
  session pack** — the record is the extended Canonical IR (`build_live_ir` in
  `capture_persist.py`, `source.kind = "session"`), the operator's `-o` path becomes the pack's
  `.md` sidecar, and a self-describing `…-blob.tar.gz` lands beside it carrying `record.ir.json`,
  the `embeddings.json` seed, speaker-isolated `audio/` clips, and a `session.md` copy. This
  **replaced** the earlier ad-hoc `<stem>.ir.json` + `.md` pair (there is no `ir.assemble_session`;
  the WAV assembler is `capture_persist.assemble_session`, the IR builder is `build_live_ir`).
  **XDG storage exists** (`paths.py`: `sessions/`, `library/`), and the enroll path is merged into
  the pack rather than a separate store. The specifics that fell out of building this — streaming
  audio retention, time-slice isolation, the embedding sidecar, idempotent extraction — are in
  **[Realized (as built)](#realized-as-built--prs-30-31)** below.
- The live view stays provisional ([ADR-0027](0027-robust-online-speaker-attribution.md)); the pack
  is what lets its provisional attribution be superseded later rather than frozen in markdown.
- "Active vs finalized" becomes an operator-visible pack state, surfaced in awareness/control
  ([ADR-0010](0010-operator-awareness-and-control.md)) alongside deployment mode.

## Realized (as built — PRs #30, #31)

Building the model surfaced sub-decisions the ADR and spec did not yet capture. They are recorded
here so the ADR matches the code. **Validated** = covered by unit tests (numpy-free, ffmpeg-guarded:
`test_pack.py`, `test_capture_persist.py`). **Pending** = mechanism built but not yet exercised on
real multi-hour live captures — the retention path is a spike to be re-confirmed on real sessions
(follow-up task #12) before it hardens.

- **Audio retention in a streaming, disk-bounded pipeline.** Live chunks are unlinked as they
  stream — the [ADR-0009](0009-capture-cadence.md) backlog guard drops the oldest unprocessed chunk
  rather than fill the disk — so retention cannot wait until drain: each *processed* chunk is
  **retired** into a `retain/` dir (`_retire` in `capture.py`) instead of deleted. At drain,
  `capture_persist.assemble_session` concatenates the retained stereo chunks into **one aligned
  session wav**; a chunk missing from the middle (dropped by the guard, or discarded while
  listening was paused) is **silence-filled** at its index offset so absolute turn timestamps still
  line up with the audio for slicing. Opt out with `--no-audio` (no `retain/`, no clips; the pack
  still writes — see the embedding-seed point). On **pack-build failure `retain/` is KEPT** so the
  session's raw audio is recoverable; on success, or when zero turns were transcribed, it is
  cleaned. *Validated:* gap-fill assembly and the with/without-audio persist paths. *Pending:*
  behaviour under a genuine multi-hour capture with real backlog drops.
- **Speaker isolation is by TIME-SLICING, not source separation.** A speaker's clip is cut from the
  channel they were captured on — the **operator from the mic channel (0)**, each **remote from the
  meeting-mix channel (1)** — by *that speaker's own diarized turn spans* (`build_speaker_clips` →
  `_slice_channel`: one ffmpeg pass, `pan` → `asplit` → per-span `atrim` → `concat`). Overlapping
  spans are coalesced, and the filtergraph is passed via `-filter_complex_script` (a file, not
  argv) so a long meeting's hundreds of turns cannot blow the argv length limit. Temporal isolation
  yields clean-enough voiceprint exemplars without separating overlapped speech. *Validated:*
  channel + span isolation. This is what decision 5's "speaker-isolated clips" concretely means.
- **The pack carries the embedding seed, so a capture primes the library even under `--no-audio`.**
  Because the Canonical IR schema is `additionalProperties: false`
  ([ADR-0006](0006-canonical-ir-contract.md); confirmed in `schemas/canonical-ir.schema.json`),
  embeddings cannot ride inline on speakers or turns. The pack therefore carries per-speaker
  vectors in a **sibling `embeddings.json`** at the blob root (`{spec_version, pack_uid, vectors}`,
  keyed by canonical speaker id), **seeded at drain from the `SessionGallery` centroids**.
  `extract` reads this cached sidecar — fast, no re-diarize — and folds it into the library; the
  *retained audio* is what enables **future re-extraction with a better model**. This refines
  decision 6 ("introspect the pack's audio → embeddings"): the **fast path reads cached
  embeddings**; the **audio path is the re-extract substrate**, not the everyday extraction route.
  A `--no-audio` pack still primes the library from the seed; it just cannot be re-processed later.
  *Validated:* extract-from-sidecar and the seed-attaches-by-label mapping (a gallery centroid whose
  label never reached the transcript is dropped, not mis-attributed).
- **`extract` is idempotent.** A pack already recorded in a voiceprint's `sources` is **not
  re-folded** (`library.enroll`: `source in existing.sources → return`), so `samples` — the
  running-mean weight and confidence signal — cannot be inflated by re-running extraction. Re-embedding
  a recorded pack under a *new model* is a known v0.1 gap (it needs per-source replacement, not
  another fold). *Validated.*
- **Voiceprint uid = `<pack_uid>-<name>`, but matching is by name.** A brand-new voiceprint's id is
  seeded from the **first** pack that produced it (`extract` → `enroll(uid=f"{pack_uid}-{slug(name)}")`);
  thereafter matching is **by display name across packs** (`find_by_name`, case-insensitive), so
  re-enrollment / re-capture of the same person **compounds one voiceprint** rather than minting
  duplicates. The deliberate tradeoff: distinct people sharing a display name — or the generic
  operator/`You` and `Remote` buckets — **collapse into one voiceprint**. Disambiguating genuine
  name collisions is deferred to teaching-mode adjudication ([ADR-0029](0029-teaching-mode-ux.md)).
  *Validated:* name-match compounding.
- **Codec fallback keeps the pack over the codec.** Clips are transcoded to **opus** when the
  ffmpeg build has `libopus`; when it does not, the **source clip is kept** under its own extension
  (`_to_opus` returns `False` → `audio/<id><srcext>`) rather than losing the pack's audio — the
  spec's §4 "codec-swappable, referenced by role/UID not codec" made concrete. The voiceprint fold
  never needs the clip at all (it reads `embeddings.json`), so a missing encoder never blocks
  extraction. *Validated.*
- **`capture.py` split at its persist seam.** Honouring this ADR's own cost bullet ("split
  `capture.py` at its natural seams, don't grow it"), the **drain-and-persist seam moved to a new
  `capture_persist.py`** (`assemble_session`, `persist_session_pack`); `capture.py`'s net change is
  the `_retire` hook plus a thinner `finally`. `pack.py` holds `write_pack` / `extract` /
  `build_speaker_clips`.

### Deferred / not-yet-realized

The ADR decides more than the build has realized; these are honestly open so the ADR does not
over-claim:

- **Decision 7 — the voiceprint library as md + frontmatter documents — is NOT built yet.**
  `library.py` still stores **one JSON per voiceprint** (`<uid>.json`), not the OKF
  md + frontmatter form. The provenance graph edge that decision 7 needs **does** exist (the record
  carries `sources` back-references to the blobs it was extracted from), so the graph is walkable;
  only the record *format* migration remains, tracked by **GitHub issue #29**.
- **Decision 8 — finalize / TTL / crypto-erase — is NOT implemented.** Packs are always written
  `state: active`; audio is never stripped, no TTL runs, and there is no data-key shred. The
  lifecycle *states* are specified (spec §7) but only the `active` half is exercised. This stays
  blocked on [ADR-0013](0013-retention-and-consent.md)'s consent/retention *policy* co-finalizing.
- **Corpus-priming of live sessions from prior packs is NOT built** — it remains the scope-boundary
  future item (an implementation plus an
  [ADR-0024](0024-live-speaker-identification.md)/[ADR-0027](0027-robust-online-speaker-attribution.md)
  amendment). The seed/`extract` path lays its substrate but does not yet feed a live session.

## Alternatives considered

- **Markdown transcript as the only artifact (status quo live path).** Rejected: lossy,
  unre-labelable, unre-renderable, and it destroys the re-process substrate — the problem this ADR
  exists to fix.
- **A separate live/session format alongside the Canonical IR.** Rejected: two source-of-truth
  formats to schema, version, and render for no gain; the IR needs only additive fields.
- **A dedicated enrollment subsystem separate from session storage.** Rejected: enrollment and
  conversation are the same underlying act (capture + keep the substrate); a separate path
  duplicates storage, extraction, and consent for one thing. Enrollment is a single-speaker,
  training-tagged pack.
- **A proprietary/opaque session database.** Rejected: it fails OKF's format-not-platform test —
  the artifacts would depend on a transcribbler runtime to read, could not be opened with `tar`,
  and would not survive the tool. Open files that reference their resources keep the value in the
  files.
- **Retain raw WAV for maximum re-process fidelity.** Rejected: ~10× the size at rest for fidelity
  the models do not consume; compressed speaker-isolated clips are ASR/diarization-adequate and
  double as adjudication exemplars.
- **Keep audio indefinitely (no finalize).** Rejected: unbounded biometric retention of
  third-party voice, contrary to
  [ADR-0013](0013-retention-and-consent.md)/[ADR-0024](0024-live-speaker-identification.md);
  finalize + TTL bound it while preserving the durable record and voiceprints.
- **One regeneration path (re-transcribe on demand).** Rejected: conflates the free restyle with
  the expensive, audio-gated re-derivation, hiding both the cost and the fact that re-processing is
  impossible once audio is gone.

## Related

- [ADR-0006](0006-canonical-ir-contract.md) — the Canonical IR the record extends; source-of-truth-vs-renders applied to live capture
- [ADR-0014](0014-ir-epoch-session-identity.md) — the deferred session/epoch identity the record now actualizes
- [ADR-0024](0024-live-speaker-identification.md) — UID/label split, merge/split, evidence store, clip-local rule the pack inherits
- [ADR-0027](0027-robust-online-speaker-attribution.md) — provisional live attribution whose offline reconciliation is this ADR's re-process, made repeatable
- [ADR-0023](0023-voiceprint-lifecycle.md) — envelope-at-rest + crypto-erase for retained clips/embeddings; the voiceprint records the library graph is built from
- [ADR-0025](0025-deployment-topology-and-data-locality.md) — per-artifact locality the pack slots into
- [ADR-0026](0026-shared-speaker-identity-store.md) — the pack as replication/reconciliation unit
- [ADR-0013](0013-retention-and-consent.md) — biometric consent/retention this ADR's TTL + opt-in co-finalize with
- [ADR-0022](0022-live-audio-ingest.md) — the sessionized live-ingest service this record/retention feeds
- [ADR-0009](0009-capture-cadence.md) — session epoch = one pack = one IR document
- [ADR-0029](0029-teaching-mode-ux.md), [ADR-0030](0030-reconciliation-questionnaire.md) — consumers this ADR enables but does not define
- [session-pack format spec](../specs/session-pack.md) — the concrete on-disk contract this ADR decides the model for
