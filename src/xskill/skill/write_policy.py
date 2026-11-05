"""Fixed distillation authority shared by framework and agent write entrances.

This is an application boundary, not a sandbox for arbitrary Python extensions.
"""

from __future__ import annotations

from contextlib import contextmanager, ExitStack, closing, nullcontext
from contextvars import ContextVar
from dataclasses import dataclass
import json
import os
from pathlib import Path, PureWindowsPath
import sqlite3
import stat
import threading


class SkillWriteDenied(PermissionError):
    """A write does not belong to the active fixed-target run."""


def normalize_root(root: str | Path) -> str:
    return os.path.normcase(str(Path(root).expanduser().resolve()))


def _validate_name(name: str) -> None:
    if (
        not isinstance(name, str)
        or not name
        or name in {".", "..", "references"}
        or name.startswith(".")
        or "/" in name
        or "\\" in name
        or ":" in name
        or PureWindowsPath(name).is_absolute()
        or name.rstrip(" .") != name
        or name.upper().split(".")[0]
        in {
            "CON",
            "PRN",
            "AUX",
            "NUL",
            *(f"COM{i}" for i in range(1, 10)),
            *(f"LPT{i}" for i in range(1, 10)),
        }
    ):
        raise SkillWriteDenied(f"unsafe skill identity: {name!r}")


@dataclass(frozen=True)
class DistillationPolicy:
    mode: str = "auto"
    run_id: str | None = None
    root_key: str = ""
    target_skill: str | None = None
    generation: int = 0

    def __post_init__(self):
        if self.mode not in {"auto", "create_one", "update_only"}:
            raise ValueError(f"unknown distillation mode: {self.mode}")
        if self.mode != "auto":
            _validate_name(self.target_skill)
            if not self.run_id or not self.root_key:
                raise ValueError("restricted policy requires run_id and root_key")
            object.__setattr__(self, "root_key", normalize_root(self.root_key))

    def require_skill(self, name: str, *, operation: str = "write_content") -> None:
        if self.mode == "auto":
            return
        _validate_name(name)
        allowed = {
            "create",
            "write_content",
            "add_candidates",
            "consume_candidates",
            "git_mutation",
            "publish",
        }
        if (
            name != self.target_skill
            or operation not in allowed
            or (operation == "create" and self.mode != "create_one")
        ):
            raise SkillWriteDenied(
                f"{self.mode} run {self.run_id} denies {operation} for {name!r}"
            )


def _persisted_library(path: str | Path) -> tuple[DistillationPolicy, Path] | None:
    """Locate only explicit registry pointers; run state always comes from SQL."""
    requested = Path(path).expanduser().absolute()
    found = None
    for root in (requested, *requested.parents):
        locator = root / ".distillation-control.json"
        if not locator.exists() and not locator.is_symlink():
            continue
        try:
            _check_path(locator)
            pointer = json.loads(locator.read_text(encoding="utf-8"))
            if not isinstance(pointer, dict):
                raise SkillWriteDenied("distillation locator must be an object")
            key = normalize_root(root)
            if pointer.get("root_key") != key:
                raise SkillWriteDenied("distillation locator root identity differs")
            database = Path(pointer["db_path"])
            if not database.is_absolute():
                raise SkillWriteDenied(
                    "distillation locator must name an explicit registry"
                )
            _check_path(database)
            uri = database.resolve().as_uri() + "?mode=ro"
            with closing(sqlite3.connect(uri, uri=True)) as connection:
                row = connection.execute(
                    "SELECT c.active_run_id,c.generation,r.mode,r.target_skill,r.root_key "
                    "FROM distillation_control c LEFT JOIN distillation_runs r "
                    "ON r.run_id=c.active_run_id WHERE c.root_key=?",
                    (key,),
                ).fetchone()
            if row is None:
                raise SkillWriteDenied("distillation locator has no registered control")
            if row[0] is None:
                if found is None:
                    found = (
                        DistillationPolicy(root_key=key, generation=int(row[1])),
                        database,
                    )
                continue
            _check_path(locator)
            _check_path(database)
            if row[4] != key or row[2] not in {"create_one", "update_only"}:
                raise SkillWriteDenied("distillation registry run identity is invalid")
            registered = DistillationPolicy(row[2], row[0], key, row[3], int(row[1]))
            if found is not None and found[0].mode != "auto" and found[0] != registered:
                raise SkillWriteDenied("conflicting nested distillation restrictions")
            found = (registered, database)
        except SkillWriteDenied:
            raise
        except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as error:
            raise SkillWriteDenied(
                "distillation registry locator cannot be verified"
            ) from error
    return found


