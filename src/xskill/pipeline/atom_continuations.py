"""Versioned continuation locators; persisted Atoms remain immutable evidence."""

from __future__ import annotations

import hashlib
import json
import tempfile
from dataclasses import replace
from pathlib import Path

from xskill.pipeline.atom import AtomTask


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _path(root: Path, atom: AtomTask) -> Path:
    # Hash the identity rather than use an externally supplied ID as a path.
    return root / atom.traj_id / "continuations" / f"{_digest(atom.atom_id)}.json"


def project_continuation(root: Path, atom: AtomTask, lines: list[str]) -> AtomTask:
    """Return an evidence-only view, never a replacement persisted Atom."""
    path = _path(root, atom)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return atom
    record = json.loads(text)
    if not isinstance(record, dict) or record.get("schema_version") != 1:
        raise ValueError("invalid Atom continuation schema")
    end = record.get("end")
    if (
        record.get("atom_id") != atom.atom_id
        or record.get("start") != atom.offset_end
        or type(end) is not int
        or not atom.offset_end < end <= len(lines) + 1
    ):
        raise ValueError("invalid Atom continuation locator")
    original = "".join(lines[atom.offset_start - 1 : atom.offset_end - 1])
    segment = "".join(lines[atom.offset_end - 1 : end - 1])
    if original != atom.raw_segment or record.get("version") != _digest(
        original + segment
    ):
        raise ValueError("Atom continuation source changed; evidence must be reviewed")
    return replace(atom, offset_end=end, raw_segment=original + segment)


def save_continuation(root: Path, atom: AtomTask, lines: list[str], end: int) -> None:
    """Atomically advance a cumulative, content-verified continuation revision.

    The splitter must stop at the next genuine User boundary. No new Atom or
    contribution is emitted; the existing Task source-revision mechanism handles
    evidence invalidation. A repeated write of the same revision is a no-op.
    """
    current = project_continuation(root, atom, lines)
    if not current.offset_end <= end <= len(lines) + 1:
        raise ValueError("Atom continuation cannot move backwards or beyond EOF")
    if current.offset_end == end:
        return
    original = "".join(lines[atom.offset_start - 1 : atom.offset_end - 1])
    if original != atom.raw_segment:
        raise ValueError("persisted Atom source changed; continuation refused")
    segment = "".join(lines[atom.offset_end - 1 : end - 1])
    record = {
        "schema_version": 1,
        "atom_id": atom.atom_id,
        "start": atom.offset_end,
        "end": end,
        "version": _digest(original + segment),
    }
    path = _path(root, atom)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=".continuation-",
        delete=False,
    ) as stream:
        temporary = Path(stream.name)
        stream.write(json.dumps(record, sort_keys=True) + "\n")
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
