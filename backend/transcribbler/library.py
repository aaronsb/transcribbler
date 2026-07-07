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
import os
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import frontmatter, paths
from .session_gallery import cosine

SPEC_VERSION = "0.1"


@dataclass
class Voiceprint:
    uid: str
    name: str
    centroid: list[float]  # 256-d running-mean embedding
    samples: int  # embeddings folded in — weights the mean and signals confidence
    updated: str  # ISO-8601 UTC
    sources: list[str] = field(default_factory=list)  # back-refs to the packs it came from


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


# A voiceprint is an OKF document (session-pack spec §8.2): a `<uid>.md` (frontmatter + a human
# body) plus a sibling `<uid>.vec.json` holding the 256-d centroid — the vector stays out of the
# YAML so the record reads/diffs cleanly. Records written before this (bare `<uid>.json`) are still
# read, and migrated to the md form the next time they're saved.
def _md_path(uid: str) -> Path:
    return paths.library_dir() / f"{uid}.md"


def _vec_path(uid: str) -> Path:
    return paths.library_dir() / f"{uid}.vec.json"


def _legacy_path(uid: str) -> Path:
    return paths.library_dir() / f"{uid}.json"


def _meta(vp: Voiceprint) -> dict:
    return {
        "spec_version": SPEC_VERSION,
        "uid": vp.uid,
        "type": "voiceprint",
        "name": vp.name,
        "samples": vp.samples,
        "updated": vp.updated,
        "sources": vp.sources,
    }


def _load_md(uid: str) -> Voiceprint | None:
    md, vec = _md_path(uid), _vec_path(uid)
    if not md.exists() or not vec.exists():  # a record without its vector is incomplete
        return None
    try:  # a corrupt record must be SKIPPED (return None), never crash load_all/best_match
        m = frontmatter.parse(md.read_text())
        centroid = json.loads(vec.read_text())
        sources = m.get("sources") or []
        if isinstance(sources, str):  # a hand-edited scalar, not a sequence — wrap, don't splat
            sources = [sources]
        return Voiceprint(
            uid=m.get("uid", uid),
            name=m.get("name", ""),
            centroid=centroid,
            samples=int(m.get("samples", 1)),
            updated=m.get("updated", ""),
            sources=list(sources),
        )
    except (ValueError, TypeError, OSError):
        return None


def _load_legacy(uid: str) -> Voiceprint | None:
    p = _legacy_path(uid)
    if not p.exists():
        return None
    try:
        return Voiceprint(**json.loads(p.read_text()))
    except (ValueError, TypeError):
        return None


def load(uid: str) -> Voiceprint | None:
    return _load_md(uid) or _load_legacy(uid)


def load_all() -> list[Voiceprint]:
    d = paths.library_dir()
    if not d.exists():
        return []
    out: dict[str, Voiceprint] = {}
    loaded: set[str] = set()  # stems whose .md record actually LOADED (not merely exists)
    for p in sorted(d.glob("*.md")):
        vp = _load_md(p.stem)
        if vp is not None:
            out[vp.uid] = vp
            loaded.add(p.stem)
    for p in sorted(d.glob("*.json")):
        if p.name.endswith(".vec.json"):
            continue  # a sibling vector, not a record
        if p.stem in loaded:
            continue  # a complete md copy loaded and wins; an INCOMPLETE md never shadows legacy
        vp = _load_legacy(p.stem)
        if vp is not None:
            out.setdefault(vp.uid, vp)
    return list(out.values())


def find_by_name(name: str) -> Voiceprint | None:
    for vp in load_all():
        if vp.name.lower() == name.lower():
            return vp
    return None


def _atomic_write(path: Path, text: str) -> None:
    """Write via a temp file + rename so a reader never sees a half-written record."""
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def save(vp: Voiceprint) -> None:
    paths.ensure(paths.library_dir())
    # Order matters for crash-safety: the vector lands first, then the .md (the "record exists"
    # signal, so it never appears without its vector), then the legacy .json is dropped last — so
    # every interrupted state is recoverable (vector-only → legacy still loads; complete md → md
    # wins over legacy). Each write is atomic (temp + rename), so no torn file is ever read.
    _atomic_write(_vec_path(vp.uid), json.dumps(vp.centroid))
    _atomic_write(_md_path(vp.uid), frontmatter.emit(_meta(vp)) + f"\n# {vp.name}\n")
    _legacy_path(vp.uid).unlink(missing_ok=True)


def _fold(centroid: list[float], count: int, emb: list[float]) -> list[float]:
    """Fold one embedding into a centroid as a running mean (matches SessionGallery).

    Requires matching dimensionality — ``zip`` would otherwise silently truncate to the
    shorter vector and corrupt the centroid (e.g. folding a new model's differently-sized
    embedding into an old print, which the enroll docstring flags).
    """
    if len(centroid) != len(emb):
        raise ValueError(f"embedding dim {len(emb)} != centroid dim {len(centroid)}")
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
