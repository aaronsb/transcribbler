# ADR-0023: Voiceprint lifecycle — enrollment, store, and matching contract

- **Status**: Proposed
- **Date**: 2026-06-30
- **Deciders**: Aaron

## Context

[ADR-0016](0016-speaker-naming-strategy.md) made voiceprint enrollment the **primary**
speaker-naming tier (`speakers[].source = "enrolled"`) but explicitly **deferred the
implementation** — it sketches an interactive `enroll` loop and an XDG store, and nothing
more. The lifecycle that makes a voiceprint store *usable over time* — enumerate, inspect,
update, delete, add-a-sample — is undecided, as is the wire shape and how matching slots
into the pipeline. This ADR fixes that contract.

The constraints already in place:

- **The embeddings are a byproduct we currently throw away.** pyannote's `DiarizeOutput`
  exposes per-speaker embeddings; the diarizer sidecar
  ([`backend/diarizer/diarize.py`](../../backend/diarizer/diarize.py)) serializes only the
  turn annotations and discards them. Enrollment and matching are mostly "stop discarding
  data we already compute" (ADR-0016).
- **Matching must happen where the pipeline runs.** Speaker naming is applied while building
  the IR ([`pipeline.py`](../../backend/transcribbler/pipeline.py) →
  [`ir.py`](../../backend/transcribbler/ir.py)), which is **server-side**. A matcher that
  names speakers during the transcribe job therefore needs the store on the server.
- **The wire and the principal already exist.** [ADR-0018](0018-client-facing-wire.md) gives
  a versioned `/v1` HTTP contract with server-owned jobs + SSE; [ADR-0020](0020-remote-access-security.md)
  gives a per-request **principal** and the UUID-ownership leak guard the JobStore uses.
- **A voiceprint is biometric data.** Storing a person's voice — especially a *third party's*
  — carries consent and retention obligations ([ADR-0013](0013-retention-and-consent.md))
  and must not be covert ([ADR-0010](0010-operator-awareness-and-control.md)).

The forces, then: reuse the job engine and the principal (don't invent a second protocol or
a second auth model); keep interactivity client-side (the operator listens and names);
build a store robust to real-world voice/channel variation; and treat biometrics as
deletable, owned, and non-covert.

## Decision

**Voiceprints are a server-side, per-principal resource family on the existing `/v1` wire.
Enrollment is two-phase — a server *extraction job* yields per-speaker embeddings + clip
offsets, the client drives the interactive naming, then commits named prints back. Each
voiceprint is a multi-sample centroid. At transcribe time a deterministic cosine matcher
names diarized speakers from the store.**

### The store — server-side, per-principal, multi-sample centroid

Lives under the XDG **data** home (biometric data is not config):
`$XDG_DATA_HOME/transcribbler/voiceprints/` (parallel to `profiles.config_dir()`, which uses
`XDG_CONFIG_HOME`). One record per voiceprint:

```
voiceprint:
  id              UUID (unguessable; the ownership/leak-guard key, as for jobs)
  owner           principal (ADR-0020) — the record is scoped to its creator
  display_name    str          # the name shown in the IR
  role, org, contact           # optional vCard-like metadata (ADR-0016)
  embedding_model              # id + dim — RECORD-level: the whole record is one space
  samples[]                    # >= 1; each: { embedding, provenance, created_at }
  centroid                     # derived (mean of samples, renormalized) — the match key
  created_at, updated_at
```

- **Multi-sample, not single.** A voiceprint accumulates samples across recordings; the
  **centroid** is the match key. This is robust to voice drift and channel/mic differences,
  and it gives `update` a meaningful semantics (add a sample, re-derive the centroid) instead
  of a lossy replace.
