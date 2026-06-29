"""Canonicalization logic (ADR-0006): turn S1/S2 into names — faithfully.

The LLM emits only a mapping table (speaker_map [+ term_map]); this module builds
the compact evidence it reasons over, then *verifies* each proposed name against
the actual transcript before applying it. An unsupported or low-confidence name is
rejected and the speaker stays a fallback label. The LLM can therefore add names
but never distort the transcript: speaker names are metadata, and a claim that
isn't backed by the cited evidence is dropped.

Pure/deterministic (the LLM call lives in the core); golden-file testable.
"""
from __future__ import annotations

import re

SYSTEM_PROMPT = (
    "You map diarized speaker ids to real names/roles using ONLY evidence present in "
    "the transcript. If a speaker's name is not stated in the text, leave display_name "
    'empty (""). Every speaker_map entry MUST include an "evidence" field quoting the '
    "exact transcript text that justifies the name/role. Do not guess. Leave term_map empty []."
)

# A proposed name is applied only if its confidence clears this AND its evidence
# is actually found in that speaker's turns.
MIN_CONFIDENCE = 0.5
# Fraction of the evidence's words that must appear in the speaker's text.
EVIDENCE_SUPPORT_THRESHOLD = 0.6


def _normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", text.lower())


def _tokens(text: str) -> list[str]:
    return _normalize(text).split()


def build_evidence(ir: dict, max_turns_per_speaker: int = 4, max_chars: int = 600) -> str:
    """Compact, per-speaker representative turns for the LLM to name speakers from.

    Picks the first turn (intros live here) plus the longest turns (substantive
    statements), capped — keeps the prompt small enough for a local model.
    """
    by_speaker: dict[str, list[dict]] = {}
    for turn in ir["turns"]:
        by_speaker.setdefault(turn["speaker_id"], []).append(turn)

    blocks = []
    for sid in (s["id"] for s in ir["speakers"]):
        turns = by_speaker.get(sid, [])
        if not turns:
            continue
        chosen = _representative(turns, max_turns_per_speaker)
        lines = []
        used = 0
        for t in chosen:
            line = f'- ({t["start"]:.0f}s) "{t["text"].strip()}"'
            if used + len(line) > max_chars:
                break
            lines.append(line)
            used += len(line)
        blocks.append(f"[{sid}] representative turns:\n" + "\n".join(lines))

    return (
        "Diarized transcript excerpts (one block per speaker id):\n\n"
        + "\n\n".join(blocks)
        + "\n\nProduce speaker_map (each with an evidence quote) and an empty term_map."
    )


def _representative(turns: list[dict], k: int) -> list[dict]:
    first = turns[0]
    longest = sorted(turns, key=lambda t: len(t["text"]), reverse=True)
    chosen, seen = [first], {id(first)}
    for t in longest:
        if len(chosen) >= k:
            break
        if id(t) not in seen:
            chosen.append(t)
            seen.add(id(t))
    return sorted(chosen, key=lambda t: t["start"])


def evidence_supported(quote: str, speaker_text: str) -> bool:
    """True if enough of the cited quote's words actually appear in the speaker's text."""
    q = _tokens(quote)
    if not q:
        return False
    present = set(_tokens(speaker_text))
    hits = sum(1 for w in q if w in present)
    return hits / len(q) >= EVIDENCE_SUPPORT_THRESHOLD


def apply_canonicalization(
    ir: dict, data: dict, *, min_confidence: float = MIN_CONFIDENCE
) -> dict:
    """Apply verified speaker names to the IR's speakers (pure; returns a new dict).

    A name is applied only when: display_name is non-empty, confidence >= threshold,
    and the cited evidence is supported by that speaker's actual turns. Otherwise the
    speaker is left unchanged (fallback). term_map is intentionally not applied yet.
    """
    speaker_text = {sid: "" for sid in (s["id"] for s in ir["speakers"])}
    for t in ir["turns"]:
        speaker_text[t["speaker_id"]] = speaker_text.get(t["speaker_id"], "") + " " + t["text"]

    proposals = {e["id"]: e for e in data.get("speaker_map", [])}
    new_speakers = []
    for sp in ir["speakers"]:
        e = proposals.get(sp["id"])
        name = (e or {}).get("display_name", "").strip()
        conf = float((e or {}).get("confidence", 0.0))
        if (
            e
            and name
            and conf >= min_confidence
            and evidence_supported(e.get("evidence", ""), speaker_text.get(sp["id"], ""))
        ):
            updated = {**sp, "display_name": name, "source": "llm", "confidence": round(conf, 3)}
            role = e.get("role", "").strip()
            if role:
                updated["role"] = role
            ev = e.get("evidence", "").strip()
            if ev:
                updated["evidence"] = [ev]
            new_speakers.append(updated)
        else:
            new_speakers.append(sp)  # unchanged fallback

    return {**ir, "speakers": new_speakers}
