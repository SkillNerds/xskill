"""Capability objects exposed to trusted, in-process algorithm kernels."""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import TYPE_CHECKING, Iterable, Iterator, Literal, Mapping

if TYPE_CHECKING:
    from xskill.utils.llm import EmbedClient, LLMClient

from xskill.kernels.base import (
    KernelInvocation,
    PublishedSkill,
    SkillSubmission,
    validate_kernel_id,
)

AtomSplitStatus = Literal["pending", "ready", "updated"]
TrajectorySource = Literal["user", "temp"]

_TEMP_TRAJECTORY_ID_RE = re.compile(r"^traj_[a-z0-9][a-z0-9_-]{0,126}$")
_USER_HEADER_RE = re.compile(r"^##\s+User\b")
_PLATFORM_HEADING_RE = re.compile(
    r"^## (?:User(?:\s|$)|Assistant(?:\s|$)|Tool Call:|Tool Output:)"
)
_KERNEL_TEMP_MARKDOWN_EXAMPLE = (
    "## User\n\n"
    "Please deploy the service.\n\n"
    "## Assistant\n\n"
    "Done.\n"
)


_HIDDEN_BUNDLE_PARTS = {
    ".git", ".canary", ".ux_scores.jsonl", ".candidates.yml",
    ".description_optimization", ".repo_locks", ".lock",
}


def _safe_bundle_relative(raw_path: str) -> PurePosixPath:
    relative = PurePosixPath(str(raw_path))
    if (
        "\\" in str(raw_path)
        or "\x00" in str(raw_path)
        or relative.is_absolute()
        or not relative.parts
        or ".." in relative.parts
        or any(
            part.startswith(".") or part in _HIDDEN_BUNDLE_PARTS
            for part in relative.parts
        )
    ):
        raise ValueError(f"unsafe skill bundle path: {raw_path!r}")
    return relative


def _bundle_files(root: Path) -> tuple[Path, ...]:
    """Return distributable regular files without platform runtime state."""
    if not root.is_dir():
        return ()
    files: list[Path] = []
    for path in root.rglob("*"):
        if path.is_symlink():
            continue
        relative = path.relative_to(root)
        if any(
            part.startswith(".") or part in _HIDDEN_BUNDLE_PARTS
            for part in relative.parts
        ):
            continue
        if path.is_file():
            files.append(path)
    return tuple(sorted(files, key=lambda item: item.relative_to(root).as_posix()))


@dataclass(frozen=True)
class TrajectoryDirectoryResource:
    """A registered directory the kernel may scan under a read-only contract.

    The Python runner is a trusted in-process plugin boundary.  ``read_only``
    therefore documents the contract but is not an OS-level sandbox.
    """

    id: str
    path: Path
    label: str
    ecosystem: str
    auto_index: bool
    trajectory_count: int
    indexed_count: int
    read_only: bool = True


@dataclass(frozen=True)
class AtomResource:
    """Read-only sub-trajectory (AtomTask) exposed under a trajectory view.

    ``ux_score`` is an integer in ``1..10`` when scored, otherwise ``None``.
    There is no trajectory-level UX aggregate on :class:`TrajectoryResource`.
    """

    atom_id: str
    trajectory_id: str
    ux_score: int | None
    used_skills: tuple[str, ...]
    intent: str = ""
    summary: str = ""
    content: str = ""
    offset_start: int = 0
    offset_end: int = 0


def _normalize_atom_ux_score(value: object) -> int | None:
    if value is None or isinstance(value, bool) or not isinstance(value, int):
        return None
    if 1 <= value <= 10:
        return value
    return None


def _load_atom_resources(trajectory_path: Path) -> tuple[AtomResource, ...]:
    from xskill.pipeline.atom import AtomTaskStore

    traj_id = trajectory_path.stem
    store = AtomTaskStore(root=trajectory_path.parent)
    resources: list[AtomResource] = []
    for atom in store.list_by_traj(traj_id):
        used = tuple(
            dict.fromkeys(
                str(item).strip()
                for item in (atom.used_skills or [])
                if str(item).strip()
            )
        )
        resources.append(AtomResource(
            atom_id=str(atom.atom_id),
            trajectory_id=str(atom.traj_id or traj_id),
            ux_score=_normalize_atom_ux_score(atom.ux_score),
            used_skills=used,
            intent=str(atom.intent or ""),
            summary=str(atom.summary or ""),
            content=str(atom.raw_segment or ""),
            offset_start=int(atom.offset_start or 0),
            offset_end=int(atom.offset_end or 0),
        ))
    return tuple(resources)