- **A record is single-space.** `embedding_model` is a **record-level** invariant: all of a
  voiceprint's samples — and therefore its centroid — live in **one** embedding space.
  Matching only ever compares within that space. Adding a sample whose `embedding_model`
  differs from the record's is **rejected** (`409`): a model change is not a mixed-space
  record but a **cold start** (old centroids don't match the new space; re-enroll), surfaced
  rather than silently wrong.
- **Storage layout (now) — flat dir + record-level owner scoping.** All records live in the
  one `voiceprints/` dir; isolation between principals rests on the record `owner` field and
  the JobStore's UUID + ownership check. Per-principal **on-disk isolation / encryption** is
  the deferred piece (see *Multi-user* below), so adopting the flat layout now does not force
  a storage rework when that policy lands — only a migration of where bytes sit, not of the
  ownership model.

### Enrollment — two-phase (extract job, then client-driven commit)

Enrollment is **not** one interactive server call. It splits along the natural seam: the
heavy GPU work is a job; the human-in-the-loop part is client-side.

1. **Extract (a job).** `POST /v1/voiceprints/extractions` with audio runs **diarize-only**
   through the existing job engine ([ADR-0019](0019-job-scheduling.md) admission/queue/SSE —
   diarization is GPU-heavy and single-flight, so it *must* queue, like any job). The result
   is **candidates**: per diarized speaker, the embedding plus a **representative clip**
   (`offset_s`, `duration_s` — the server picks a long, central contiguous segment, ~up to
   60 s) and, where the store already matches, a **suggested** existing voiceprint.
2. **Name (client-side).** The client already holds the audio it submitted, so it plays each
   clip locally and the operator names or skips — the interactive ADR-0016 loop, kept on the
   thin client (the same division of labor as ADR-0022 putting VAD on the capture host). The
   client only needs the **clip offsets** to do this; it never needs the embedding bytes.
3. **Commit by reference (cheap calls).** Per named speaker, the client either **creates** a
   new voiceprint (`POST /v1/voiceprints` with `{extraction_id, candidate_id, metadata}`) or
   **adds a sample** to an existing one (`POST /v1/voiceprints/{id}/samples` with
   `{extraction_id, candidate_id}`). The commit names a candidate **by reference**; the
   embedding never leaves the server — preserving the "biometrics stay in the store"
   invariant the client-side-store alternative is rejected for failing.

**Extract-result retention.** The extraction job's embeddings + clips are server-side state
with a bounded life: an extraction expires (TTL, e.g. the job-store retention window) and is
purged like any job, and `DELETE /v1/voiceprints/extractions/{id}` discards it eagerly.
Committed samples are copied into the durable voiceprint record; an expired extraction simply
can no longer be referenced (commit returns `404 expired`). A no-op enrollment (operator
skips everyone) commits nothing and the extraction expires untouched — a clean no-op. Extract
over silence yields **empty candidates**; the client shows "no speakers found" and commits
nothing.

Rejected the **server-side interactive enroll job** (a stateful job streaming
`speaker_ready` and blocking on POSTed name/skip decisions): it re-introduces long-lived
interactive job state the batch wire deliberately avoids, and it would re-stream audio the
client already has.

### Lifecycle wire — additive under `/v1`, behind a capability

All additive under `/v1` (no `/v2`), advertised as a `voiceprints` entry in `GET /v1/version`
`capabilities` with a minor `wire_version` bump, so an older backend is *detected*, not
blind-`404`ed (ADR-0018 versioning rule). Every route is scoped to the caller-principal.

| Method + path | Purpose |
|---|---|
| `POST /v1/voiceprints/extractions` | **Phase-1** extract: diarize-only job → candidates (clips + suggestions; embeddings held server-side). SSE via the job's `events`. |
| `DELETE /v1/voiceprints/extractions/{id}` | Discard an extraction + its embeddings eagerly (else it TTL-expires). |
| `GET /v1/voiceprints` | **Enumerate** — metadata + sample count + centroid summary. **Raw embeddings are never returned** (biometric minimization). |
| `POST /v1/voiceprints` | **Create** from an extraction candidate **by reference** (`{extraction_id, candidate_id, metadata}`). |
| `GET /v1/voiceprints/{id}` | **Inspect** one record — metadata only; raw embeddings excluded, same as enumerate. |
| `PATCH /v1/voiceprints/{id}` | **Update metadata** (name / role / org / contact). |
| `POST /v1/voiceprints/{id}/samples` | **Add a sample** by reference (`{extraction_id, candidate_id}`); re-derives the centroid. Rejects (`409`) a different `embedding_model`. |
| `DELETE /v1/voiceprints/{id}/samples/{sample_id}` | Remove one sample, re-derive. **Removing the last sample is rejected (`409`)** — delete the record instead. |
| `DELETE /v1/voiceprints/{id}` | **Erase** the whole record, embedding bytes included. |

`/v1/jobs` (transcribe) and `/v1/voiceprints/extractions` (enroll) are distinct shapes
sharing the job engine underneath; the rest are cheap synchronous CRUD.

### Matching at transcribe time — deterministic, ambiguity-guarded, precedence-aware

Matching **requires diarization** — it consumes per-speaker embeddings, so it only runs on a
job with `diarize=true`. It is gated by a profile property (`voiceprints.enabled`) **and** a
non-empty store; a job submitted with `diarize=false` skips matching regardless of the
profile (no embeddings to match). The matcher slots into the pipeline **after** the diarizer's
canonical speaker map ([`align.canonical_speaker_map`](../../backend/transcribbler/align.py))
and **before** the optional LLM canonicalization ([`canon.py`](../../backend/transcribbler/canon.py)):

- For each canonical diarized speaker, cosine-compare its embedding to every stored centroid
  **in the same embedding space**; take the best match **above a threshold** (profile-level,
  `voiceprints.threshold ≈ 0.7`).
- **Ambiguity guard:** if the top-two candidates are within a small margin
  (`voiceprints.ambiguity_margin`), assign **no** match. ADR-0016's named failure mode for the
  LLM tier is confident *mis-attribution*; the voiceprint tier must not reproduce it —
  abstaining is correct.
- A match sets `source = "enrolled"`, `display_name` from the voiceprint, `confidence` = the
  similarity, and `evidence = ["voiceprint:<id>"]` (a referencable token, in keeping with the
  human-readable `evidence` items the LLM tier emits). The IR carries the **id and the name**,
  never the embedding (biometrics stay in the store).

**Source precedence** (who wins the label), per ADR-0016: **manual > enrolled > llm >
fallback**, enforced at two points. (1) **Pipeline-time** `enrolled > llm > fallback`: the LLM
canonicalization pass ([`canon.py`](../../backend/transcribbler/canon.py)) is offered **only
the still-unnamed** speakers and **must not overwrite** a speaker already `source:"enrolled"`
— otherwise the stated order would silently invert, since canon runs after the matcher.
(2) **Operator edit** `manual > *`: a human override (`source:"manual"`) always wins, applied
post-hoc as the one-row edit of ADR-0016. The `source` field records the winning tier.

**IR compatibility (two separate mechanisms — do not conflate).** The
`speakers[].source` enum gains **`"enrolled"`** (currently `["llm","manual","fallback"]` at
`schemas/canonical-ir.schema.json`). Because typify generates a **closed** Rust enum
(`build.rs`), an older client would **fail to deserialize** the new variant — dropping
`additionalProperties:false` only tolerates unknown object *keys*, not unknown enum *values*.
So this is handled by the **`ir_schema_version` handshake**, not the wire capability: bump
`ir_schema_version` (`0.1 → 0.2`) so a client detects the change at `GET /v1/version` rather
than silently mis-parsing (ADR-0018's reason the handshake exists), **and** give the client's
`source` field a tolerant catch-all variant so a forward-compatible read degrades to "unknown
source" instead of erroring. This is distinct from the `voiceprints` **wire capability**
(below), which advertises the *endpoints*, not the *IR schema*.

### Multi-user: per-principal ownership now; multi-tenant policy deferred

**multi-user ≠ multi-tenant.** The backend serves multiple *principals* but is not a
tenant-isolated service. Scoping each voiceprint to its **owner-principal** (the ADR-0020
model the JobStore already enforces) is free and is the honest minimum: your prints are
yours; another principal can neither enumerate nor match against them.

**Explicitly out of scope for this ADR** (deferred, not silently omitted — the wire stays
forward-compatible with a later sharing layer):

- **Cross-principal sharing** — a shared org directory of voiceprints, ACLs, or a "team"
  scope.
- **Biometric residency policy on a shared always-on host** (the cube, [ADR-0002](0002-hardware-topology.md)) —
  who may store whose voice on shared hardware, and under what retention. This is heavier
  than, and distinct from, the ADR-0013 third-party-*consent* concern below, and needs its
  own decision when multi-tenant use is real.
- **Any cross-principal visibility or admin surface.**

### Biometric privacy (ADR-0013, ADR-0010)

[ADR-0013](0013-retention-and-consent.md) is still **Proposed (a stub)** — its consent and
retention decision is unfinalized, with a current posture of documented operator
responsibility. So the biometric obligations here **co-finalize with ADR-0013** (the same
honest dependency ADR-0022 declares on its Proposed stubs); this ADR sets the mechanism, not
the finalized policy.

- **Consent is an obligation, not a UI nicety.** Enrolling a *third party's* voice is
  biometric capture; the enroll flow surfaces this (ADR-0010 not-covert). The retention/
  consent rules it follows land with ADR-0013.
- **Deletion is real.** `DELETE` removes the embedding bytes, not just an index row.
- **Minimization.** Enumerate and inspect return metadata, not raw biometrics; the store is
  local and never synced off-box by this ADR.
- **Off unless opted in.** Matching runs only when the store is non-empty *and* the profile
  enables `voiceprints` — no silent biometric matching.

```mermaid
sequenceDiagram
    participant C as Client (CLI / capture)
    participant B as Backend (server-side store)
    C->>B: POST /v1/voiceprints/extractions (audio)
    B->>B: diarize-only (job) -> embeddings + clips
    B-->>C: SSE done {candidates: [{embedding, clip, suggested?}]}
    loop per unfamiliar speaker (client-side)
        C->>C: play clip locally; operator names / skips
        C->>B: POST /v1/voiceprints (create)  OR  /{id}/samples (add)
    end
    Note over B: later, at transcribe time
    C->>B: POST /v1/jobs (audio)
    B->>B: diarize -> cosine-match centroids -> source:"enrolled"
    B-->>C: SSE done {ir with named speakers}
```

## Consequences

### Good

- **One contract, one auth model.** Enroll-extract reuses the ADR-0018/0019 job engine; CRUD
  reuses the ADR-0020 principal + leak guard. Little new protocol surface to build, secure,
  or test.
- **Mostly un-discarding data.** The embeddings already exist; the bulk of the work is
  plumbing them out of the sidecar and comparing vectors.
- **Robust + correctable.** Multi-sample centroids tolerate channel/voice variation; metadata
  edits and add-a-sample are cheap, decoupled from any transcript (ADR-0016's one-row-edit
  property holds).
- **Deterministic, cheap naming** for recurring speakers — the operator's actual reality —
  at a fraction of the LLM tier's compute, with an abstain-on-ambiguity guard against
  mis-attribution.
- **Clean seams.** The matcher depends on a store **interface** (DIP — testable against an
  in-memory store, no disk), and the *same* matcher computes both transcribe-time matches and
  the extract-time "suggested" candidates (SRP — one cosine-against-centroids routine, two
  callers). Store, matcher, wire, and client UX stay independently substitutable.

### Bad / costs

- **A threshold + margin to tune** — voice drift, channels, and short clips all affect cosine
  scores; defaults will need iteration on real recordings.
- **Cold start on model change.** Re-embedding the diarizer invalidates stored centroids
  (different space); acceptable and surfaced, but it means a model swap costs re-enrollment.
- **Biometric data at rest.** Even local + per-principal, the store is biometric and inherits
  ADR-0013's obligations; the shared-host policy is left unresolved (deferred above).
- **Per-chunk-of-lifecycle surface.** Several new routes + a store module + a matcher + a
  client enroll UX — more than a single PR; sliced below.

## Alternatives considered

- **Client-side store** (embeddings shipped back per job, client matches as a post-step) —
  rejected: the server couldn't name speakers *during* the job (naming would become a
  separate client render pass), and every transcribe would ship raw biometrics over the wire.
- **Single embedding per voiceprint** — rejected: brittle to channel/voice variation, and
  `update` would discard prior signal.
- **Server-side interactive enroll job** — rejected: long-lived interactive job state the
  batch wire avoids, and it re-streams audio the client already holds.
- **Embedding stored in the IR** — rejected: puts biometric data in every transcript;
  reference by id instead.
- **A bespoke voiceprint protocol** — rejected for the same reason ADR-0018 rejected gRPC:
  no benefit over `/v1` CRUD + the job engine.

## Related

- [ADR-0016](0016-speaker-naming-strategy.md) (the strategy this implements; `source` tiers
  and precedence), [ADR-0005](0005-diarization-flow.md) (diarization provides the
  embeddings), [ADR-0006](0006-canonical-ir-contract.md) (the `speakers[]` table + `source`
  enum this extends), [ADR-0018](0018-client-facing-wire.md) (the wire + versioning this is
  additive to), [ADR-0019](0019-job-scheduling.md) (the extract job's admission/queue),
  [ADR-0020](0020-remote-access-security.md) (the principal that owns a voiceprint),
  [ADR-0013](0013-retention-and-consent.md) (biometric consent/retention/erasure),
  [ADR-0010](0010-operator-awareness-and-control.md) (enrollment is not covert),
  [ADR-0014](0014-ir-epoch-session-identity.md) (the same embeddings also enable speaker
  consolidation — out of scope here), [ADR-0002](0002-hardware-topology.md) (the shared host
  whose biometric-residency policy is deferred).
