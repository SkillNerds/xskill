"""Read-only publication checks for restricted distillation artifacts.

Existence checks deliberately do not execute generated scripts. An explicit ref
is inspected in Git's object store and never repaired from the working tree.
"""
from __future__ import annotations

import os
import posixpath
import re
import stat
from pathlib import Path
from urllib.parse import unquote, urlsplit

from dulwich.errors import NotGitRepository
from dulwich.objects import Commit, Tree

from xskill.skill.frontmatter import FrontmatterError, parse_strict
from xskill.skill.git import (
    BABY_STUB_BODY_MARKER, _lookup_path_in_tree, _open_repo, _read_blob,
    _resolve_rev,
)


class ArtifactValidationError(ValueError):
    """A draft or committed artifact cannot be reported as deliverable."""


_INLINE_LINK = re.compile(
    r"!?\[[^\]\n]*\]\(\s*(<[^>\n]*>|(?:[^()\s]|\([^()\n]*\))+)"
    r"(?:\s+[\"'][^\n]*?[\"'])?\s*\)"
)
_LINK_DEFINITION = re.compile(r"(?m)^\s{0,3}\[[^\]\n]+\]:\s*(<[^>\n]*>|\S+)")
_EXPLICIT_REFERENCE = re.compile(
    r"(?<![\w/\\])(?:[/\\]+)?(?:[^\s`\"'<>\[\]()/\\]+[/\\])*"
    r"(?:scripts|references)[/\\][^\s`\"'<>\[\]()]+"
)
_URL = re.compile(r"[a-zA-Z][a-zA-Z0-9+.-]*://[^\s<>`]+")


def _safe_fs_path(path: Path) -> os.stat_result:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
        raise ArtifactValidationError(f"unsafe link/reparse point: {path}")
    if stat.S_ISREG(info.st_mode) and info.st_nlink > 1:
        raise ArtifactValidationError(f"unsafe hard link: {path}")
    return info


def _workspace_files(root: Path) -> dict[str, bytes | None]:
    """Inventory regular files without following links or reading support code."""
    files: dict[str, bytes | None] = {}

    def visit(directory: Path) -> None:
        for child in directory.iterdir():
            if directory == root and child.name == ".git":
                continue
            info = _safe_fs_path(child)
            if stat.S_ISDIR(info.st_mode):
                visit(child)
            elif stat.S_ISREG(info.st_mode):
                files[child.relative_to(root).as_posix()] = None
            else:
                raise ArtifactValidationError(f"unsafe file type: {child}")

    visit(root)
    return files


def _tree_files(repo, tree_sha: bytes, prefix: str = "") -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    tree = repo[tree_sha]
    if not isinstance(tree, Tree):
        raise ArtifactValidationError("commit does not contain a readable tree")
    for entry in tree.items():
        name = entry.path.decode("utf-8", errors="strict")
        if name in (".", "..", ".git") or any(c in name for c in ("/", "\\", ":", "\0")):
            raise ArtifactValidationError(f"unsafe committed path: {prefix}{name}")
        path = prefix + name
        if stat.S_ISDIR(entry.mode):
            files.update(_tree_files(repo, entry.sha, path + "/"))
        elif stat.S_ISREG(entry.mode):
            files[path] = entry.sha
        else:
            raise ArtifactValidationError(f"unsafe committed link/file type: {path}")
    return files


def _local_reference(raw: str, parent: str) -> str | None:
    raw = unquote(raw.strip().strip("<>"))
    if not raw or raw.startswith("#"):
        return None
    # Check drives before URL schemes: C:/... is a local unsafe path.
    if re.match(r"^[A-Za-z]:", raw) or raw.startswith(("/", "\\", "~")) or "\\" in raw:
        raise ArtifactValidationError(f"unsafe local reference: {raw}")
    parsed = urlsplit(raw)
    if parsed.scheme:
        if parsed.scheme.lower() == "file":
            raise ArtifactValidationError(f"unsafe local reference: {raw}")
        return None
    path = parsed.path
    if not path:
        return None
    if any(c in path for c in ("{", "}", "$", "*", "?", "\0")):
        raise ArtifactValidationError(f"unverified dynamic local reference: {raw}")
    normalized = posixpath.normpath(posixpath.join(parent, path))
    if normalized == ".." or normalized.startswith("../") or ".git" in normalized.split("/") or ":" in normalized:
        raise ArtifactValidationError(f"unsafe local reference: {raw}")
    return normalized.rstrip("/")


def _references(text: str, parent: str) -> set[str]:
    paths = set()
    for pattern in (_INLINE_LINK, _LINK_DEFINITION):
        for match in pattern.finditer(text):
            path = _local_reference(match.group(1), parent)
            if path:
                paths.add(path)
    # Links are already parsed; omit them to avoid truncating paths with spaces.
    plain = _INLINE_LINK.sub("", text)
    plain = _LINK_DEFINITION.sub("", plain)
    plain = _URL.sub("", plain)
    if re.search(r"(?:scripts|references)/[^\s`]*[<>]", plain):
        raise ArtifactValidationError("unverified dynamic local reference with angle placeholder")
    for match in _EXPLICIT_REFERENCE.finditer(plain):
        path = _local_reference(match.group(0).rstrip(".,;:!，。；："), parent)
        if path:
            paths.add(path)
    return paths