_current: ContextVar[DistillationPolicy | None] = ContextVar(
    "distillation_write_policy", default=None
)
_fence_lock = threading.RLock()


@dataclass
class _Fence:
    policy: DistillationPolicy
    db_path: Path | None = None
    depth: int = 1
    creation_reserved: bool = False
    identity: tuple[int, int] | None = None


_fences: dict[str, _Fence] = {}


def current_write_policy(root: str | Path | None = None) -> DistillationPolicy:
    policy = _current.get()
    if root is not None:
        key = normalize_root(root)
        with _fence_lock:
            matching = [
                (k, f) for k, f in _fences.items() if Path(key).is_relative_to(Path(k))
            ]
        if matching:
            return max(matching, key=lambda pair: len(pair[0]))[1].policy
        persisted = _persisted_library(root)
        if persisted is not None and persisted[0].mode != "auto":
            return persisted[0]
        if (
            policy
            and policy.mode != "auto"
            and not Path(key).is_relative_to(Path(policy.root_key))
        ):
            raise SkillWriteDenied(
                "bound policy cannot authorize a different skill library"
            )
    return policy or DistillationPolicy()


@contextmanager
def use_write_policy(policy: DistillationPolicy):
    previous = _current.get()
    if previous and previous.mode != "auto" and policy != previous:
        raise SkillWriteDenied(
            "nested context cannot replace fixed distillation authority"
        )
    with _fence_lock:
        fence = _fences.get(policy.root_key)
        if fence and fence.policy != policy:
            raise SkillWriteDenied("policy is stale or conflicts with library fence")
    token = _current.set(policy)
    try:
        yield policy
    finally:
        _current.reset(token)


@contextmanager
def library_write_guard(
    root: str | Path, policy: DistillationPolicy, db_path: Path | None = None,
    *, bind_context: bool = True,
):
    key = normalize_root(root)
    persisted = _persisted_library(root)
    if persisted is not None:
        saved, registered_db = persisted
        if saved.mode != "auto" and saved != policy:
            raise SkillWriteDenied("guard authority differs from persisted restriction")
        if db_path is not None and normalize_root(db_path) != normalize_root(
            registered_db
        ):
            raise SkillWriteDenied("guard registry differs from library locator")
        db_path = registered_db
    if policy.root_key and policy.root_key != key:
        raise SkillWriteDenied("policy root does not match guarded library")
    guarded_path = Path(root).expanduser().absolute()
    root_identity = None
    if policy.mode != "auto":
        _check_path(guarded_path)
        if guarded_path.exists():
            info = guarded_path.stat()
            root_identity = (info.st_dev, info.st_ino)
    with _fence_lock:
        for guarded_root, guarded in _fences.items():
            overlaps = Path(key).is_relative_to(Path(guarded_root)) or Path(
                guarded_root
            ).is_relative_to(Path(key))
            if (
                overlaps
                and guarded.policy != policy
                and (guarded.policy.mode != "auto" or policy.mode != "auto")
            ):
                raise SkillWriteDenied(
                    "overlapping library guards cannot broaden authority"
                )
        existing = _fences.get(key)
        if existing and existing.policy != policy:
            raise SkillWriteDenied("cannot broaden or replace active library fence")
        if existing:
            existing.depth += 1
        else:
            fence = _Fence(policy, Path(db_path) if db_path else None)
            if policy.mode != "auto":
                _check_generation(fence)
            target = Path(key) / str(policy.target_skill)
            if policy.mode != "auto" and target.exists():
                info = target.stat()
                fence.identity = (info.st_dev, info.st_ino)
            _fences[key] = fence
    try:
        with ExitStack() as root_paths:
            if policy.mode != "auto":
                root_paths.enter_context(
                    _pinned_parent(guarded_path / ".distillation-library-pin")
                )
                _check_path(guarded_path)
                if root_identity is not None:
                    current = guarded_path.stat()
                    if (current.st_dev, current.st_ino) != root_identity:
                        raise SkillWriteDenied(
                            "library root identity changed during guard registration"
                        )
                _check_generation(_fences[key])
            # The web host keeps a process-wide fence that its controller can
            # replace from another thread after draining requests.
            with use_write_policy(policy) if bind_context else nullcontext():
                yield policy
    finally:
        with _fence_lock:
            fence = _fences[key]
            fence.depth -= 1
            if fence.depth == 0:
                del _fences[key]


