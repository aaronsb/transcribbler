# Where we are / what's next

Session handoff for resuming work (e.g. after context compaction).

## Status: working MVP (CLI)

Point it at a recording → diarized, readable transcript. Verified on a real 57-min
cell-phone conference (~2.5 min to process on the RX 7900 XTX).

```bash
make install                 # uv sync + put `transcribbler` on ~/.local/bin (on PATH)
transcribbler probe          # detect GPU backends (recommends vulkan here)
transcribbler transcribe call.m4a -f md -o call.md   # profile auto-selected by GPU
transcribbler render call.ir.json -f vtt      # re-render an IR without re-transcribing
# -p overrides: a bare name (desktop-vulkan / cube-cuda), a .toml path, or $TRANSCRIBBLER_PROFILE
# --progress/--no-progress: live ASR/diar % to stderr (default: on when TTY)
# --prompt "names, jargon": bias ASR spelling (or set [asr] prompt in the profile)
make test | lint | check | validate-schemas | backend-smoke
```

Pipeline: **whisper.cpp (Vulkan) → pyannote (ROCm) → overlap-align → Canonical IR → render (md/vtt/json)**.

## Done (ADR-0008 stages 1–3)
- Compute backend + **Canonical IR** contract (`schemas/`, validated + CI) with selectable **GPU profiles** (ADR-0015).
- ASR (whisper.cpp/Vulkan) + **diarization** (pyannote/ROCm, sidecar in its own torch-ROCm env) + overlap alignment.
- **Renderers** md/vtt/json; `transcribe`/`render`/`probe` CLI; Makefile control panel + ruff + CI.

## Next (task list — see TaskCreate/TaskList)
1. **Voiceprint enrollment (ADR-0016)** — the primary naming path. Emit `speaker_embeddings`
   from the sidecar (currently discarded), `enroll` mode, XDG voiceprint store, cosine match
   at transcribe → `source: "enrolled"`. *Highest value.*
2. **Batch/dir + YouTube ingest** — for bulk (the 431-file Alan Watts set).
3. **cube (CUDA) topology** — build engines on cube, finalize `profiles/cube-cuda.toml`; prove "swap GPU = swap profile".
4. **Capture daemon** — PipeWire ring buffer + Silero VAD + pre-roll + prompt-to-keep (ADR-0009/0010).

## Key facts / context
- **Hardware:** desktop = AMD RX 7900 XTX (24GB, Vulkan/ROCm, gfx1100); cube = NVIDIA RTX 4060 Ti (CUDA, always-on).
- **Engines (host-specific paths in profiles):** whisper.cpp at `~/Projects/ai/whisper.cpp` (Vulkan build);
  `llama-server` at `/usr/bin`; canon model `~/Projects/ai/llama.cpp/Qwen3.6-35B-A3B-UD-Q4_K_M.gguf`.
- **Diarizer sidecar:** `backend/diarizer/` — own uv env, `torch==2.10.0+rocm7.0`, `pyannote.audio>=4.0`.
  HF token in `.env.hf` (gitignored); gated model `pyannote/speaker-diarization-community-1` (accepted).
- **LLM canonicalization (ADR-0006):** built, tested, **off by default** — low yield / high compute /
  mis-attribution risk. Opt-in via the profile `[llm]` stage. Superseded by enrollment as primary naming.
- **Decisions:** ADRs `docs/architecture/0001–0016`. Naming strategy = ADR-0016.

## Gotchas learned (so we don't relearn them)
- pyannote 4.x: load wav yourself (soundfile) + pass `{waveform, sample_rate}` to dodge the torchcodec
  `AudioDecoder` gap; use `DiarizeOutput.speaker_diarization`; `set_telemetry_metrics(False)`.
- llama.cpp: the schema's `^S[0-9]+$` pattern breaks `--json-schema`; use the hand GBNF
  (`schemas/canonicalization-data.gbnf`). Grammar + thinking conflict (grammar forces JSON from token 0);
  non-thinking + grammar is clean. Reasoning fixes cross-speaker attribution but costs ~3k tokens/call.
- Makefile: no inline comments after `VAR ?= value` (trailing whitespace folds into the value).
- uv + torch-ROCm: needs `index-strategy = "unsafe-best-match"` (triton-rocm vs PyPI).
