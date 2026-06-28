# ADR-0006: Canonical IR as the backend-agnostic pipeline contract

- **Status**: Accepted
- **Date**: 2026-06-28
- **Deciders**: Aaron

## Context

The old `whisper-service` "reassembled" chunks by concatenating text and offsetting
timestamps — no speaker handling, and a latent drift bug because the offset advanced by
the last segment's (chunk-relative) end rather than true chunk duration. We also need
speakers consistent across a whole recording and resolved to **names/roles**, and we have
two different backends ([ADR-0005](0005-diarization-flow.md)) that must produce
interchangeable output.

The operator's insight: a **canonicalization pass** should line everything up
deterministically — and a local LLM can map "Speaker N" to names/tags by emitting
**structured JSON** ("canonicalization data"), not by rewriting the transcript.

## Decision

Define a **Canonical IR** (intermediate representation) as the single contract every
backend produces and every renderer/store consumes. Replace "reassemble" with
"canonicalize → IR."

**Separate the two problems, and keep the LLM boxed as data:**

1. **Speaker identity across the recording** — solved deterministically (global
   diarization gives stable IDs; embedding-clustering fallback when chunked). *Not* an
   LLM job.
2. **Speaker → name/role/tag + term normalization** — an LLM reads compact evidence and
   **emits a small JSON mapping table**, never the transcript body. A **pure `apply()`
   function** then rewrites the IR from (clusters + mapping). Same inputs → same output.

The LLM runs greedy/temp-0 with a fixed seed and **GBNF grammar** ([ADR-0004](0004-llama-cpp-native-backend.md))
so output is always schema-valid. Its mapping is **cached, reviewable, and overridable**,
with a non-LLM fallback (plain "Speaker A/B/C") so the pipeline degrades gracefully.

### Canonical IR (sketch — to be finalized before coding)

```jsonc
{
  "schema_version": "0.1",
  "source": { "uri": "...", "sha256": "...", "duration_s": 1234.5, "captured_at": "..." },
  "backend": { "kind": "modular|joint", "asr": "whisper.cpp/large-v3", "diarizer": "pyannote/community-1" },
  "speakers": [
    { "id": "S1", "display_name": "Aaron", "role": "host",
      "confidence": 0.82, "evidence": ["self-intro @ 12.4s"], "source": "llm|manual|fallback" }
  ],
  "turns": [
    { "speaker_id": "S1", "start": 12.40, "end": 18.90, "text": "...",
      "words": [ { "t": 12.40, "d": 0.22, "w": "so" } ],
      "secondary_speakers": [],            // for overlapped speech
      "provenance": { "chunk": 3, "offset_s": 600.0 } }
  ],
  "glossary": [ { "canonical": "Praecipio", "variants": ["precipio", "precipeo"] } ],
  "tags": ["meeting", "sales-call"]
}
```

### LLM canonicalization-data output (GBNF-constrained)

```jsonc
{
  "speaker_map": [
    { "id": "S1", "display_name": "Aaron", "role": "host", "confidence": 0.82,
      "evidence": "introduces self at 12.4s" }
  ],
  "term_map": [ { "canonical": "Praecipio", "variants": ["precipio", "precipeo"] } ]
}
```

The LLM is fed only **compact evidence** (per speaker: the intro turn, longest turns,
turns containing proper nouns) plus the turn skeleton — fits an 8–32k local context.

## Consequences

### Good
- One contract; backends and renderers are mutually independent.
- Determinism where it counts: identity + `apply()` are reproducible and golden-file
  testable; the LLM can't hallucinate transcript content (it only emits a mapping).
- Timestamp drift is impossible by construction (provenance carries true offsets).
- Term normalization fixes Whisper's inconsistent spelling of names/jargon across chunks.

### Bad / costs
- An IR schema to design, version, and migrate.
- LLM determinism is "reproducible-in-practice" (temp-0 + seed + pinned quant), not a hard
  guarantee — hence caching + review + fallback.

## Alternatives considered

- **Patch the old `reassemble()`** — rejected; the size-driven, speaker-blind design is the
  wrong foundation.
- **Let the LLM rewrite the full transcript** — rejected; nondeterministic and can
  hallucinate dialogue.
- **Embedding clustering as the default for IDs** — used as fallback; global diarization is
  cleaner when available ([ADR-0005](0005-diarization-flow.md)).

## Related

- ADR-0004 (GBNF), ADR-0005 (who produces the IR), ADR-0008 (when it's built).
