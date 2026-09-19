from __future__ import annotations

import hashlib
from pathlib import Path


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def sha256_sorted_uids(uids: list[str]) -> str:
    text = "\n".join(sorted(uids)) + "\n"
    return sha256_bytes(text.encode("utf-8"))


def hash_skill(*, skill_dir: Path | None = None, skill_file: Path | None = None) -> str | None:
    if skill_file is not None:
        path = Path(skill_file)
        if not path.is_file():
            raise FileNotFoundError(f"skill file not found: {path}")
        return sha256_file(path)
    if skill_dir is None:
        return None
    root = Path(skill_dir)
    if not root.is_dir():
        raise FileNotFoundError(f"skill dir not found: {root}")
    digest = hashlib.sha256()
    found = False
    for path in sorted(root.rglob("SKILL.md")):
        found = True
        rel = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(rel)
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\n")
    if not found:
        for path in sorted(p for p in root.rglob("*") if p.is_file()):
            rel = path.relative_to(root).as_posix().encode("utf-8")
            digest.update(rel)
            digest.update(b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\n")
            found = True
    if not found:
        raise FileNotFoundError(f"no skill files under {root}")
    return digest.hexdigest()


def short_sha(digest: str | None) -> str:
    if not digest:
        return "none"
    if len(digest) < 8:
        return digest
    return f"{digest[:4]}...{digest[-4:]}"
