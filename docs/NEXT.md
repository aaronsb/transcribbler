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

The architecture spine landed as **ADR-0017–0021** (Rust single-binary clients over a Python
backend service; one client-facing HTTP wire over Unix-socket locally + TCP remotely; job
scheduling with a "partial singleton" live mode; per-locality security tiers; a deferred
internet cloud-worker tier). ADR-0017 **resequences the build order**: introduce the
client/server boundary *before* the remaining features.

**Build order (the implementation spine — do in order):**
1. **Extract the backend HTTP service (ADR-0018)** [task #6] — wrap the existing cores
   (`whisper_cpp`, `pyannote`, renderers) in the client-facing wire: audio → IR over HTTP on a
   Unix socket, server-owned async jobs, SSE `queued/progress/paused/done|error|canceled`,
   profile-by-name (server-side allowlist), `/version`. No behavior change vs today's CLI.
2. **Build the Rust CLI to parity over the wire (ADR-0017)** [task #7, blocked by #6] —
   `clients/cli/` (cargo), IR types **codegen'd from `schemas/`**; `transcribe/render/probe`
   parity; render lives client-side. This is the "feels like one clean binary" successor to
   whisper-client.

**Then the features (most now ride on the wire):**
3. **Batch/dir + YouTube ingest** [task #2, blocked by #7] — Rust-client feature; bulk (the
   431-file Alan Watts set).
4. **Live audio-ingest ADR** [task #8] — capture-daemon streaming path (the wire's live mode).
5. **Capture daemon** [task #4, blocked by #7, #8] — PipeWire ring buffer + Silero VAD +
   pre-roll + prompt-to-keep (ADR-0009/0010).

**Independent / unblocked backend work (can start anytime, no wire dependency):**
- **Voiceprint enrollment (ADR-0016)** [task #1] — the primary naming path. Emit
  `speaker_embeddings` from the sidecar (currently discarded), `enroll` mode, XDG voiceprint
  store, cosine match at transcribe → `source: "enrolled"`. *Highest backend value.*
- **cube (CUDA) topology** [task #3] — build engines on cube, finalize `profiles/cube-cuda.toml`;
  prove "swap GPU = swap profile".

## Key facts / context
- **Hardware:** desktop = AMD RX 7900 XTX (24GB, Vulkan/ROCm, gfx1100); cube = NVIDIA RTX 4060 Ti (CUDA, always-on).
- **Engines (host-specific paths in profiles):** whisper.cpp at `~/Projects/ai/whisper.cpp` (Vulkan build);
  `llama-server` at `/usr/bin`; canon model `~/Projects/ai/llama.cpp/Qwen3.6-35B-A3B-UD-Q4_K_M.gguf`.
- **Diarizer sidecar:** `backend/diarizer/` — own uv env, `torch==2.10.0+rocm7.0`, `pyannote.audio>=4.0`.
  HF token in `.env.hf` (gitignored); gated model `pyannote/speaker-diarization-community-1` (accepted).
- **LLM canonicalization (ADR-0006):** built, tested, **off by default** — low yield / high compute /
  mis-attribution risk. Opt-in via the profile `[llm]` stage. Superseded by enrollment as primary naming.
- **Decisions:** ADRs `docs/architecture/0001–0021`. Naming strategy = ADR-0016; client/server
  architecture spine = ADR-0017–0021 (0021 Proposed/deferred, the rest Accepted).

## Gotchas learned (so we don't relearn them)
- pyannote 4.x: load wav yourself (soundfile) + pass `{waveform, sample_rate}` to dodge the torchcodec
  `AudioDecoder` gap; use `DiarizeOutput.speaker_diarization`; `set_telemetry_metrics(False)`.
- llama.cpp: the schema's `^S[0-9]+$` pattern breaks `--json-schema`; use the hand GBNF
  (`schemas/canonicalization-data.gbnf`). Grammar + thinking conflict (grammar forces JSON from token 0);
  non-thinking + grammar is clean. Reasoning fixes cross-speaker attribution but costs ~3k tokens/call.
- Makefile: no inline comments after `VAR ?= value` (trailing whitespace folds into the value).
- uv + torch-ROCm: needs `index-strategy = "unsafe-best-match"` (triton-rocm vs PyPI).
- **Two HTTP layers — don't conflate.** ADR-0004's OpenAI-compatible API is the *internal engine*
  wire (in front of llama-swap). ADR-0018 is the *new client-facing* contract (audio→IR, SSE,
  profile-by-name) that the Rust clients speak. Profiles are a **server-side allowlist** — clients
  send a profile *name*, never a binary/model path.
- **"spool" is two things:** client-side store-and-forward on the capture host (ADR-0012) vs. the
  backend job queue + live audio buffer (ADR-0019). Different structures.
- **Multi-user ≠ multi-tenant:** shared-backend fair-share is the *remote* case (ADR-0020); local
  UDS is single-user (per-user socket + socket activation = one backend per OS user).