def resolve_atom_split_status(
    registry_status: str | None,
    *,
    atom_count: int,
) -> AtomSplitStatus:
    """Map platform registry status onto the kernel-facing split view.

    - ``pending``: first-time split not finished; expose no atoms.
    - ``ready``: current markdown body has been split through.
    - ``updated``: body grew again and incremental split is in progress;
      previously produced atoms remain readable and unchanged.
    """
    status = str(registry_status or "").strip()
    if status == "updated":
        return "updated"
    if status == "splitting":
        return "updated" if atom_count > 0 else "pending"
    if status == "discovered":
        return "pending"
    if status in {
        "split_done", "indexed", "clustering", "done", "meta_done",
        "filtered", "error",
    }:
        return "ready"
    # Manual / unregistered roots: atoms on disk mean a usable split view.
    if atom_count > 0:
        return "ready"
    return "pending"


@dataclass(frozen=True)
class TrajectoryResource:
    """Read-only reference with a registry-qualified stable ID.

    Stable platform input is the sanitized Markdown body. Sub-trajectory
    evidence lives under :attr:`atoms` and is gated by
    :attr:`atom_split_status`. This object does not expose a trajectory-level
    UX score.
    """

    id: str
    trajectory_id: str
    path: Path
    watch_dir_id: int
    watch_dir: Path
    label: str
    ecosystem: str
    status: str | None
    metadata: Mapping[str, object]
    used_skills: tuple[str, ...] = ()
    atom_split_status: AtomSplitStatus = "pending"
    atoms: tuple[AtomResource, ...] = ()
    source: TrajectorySource = "user"

    def read_text(self) -> str:
        return self.path.read_text(encoding="utf-8")

    def read_raw_json(self) -> dict:
        raw_path = self.path.with_suffix(".json")
        if not raw_path.is_file():
            return {}
        return json.loads(raw_path.read_text(encoding="utf-8"))


def _validate_kernel_temp_markdown(markdown: str) -> None:
    """Validate platform-style markdown before ``TrajectoryReader.create_temp``."""
    if not isinstance(markdown, str) or not markdown.strip():
        raise ValueError(
            "temp trajectory markdown must be non-empty with at least one ## User "
            "section; example:\n"
            f"{_KERNEL_TEMP_MARKDOWN_EXAMPLE}"
        )
    has_user = False
    for line in markdown.splitlines():
        stripped = line.strip()
        if not stripped.startswith("## "):
            continue
        if _USER_HEADER_RE.match(stripped):
            has_user = True
            continue
        if stripped.lower().startswith("## user"):
            raise ValueError(
                "temp trajectory markdown uses malformed ## User heading; "
                "expected platform headings ## User / ## Assistant / "
                "## Tool Call: / ## Tool Output:; example:\n"
                f"{_KERNEL_TEMP_MARKDOWN_EXAMPLE}"
            )
        if not _PLATFORM_HEADING_RE.match(stripped):
            raise ValueError(
                "temp trajectory markdown contains unsupported section heading; "
                "expected platform headings ## User / ## Assistant / "
                "## Tool Call: / ## Tool Output:; example:\n"
                f"{_KERNEL_TEMP_MARKDOWN_EXAMPLE}"
            )
    if not has_user:
        raise ValueError(
            "temp trajectory markdown must contain at least one ## User section; "
            "example:\n"
            f"{_KERNEL_TEMP_MARKDOWN_EXAMPLE}"
        )


def _trajectory_resource_from_path(
    *,
    path: Path,
    watch_dir: TrajectoryDirectoryResource,
    status: str | None,
    metadata: Mapping[str, object],
    used_skills: tuple[str, ...],
    source: TrajectorySource = "user",
) -> TrajectoryResource:
    disk_atoms = _load_atom_resources(path)
    split_status = resolve_atom_split_status(status, atom_count=len(disk_atoms))
    atoms = () if split_status == "pending" else disk_atoms
    relative_path = path.relative_to(watch_dir.path).as_posix()
    return TrajectoryResource(
        id=f"{watch_dir.id}:{relative_path}",
        trajectory_id=path.stem,
        path=path,
        watch_dir_id=(
            int(watch_dir.id) if watch_dir.id != "root" else 0
        ),
        watch_dir=watch_dir.path,
        label=watch_dir.label,
        ecosystem=watch_dir.ecosystem,
        status=status,
        metadata=MappingProxyType(dict(metadata)),
        used_skills=used_skills,
        atom_split_status=split_status,
        atoms=atoms,
        source=source,
    )


