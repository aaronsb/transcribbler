# ADR-0008: Build order — CLI daemon first

- **Status**: Accepted
- **Date**: 2026-06-28
- **Deciders**: Aaron

## Context

The full vision spans a backend, a CLI, a KDE tray client, an always-on PipeWire capture
daemon with heuristics, and an optional joint backend. Building desktop integration first
would entangle GUI/Wayland/KDE concerns with the core pipeline before it's proven. The
operator asked to **build the CLI mode first**.

## Decision

Build in dependency order, each stage usable on its own:

1. **Backend service on cube** — reuse whisper.cpp + pyannote + llama-swap
   ([ADR-0004](0004-llama-cpp-native-backend.md)); define the **Canonical IR**
   ([ADR-0006](0006-canonical-ir-contract.md)). Exit: audio in → diarized IR out via
   OpenAI-compatible API.
2. **CLI client** — the `whisper-client` successor: batch files + directories + YouTube
   (`yt-dlp`→`ffmpeg`), job submit/poll, render IR → markdown/VTT/JSON. Exit: usable daily
   for batch jobs.
3. **Canonicalization stage** — deterministic stitch + GBNF speaker/term mapping
   ([ADR-0006](0006-canonical-ir-contract.md)). Exit: speaker-named transcripts.
4. **CLI capture daemon** — PipeWire ring buffer + Silero VAD + pre-roll + prompt-to-keep,
   headless. Exit: always-on capture without a desktop.
5. **KDE tray client + desktop heuristics** — mic-active / Zoom-or-Chrome-running triggers,
   toast prompts. Exit: the system-tray experience.
6. **VibeVoice "hard cases" backend + live-preview tier** — the 24GB-card joint backend and
   the two-tier live mode ([ADR-0005](0005-diarization-flow.md)).

## Consequences

### Good
- Core pipeline is proven headless before any GUI/Wayland complexity.
- Every stage ships value independently; the CLI is useful long before the tray exists.
- Matches the reuse-first strategy (backend is mostly assembly, not new code).

### Bad / costs
- The headline ambient-capture UX arrives in the middle, not first (accepted; the engine
  must exist first).

## Alternatives considered

- **Tray/desktop first** — rejected; couples GUI concerns to an unproven core.
- **Capture daemon before the backend** — rejected; nothing to send audio to yet.

## Related

- ADR-0001 (clients are thin), ADR-0005/0006 (what stages 1–3 produce).
