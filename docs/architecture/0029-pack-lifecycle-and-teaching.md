# ADR-0029: Pack lifecycle operations — packs as curatable bundles that teach the voiceprint library

- **Status**: Accepted
- **Date**: 2026-07-08
- **Decided by**: extends [ADR-0028](0028-session-resource-pack-and-regeneration.md) and the [session-pack spec](../specs/session-pack.md) (§7–§9); reserved slot the spec named "teaching-mode UX"

## Context

[ADR-0028](0028-session-resource-pack-and-regeneration.md) made the **session pack** the one unit
of persistence and defined **`extract(pack)`** as the universal path from a pack's speaker
embeddings into the durable voiceprint library. The [spec](../specs/session-pack.md) fixed the
on-disk shape and named the lifecycle: states (§7), extraction (§8), and a bidirectional
session↔voiceprint reference graph (§9).

But the pack was only ever *created*. In the build to date:

- `extract()` existed in code but was called **only inside `enroll`** — never exposed.
- There was **no way to see** what packs exist, or **inspect** one's speakers/audio.
- A `listen` capture produced a pack whose remote speakers were anonymous (`S1`, `Remote`) — and
  **nothing could rename them**, so a real meeting could not become named training data.
- The only route to a named voiceprint was reading the Rainbow Passage aloud (`enroll`).

The consequence: the richest training substrate we produce — real meeting audio, already diarized,
already carrying per-speaker embeddings and isolated clips — was a **dead end**. The speaker most
worth learning (a recurring colleague on a real call) was exactly the one we could not name.
Maturing the *lifecycle* of the bundle is the precondition for any voiceprint wrangling.

## Decision

**A session pack is an operable, curatable object, not a write-once artifact.** Expose the
lifecycle the spec already defines as a `pack` verb group, and make **relabel** the core teaching
primitive that turns an anonymous diarized speaker into named library training data.

### 1 — Packs are addressable and discoverable

`pack list` enumerates packs; `pack show <uid>` inspects one (speakers, per-speaker turn/speech
counts, whether an isolated clip is present, audio state, graph edges). Packs are addressed by
their `<uid>` (§3.1) or a unique prefix. **Discovery is recursive** over the sessions store: a
`listen` session lands its pair in its own `sessions/<id>/` subdir while an `enroll` pack lands
flat — both are found, ordered newest-first by the date-prefixed blob name. The **blob is
authoritative** (§6): a pack whose loose `.md` was deleted still lists, its metadata read from the
embedded `session.md`.

### 2 — Relabel is the teaching primitive

`pack relabel <uid> <speaker> <name>` sets the speaker's `display_name` in `record.ir.json` and
**re-packs** the bundle. Because the record is the source of truth and the isolated clip +
embedding are keyed by the canonical speaker id, the *next* `extract` folds that speaker into the
library **under the corrected name**. This is the load-bearing loop:

```
listen (real meeting) → pack list → pack audition S1 → pack relabel S1 Priya → pack extract → voiceprint "Priya"
```

`pack audition <uid> <speaker>` plays the speaker's isolated clip so the operator can identify
*who* an anonymous label is before naming it — the clips ADR-0028 §6 retained exactly for this.

`pack delete <uid>` removes a pack (blob + sidecar, tidying an emptied `sessions/<id>/` subdir).
Crucially it deletes the *session*, **not the identities it taught**: a voiceprint compounds from
several packs, so pruning one is a separate deliberate act — never a side effect of dropping a
session. This is the everyday hygiene verb (e.g. clearing practice captures); the audio-shredding
**finalize** of §7 is the heavier, policy-bound sibling, still deferred.

### 3 — Extraction is exposed and closes the graph

`pack extract <uid>` runs the universal `extract()` (the fast path over the cached
`embeddings.json` seed, §8.1) and then **writes the session→voiceprint back-edge** (`voiceprints:`,
§9) onto the pack, so the reference graph is bidirectional in both the loose sidecar and the
authoritative embedded copy. `extract` remains idempotent (folding a `source` already present is a
no-op), so re-running the loop cannot inflate `samples`.

### 4 — One atomic rewrite primitive under every mutation