class TrajectoryReader:
    """Filesystem-capable facade over this invocation's trajectory input.

    ``root`` is the absolute input root exposed to the kernel for its own batch
    readers and filesystem tools.  Registered watch directories remain the
    preferred source of attribution and status metadata.  When no watch
    directory is registered, the reader falls back to recursively discovering
    ``traj_*.md`` below ``root`` so manually supplied trajectory trees still work.
    """

    def __init__(
        self,
        db_path: Path,
        *,
        root: Path | None = None,
        temp_root: Path | None = None,
    ):
        self._db_path = Path(db_path)
        self.root = (
            Path(root).expanduser().resolve()
            if root is not None
            else None
        )
        self._temp_root = (
            Path(temp_root).expanduser().resolve()
            if temp_root is not None
            else None
        )
        self._temp_watch_dir_id: int | None = None

    def directories(self) -> tuple[TrajectoryDirectoryResource, ...]:
        """Expose roots for ``rg``/``find``/DuckDB and other batch readers."""
        from xskill.pipeline.registry import Registry

        registered = tuple(
            TrajectoryDirectoryResource(
                id=str(item.id),
                path=item.path,
                label=item.label,
                ecosystem=item.ecosystem,
                auto_index=item.auto_index,
                trajectory_count=item.traj_count,
                indexed_count=item.indexed_count,
            )
            for item in Registry(self._db_path).list()
        )
        if registered or self.root is None or not self.root.is_dir():
            return registered

        # A manually supplied trajectory root does not have to be registered.
        # Represent it as one synthetic directory while preserving the same
        # provider-facing resource contract.
        trajectory_count = sum(
            1
            for path in self.root.rglob("traj_*.md")
            if path.is_file() and not path.is_symlink()
        )
        return (TrajectoryDirectoryResource(
            id="root",
            path=self.root,
            label=self.root.name,
            ecosystem="kernel-input",
            auto_index=False,
            trajectory_count=trajectory_count,
            indexed_count=0,
        ),)

    def iter(
        self,
        *,
        directory_id: str | None = None,
        statuses: Iterable[str] | None = None,
    ) -> Iterator[TrajectoryResource]:
        """Stream resources without materializing the complete corpus."""
        from xskill.pipeline.registry import Registry
        from xskill.pipeline.trajectory import Trajectory

        accepted = set(statuses) if statuses is not None else None
        registry = Registry(self._db_path)
        for watch_dir in self.directories():
            if directory_id is not None and str(watch_dir.id) != str(directory_id):
                continue
            if not watch_dir.path.is_dir():
                continue
            for path in sorted(watch_dir.path.rglob("traj_*.md")):
                if path.is_symlink() or not path.is_file():
                    continue
                trajectory = Trajectory.load(path, registry=registry)
                status = trajectory.status
                if accepted is not None and status not in accepted:
                    continue
                try:
                    metadata = trajectory.meta
                except (OSError, json.JSONDecodeError):
                    metadata = {}
                used_skills = tuple(dict.fromkeys(
                    item.strip()
                    for item in str(trajectory.skill_used or "").split(",")
                    if item.strip()
                ))
                source = (
                    "temp" if watch_dir.ecosystem == "kernel-temp" else "user"
                )
                yield _trajectory_resource_from_path(
                    path=path,
                    watch_dir=watch_dir,
                    status=status,
                    metadata=metadata,
                    used_skills=used_skills,
                    source=source,
                )

    def list(
        self,
        *,
        statuses: Iterable[str] | None = None,
    ) -> list[TrajectoryResource]:
        return list(self.iter(statuses=statuses))

    def get(self, resource_id: str) -> TrajectoryResource:
        for resource in self.iter():
            if resource.id == resource_id:
                return resource
        raise KeyError(f"trajectory not found: {resource_id}")

    def _ensure_temp_watch_dir(self) -> tuple[Path, TrajectoryDirectoryResource]:
        if self._temp_root is None:
            raise RuntimeError(
                "TrajectoryReader has no temp_root configured; cannot create_temp"
            )
        from xskill.pipeline.registry import Registry, register_dir

        temp_root = self._temp_root
        temp_root.mkdir(parents=True, exist_ok=True)
        if self._temp_watch_dir_id is None:
            registry = Registry(self._db_path)
            resolved = temp_root.resolve()
            for item in registry.list():
                if item.path.resolve() == resolved:
                    self._temp_watch_dir_id = int(item.id)
                    break
            else:
                self._temp_watch_dir_id = register_dir(
                    temp_root,
                    label="kernel-temp",
                    ecosystem="kernel-temp",
                    auto_index=False,
                    db_path=self._db_path,
                )
        watch_dir = TrajectoryDirectoryResource(
            id=str(self._temp_watch_dir_id),
            path=temp_root,
            label="kernel-temp",
            ecosystem="kernel-temp",
            auto_index=False,
            trajectory_count=0,
            indexed_count=0,
        )
        return temp_root, watch_dir

    def create_temp(
        self,
        markdown: str,
        *,
        trajectory_id: str,
    ) -> TrajectoryResource:
        """Write a kernel-owned temp trajectory and register it for platform split."""
        normalized_id = str(trajectory_id or "").strip()
        if not _TEMP_TRAJECTORY_ID_RE.fullmatch(normalized_id):
            raise ValueError(
                "trajectory_id must match traj_[a-z0-9][a-z0-9_-]{0,126}: "
                f"{trajectory_id!r}"
            )
        _validate_kernel_temp_markdown(markdown)

        temp_root, watch_dir = self._ensure_temp_watch_dir()
        path = (temp_root / f"{normalized_id}.md").resolve()
        if temp_root not in path.parents:
            raise ValueError(f"unsafe temp trajectory path: {path}")

        path.write_text(markdown, encoding="utf-8")

        from xskill.pipeline.registry import discover_trajectories

        discover_trajectories(
            int(watch_dir.id),
            temp_root,
            db_path=self._db_path,
        )

        return _trajectory_resource_from_path(
            path=path,
            watch_dir=watch_dir,
            status="discovered",
            metadata={},
            used_skills=(),
            source="temp",
        )


