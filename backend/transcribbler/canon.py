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
    "You map diarized speaker ids (S1, S2, ...) to real names/roles using ONLY evidence "
    "present in the transcript. A speaker may be named by SELF-introduction (\"I'm Bob\") "
    "OR by another speaker addressing/mentioning them (\"thanks, Bob\"; \"what do you think, "
    "Sarah?\"; \"you've been listening to X\"). Use who-addresses-whom to attribute names to "
    "the right id. If a speaker's name is not determinable, leave display_name empty (\"\"). "
    'Every speaker_map entry MUST include an "evidence" field quoting the exact transcript '
    "text (verbatim, appears above) that justifies it. Do not guess names not in the text. "
    "Leave term_map empty []."
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


_NAME_MENTION = re.compile(r"\b(?:thanks|thank you|hey|hi|welcome|listening to|i'?m|i am|this is|my name is)\b", re.I)


def build_evidence(ir: dict, char_budget: int = 6000) -> str:
    """A chronological, speaker-tagged view so the model can attribute names across
    speakers (who addresses whom), not just from self-introductions.

    Whole transcript if it fits the budget; otherwise intros (head) + outros (tail)
    + turns that look like they mention/address a name — the places identities surface.
    """
    ids = [s["id"] for s in ir["speakers"]]
    lines = [f'[{t["speaker_id"]}] ({t["start"]:.0f}s) {t["text"].strip()}' for t in ir["turns"]]
    full = "\n".join(lines)

    if len(full) <= char_budget:
        body = full
    else:
        head, size = [], 0
        for ln in lines:
            if size + len(ln) > char_budget * 0.55:
                break
            head.append(ln)
            size += len(ln)
        mentions = [ln for t, ln in zip(ir["turns"], lines) if _NAME_MENTION.search(t["text"])][:40]
        tail = lines[-8:]  # outros often name the speaker
        seen, body_lines = set(), []
        for ln in head + mentions + tail:
            if ln not in seen:
                seen.add(ln)
                body_lines.append(ln)
        body = "\n".join(body_lines)

    return (
        f"Identify these diarized speakers: {', '.join(ids)}.\n\n"
        "Transcript (speaker-tagged):\n" + body
        + "\n\nProduce speaker_map (each entry's evidence must be a quote that appears "
        "verbatim above) and an empty term_map."
    )


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
    and the cited evidence appears verbatim *somewhere in the transcript* — so the model
    cannot invent the naming statement, but may attribute a name stated by another
    speaker (e.g. "thanks, Bob") to the right id. Otherwise the speaker is left unchanged
    (fallback). term_map is intentionally not applied yet.
    """
    corpus = " ".join(t["text"] for t in ir["turns"])

    proposals = {e["id"]: e for e in data.get("speaker_map", [])}
    new_speakers = []
    for sp in ir["speakers"]:
        e = proposals.get(sp["id"])
        name = (e or {}).get("display_name", "").strip()
        role = (e or {}).get("role", "").strip()
        conf = float((e or {}).get("confidence", 0.0))
        verified = bool(e) and conf >= min_confidence and evidence_supported(e.get("evidence", ""), corpus)

        if verified and (name or role):  # apply whichever attributes are present
            updated = {**sp, "source": "llm"}
            if name:
                updated["display_name"] = name
                updated["confidence"] = round(conf, 3)
            if role:
                updated["role"] = role
            ev = e.get("evidence", "").strip()
            if ev:
                updated["evidence"] = [ev]
            new_speakers.append(updated)
        else:
            new_speakers.append(sp)  # unchanged fallback

    return {**ir, "speakers": new_speakers}
