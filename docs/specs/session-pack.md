# Session Pack Format — v0.1

- **spec_version**: 0.1
- **Status**: Draft (implementable contract; expected to evolve)
- **Date**: 2026-07-07
- **Decided by**: [ADR-0028](../architecture/0028-session-resource-pack-and-regeneration.md)

This is the concrete on-disk contract for a **session pack** — the one unit of persistence in
transcribbler. It is *not* an ADR; ADR-0028 decides the model and the durable principles, and this
spec fixes the precise, implementable shape those principles take on disk. Where ADR-0028 says
"generalize," this document says "here is exactly what it looks like."

Design lineage is the **Open Knowledge Format** (OKF): a pack is *just files* that survive their
tooling — minimally opinionated (a tiny required core, freely extended), producer/consumer
independent (the format is the contract), and format-not-platform (openable with `tar` plus stdlib
parsers, no transcribbler runtime required).

`spec_version` is `0.1` and is meant to evolve. Consumers MUST tolerate unknown frontmatter keys
and unknown files in the blob (minimally-opinionated rule); producers MAY add both without a
version bump. A version bump is reserved for changes to the *required core* or the blob's required
members.

---

## 1. Overview

A session pack captures one session — a conversation **or** an enrollment — as:

1. a human-friendly **`.md`** sidecar (YAML frontmatter + rendered transcript), and
2. a verbose-named **`.tar.gz`** blob (the self-describing archive of record + audio + a copy of
   the `.md`).

The `.md` is a convenience view; the blob is authoritative and stands alone. Markdown, SRT, VTT,
and speaker-grouped outputs are **renders** of the record inside the blob — never the source of
truth.

Every capture produces the same kind of pack. An **enrollment is a pack with a single speaker,
tagged for training** — not a separate format.

---

## 2. Store layout

Packs live under the XDG data dir (`paths.py`), already established:

```
$XDG_DATA_HOME/transcribbler/            # default: ~/.local/share/transcribbler/
├── sessions/                            # one pack = two loose files here
│   ├── 2026-07-07-team-standup.md                              # friendly sidecar
│   ├── 2026-07-07-090000-0-3600-3f9a2c-blob.tar.gz             # self-describing blob
│   ├── priya-enrollment.md
│   └── 2026-07-07-141205-0-42-7b1e04-blob.tar.gz
└── library/                            # the voiceprint graph (OKF too, see §8)
    ├── 3f9a2c-priya.md                  # one voiceprint = one md + frontmatter
    └── 9c1d77-jonathan.md
```

The two loose artifacts for a pack share a directory; the `.md` frontmatter names its blob (§5),
so the pair is discoverable from the sidecar, and the blob is discoverable/rebuildable on its own.

---

## 3. Naming grammar

### 3.1 Blob (authoritative, machine-derived, verbose on purpose)

```
YYYY-MM-DD-HHMMSS-<start>-<length>-<uid>-blob.tar.gz
```

| Field       | Meaning                                                                                          |
|-------------|--------------------------------------------------------------------------------------------------|
| `YYYY-MM-DD`| UTC date the session began.                                                                       |
| `HHMMSS`    | UTC wall-clock time the session began (zero-padded, 24h).                                         |
| `<start>`   | Start **offset in whole seconds** from the session origin. `0` for a standalone session; nonzero when a pack is a slice/continuation of a longer capture ([ADR-0009](../architecture/0009-capture-cadence.md) epoch). |
| `<length>`  | Session **duration in whole seconds** (matches the IR `source.duration_s`, floored).             |
| `<uid>`     | Short pack id — first 6 hex chars of a UUID4 (`uid = uuid4().hex[:6]`). Disambiguates same-second starts; ties the blob to its `.md` frontmatter `id`. |
| `-blob.tar.gz` | Literal suffix marking the self-describing archive.                                           |

Example: `2026-07-07-090000-0-3600-3f9a2c-blob.tar.gz` — a session that began 2026-07-07 09:00:00
UTC, offset 0, 3600 s (1 h) long, pack `3f9a2c`.

The verbose name is deliberate: a blob is identifiable and sortable from its filename alone, with
no need to open it.

### 3.2 Friendly `.md` (human-chosen or derived, low ceremony)

The sidecar name is a human-friendly slug — a title the operator picks, or a derived default
(`YYYY-MM-DD-<slug>.md`). It carries no load-bearing structure; the binding to the blob lives in
its frontmatter (`blob:`, §5), not in its filename. Renaming the `.md` is safe.

---

## 4. Blob (tar.gz) internal layout

```
2026-07-07-090000-0-3600-3f9a2c-blob.tar.gz
└── (tar root)
    ├── record.ir.json          # the extended Canonical IR — SOURCE OF TRUTH
    ├── session.md              # packed COPY of the loose .md at creation (self-describing)
    ├── audio/                  # compressed, speaker-isolated clips (active packs only)
    │   ├── S1.opus
    │   ├── S2.opus
    │   └── You.opus
    └── renders/                # OPTIONAL cached renders (regenerable; never authoritative)
        └── transcript.srt
```

