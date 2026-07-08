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

from . import frontmatter, library, paths, render

SPEC_VERSION = library.SPEC_VERSION  # single source (library can't import pack; pack imports it)


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


_frontmatter = frontmatter.emit  # the pack sidecar's frontmatter (spec §5); shared emitter


def _label_of(speaker: dict) -> str:
    return speaker.get("display_name") or speaker["id"]


_FFMPEG_TIMEOUT = 300  # seconds; a slice/transcode of a long session shouldn't hang forever


def _to_opus(src: Path, dst: Path) -> bool:
    """Transcode ``src`` → opus at ``dst``. Returns False if the encoder is unavailable.

    A minimal/static ffmpeg without the libopus encoder is common; the caller falls back to
    the source clip (spec §4: the record references clips by role/UID, not codec) rather than
    losing the pack's audio — the voiceprint fold never needs the clip at all.
    """
    try:
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src),
             "-c:a", "libopus", "-b:a", "24k", str(dst)],
            check=True, timeout=_FFMPEG_TIMEOUT,
        )
        return True
    except subprocess.CalledProcessError:
        return False


def _coalesce(spans: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Merge overlapping/touching spans (sorted) so the select expression stays compact."""
    merged: list[tuple[float, float]] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _slice_channel(session_wav: Path, spans: list[tuple[float, float]], channel: int, dst: Path) -> None:
    """Slice ``spans`` (seconds) from one ``channel`` of a stereo wav → concatenated ``dst``.

    One ffmpeg pass: pan the channel to mono, ``asplit`` it per span, ``atrim`` each span (PTS
    reset), then ``concat``. This is *temporal* isolation — each clip holds only that speaker's
    speaking spans (the operator from the mic channel, a remote from the meeting channel by its
    diarized turns), a clean, sample-accurate voiceprint exemplar. Overlapping spans are
    coalesced first, and the graph is passed via ``-filter_complex_script`` (a file, not argv)
    so a long meeting's hundreds of turns can't blow the argv length limit (~500 spans inline).
    """
    merged = _coalesce(spans)
    n = len(merged)
    splits = "".join(f"[m{i}]" for i in range(n))
    graph = [f"[0:a]pan=mono|c0=c{channel},asplit={n}{splits}"]
    for i, (start, end) in enumerate(merged):
        graph.append(f"[m{i}]atrim=start={start:.3f}:end={end:.3f},asetpts=PTS-STARTPTS[a{i}]")
    graph.append("".join(f"[a{i}]" for i in range(n)) + f"concat=n={n}:v=0:a=1[o]")

    fd, script = tempfile.mkstemp(suffix=".txt")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(";".join(graph))
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(session_wav),
             "-filter_complex_script", script, "-map", "[o]", "-ar", "16000", "-ac", "1", str(dst)],
            check=True, timeout=_FFMPEG_TIMEOUT,
        )
    finally:
        os.unlink(script)


def build_speaker_clips(
    session_wav: Path,
    spans_by_id: dict[str, list[tuple[float, float]]],
    channel_by_id: dict[str, int],
    *,
    dst_dir: Path,
) -> dict[str, Path]:
    """Cut per-speaker isolated clips from a stereo session wav, keyed by canonical id.

    ``spans_by_id`` maps a speaker id (``S0`` …) to its speaking spans; ``channel_by_id``
    picks that speaker's source channel (mic vs meeting). Speakers with no spans are skipped.
    Returns ``{id: clip_wav}`` — ready to hand to :func:`write_pack` as its ``audio`` map.
    """
    clips: dict[str, Path] = {}
    for sid, spans in spans_by_id.items():
        if not spans:
            continue
        dst = dst_dir / f"{sid}.wav"
        _slice_channel(session_wav, spans, channel_by_id.get(sid, 1), dst)
        clips[sid] = dst
    return clips


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
    md_path: Path | None = None,
    pending: list[str] | None = None,
) -> PackResult:
    """Write one session pack: the loose ``.md`` sidecar + the self-describing blob.

    ``embeddings`` and ``audio`` are keyed by canonical speaker id (``S0``, ``S1`` …), as
    they appear in ``ir['speakers']``. Each clip is archived under ``audio/<id>.<ext>`` —
    keyed by the collision-free canonical id (not the display label, which two speakers can
    share), opus when the encoder is present else the source codec. The pack is written
    **active** (audio present).

    ``md_path`` pins the loose sidecar to an explicit path (e.g. the operator's ``-o``
    transcript) instead of deriving+disambiguating one under ``dest_dir``; the blob still
    lands beside it in ``dest_dir``.

    ``pending`` is the list of canonical speaker ids the operator flagged live to remember;
    it is stamped as ``pending_enrollment`` so ``pack enroll`` knows whom to name (ADR-0024).
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
    if pending:
        meta["pending_enrollment"] = list(pending)
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
                if _to_opus(src, opus):
                    tar.add(opus, arcname=f"audio/{sid}.opus", filter=_normalize)
                else:  # no libopus encoder → keep the source clip rather than lose the audio
                    tar.add(src, arcname=f"audio/{sid}{src.suffix}", filter=_normalize)
        os.replace(tmp, blob_path)
    finally:
        Path(tmp).unlink(missing_ok=True)

    # Loose sidecar written last, once the blob is durable. When not pinned to an explicit
    # path, derive one and disambiguate on a same-day + same-title collision (the common case
    # for repeated enrollment of one person) so a new pack's .md can't overwrite a prior one
    # and orphan its blob from loose-file discovery.
    if md_path is None:
        md_path = dest / f"{started:%Y-%m-%d}-{slug(title)}.md"
        if md_path.exists():
            md_path = dest / f"{started:%Y-%m-%d}-{slug(title)}-{uid}.md"
    md_path.write_text(sidecar)

    return PackResult(uid=uid, md_path=md_path, blob_path=blob_path)


def extract(blob_path: Path, *, only: list[str] | None = None) -> list[library.Voiceprint]:
    """Fold every speaker embedding in a pack into the durable library (spec §8.1).

    Universal over any pack: reads the embedding sidecar + record, maps each speaker id to
    its display name, and enrolls/compounds it under a stable ``<pack_uid>-<name>`` uid with
    a ``sources`` back-reference to this blob. Returns the resulting voiceprints.

    ``only`` restricts the fold to those canonical speaker ids — the guided live-enroll walk
    names one speaker at a time and folds just that one, so the speakers the operator chose
    *not* to name never litter the library with anonymous ``<pack_uid>-sN`` voiceprints. The
    default (``None``) folds every speaker, unchanged.

    Name is the identity key (spec §8.2's ``<pack_uid>-<name>``), so distinct people who
    share a display name — or the generic operator label ``You`` across capture packs — fold
    into one voiceprint. That is intended for a returning speaker; disambiguating genuine
    name collisions is left to teaching-mode adjudication (future ADR-0029).

    Raises ``ValueError`` on any pack that isn't extractable — a foreign pack with no
    ``embeddings.json`` sidecar (``KeyError``), a truncated/corrupt archive (``TarError``), a
    non-regular member where a file is expected (``extractfile`` → ``None``), or malformed
    JSON (``JSONDecodeError``) — rather than leaking an opaque traceback.
    """
    try:
        with tarfile.open(blob_path, "r:gz") as tar:
            record = tar.extractfile("record.ir.json")
            sidecar = tar.extractfile("embeddings.json")
            if record is None or sidecar is None:
                raise KeyError("record.ir.json/embeddings.json is not a regular file")
            ir = json.loads(record.read())
            embed_doc = json.loads(sidecar.read())
    except (KeyError, tarfile.TarError, json.JSONDecodeError) as e:
        raise ValueError(f"{blob_path.name}: not an extractable pack ({e})") from e

    pack_uid = embed_doc["pack_uid"]
    names = {s["id"]: _label_of(s) for s in ir["speakers"]}
    # Back-reference is a path relative to the library dir (where the voiceprint lives) → the
    # blob's ACTUAL location, so it resolves whether the pack sits in the canonical sessions
    # store or beside an operator's ``-o`` transcript (spec §9 relative-link convention).
    source_ref = os.path.relpath(blob_path.resolve(), paths.library_dir())

    only_set = set(only) if only is not None else None
    updates: list[library.Voiceprint] = []
    for sid, vector in embed_doc["vectors"].items():
        if only_set is not None and sid not in only_set:
            continue
        name = names.get(sid, sid)
        vp = library.enroll(name, vector, uid=f"{pack_uid}-{slug(name)}", source=source_ref)
        updates.append(vp)
    return updates


# ---- lifecycle: discover, inspect, relabel, link (spec §7–§9) ----------------


@dataclass
class PackInfo:
    """One discovered pack: the authoritative blob, its (optional) loose sidecar, its metadata.

    ``meta`` is the ``.md`` frontmatter — read from the loose sidecar when present, else from the
    blob's embedded ``session.md`` (spec §6: the embedded copy is authoritative if the loose one
    is lost). ``uid`` comes from the frontmatter ``id``, falling back to the blob filename (§3.1).
    """

    uid: str
    blob_path: Path
    md_path: Path | None
    meta: dict


def _uid_from_blob(name: str) -> str:
    """Recover the pack uid from a blob filename (`…-<uid>-blob.tar.gz`, spec §3.1)."""
    return name.removesuffix("-blob.tar.gz").rsplit("-", 1)[-1]


def _member_bytes(tar: tarfile.TarFile, name: str) -> bytes | None:
    """Read one archive member as bytes; ``None`` if absent or not a regular file."""
    try:
        f = tar.extractfile(name)
    except KeyError:
        return None
    return f.read() if f is not None else None


def read_record(blob_path: Path) -> dict:
    """The pack's Canonical IR (``record.ir.json``) — the source of truth."""
    with tarfile.open(blob_path, "r:gz") as tar:
        data = _member_bytes(tar, "record.ir.json")
    if data is None:
        raise ValueError(f"{blob_path.name}: no record.ir.json")
    return json.loads(data)


def _embedded_meta(blob_path: Path) -> dict:
    """Frontmatter from the blob's embedded ``session.md`` (spec §6 fallback)."""
    with tarfile.open(blob_path, "r:gz") as tar:
        data = _member_bytes(tar, "session.md")
    return frontmatter.parse(data.decode()) if data else {}


def find_packs(store: Path | None = None) -> list[PackInfo]:
    """Discover every pack under ``store`` (default: the sessions dir), newest blob first.

    Discovery is **recursive**: a `listen` session lands its pair in its own
    ``sessions/<id>/`` subdir (beside the operator's transcript) while an ``enroll`` pack lands
    flat in ``sessions/`` — both are found. Blobs are authoritative, so the walk keys on
    ``*-blob.tar.gz``; a loose ``.md`` sharing the blob's directory and naming it via its
    ``blob:`` frontmatter edge (§9) supplies the metadata, else the embedded ``session.md`` does
    (§6) — so a pack whose sidecar was deleted still lists. Ordered by the date-prefixed blob
    name, newest first, across subdirs.
    """
    store = store or paths.sessions_dir()
    if not store.exists():
        return []
    md_by_key: dict[tuple[Path, str], tuple[Path, dict]] = {}
    for md in store.rglob("*.md"):
        m = frontmatter.parse(md.read_text())
        blob = m.get("blob")
        if isinstance(blob, str):
            md_by_key[(md.parent, blob)] = (md, m)  # the pair shares a directory (spec §2)

    packs: list[PackInfo] = []
    for blob in sorted(store.rglob("*-blob.tar.gz"), key=lambda p: p.name, reverse=True):
        linked = md_by_key.get((blob.parent, blob.name))
        if linked is not None:
            md_path, meta = linked
        else:
            md_path, meta = None, _embedded_meta(blob)
        uid = meta.get("id") or _uid_from_blob(blob.name)
        packs.append(PackInfo(uid=str(uid), blob_path=blob, md_path=md_path, meta=meta))
    return packs


def load_pack(ident: str, store: Path | None = None) -> PackInfo:
    """Resolve one pack by uid (exact or unique prefix) or by a blob/`.md` path.

    Raises ``ValueError`` if nothing matches, or if a uid prefix is ambiguous.
    """
    p = Path(ident)
    if p.suffix == ".gz" and p.exists():  # a direct blob path
        meta = _embedded_meta(p)
        return PackInfo(str(meta.get("id") or _uid_from_blob(p.name)), p, None, meta)

    packs = find_packs(store)
    exact = [pk for pk in packs if pk.uid == ident]
    if exact:
        return exact[0]
    pref = [pk for pk in packs if pk.uid.startswith(ident)]
    if len(pref) == 1:
        return pref[0]
    if len(pref) > 1:
        raise ValueError(f"ambiguous pack id {ident!r}: matches {', '.join(pk.uid for pk in pref)}")
    raise ValueError(f"no pack matching {ident!r}")


def pack_details(pack: PackInfo) -> dict:
    """Per-speaker stats + audio presence for ``show`` — folds the IR turns by speaker.

    Returns ``{meta, has_audio, duration_s, speakers: [{id, name, turns, speech_s, clip}]}``.
    ``clip`` is whether an isolated ``audio/<id>.*`` exists (the audition/relabel substrate).
    """
    ir = read_record(pack.blob_path)
    with tarfile.open(pack.blob_path, "r:gz") as tar:
        members = tar.getnames()
    audio = {m for m in members if m.startswith("audio/")}

    stats: dict[str, dict] = {}
    for t in ir["turns"]:
        s = stats.setdefault(t["speaker_id"], {"turns": 0, "speech_s": 0.0})
        s["turns"] += 1
        s["speech_s"] += t["end"] - t["start"]

    speakers = []
    for sp in ir["speakers"]:
        st = stats.get(sp["id"], {"turns": 0, "speech_s": 0.0})
        has_clip = any(m.startswith(f"audio/{sp['id']}.") for m in audio)
        speakers.append({"id": sp["id"], "name": _label_of(sp),
                         "turns": st["turns"], "speech_s": st["speech_s"], "clip": has_clip})
    return {
        "meta": pack.meta,
        "has_audio": bool(audio),
        "duration_s": float(ir["source"]["duration_s"]),
        "speakers": speakers,
    }


def read_clip(pack: PackInfo, speaker_id: str, dst: Path) -> Path:
    """Extract a speaker's isolated clip (``audio/<id>.*``) from the blob to ``dst``.

    Returns the written path (``dst`` with the clip's real suffix). Raises ``ValueError`` if the
    pack carries no clip for that speaker (finalized/``--no-audio``, or an unknown id).
    """
    with tarfile.open(pack.blob_path, "r:gz") as tar:
        member = next((m for m in tar.getnames() if m.startswith(f"audio/{speaker_id}.")), None)
        if member is None:
            raise ValueError(f"pack {pack.uid} has no audio clip for {speaker_id!r}")
        data = _member_bytes(tar, member)
    if data is None:
        raise ValueError(f"pack {pack.uid}: clip {member} is not readable")
    out = dst.with_suffix(Path(member).suffix)
    out.write_bytes(data)
    return out


def _sidecar_for(ir: dict, meta: dict) -> str:
    """Rebuild the ``.md`` sidecar (frontmatter + rendered transcript) — mirrors write_pack."""
    return _frontmatter(meta) + "\n" + render.to_markdown(ir)


def _rewrite_blob(blob_path: Path, *, new_ir: dict, new_sidecar: str, drop_audio: bool = False) -> None:
    """Rewrite a blob in place, replacing record.ir.json + session.md, passing other members through.

    The one primitive under every mutation: relabel (replace the two documents) and, later,
    finalize (``drop_audio=True`` strips ``audio/``). Written to a temp sibling and atomically
    swapped, so a crash mid-rewrite can never truncate the authoritative blob. Members are
    re-normalized (mtime/owner stripped) exactly as write_pack does, so a rewrite leaks no
    host metadata.
    """
    tmp = blob_path.parent / f".{blob_path.name}.partial"
    try:
        with tarfile.open(blob_path, "r:gz") as src, tarfile.open(tmp, "w:gz") as dst:
            for member in src.getmembers():
                if member.name in ("record.ir.json", "session.md"):
                    continue  # the two authored documents are (re)written below, always
                elif drop_audio and member.name.startswith("audio/"):
                    continue
                elif member.isfile():
                    _add_bytes(dst, member.name, _member_bytes(src, member.name) or b"")
            # Write the two documents unconditionally — replacing them if the source carried them,
            # adding them if it didn't. A pack somehow missing its embedded session.md must not
            # come out of a rewrite still missing it (the §6 drift the loose/embedded split forbids).
            _add_bytes(dst, "record.ir.json", (json.dumps(new_ir, indent=2) + "\n").encode())
            _add_bytes(dst, "session.md", new_sidecar.encode())
        os.replace(tmp, blob_path)
    finally:
        Path(tmp).unlink(missing_ok=True)


def relabel(pack: PackInfo, speaker_id: str, name: str) -> PackInfo:
    """Rename a speaker in the pack: set its ``display_name`` and re-pack (blob + loose sidecar).

    The rewrite refreshes ``record.ir.json`` (the identity the next ``extract`` reads), the
    embedded ``session.md``, its ``participants`` mirror, and the loose ``.md`` view. This turns
    an anonymous diarized remote (``S1``) into a named speaker whose embedding + isolated clip
    then fold into the library under the right name — the capture→named-voiceprint path.
    """
    from .ir import validate_ir

    if not name.strip():
        raise ValueError("a voiceprint name cannot be empty")
    ir = read_record(pack.blob_path)
    spk = next((s for s in ir["speakers"] if s["id"] == speaker_id), None)
    if spk is None:
        have = ", ".join(s["id"] for s in ir["speakers"])
        raise ValueError(f"pack {pack.uid} has no speaker {speaker_id!r} (has: {have})")
    spk["display_name"] = name
    validate_ir(ir)  # stay schema-valid; a bad relabel fails here, before touching the blob

    meta = dict(pack.meta)
    meta["participants"] = [_label_of(s) for s in ir["speakers"]]
    sidecar = _sidecar_for(ir, meta)
    _rewrite_blob(pack.blob_path, new_ir=ir, new_sidecar=sidecar)
    if pack.md_path is not None:
        pack.md_path.write_text(sidecar)
    return PackInfo(pack.uid, pack.blob_path, pack.md_path, meta)


def delete_pack(pack: PackInfo) -> None:
    """Delete a pack: remove its blob and loose sidecar, tidying an emptied session subdir.

    Deletes the pack, **not** the identities it taught: a voiceprint compounds from several packs
    (its running-mean centroid + ``sources`` graph), so pruning one is a separate, deliberate act,
    not a side effect of dropping a session. A `listen` pack lives in its own ``sessions/<id>/``
    subdir; that dir is removed once emptied, but never the top-level sessions store itself.
    """
    pack.blob_path.unlink(missing_ok=True)
    if pack.md_path is not None:
        pack.md_path.unlink(missing_ok=True)
    parent = pack.blob_path.parent
    if parent.resolve() != paths.sessions_dir().resolve():
        try:
            parent.rmdir()  # only succeeds if now empty
        except OSError:
            pass


def set_pending(pack: PackInfo, speaker_ids: list[str]) -> PackInfo:
    """Rewrite the pack's ``pending_enrollment`` — the durable record of who to name.

    A non-empty list is stamped as ``pending_enrollment``; an empty list clears the key
    entirely. This is how the guided ``pack enroll`` walk records progress: speakers still
    to name stay listed (so the walk resumes), named ones are dropped. Meta-only, through the
    one atomic rewrite primitive, so a crash mid-walk can't corrupt the blob. A no-op (same
    list) skips the O(pack) rewrite.
    """
    old = pack.meta.get("pending_enrollment")
    new = list(speaker_ids) if speaker_ids else None
    if new == old:
        return pack
    meta = dict(pack.meta)
    if new is not None:
        meta["pending_enrollment"] = new
    else:
        meta.pop("pending_enrollment", None)
    ir = read_record(pack.blob_path)
    sidecar = _sidecar_for(ir, meta)
    _rewrite_blob(pack.blob_path, new_ir=ir, new_sidecar=sidecar)
    if pack.md_path is not None:
        pack.md_path.write_text(sidecar)
    return PackInfo(pack.uid, pack.blob_path, pack.md_path, meta)


def link_voiceprints(pack: PackInfo, voiceprints: list[library.Voiceprint]) -> PackInfo:
    """Write the session→voiceprint back-edge (spec §9) after an extract, closing the graph.

    ``extract`` records the voiceprint→session ``sources`` edge; this writes the matching
    ``voiceprints:`` edge (relative paths to ``library/<uid>.md``) onto the session's frontmatter,
    merged/deduped with any existing edges, in both the loose ``.md`` and the embedded copy.
    """
    if not voiceprints:
        return pack
    edges = {
        os.path.relpath((paths.library_dir() / f"{vp.uid}.md").resolve(), pack.blob_path.parent)
        for vp in voiceprints
    }
    existing = pack.meta.get("voiceprints") or []
    merged = sorted({*existing, *edges})
    if merged == existing:
        return pack
    meta = dict(pack.meta)
    meta["voiceprints"] = merged
    ir = read_record(pack.blob_path)
    sidecar = _sidecar_for(ir, meta)
    _rewrite_blob(pack.blob_path, new_ir=ir, new_sidecar=sidecar)
    if pack.md_path is not None:
        pack.md_path.write_text(sidecar)
    return PackInfo(pack.uid, pack.blob_path, pack.md_path, meta)
