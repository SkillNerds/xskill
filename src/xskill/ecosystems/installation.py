"""生态安装状态的公共 API。

集中管理 install meta、link/junction 识别、copy 新鲜度与 Git HEAD 校验。
调用方不应跨模块依赖 ``ecosystems._fallback`` 的私有实现。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets
import stat
import tempfile
import time
from operator import attrgetter
from pathlib import Path
from typing import Literal, NoReturn

from dulwich.errors import NotGitRepository
from dulwich.objects import Commit
from dulwich.refs import check_ref_format
from dulwich.repo import Repo

logger = logging.getLogger("xskill.installation")

InstallMode = Literal["symlink", "junction", "copy"]

_INSTALL_META_PREFIX = ".xskill-install-meta-"
COPY_INSTALL_MARKER_NAME = ".xskill-install-identity.json"
_OBJECT_ID_PATTERN = re.compile(rb"[0-9a-f]{40}")
_INSTALLATION_ID_PATTERN = re.compile(r"[0-9a-f]{32}")
_CONTENT_IDENTITY_PATTERN = re.compile(r"[0-9a-f]{64}")
_MAX_IDENTITY_MARKER_BYTES = 64 * 1024
_MAX_INSTALL_METADATA_BYTES = 1024 * 1024
_MAX_COPY_BASELINE_FILES = 20_000
_OPEN_SUPPORTS_DIR_FD = os.open in os.supports_dir_fd


def _stat_is_reparse_point(file_stat: os.stat_result) -> bool:
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    file_attributes = getattr(file_stat, "st_file_attributes", 0)
    return bool(reparse_flag and file_attributes & reparse_flag)


class GitHeadError(RuntimeError):
    """Git HEAD 无法被安全验证。异常消息不包含底层异常或仓库绝对路径。"""

    def __init__(self, error_type: str):
        self.error_type = error_type
        super().__init__("Git HEAD 校验失败，请检查 skill 仓库完整性")


class InstallationMetadataError(RuntimeError):
    """安装元数据无法安全读取或写入。"""

    def __init__(self, error_type: str):
        self.error_type = error_type
        super().__init__("安装元数据操作失败，请检查目标目录状态")


class InstallSafetyError(RuntimeError):
    """安装前安全检查不允许继续破坏性覆盖。"""

    def __init__(self, error_type: str):
        self.error_type = error_type
        super().__init__("安装安全检查失败，已保留现有目标目录")


def _path_hash(path: Path) -> str:
    return hashlib.sha256(
        os.path.abspath(os.path.normpath(str(path))).encode(
            "utf-8", errors="surrogatepass",
        )
    ).hexdigest()[:16]


def _log_git_head_error(skill_path: Path, error_type: str) -> None:
    logger.error(
        "Git HEAD validation failed path_hash=%s error_type=%s",
        _path_hash(skill_path), error_type,
    )


def _raise_git_head_error(
    skill_path: Path, error_type: str,
) -> NoReturn:
    _log_git_head_error(skill_path, error_type)
    raise GitHeadError(error_type) from None


def _raise_metadata_error(dest: Path, error_type: str) -> NoReturn:
    logger.error(
        "installation metadata operation failed path_hash=%s error_type=%s",
        _path_hash(dest), error_type,
    )
    raise InstallationMetadataError(error_type) from None


def _log_metadata_write_cause(
    dest: Path,
    stage: str,
    error: BaseException,
) -> None:
    """记录可诊断且不包含底层路径或异常文本的写入失败原因。"""
    logger.error(
        "installation metadata write failed path_hash=%s stage=%s "
        "exception_type=%s errno=%s winerror=%s",
        _path_hash(dest),
        stage,
        type(error).__name__,
        getattr(error, "errno", None),
        getattr(error, "winerror", None),
    )


def install_metadata_path(dest: Path) -> Path:
    """返回 target 对应的旁路安装元数据路径。"""
    return dest.parent / f"{_INSTALL_META_PREFIX}{dest.name}.json"


def _validate_metadata(metadata: object, dest: Path) -> dict:
    if not isinstance(metadata, dict):
        _raise_metadata_error(dest, "INSTALL_METADATA_DAMAGED")
    metadata_mode = metadata.get("mode")
    installation_id = metadata.get("installation_id")
    content_identity = metadata.get("content_identity")
    baseline_identity = metadata.get("baseline_identity")
    file_fingerprints = metadata.get("file_fingerprints")
    if (
        not isinstance(metadata_mode, str)
        or metadata_mode not in {"symlink", "junction", "copy"}
        or not isinstance(metadata.get("source"), str)
        or (
            "source_sha" in metadata
            and not isinstance(metadata["source_sha"], str)
        )
        or (
            "installed_at" in metadata
            and (
                isinstance(metadata["installed_at"], bool)
                or not isinstance(metadata["installed_at"], (int, float))
            )
        )
        or (
            installation_id is not None
            and (
                not isinstance(installation_id, str)
                or _INSTALLATION_ID_PATTERN.fullmatch(installation_id) is None
            )
        )
        or (
            content_identity is not None
            and (
                not isinstance(content_identity, str)
                or _CONTENT_IDENTITY_PATTERN.fullmatch(
                    content_identity,
                ) is None
            )
        )
        or ((installation_id is None) != (content_identity is None))
        or (
            baseline_identity is not None
            and (
                not isinstance(baseline_identity, str)
                or _CONTENT_IDENTITY_PATTERN.fullmatch(
                    baseline_identity,
                ) is None
            )
        )
        or (
            file_fingerprints is not None
            and (
                not isinstance(file_fingerprints, dict)
                or len(file_fingerprints) > _MAX_COPY_BASELINE_FILES
                or any(
                    not isinstance(relative_path, str)
                    or not relative_path
                    or Path(relative_path).is_absolute()
                    or ".." in Path(relative_path).parts
                    or not isinstance(file_hash, str)
                    or _CONTENT_IDENTITY_PATTERN.fullmatch(file_hash) is None
                    for relative_path, file_hash in file_fingerprints.items()
                )
            )
        )
        or ((baseline_identity is None) != (file_fingerprints is None))
    ):
        _raise_metadata_error(dest, "INSTALL_METADATA_DAMAGED")
    return metadata


def read_install_metadata_file(metadata_path: Path, dest: Path) -> dict | None:
    """读取指定 sidecar；仅文件明确不存在时返回 ``None``。

    该入口供卸载事务读取已原子隔离的 sidecar，schema 与正常安装元数据一致。
    """
    try:
        metadata_stat = metadata_path.lstat()
    except FileNotFoundError:
        return None
    except Exception:  # pylint: disable=broad-exception-caught
        _raise_metadata_error(dest, "INSTALL_METADATA_READ_FAILED")
    if (
        not stat.S_ISREG(metadata_stat.st_mode)
        or _stat_is_reparse_point(metadata_stat)
        or metadata_stat.st_size > _MAX_INSTALL_METADATA_BYTES
    ):
        _raise_metadata_error(dest, "INSTALL_METADATA_DAMAGED")
    open_flags = os.O_RDONLY
    open_flags |= getattr(os, "O_BINARY", 0)
    open_flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        file_descriptor = os.open(metadata_path, open_flags)
    except Exception:  # pylint: disable=broad-exception-caught
        _raise_metadata_error(dest, "INSTALL_METADATA_READ_FAILED")
    try:
        opened_stat = os.fstat(file_descriptor)
        if (
            not stat.S_ISREG(opened_stat.st_mode)
            or _stat_is_reparse_point(opened_stat)
            or (
                opened_stat.st_dev,
                opened_stat.st_ino,
                opened_stat.st_size,
            ) != (
                metadata_stat.st_dev,
                metadata_stat.st_ino,
                metadata_stat.st_size,
            )
        ):
            _raise_metadata_error(dest, "INSTALL_METADATA_DAMAGED")
        metadata_chunks: list[bytes] = []
        remaining = _MAX_INSTALL_METADATA_BYTES + 1
        while remaining > 0:
            metadata_chunk = os.read(
                file_descriptor, min(remaining, 8192),
            )
            if not metadata_chunk:
                break
            metadata_chunks.append(metadata_chunk)
            remaining -= len(metadata_chunk)
        raw_metadata_bytes = b"".join(metadata_chunks)
        final_stat = os.fstat(file_descriptor)
        if (
            len(raw_metadata_bytes) > _MAX_INSTALL_METADATA_BYTES
            or (
                final_stat.st_dev,
                final_stat.st_ino,
                final_stat.st_size,
                final_stat.st_mtime_ns,
            ) != (
                opened_stat.st_dev,
                opened_stat.st_ino,
                opened_stat.st_size,
                opened_stat.st_mtime_ns,
            )
        ):
            _raise_metadata_error(dest, "INSTALL_METADATA_DAMAGED")
    except InstallationMetadataError:
        raise
    except Exception:  # pylint: disable=broad-exception-caught
        _raise_metadata_error(dest, "INSTALL_METADATA_READ_FAILED")
    finally:
        os.close(file_descriptor)
    try:
        raw_metadata = raw_metadata_bytes.decode(
            "utf-8", errors="strict",
        )
        metadata = json.loads(raw_metadata)
    except (TypeError, UnicodeDecodeError, ValueError):
        _raise_metadata_error(dest, "INSTALL_METADATA_DAMAGED")
    return _validate_metadata(metadata, dest)


def read_install_metadata(dest: Path) -> dict | None:
    """读取安装元数据；仅文件明确不存在时返回 ``None``。"""
    return read_install_metadata_file(install_metadata_path(dest), dest)


def _validated_head_sha(repo: Repo, skill_path: Path) -> str | None:
    """读取并校验已打开仓库的 HEAD；不负责关闭 repo。"""
    try:
        raw_head = repo.refs.read_ref(b"HEAD")
    except Exception:  # pylint: disable=broad-exception-caught
        _raise_git_head_error(skill_path, "GIT_HEAD_DAMAGED")
    if raw_head is None:
        _raise_git_head_error(skill_path, "GIT_HEAD_MISSING")

    is_symref = raw_head.startswith(b"ref: ")
    if is_symref:
        head_target = raw_head[5:]
        if not check_ref_format(head_target):
            _raise_git_head_error(skill_path, "GIT_HEAD_DAMAGED")
    elif _OBJECT_ID_PATTERN.fullmatch(raw_head) is None:
        _raise_git_head_error(skill_path, "GIT_HEAD_DAMAGED")

    try:
        object_id = repo.refs[b"HEAD"]
    except KeyError:
        if not is_symref:
            _raise_git_head_error(skill_path, "GIT_HEAD_MISSING")
        try:
            refs = repo.refs.as_dict()
        except Exception:  # pylint: disable=broad-exception-caught
            _raise_git_head_error(skill_path, "GIT_REFS_DAMAGED")
        # 正常空仓必须具有合法的 unborn symref，且没有任何已解析 ref。
        if not refs:
            return None
        _raise_git_head_error(skill_path, "GIT_HEAD_MISSING")
    except Exception:  # pylint: disable=broad-exception-caught
        _raise_git_head_error(skill_path, "GIT_REFS_DAMAGED")

    if (
        not isinstance(object_id, bytes)
        or _OBJECT_ID_PATTERN.fullmatch(object_id) is None
    ):
        _raise_git_head_error(skill_path, "GIT_HEAD_DAMAGED")
    try:
        head_object = repo.object_store[object_id]
    except KeyError:
        _raise_git_head_error(skill_path, "GIT_HEAD_OBJECT_MISSING")
    except Exception:  # pylint: disable=broad-exception-caught
        _raise_git_head_error(skill_path, "GIT_OBJECT_STORE_DAMAGED")
    if not isinstance(head_object, Commit):
        _raise_git_head_error(skill_path, "GIT_HEAD_NOT_COMMIT")
    return object_id.decode("ascii", errors="strict")


def read_skill_head_sha(skill_path: Path) -> str | None:
    """返回经过对象校验的 HEAD Commit SHA；明确非 Git/空仓返回 ``None``。

    Dulwich refs 统一处理 loose refs、packed refs、detached HEAD 和 linked
    worktree。存在 ``.git`` 但 HEAD/ref/object 损坏时记录安全分类并抛
    :class:`GitHeadError`，禁止把伪 SHA 写入安装元数据。
    """
    git_marker = skill_path / ".git"
    try:
        git_marker.lstat()
    except FileNotFoundError:
        return None
    except OSError:
        _raise_git_head_error(skill_path, "GIT_MARKER_IO_ERROR")

    try:
        repo = Repo(str(skill_path))
    except NotGitRepository:
        _raise_git_head_error(skill_path, "GIT_REPOSITORY_DAMAGED")
    except Exception:  # pylint: disable=broad-exception-caught
        # Repo 是文件系统/对象库边界。第三方库可能按损坏形态抛出不同异常，
        # 统一转成不携带底层文本和绝对路径的安全错误。
        _raise_git_head_error(skill_path, "GIT_REPOSITORY_IO_ERROR")

    head_sha: str | None = None
    primary_error: GitHeadError | None = None
    try:
        head_sha = _validated_head_sha(repo, skill_path)
    except GitHeadError as git_error:
        primary_error = git_error
    except Exception:  # pylint: disable=broad-exception-caught
        _log_git_head_error(skill_path, "GIT_REPOSITORY_DAMAGED")
        primary_error = GitHeadError("GIT_REPOSITORY_DAMAGED")

    try:
        try:
            repo.close()
        except Exception:  # pylint: disable=broad-exception-caught
            if primary_error is None:
                _raise_git_head_error(
                    skill_path, "GIT_REPOSITORY_CLOSE_ERROR",
                )
            _log_git_head_error(
                skill_path, "GIT_REPOSITORY_CLOSE_ERROR",
            )
    except GitHeadError as close_error:
        primary_error = close_error

    if primary_error is not None:
        raise primary_error from None
    return head_sha


def _content_identity(source: Path, source_sha: str) -> str:
    """生成低开销内容标识：Git 用 commit，非 Git 用 SKILL.md 内容。"""
    digest = hashlib.sha256()
    digest.update(b"xskill-install-content-v1\0")
    if source_sha:
        digest.update(b"git\0")
        digest.update(source_sha.encode("ascii", errors="strict"))
        return digest.hexdigest()
    digest.update(b"skill\0")
    with (source / "SKILL.md").open("rb") as skill_file:
        while True:
            chunk = skill_file.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _hash_verified_copy_file(
    dest: Path,
    relative_path: Path,
    expected_stat: os.stat_result,
) -> str:
    open_flags = os.O_RDONLY
    open_flags |= getattr(os, "O_BINARY", 0)
    open_flags |= getattr(os, "O_NOFOLLOW", 0)
    open_flags |= getattr(os, "O_NONBLOCK", 0)
    directory_flags = os.O_RDONLY
    directory_flags |= getattr(os, "O_DIRECTORY", 0)
    directory_flags |= getattr(os, "O_NOFOLLOW", 0)
    directory_descriptors: list[int] = []
    try:
        if _OPEN_SUPPORTS_DIR_FD:
            current_directory = os.open(dest, directory_flags)
            directory_descriptors.append(current_directory)
            for path_part in relative_path.parent.parts:
                current_directory = os.open(
                    path_part,
                    directory_flags,
                    dir_fd=current_directory,
                )
                directory_descriptors.append(current_directory)
            file_descriptor = os.open(
                relative_path.name,
                open_flags,
                dir_fd=current_directory,
            )
        else:
            file_descriptor = os.open(dest / relative_path, open_flags)
    except Exception:
        for directory_descriptor in reversed(directory_descriptors):
            os.close(directory_descriptor)
        raise
    try:
        opened_stat = os.fstat(file_descriptor)
        expected_identity = (
            expected_stat.st_dev,
            expected_stat.st_ino,
            stat.S_IFMT(expected_stat.st_mode),
        )
        opened_identity = (
            opened_stat.st_dev,
            opened_stat.st_ino,
            stat.S_IFMT(opened_stat.st_mode),
        )
        if (
            opened_identity != expected_identity
            or not stat.S_ISREG(opened_stat.st_mode)
            or _stat_is_reparse_point(opened_stat)
        ):
            raise OSError("copy baseline file identity changed")
        if os.name != "nt":
            os.set_blocking(file_descriptor, True)
        digest = hashlib.sha256()
        while True:
            file_chunk = os.read(file_descriptor, 1024 * 1024)
            if not file_chunk:
                break
            digest.update(file_chunk)
        final_stat = os.fstat(file_descriptor)
        if (
            final_stat.st_dev,
            final_stat.st_ino,
            stat.S_IFMT(final_stat.st_mode),
            final_stat.st_size,
            final_stat.st_mtime_ns,
            final_stat.st_ctime_ns,
        ) != (
            expected_stat.st_dev,
            expected_stat.st_ino,
            stat.S_IFMT(expected_stat.st_mode),
            expected_stat.st_size,
            expected_stat.st_mtime_ns,
            expected_stat.st_ctime_ns,
        ):
            raise OSError("copy baseline file changed while reading")
        return digest.hexdigest()
    finally:
        os.close(file_descriptor)
        for directory_descriptor in reversed(directory_descriptors):
            os.close(directory_descriptor)


def _safe_copy_file_fingerprints(dest: Path) -> dict[str, str]:
    fingerprints: dict[str, str] = {}
    pending_directories: list[tuple[Path, Path]] = [(dest, Path())]
    root_stat = dest.lstat()
    if (
        not stat.S_ISDIR(root_stat.st_mode)
        or _stat_is_reparse_point(root_stat)
        or is_link_or_junction(dest)
    ):
        raise OSError("copy baseline root is unsafe")
    while pending_directories:
        current_directory, relative_directory = pending_directories.pop()
        current_stat = current_directory.lstat()
        if (
            not stat.S_ISDIR(current_stat.st_mode)
            or _stat_is_reparse_point(current_stat)
        ):
            raise OSError("copy baseline directory is unsafe")
        with os.scandir(current_directory) as entries:
            for entry in sorted(entries, key=attrgetter("name")):
                relative_path = relative_directory / entry.name
                if (
                    relative_directory == Path()
                    and entry.name == COPY_INSTALL_MARKER_NAME
                ):
                    continue
                entry_stat = entry.stat(follow_symlinks=False)
                if _stat_is_reparse_point(entry_stat):
                    raise OSError("copy baseline contains reparse point")
                if stat.S_ISDIR(entry_stat.st_mode):
                    pending_directories.append(
                        (Path(entry.path), relative_path),
                    )
                    continue
                if not stat.S_ISREG(entry_stat.st_mode):
                    raise OSError("copy baseline contains special file")
                if len(fingerprints) >= _MAX_COPY_BASELINE_FILES:
                    raise OSError("copy baseline contains too many files")
                fingerprints[relative_path.as_posix()] = (
                    _hash_verified_copy_file(
                        dest, relative_path, entry_stat,
                    )
                )
    return fingerprints


def _copy_baseline_identity(file_fingerprints: dict[str, str]) -> str:
    baseline_bytes = json.dumps(
        file_fingerprints,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8", errors="strict")
    return hashlib.sha256(baseline_bytes).hexdigest()


def _atomic_write_json(path: Path, payload: dict) -> None:
    """同目录临时文件 + replace，避免崩溃留下半截 JSON。"""
    payload_bytes = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8", errors="strict")
    if len(payload_bytes) > _MAX_INSTALL_METADATA_BYTES:
        raise OSError("installation metadata exceeds size limit")
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.tmp-",
        dir=path.parent,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(file_descriptor, "wb") as output:
            output.write(payload_bytes)
            output.flush()
            os.fsync(output.fileno())
        temporary_path.replace(path)
    except Exception:
        try:
            temporary_path.unlink()
        except OSError:
            pass
        raise


def _safe_read_copy_identity_marker(dest: Path) -> dict | None:
    """nofollow 读取 copy 内部 marker；特殊文件、竞态或损坏均不可信。"""
    try:
        dest_stat = dest.lstat()
        if (
            not stat.S_ISDIR(dest_stat.st_mode)
            or _stat_is_reparse_point(dest_stat)
            or is_link_or_junction(dest)
        ):
            return None
        marker_path = dest / COPY_INSTALL_MARKER_NAME
        marker_stat = marker_path.lstat()
        if (
            not stat.S_ISREG(marker_stat.st_mode)
            or _stat_is_reparse_point(marker_stat)
            or marker_stat.st_size > _MAX_IDENTITY_MARKER_BYTES
        ):
            return None
        open_flags = os.O_RDONLY
        open_flags |= getattr(os, "O_BINARY", 0)
        open_flags |= getattr(os, "O_NOFOLLOW", 0)
        file_descriptor = os.open(marker_path, open_flags)
    except (FileNotFoundError, OSError, ValueError):
        return None
    try:
        opened_stat = os.fstat(file_descriptor)
        if (
            not stat.S_ISREG(opened_stat.st_mode)
            or _stat_is_reparse_point(opened_stat)
            or (
                opened_stat.st_dev,
                opened_stat.st_ino,
                opened_stat.st_size,
            ) != (
                marker_stat.st_dev,
                marker_stat.st_ino,
                marker_stat.st_size,
            )
        ):
            return None
        chunks: list[bytes] = []
        remaining = _MAX_IDENTITY_MARKER_BYTES + 1
        while remaining > 0:
            chunk = os.read(file_descriptor, min(remaining, 8192))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw_marker = b"".join(chunks)
        if len(raw_marker) > _MAX_IDENTITY_MARKER_BYTES:
            return None
        final_stat = os.fstat(file_descriptor)
        if (
            final_stat.st_dev,
            final_stat.st_ino,
            final_stat.st_size,
            final_stat.st_mtime_ns,
        ) != (
            opened_stat.st_dev,
            opened_stat.st_ino,
            opened_stat.st_size,
            opened_stat.st_mtime_ns,
        ):
            return None
    except (OSError, ValueError):
        return None
    finally:
        os.close(file_descriptor)
    try:
        marker = json.loads(raw_marker.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(marker, dict):
        return None
    installation_id = marker.get("installation_id")
    content_identity = marker.get("content_identity")
    baseline_identity = marker.get("baseline_identity")
    if (
        marker.get("schema_version") != 1
        or not isinstance(installation_id, str)
        or _INSTALLATION_ID_PATTERN.fullmatch(installation_id) is None
        or not isinstance(content_identity, str)
        or _CONTENT_IDENTITY_PATTERN.fullmatch(content_identity) is None
        or (
            baseline_identity is not None
            and (
                not isinstance(baseline_identity, str)
                or _CONTENT_IDENTITY_PATTERN.fullmatch(
                    baseline_identity,
                ) is None
            )
        )
    ):
        return None
    return marker


def copy_install_identity_matches(
    dest: Path,
    source: Path | None = None,
    *,
    metadata: dict | None = None,
) -> bool:
    """copy 仅在 target marker 与 sidecar 双向匹配时才视为 xskill 所有。

    旧 sidecar 没有 ``installation_id`` 时明确返回 False，绝不据此递归删除。
    ``metadata`` 用于卸载事务校验已经隔离/改名后的目标。
    """
    if metadata is None:
        metadata = read_install_metadata(dest)
    if metadata is None or metadata.get("mode") != "copy":
        return False
    if source is not None:
        raw_source = metadata.get("source")
        if not isinstance(raw_source, str) or not Path(raw_source).is_absolute():
            return False
        try:
            expected_source = os.path.normcase(
                os.path.realpath(os.path.abspath(str(source))),
            )
            recorded_source = os.path.normcase(
                os.path.realpath(os.path.abspath(raw_source)),
            )
        except (OSError, ValueError):
            return False
        if expected_source != recorded_source:
            return False
    installation_id = metadata.get("installation_id")
    content_identity = metadata.get("content_identity")
    baseline_identity = metadata.get("baseline_identity")
    if (
        not isinstance(installation_id, str)
        or _INSTALLATION_ID_PATTERN.fullmatch(installation_id) is None
        or not isinstance(content_identity, str)
        or _CONTENT_IDENTITY_PATTERN.fullmatch(content_identity) is None
    ):
        return False
    marker = _safe_read_copy_identity_marker(dest)
    return (
        marker is not None
        and marker.get("installation_id") == installation_id
        and marker.get("content_identity") == content_identity
        and marker.get("baseline_identity") == baseline_identity
    )


def read_copy_install_baseline(
    dest: Path, source: Path | None = None,
) -> dict[str, str]:
    """读取并验证 copy 安装时的逐文件内容基线。"""
    metadata = read_install_metadata(dest)
    if (
        metadata is None
        or metadata.get("mode") != "copy"
        or not copy_install_identity_matches(
            dest, source, metadata=metadata,
        )
    ):
        _raise_metadata_error(dest, "INSTALL_COPY_IDENTITY_MISMATCH")
    baseline_identity = metadata.get("baseline_identity")
    file_fingerprints = metadata.get("file_fingerprints")
    if (
        not isinstance(baseline_identity, str)
        or not isinstance(file_fingerprints, dict)
        or _copy_baseline_identity(file_fingerprints)
        != baseline_identity
    ):
        _raise_metadata_error(dest, "INSTALL_COPY_BASELINE_DAMAGED")
    return dict(file_fingerprints)


def write_install_metadata(
    dest: Path, source: Path, mode: InstallMode,
) -> None:
    """安装成功后原子写入 sidecar；copy 同时写 target 内部身份 marker。"""
    try:
        resolved_source = str(source.resolve())
    except Exception:  # pylint: disable=broad-exception-caught
        _raise_metadata_error(dest, "INSTALL_METADATA_SOURCE_FAILED")
    source_sha = read_skill_head_sha(source) or ""
    try:
        content_identity = _content_identity(source, source_sha)
    except Exception:  # pylint: disable=broad-exception-caught
        _raise_metadata_error(dest, "INSTALL_CONTENT_IDENTITY_FAILED")
    metadata = {
        "mode": mode,
        "source": resolved_source,
        "source_sha": source_sha,
        "installed_at": time.time(),
        "installation_id": secrets.token_hex(16),
        "content_identity": content_identity,
    }
    write_stage = "copy_baseline"
    try:
        if mode == "copy":
            file_fingerprints = _safe_copy_file_fingerprints(dest)
            baseline_identity = _copy_baseline_identity(
                file_fingerprints,
            )
            metadata["baseline_identity"] = baseline_identity
            metadata["file_fingerprints"] = file_fingerprints
            metadata_bytes = json.dumps(
                metadata,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8", errors="strict")
            if len(metadata_bytes) > _MAX_INSTALL_METADATA_BYTES:
                raise OSError("installation metadata exceeds size limit")
            write_stage = "copy_identity_marker"
            _atomic_write_json(
                dest / COPY_INSTALL_MARKER_NAME,
                {
                    "schema_version": 1,
                    "installation_id": metadata["installation_id"],
                    "content_identity": metadata["content_identity"],
                    "baseline_identity": metadata["baseline_identity"],
                },
            )
        write_stage = "install_sidecar"
        _atomic_write_json(install_metadata_path(dest), metadata)
    except Exception as write_error:  # pylint: disable=broad-exception-caught
        _log_metadata_write_cause(dest, write_stage, write_error)
        _raise_metadata_error(dest, "INSTALL_METADATA_WRITE_FAILED")
    if mode == "copy" and not copy_install_identity_matches(
        dest, source, metadata=metadata,
    ):
        _raise_metadata_error(dest, "INSTALL_METADATA_VERIFY_FAILED")


def is_link_or_junction(path: Path) -> bool:
    """判断 path 是否为 symlink 或 Windows directory junction。"""
    try:
        if path.is_symlink():
            return True
    except OSError:
        return False
    if os.name != "nt":
        return False
    try:
        return bool(
            path.lstat().st_file_attributes
            & stat.FILE_ATTRIBUTE_REPARSE_POINT
        )
    except (OSError, AttributeError):
        return False


def ensure_link_install_metadata(dest: Path, source: Path) -> None:
    """为已正确指向 source 的 link/junction 补齐并验证安装元数据。"""
    if dest.is_symlink():
        expected_mode = "symlink"
    elif is_link_or_junction(dest):
        expected_mode = "junction"
    else:
        _raise_metadata_error(dest, "INSTALL_METADATA_TARGET_NOT_LINK")
    try:
        expected_source = str(source.resolve())
    except Exception:  # pylint: disable=broad-exception-caught
        _raise_metadata_error(dest, "INSTALL_METADATA_SOURCE_FAILED")
    expected_sha = read_skill_head_sha(source) or ""
    try:
        metadata = read_install_metadata(dest)
    except InstallationMetadataError as metadata_error:
        if metadata_error.error_type != "INSTALL_METADATA_DAMAGED":
            raise
        # 上一次 write_text 可能留下半截 JSON；正确 link 指向 source 足以证明
        # 本次重试可安全重建 sidecar。
        metadata = None
    metadata_is_current = (
        metadata is not None
        and metadata.get("mode") == expected_mode
        and metadata.get("source") == expected_source
        and metadata.get("source_sha") == expected_sha
        and isinstance(metadata.get("installation_id"), str)
        and _INSTALLATION_ID_PATTERN.fullmatch(
            metadata["installation_id"],
        ) is not None
        and isinstance(metadata.get("content_identity"), str)
        and _CONTENT_IDENTITY_PATTERN.fullmatch(
            metadata["content_identity"],
        ) is not None
        and isinstance(metadata.get("installed_at"), (int, float))
        and not isinstance(metadata.get("installed_at"), bool)
    )
    if not metadata_is_current:
        write_install_metadata(dest, source, expected_mode)
        metadata = read_install_metadata(dest)
    if (
        metadata is None
        or metadata.get("mode") != expected_mode
        or metadata.get("source") != expected_source
        or metadata.get("source_sha") != expected_sha
        or not isinstance(metadata.get("installation_id"), str)
        or _INSTALLATION_ID_PATTERN.fullmatch(
            metadata["installation_id"],
        ) is None
        or not isinstance(metadata.get("content_identity"), str)
        or _CONTENT_IDENTITY_PATTERN.fullmatch(
            metadata["content_identity"],
        ) is None
        or not isinstance(metadata.get("installed_at"), (int, float))
        or isinstance(metadata.get("installed_at"), bool)
    ):
        _raise_metadata_error(dest, "INSTALL_METADATA_VERIFY_FAILED")


def link_install_metadata_is_current(dest: Path, source: Path) -> bool:
    """只读验证 link/junction sidecar 是否完整对应当前 source。"""
    if dest.is_symlink():
        expected_mode = "symlink"
    elif is_link_or_junction(dest):
        expected_mode = "junction"
    else:
        return False
    try:
        expected_source = str(source.resolve())
    except Exception:  # pylint: disable=broad-exception-caught
        _raise_metadata_error(dest, "INSTALL_METADATA_SOURCE_FAILED")
    expected_sha = read_skill_head_sha(source) or ""
    metadata = read_install_metadata(dest)
    return (
        metadata is not None
        and metadata.get("mode") == expected_mode
        and metadata.get("source") == expected_source
        and metadata.get("source_sha") == expected_sha
        and isinstance(metadata.get("installation_id"), str)
        and _INSTALLATION_ID_PATTERN.fullmatch(
            metadata["installation_id"],
        ) is not None
        and isinstance(metadata.get("content_identity"), str)
        and _CONTENT_IDENTITY_PATTERN.fullmatch(
            metadata["content_identity"],
        ) is not None
        and isinstance(metadata.get("installed_at"), (int, float))
        and not isinstance(metadata.get("installed_at"), bool)
    )


def installed_mode(dest: Path) -> str | None:
    """link-first 返回最终安装模式；真实目录才读取 install meta。"""
    if dest.is_symlink():
        return "symlink"
    if is_link_or_junction(dest):
        return "junction"
    if not dest.exists():
        return None
    metadata = read_install_metadata(dest)
    if metadata is None:
        return "copy"
    mode = metadata.get("mode")
    return mode if mode in {"symlink", "junction", "copy"} else "copy"


def copy_install_is_current(src_dir: Path, dest: Path) -> bool:
    """判断 copy 是否仍对应当前 Git HEAD 或 SkillHub marker。"""
    metadata = read_install_metadata(dest)
    if (
        not dest.is_dir()
        or metadata is None
        or metadata.get("mode") != "copy"
        or metadata.get("source") != str(src_dir)
        or "source_sha" not in metadata
    ):
        return False
    recorded_sha = metadata["source_sha"]
    if recorded_sha:
        source_is_current = read_skill_head_sha(src_dir) == recorded_sha
        return (
            source_is_current
            and copy_install_identity_matches(
                dest, src_dir, metadata=metadata,
            )
        )
    if not copy_install_identity_matches(
        dest, src_dir, metadata=metadata,
    ):
        return False
    for marker_name in (".xskill_search.json", ".xskill_skillhub.json"):
        source_marker = src_dir / marker_name
        dest_marker = dest / marker_name
        if not (source_marker.is_file() and dest_marker.is_file()):
            continue
        try:
            source_metadata = json.loads(
                source_marker.read_text(encoding="utf-8")
            )
            dest_metadata = json.loads(
                dest_marker.read_text(encoding="utf-8")
            )
        except (OSError, ValueError):
            return False
        return (
            isinstance(source_metadata, dict)
            and source_metadata == dest_metadata
        )
    return False
