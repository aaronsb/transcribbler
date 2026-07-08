"""Render a Canonical IR to human-readable formats.

The IR is the source of truth; renderers are pure views over it. A speaker's label
is its display_name if named, else its id (S1, S2) — names live only in the
speakers[] table, so renderers resolve them by id at output time.
"""

from __future__ import annotations


def _label_map(ir: dict) -> dict[str, str]:
    return {s["id"]: (s.get("display_name") or s["id"]) for s in ir["speakers"]}


def _ts(seconds: float) -> str:
    s = max(0.0, float(seconds))
    h, rem = divmod(int(s), 3600)
    m, sec = divmod(rem, 60)
    ms = int(round((s - int(s)) * 1000))
    return f"{h:02d}:{m:02d}:{sec:02d}.{ms:03d}"


# Blocks whose weakest turn falls below this mean-token-probability are flagged in the
# transcript as likely-garbage spans worth a human's eye (ADR-0028 → ADR-0030 reconciliation).
LOW_CONFIDENCE = 0.6


def _merge_confidence(current: float | None, incoming: float | None) -> float | None:
    """A block is as trustworthy as its weakest attributed turn → keep the minimum."""
    if incoming is None:
        return current
    return incoming if current is None else min(current, incoming)


def _group_consecutive(turns: list[dict]) -> list[dict]:
    """Merge runs of consecutive turns by the same speaker into one block."""
    blocks: list[dict] = []
    for t in turns:
        if blocks and blocks[-1]["speaker_id"] == t["speaker_id"]:
            blocks[-1]["end"] = t["end"]
            blocks[-1]["text"] += " " + t["text"].strip()
            blocks[-1]["confidence"] = _merge_confidence(blocks[-1]["confidence"], t.get("confidence"))
        else:
            blocks.append(
                {
                    "speaker_id": t["speaker_id"],
                    "start": t["start"],
                    "end": t["end"],
                    "text": t["text"].strip(),
                    "confidence": t.get("confidence"),
                }
            )
    return blocks


def to_markdown(ir: dict) -> str:
    labels = _label_map(ir)
    src = ir.get("source", {})
    dur_min = src.get("duration_s", 0) / 60
    head = ["# Transcript", ""]
    if src.get("uri"):
        head.append(f"- **Source:** {src['uri']}")
    head.append(f"- **Duration:** {dur_min:.1f} min")
    be = ir.get("backend", {})
    head.append(
        f"- **Backend:** {be.get('asr', '?')}" + (f" + {be['diarizer']}" if be.get("diarizer") else "")
    )
    head.append(
        "- **Speakers:** "
        + ", ".join(
            f"{labels[s['id']]}" + (f" ({s['role']})" if s.get("role") else "") for s in ir["speakers"]
        )
        or "—"
    )
    head.append("")

    body = []
    for b in _group_consecutive(ir["turns"]):
        conf = b["confidence"]
        flag = f"  ⚠ low confidence ({conf:.2f})" if conf is not None and conf < LOW_CONFIDENCE else ""
        body.append(f"**{labels.get(b['speaker_id'], b['speaker_id'])}** [{_ts(b['start'])}]{flag}")
        body.append(b["text"])
        body.append("")
    return "\n".join(head + body).rstrip() + "\n"


def to_vtt(ir: dict) -> str:
    labels = _label_map(ir)
    out = ["WEBVTT", ""]
    for t in ir["turns"]:
        name = labels.get(t["speaker_id"], t["speaker_id"])
        out.append(f"{_ts(t['start'])} --> {_ts(t['end'])}")
        out.append(f"<v {name}>{t['text'].strip()}")
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def render(ir: dict, fmt: str) -> str:
    import json

    if fmt == "json":
        return json.dumps(ir, indent=2, ensure_ascii=False) + "\n"
    if fmt in ("md", "markdown"):
        return to_markdown(ir)
    if fmt == "vtt":
        return to_vtt(ir)
    raise ValueError(f"unknown format: {fmt!r} (use json|md|vtt)")