@dataclass(frozen=True)
class SkillVersionResource:
    """Version-level user feedback bound to a Git commit."""

    commit_sha: str
    side: str
    ux_average: float | None
    ux_samples: int
    first_scored_at: str | None
    last_scored_at: str | None


@dataclass(frozen=True)
class SkillResource:
    """Read-only main bundle and its main/staging feedback view."""

    name: str
    description: str
    path: Path
    metadata: Mapping[str, object]
    ux_average: float | None
    ux_samples: int
    main_commit_sha: str
    staging_commit_sha: str | None
    versions: tuple[SkillVersionResource, ...]

    def list_files(self) -> tuple[str, ...]:
        return tuple(
            path.relative_to(self.path).as_posix()
            for path in _bundle_files(self.path)
        )

    def read_text(self, relative_path: str = "SKILL.md") -> str:
        relative = _safe_bundle_relative(relative_path)
        target = self.path.joinpath(*relative.parts)
        if not target.is_file() or target.is_symlink():
            raise FileNotFoundError(relative_path)
        return target.read_text(encoding="utf-8")


@dataclass(frozen=True)
class SkillDraft:
    """Editable copy inside the kernel workspace, never the live Skill repo."""

    name: str
    path: Path
    base_commit_sha: str

    def list_files(self) -> tuple[str, ...]:
        return tuple(
            path.relative_to(self.path).as_posix()
            for path in _bundle_files(self.path)
        )


