# Prior art & research (2026-06)

Research that informed the ADRs. Captured so the reasoning survives; not a spec.

## Predecessor projects (the author's)

- **whisper-client** (Rust, `aaronsb/whisper-client`) — thin CLI: walks dirs, submits
  multipart jobs, polls `/status/{id}`, renders markdown; YouTube via `yt-dlp`→`ffmpeg`.
  *Keep:* thin-client model, job polling, YouTube ingest, markdown output. The CLI in
  [ADR-0008](../architecture/0008-build-order.md) is its successor.
- **whisper-service** (Python/FastAPI, `aaronsb/whisper-service`) — job queue
  (ThreadPoolExecutor), dual local/OpenAI mode, **silence-based chunking for the 25MB
  OpenAI limit**, streaming transcript endpoint, port 9673. *Keep:* silence-detect
  heuristic, job-queue shape. *Discard:* size-driven chunking (obsolete locally) and
  naive `reassemble_transcriptions()` (no speakers; latent timestamp-drift bug — offset
  advanced by last segment's chunk-relative end). See [ADR-0006](../architecture/0006-canonical-ir-contract.md).
- **nemo-diariziation** (Python, NVIDIA Sortformer) — **dead end**: 4-speaker cap, O(n²)
  attention OOM past ~5 min on 16GB. Motivates "diarize globally, chunk ASR"
  ([ADR-0005](../architecture/0005-diarization-flow.md)).
- **foray/transcripts** — Zoom WebVTT + `vtt_to_markdown.py`. Informs the VTT/markdown
  renderers.

## Transcription engines (AMD/CUDA, 2026)

- **whisper.cpp** — Vulkan + HIP/ROCm (gfx1100 supported), MIT, OpenAI-compatible server,
  no Python ABI risk. Chosen default ASR. https://github.com/ggml-org/whisper.cpp
- **faster-whisper / CTranslate2** — fastest on CUDA; no official ROCm (community fork
  `arlo-phoenix/CTranslate2-rocm`). https://github.com/SYSTRAN/faster-whisper
- **WhisperX** — Whisper + wav2vec2 alignment + pyannote diarization; ROCm 7.2 community
  build exists but fragile. https://github.com/m-bain/whisperX
- **Parakeet-TDT-0.6B-v3 via onnx-asr** — extreme throughput, tiny VRAM, ROCm EP; dark
  horse. https://github.com/istupakov/onnx-asr
- *Caveat:* Python **3.14** is ahead of most ML wheels; run the PyTorch stack in a
  3.11/3.12 venv ([ADR-0002](../architecture/0002-hardware-topology.md)).

## Diarization (what's new since the NeMo experiment, Aug 2025)

- **pyannote.audio 4.0 + community-1** — ~50% less speaker confusion vs 3.1; "exclusive
  mode" for clean Whisper alignment; unlimited speakers; MIT (HF-gated). Default diarizer.
  https://www.pyannote.ai/blog/community-1
- **Microsoft VibeVoice-ASR** (Jan 2026) — 9B unified ASR+diarization+timestamps, single
  pass to 60 min, no chunking, 64K context, MIT, 50+ langs. ~18GB FP16 ⇒ desktop 24GB
  card. The "hard cases" backend. https://github.com/microsoft/VibeVoice
- **NVIDIA Streaming Sortformer** (Aug 2025) — real-time but still 4-speaker capped,
  CUDA/NeMo. Relegated to the live-preview tier.
- **OpenAI gpt-4o-transcribe-diarize** — cloud native diarization (~$0.006/min); optional
  fallback only.

## Idle GPU unload / orchestration

- **llama-swap** — Go proxy, manages `llama-server`/`whisper.cpp`/etc. with per-model idle
  TTL behind an OpenAI endpoint. Chosen orchestrator. https://github.com/mostlygeek/llama-swap
- **llama.cpp router mode** (Apr 2026) — native multi-model load/unload for LLMs.
  https://huggingface.co/blog/ggml-org/model-management-in-llamacpp
- **speaches** — cleanest pure OpenAI STT server with `stt_model_ttl`; CUDA-oriented;
  alternative backend. https://speaches.ai/configuration/
- **LocalAI** watchdog-idle, **agent-cli server whisper** (closest existing blueprint:
  Wyoming+OpenAI+WS, TTL unload, manual unload). https://agent-cli.nijho.lt/

## Capture / VAD / clients (Unix-like references)

- **wyoming-satellite** — canonical "thin always-on capture client → central backend".
  https://github.com/rhasspy/wyoming-satellite
- **Silero VAD** (default) / **ten-vad** (lower-latency) — speech gating.
- **PipeWire** — `pw-record` / libpipewire into a ring buffer; detect active streams via
  `pactl list short sources` (monitors end in `.monitor`) / `pw-dump`.
- **dictee** (Qt + separate Rust/Parakeet backend, has diarization) and **voxtype-tray** —
  KDE thin-client references. https://github.com/rcspam/dictee
- *Avoid as backends (monoliths, client-UX refs only):* Hyprnote, anarlog, Handy, Vibe.

## The gap transcribbler fills

No small, composable, Linux-first, dual-CUDA/ROCm STT **daemon** does
transcription + real diarization + canonicalization, with idle GPU unload and an
OpenAI-compatible API, AND a thin-client ecosystem including an **always-on PipeWire
capture daemon with pre-roll + prompt-to-keep**. Each piece exists somewhere; nobody has
assembled them in Unix-like form. See [ADR-0001](../architecture/0001-composable-daemon-and-thin-clients.md).
