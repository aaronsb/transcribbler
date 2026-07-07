"""Drain a finished live capture into a session pack (session-pack spec + ADR-0028).

Extracted from ``capture.py`` (which the memory flags as over-long) as the drain-and-persist
seam: given the session's emitted turns, the session gallery's per-speaker centroids, and the
retained session audio, assemble the Canonical IR, cut speaker-isolated clips, and write one
real session pack — the ``.md`` sidecar (pinned to the operator's ``-o``) plus the
self-describing blob — replacing the earlier ad-hoc ``<stem>.ir.json`` + ``.md`` pair.

The pack always carries the gallery's centroids as its embedding seed, so a capture prims the
durable voiceprint library on ``extract`` even when audio wasn't retained; retained audio adds
the re-extract substrate (and the ADR-0029 adjudication exemplars).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from . import pack
from .ir import build_live_ir
from .profiles import Profile

# a capture chunk is stereo: mic on channel 0 (operator), meeting mix on channel 1 (remotes)
_MIC_CHANNEL = 0
_MEETING_CHANNEL = 1

TurnTuple = tuple[float, float, str, str]  # (start, end, speaker_label, text)


_ASSEMBLE_TIMEOUT = 300  # seconds; assembling a long session shouldn't hang the drain forever


def _concat_quote(path: Path) -> str:
    """Single-quote a path for an ffmpeg concat list, escaping any literal quote as ``'\\''``."""
    escaped = str(path.resolve()).replace("'", "'\\''")
    return f"'{escaped}'"


def _silence(segment_s: int, dst: Path) -> Path:
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
         "-i", "anullsrc=channel_layout=stereo:sample_rate=16000", "-t", str(segment_s),
         "-c:a", "pcm_s16le", str(dst)],
        check=True, timeout=_ASSEMBLE_TIMEOUT,
    )
    return dst


def assemble_session(retain_dir: Path, segment_s: int, dst: Path) -> Path | None:
    """Concatenate retained stereo chunks into one aligned session wav (or None if none kept).

    Chunks are placed at their index offset with any *interior* gap (a chunk dropped by the
    backlog guard or muted while paused) filled by ``segment_s`` of silence, so absolute turn
    timestamps still line up with the assembled audio for slicing.

    Uses ffmpeg's **concat demuxer** (a list file) rather than an N-input filtergraph, so a
    multi-hour capture's ~hundreds of chunks concatenate in one bounded, timeout-guarded pass
    without an argv/filtergraph explosion.
    """
    kept = {}
    for p in retain_dir.glob("chunk_*.wav"):
        try:
            kept[int(p.stem.split("_")[1])] = p
        except (IndexError, ValueError):
            pass
    if not kept:
        return None

    silence: Path | None = None
    parts: list[Path] = []
    for i in range(max(kept) + 1):
        if i in kept:
            parts.append(kept[i])
        else:  # gap-fill to preserve alignment
            if silence is None:
                silence = _silence(segment_s, retain_dir / "_gap.wav")
            parts.append(silence)

    listing = retain_dir / "_concat.txt"
    listing.write_text("".join(f"file {_concat_quote(p)}\n" for p in parts))
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0",
         "-i", str(listing), "-ar", "16000", "-ac", "2", "-c:a", "pcm_s16le", str(dst)],
        check=True, timeout=_ASSEMBLE_TIMEOUT,
    )
    return dst


def persist_session_pack(
    turns: list[TurnTuple],
    profile: Profile,
    *,
    operator_label: str,
    diarized: bool,
    centroids: dict[str, list[float]],
    session_wav: Path | None,
    out_path: Path,
    tags: list[str] | None = None,
    clip_dir: Path | None = None,
) -> pack.PackResult:
    """Build the IR from ``turns`` and write a session pack beside ``out_path``.

    ``centroids`` is the gallery's ``{sid: centroid}`` seed; ``session_wav`` (if present) is the
    assembled stereo audio to cut speaker-isolated clips from — the operator from the mic
    channel, each remote from the meeting channel, by its own turn spans.
    """
    ir = build_live_ir(
        turns,
        profile,
        duration_s=max(end for _, end, _, _ in turns),
        operator_label=operator_label,
        diarized=diarized,
    )
    ids = [s["id"] for s in ir["speakers"]]
    label_by_id = {s["id"]: (s.get("display_name") or s["id"]) for s in ir["speakers"]}
    id_by_label = {label: sid for sid, label in label_by_id.items()}

    # A gallery centroid is keyed by the speaker's LABEL (its gallery sid, e.g. "S1"). Attach it
    # to the IR speaker with that exact label — NOT by assuming the gallery sid equals the IR id,
    # which build_live_ir's free-id remap can violate (it may hand a silent gallery id like "S1"
    # to the "Remote"/"You" bucket), folding the wrong person's voiceprint. A centroid whose label
    # never reached the transcript is dropped rather than mis-attributed.
    embeddings = {id_by_label[label]: vec for label, vec in centroids.items() if label in id_by_label}

    clips: dict[str, Path] = {}
    if session_wav is not None:
        spans_by_id = {
            sid: [(s, e) for (s, e, lbl, _) in turns if lbl == label_by_id[sid]] for sid in ids
        }
        channel_by_id = {
            sid: (_MIC_CHANNEL if label_by_id[sid] == operator_label else _MEETING_CHANNEL)
            for sid in ids
        }
        clips = pack.build_speaker_clips(
            session_wav, spans_by_id, channel_by_id, dst_dir=clip_dir or session_wav.parent
        )

    return pack.write_pack(
        ir,
        title=out_path.stem,
        tags=tags or ["meeting", "transcription"],
        embeddings=embeddings,
        audio=clips,
        dest_dir=out_path.parent,
        md_path=out_path,
    )
