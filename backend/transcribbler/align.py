"""Overlap alignment (ADR-0005).

Global diarization gives speaker turns over the whole recording; ASR gives text
segments. Alignment assigns each ASR segment to the speaker whose turn it most
overlaps ("who said what"), and flags other overlapping speakers as secondary
(overlapped speech). Speaker identity is already global, so canonical ids are a
deterministic first-appearance numbering — no cross-chunk reconciliation needed.

This is pure, deterministic code (no ML): same inputs -> same output, so it is
golden-file testable.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .cores.base import Segment, SpeakerTurn

# Fraction of a segment a non-primary speaker must cover to count as "secondary"
# (overlapped speech), rather than incidental boundary spill.
SECONDARY_MIN_OVERLAP = 0.30


@dataclass(frozen=True)
class AlignedTurn:
    speaker_id: str
    start: float
    end: float
    text: str
    secondary_speakers: list[str] = field(default_factory=list)
    confidence: float | None = None  # carried from the source ASR segment


def _overlap(a_start: float, a_end: float, b_start: float, b_end: float) -> float:
    """Length of the temporal overlap of [a_start,a_end] and [b_start,b_end]."""
    return max(0.0, min(a_end, b_end) - max(a_start, b_start))


def canonical_speaker_map(turns: list[SpeakerTurn]) -> dict[str, str]:
    """Map engine-local labels to canonical S1, S2, ... by first appearance in time."""
    mapping: dict[str, str] = {}
    for turn in sorted(turns, key=lambda t: (t.start, t.end)):
        if turn.label not in mapping:
            mapping[turn.label] = f"S{len(mapping) + 1}"
    return mapping


def _primary_and_secondary(
    seg: Segment, turns: list[SpeakerTurn], labels: dict[str, str]
) -> tuple[str, list[str]]:
    """Pick the max-overlap speaker for a segment; collect significant co-speakers."""
    by_label: dict[str, float] = {}
    for t in turns:
        ov = _overlap(seg.start, seg.end, t.start, t.end)
        if ov > 0:
            by_label[t.label] = by_label.get(t.label, 0.0) + ov

    if not by_label:
        # Segment falls in a diarization gap: attribute to the nearest turn by midpoint.
        mid = (seg.start + seg.end) / 2
        nearest = min(turns, key=lambda t: abs((t.start + t.end) / 2 - mid))
        return labels[nearest.label], []

    # Primary = most overlap; ties broken deterministically by lowest local label.
    primary = max(sorted(by_label), key=lambda lbl: by_label[lbl])
    seg_len = max(seg.end - seg.start, 1e-9)
    secondary = sorted(
        labels[lbl]
        for lbl, ov in by_label.items()
        if lbl != primary and ov / seg_len >= SECONDARY_MIN_OVERLAP
    )
    return labels[primary], secondary


def align(segments: list[Segment], turns: list[SpeakerTurn]) -> tuple[list[str], list[AlignedTurn]]:
    """Attribute ASR segments to canonical speakers.

    Returns (ordered canonical speaker ids, aligned turns). Raises if there are
    no diarization turns — callers use the ASR-only path instead.
    """
    if not turns:
        raise ValueError("align() requires diarization turns; use the ASR-only path otherwise")
    labels = canonical_speaker_map(turns)
    aligned = []
    for seg in segments:
        primary, secondary = _primary_and_secondary(seg, turns, labels)
        aligned.append(
            AlignedTurn(
                speaker_id=primary,
                start=seg.start,
                end=seg.end,
                text=seg.text,
                secondary_speakers=secondary,
                confidence=seg.confidence,
            )
        )
    # Only declare speakers actually referenced by a turn (primary or secondary).
    # A diarized speaker whose turns never won any segment is dropped, not emitted
    # as an orphan speaker with zero turns.
    used: set[str] = {a.speaker_id for a in aligned}
    for a in aligned:
        used.update(a.secondary_speakers)
    ordered_ids = [labels[label] for label in labels if labels[label] in used]
    return ordered_ids, aligned