class SkillReader:
    """Version-aware read facade plus managed workspace checkout."""

    def __init__(self, skill_dir: Path, *, workspace: Path):
        self._skill_dir = Path(skill_dir)
        self._workspace = Path(workspace).resolve()

    def list(self, *, days: int = 30) -> list[SkillResource]:
        from xskill import canary
        from xskill.skill.repo import SkillRepo

        result: list[SkillResource] = []
        for skill in SkillRepo(self._skill_dir):
            recent = skill.recent_ux_scores(days=days)
            main_commit = canary.main_sha(skill.path) or ""
            staging_commit = canary.staging_sha(skill.path)
            aggregates = {
                row["commit_sha"]: row
                for row in canary.aggregate_ux_by_version(recent)
            }
            versions: list[SkillVersionResource] = []
            for side, commit_sha in (
                ("main", main_commit), ("staging", staging_commit),
            ):
                if not commit_sha:
                    continue
                aggregate = aggregates.get(commit_sha, {})
                versions.append(SkillVersionResource(
                    commit_sha=commit_sha,
                    side=side,
                    ux_average=aggregate.get("avg"),
                    ux_samples=int(aggregate.get("count", 0)),
                    first_scored_at=aggregate.get("first_scored_at"),
                    last_scored_at=aggregate.get("last_scored_at"),
                ))
            result.append(SkillResource(
                name=skill.name,
                description=skill.description,
                path=skill.path,
                metadata=MappingProxyType(dict(
                    skill.frontmatter.get("metadata", {}) or {}
                )),
                ux_average=skill.ux_avg(days=days),
                ux_samples=sum(
                    1 for row in recent
                    if isinstance(row.get("score"), (int, float))
                    and not isinstance(row.get("score"), bool)
                ),
                main_commit_sha=main_commit,
                staging_commit_sha=staging_commit,
                versions=tuple(versions),
            ))
        return result

    def get(self, name: str, *, days: int = 30) -> SkillResource:
        for skill in self.list(days=days):
            if skill.name == name:
                return skill
        raise KeyError(f"skill not found: {name}")

    def checkout(self, name: str) -> SkillDraft:
        """Copy main's full bundle into a deterministic workspace checkout."""
        resource = self.get(name)
        if not resource.main_commit_sha:
            raise RuntimeError(f"skill {name} has no main commit")
        destination = (
            self._workspace / "skill_checkouts" / name
            / resource.main_commit_sha[:12]
        ).resolve()
        if self._workspace not in destination.parents:
            raise ValueError("skill checkout must stay inside kernel workspace")
        if destination.exists():
            if not destination.is_dir() or destination.is_symlink():
                raise RuntimeError(f"unsafe checkout path: {destination}")
            return SkillDraft(name, destination, resource.main_commit_sha)
        destination.mkdir(parents=True, exist_ok=False)
        for source in _bundle_files(resource.path):
            relative = source.relative_to(resource.path)
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        return SkillDraft(name, destination, resource.main_commit_sha)


