"""Session-pack persistence — the one unit of persistence (docs/specs/session-pack.md v0.1).

A pack is *just files* (OKF lineage): a human-friendly ``.md`` sidecar (YAML frontmatter +
rendered transcript) plus a verbose-named self-describing ``…-blob.tar.gz``:

    <YYYY-MM-DD-HHMMSS-start-length-uid>-blob.tar.gz
    ├── record.ir.json     # the Canonical IR — SOURCE OF TRUTH (schema-valid; ADR-0006)
    ├── session.md         # a copy of the loose .md at creation → self-describing (spec §6)
    ├── audio/<label>.opus # speaker-isolated clips (active packs; substrate for re-extract)
    └── embeddings.json     # per-speaker embedding sidecar (spec §8.2: vectors "in a sibling")

The blob is authoritative and stands alone; the loose ``.md`` is a convenience view. An
**enrollment** is not a special format — it is a single-speaker pack tagged ``training``.

``extract(blob)`` is the universal operation over *any* pack: read its embeddings, fold them
into the durable voiceprint library, and record the back-reference. Kept dependency-light
(stdlib ``tarfile`` + ``ffmpeg`` for opus) and numpy-free, matching library.py/session_gallery.
"""

from __future__ import annotations

import io
import json
import os
import re
import subprocess
import tarfile
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import library, paths, render

SPEC_VERSION = "0.1"


def new_uid() -> str:
    """Short pack id — first 6 hex of a UUID4 (spec §3.1)."""
    return uuid.uuid4().hex[:6]


def slug(text: str) -> str:
    """Filesystem-friendly slug for titles and voiceprint names."""
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s or "untitled"


@dataclass
class PackResult:
    uid: str
    md_path: Path
    blob_path: Path


def blob_name(started: datetime, *, start_s: int, length_s: int, uid: str) -> str:
    """The verbose, sortable, self-identifying blob filename (spec §3.1)."""
    return f"{started:%Y-%m-%d-%H%M%S}-{start_s}-{length_s}-{uid}-blob.tar.gz"


def _frontmatter(meta: dict) -> str:
    """Minimal deterministic YAML frontmatter for flat scalars + string lists.

    Deliberately hand-rolled: the sidecar frontmatter is a small, closed set of flat
    fields (spec §5), so we avoid a YAML dependency and keep output stable/diffable.
    """
    lines = ["---"]
    for key, value in meta.items():
        if value is None:
            continue
        if isinstance(value, list):
            if not value:
                lines.append(f"{key}: []")
            else:
                lines.append(f"{key}:")
                lines.extend(f"  - {item}" for item in value)
        else:
            lines.append(f"{key}: {value}")
    lines.append("---")
    return "\n".join(lines) + "\n"


def _label_of(speaker: dict) -> str:
    return speaker.get("display_name") or speaker["id"]


def _to_opus(src: Path, dst: Path) -> None:
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src),
         "-c:a", "libopus", "-b:a", "24k", str(dst)],
        check=True,
    )


def _normalize(info: tarfile.TarInfo) -> tarfile.TarInfo:
    """Strip mtime/owner from a member so the blob carries no host-identifying metadata.

    (The gzip wrapper still stamps its own header mtime, so the archive is not byte-for-byte
    reproducible — but its *contents* no longer leak the source file's timestamps or owner.)
    """
    info.mtime = 0
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    return info


def _add_bytes(tar: tarfile.TarFile, arcname: str, data: bytes) -> None:
    info = _normalize(tarfile.TarInfo(arcname))
    info.size = len(data)
    tar.addfile(info, io.BytesIO(data))