def _check_path(path: Path) -> None:
    for item in (*reversed(path.parents), path):
        try:
            info = item.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise SkillWriteDenied(f"linked/reparse write path denied: {item}")
        if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
            raise SkillWriteDenied(f"special write path denied: {item}")
        if stat.S_ISREG(info.st_mode) and info.st_nlink > 1:
            raise SkillWriteDenied(f"hardlinked write file denied: {item}")


def _check_generation(fence: _Fence) -> None:
    if fence.db_path is None:
        return
    _check_path(fence.db_path)
    uri = fence.db_path.resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        row = connection.execute(
            "SELECT c.active_run_id,c.generation,r.mode,r.target_skill,r.root_key,"
            "r.target_identity,r.owns_target FROM distillation_control c "
            "LEFT JOIN distillation_runs r ON r.run_id=c.active_run_id "
            "WHERE c.root_key=?",
            (fence.policy.root_key,),
        ).fetchone()
    policy = fence.policy
    if row is None or row[:5] != (
        policy.run_id,
        policy.generation,
        policy.mode,
        policy.target_skill,
        policy.root_key,
    ):
        raise SkillWriteDenied("distillation authority is stale")
    if policy.mode == "update_only" or bool(row[6]):
        target = Path(policy.root_key) / policy.target_skill
        _check_path(target)
        try:
            identity = json.loads(row[5])
            current = target.stat()
        except (OSError, ValueError, TypeError) as error:
            raise SkillWriteDenied(
                "persisted target directory identity cannot be verified"
            ) from error
        if identity != {"dev": current.st_dev, "ino": current.st_ino}:
            raise SkillWriteDenied("persisted target directory identity changed")


def _owned_target(target: Path, policy: DistillationPolicy) -> bool:
    marker = target / ".distillation-run.json"
    _check_path(marker)
    try:
        value = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return (
        value.get("run_id") == policy.run_id
        and value.get("root_key") == policy.root_key
    )