- **`record.ir.json`** — the extended Canonical IR ([ADR-0006](../architecture/0006-canonical-ir-contract.md),
  `source.kind = "session"`). The **only** source of truth. Turns reference opaque speaker UIDs;
  names resolve at render time; per-turn confidence and per-segment embedding references live here.
- **`session.md`** — a copy of the loose sidecar taken **at pack creation**, so the blob is
  self-describing. See §6.
- **`audio/`** — compressed speaker-isolated clips, named by speaker id/UID, opus-class by default
  (codec swappable — the record references clips by role/UID + offset, not by codec). Present only
  while the pack is **active**; removed on finalize (§7). Clips double as
  [ADR-0024](../architecture/0024-live-speaker-identification.md) audition/adjudication exemplars,
  joined by UID.
- **`renders/`** — optional cached views (`.srt`, grouped `.md`, …). Pure functions of
  `record.ir.json`; may be regenerated or dropped at will; never authoritative.

**Metadata lives in the `.md` frontmatter, not in a separate manifest.** The blob carries its
metadata by carrying `session.md`. There is deliberately no `manifest.json` — the OKF
reference-as-graph principle keeps the human-readable document as the metadata surface.

---

## 5. The `.md` frontmatter schema

YAML frontmatter, minimally opinionated. **Required core** (a version bump is required to change
it):

| Key             | Type   | Meaning                                                            |
|-----------------|--------|-------------------------------------------------------------------|
| `spec_version`  | string | Session-pack spec version, e.g. `"0.1"`.                          |
| `id`            | string | The pack `<uid>` (matches the blob name and `record.ir.json`).    |
| `type`          | string | Always `session_pack`.                                            |
| `timestamp`     | string | ISO-8601 UTC session start.                                       |

**Optional** (producers add freely; consumers tolerate absence):

