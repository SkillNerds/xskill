"""生态安装状态的公共 API。

集中管理 install meta、link/junction 识别、copy 新鲜度与 Git HEAD 校验。
调用方不应跨模块依赖 ``ecosystems._fallback`` 的私有实现。
"""
from __future__ import annotations

from xskill.skill.write_policy import require_install_write

import hashlib
import json
import logging
import os
import re
import secrets
import stat
import tempfile
import time
from enum import Enum
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


class CopyBaselineRepairStatus(str, Enum):
    """显式 copy 基线审计/修复的无歧义结果。"""

    CURRENT = "current"
    REPAIRABLE = "repairable"
    REPAIRED = "repaired"
    DIVERGED = "diverged"
    INVALID = "invalid"
    CONCURRENT = "concurrent"
    FAILED = "failed"


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
    """读取安装元数据；优先 InstallLedger，其次遗留 sidecar（并回填 ledger）。

    仅「账本与 sidecar 都不存在」时返回 ``None``。sidecar 损坏仍 fail-loud。
    """
    from xskill.ecosystems.install_ledger import get_default_ledger

    ledger = get_default_ledger()
    row = ledger.read_install(dest)
    if row is not None:
        return row
    sidecar = install_metadata_path(dest)
    try:
        meta = read_install_metadata_file(sidecar, dest)
    except AttributeError:
        _raise_metadata_error(dest, "INSTALL_METADATA_READ_FAILED")
    if meta is None:
        return None
    try:
        ledger.record_install(
            dest,
            skill_name=Path(dest).name,
            mode=str(meta["mode"]),
            source=str(meta["source"]),
            source_sha=str(meta.get("source_sha") or ""),
            installation_id=str(meta["installation_id"]),
            content_identity=str(meta["content_identity"]),
            baseline_identity=(
                meta["baseline_identity"]
                if isinstance(meta.get("baseline_identity"), str)
                else None
            ),
            file_fingerprints=(
                meta["file_fingerprints"]
                if isinstance(meta.get("file_fingerprints"), dict)
                else None
            ),
            installed_at=(
                float(meta["installed_at"])
                if isinstance(meta.get("installed_at"), (int, float))
                else None
            ),
        )
        try:
            if hasattr(sidecar, "unlink"):
                sidecar.unlink(missing_ok=True)
            marker = Path(dest) / COPY_INSTALL_MARKER_NAME
            if marker.is_file():
                marker.unlink(missing_ok=True)
        except OSError:
            pass
    except Exception:  # pylint: disable=broad-exception-caught
        logger.warning(
            "install ledger backfill failed path_hash=%s",
            _path_hash(dest),
        )
    return meta


_GIT_COMMIT_SHA_PATTERN = re.compile(r"[0-9a-f]{40}")
# git tree 入口的 mode 高位（与 POSIX S_IFMT 同形）；勿只靠平台 S_IS*，
# 个别环境下对原始 git mode 的宏判断会漏掉普通文件。
_GIT_MODE_MASK = 0o160000
_GIT_MODE_FILE = 0o100000
_GIT_MODE_DIR = 0o040000


def _git_mode_is_dir(mode: int) -> bool:
    return (mode & _GIT_MODE_MASK) == _GIT_MODE_DIR or stat.S_ISDIR(mode)


def _git_mode_is_file(mode: int) -> bool:
    return (mode & _GIT_MODE_MASK) == _GIT_MODE_FILE or stat.S_ISREG(mode)


