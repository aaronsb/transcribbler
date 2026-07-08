# ADR-0024: Live speaker identification — online voiceprint matching with human-adjudicated identity

- **Status**: Proposed
- **Date**: 2026-07-06
- **Updated**: 2026-07-08 — concrete live adjudication surface, re-renderable session model, and recompute-on-identify specified (still Proposed)
- **Deciders**: Aaron

## Context

[ADR-0023](0023-voiceprint-lifecycle.md) fixed the voiceprint **store, enrollment, and
matching** contract for *batch* transcription: a file goes in, the matcher names speakers
against a per-principal store while building the IR. It deliberately left two things open
that only appear once transcription runs **continuously against a live conversation**:

1. **Identity across time.** Diarization gives *per-run* local labels (`SPEAKER_00`…). In a
   live capture split into chunks, those labels reset every chunk — `S1` in one chunk is not
   the same person as `S1` in the next. There is no session-stable, let alone
   cross-session-stable, notion of "who."
2. **Identity resolution under error.** Online clustering — matching voices as they arrive,
   with no global view of the whole recording — is inherently less accurate than offline
   clustering. It *will* split one person into two, or merge two into one. The batch contract
   has no answer for correcting that.

Forces specific to this decision:

- **The population is a warm open set.** The operator works with a slowly-moving pool: a
  stable core of recurring people, with newcomers rolling in and others leaving for good and
  never returning. The core frequently co-occurs with the churning edges. This is neither a
  closed roster (a classifier would misattribute every newcomer to the nearest known voice)
  nor a fresh crowd each time (enrollment and naming should amortize, not restart).
- **Enrollment and naming amortize; refinement compounds.** Because most people recur over
  months, the cost of first-encounter identification and naming is paid once per person, then
  reused. Each additional session sharpens a person's voiceprint rather than starting over.
- **The operator is a free, deterministic anchor.** Live capture separates the operator's mic
  from the meeting output as two channels; the mic is empirically bleed-free (headset /
  echo-cancelled source), so the operator is identified *by channel*, needs no clustering, and
  supplies the cleanest enrollment sample in the system.
- **Online matching must run in a warm process.** The per-chunk diarizer sidecar reloads its
  model on every invocation — measured at ~9–13 s per chunk, almost all of it model load, not
  inference. A live matcher that also maintains a running gallery cannot pay that per chunk.
- **This audio is biometric data about third parties.** Retaining listenable voice clips of
  colleagues over months is materially more sensitive than retaining embeddings, and those
  people did not opt into the operator's gallery ([ADR-0013](0013-retention-and-consent.md),
  [ADR-0010](0010-operator-awareness-and-control.md)).

## Decision

Adopt the **Google Photos face-grouping pattern, applied to voices**: trust unsupervised
clustering only provisionally, and make it reliable through cheap, **non-destructive human
adjudication** over an open, evolving gallery. The machine proposes identities; the operator
corrects them with evidence; corrections feed back as supervision.

### Identity is an opaque UID; the name is a mutable label

Every speaker is a stable, opaque **UID**. Turns, exemplars, and gallery entries reference the
UID and never the name. A human-facing **name** is mutable metadata resolved at render time. A
speaker may remain unnamed indefinitely while still being a consistent entity across months.

This split is the linchpin: because names are labels, **naming, renaming, merging, and
splitting never rewrite turn references** — every identity operation is reversible and cheap.

### Online matching — session gallery seeded from the persistent gallery, with open-set rejection

At session start, load the persistent per-principal gallery ([ADR-0023](0023-voiceprint-lifecycle.md))
into memory as the **session gallery**. For each incoming speech segment, extract an embedding
and match it against the session gallery by similarity:

- **Above the match threshold** → attribute to that UID.
- **Below the open-set floor** → this is a *new* voice; mint a new UID. The set is never
  treated as closed — an unrecognized voice is always allowed to be someone new, not forced
  onto the nearest known person.

A **minimum-speech gate** must pass before an embedding is trusted (short/boundary-cut
segments produce unreliable embeddings — the temporal analogue of a blurry face).

### Refinement — running centroids plus retained exemplars, compounding across sessions

Each segment attributed to a UID folds into that UID's representation (a running/weighted
centroid, optionally multiple exemplars to tolerate drift, illness, or a different mic). At
session end, refined representations write back to the persistent gallery, so accuracy
**compounds** across meetings rather than resetting.

