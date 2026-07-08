# Where we are / what's next

Session handoff for resuming work (e.g. after context compaction).

## Status: working MVP — client/server wire + live capture + voiceprints

Point it at a recording → diarized, readable transcript. Verified on a real 57-min
cell-phone conference (~2.5 min to process on the RX 7900 XTX). The client/server
architecture spine has since landed: a Python backend HTTP service and a Rust CLI that
speaks it, plus live capture, voiceprint enrollment, and session packs.

```bash
make install                 # uv sync + put `transcribbler` on ~/.local/bin (on PATH)
transcribbler probe          # detect GPU backends (recommends vulkan here)
transcribbler transcribe call.m4a -f md -o call.md   # profile auto-selected by GPU
transcribbler render call.ir.json -f vtt      # re-render an IR without re-transcribing
transcribbler enroll         # guided read-aloud → voiceprint record (session pack)
transcribbler capture        # live mic+meeting → rolling transcript on disk
transcribbler library        # list/inspect voiceprint records
# -p overrides: a bare name (desktop-vulkan / cube-cuda), a .toml path, or $TRANSCRIBBLER_PROFILE
# --progress/--no-progress: live ASR/diar % to stderr (default: on when TTY)
# --prompt "names, jargon": bias ASR spelling (or set [asr] prompt in the profile)
make test | lint | check | validate-schemas | backend-smoke
```

Pipeline: **whisper.cpp (Vulkan) → pyannote (ROCm) → overlap-align → Canonical IR → render (md/vtt/json)**.

## Done

**Core pipeline (ADR-0008 stages 1–3):**
- Compute backend + **Canonical IR** contract (`schemas/`, validated + CI) with selectable **GPU profiles** (ADR-0015).
- ASR (whisper.cpp/Vulkan) + **diarization** (pyannote/ROCm, sidecar in its own torch-ROCm env) + overlap alignment.
- **Renderers** md/vtt/json; `transcribe`/`render`/`probe` Python CLI; Makefile control panel + ruff + CI.

**Client/server spine (ADR-0017–0019) — landed:**
- **Backend HTTP service (ADR-0018):** audio → IR over HTTP on a Unix socket; server-owned async
  jobs; SSE `queued/progress/paused/done|error|canceled`; `/v1/jobs`, `/v1/jobs/{id}/events|result`,
  `/v1/profiles`, `/v1/version`, `/v1/healthz`; profile-by-name (server-side allowlist).
- **Rust CLI (ADR-0017):** `clients/cli/` (cargo), IR types codegen'd from `schemas/`;
  `transcribe`/`render`/`profiles`/`version` over the wire; render lives client-side.

**Features — landed:**
- **Voiceprint enrollment (ADR-0016/0023):** durable XDG voiceprint store; guided read-aloud
  `enroll`; records as OKF markdown+frontmatter; `library` inspect (#29).
- **Session packs (ADR-0028):** `enroll` produces a real session pack; `capture`/`listen` persist
  packs with retained audio; extract folds from the pack.
- **Live capture (ADR-0022):** capture/listen path with live IR routing.
- **Live speaker-ID aids (ADR-0024):** live audio-source dB meter to pin mic/meeting routes (#10);
  low-confidence ASR spans flagged in transcript/IR (#11).

## Next (no live TaskList — pick and open a GitHub issue + branch)

**Genuinely remaining, unblocked:**
- **Batch/dir + YouTube ingest** — Rust-client feature; `yt-dlp`→`ffmpeg` normalize, bulk over a
  directory (the 431-file Alan Watts set). No code yet; only referenced in ADR-0008/0019. Rides on
  the wire, which now exists.
- **cube (CUDA) topology** — `profiles/cube-cuda.toml` is still marked "EXAMPLE — paths TBD". Build
  engines on cube (RTX 4060 Ti), finalize the profile, prove "swap GPU = swap profile".
- **Rust-client parity for `enroll`/`capture`/`meter`/`library`** — these live in the Python CLI
  today; the Rust client covers only transcribe/render/profiles/version.
- **Remote/TLS access (ADR-0020)** — deferred. The Rust client `bail!`s on `https://` today
  ("tunnel over SSH and use http://"). No TLS client yet.

**Housekeeping / drift to reconcile:**
- **ADR status drift:** 0022 (live-audio), 0023 (voiceprint-lifecycle), 0024 (live-speaker-id) still
  read **Proposed** though their features shipped — reconcile to Accepted like ADR-0028 got (PR #32).
- Stray artifact `backend/transcript-20260707-132240.md` — gitignore or remove.

## Key facts / context
- **Hardware:** desktop = AMD RX 7900 XTX (24GB, Vulkan/ROCm, gfx1100); cube = NVIDIA RTX 4060 Ti (CUDA, always-on).
- **Engines (host-specific paths in profiles):** whisper.cpp at `~/Projects/ai/whisper.cpp` (Vulkan build);
  `llama-server` at `/usr/bin`; canon model `~/Projects/ai/llama.cpp/Qwen3.6-35B-A3B-UD-Q4_K_M.gguf`.
- **Diarizer sidecar:** `backend/diarizer/` — own uv env, `torch==2.10.0+rocm7.0`, `pyannote.audio>=4.0`.
  HF token in `.env.hf` (gitignored); gated model `pyannote/speaker-diarization-community-1` (accepted).
- **LLM canonicalization (ADR-0006):** built, tested, **off by default** — low yield / high compute /
  mis-attribution risk. Opt-in via the profile `[llm]` stage. Superseded by enrollment as primary naming.
- **Decisions:** ADRs `docs/architecture/0001–0028`. Naming strategy = ADR-0016; client/server
  architecture spine = ADR-0017–0021 (0021 Proposed/deferred, the rest Accepted); live-capture +
  voiceprints + session packs = ADR-0022–0028 (0025/0026 Draft, some status drift — see Next).

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
