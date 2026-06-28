# ADR-0004: llama.cpp-native backend with idle GPU unload

- **Status**: Accepted
- **Date**: 2026-06-28
- **Deciders**: Aaron

## Context

We want dynamic GPU model load/unload (idle TTL → free VRAM, Ollama `keep_alive`-style)
so the GPU is available between bursts ([ADR-0002](0002-hardware-topology.md)). The
operator's explicit preference is **llama.cpp over Ollama**.

Good news, verified 2026-06: the relevant tooling is already llama.cpp-native.
- **llama-swap** is a Go proxy built for the llama.cpp world; it starts/stops
  `llama-server`, **`whisper.cpp`**, `stable-diffusion.cpp`, etc., with per-model idle
  **TTL unload** behind one OpenAI-compatible endpoint. (Ollama is what it replaces.)
- **llama.cpp `llama-server`** itself gained **router mode** (`--models-dir/--models-preset/
  --models-autoload/--models-max`): dynamic load/unload/switch without restart, one model
  resident per worker. This covers *LLM* swapping natively.
- llama.cpp natively supports **GBNF / JSON-schema grammar-constrained decoding** — which
  guarantees well-formed structured output for canonicalization ([ADR-0006](0006-canonical-ir-contract.md)).
  This is a decisive advantage over Ollama's coarser `format=json`.

The one thing with **no ggml/llama.cpp version: diarization.** pyannote/WhisperX are
PyTorch-only.

## Decision

The default backend is the **llama.cpp/ggml family**, fronted by **llama-swap** for one
OpenAI-compatible endpoint with idle GPU release:

```
                 ┌─ whisper.cpp server   (ASR; ggml; ROCm/CUDA)
llama-swap  ─────┼─ pyannote sidecar     (diarization; PyTorch — the one non-ggml piece)
(idle TTL,       └─ llama-server + GBNF  (canonicalization JSON; ggml)
 OpenAI API)        └ or native router mode if/when multiple LLMs are run
```

- **ASR** = whisper.cpp (`whisper-server`, OpenAI-compatible), runs on both GPUs.
- **Canonicalization LLM** = `llama-server` with a **GBNF grammar** for guaranteed-valid
  JSON output.
- **Diarization** = pyannote PyTorch sidecar exposing an HTTP endpoint; llama-swap can
  start/stop it as a generic process so it still participates in idle-unload.
- **Orchestration** = **llama-swap** (engine-agnostic) rather than native router mode,
  because the fleet is heterogeneous (ggml ASR + ggml LLM + PyTorch diarizer). Reserve
  native router mode for when multiple *LLMs* are run.

## Consequences

### Good
- Fully llama.cpp-native except the unavoidable PyTorch diarizer; no Ollama.
- GBNF makes canonicalization output structurally guaranteed.
- One OpenAI-compatible endpoint; idle GPU release across the whole fleet.
- whisper.cpp as a native binary dodges the Python 3.14 wheel problem on the hot path.

### Bad / costs
- Diarization remains a PyTorch service (separate venv, ROCm/CUDA wheels) — irreducible.
- llama-swap is another process to supervise (cheap, single Go binary).

## Alternatives considered

- **speaches** as backend — cleanest pure OpenAI server with idle TTL, but
  CTranslate2/CUDA-oriented and not llama.cpp; kept as a possible alternative backend.
- **Ollama** — rejected per operator preference and weaker grammar control.
- **Native llama.cpp router mode only** — insufficient; doesn't manage the PyTorch
  diarizer or whisper.cpp as first-class swap targets.

## Related

- ADR-0002, ADR-0003, ADR-0006 (why GBNF matters).