def require_skill_write(
    path: str | Path, operation: str = "write_content"
) -> DistillationPolicy:
    raw = Path(path).expanduser()
    lexical = raw.absolute()
    active = _current.get()
    with _fence_lock:
        matches = [
            (key, value)
            for key, value in _fences.items()
            if lexical.is_relative_to(Path(key))
            or lexical.resolve().is_relative_to(Path(key))
        ]
    fence = max(matches, key=lambda pair: len(pair[0]))[1] if matches else None
    policy = fence.policy if fence else active
    if fence is None:
        persisted = _persisted_library(lexical)
        if persisted is not None and persisted[0].mode != "auto":
            policy = persisted[0]
    if policy is None or policy.mode == "auto":
        return policy or DistillationPolicy()
    if fence is None:
        raise SkillWriteDenied("restricted writes require a live library guard")
    if ".." in raw.parts:
        raise SkillWriteDenied("write traversal denied")
    if active != policy:
        raise SkillWriteDenied(
            "restricted library write requires current run authority"
        )
    root = Path(policy.root_key)
    if not lexical.is_relative_to(root):
        raise SkillWriteDenied(
            "bound policy cannot authorize a different skill library"
        )
    relative = lexical.relative_to(root)
    if not relative.parts:
        raise SkillWriteDenied("library-wide write denied in restricted mode")
    name = relative.parts[0]
    policy.require_skill(name, operation=operation)
    if any(
        part.casefold() in {".distillation-control.json", ".distillation-run.json"}
        for part in relative.parts[1:]
    ):
        raise SkillWriteDenied(
            "distillation control metadata is reserved for the runtime"
        )
    _check_path(lexical)
    if not lexical.resolve().is_relative_to(root / name):
        raise SkillWriteDenied("write escaped target directory")
    if fence:
        _check_generation(fence)
    target = root / name
    if policy.mode == "update_only" and not target.is_dir():
        raise SkillWriteDenied("update_only target no longer exists")
    if operation == "create":
        if target.exists() or (fence and fence.creation_reserved):
            raise SkillWriteDenied("create_one target already exists or was reserved")
        if fence:
            fence.creation_reserved = True
    elif (
        policy.mode == "create_one"
        and not (fence and fence.creation_reserved)
        and not _owned_target(target, policy)
    ):
        raise SkillWriteDenied("target is not owned by this create_one run")
    if target.exists() and fence:
        info = target.stat()
        identity = (info.st_dev, info.st_ino)
        if fence.identity is not None and identity != fence.identity:
            raise SkillWriteDenied("target directory identity changed")
        fence.identity = identity
    if operation == "git_mutation" and (target / "SKILL.md").is_file():
        _require_content_identity(
            (target / "SKILL.md").read_text(encoding="utf-8"), policy
        )
    git = target / ".git"
    if git.exists() and not git.is_dir():
        raise SkillWriteDenied("external .git indirection denied")
    if (operation == "git_mutation" or lexical == target) and target.exists():
        for directory, dirs, files in os.walk(target, followlinks=False):
            for entry in (*dirs, *files):
                _check_path(Path(directory) / entry)
        if (
            (git / "objects" / "info" / "alternates").exists()
            or (git / "objects" / "info" / "http-alternates").exists()
            or (git / "commondir").exists()
        ):
            raise SkillWriteDenied("external Git object/common directory denied")
        if (git / "config").is_file():
            from dulwich.config import ConfigFile

            config = ConfigFile.from_path(str(git / "config"))
            try:
                config.get((b"core",), b"worktree")
            except KeyError:
                pass
            else:
                raise SkillWriteDenied("Git worktree indirection denied")
    return policy


def require_management_write(root: str | Path, operation: str = "management") -> None:
    policy = current_write_policy(root)
    if policy.mode != "auto":
        raise SkillWriteDenied(
            f"{operation} is unavailable during a restricted distillation run"
        )


def require_auxiliary_write(
    path: str | Path,
    skill_path: str | Path,
    operation: str = "publish",
    expected_root: str | Path | None = None,
    *,
    allow_source_link: bool = False,
) -> DistillationPolicy:
    """Authorize one named auxiliary target, never its entire parent directory."""
    source = Path(skill_path).expanduser().absolute()
    if source.parent.name == ".canary":
        original = source.parent.parent / source.name
        policy = require_skill_write(original, operation=operation)
        if policy.mode != "auto":
            _check_path(source)
            if source.is_dir():
                for directory, dirs, files in os.walk(source, followlinks=False):
                    for name in (*dirs, *files):
                        _check_path(Path(directory) / name)
    else:
        policy = require_skill_write(source, operation=operation)
    if policy.mode == "auto":
        return policy
    destination = Path(path).expanduser().absolute()
    if ".." in Path(path).parts:
        raise SkillWriteDenied("auxiliary traversal denied")
    if (
        expected_root is not None
        and destination
        != Path(expected_root).expanduser().absolute() / policy.target_skill
    ):
        raise SkillWriteDenied("auxiliary path must identify exactly the target skill")
    if destination.name != policy.target_skill:
        raise SkillWriteDenied("auxiliary destination identity differs from target")
    library = Path(policy.root_key)
    if destination.is_relative_to(library) and destination not in {
        library / ".canary" / policy.target_skill,
        library / policy.target_skill,
    }:
        raise SkillWriteDenied("auxiliary destination overlaps another library path")
    _check_path(destination.parent)
    try:
        info = destination.lstat()
    except FileNotFoundError:
        info = None
    if info and (
        stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400
    ):
        allowed_sources = {
            library / policy.target_skill,
            library / ".canary" / policy.target_skill,
        }
        resolved = destination.resolve()
        if not allow_source_link or resolved not in allowed_sources:
            raise SkillWriteDenied(
                "auxiliary destination link points outside authorized source"
            )
        _check_path(resolved)
    else:
        _check_path(destination)
        if destination.is_dir():
            for directory, dirs, files in os.walk(destination, followlinks=False):
                for name in (*dirs, *files):
                    _check_path(Path(directory) / name)
    return policy