def write_pack(
    ir: dict,
    *,
    title: str,
    tags: list[str],
    embeddings: dict[str, list[float]],
    audio: dict[str, Path],
    started: datetime | None = None,
    start_s: int = 0,
    uid: str | None = None,
    dest_dir: Path | None = None,
) -> PackResult:
    """Write one session pack: the loose ``.md`` sidecar + the self-describing blob.

    ``embeddings`` and ``audio`` are keyed by canonical speaker id (``S0``, ``S1`` …), as
    they appear in ``ir['speakers']``. ``audio`` sources are transcoded to opus inside the
    blob under ``audio/<label>.opus``. The pack is written **active** (audio present).
    """
    started = started or datetime.now(timezone.utc)
    uid = uid or new_uid()
    length_s = int(ir["source"]["duration_s"])
    dest = paths.ensure(dest_dir or paths.sessions_dir())

    archive = blob_name(started, start_s=start_s, length_s=length_s, uid=uid)
    blob_path = dest / archive
    label_by_id = {s["id"]: _label_of(s) for s in ir["speakers"]}
    meta = {
        "spec_version": SPEC_VERSION,
        "id": uid,
        "type": "session_pack",
        "timestamp": started.isoformat(timespec="seconds").replace("+00:00", "Z"),
        "title": title,
        "start": start_s,
        "length": length_s,
        "state": "active",
        "tags": list(tags),
        "participants": [label_by_id[s["id"]] for s in ir["speakers"]],
        "blob": archive,
    }
    sidecar = _frontmatter(meta) + "\n" + render.to_markdown(ir)
    embed_doc = {"spec_version": SPEC_VERSION, "pack_uid": uid, "vectors": embeddings}

    # Build the blob to a temp file, transcode clips into a scratch dir, then atomically
    # move it into place. A crash or an ffmpeg failure mid-write can never leave a loose
    # .md pointing at a truncated blob, and no stray .opus is left beside the source audio.
    tmp = dest / f".{archive}.partial"
    try:
        with tempfile.TemporaryDirectory() as clips, tarfile.open(tmp, "w:gz") as tar:
            _add_bytes(tar, "record.ir.json", (json.dumps(ir, indent=2) + "\n").encode())
            _add_bytes(tar, "session.md", sidecar.encode())
            _add_bytes(tar, "embeddings.json", (json.dumps(embed_doc, indent=2) + "\n").encode())
            for sid, src in audio.items():
                if not src.exists():
                    continue
                opus = Path(clips) / f"{sid}.opus"
                _to_opus(src, opus)
                tar.add(opus, arcname=f"audio/{label_by_id.get(sid, sid)}.opus", filter=_normalize)
        os.replace(tmp, blob_path)
    finally:
        Path(tmp).unlink(missing_ok=True)

    # Loose sidecar written last, once the blob is durable. Disambiguate on a same-day +
    # same-title collision (the common case for repeated enrollment of one person) so a new
    # pack's .md can't overwrite a prior one and orphan its blob from loose-file discovery.
    md_path = dest / f"{started:%Y-%m-%d}-{slug(title)}.md"
    if md_path.exists():
        md_path = dest / f"{started:%Y-%m-%d}-{slug(title)}-{uid}.md"
    md_path.write_text(sidecar)

    return PackResult(uid=uid, md_path=md_path, blob_path=blob_path)


def extract(blob_path: Path) -> list[library.Voiceprint]:
    """Fold every speaker embedding in a pack into the durable library (spec §8.1).

    Universal over any pack: reads the embedding sidecar + record, maps each speaker id to
    its display name, and enrolls/compounds it under a stable ``<pack_uid>-<name>`` uid with
    a ``sources`` back-reference to this blob. Returns the resulting voiceprints.

    Name is the identity key (spec §8.2's ``<pack_uid>-<name>``), so distinct people who
    share a display name — or the generic operator label ``You`` across capture packs — fold
    into one voiceprint. That is intended for a returning speaker; disambiguating genuine
    name collisions is left to teaching-mode adjudication (future ADR-0029).

    Raises ``ValueError`` on a pack that isn't extractable (e.g. a foreign pack with no
    ``embeddings.json`` sidecar) rather than an opaque ``KeyError``.
    """
    try:
        with tarfile.open(blob_path, "r:gz") as tar:
            ir = json.loads(tar.extractfile("record.ir.json").read())
            embed_doc = json.loads(tar.extractfile("embeddings.json").read())
    except KeyError as e:
        raise ValueError(f"{blob_path.name}: not an extractable pack (missing {e})") from e

    pack_uid = embed_doc["pack_uid"]
    names = {s["id"]: _label_of(s) for s in ir["speakers"]}
    source_ref = f"../sessions/{blob_path.name}"

    updates: list[library.Voiceprint] = []
    for sid, vector in embed_doc["vectors"].items():
        name = names.get(sid, sid)
        vp = library.enroll(name, vector, uid=f"{pack_uid}-{slug(name)}", source=source_ref)
        updates.append(vp)
    return updates