### Evidence store — per-segment embeddings + short audio exemplars + timestamps

The gallery retains, per UID, not just a collapsed centroid but the **per-segment embeddings
and a few short audio exemplars, each stamped with date/time and session**. This exists for
two reasons: the exemplars let the operator *listen and identify* who a UID really is by
cross-referencing their own meeting record; and the retained per-segment data is what makes
**split** possible (a centroid alone cannot be re-clustered).

### Locality — the clip stays local, the embedding travels

The two artifacts have different sensitivity, so they get different locality, joined by the UID:

- **Audio exemplars are local-only.** A clip *is* the person's voice; it is the most sensitive
  artifact in the system and never crosses a network boundary. It lives on the operator's
  machine, in a clip store keyed by UID (`UID → clips + timestamps`), encrypted at rest
  ([ADR-0023](0023-voiceprint-lifecycle.md)) and TTL'd. The "listen and identify" and
  merge/split adjudication run against this local store.
- **Embeddings travel with the matcher.** The embedding is a vector that does not reconstruct
  audio; it lives wherever matching runs (`UID → embedding/centroid`), which may be a remote
  compute backend.
- **The UID is the only thing that must be shared** between the two, plus the `UID → name`
  label the operator assigns. Raw voice therefore stays put even when compute is remote.

The precise trust boundary this rides on (which processes are local vs remote, and what may
cross) is broader than speaker identity and is called out as a follow-up below.

### Merge and split are first-class, non-destructive operations

- **Merge** (`uid_a`, `uid_b` are the same person): choose a surviving UID, union exemplars and
  embeddings, recombine the representation, and repoint references. Cheap.
- **Split** (`uid` is actually two people): re-cluster that UID's retained embeddings/exemplars
  into two, mint UIDs, repoint the affected turns. Requires the evidence store above.

Both are ordinary operations on the gallery, invoked by the operator (or suggested by the
system, "are these the same voice?"), and neither destroys history.

### Lifecycle — enroll → active → dormant → retired

A UID moves through states: **enrolled** (crossed the novelty floor), **active** (refined each
session it appears), **dormant** (unheard for a configured interval — dropped from the active
match set to suppress false matches to departed people), and **retired** — crypto-erased by
shredding its data key ([ADR-0023](0023-voiceprint-lifecycle.md)), satisfying the retention and
consent obligation ([ADR-0013](0013-retention-and-consent.md)) when someone leaves for good.

### Naming — operator adjudication, LLM-from-context, and meeting-metadata hints

Names attach to UIDs from three sources, in precedence with the enrolled tier of
[ADR-0016](0016-speaker-naming-strategy.md): explicit operator adjudication (authoritative);
LLM inference from conversational context ("thanks, Priya" binds the addressed speaker); and
the **meeting's own attendee list** (the date/time stamp lets a UID set be cross-referenced
against who was scheduled). A known core member naming an unknown newcomer is a strong binding
cue even before that newcomer's voiceprint matures.

### The persistent daemon owns the warm model and the session gallery

Online matching runs in a **persistent, sessionized process** that loads the embedding/diarization
model once and holds the session gallery in memory across chunks, emitting
`{stable_uid, embedding}` per segment. This folds diarization into the sessionized live-ingest
service ([ADR-0022](0022-live-audio-ingest.md)) rather than shelling a cold sidecar per chunk.

### Live view is provisional; the authoritative transcript is reconciled offline

The online attribution is the *live* view and is explicitly provisional. At session end, a
single **offline re-diarization** over the full audio produces the authoritative speaker
assignment, and its result is **reconciled** against the online UIDs (the online→offline label
map). The operator's merge/split/name decisions apply to the reconciled result.

## Live adjudication surface (the concrete in-meeting mechanism)

The Decision above fixes the *model* (opaque UIDs, human adjudication, evidence store, warm
daemon). This section fixes the concrete surface the operator actually uses **while
participating in the meeting** — the specification that makes "the machine proposes, the
operator corrects" a real, low-friction interaction rather than a principle.

### The operator is human-*on*-the-loop, not in-the-loop

The machine auto-matches and auto-consolidates every window on its own; the operator glances at
the running transcript and **intervenes only when they spot an error**. No decision is gated on
the human — attribution never blocks waiting for input. This is the difference between watching
a meeting and running a labelling tool, and it is the load-bearing UX constraint.