def _validate_text(text: str, expected_name: str) -> tuple[str, str]:
    try:
        fm, body = parse_strict(text)
    except FrontmatterError as exc:
        raise ArtifactValidationError(f"invalid frontmatter: {exc}") from exc
    name = fm.get("name")
    if not isinstance(name, str) or name != expected_name:
        raise ArtifactValidationError(f"frontmatter name {name!r} does not match {expected_name!r}")
    substantive = "\n".join(
        line.strip() for line in body.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    )
    stub_lines = substantive.splitlines()
    all_stub_lines = bool(stub_lines) and all(
        re.match(r"^[\s*_.()—-]*(?:TODO\b|TBD\b|placeholder\b|待补充|待完善|占位)", line, re.IGNORECASE)
        for line in stub_lines
    )
    if BABY_STUB_BODY_MARKER in body or not substantive or all_stub_lines or re.fullmatch(
        r"[\s*_.()—-]*(?:TODO|TBD|placeholder|待补充|待完善|占位稿|占位)[\s*_.()—-]*",
        substantive, re.IGNORECASE,
    ):
        raise ArtifactValidationError("placeholder body is not a complete artifact")
    return name, body


def validate_skill_artifact(
    skill_path: str | Path, *, expected_name: str | None = None,
    ref: str | None = None,
) -> dict:
    """Validate identity, static references, and a real committed tree if requested.

    Return ``name``, the requested ``ref`` (None for workspace), resolved ``sha``
    (None for workspace), and sorted delivered ``files``. Raise
    :class:`ArtifactValidationError` on missing, unsafe, or unverifiable content.
    """
    root = Path(skill_path).absolute()
    expected_name = root.name if expected_name is None else expected_name
    try:
        if root.name != expected_name:
            raise ArtifactValidationError(
                f"skill directory identity {root.name!r} does not match expected name {expected_name!r}"
            )
        if not stat.S_ISDIR(_safe_fs_path(root).st_mode):
            raise ArtifactValidationError(f"skill directory does not exist: {root}")
        if ref is None:
            files = _workspace_files(root)

            def read(path: str) -> str:
                _safe_fs_path(root / path)
                return (root / path).read_text(encoding="utf-8")

            return _validate_files(files, read, expected_name, ref=None, sha=None)

        git_dir = root / ".git"
        if not stat.S_ISDIR(_safe_fs_path(git_dir).st_mode):
            raise ArtifactValidationError("unsafe .git: expected an independent directory")
        # References and object fan-out directories can also be junctions.
        # Inventory metadata only; never read object bytes through an unchecked link.
        _workspace_files(git_dir)
        for relative in ("objects", "objects/info", "objects/info/alternates", "commondir"):
            path = git_dir / relative
            if path.exists() or path.is_symlink():
                _safe_fs_path(path)
                if relative in ("objects/info/alternates", "commondir"):
                    raise ArtifactValidationError("unsafe external Git object/reference store")
        with _open_repo(root) as repo:
            sha = _resolve_rev(repo, ref)
            if sha is None or not isinstance(repo[sha], Commit):
                raise ArtifactValidationError(f"ref {ref!r} does not resolve to a committed artifact")
            commit = repo[sha]
            files = _tree_files(repo, commit.tree)

            def read(path: str) -> str:
                blob_sha = _lookup_path_in_tree(repo, commit.tree, path)
                data = _read_blob(repo, blob_sha) if blob_sha else None
                if data is None:
                    raise ArtifactValidationError(f"cannot read committed file: {path}")
                return data.decode("utf-8", errors="strict")

            return _validate_files(files, read, expected_name, ref=ref, sha=sha.decode("ascii"))
    except ArtifactValidationError:
        raise
    except (OSError, UnicodeError, KeyError, ValueError, NotGitRepository) as exc:
        raise ArtifactValidationError(f"cannot validate artifact {root} at ref {ref!r}: {exc}") from exc


def _validate_files(files: dict, read, expected_name: str, *, ref, sha) -> dict:
    if "SKILL.md" not in files:
        raise ArtifactValidationError("artifact is missing SKILL.md")
    name, body = _validate_text(read("SKILL.md"), expected_name)
    pending = [("SKILL.md", body)]
    checked = set()
    while pending:
        source, text = pending.pop()
        if source in checked:
            continue
        checked.add(source)
        for path in _references(text, posixpath.dirname(source)):
            if path not in files:
                # Links to a directory are valid if the delivered tree has files in it.
                if any(file.startswith(path + "/") for file in files):
                    continue
                raise ArtifactValidationError(f"missing local reference {path!r} in {source} at ref {ref!r}")
            if path.lower().endswith(".md") and path not in checked:
                pending.append((path, read(path)))
    return {"name": name, "ref": ref, "sha": sha, "files": sorted(files)}
