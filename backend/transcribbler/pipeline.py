"""The transcription pipeline (ADR-0008 stages 1–3).

Extracted so the CLI and the HTTP service (ADR-0018) drive *one* implementation
and produce identical IR. Callers own profile resolution and output; this owns
the work: ASR → diarize → build IR → (optional) canonicalize.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from .canon import apply_canonicalization, build_evidence
from .cores import asr_core, canonicalizer_core, diarizer_core
from .ir import build_ir, validate_ir
from .profiles import Profile
from .progress import ProgressSink


def run_pipeline(
    audio: Path,
    profile: Profile,
    *,
    diarize: bool = True,
    canon: bool = True,
    prompt: str | None = None,
    asr_progress: ProgressSink | None = None,
    diar_progress: ProgressSink | None = None,
    log: Callable[[str], None] | None = None,
) -> dict:
    """Run ``profile``'s pipeline on ``audio`` → a validated Canonical IR dict.

    ``diarize`` / ``canon`` gate the optional stages (each still requires the
    profile to enable it). ``asr_progress`` / ``diar_progress`` are per-stage
    progress sinks. ``log`` receives human-readable status lines — the CLI prints
    them to stderr; the service may drop them. Assumes ``profile.asr`` is enabled
    (callers check, since the right failure mode differs by caller).
    """
    say = log or (lambda _m: None)

    core = asr_core(profile.asr)
    say(f"[{profile.name}] {core.name} ({profile.asr.backend}) → {audio.name}")
    segments = core.transcribe(audio, progress=asr_progress, prompt=prompt)

    diar_turns = None
    if diarize and profile.diar.enabled:
        diarizer = diarizer_core(profile.diar)
        say(f"  diarizing: {diarizer.name} ({profile.diar.backend})")
        diar_turns = diarizer.diarize(audio, progress=diar_progress)
        say(f"  {len(diar_turns)} speaker turns")

    ir = build_ir(segments, profile, audio, diar_turns=diar_turns)

    if canon and profile.llm.enabled:
        canonicalizer = canonicalizer_core(profile.llm)
        say(f"  canonicalizing: {canonicalizer.name} ({profile.llm.backend})")
        data = canonicalizer.canonicalize(build_evidence(ir))
        ir = apply_canonicalization(ir, data)
        validate_ir(ir)
        named = [s for s in ir["speakers"] if s.get("display_name")]
        say(f"  named {len(named)}/{len(ir['speakers'])} speakers")

    return ir