def _git_tree_fingerprints(source: Path, sha: str) -> dict[str, str] | None:
    """按 commit 的 git tree 重建逐文件内容指纹。

    口径与 ``_safe_copy_file_fingerprints`` / reverse_sync 一致：文件字节
    sha256、POSIX 相对路径。当 HEAD 仍停在该 install commit 时，优先用
    **工作区**字节——reverse_sync 比对的是 worktree；Windows 上 blob 与
    工作区可能因换行/checkout 口径不一致，用 blob 做基线会误报
    ``REVERSE_SYNC_CONTENT_CONFLICT``。任何一步读不到对象都返回 None。
    """
    try:
        repo = Repo(str(source))
    except (NotGitRepository, OSError):
        return None
    try:
        try:
            obj = repo[sha.encode("ascii")]
        except KeyError:
            return None
        if not isinstance(obj, Commit):
            return None
        fingerprints: dict[str, str] = {}
        stack: list[tuple[bytes, Path]] = [(obj.tree, Path())]
        while stack:
            tree_id, prefix = stack.pop()
            try:
                tree = repo[tree_id]
            except KeyError:
                return None
            for raw_name, mode, entry_id in tree.iteritems():
                try:
                    name = raw_name.decode("utf-8", errors="strict")
                except UnicodeDecodeError:
                    return None
                relative = prefix / name
                if _git_mode_is_dir(mode):
                    stack.append((entry_id, relative))
                elif _git_mode_is_file(mode):
                    try:
                        blob = repo[entry_id]
                    except KeyError:
                        return None
                    fingerprints[relative.as_posix()] = hashlib.sha256(
                        blob.data,
                    ).hexdigest()
        try:
            head_raw = repo.refs[b"HEAD"]
            head_hex = (
                head_raw.decode("ascii")
                if isinstance(head_raw, (bytes, bytearray))
                else None
            )
        except (KeyError, UnicodeDecodeError, TypeError):
            head_hex = None
        if head_hex == sha:
            source_root = Path(source)
            for relative_name in list(fingerprints):
                worktree_path = source_root.joinpath(*relative_name.split("/"))
                try:
                    path_stat = worktree_path.lstat()
                except OSError:
                    continue
                if (
                    not stat.S_ISREG(path_stat.st_mode)
                    or _stat_is_reparse_point(path_stat)
                ):
                    continue
                try:
                    fingerprints[relative_name] = hashlib.sha256(
                        worktree_path.read_bytes(),
                    ).hexdigest()
                except OSError:
                    continue
        return fingerprints
    except (OSError, ValueError, KeyError):
        return None
    finally:
        repo.close()


def adopt_orphan_copy_install(
    dest: Path,
    source: Path,
    *,
    legacy_meta_path: Path | None = None,
) -> bool:
    """孤儿 dest（无账本行、无 sidecar）按 legacy meta 的 source_sha 从 git
    重建安装时基线并登记账本；成功返回 True。

    历史迁移失败的存量 dest 只剩生态目录内老 meta（canary 比对数据）。
    其中的 source_sha 记录了安装那一刻的源 commit——从 git object 取回该
    提交的文件内容算指纹，三方回流就能区分 dest 侧用户编辑与 source 侧
    前进，基线不靠猜。登记后清掉 dest 内旧 marker，账本此后是唯一身份
    来源。任何一步拿不到数据都返回 False，维持原冻结状态（安全方向）。
    """
    require_install_write(source, dest)
    from xskill.ecosystems.install_ledger import get_default_ledger

    try:
        if read_install_metadata(dest) is not None:
            return False
    except InstallationMetadataError:
        return False
    if legacy_meta_path is None:
        return False
    try:
        legacy = json.loads(legacy_meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, UnicodeDecodeError):
        return False
    if not isinstance(legacy, dict):
        return False
    source_sha = legacy.get("source_sha")
    if (
        not isinstance(source_sha, str)
        or _GIT_COMMIT_SHA_PATTERN.fullmatch(source_sha) is None
    ):
        return False
    fingerprints = _git_tree_fingerprints(Path(source), source_sha)
    if fingerprints is None:
        return False
    try:
        resolved_source = str(Path(source).resolve())
    except OSError:
        return False
    installed_at = legacy.get("installed_at")
    if isinstance(installed_at, bool) or not isinstance(
        installed_at, (int, float),
    ):
        installed_at = None
    try:
        get_default_ledger().record_install(
            dest,
            skill_name=Path(dest).name,
            mode="copy",
            source=resolved_source,
            source_sha=source_sha,
            installation_id=secrets.token_hex(16),
            content_identity=_content_identity(Path(source), source_sha),
            baseline_identity=_copy_baseline_identity(fingerprints),
            file_fingerprints=fingerprints,
            installed_at=installed_at,
        )
    except Exception:  # pylint: disable=broad-exception-caught
        logger.exception(
            "orphan copy install adoption failed path_hash=%s",
            _path_hash(Path(dest)),
        )
        return False
    try:
        marker = Path(dest) / COPY_INSTALL_MARKER_NAME
        if marker.is_symlink() or marker.is_file():
            marker.unlink(missing_ok=True)
    except OSError:
        pass
    logger.info(
        "orphan copy install adopted path_hash=%s",
        _path_hash(Path(dest)),
    )
    return True


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
        expected_content_stat = (
            expected_stat.st_size,
            expected_stat.st_mtime_ns,
        )
        opened_content_stat = (
            opened_stat.st_size,
            opened_stat.st_mtime_ns,
        )
        if os.name != "nt":
            expected_content_stat += (expected_stat.st_ctime_ns,)
            opened_content_stat += (opened_stat.st_ctime_ns,)
        if opened_content_stat != expected_content_stat:
            raise OSError("copy baseline file changed before reading")
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
            opened_stat.st_dev,
            opened_stat.st_ino,
            stat.S_IFMT(opened_stat.st_mode),
            opened_stat.st_size,
            opened_stat.st_mtime_ns,
            opened_stat.st_ctime_ns,
        ):
            raise OSError("copy baseline file changed while reading")
        return digest.hexdigest()
    finally:
        os.close(file_descriptor)
        for directory_descriptor in reversed(directory_descriptors):
            os.close(directory_descriptor)


