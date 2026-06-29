"""Canonical IR construction + validation (ADR-0006).

This first slice is ASR-only: with no diarization, every turn is attributed to a
single fallback speaker. When a diarizer is wired in, speaker assignment becomes
the overlap-alignment step (ADR-0005); the IR shape does not change.
"""
from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from pathlib import Path

from jsonschema import Draft202012Validator

from .align import align
from .cores.base import Segment, SpeakerTurn
from .profiles import Profile

SCHEMA_VERSION = "0.1"
# repo_root/schemas/canonical-ir.schema.json  (backend/transcribbler/ir.py -> up 3)
_SCHEMA_PATH = Path(__file__).resolve().parents[2] / "schemas" / "canonical-ir.schema.json"


@lru_cache(maxsize=1)
def _validator() -> Draft202012Validator:
    schema = json.loads(_SCHEMA_PATH.read_text())
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=Draft202012Validator.FORMAT_CHECKER)


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def build_ir(
    segments: list[Segment],
    profile: Profile,
    source_path: Path,
    *,
    diar_turns: list[SpeakerTurn] | None = None,
    validate: bool = True,
) -> dict:
    """Assemble a Canonical IR document from ASR segments.

    With `diar_turns`, segments are speaker-attributed via overlap alignment
    (ADR-0005). Without them, this is the ASR-only fallback: one fallback speaker.
    """
    duration = max((s.end for s in segments), default=0.0)
    backend = {"kind": "modular", "asr": f"{profile.asr.engine}:{profile.asr.backend}"}

    if diar_turns:
        backend["diarizer"] = f"{profile.diar.engine}:{profile.diar.backend}"
        speaker_ids, aligned = align(segments, diar_turns)
        speakers = [{"id": sid, "source": "fallback"} for sid in speaker_ids]
        turns = [
            {
                "speaker_id": a.speaker_id,
                "start": round(a.start, 3),
                "end": round(a.end, 3),
                "text": a.text,
                **({"secondary_speakers": a.secondary_speakers} if a.secondary_speakers else {}),
                "provenance": {"chunk": 0, "offset_s": 0.0},
            }
            for a in aligned
        ]
    else:
        speakers = [{"id": "S1", "source": "fallback"}]
        turns = [
            {
                "speaker_id": "S1",
                "start": round(s.start, 3),
                "end": round(s.end, 3),
                "text": s.text,
                "provenance": {"chunk": 0, "offset_s": 0.0},
            }
            for s in segments
        ]

    ir = {
        "schema_version": SCHEMA_VERSION,
        "source": {
            "kind": "batch",
            "uri": source_path.resolve().as_uri(),
            "sha256": sha256_of(source_path),
            "duration_s": round(duration, 3) if duration > 0 else 0.001,
        },
        "backend": backend,
        "speakers": speakers,
        "turns": turns,
    }
    if validate:
        errors = sorted(_validator().iter_errors(ir), key=lambda e: list(map(str, e.path)))
        if errors:
            locs = "; ".join(f"{'/'.join(map(str, e.path)) or '<root>'}: {e.message}" for e in errors[:5])
            raise ValueError(f"produced IR fails schema: {locs}")
    return ir
