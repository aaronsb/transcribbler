"""Durable speaker voiceprint library (ADR-0023/0024 slice; build-first spike).

A voiceprint is a *named, persistent* speaker identity: a running-mean centroid in
the diarizer's 256-d embedding space, accumulated across sessions. It is the durable
counterpart to the session-only ``SessionGallery`` — the compounding asset that lets
a returning speaker be recognised no matter how their audio varies session to session.

Stored one JSON per voiceprint under the XDG data dir (``paths.library_dir()``). First
cut: plain JSON, no encryption yet (ADR-0023 envelope-at-rest is a follow-up) and no
audio clips yet — just the vector, a name, and provenance, enough to enroll and match.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import paths
from .session_gallery import cosine


@dataclass
class Voiceprint:
    uid: str
    name: str
    centroid: list[float]  # 256-d running-mean embedding
    samples: int  # embeddings folded in — weights the mean and signals confidence
    updated: str  # ISO-8601 UTC
    sources: list[str] = field(default_factory=list)  # back-refs to the packs it came from


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _path(uid: str) -> Path:
    return paths.library_dir() / f"{uid}.json"


def load(uid: str) -> Voiceprint | None:
    p = _path(uid)
    if not p.exists():
        return None
    return Voiceprint(**json.loads(p.read_text()))


def load_all() -> list[Voiceprint]:
    d = paths.library_dir()
    if not d.exists():
        return []
    out: list[Voiceprint] = []
    for p in sorted(d.glob("*.json")):
        try:
            out.append(Voiceprint(**json.loads(p.read_text())))
        except (ValueError, TypeError):
            pass  # ignore anything that isn't a voiceprint record
    return out


def find_by_name(name: str) -> Voiceprint | None:
    for vp in load_all():
        if vp.name.lower() == name.lower():
            return vp
    return None


def save(vp: Voiceprint) -> None:
    paths.ensure(paths.library_dir())
    _path(vp.uid).write_text(json.dumps(asdict(vp), indent=2))


def _fold(centroid: list[float], count: int, emb: list[float]) -> list[float]:
    """Fold one embedding into a centroid as a running mean (matches SessionGallery)."""
    return [(c * count + e) / (count + 1) for c, e in zip(centroid, emb)]


def enroll(
    name: str,
    embedding: list[float],
    *,
    uid: str | None = None,
    source: str | None = None,
) -> Voiceprint:
    """Create a named voiceprint, or fold ``embedding`` into an existing one.

    Matching is by name (case-insensitive), falling back to ``uid`` — so re-enrolling the
    same person from a *new* pack (which carries a fresh ``uid`` seed) still compounds into
    their one voiceprint rather than minting a duplicate. Compounding makes the centroid a
    better estimate of that speaker's cloud and raises ``samples`` (a confidence signal).

    ``uid`` seeds the id of a brand-new voiceprint (the pack convention ``<pack_uid>-<name>``,
    spec §8.2); once set it is stable for that speaker's life. ``source`` is a back-reference
    to the originating pack, appended (deduped) to the voiceprint's ``sources`` graph edge.

    Folding a ``source`` is **idempotent**: re-extracting a pack already recorded in
    ``sources`` is a no-op, so ``samples`` (the confidence signal) can't be inflated by a
    repeated ``extract`` (spec §8.1). Re-embedding a recorded pack under a *new model* is a
    known v0.1 limitation — it needs per-source replacement, not another fold.
    """
    existing = find_by_name(name) or (load(uid) if uid else None)
    if existing is not None:
        if source and source in existing.sources:
            return existing  # already folded this pack — idempotent
        sources = [*existing.sources, source] if source else existing.sources
        vp = Voiceprint(
            uid=existing.uid,
            name=name,
            centroid=_fold(existing.centroid, existing.samples, embedding),
            samples=existing.samples + 1,
            updated=_now(),
            sources=sources,
        )
    else:
        vp = Voiceprint(
            uid or uuid.uuid4().hex[:12],
            name,
            list(embedding),
            1,
            _now(),
            [source] if source else [],
        )
    save(vp)
    return vp


def best_match(embedding: list[float], *, threshold: float = 0.5) -> tuple[Voiceprint, float] | None:
    """Nearest enrolled voiceprint to ``embedding`` by cosine, if it clears ``threshold``."""
    best: tuple[Voiceprint, float] | None = None
    for vp in load_all():
        sim = cosine(embedding, vp.centroid)
        if sim >= threshold and (best is None or sim > best[1]):
            best = (vp, sim)
    return best
