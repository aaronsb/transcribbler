# ADR-0003: Reuse inference engines; build the gap

- **Status**: Accepted
- **Date**: 2026-06-28
- **Deciders**: Aaron

## Context

Mature, well-maintained inference engines and backend servers already exist (whisper.cpp,
faster-whisper, pyannote, WhisperX, speaches, llama.cpp). Writing another inference engine
would be wasted effort and a maintenance liability. But no existing tool assembles the
*whole* shape we want (see [ADR-0001](0001-composable-daemon-and-thin-clients.md) and
[prior-art](../design/prior-art.md)): the connective tissue and the ambient capture
behavior are missing from the Unix-like world.

The closest existing thing, `agent-cli server whisper`, already proves most of the
backend (Wyoming + OpenAI API + WebSocket, TTL idle-unload, manual unload). Its gaps are
exactly our constraints: macOS-leaning clients, no ROCm, diarization done client-side.

## Decision

**Do not write an inference engine.** Reuse existing engines for ASR, diarization, and
the canonicalization LLM. **Build only the parts that don't exist** in composable form:

| Reuse (don't build) | Build (the gap) |
|---|---|
| ASR: whisper.cpp / faster-whisper | The **Canonical IR** + canonicalization stage ([ADR-0006](0006-canonical-ir-contract.md)) |
| Diarization: pyannote 4.x / WhisperX | The **thin clients** (CLI, KDE tray) |
| Canonicalization LLM: llama.cpp | The **PipeWire always-on capture daemon** (VAD + pre-roll + prompt-to-keep) |
| Idle-unload proxy: llama-swap | The thin **OpenAI-compatible shim**/glue + routing where engines don't provide it |
| Joint backend: VibeVoice-ASR | Packaging & supervisor wrappers ([ADR-0007](0007-supervisor-agnostic-packaging.md)) |

Treat engines as **swappable upstreams** behind a stable contract, so any of them can be
replaced without touching clients or the canonicalization stage.

## Consequences

### Good
- Effort goes to the genuinely novel/missing pieces, where the value is.
- Engine upgrades (new Whisper, new pyannote) are drop-in.
- Smaller, more maintainable codebase that we actually own.

### Bad / costs
- Dependency on upstream projects' health and APIs (mitigated by the swappable-upstream
  boundary and the OpenAI-compatible contract).
- Some impedance-matching glue per engine.

## Alternatives considered

- **Fork/adopt `agent-cli` wholesale** — strong blueprint, but macOS-leaning and no ROCm;
  we study it rather than depend on it.
- **Build a bespoke engine** — rejected; cost with no benefit.

## Related

- ADR-0001, ADR-0004 (the concrete reused stack), ADR-0006 (the part we own).
