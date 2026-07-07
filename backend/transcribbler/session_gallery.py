"""Session-scoped speaker stitching (ADR-0024 session slice; ADR-0027 linking).

The diarizer labels speakers *per inference* (`SPEAKER_00`…), so the same person
gets a different label in each window. This maps those window-local labels to
**session-stable** ids (`S1`, `S2`…).

Two linkers, in priority order (ADR-0027):

1. **Temporal** (primary). With overlapping diarization windows, consecutive
   windows share a span of *identical* audio. A label active in that shared span
   is linked to whichever session id owned the same time in the previous window —
   alignment by *when*, which is far more robust than comparing embeddings.
2. **Embedding** (fallback). A label absent from the shared span (a speaker who
   just started talking) has no temporal anchor, so it is matched by cosine against
   running per-speaker centroids; no match mints a new id.

Session-only: the gallery lives for one capture run and is discarded — no
persistence and no cross-session matching (deferred to the persistent gallery of
ADR-0024/0026). The operator is not handled here; they are identified by channel.

Pure-Python (no numpy in the backend venv); embeddings are short lists of floats
and there are only a handful of speakers, so cost is negligible.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass

# A window-local label must own at least this many seconds of the shared span to
# be temporally linked — guards against incidental sub-second overlaps.
_MIN_LINK_S = 0.5


def cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity of two equal-length vectors; 0.0 if either is zero-norm."""
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def _interval_overlap(a0: float, a1: float, b0: float, b1: float) -> float:
    return max(0.0, min(a1, b1) - max(a0, b0))


@dataclass
class _Speaker:
    sid: str
    centroid: list[float]
    count: int  # embeddings folded in — weights the running mean


class SessionGallery:
    """Assigns session-stable speaker ids from per-window diarizer output.

    ``threshold`` is the cosine floor for the *embedding fallback* — below it, a
    voice with no temporal anchor is treated as new. It is embedding/mic dependent
    and meant to be tuned against real audio (ADR-0024 names threshold choice as a
    real decision).
    """

    def __init__(self, threshold: float = 0.5, *, on_new_speaker: Callable[[str], None] | None = None):
        self.threshold = threshold
        self._on_new_speaker = on_new_speaker
        self._speakers: list[_Speaker] = []
        self._by_sid: dict[str, _Speaker] = {}
        self._n = 0

    def _mint(self, centroid: list[float] | None) -> str:
        self._n += 1
        sid = f"S{self._n}"
        if centroid is not None:  # only matchable speakers join the gallery
            sp = _Speaker(sid, list(centroid), 1)
            self._speakers.append(sp)
            self._by_sid[sid] = sp
            if self._on_new_speaker is not None:
                self._on_new_speaker(sid)
        return sid

    @staticmethod
    def _refine(sp: _Speaker, emb: list[float]) -> None:
        """Fold an embedding into a speaker's centroid as a running mean."""
        n = sp.count
        sp.centroid = [(c * n + e) / (n + 1) for c, e in zip(sp.centroid, emb)]
        sp.count = n + 1

    def _match_embeddings(
        self, usable: list[tuple[str, list[float]]], used_sids: set[str]
    ) -> dict[str, _Speaker]:
        """Greedy best-first cosine match of labels to *unused* gallery speakers."""
        pairs: list[tuple[float, str, _Speaker]] = []
        for label, emb in usable:
            for sp in self._speakers:
                if sp.sid in used_sids:
                    continue
                pairs.append((cosine(emb, sp.centroid), label, sp))
        pairs.sort(key=lambda p: p[0], reverse=True)

        matched: dict[str, _Speaker] = {}
        taken = set(used_sids)
        for sim, label, sp in pairs:
            if sim < self.threshold:
                break  # sorted descending: no remaining pair can qualify
            if label in matched or sp.sid in taken:
                continue
            matched[label] = sp
            taken.add(sp.sid)
        return matched

    @staticmethod
    def _temporal_link(
        local_turns: list[tuple[float, float, str]],
        prev_sid_turns: list[tuple[float, float, str]],
        shared_span: tuple[float, float],
    ) -> dict[str, str]:
        """Link this window's local labels to prior session ids by shared-span time.

        For each (local label, previous session id) pair, accumulate the seconds
        their turns overlap *within* the shared span, then assign greedily best-first,
        one-to-one, above ``_MIN_LINK_S``.
        """
        lo, hi = shared_span
        acc: dict[tuple[str, str], float] = {}
        for a0, a1, label in local_turns:
            ca0, ca1 = max(a0, lo), min(a1, hi)
            if ca1 <= ca0:
                continue
            for b0, b1, sid in prev_sid_turns:
                ov = _interval_overlap(ca0, ca1, b0, b1)
                if ov > 0.0:
                    acc[(label, sid)] = acc.get((label, sid), 0.0) + ov

        mapping: dict[str, str] = {}
        used_labels: set[str] = set()
        used_sids: set[str] = set()
        for (label, sid), ov in sorted(acc.items(), key=lambda kv: kv[1], reverse=True):
            if ov < _MIN_LINK_S:
                break
            if label in used_labels or sid in used_sids:
                continue
            mapping[label] = sid
            used_labels.add(label)
            used_sids.add(sid)
        return mapping

    def _assign(self, speakers: list[dict], temporal: dict[str, str]) -> dict[str, str]:
        """Resolve a window's local labels to session ids: temporal, then embedding, then mint."""
        result: dict[str, str] = {}
        used_sids: set[str] = set()
        emb_by_label = {s["label"]: s["embedding"] for s in speakers if s.get("embedding")}

        # 1. temporal links win outright; refine the linked speaker's centroid
        for label, sid in temporal.items():
            result[label] = sid
            used_sids.add(sid)
            sp = self._by_sid.get(sid)
            if sp is not None and emb_by_label.get(label):
                self._refine(sp, emb_by_label[label])

        # 2. embedding fallback for labels with no temporal anchor
        usable = [(lbl, emb) for lbl, emb in emb_by_label.items() if lbl not in result]
        matched = self._match_embeddings(usable, used_sids)
        for label, emb in usable:
            sp = matched.get(label)
            if sp is not None:
                self._refine(sp, emb)
                result[label] = sp.sid
                used_sids.add(sp.sid)
            else:
                result[label] = self._mint(emb)

        # 3. labels whose embedding was unusable can't be stitched — fresh id that
        #    never joins the gallery, so it won't false-match later
        for s in speakers:
            if s["label"] not in result:
                result[s["label"]] = self._mint(None)
        return result

    def assign_chunk(self, speakers: list[dict]) -> dict[str, str]:
        """Map an isolated chunk's local labels → session ids by embedding only.

        Used for the cold-start window (no predecessor to link against). ``speakers``
        is the daemon's ``[{"label", "embedding"}]``; returns ``{local_label: sid}``.
        """
        return self._assign(speakers, {})

    def assign_window(
        self,
        speakers: list[dict],
        local_turns: list[tuple[float, float, str]],
        prev_sid_turns: list[tuple[float, float, str]],
        shared_span: tuple[float, float],
    ) -> dict[str, str]:
        """Map an overlapping window's local labels → session ids (ADR-0027).

        Temporal-links labels against ``prev_sid_turns`` (the previous window's turns,
        already session-mapped, absolute seconds) within ``shared_span``, then falls
        back to embedding matching for the remainder. ``local_turns`` is this window's
        ``[(start, end, local_label)]`` in absolute seconds.
        """
        temporal = self._temporal_link(local_turns, prev_sid_turns, shared_span)
        return self._assign(speakers, temporal)

    @property
    def speaker_count(self) -> int:
        return len(self._speakers)