def require_install_write(source: str | Path, dest: str | Path) -> DistillationPolicy:
    policy = require_auxiliary_write(dest, source, allow_source_link=True)
    if policy.mode != "auto":
        backup = Path(dest).parent / f".{Path(dest).name}.replaced-by-symlink"
        _check_path(backup)
        if backup.is_dir():
            for directory, dirs, files in os.walk(backup, followlinks=False):
                for name in (*dirs, *files):
                    _check_path(Path(directory) / name)
    return policy


def require_repo_lock_write(path: str | Path, skill_path: str | Path) -> None:
    import hashlib

    policy = require_skill_write(skill_path)
    if policy.mode == "auto":
        return
    expected = (
        Path(skill_path).resolve().parent
        / ".repo_locks"
        / (
            hashlib.sha256(str(Path(skill_path).resolve()).encode("utf-8")).hexdigest()
            + ".lock"
        )
    )
    if Path(path).absolute() != expected:
        raise SkillWriteDenied("unexpected repository lock path")
    _check_path(expected)


@contextmanager
def _pinned_parent(path: Path):
    """Hold checked directory handles for writes without following links."""
    if os.name != "nt":
        descriptors = []
        try:
            flags = (
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0)
            )
            descriptor = os.open(path.anchor, flags)
            descriptors.append(descriptor)
            complete = True
            for part in path.parent.relative_to(path.anchor).parts:
                try:
                    descriptor = os.open(part, flags, dir_fd=descriptor)
                except FileNotFoundError:
                    complete = False
                    break
                descriptors.append(descriptor)
            _check_path(path)
            yield descriptor if complete else None
        finally:
            for descriptor in reversed(descriptors):
                os.close(descriptor)
        return
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create.restype = wintypes.HANDLE
    close = kernel.CloseHandle
    close.argtypes = [wintypes.HANDLE]
    handles = []
    try:
        for parent in reversed(path.parents):
            if not parent.exists():
                continue
            handle = create(str(parent), 0x81, 0x3, None, 3, 0x02200000, None)
            if handle == ctypes.c_void_p(-1).value:
                raise SkillWriteDenied(f"cannot pin write directory: {parent}")
            handles.append(handle)
        _check_path(path)
        yield
    finally:
        for handle in reversed(handles):
            close(handle)