| Key            | Type        | Meaning                                                                        |
|----------------|-------------|--------------------------------------------------------------------------------|
| `title`        | string      | Human title (also drives the friendly filename).                               |
| `start`        | integer     | Start offset in seconds (matches blob `<start>`).                              |
| `length` / `duration` | integer | Session duration in seconds (matches blob `<length>`).                     |
| `state`        | string      | `active` or `finalized` (§7).                                                   |
| `participants` | list        | Display names / UIDs present (a convenience mirror of the record's speakers).  |
| `tags`         | list        | Purpose tags — `meeting`, `transcription`, `training`, `enrollment`, …. Mirrors the IR `tags[]`. |
| `quality`      | map         | Quality/discrimination metrics (SNR, min-speech coverage, embedding separation…). |
| `blob`         | string      | **The `resource:` reference** — the blob filename this `.md` describes.         |
| `voiceprints`  | list        | Reference-graph links to `library/` voiceprint records present in this session (§8). |

### Example

```yaml
---
spec_version: "0.1"
id: 3f9a2c
type: session_pack
timestamp: 2026-07-07T09:00:00Z
title: Team standup
start: 0
length: 3600
state: active
tags: [meeting, transcription]
participants: [You, Priya, Jonathan]
quality:
  min_speech_coverage: 0.82
  embedding_separation: 0.41
blob: 2026-07-07-090000-0-3600-3f9a2c-blob.tar.gz
voiceprints:
  - ../library/3f9a2c-priya.md
  - ../library/9c1d77-jonathan.md
---

# Team standup — 2026-07-07

[09:00:02] You: Morning, let's get started.
[09:00:05] Priya: ...
```

An **enrollment** pack differs only in contents and tags:

```yaml
---
spec_version: "0.1"
id: 7b1e04
type: session_pack
timestamp: 2026-07-07T14:12:05Z
title: Priya enrollment
tags: [enrollment, training]
participants: [Priya]
blob: 2026-07-07-141205-0-42-7b1e04-blob.tar.gz
voiceprints:
  - ../library/3f9a2c-priya.md
---
```

---

## 6. Self-describing rule

The blob **always** contains `session.md`, a copy of the loose sidecar taken at creation. If the
loose `.md` is lost, moved, or deleted ("I deleted my `.md` files"), the embedded `session.md` is
**authoritative** and the loose sidecar can be regenerated from it. A pack is therefore complete on
its own: `record.ir.json` (truth) + `session.md` (metadata + a human view) + `audio/` (substrate)
need nothing outside the archive to be understood or re-rendered.

If the loose `.md` and the embedded `session.md` disagree (e.g. a title edit not yet re-packed),
the **embedded copy defines the pack's identity/metadata**; the loose file is treated as an
un-committed edit. Re-packing refreshes the embedded copy.

---

## 7. Lifecycle states

| State       | Audio present | Regeneration available            |
|-------------|---------------|-----------------------------------|
| **active**  | yes (`audio/`)| re-render **and** re-process       |
| **finalized**| no           | re-render **only**                 |

- **Re-render** — pure function of `record.ir.json` → any transcript style. Always available, whole
  life of the pack.
- **Re-process** — re-run ASR / diarization / attribution over `audio/` to regenerate
  `record.ir.json` itself. Available only while the pack is **active**.

**Finalize** strips `audio/` from the blob, keeping `record.ir.json` and the extracted voiceprints
(the compounding asset). Stripping audio — by explicit **finalize**, a retention **TTL**, or
**delete** — is a **crypto-erase**: shred the clips' per-record data key
([ADR-0023](../architecture/0023-voiceprint-lifecycle.md)), not merely a file/index delete. The
active-state TTL and opt-in posture are policy that co-finalizes with
[ADR-0013](../architecture/0013-retention-and-consent.md); this spec defines the *mechanism*.

`state` in the `.md` frontmatter reflects the current state; the presence/absence of `audio/` in
the blob is the ground truth.

---

## 8. Extraction interface and the voiceprint record

### 8.1 Extraction (conceptual signature)

Extraction is **one operation over any pack**, independent of whether the pack is a conversation or
an enrollment:

```
extract(pack) -> list[VoiceprintUpdate]

  # for each speaker UID in the pack:
  #   pack.audio[uid]  ->  embedding(s)
  #   ->  fold into library[uid]  (running-mean centroid + samples, matches library.py)
  #   ->  record back-reference:  library[uid].sources += pack.blob
```

Any pack can improve a voiceprint incidentally; an **enrollment pack** (single speaker, clean,
`tags: [enrollment, training]`) is purpose-built *ideal* training data feeding the identical
interface. Because purpose is a tag, the store can batch-re-extract by class — e.g. "re-extract
from every `meeting` pack with the new embedding model."

### 8.2 Voiceprint record format

A voiceprint is **its own OKF document** under `library/`: markdown + frontmatter, with
back-references to the session blobs its embeddings came from. (The embedding vector itself may live
inline or in a sibling binary, sealed per ADR-0023; back-references are the graph edges.)

```yaml
---
spec_version: "0.1"
uid: 3f9a2c-priya
type: voiceprint
name: Priya
samples: 47                      # embeddings folded in (weights the mean; signals confidence)
updated: 2026-07-07T14:12:47Z
sources:                         # back-references into the session graph (blobs this was extracted from)
  - ../sessions/2026-07-07-090000-0-3600-3f9a2c-blob.tar.gz
  - ../sessions/2026-07-07-141205-0-42-7b1e04-blob.tar.gz
---

# Priya

Recurring participant. First enrolled 2026-07-07.
```

---

## 9. Reference-graph conventions

Sessions and the library form **one browsable graph** of small text files pointing at binary blobs
(OKF reference-as-graph). Edges are relative paths in frontmatter:

- **session `.md` → blob**: `blob:` (the pack's own archive).
- **session `.md` → voiceprints**: `voiceprints: [ ... ]` (people present in the session).
- **voiceprint `.md` → session blobs**: `sources: [ ... ]` (where its embeddings came from).
- **session `.md` → related sessions** (optional): `related: [ ... ]` (e.g. slices of one epoch,
  or a follow-up meeting).

Conventions:

- Links are **relative paths** from the file's own location, so a `sessions/` + `library/` subtree
  is relocatable as a whole.
- The graph is **bidirectional by convention**: a `voiceprints:` edge on a session should have a
  matching `sources:` edge on the voiceprint. Tools may reconcile the two directions; neither alone
  is required to be complete.
- Walking the graph requires only a YAML parser and the filesystem — no index server.

---

## 10. Not in v0.1

Explicitly out of scope for this version, to keep the contract bounded:

- **Encryption-at-rest wiring.** The crypto-erase *semantics* are specified (§7); the actual
  per-record data-key envelope and key store ([ADR-0023](../architecture/0023-voiceprint-lifecycle.md))
  are not wired here.
- **Interactive teaching / adjudication UX** ([ADR-0029](../architecture/0029-teaching-mode-ux.md),
  future) — this format *enables* it (speaker-isolated clips + UID graph) but does not define it.
- **End-of-session reconciliation questionnaire** ([ADR-0030](../architecture/0030-reconciliation-questionnaire.md),
  future) — a `quality`/`reconciliation` frontmatter slot is reserved, its content is not specified.
- **Corpus-priming of live sessions** from prior packs — an implementation plus an ADR-0024/0027
  amendment, not this spec.
- **Cross-store replication/merge semantics** ([ADR-0026](../architecture/0026-shared-speaker-identity-store.md)) —
  the pack is named as the replication unit; the merge protocol is elsewhere.
