# transcribbler backend

The compute backend: a thin orchestrator over swappable inference engines
("cores"), selected by a declarative **compute profile** ([ADR-0015](../docs/architecture/0015-pluggable-compute-backends.md)),
producing **Canonical IR** ([ADR-0006](../docs/architecture/0006-canonical-ir-contract.md)).

> **Status: stage 1, first slice.** ASR-only (no diarization yet), CLI-only (no HTTP
> server yet). Proves the profile → GPU → IR path end to end. Diarization, the
> canonicalization LLM, and the OpenAI-compatible server come next (ADR-0008).

## Layout

```
transcribbler/
  profiles.py     # load a compute profile (.toml) — engine, backend, binary, model per stage
  probe.py        # detect GPU backends (cuda/rocm/vulkan); recommend one (Vulkan-first)
  ir.py           # build + schema-validate Canonical IR
  cores/
    base.py       # ASRCore protocol + Segment — the tiny swappable-engine interface
    whisper_cpp.py# whisper.cpp subprocess adapter (device chosen at whisper.cpp build time)
  cli.py          # `transcribbler probe` / `transcribbler transcribe`
```

## Prereqs

- An engine binary the profile points at. For `desktop-vulkan`, build whisper.cpp with
  Vulkan and fetch a model:
  ```bash
  git clone --depth 1 https://github.com/ggml-org/whisper.cpp ~/Projects/ai/whisper.cpp
  cmake -S ~/Projects/ai/whisper.cpp -B ~/Projects/ai/whisper.cpp/build -DGGML_VULKAN=1 -DCMAKE_BUILD_TYPE=Release
  cmake --build ~/Projects/ai/whisper.cpp/build -j
  bash ~/Projects/ai/whisper.cpp/models/download-ggml-model.sh base.en
  ```
- `ffmpeg` on PATH (input is normalized to 16 kHz mono WAV).
- `uv` for running the backend.

## Use

```bash
uv run --project backend transcribbler probe
uv run --project backend transcribbler transcribe path/to/audio.wav \
    -p profiles/desktop-vulkan.toml -o out.ir.json
```

Swapping GPUs = swapping the `-p` profile; the CLI and IR output are identical
(ADR-0015). `make backend-smoke` runs an end-to-end check against the whisper.cpp
sample if it's present.