Every mutation (relabel now; finalize later) goes through a single `_rewrite_blob` primitive:
replace `record.ir.json` + `session.md`, pass every other member through, re-normalize metadata
(mtime/owner stripped, as at creation), write to a temp sibling, and atomically swap. A crash
mid-rewrite can never truncate the authoritative blob or leak host metadata.

### Scope boundary

This ADR decides the **operations and their contract**; it does not add a schema field or change
the pack format (spec v0.1 already carries `display_name`, `state`, and the graph edges). The CLI
`pack` verb group is the surface today; the same operations move behind the client-facing wire
(ADR-0018) unchanged when a client needs them.

## Consequences

### Good

- Real captures become **first-class training data**: any meeting can be curated into named
  voiceprints, not just read-aloud enrollments — directly serving the speaker-discrimination goal
  ([ADR-0016](0016-speaker-naming-strategy.md)).
- The bundle is now **inspectable and browsable**, closing the gap between the spec's promised
  lifecycle (§7–§9) and what shipped.
- The bidirectional graph (§9) is actually written, so `sessions/` + `library/` walk as one graph.
- Mutations are **crash-safe and metadata-clean** by construction (one primitive).

### Bad / costs

- Relabel **rewrites the whole blob** to change one name — O(pack size) I/O for a small edit. Packs
  are small (opus clips), so acceptable; a future in-place metadata edit is possible but not worth
  the complexity now.
- `audition` shells out to a player (`ffplay`/`paplay`/`aplay`); no player → no audition (the
  relabel path still works without it). Acceptable degradation.
- Relabel keys the extracted voiceprint uid as `<pack_uid>-<slug(name)>`; two distinct people who
  share a display name still need adjudication (deferred) to avoid collision.

### Neutral

- The `pack` verb group is CLI-only for now; parity behind the wire is deferred with the rest of
  the Rust-client feature surface.

## Realized (as built)

- `pack.py`: `find_packs` (recursive), `load_pack` (uid/prefix/path), `pack_details`, `read_clip`,
  `relabel`, `link_voiceprints`, `delete_pack`, and the `_rewrite_blob` primitive.
- `cli.py`: `pack {list,show,extract,audition,relabel,delete}`.
- Verified end-to-end on **real capture audio**: relabel of a diarized `S1` → named voiceprint in
  the library, with the `voiceprints:` back-edge written at the correct relative depth for the
  nested `sessions/<id>/` layout.

### Deferred / not-yet-realized

- **Finalize / retention / crypto-erase** (§7) — strip `audio/`, flip `state`, TTL. The
  `_rewrite_blob` primitive already supports `drop_audio`; the policy + key-shred wiring
  co-finalizes with [ADR-0013](0013-retention-and-consent.md)/[ADR-0023](0023-voiceprint-lifecycle.md).
- **Re-process** (§7) — re-run ASR/diarization over `audio/` to regenerate the record.
- **Adjudication** — merge/split speakers, disambiguate genuine name collisions, promote across
  packs; and an **interactive teaching TUI** over the audition→relabel→extract loop.
- **Quality metrics** (§5 `quality`) — SNR, speech coverage, embedding separation.

## Alternatives considered

- **Leave `extract` buried in `enroll`; add only read-only `list`/`show`.** Rejected: read-only
  visibility does not let a meeting speaker be named, which is the entire point — the anonymous
  remote stays a dead end.
- **A separate "teaching store" distinct from packs.** Rejected: violates ADR-0028's "one unit of
  persistence." The pack already holds the record, clips, and embeddings; curation belongs on it.
- **Edit only the loose `.md` and re-derive the blob lazily.** Rejected: the blob is authoritative
  (§6); a loose-only edit would make the loose sidecar and the embedded copy disagree until some
  later re-pack, exactly the drift §6 warns against.

## Related

- [ADR-0028](0028-session-resource-pack-and-regeneration.md) — the pack model this operationalizes.
- [session-pack spec](../specs/session-pack.md) — §7 states, §8 extraction, §9 graph.
- [ADR-0016](0016-speaker-naming-strategy.md) — voiceprint enrollment as the primary naming path.
- [ADR-0024](0024-live-speaker-identification.md) — the live path that consumes named voiceprints.
- [ADR-0013](0013-retention-and-consent.md) / [ADR-0023](0023-voiceprint-lifecycle.md) — the
  retention/crypto-erase policy the deferred `finalize` co-finalizes with.