def _safe_copy_file_fingerprints(
    dest: Path,
    *,
    exclude_root_names: frozenset[str] = frozenset(),
    exclude_root_prefixes: tuple[str, ...] = (),
) -> dict[str, str]:
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
                    and (
                        entry.name == COPY_INSTALL_MARKER_NAME
                        or entry.name in exclude_root_names
                        or any(
                            entry.name.startswith(prefix)
                            for prefix in exclude_root_prefixes
                        )
                    )
                ):
                    continue
                # Windows 的 DirEntry.stat() 可能直接使用 WIN32_FIND_DATA
                # 缓存，其 st_dev/st_ino 为 0；后续 os.fstat() 则返回真实
                # 文件身份，导致安全校验把正常文件误判成竞态替换。
                entry_stat = Path(entry.path).lstat()
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
        except OSError as cleanup_error:
            logger.warning(
                "installation metadata temporary cleanup failed "
                "path_hash=%s exception_type=%s",
                _path_hash(temporary_path),
                type(cleanup_error).__name__,
            )
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
    """copy 安装是否仍由 xskill 账本声明为所有。

    以 InstallLedger（或遗留 sidecar 回填）中的 installation_id /
    content_identity / baseline_identity 为准。若目录内仍有旧 marker，
    则必须与账本一致；无 marker 时仅认账本（DB 化后的常态）。
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
        or not isinstance(baseline_identity, str)
        or _CONTENT_IDENTITY_PATTERN.fullmatch(baseline_identity) is None
    ):
        return False
    marker = _safe_read_copy_identity_marker(dest)
    if marker is None:
        return True
    return (
        marker.get("installation_id") == installation_id
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
    """安装成功后写入 InstallLedger（作废同 dest 未完成卸装）。

    不再向用户生态目录写 sidecar / copy identity marker。
    """
    require_install_write(source, dest)
    from xskill.ecosystems.install_ledger import get_default_ledger

    try:
        resolved_source = str(source.resolve())
    except Exception:  # pylint: disable=broad-exception-caught
        _raise_metadata_error(dest, "INSTALL_METADATA_SOURCE_FAILED")
    source_sha = read_skill_head_sha(source) or ""
    try:
        content_identity = _content_identity(source, source_sha)
    except Exception:  # pylint: disable=broad-exception-caught
        _raise_metadata_error(dest, "INSTALL_CONTENT_IDENTITY_FAILED")
    installation_id = secrets.token_hex(16)
    baseline_identity = None
    file_fingerprints = None
    write_stage = "copy_baseline"
    try:
        if mode == "copy":
            file_fingerprints = _safe_copy_file_fingerprints(dest)
            baseline_identity = _copy_baseline_identity(file_fingerprints)
            metadata_probe = {
                "mode": mode,
                "source": resolved_source,
                "source_sha": source_sha,
                "installed_at": time.time(),
                "installation_id": installation_id,
                "content_identity": content_identity,
                "baseline_identity": baseline_identity,
                "file_fingerprints": file_fingerprints,
            }
            metadata_bytes = json.dumps(
                metadata_probe,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8", errors="strict")
            if len(metadata_bytes) > _MAX_INSTALL_METADATA_BYTES:
                raise OSError("installation metadata exceeds size limit")
        write_stage = "install_ledger"
        get_default_ledger().record_install(
            dest,
            skill_name=Path(dest).name,
            mode=mode,
            source=resolved_source,
            source_sha=source_sha,
            installation_id=installation_id,
            content_identity=content_identity,
            baseline_identity=baseline_identity,
            file_fingerprints=file_fingerprints,
        )
        write_stage = "legacy_sidecar_cleanup"
        for leftover in (
            install_metadata_path(dest),
            Path(dest) / COPY_INSTALL_MARKER_NAME,
        ):
            try:
                if leftover.is_symlink() or leftover.is_file():
                    leftover.unlink(missing_ok=True)
            except OSError:
                pass
    except InstallationMetadataError:
        raise
    except Exception as write_error:  # pylint: disable=broad-exception-caught
        _log_metadata_write_cause(dest, write_stage, write_error)
        _raise_metadata_error(dest, "INSTALL_METADATA_WRITE_FAILED")
    if mode == "copy":
        metadata = {
            "mode": mode,
            "source": resolved_source,
            "source_sha": source_sha,
            "installed_at": time.time(),
            "installation_id": installation_id,
            "content_identity": content_identity,
            "baseline_identity": baseline_identity,
            "file_fingerprints": file_fingerprints,
        }
        if not copy_install_identity_matches(
            dest, source, metadata=metadata,
        ):
            _raise_metadata_error(dest, "INSTALL_METADATA_VERIFY_FAILED")


def refresh_copy_install_baseline(dest: Path) -> bool:
    """安装器在 record 之后再次写入 dest 时，同步账本指纹（不升 generation）。

    返回是否成功更新了一条 active copy 行。
    """
    from xskill.ecosystems.install_ledger import get_default_ledger

    dest = Path(dest)
    ledger = get_default_ledger()
    meta = ledger.read_install(dest)
    if meta is None or meta.get("mode") != "copy":
        return False
    try:
        file_fingerprints = _safe_copy_file_fingerprints(dest)
        baseline_identity = _copy_baseline_identity(file_fingerprints)
    except OSError:
        return False
    return ledger.update_copy_baseline(
        dest,
        file_fingerprints=file_fingerprints,
        baseline_identity=baseline_identity,
    )


_COPY_REPAIR_SOURCE_EXCLUDE = frozenset({
    ".git",
    ".xskill-install-meta.json",
})


def _copy_repair_source_fingerprints(root: Path) -> dict[str, str]:
    return _safe_copy_file_fingerprints(
        root,
        exclude_root_names=_COPY_REPAIR_SOURCE_EXCLUDE,
        exclude_root_prefixes=(_INSTALL_META_PREFIX,),
    )


def _copy_repair_dest_user_fingerprints(
    fingerprints: dict[str, str],
) -> dict[str, str]:
    return {
        relative_name: file_hash
        for relative_name, file_hash in fingerprints.items()
        if relative_name.partition("/")[0]
        not in _COPY_REPAIR_SOURCE_EXCLUDE
    }


def repair_copy_install_baseline(
    dest: Path,
    *,
    apply: bool = True,
) -> CopyBaselineRepairStatus:
    """仅在 source 与 copy dest 完全一致时安全重算一条存量基线。

    该入口不会用当前 dest 无条件覆盖账本：任何待回流用户编辑、source 前进、
    特殊文件、身份不匹配或并发安装换代都会拒绝。写入使用安装世代、安装 ID
    与旧基线身份做 CAS；写后复核若发现外部竞态，则尽力回滚旧基线。
    """
    from xskill.ecosystems.install_ledger import get_default_ledger
    from xskill.skill.git import skill_repo_lock

    dest = Path(dest)
    ledger = get_default_ledger()
    try:
        metadata = ledger.read_install(dest)
        if metadata is None:
            return CopyBaselineRepairStatus.INVALID
        metadata = _validate_metadata(metadata, dest)
    except (InstallationMetadataError, OSError, ValueError):
        return CopyBaselineRepairStatus.INVALID
    raw_source = metadata.get("source")
    old_fingerprints = metadata.get("file_fingerprints")
    old_baseline_identity = metadata.get("baseline_identity")
    generation = metadata.get("generation")
    installation_id = metadata.get("installation_id")
    if (
        metadata.get("mode") != "copy"
        or not isinstance(raw_source, str)
        or not Path(raw_source).is_absolute()
        or not isinstance(old_fingerprints, dict)
        or not isinstance(old_baseline_identity, str)
        or isinstance(generation, bool)
        or not isinstance(generation, int)
        or not isinstance(installation_id, str)
        or not copy_install_identity_matches(
            dest, Path(raw_source), metadata=metadata,
        )
    ):
        return CopyBaselineRepairStatus.INVALID
    source = Path(raw_source)
    baseline_written = False
    new_baseline_identity = ""
    try:
        with skill_repo_lock(source, use_git_write_limit=False):
            dest_fingerprints = _safe_copy_file_fingerprints(dest)
            new_baseline_identity = _copy_baseline_identity(
                dest_fingerprints,
            )
            if (
                dest_fingerprints == old_fingerprints
                and new_baseline_identity == old_baseline_identity
            ):
                return CopyBaselineRepairStatus.CURRENT
            source_user_fingerprints = _copy_repair_source_fingerprints(
                source,
            )
            dest_user_fingerprints = (
                _copy_repair_dest_user_fingerprints(dest_fingerprints)
            )
            if (
                "SKILL.md" not in source_user_fingerprints
                or "SKILL.md" not in dest_user_fingerprints
            ):
                return CopyBaselineRepairStatus.INVALID
            if source_user_fingerprints != dest_user_fingerprints:
                return CopyBaselineRepairStatus.DIVERGED
            if not apply:
                return CopyBaselineRepairStatus.REPAIRABLE
            if not ledger.compare_and_swap_copy_baseline(
                dest,
                expected_generation=generation,
                expected_installation_id=installation_id,
                expected_baseline_identity=old_baseline_identity,
                file_fingerprints=dest_fingerprints,
                baseline_identity=new_baseline_identity,
            ):
                return CopyBaselineRepairStatus.CONCURRENT
            baseline_written = True
            verified_dest = _safe_copy_file_fingerprints(dest)
            verified_source_user = _copy_repair_source_fingerprints(
                source,
            )
            verified_dest_user = _copy_repair_dest_user_fingerprints(
                verified_dest,
            )
            current_metadata = ledger.read_install(dest)
            ledger_still_matches = (
                current_metadata is not None
                and current_metadata.get("generation") == generation
                and current_metadata.get("installation_id")
                == installation_id
                and current_metadata.get("baseline_identity")
                == new_baseline_identity
            )
            if not ledger_still_matches:
                baseline_written = False
                return CopyBaselineRepairStatus.CONCURRENT
            if (
                verified_dest == dest_fingerprints
                and verified_source_user == source_user_fingerprints
                and verified_dest_user == dest_user_fingerprints
            ):
                baseline_written = False
                return CopyBaselineRepairStatus.REPAIRED
            rolled_back = ledger.compare_and_swap_copy_baseline(
                dest,
                expected_generation=generation,
                expected_installation_id=installation_id,
                expected_baseline_identity=new_baseline_identity,
                file_fingerprints=old_fingerprints,
                baseline_identity=old_baseline_identity,
            )
            baseline_written = not rolled_back
            return (
                CopyBaselineRepairStatus.FAILED
                if rolled_back
                else CopyBaselineRepairStatus.CONCURRENT
            )
    except Exception as repair_error:  # pylint: disable=broad-exception-caught
        rollback_ok = False
        if baseline_written:
            try:
                rollback_ok = ledger.compare_and_swap_copy_baseline(
                    dest,
                    expected_generation=generation,
                    expected_installation_id=installation_id,
                    expected_baseline_identity=new_baseline_identity,
                    file_fingerprints=old_fingerprints,
                    baseline_identity=old_baseline_identity,
                )
            except Exception as rollback_error:  # pylint: disable=broad-exception-caught
                logger.error(
                    "copy baseline repair rollback failed path_hash=%s "
                    "error_type=%s",
                    _path_hash(dest),
                    type(rollback_error).__name__,
                )
        logger.warning(
            "copy baseline repair failed path_hash=%s error_type=%s "
            "rollback_ok=%s",
            _path_hash(dest),
            type(repair_error).__name__,
            rollback_ok,
        )
        return CopyBaselineRepairStatus.FAILED


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
    require_install_write(source, dest)
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
    for marker_name in (
        ".xskill_download.json",
        ".xskill_search.json",
        ".xskill_skillhub.json",
    ):
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