def safe_write_text(
    path: str | Path,
    content: str | bytes,
    *,
    encoding: str = "utf-8",
    append: bool = False,
    create_parents: bool = False,
    auxiliary_skill_path: str | Path | None = None,
    auxiliary_root: str | Path | None = None,
    mode: int | None = None,
) -> int:
    """Verify the opened file before truncating; reject link replacement races."""
    target = Path(path).expanduser().absolute()

    def authorize():
        if auxiliary_skill_path is not None:
            auxiliary = (
                Path(auxiliary_root).absolute()
                if auxiliary_root is not None
                else target.parent
            )
            if not target.is_relative_to(auxiliary) or ".." in Path(path).parts:
                raise SkillWriteDenied("auxiliary file escaped its registered target")
            return require_auxiliary_write(auxiliary, auxiliary_skill_path)
        return require_skill_write(target)

    policy = authorize()
    if (
        policy.mode != "auto"
        and target.name.upper() == "SKILL.MD"
        and (
            auxiliary_skill_path is not None
            or target.parent == Path(policy.root_key) / policy.target_skill
        )
    ):
        _require_content_identity(
            content.decode(encoding) if isinstance(content, bytes) else content, policy
        )
    if create_parents:
        if auxiliary_skill_path is not None and policy.mode != "auto":
            _mkdir_authorized(target.parent, authorize)
        else:
            safe_mkdir(target.parent)
    if policy.mode == "auto":
        if isinstance(content, bytes):
            if append:
                with target.open("ab") as stream:
                    return stream.write(content)
            return target.write_bytes(content)
        if append:
            with target.open("a", encoding=encoding) as stream:
                return stream.write(content)
        return target.write_text(content, encoding=encoding)
    with _pinned_parent(target) as parent_descriptor:
        authorize()
        _check_path(target)
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_BINARY", 0)
        )
        descriptor = os.open(
            target.name if parent_descriptor is not None else target,
            flags,
            0o600,
            **({"dir_fd": parent_descriptor} if parent_descriptor is not None else {}),
        )
        try:
            opened = os.fstat(descriptor)
            current = target.lstat()
            authorize()
            _check_path(target)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_nlink != 1
                or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)
            ):
                raise SkillWriteDenied("opened write file identity changed")
            if append:
                os.lseek(descriptor, 0, os.SEEK_END)
            else:
                os.ftruncate(descriptor, 0)
            payload = (
                content if isinstance(content, bytes) else content.encode(encoding)
            )
            written = 0
            while written < len(payload):
                written += os.write(descriptor, payload[written:])
            if mode is not None and os.name != "nt":
                os.fchmod(descriptor, mode)
            return len(content)
        finally:
            os.close(descriptor)


def safe_mkdir(
    path: str | Path, *, parents: bool = True, exist_ok: bool = True
) -> None:
    """Create directories only after pinning and rechecking existing prefixes."""
    target = Path(path).expanduser().absolute()
    policy = require_skill_write(target)
    if policy.mode == "auto":
        target.mkdir(parents=parents, exist_ok=exist_ok)
        return
    missing = []
    cursor = target
    while not cursor.exists():
        missing.append(cursor)
        cursor = cursor.parent
    if len(missing) > 1 and not parents:
        raise FileNotFoundError(str(target.parent))
    if not missing and not exist_ok:
        raise FileExistsError(str(target))
    for directory in reversed(missing):
        with _pinned_parent(directory) as parent_descriptor:
            require_skill_write(directory)
            if parent_descriptor is None:
                directory.mkdir(exist_ok=exist_ok)
            else:
                try:
                    os.mkdir(directory.name, dir_fd=parent_descriptor)
                except FileExistsError:
                    if not exist_ok:
                        raise
            require_skill_write(directory)


def _require_content_identity(content: str, policy: DistillationPolicy) -> None:
    from xskill.skill.frontmatter import parse_strict

    try:
        frontmatter, _ = parse_strict(content)
    except ValueError as error:
        raise SkillWriteDenied("target SKILL.md frontmatter is invalid") from error
    if frontmatter.get("name") != policy.target_skill:
        raise SkillWriteDenied("target frontmatter identity cannot change")


def _mkdir_authorized(path: Path, authorize) -> None:
    missing = []
    cursor = path
    while not cursor.exists():
        missing.append(cursor)
        cursor = cursor.parent
    for directory in reversed(missing):
        with _pinned_parent(directory) as descriptor:
            authorize()
            _check_path(directory)
            if descriptor is None:
                directory.mkdir()
            else:
                os.mkdir(directory.name, dir_fd=descriptor)
            _check_path(directory)


@contextmanager
def auxiliary_write_guard(
    path: str | Path,
    skill_path: str | Path,
    *,
    expected_root: str | Path | None = None,
    allow_source_link: bool = False,
    create: bool = False,
    pin_target: bool = False,
):
    """Pin exact auxiliary prefixes across installation/materialization mutations."""
    destination = Path(path).expanduser().absolute()

    def authorize():
        return require_auxiliary_write(
            destination,
            skill_path,
            expected_root=expected_root,
            allow_source_link=allow_source_link,
        )

    policy = authorize()
    if policy.mode == "auto":
        yield destination
        return
    _mkdir_authorized(destination.parent, authorize)
    with ExitStack() as stack:
        stack.enter_context(
            _pinned_parent(destination.parent / ".distillation-aux-parent-pin")
        )
        authorize()
        if create:
            _mkdir_authorized(destination, authorize)
        if pin_target:
            if not destination.is_dir() or destination.is_symlink():
                raise SkillWriteDenied("auxiliary target is not a normal directory")
            stack.enter_context(
                _pinned_parent(destination / ".distillation-path-check")
            )
            authorize()
        yield destination
        authorize()