class SkillPublisher:
    """Validated, Git-aware write gateway; kernels never write skill_dir."""

    _MAX_BUNDLE_BYTES = 2 * 1024 * 1024
    _RESERVED_PARTS = _HIDDEN_BUNDLE_PARTS

    def __init__(
        self,
        *,
        skill_dir: Path,
        kernel_id: str,
        kernel_version: str,
        run_id: str,
    ):
        self._skill_dir = Path(skill_dir)
        self._kernel_id = validate_kernel_id(kernel_id)
        self._kernel_version = str(kernel_version)
        self._run_id = str(run_id)
        self._published: list[PublishedSkill] = []

    @property
    def published(self) -> tuple[PublishedSkill, ...]:
        return tuple(self._published)

    def submit(self, draft: SkillSubmission) -> PublishedSkill:
        name, skill_md, files = self._validate_and_attribute(draft)
        skill_path = self._skill_dir / name
        self._skill_dir.mkdir(parents=True, exist_ok=True)
        if skill_path.exists():
            if not draft.base_commit_sha:
                raise ValueError(
                    f"updating skill {name} requires base_commit_sha; "
                    "read context.skills.get(name).main_commit_sha first"
                )
            published = self._stage_existing(
                skill_path, name, skill_md, files, draft.message,
                draft.base_commit_sha,
            )
        else:
            if draft.base_commit_sha is not None:
                raise ValueError(
                    f"new skill {name} must not declare base_commit_sha"
                )
            published = self._create_new(
                skill_path, name, skill_md, files, draft.message,
            )
        self._published.append(published)
        return published

    def submit_checkout(
        self,
        checkout: SkillDraft,
        *,
        message: str,
        source_trajectory_ids: tuple[str, ...] = (),
    ) -> PublishedSkill:
        """Publish an exact UTF-8 text snapshot from a managed checkout."""
        root = checkout.path.resolve()
        if not root.is_dir() or root.is_symlink():
            raise ValueError("skill checkout is not a regular directory")
        skill_md_path = root / "SKILL.md"
        if not skill_md_path.is_file() or skill_md_path.is_symlink():
            raise ValueError("skill checkout must contain SKILL.md")
        files: dict[str, str] = {}
        for path in _bundle_files(root):
            relative = path.relative_to(root).as_posix()
            if relative == "SKILL.md":
                continue
            try:
                files[relative] = path.read_text(encoding="utf-8")
            except UnicodeDecodeError as exc:
                raise ValueError(
                    f"kernel bundle file must be UTF-8 text: {relative!r}"
                ) from exc
        return self.submit(SkillSubmission(
            name=checkout.name,
            skill_md=skill_md_path.read_text(encoding="utf-8"),
            files=files,
            source_trajectory_ids=source_trajectory_ids,
            message=message,
            base_commit_sha=checkout.base_commit_sha,
        ))

    def _validate_and_attribute(
        self,
        draft: SkillSubmission,
    ) -> tuple[str, str, dict[str, str]]:
        from xskill.skill.frontmatter import parse_strict, serialize

        name = validate_kernel_id(draft.name)
        frontmatter, body = parse_strict(draft.skill_md)
        declared_name = str(frontmatter.get("name") or "").strip()
        if declared_name != name:
            raise ValueError(
                f"SKILL.md name {declared_name!r} does not match draft {name!r}"
            )
        description = frontmatter.get("description")
        if not isinstance(description, str) or not description.strip():
            raise ValueError("kernel submission description must not be empty")
        metadata = frontmatter.setdefault("metadata", {})
        if not isinstance(metadata, dict):
            raise ValueError("SKILL.md metadata must be a mapping")
        # Per-run attribution belongs to kernel_runs.db; embedding run_id would
        # make identical algorithm output produce different Skill content.
        metadata["kernel"] = {
            "id": self._kernel_id,
            "version": self._kernel_version,
        }
        if draft.source_trajectory_ids:
            existing = metadata.get("source_trajs") or []
            if not isinstance(existing, list):
                raise ValueError("metadata.source_trajs must be a list")
            metadata["source_trajs"] = list(dict.fromkeys(
                [str(item) for item in existing]
                + [str(item) for item in draft.source_trajectory_ids]
            ))

        files: dict[str, str] = {}
        total = len(draft.skill_md.encode("utf-8"))
        for raw_name, contents in draft.files.items():
            relative = _safe_bundle_relative(str(raw_name))
            if relative.as_posix() == "SKILL.md":
                raise ValueError("SKILL.md must be provided through skill_md")
            if not isinstance(contents, str):
                raise ValueError(f"kernel bundle file must be text: {raw_name!r}")
            total += len(contents.encode("utf-8"))
            files[relative.as_posix()] = contents
        if total > self._MAX_BUNDLE_BYTES:
            raise ValueError(
                f"kernel bundle exceeds {self._MAX_BUNDLE_BYTES} bytes"
            )
        return name, serialize(frontmatter, body), files

    @staticmethod
    def _write_bundle(
        skill_path: Path,
        skill_md: str,
        files: Mapping[str, str],
    ) -> None:
        (skill_path / "SKILL.md").write_text(skill_md, encoding="utf-8")
        for relative, contents in files.items():
            target = skill_path.joinpath(*PurePosixPath(relative).parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(contents, encoding="utf-8")

    def _create_new(
        self,
        skill_path: Path,
        name: str,
        skill_md: str,
        files: Mapping[str, str],
        message: str,
    ) -> PublishedSkill:
        from xskill.canary import main_sha
        from xskill.skill.frontmatter import parse
        from xskill.skill.git import (
            commit_baby_to_main_branch,
            init_skill_repo_on_baby,
        )

        frontmatter, _ = parse(skill_md)
        draft_root = self._skill_dir / ".kernel_drafts"
        run_draft_root = draft_root / self._run_id
        draft_path = run_draft_root / name
        try:
            init_skill_repo_on_baby(
                str(draft_path), name, str(frontmatter["description"]),
            )
            self._write_bundle(draft_path, skill_md, files)
            if not commit_baby_to_main_branch(
                str(draft_path), f"kernel({self._kernel_id}): {message}",
            ):
                raise RuntimeError(f"failed to publish new skill: {name}")
            draft_path.replace(skill_path)
        finally:
            # A successful replace moves the draft repository away. On any
            # failure, remove the incomplete repository. Empty parents are
            # best-effort cleanup because another publication from the same
            # run may still be using them.
            if draft_path.is_symlink():
                try:
                    draft_path.unlink()
                except OSError:
                    pass
            elif draft_path.exists():
                shutil.rmtree(draft_path, ignore_errors=True)
            try:
                remaining_drafts = [
                    child
                    for child in run_draft_root.iterdir()
                    if child.name != ".repo_locks"
                ]
            except OSError:
                remaining_drafts = None
            if remaining_drafts == []:
                # Repository locks live beside draft repositories and remain
                # as ordinary files after their context exits. Once the last
                # draft is gone, the whole run workspace is disposable.
                shutil.rmtree(run_draft_root, ignore_errors=True)
            try:
                draft_root.rmdir()
            except OSError:
                pass
        return PublishedSkill(
            name=name, action="created", commit_sha=main_sha(skill_path) or "",
        )

    def _stage_existing(
        self,
        skill_path: Path,
        name: str,
        skill_md: str,
        files: Mapping[str, str],
        message: str,
        base_commit_sha: str,
    ) -> PublishedSkill:
        from xskill.canary import has_staging, main_sha, staging_sha
        from xskill.skill.git import (
            commit_to_staging_branch,
            current_branch,
            has_changes,
            run_git,
            skill_repo_lock,
        )

        with skill_repo_lock(skill_path):
            previous_commit = main_sha(skill_path) or ""
            if previous_commit != base_commit_sha:
                raise RuntimeError(
                    f"skill {name} changed since checkout: expected "
                    f"{base_commit_sha}, current {previous_commit}"
                )
            if current_branch(str(skill_path)) != "main":
                raise RuntimeError(f"skill {name} is not on main")
            if has_staging(skill_path):
                raise RuntimeError(f"skill {name} already has an active staging")
            if has_changes(str(skill_path)):
                raise RuntimeError(f"skill {name} has uncommitted changes")
            original_files = {
                path.relative_to(skill_path).as_posix()
                for path in _bundle_files(skill_path)
            }
            desired_files = {"SKILL.md", *files.keys()}
            candidate_only = desired_files - original_files
            try:
                for relative in original_files - desired_files:
                    (skill_path / relative).unlink()
                self._write_bundle(skill_path, skill_md, files)
                if not commit_to_staging_branch(
                    str(skill_path), f"kernel({self._kernel_id}): {message}",
                ):
                    raise RuntimeError(f"failed to stage skill: {name}")
            except BaseException:
                run_git(["reset", "--hard", "main"], cwd=str(skill_path))
                self._remove_candidate_only(skill_path, candidate_only)
                raise
            self._remove_candidate_only(skill_path, candidate_only)
        return PublishedSkill(
            name=name,
            action="staged",
            commit_sha=staging_sha(skill_path) or "",
            previous_commit_sha=previous_commit,
        )

    @staticmethod
    def _remove_candidate_only(skill_path: Path, relative_paths: set[str]) -> None:
        for relative in sorted(relative_paths, reverse=True):
            target = skill_path / relative
            if target.is_file() and not target.is_symlink():
                target.unlink()
        for directory in sorted(
            (item for item in skill_path.rglob("*") if item.is_dir()),
            key=lambda item: len(item.parts),
            reverse=True,
        ):
            if directory.name == ".git" or ".git" in directory.parts:
                continue
            try:
                directory.rmdir()
            except OSError:
                pass


@dataclass(frozen=True)
class KernelContext:
    """One bounded invocation plus the provider's small capability surface."""

    invocation: KernelInvocation
    workspace: Path
    config_path: Path
    xskill_config_path: Path
    trajectories: TrajectoryReader
    skills: SkillReader
    publisher: SkillPublisher
    llm: "LLMClient | None"
    embedding: "EmbedClient | None"

    @property
    def run_id(self) -> str:
        return self.invocation.run_id

    @property
    def trajectory_root(self) -> Path:
        """Absolute filesystem root selected for this kernel invocation."""
        if self.trajectories.root is None:
            raise RuntimeError("kernel invocation has no trajectory root")
        return self.trajectories.root