### A re-renderable session model — turns reference UIDs, labels and confidence resolve at render

The live transcript is **not** append-only text. Each turn stores `(uid, start, end, text)`; the
displayed **name** and **match confidence** are resolved from the gallery at render time. Any
identity operation (name / join / merge / split) is therefore an edit to `uid → name` and the
`uid → uid` consolidation map, followed by a **re-render of the whole session-so-far** — turn
references never change (the linchpin above). The recompute is trivial by construction: a UID
carries a running centroid, so re-scoring the handful of session UIDs against a new anchor is a
few dozen dot products — microseconds. The transcript **file** is rewritten in full on every op
(always consistent); the **view** redraws from the same model.

### The interaction — select rows, act on the selection

A full-screen TUI (`curses`, matching the backend's no-heavy-deps posture) presents the live
transcript as navigable rows while new turns stream in below:

- **Navigate + select.** Arrow over rows; `space` toggles an inverse-highlight selection. New
  turns keep appending during selection; selection is keyed by turn UID, so it survives the
  re-renders those appends cause. Selecting whole-speaker (all rows of one `S[n]`) is a shortcut
  over the common case; per-row selection is what makes **split** precise.
- **Act.** `Enter` on a selection opens a dialog:
  - **Enroll New Print** (the speakers are unidentified) → name prompt → save → back to the
    transcript.
  - **Join To Print** → a list of existing voiceprints, with **Enroll New Print** pinned
    distinctively at the bottom as a second path in.
  - `Esc` pops one level up the dialog stack; it never commits.
- The selected **clean-prose rows are the enrollment exemplars** — the operator points at the
  utterances that best represent the voice, realizing the evidence-store model literally.

### Recompute-on-identify — naming one speaker re-scores all the others

The moment a UID is named or joined, it becomes an **anchor**, and the gallery re-scores every
*other* still-unidentified UID against it. This is what makes a live session self-correct: if one
voice was fragmented into `S2`, `S4`, `S6`, `S10`, naming `S2` pulls the others in.

- **Above a consolidation ceiling** → auto-merge into the anchor (the machine is confident).
- **Below it** → left displayed with their **updated confidence**, as a *proposal* — never
  silently merged. HOTL forbids the machine merging a maybe.

A **confidence column** in the transcript is therefore the operator's triage cue: low-confidence
rows are exactly the ones worth selecting and running through the same enroll/join, which
recomputes with the new composite score. One tunable threshold separates "auto-consolidate" from
"propose"; it is corpus/mic-dependent (see the threshold cost below).

### In-situ enrollment is a live pack relabel + extract

Enrolling from selected rows is the same operation as ADR-0028/0029's post-session
`relabel` + `extract`, run *during* the session: the speaker's centroid folds into a named
voiceprint in the durable library, and the session pack ([ADR-0028](0028-session-resource-pack-and-regeneration.md))
is its exemplar/audio substrate. The pack-lifecycle verbs ([ADR-0029](0029-pack-lifecycle-and-teaching.md))
are the offline half of the same surface; this is the online half.

### Matching is inline; only the model is warm

The warm daemon (decided above) exists to amortize *model load*, not to parallelize matching.
The per-segment cosine match and the recompute-on-identify are cheap and run **inline** in the
capture loop; the operator's actions run on the existing key-input thread. No separate threads
are introduced for identity work — that would add shared-gallery races for no latency gain. If
per-window wall-time ever becomes the constraint, the real lever is parallelizing ASR against
diarization within a window (they are independent), not threading the matcher.

### Operator live decisions survive the offline reconciliation

The offline re-diarization pass (below) improves diarization *quality*, but the operator's live
name/join/merge/split decisions are **authoritative supervision**: they are inputs the
reconciliation must honour, not proposals it may overwrite. Reconciliation may re-attribute a
turn's diarized boundary; it may not un-name a UID the operator named.

## Consequences

### Good

- Cross-chunk and cross-session identity: the gap ADR-0023's batch contract left open is closed.
- Errors are cheap to fix and never destructive — the UID/label split makes every identity
  operation reversible.
- Accuracy improves over time with zero extra modeling: refinement compounds and human
  corrections are supervision.
- **The operator-effort curve declines across sessions.** Because the session gallery is seeded
  from the persistent library at start, every session inherits the combined matching work of all
  prior sessions: known voices auto-anchor on arrival and need no adjudication, so the operator
  only ever names genuine newcomers. After a handful of meetings the recurring core is covered
  and the dataset is self-maintaining — a usable diarization corpus that needs little caretaking,
  produced as a byproduct of normal meeting participation rather than a labelling project.
- The operator anchor removes the hardest speaker from the clustering problem entirely.
- A well-understood, battle-tested UX pattern (Photos face grouping) — low conceptual risk.

### Bad / costs

- Storing listenable third-party voice audio raises the consent/retention bar; the lifecycle,
  encryption, and expunge obligations are now load-bearing, not hypothetical.
- The evidence store (per-segment embeddings + exemplars) costs storage and sensitivity that a
  centroid-only store would not — the price of supporting split.
- Threshold tuning (match, open-set floor, dormancy TTL) is corpus- and mic-dependent and has
  no universal constant; getting it wrong yields false-new or false-match errors.
- A persistent daemon adds process-lifecycle and memory-residency concerns (gallery in RAM).
- The live adjudication TUI is a real build: a full-screen `curses` view with tail-follow, a
  UID-keyed selection model that survives re-renders, and a modal dialog stack. It replaces the
  interim scrolling console and is the most UX-heavy component in the backend to date.
- The whole pipeline is tuned for *speech*: ASR and the speaker-embedding model both degrade on
  full-on music / singing. Sung content transcribes poorly (mostly `[Music]` markers, garbled
  lyrics) and yields unreliable voiceprints that break stitching in *both* directions —
  observed merging distinct voices into one and over-splitting one into several. Accuracy on
  musical audio should not be expected; it is out of the target domain (meetings/conversation).

### Neutral

- Online quality remains below offline; the design accepts this and compensates with the
  offline reconciliation pass and human adjudication rather than chasing perfect online clustering.
- The gallery becomes a long-lived, self-maintaining store with its own operations surface,
  not a static enrollment file.

## Alternatives considered

- **Closed-set classifier over a fixed roster.** Rejected: the population churns; a closed set
  misattributes every newcomer to the nearest known voice and has no path to enroll new people.
- **Pure offline batch identification (no live view).** Rejected: the live use case needs a
  running transcript during the meeting, not only at the end.
- **Trust online clustering without human adjudication.** Rejected: online clustering errs by
  construction; without cheap correction the transcript's identities stay wrong.
- **Centroid-only store (no retained exemplars/embeddings).** Rejected: cannot re-cluster, so
  **split** is impossible and the operator cannot listen to verify identity.
- **Per-chunk cold sidecar (status quo).** Rejected on measurement: ~9–13 s/chunk of model
  reload, and no place to hold a running gallery; a warm daemon is required.

## Open questions / follow-up

- **The local/remote trust boundary is under-specified system-wide.** This ADR pins the
  locality of *its own* artifacts (clip local, embedding with the matcher), but the general
  question — which processes are local vs remote, whether "server-side" means localhost or a
  networked box, and what is allowed to cross — spans capture, compute, the voiceprint store
  ([ADR-0023](0023-voiceprint-lifecycle.md) placed the store + matcher "server-side"), the
  clients/service split ([ADR-0017](0017-rust-clients-python-service.md)), and the cloud worker
  ([ADR-0021](0021-internet-cloud-worker.md)). It deserves its own ADR establishing a data-locality
  / deployment-topology model that this ADR's clip-local rule slots into.

## Related

- [ADR-0023](0023-voiceprint-lifecycle.md) — voiceprint store, enrollment, matching (batch); this ADR extends it to the online, cross-session, human-adjudicated case
- [ADR-0028](0028-session-resource-pack-and-regeneration.md) — the session pack is the exemplar/audio substrate the live gallery writes back to
- [ADR-0029](0029-pack-lifecycle-and-teaching.md) — the offline pack-lifecycle verbs (relabel/extract/audition) that in-situ enrollment is the live half of
- [ADR-0016](0016-speaker-naming-strategy.md) — speaker naming tiers and precedence
- [ADR-0022](0022-live-audio-ingest.md) — sessionized live audio ingest; the daemon lives here
- [ADR-0013](0013-retention-and-consent.md) — retention and consent for biometric data
- [ADR-0010](0010-operator-awareness-and-control.md) — operator awareness and control
- [ADR-0018](0018-client-facing-wire.md) — versioned `/v1` wire for the gallery/adjudication operations