def pin_install_write(destination):
    """Apply the same auxiliary guard to shared and ecosystem installer entrances."""
    from functools import wraps

    def decorate(function):
        @wraps(function)
        def wrapped(*args, **kwargs):
            source = (
                args[0]
                if args
                else kwargs.get(
                    "src_dir", kwargs.get("skill_path", kwargs.get("source"))
                )
            )
            output = destination(*args, **kwargs)
            require_install_write(source, output)
            with auxiliary_write_guard(output, source, allow_source_link=True):
                return function(*args, **kwargs)

        return wrapped

    return decorate


def safe_write_bytes(
    path: str | Path,
    content: bytes,
    *,
    auxiliary_skill_path: str | Path | None = None,
    auxiliary_root: str | Path | None = None,
    create_parents: bool = False,
    mode: int | None = None,
) -> int:
    return safe_write_text(
        path,
        content,
        auxiliary_skill_path=auxiliary_skill_path,
        auxiliary_root=auxiliary_root,
        create_parents=create_parents,
        mode=mode,
    )


def safe_auxiliary_mkdir(
    path: str | Path, *, auxiliary_skill_path: str | Path, auxiliary_root: str | Path
) -> None:
    root = Path(auxiliary_root).absolute()
    directory = Path(path).absolute()
    if not directory.is_relative_to(root) or ".." in Path(path).parts:
        raise SkillWriteDenied("auxiliary directory escaped its registered target")

    def authorize():
        return require_auxiliary_write(root, auxiliary_skill_path)

    policy = authorize()
    if policy.mode == "auto":
        directory.mkdir(parents=True, exist_ok=True)
    else:
        _mkdir_authorized(directory, authorize)


def safe_remove_auxiliary_tree(path: str | Path, skill_path: str | Path) -> None:
    """Remove only the authorized auxiliary tree with no-follow leaf operations."""
    import shutil

    destination = Path(path).expanduser().absolute()
    policy = require_auxiliary_write(destination, skill_path)
    if policy.mode == "auto":
        shutil.rmtree(destination)
        return
    with auxiliary_write_guard(destination, skill_path):
        if not destination.exists():
            return
        if os.name != "nt":

            def remove_at(parent_descriptor, name):
                descriptor = os.open(
                    name,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=parent_descriptor,
                )
                try:
                    for entry in os.listdir(descriptor):
                        info = os.stat(entry, dir_fd=descriptor, follow_symlinks=False)
                        if stat.S_ISDIR(info.st_mode):
                            remove_at(descriptor, entry)
                        elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                            os.unlink(entry, dir_fd=descriptor)
                        else:
                            raise SkillWriteDenied("unsafe auxiliary deletion entry")
                finally:
                    os.close(descriptor)
                os.rmdir(name, dir_fd=parent_descriptor)

            with _pinned_parent(destination) as parent_descriptor:
                remove_at(parent_descriptor, destination.name)
            return

        def remove_directory(directory):
            with _pinned_parent(directory / ".distillation-delete-check"):
                _check_path(directory)
                for entry in directory.iterdir():
                    _check_path(entry)
                    if entry.is_dir():
                        remove_directory(entry)
                    else:
                        entry.unlink()
            # Parent remains pinned; release this directory's own pin before rmdir.
            _check_path(directory)
            directory.rmdir()

        remove_directory(destination)


def unlink_install_entry(path: str | Path, source: str | Path) -> None:
    """Unlink one permitted install mount, including Windows directory junctions."""
    destination = Path(path)
    policy = require_install_write(source, destination)
    with auxiliary_write_guard(destination, source, allow_source_link=True):
        info = destination.lstat()
        if (
            policy.mode != "auto"
            and getattr(info, "st_file_attributes", 0) & 0x400
            and not destination.is_symlink()
        ):
            destination.rmdir()
        else:
            destination.unlink()
