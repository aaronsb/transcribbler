# ADR-0027: Robust online speaker attribution for live capture — overlapping-window diarization + turn-aware attribution

- **Status**: Proposed
- **Date**: 2026-07-07
- **Deciders**: Aaron

## Context

[ADR-0024](0024-live-speaker-identification.md) set the identity *model* (opaque UID,
session gallery, refinement, human adjudication). The live-capture MVP spike
(`backend/transcribbler/capture.py` — explicitly a spike toward
[ADR-0022](0022-live-audio-ingest.md)'s sessionized service, not that service) is the first
place that model runs against real audio. The **capture and transcription** layers work well.
The **identity** layer does not, and the first real meeting (2026-07-07, ~6–7 people) surfaced
**two distinct failure modes** that the ADR-0024 model named in the abstract but did not
resolve mechanically.

**Failure A — cross-chunk churn.** The spike diarizes each 20 s chunk *in isolation*, so a
speaker with little airtime in a chunk yields a thin, noisy embedding. `SessionGallery`
(`session_gallery.py`) then matches that embedding by cosine against running centroids
(threshold 0.5, greedy one-to-one within a chunk) — and with a bad embedding it both
**over-splits** (mints a new id for a returning speaker) and **merges** (collides two people
onto one id). Evidence: the meeting minted `S1,S2,S4,S6,S8,S9` with gaps — `S3/S5/S7` were
minted then vanished, the signature of churn — and the operator (auditory ground truth)
reported "both — it's just unstable."

**Failure B — intra-segment speaker bleed.** A single Whisper ASR segment can span a speaker
change, and max-overlap attribution (`_overlap` in `capture.py`) buckets the *whole* segment
under one label. Concrete evidence at 06:46: one segment —
*"something. But I know, I know they were fixed. Okay. So mid day and then we'll see if B. Rob
can"* — is Jonathan (`S1`) for the first half and Val (`S6`) from *"Okay. So mid day…"* on.
Root cause: ASR segment granularity is coarser than diarization turn granularity, and segments
are not cut at speaker-change boundaries.

Two constraints shape the fix. `Segment` is **segment-level only** — `(start, end, text)`, no
word timestamps ([ADR-0015](0015-pluggable-compute-backends.md), `cores/base.py`). And the
warm diarizer daemon (`diarizer_daemon.py`, pyannote community-1) runs at **~0.6–2.2 s per 20 s
chunk** — a wide margin under a 20 s realtime budget, so there is compute headroom to spend.

## Decision

Improve **in-session** attribution robustness in the spike along two independent axes, and
draw a firm scope line against persistence. This is a **draft / debate** artifact: each
decision below lays out the forks and lands a recommendation, for review before commit.

### Decision 1 (fixes A) — overlapping-window diarization with temporal cross-window linking

- **1a — status quo** (isolated 20 s chunks + embedding-only gallery). *Reject.* It is the root
  cause of A.
- **1b — longer chunks (30–45 s).** Cheap (config only), gives fatter embeddings. But it
  worsens latency and does **not** solve label-linking across windows — the same over-split /
  merge failure recurs at the new window boundaries. *Reject as insufficient.*
- **1c — overlapping-window online diarization (RECOMMEND).** Diarize *overlapping* windows
  (e.g. 40 s window, 20 s hop, so each pair of adjacent 20 s chunks is diarized together) and
  emit only the **new** region. Link window *N*'s local labels to window *N−1*'s by **temporal
  overlap in the shared 20 s span** — the same physical audio is labeled by both windows, so
  align by *time*, which is far more robust than embedding cosine. Use embeddings only as a
  **fallback** for a speaker who is absent from the shared span. Cost is ~2× diarizer compute
  (≈1.5–4 s per hop, still comfortably inside the 20 s budget). This also hands the ASR **free
  left-context** on every window, which **subsumes** the separately-planned "ASR seam-clipping"
  fix.

**Recommendation: 1c.** Temporal linking attacks A at its mechanism — labels are linked by
when they occur, not by fragile embeddings — while embeddings degrade to a fallback rather than
the primary key. The cost is affordable given the measured daemon headroom.

### Decision 2 (fixes B) — attribution granularity

- **2a — proportional time-split.** Cut a segment's text at the turn boundary by time fraction.
  No new plumbing; crude (splits mid-word by character ratio).
- **2b — word-level timestamps.** Extend the whisper.cpp core to emit word times and add them
  to `Segment`. Precise splits — but **reopens [ADR-0015](0015-pluggable-compute-backends.md)**'s
  minimal-interface decision, since it grows the core contract.
- **2c — diarize-first, then ASR each speaker-homogeneous turn separately.** Single-speaker by
  construction, so B cannot occur — but it reshapes the loop, risks hallucination on very short
  (<1 s) turns, and loses cross-turn ASR context. Most invasive.

**Recommendation: adopt 1c first, then choose 2a/2b/2c as a follow-on.** 1c already improves
turn boundaries and, via emit-only-new-region alignment, reduces B on its own; measure the
residual before paying for 2b (which reopens ADR-0015) or 2c (the most invasive reshape). 2a is
the cheap interim if residual B is still visible after 1c.

### Decision 3 — relationship to persistence (scope boundary)

In-session robustness (this ADR) is **separable** from the persistent / shared identity store —
[ADR-0024](0024-live-speaker-identification.md)'s persistent-gallery phase,
[ADR-0026](0026-shared-speaker-identity-store.md), and the deferred "UID confidence +
merge/bundle" work. They **share** the gallery/matching machinery, and a persistent,
confidence-weighted gallery would later make A more robust still — but **this ADR scopes
in-session consistency only.** Persistence is explicitly out of scope here, so the fix does not
grow into a store redesign.

## Consequences

### Good

- Attacks A at its mechanism: cross-window labels link by time, not by thin embeddings, with
  embeddings demoted to a fallback — directly targeting the over-split/merge churn observed.
- 1c gives the ASR free left-context and **subsumes** the standalone ASR seam-clipping fix —
  one change, two problems.
- Clear scope line: in-session robustness lands without waiting on or entangling the persistent
  store (ADR-0024 phase, ADR-0026).

### Bad / costs

- ~2× diarizer compute (≈1.5–4 s per 20 s hop vs a 20 s realtime budget) — affordable given the
  measured warm-daemon headroom, but it narrows the margin.
- New **emit-only-new-region** dedup logic: overlapping windows must not double-emit the shared
  span, adding correctness surface to the loop.
- If 2b is later chosen, it **reopens [ADR-0015](0015-pluggable-compute-backends.md)**'s
  minimal-core-interface decision (word timestamps on `Segment`).
- 2c, if chosen, reshapes the capture loop and risks hallucination on sub-second turns.

### Neutral

- The spike (`capture.py`) remains a spike; this ADR governs its **diarization/attribution
  approach**, which feeds the eventual [ADR-0022](0022-live-audio-ingest.md) sessionized service
  rather than being that service.
- Residual B after 1c is a measurement, not a foregone conclusion — the 2a/2b/2c choice is
  deferred to that measurement.

## Validation

The meeting cannot be replayed — chunks are unlinked and unlinkable after processing. Validate
against a **controlled recording** instead: a 2-speaker podcast played into Chrome, where ground
truth is known and turn boundaries are unambiguous, exercising both cross-window linking (A) and
intra-segment bleed (B) deterministically.

## Alternatives considered

- **1a status quo** and **1b longer chunks** — rejected above (root cause; insufficient).
- **Fix A purely by raising the gallery threshold / better embedding smoothing.** Rejected as
  primary: it treats the symptom (bad cosine matches) rather than the cause (per-chunk isolated
  diarization) and trades over-split for merge without removing either.
- **Do B first (2b/2c) before A.** Rejected: A is the larger observed instability, and 1c
  reduces B as a side effect — so ordering 1c first is strictly cheaper.
- **Fold this into the persistent-gallery work now.** Rejected: couples an in-session fix to a
  larger store redesign; Decision 3 keeps them separable.

## Related

- [ADR-0024](0024-live-speaker-identification.md) — parent; the identity model and session-gallery stitching this ADR refines
- [ADR-0022](0022-live-audio-ingest.md) — the sessionized service this spike's approach feeds
- [ADR-0015](0015-pluggable-compute-backends.md) — the minimal core interface that option 2b would reopen
- [ADR-0026](0026-shared-speaker-identity-store.md) — the persistent/shared store this ADR scopes *out*
- [ADR-0023](0023-voiceprint-lifecycle.md) — the voiceprint store the persistent phase builds on
- [ADR-0009](0009-capture-cadence.md) — capture cadence / chunking the windowing sits on
