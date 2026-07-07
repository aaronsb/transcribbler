"""Session-scoped speaker stitching (ADR-0024 — session-only slice).

The diarizer labels speakers *per chunk* (`SPEAKER_00`…), so the same person gets
a different label in each chunk. This maps those chunk-local labels to
**session-stable** ids (`S1`, `S2`…) by matching each local speaker's voiceprint
embedding against running centroids: a match above threshold reuses the id and
refines the centroid; nothing above threshold mints a new id.

Session-only: the gallery lives for one capture run and is discarded — no
persistence and no cross-session matching (deferred to the persistent gallery of
ADR-0024/0026). The operator is not handled here; they are identified by channel.

Pure-Python (no numpy in the backend venv); embeddings are short lists of floats
and there are only a handful of speakers, so cost is negligible.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


def cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity of two equal-length vectors; 0.0 if either is zero-norm."""
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


@dataclass
class _Speaker:
    sid: str
    centroid: list[float]
    count: int  # embeddings folded in — weights the running mean


class SessionGallery:
    """Assigns session-stable speaker ids from per-chunk embeddings.

    ``threshold`` is the cosine floor for "same speaker" — below it, a voice is
    treated as new. It is embedding/mic dependent and meant to be tuned against
    real audio (ADR-0024 names threshold choice as a real decision).
    """

    def __init__(self, threshold: float = 0.5):
        self.threshold = threshold
        self._speakers: list[_Speaker] = []
        self._n = 0

    def _mint(self, centroid: list[float] | None) -> str:
        self._n += 1
        sid = f"S{self._n}"
        if centroid is not None:  # only matchable speakers join the gallery
            self._speakers.append(_Speaker(sid, list(centroid), 1))
        return sid

    @staticmethod
    def _refine(sp: _Speaker, emb: list[float]) -> None:
        """Fold an embedding into a speaker's centroid as a running mean."""
        n = sp.count
        sp.centroid = [(c * n + e) / (n + 1) for c, e in zip(sp.centroid, emb)]
        sp.count = n + 1

    def assign_chunk(self, speakers: list[dict]) -> dict[str, str]:
        """Map this chunk's local labels → session-stable ids.

        ``speakers`` is the daemon's per-chunk list of
        ``{"label": str, "embedding": list[float] | None}``. Returns
        ``{local_label: session_id}``. Matching is one-to-one within the chunk so
        two distinct local speakers never collapse onto one session id.
        """
        result: dict[str, str] = {}
        usable = [(s["label"], s["embedding"]) for s in speakers if s.get("embedding")]

        # Rank every (local, existing-session) pair by similarity, assign greedily
        # best-first, each local and each session used at most once this chunk.
        pairs: list[tuple[float, str, _Speaker]] = []
        for label, emb in usable:
            for sp in self._speakers:
                pairs.append((cosine(emb, sp.centroid), label, sp))
        pairs.sort(key=lambda p: p[0], reverse=True)

        matched: dict[str, _Speaker] = {}
        used_sessions: set[str] = set()
        for sim, label, sp in pairs:
            if sim < self.threshold:
                break  # sorted descending: no remaining pair can qualify
            if label in matched or sp.sid in used_sessions:
                continue
            matched[label] = sp
            used_sessions.add(sp.sid)

        emb_by_label = dict(usable)
        for label, emb in usable:
            sp = matched.get(label)
            if sp is not None:
                self._refine(sp, emb)
                result[label] = sp.sid
            else:
                result[label] = self._mint(emb)

        # Locals whose embedding was unusable (None) can't be stitched — give each a
        # fresh id that never joins the gallery, so it won't false-match later.
        for s in speakers:
            if s["label"] not in result:
                result[s["label"]] = self._mint(None)
        return result

    @property
    def speaker_count(self) -> int:
        return len(self._speakers)
