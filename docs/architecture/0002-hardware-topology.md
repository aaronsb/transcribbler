# ADR-0002: Two-machine topology — CUDA backend on cube, ROCm desktop for clients

- **Status**: Accepted
- **Date**: 2026-06-28
- **Deciders**: Aaron

## Context

Two machines are available:

| Machine | GPU | Notes |
|---|---|---|
| **desktop** | AMD RX 7900 XTX, 24GB, ROCm (gfx1100); Ryzen 9 9950X3D | KDE workstation, where capture happens; used for games/work |
| **cube** (`aaron@cube`) | NVIDIA RTX 4060 Ti, 16GB, **CUDA**; i9-9900K; 62GB RAM | Always-on; already runs docker (traefik/n8n/dockge); idle GPU |

The single biggest constraint in local transcription/diarization in 2026 is CUDA vs
ROCm: the best stacks (faster-whisper/CTranslate2, pyannote, WhisperX, NeMo) are
first-class on CUDA and second-class/fragile on ROCm. Python 3.14 (on both hosts) is
also ahead of most ML wheels, which target 3.11–3.12.

cube being CUDA-capable and always-on removes the dominant constraint, and keeping the
heavy work off the desktop card means the desktop GPU stays free for games/work.

## Decision

- **cube is the primary backend host.** All routine transcription + diarization +
  canonicalization runs there on CUDA. 16GB comfortably holds `large-v3` + pyannote.
- **The desktop runs the clients** (CLI, tray, capture daemon), which stream audio to
  cube over the network.
- **The desktop's 24GB AMD card is a secondary/specialty backend**, used only where it
  is genuinely better — notably **VibeVoice-ASR 9B**, whose ~18GB FP16 footprint does
  not fit cube's 16GB but does fit 24GB (see [ADR-0005](0005-diarization-flow.md)).
- The ML Python stack runs in a pinned **3.11/3.12** venv/container, not 3.14
  (see [ADR-0007](0007-supervisor-agnostic-packaging.md)). whisper.cpp (native, no
  Python ABI) sidesteps this on the hot path.

## Consequences

### Good
- Best-supported (CUDA) path for the hard parts; desktop GPU stays free.
- Backend is always-on and reachable; clients can be ephemeral.
- A real reason to use *both* GPUs rather than fighting ROCm everywhere.

### Bad / costs
- Network dependency: desktop clients need cube reachable (mitigate with a local
  fallback backend on the desktop for offline/laptop scenarios).
- Two GPU vendors ⇒ potentially **two backend builds** (CUDA and ROCm); accepted.

## Alternatives considered

- **Everything on the desktop via ROCm** — rejected as the default; fragile stack,
  steals the GPU from games, Python 3.14 wheel pain. Retained as a fallback.
- **Everything on cube including VibeVoice** — blocked by 16GB VRAM for the 9B model.

## Related

- ADR-0003 (reuse vs build), ADR-0004 (backend stack), ADR-0005 (which model where).
