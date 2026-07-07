# How voiceprints work (embeddings, centroids, and matching)

This is a *how it works* explainer, not a decision (for the storage decision see
[ADR-0028](../architecture/0028-session-resource-pack-and-regeneration.md); for the on-disk shape
see the [session-pack spec](../specs/session-pack.md)). It exists so the next reader understands
*why* the identity layer behaves the way it does — especially why the same person sometimes gets
two ids, and why a durable library fixes it.

## A voiceprint is a coordinate, not a recording

The diarizer runs three different models; only one makes the fingerprint:

- **Whisper (STT)** — audio → *words* (what was said).
- **pyannote diarization** — audio → *turns* (when each distinct voice is active).
- **the speaker-embedding model** (inside pyannote) — a short audio segment → a **256-dimensional
  vector**. This is a **speaker-verification** model: its entire training objective is "same voice
  → nearby vector, different voice → distant vector," *regardless of the words spoken*. It is not
  STT and not TTS — a third species.

If you already have a mental model for **text embeddings** ("this string becomes a point in a
coordinate space; I compare two points with cosine similarity and get a coefficient"), it transfers
almost exactly. The only differences: the input is audio instead of text, and the space encodes
*voice identity* instead of *meaning*.

Matching is the same cosine test as text embeddings. Calibrated on real audio: **same speaker
lands ~0.8–0.95, different speakers ~0.07.** That enormous gap is what makes the whole thing work.

## The catch: a speaker is a *cloud*, not a point

Text embeddings are **deterministic** — the same string always produces the exact same vector. Audio
is not. A single person's voice produces a **cloud** of vectors that wobbles with:

- **audio quality / channel** (a clean headset vs a car speakerphone) — the big one
- clip length (a short clip is a noisier estimate)
- background noise, codec, microphone
- vocal state (excitement, character voices, whispering)

So "Aaron" is not a point in the 256-d space — he's a fuzzy **region**. Two recordings of the same
person land in *different parts of that region*, and if they're far enough apart, a naive match can
fall below threshold and mint a *second* id for one person. (That is exactly the S1/S10 split we saw
live: a caller's voiceprint shifted when his audio quality changed mid-call — driving vs parked —
and the two conditions landed in different parts of his cloud.)

## The centroid: a running estimate of the cloud's center

A voiceprint stores a **centroid** — the running mean of every embedding folded into it — plus a
`samples` count. Adding a sample nudges the centroid toward the true center of the cloud.

Two things follow, and they're worth internalizing:

1. **New same-condition samples score higher, then plateau.** With one sample the "centroid" is just
   that one noisy point, so a second take measures sample-to-sample distance. With more samples the
   centroid becomes a *less noisy, more central* estimate, so a fresh take lands closer to it. In
   practice we watched a re-read climb 0.931 → 0.954 as the centroid settled.
2. **It never reaches 1.0, and that's healthy.** The ceiling is set by your **intrinsic take-to-take
   variance** — you never say it exactly the same way twice. The residual gap from 1.0 *is* your
   natural spread.

**Diminishing returns are real and fast.** The Nth sample shifts a running mean by only ~1/N, so by
roughly 5–10 same-condition samples the centroid is essentially frozen. More identical reads buy
almost nothing after that.

### Why it's not the model being "random"

The embedding model is **essentially deterministic**: same audio in → same vector out. Feed it the
*identical clip* twice and you get cosine ≈ 1.0 (there is a ~1e-6 wobble from floating-point/GPU
non-associativity, but that's a rounding whisper, not real variance). The non-determinism lives in
the **microphone**, not the math — you can never hand the model the same audio twice.

A useful consequence: **extraction is reproducible.** Re-embedding a retained clip always yields the
same vector, so when a clip's embedding *does* change on re-process, it's because the *model* got
better — a real, attributable improvement, not noise. That's what makes "re-process with a better
model" trustworthy.

## Count converges; only *diversity* widens coverage

This is the most important practical point. Reading the same passage into the same mic ten times
converges a **narrow** cloud — a great estimate of "you, calm, at your desk." It will match future
desk-reads at ~0.95 and might still match a bad phone connection poorly, because that's a *different
region* of your true cloud.

- **Count** → convergence *within a condition* (diminishing returns, fast).
- **Diversity** → coverage *across conditions* (the thing that actually prevents fragmentation).

So enrollment should court variety — normal, softer, farther from the mic, a noisier take — rather
than identical repetition. A few *diverse* samples beat many *identical* ones. `samples` is a
confidence signal, but "samples across conditions" is the signal that matters.

## Open design question: one mean vs many prototypes

A single running-mean centroid **averages away** condition variance. As a speaker's cloud widens
(more conditions), the mean drifts toward "okay everywhere, perfect nowhere." The alternative,
borrowed from face recognition (and the Google-Photos grouping pattern this project keeps citing),
is to keep **multiple prototypes per speaker** — a small set of exemplars covering the cloud — and
match against the *nearest* one.

That reframes **merge** (["are S1 and S10 the same person?"](../architecture/0024-live-speaker-identification.md)):
it isn't just cleanup, it's a choice about representation. Blending two condition-clusters into one
mean *loses* both; keeping them as two prototypes of one identity *preserves* the coverage. The
current implementation uses a single running-mean centroid (simple, works well within a condition);
whether a voiceprint should hold multiple prototypes is an open question the library design will
have to settle as cross-condition data accumulates.

## How this maps to the code

- **`session_gallery.py`** — the *session-only* gallery: centroids built from scratch each run and
  discarded at the end. Thin, single-session estimates → prone to the fragmentation above. This is
  why in-session churn happens and why [ADR-0027](../architecture/0027-robust-online-speaker-attribution.md)
  leans on *temporal* linking (shared-audio overlap) rather than embeddings where it can.
- **`library.py`** — the *durable* voiceprint: a centroid compounded across sessions, the counterpart
  that makes returning speakers recognizable across conditions. The compounding asset.
- **Enrollment** — the cleanest possible sample: a known single speaker reading aloud, one voiceprint
  built directly. Ideal *training* data, but the same `extract → fold into centroid` operation runs
  over any session (see the [spec](../specs/session-pack.md)).
