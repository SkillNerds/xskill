"""UserEditAbsorbAgent —— 把用户对 ~/.claude/skills/<name>/ 的手改吸回 main
================================================================================

场景：因为 install_to_claude_code 用 symlink，用户改 ``~/.claude/skills/<name>/``
下任何文件实际改的是 xskill 源仓库。watcher 周期性检测：

- 任一文件（SKILL.md / scripts/* / references/* 或新增目录）mtime 超过该
  skill 最近一次 git commit 时间
- 且距 ``now ≥ 3 分钟``（避免用户编辑过程被误触）

满足 → 触发本 agent：读 git diff → LLM 写 commit message → 直接 commit
到 main → 如果存在 staging 一并删除（用户手改优先级压过灰度候选）。

行为上"用户手改"被视为 ground truth：
- 不走灰度（用户已经验证过了才会去手改）
- 不区分 baby/main：手改情况下都强制 commit 到 main（baby 也提前 graduate）
- 清空 candidates buffer（手改可能包含了原本要靠 cluster 攒的内容）
"""
from __future__ import annotations

import codecs
import hashlib
import json
import logging
import os
import secrets
import shutil
import stat
import tempfile
import time
from dataclasses import dataclass
from enum import Enum
from operator import attrgetter
from pathlib import Path
from typing import Any, Callable, Optional

from xskill.skill import candidates as C

logger = logging.getLogger("xskill.user_edit_absorb_agent")

# 用户停止编辑的最小静默时间——避免编辑过程被中途回流
USER_EDIT_QUIET_SECONDS = 180


SYSTEM_PROMPT = """你是 UserEditAbsorbAgent。某个 skill 的源文件被用户**手动**
修改了（通过 ``~/.claude/skills/<name>/`` 改的，因为 symlink 链接到我们的源仓
库）。你的任务是把用户改动作为 ground truth 吸收回 main 分支。

# 输入
我会给你：
  - skill_name（slug）
  - 该 skill 当前所在 git 分支
  - ``git diff`` 的完整内容（显示用户改了什么）

# 目标
1. 看 diff 判断用户改了什么（增 SKILL.md 内容？新加 scripts？删除某段？）
2. 调 ``absorb_user_edit_to_main(skill_name, message)`` 工具吸收：
   - 工具内部会：git add . + commit -m <message> + (若 baby) rename baby→main
     + (若有 staging) git branch -D staging
   - 你不需要操作 git，只需写一个合理的 commit message

# commit message 格式
``absorb user edit: <一句话总结用户改了什么>``，例：
  - ``absorb user edit: 用户在 ## 验证 阶段加了 lsof 命令``
  - ``absorb user edit: 用户删除了过时的 references/legacy.md``

# 硬禁止
- 不要在 diff 之外推断用户意图——用户改什么就吸什么
- 不要试图"修正"用户的改动——他们是 ground truth
- 不要调任何写文件工具：用户已经写完了，你只需 commit
"""


@dataclass
class UserEditAbsorbAgent:
    """每实例服务一个具体 skill；watcher 检测到手改触发。"""
    skill_dir: Path
    agno_agent_factory: Callable[..., Any]
    llm_cfg: dict

    def run(self) -> bool:
        """跑一次 absorb：读 diff → agent 给 message → commit + 清 candidates。

        返回 True 表示成功 commit（agent 调过 absorb 工具）；False 表示
        diff 为空 / agent 没 commit。
        """
        from xskill.skill.git import run_git

        # 读 diff 让 agent 看
        code, diff_out, _ = run_git(["diff", "HEAD"], cwd=str(self.skill_dir))
        # 也加 untracked 文件 (status 看 untracked 数量)
        _, status_out, _ = run_git(["status", "--porcelain"], cwd=str(self.skill_dir))
        if not diff_out and not status_out:
            return False
        candidate_snapshot = C.load_candidates(self.skill_dir)
        snapshot_atom_ids = {
            str(candidate["atom_id"])
            for candidate in candidate_snapshot.get("candidates", [])
            if candidate.get("atom_id")
        }

        from xskill.agents import agent_tools
        skill_name = self.skill_dir.name

        # 构造 user_msg：含 skill_name + 当前分支 + 完整 diff + 未追踪文件列表
        from xskill.skill.git import current_branch
        cur_branch = current_branch(str(self.skill_dir))
        user_msg_parts = [
            f"skill_name: {skill_name}",
            f"current_branch: {cur_branch}",
            "",
            "# git status (含未追踪):",
            status_out or "(no untracked)",
            "",
            "# git diff HEAD（已追踪文件的改动）:",
            (diff_out[:8000] if diff_out else "(no tracked file changes)"),
        ]
        user_msg = "\n".join(user_msg_parts)

        agent = self.agno_agent_factory(
            instructions=[SYSTEM_PROMPT],
            tools=[agent_tools.absorb_user_edit_to_main],
        )
        try:
            agent.run(user_msg)
        except Exception as absorb_error:
            logger.warning(
                "UserEditAbsorbAgent failed skill_id_hash=%s error_type=%s",
                hashlib.sha256(
                    skill_name.encode("utf-8"),
                ).hexdigest()[:12],
                type(absorb_error).__name__,
            )
            return False

        # 检查是否成功 commit（最新 commit message 含 "absorb user edit"）
        _, last_msg, _ = run_git(["log", "-1", "--format=%s"], cwd=str(self.skill_dir))
        if "absorb user edit" not in last_msg.lower():
            logger.warning(
                "UserEditAbsorbAgent ran but no absorb commit landed: %s "
                "(last commit: %r)",
                skill_name, last_msg[:120],
            )
            return False
        # 手改开始前已有的候选已被人工版本超越；LLM/commit 期间新到的候选
        # 属于后续轨迹，必须保留给下一轮整理。
        if snapshot_atom_ids:
            C.remove_candidates(self.skill_dir, snapshot_atom_ids)
        logger.info(
            "UserEditAbsorbAgent absorbed user edit skill_id_hash=%s",
            hashlib.sha256(
                skill_name.encode("utf-8"),
            ).hexdigest()[:12],
        )
        return True


def _max_workspace_mtime(skill_dir: Path) -> float:
    """扫该 skill 工作区所有相关文件 + 目录的 max(mtime)。

    跳过 git 内部 / candidates buffer / canary 物化 / ux 评分——这些是
    daemon 自己的运行时产物，不算"用户手改"。无相关文件返回 0.0。
    """
    max_mtime = 0.0
    for p in skill_dir.rglob("*"):
        try:
            rel = p.relative_to(skill_dir)
            parts = rel.parts
            if not parts:
                continue
            if parts[0] == ".git":
                continue
            if parts[0] in (".candidates.yml", ".canary", ".ux_scores.jsonl"):
                continue
            m = p.stat().st_mtime
            if m > max_mtime:
                max_mtime = m
        except (OSError, ValueError):
            continue
    return max_mtime


def has_pending_user_edit(skill_dir: Path) -> bool:
    """该 skill 工作区有未 commit 的用户手改（不管静默多久）。

    = ``detect_user_edits`` 的判据 (a)，去掉静默检查 (b)。

    判据：
    - 取 SKILL.md / scripts/** / references/** 等所有非 .git / 非
      .candidates.yml 文件的 max(mtime)
    - 该 mtime 比最近一次 git commit 时间严格大 ≥1 秒 → 有未 commit 改动

    ``git log --format=%ct`` 返回**整数秒**（Unix ts truncate 掉小数部分），
    ``os.stat().st_mtime`` 返回**浮点秒**。同一秒内"write file → git commit"
    时，file mtime = N.XXX 而 commit_ts = N → 浮点差 0.X 秒，会被误判为
    "用户编辑了文件"。要求 mtime 比 commit_ts 严格大 ≥1 秒才算真的编辑。
    """
    from xskill.skill.git import run_git

    if not (skill_dir / ".git").is_dir():
        return False

    # last commit timestamp
    code, ts_out, _ = run_git(
        ["log", "-1", "--format=%ct", "HEAD"], cwd=str(skill_dir),
    )
    if code != 0 or not ts_out.strip():
        return False
    try:
        last_commit_ts = float(ts_out.strip())
    except ValueError:
        return False

    max_mtime = _max_workspace_mtime(skill_dir)
    # 见 docstring：要求 mtime 比 commit_ts 严格大 ≥1 秒才算真的编辑。
    return max_mtime - last_commit_ts >= 1.0


def detect_user_edits(skill_dir: Path, *, quiet_seconds: int = USER_EDIT_QUIET_SECONDS) -> bool:
    """检测该 skill 是否有用户手改且已稳定 (>=3 分钟没新动作)。

    判据：
    - (a) ``has_pending_user_edit``：有未 commit 改动
    - (b) ``now - max_mtime ≥ quiet_seconds`` → 用户已停止编辑 ≥3 分钟

    两个都过才返回 True（表示该跑 absorb）。
    """
    import time

    if not has_pending_user_edit(skill_dir):
        return False
    max_mtime = _max_workspace_mtime(skill_dir)
    if (time.time() - max_mtime) < quiet_seconds:
        return False  # 改动太新，可能用户还在编辑
    return True


# ──────────────────────────────────────────────────────────────────
# dest → source 回流桥（通用 copy-mode install 回流）
# ──────────────────────────────────────────────────────────────────
#
# 凡是 ``ecosystems._fallback.install_dir`` 走到 copy 路径的生态（dest 是
# 真目录，跟源仓解耦），用户改 dest 都不会被 absorb agent 看到——absorb
# 走的是源仓 mtime。本模块的 ``reverse_sync_copy_dest`` 把 dest 用户改
# 灌回源仓，让 source mtime 看起来"刚被改过"，后续原有 absorb / push-edit
# 链路就能像处理普通源仓改动一样收编。
#
# 历史背景：openclaw 是第一个被迫走 copy 的生态（openclaw discovery 对
# 非 bundled 档做 realpath 检查，symlink 跑出 root 会被拒）；ngagent 在
# Windows non-DevMode 下也撞同样问题（Node.js Dirent 把 junction 当
# symlink 不当目录看，详见 issue #34），所以把这套机制泛化给所有 copy
# 模式的生态用。
#
# install-meta 由 ``_fallback._write_install_meta`` 统一写到
# ``dest.parent / .xskill-install-meta-<dest.name>.json``。本模块也兼容
# 读取 **dest 内部** 的老 ``.xskill-install-meta.json``（openclaw 旧版位置）
# ——新装的 skill 都已是新位置，老路径只为存量 dest 提供平滑过渡，不应
# 有新代码再往 dest 内部写 meta。

# openclaw 旧位置：写在 dest 内部（保留以兼容存量装出去的 dest）
_OPENCLAW_INSTALL_META = ".xskill-install-meta.json"

# 默认 reverse_sync exclude：``.git`` + 兼容 openclaw 老位置 meta（dest 内部）。
# 新位置 meta 在 ``dest.parent`` 旁边，``dest.rglob("*")`` 扫不到，无需 exclude。
# 老位置在 ``dest/.xskill-install-meta.json``——只要 dest 还可能存在老 meta
# （存量 openclaw 装好的 dest 升级前都是老位置），就必须在所有 helper 的默认
# exclude 里把它排掉，否则 install 时重写 meta 会触发误判 "用户改了 dest"。
_COPY_INSTALL_IDENTITY_MARKER = ".xskill-install-identity.json"
_OPEN_SUPPORTS_DIR_FD = os.open in os.supports_dir_fd
_REVERSE_TRANSACTION_SCHEMA = 1
_MAX_REVERSE_TRANSACTIONS = 1
_MAX_REVERSE_MANIFEST_BYTES = 1024 * 1024
_DEFAULT_REVERSE_SYNC_EXCLUDE = frozenset({
    ".git",
    _OPENCLAW_INSTALL_META,
    _COPY_INSTALL_IDENTITY_MARKER,
})


class ReverseSyncStatus(str, Enum):
    """copy 安装回流的无歧义结果。"""

    NO_EDIT = "no_edit"
    RECENT_EDIT = "recent_edit"
    SYNCED = "synced"
    FAILED = "failed"


class _DestEditStatus(str, Enum):
    NO_EDIT = "no_edit"
    RECENT_EDIT = "recent_edit"
    READY = "ready"
    FAILED = "failed"


@dataclass(frozen=True)
class _SafeDestFile:
    relative_path: Path
    device: int
    inode: int
    mode: int
    size: int
    mtime_ns: int
    ctime_ns: int


@dataclass(frozen=True)
class _DestEditAssessment:
    status: _DestEditStatus
    files: tuple[_SafeDestFile, ...] = ()


def _reverse_sync_path_hash(path: Path) -> str:
    return hashlib.sha256(
        os.path.abspath(os.path.normpath(str(path))).encode(
            "utf-8", errors="surrogatepass",
        ),
    ).hexdigest()[:16]


def _log_reverse_sync_failure(dest_dir: Path, error_type: str) -> None:
    logger.warning(
        "reverse sync failed path_hash=%s error_type=%s",
        _reverse_sync_path_hash(dest_dir),
        error_type,
    )


def _log_reverse_transaction_failure(
    source_dir: Path,
    stage: str,
    error: BaseException | None = None,
) -> None:
    """记录不包含底层路径或异常文本的回流事务失败原因。"""
    logger.warning(
        "reverse transaction failed path_hash=%s stage=%s "
        "exception_type=%s errno=%s winerror=%s",
        _reverse_sync_path_hash(source_dir),
        stage,
        type(error).__name__ if error is not None else None,
        getattr(error, "errno", None),
        getattr(error, "winerror", None),
    )


def _read_install_meta_ts(dest_dir: Path) -> tuple[bool, Optional[float]]:
    """读取 installed_at，返回 ``(读取状态正常, 时间戳)``。

    优先读**新位置**（``dest.parent`` 旁边的 ``.xskill-install-meta-<name>.json``）；
    新位置缺失才退到 openclaw 旧位置（``dest/.xskill-install-meta.json``）
    做兼容——存量 openclaw dest 升级前还是老位置；新装的统一在新位置。
    """
    from xskill.ecosystems.installation import (
        InstallationMetadataError,
        read_install_metadata,
    )

    try:
        data = read_install_metadata(dest_dir)
    except InstallationMetadataError:
        return False, None
    if data is None:
        data = _read_legacy_meta_lenient(dest_dir / _OPENCLAW_INSTALL_META)
    if data is None:
        return True, None
    installed_timestamp = data.get("installed_at")
    if (
        isinstance(installed_timestamp, bool)
        or not isinstance(installed_timestamp, (int, float))
    ):
        return False, None
    return True, float(installed_timestamp)


def _read_legacy_meta_lenient(meta_path: Path) -> Optional[dict]:
    """宽松读 openclaw 老位置 meta，只作 installed_at 数据源。

    该文件由 ``install_to_openclaw`` 为 canary 比对而写，只有
    source_sha/side/installed_at/ecosystem 四个字段——不是安装账格式，
    不能过 ``read_install_metadata_file`` 的严格校验（必报 DAMAGED）。
    这里只取其 dict，取不到就当没有（安全跳过，不算读失败）。
    """
    try:
        parsed = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, UnicodeDecodeError):
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed


def _is_reparse_point(file_stat: os.stat_result) -> bool:
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    file_attributes = getattr(file_stat, "st_file_attributes", 0)
    return bool(reparse_flag and file_attributes & reparse_flag)


def _safe_dest_files(
    dest_dir: Path, exclude: frozenset[str],
) -> tuple[bool, tuple[_SafeDestFile, ...]]:
    """nofollow 扫描 dest；任一 symlink/reparse/特殊文件都整体失败。"""
    scanned_files: list[_SafeDestFile] = []
    pending_dirs: list[tuple[Path, Path]] = [(dest_dir, Path())]
    try:
        root_stat = dest_dir.lstat()
        if (
            not stat.S_ISDIR(root_stat.st_mode)
            or _is_reparse_point(root_stat)
        ):
            return False, ()
        while pending_dirs:
            current_dir, relative_dir = pending_dirs.pop()
            with os.scandir(current_dir) as entries:
                for entry in sorted(entries, key=attrgetter("name")):
                    relative_path = relative_dir / entry.name
                    if relative_path.parts[0] in exclude:
                        continue
                    # Windows 的 DirEntry.stat() 可能返回没有真实文件身份的
                    # WIN32_FIND_DATA 缓存；lstat 与后续 fstat 统一走句柄信息。
                    entry_stat = Path(entry.path).lstat()
                    if _is_reparse_point(entry_stat):
                        return False, ()
                    if stat.S_ISDIR(entry_stat.st_mode):
                        pending_dirs.append(
                            (Path(entry.path), relative_path),
                        )
                        continue
                    if not stat.S_ISREG(entry_stat.st_mode):
                        return False, ()
                    scanned_files.append(_SafeDestFile(
                        relative_path=relative_path,
                        device=entry_stat.st_dev,
                        inode=entry_stat.st_ino,
                        mode=entry_stat.st_mode,
                        size=entry_stat.st_size,
                        mtime_ns=entry_stat.st_mtime_ns,
                        ctime_ns=entry_stat.st_ctime_ns,
                    ))
    except (OSError, ValueError):
        return False, ()
    return True, tuple(scanned_files)


def _dest_edit_status(
    dest_dir: Path, *, quiet_seconds: int,
    exclude: frozenset[str],
) -> _DestEditAssessment:
    try:
        root_stat = dest_dir.lstat()
        # Link/junction 安装与 source 共享同一份文件，不需要 copy
        # reverse-sync。返回 NO_EDIT 让调用方继续走 git status；仍不跟随
        # 链接扫描，真实 copy 内部出现链接时继续 fail-closed。
        if stat.S_ISLNK(root_stat.st_mode) or _is_reparse_point(root_stat):
            return _DestEditAssessment(_DestEditStatus.NO_EDIT)
        if not stat.S_ISDIR(root_stat.st_mode):
            return _DestEditAssessment(_DestEditStatus.FAILED)
    except FileNotFoundError:
        return _DestEditAssessment(_DestEditStatus.NO_EDIT)
    except OSError:
        return _DestEditAssessment(_DestEditStatus.FAILED)
    metadata_ok, installed_at = _read_install_meta_ts(dest_dir)
    if not metadata_ok or installed_at is None:
        return _DestEditAssessment(_DestEditStatus.FAILED)
    scan_ok, scanned_files = _safe_dest_files(dest_dir, exclude)
    if not scan_ok:
        return _DestEditAssessment(_DestEditStatus.FAILED)
    max_mtime = max(
        (file_info.mtime_ns / 1_000_000_000 for file_info in scanned_files),
        default=0.0,
    )
    if max_mtime - installed_at < 1.0:
        return _DestEditAssessment(
            _DestEditStatus.NO_EDIT, scanned_files,
        )
    if (time.time() - max_mtime) < quiet_seconds:
        return _DestEditAssessment(
            _DestEditStatus.RECENT_EDIT, scanned_files,
        )
    return _DestEditAssessment(_DestEditStatus.READY, scanned_files)


def has_pending_dest_edit(
    dest_dir: Path, *,
    quiet_seconds: int = USER_EDIT_QUIET_SECONDS,
    exclude: frozenset[str] = _DEFAULT_REVERSE_SYNC_EXCLUDE,
) -> bool:
    """dest 是否相对安装基线有文件状态变化且已结束静默期。"""
    from xskill.ecosystems.installation import read_copy_install_baseline

    assessment = _dest_edit_status(
        dest_dir,
        quiet_seconds=quiet_seconds,
        exclude=exclude,
    )
    if assessment.status == _DestEditStatus.FAILED:
        return False
    if not assessment.files:
        return False
    try:
        baseline_fingerprints = read_copy_install_baseline(dest_dir)
    except Exception:  # pylint: disable=broad-exception-caught
        _log_reverse_sync_failure(
            dest_dir, "REVERSE_SYNC_BASELINE_FAILED",
        )
        return False
    try:
        candidate_files = [
            file_info
            for file_info in assessment.files
            if _hash_verified_file(dest_dir, file_info)
            != baseline_fingerprints.get(
                file_info.relative_path.as_posix(),
            )
        ]
    except (OSError, ValueError):
        _log_reverse_sync_failure(
            dest_dir, "REVERSE_SYNC_CONTENT_READ_FAILED",
        )
        return False
    if not candidate_files:
        return False
    last_change_time = max(
        max(file_info.mtime_ns, file_info.ctime_ns)
        / 1_000_000_000
        for file_info in candidate_files
    )
    return time.time() - last_change_time >= quiet_seconds


def _ensure_real_directory(root: Path, relative_dir: Path) -> list[Path]:
    """创建缺失父目录；已有路径必须是真目录且不是 reparse point。"""
    created_dirs: list[Path] = []
    current_dir = root
    for path_part in relative_dir.parts:
        current_dir = current_dir / path_part
        try:
            current_stat = current_dir.lstat()
        except FileNotFoundError:
            current_dir.mkdir()
            created_dirs.append(current_dir)
            continue
        if (
            not stat.S_ISDIR(current_stat.st_mode)
            or _is_reparse_point(current_stat)
        ):
            raise OSError("unsafe directory entry")
    return created_dirs


def _validate_real_directory_prefix(root: Path, relative_dir: Path) -> None:
    """验证已存在的父目录前缀，遇到首个缺失目录即停止。"""
    current_dir = root
    for path_part in relative_dir.parts:
        current_dir = current_dir / path_part
        try:
            current_stat = current_dir.lstat()
        except FileNotFoundError:
            return
        if (
            not stat.S_ISDIR(current_stat.st_mode)
            or _is_reparse_point(current_stat)
        ):
            raise OSError("unsafe directory entry")


def _copy_verified_file_to_stage(
    dest_root: Path,
    file_info: _SafeDestFile,
    staged_path: Path,
) -> None:
    """O_NOFOLLOW + fstat 身份校验后把一个普通文件写入受控 staging。"""
    open_flags = os.O_RDONLY
    open_flags |= getattr(os, "O_BINARY", 0)
    open_flags |= getattr(os, "O_NOFOLLOW", 0)
    open_flags |= getattr(os, "O_NONBLOCK", 0)
    directory_flags = os.O_RDONLY
    directory_flags |= getattr(os, "O_DIRECTORY", 0)
    directory_flags |= getattr(os, "O_NOFOLLOW", 0)
    opened_directories: list[int] = []
    try:
        if _OPEN_SUPPORTS_DIR_FD:
            current_directory = os.open(dest_root, directory_flags)
            opened_directories.append(current_directory)
            for path_part in file_info.relative_path.parent.parts:
                current_directory = os.open(
                    path_part,
                    directory_flags,
                    dir_fd=current_directory,
                )
                opened_directories.append(current_directory)
            file_descriptor = os.open(
                file_info.relative_path.name,
                open_flags,
                dir_fd=current_directory,
            )
        elif os.name == "nt":
            root_stat = dest_root.lstat()
            if (
                not stat.S_ISDIR(root_stat.st_mode)
                or _is_reparse_point(root_stat)
            ):
                raise OSError("unsafe source root")
            _validate_real_directory_prefix(
                dest_root, file_info.relative_path.parent,
            )
            file_descriptor = os.open(
                dest_root / file_info.relative_path,
                open_flags,
            )
        else:
            raise OSError("safe directory-relative open unavailable")
    except Exception:
        for directory_descriptor in reversed(opened_directories):
            os.close(directory_descriptor)
        raise
    try:
        opened_stat = os.fstat(file_descriptor)
        expected_identity = (
            file_info.device,
            file_info.inode,
            stat.S_IFMT(file_info.mode),
        )
        opened_identity = (
            opened_stat.st_dev,
            opened_stat.st_ino,
            stat.S_IFMT(opened_stat.st_mode),
        )
        if (
            opened_identity != expected_identity
            or not stat.S_ISREG(opened_stat.st_mode)
            or _is_reparse_point(opened_stat)
        ):
            raise OSError("source identity changed")
        expected_content_stat = (
            file_info.size,
            file_info.mtime_ns,
        )
        opened_content_stat = (
            opened_stat.st_size,
            opened_stat.st_mtime_ns,
        )
        if os.name != "nt":
            expected_content_stat += (file_info.ctime_ns,)
            opened_content_stat += (opened_stat.st_ctime_ns,)
        if opened_content_stat != expected_content_stat:
            raise OSError("source changed before reading")
        if os.name != "nt":
            os.set_blocking(file_descriptor, True)
        with os.fdopen(
            os.dup(file_descriptor), "rb", closefd=True,
        ) as source_file, open(staged_path, "wb") as staged_file:
            shutil.copyfileobj(source_file, staged_file)
            staged_file.flush()
            os.fsync(staged_file.fileno())
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
            raise OSError("source changed while reading")
        os.chmod(staged_path, stat.S_IMODE(file_info.mode))
        os.utime(
            staged_path,
            ns=(file_info.mtime_ns, file_info.mtime_ns),
        )
    finally:
        os.close(file_descriptor)
        for directory_descriptor in reversed(opened_directories):
            os.close(directory_descriptor)


def _hash_verified_file(
    root: Path,
    file_info: _SafeDestFile,
    *,
    normalize_utf8_newlines: bool = False,
) -> str:
    """安全读取文件并返回摘要；非文本的换行规范化摘要为空字符串。"""
    open_flags = os.O_RDONLY
    open_flags |= getattr(os, "O_BINARY", 0)
    open_flags |= getattr(os, "O_NOFOLLOW", 0)
    open_flags |= getattr(os, "O_NONBLOCK", 0)
    directory_flags = os.O_RDONLY
    directory_flags |= getattr(os, "O_DIRECTORY", 0)
    directory_flags |= getattr(os, "O_NOFOLLOW", 0)
    opened_directories: list[int] = []
    try:
        if _OPEN_SUPPORTS_DIR_FD:
            current_directory = os.open(root, directory_flags)
            opened_directories.append(current_directory)
            for path_part in file_info.relative_path.parent.parts:
                current_directory = os.open(
                    path_part,
                    directory_flags,
                    dir_fd=current_directory,
                )
                opened_directories.append(current_directory)
            file_descriptor = os.open(
                file_info.relative_path.name,
                open_flags,
                dir_fd=current_directory,
            )
        elif os.name == "nt":
            root_stat = root.lstat()
            if (
                not stat.S_ISDIR(root_stat.st_mode)
                or _is_reparse_point(root_stat)
            ):
                raise OSError("unsafe file root")
            _validate_real_directory_prefix(
                root, file_info.relative_path.parent,
            )
            file_descriptor = os.open(
                root / file_info.relative_path,
                open_flags,
            )
        else:
            raise OSError("safe directory-relative open unavailable")
    except Exception:
        for directory_descriptor in reversed(opened_directories):
            os.close(directory_descriptor)
        raise
    try:
        opened_stat = os.fstat(file_descriptor)
        expected_identity = (
            file_info.device,
            file_info.inode,
            stat.S_IFMT(file_info.mode),
        )
        opened_identity = (
            opened_stat.st_dev,
            opened_stat.st_ino,
            stat.S_IFMT(opened_stat.st_mode),
        )
        if (
            opened_identity != expected_identity
            or not stat.S_ISREG(opened_stat.st_mode)
            or _is_reparse_point(opened_stat)
        ):
            raise OSError("file identity changed")
        expected_content_stat = (
            file_info.size,
            file_info.mtime_ns,
        )
        opened_content_stat = (
            opened_stat.st_size,
            opened_stat.st_mtime_ns,
        )
        if os.name != "nt":
            expected_content_stat += (file_info.ctime_ns,)
            opened_content_stat += (opened_stat.st_ctime_ns,)
        if opened_content_stat != expected_content_stat:
            raise OSError("file changed before reading")
        if os.name != "nt":
            os.set_blocking(file_descriptor, True)
        digest = hashlib.sha256()
        pending_cr = False
        is_text = True
        if not normalize_utf8_newlines:
            while True:
                file_chunk = os.read(file_descriptor, 1024 * 1024)
                if not file_chunk:
                    break
                digest.update(file_chunk)
        else:
            def raw_chunks():
                while True:
                    file_chunk = os.read(file_descriptor, 1024 * 1024)
                    if not file_chunk:
                        break
                    yield file_chunk

            try:
                decoded_chunks = codecs.iterdecode(
                    raw_chunks(), "utf-8", errors="strict",
                )
                for decoded_chunk in decoded_chunks:
                    if "\x00" in decoded_chunk:
                        is_text = False
                        break
                    if pending_cr:
                        digest.update(b"\n")
                        if decoded_chunk.startswith("\n"):
                            decoded_chunk = decoded_chunk[1:]
                        pending_cr = False
                    if decoded_chunk.endswith("\r"):
                        decoded_chunk = decoded_chunk[:-1]
                        pending_cr = True
                    digest.update(
                        decoded_chunk.replace("\r\n", "\n")
                        .replace("\r", "\n")
                        .encode("utf-8"),
                    )
            except UnicodeDecodeError:
                is_text = False
            if not is_text:
                while os.read(file_descriptor, 1024 * 1024):
                    pass
            elif pending_cr:
                digest.update(b"\n")
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
            raise OSError("file changed while reading")
        return digest.hexdigest() if is_text else ""
    finally:
        os.close(file_descriptor)
        for directory_descriptor in reversed(opened_directories):
            os.close(directory_descriptor)


def _safe_source_file_info(
    source_dir: Path, relative_path: Path,
) -> _SafeDestFile | None:
    _validate_real_directory_prefix(
        source_dir, relative_path.parent,
    )
    source_path = source_dir / relative_path
    try:
        source_stat = source_path.lstat()
    except FileNotFoundError:
        return None
    if (
        not stat.S_ISREG(source_stat.st_mode)
        or _is_reparse_point(source_stat)
    ):
        raise OSError("unsafe source target")
    return _SafeDestFile(
        relative_path=relative_path,
        device=source_stat.st_dev,
        inode=source_stat.st_ino,
        mode=source_stat.st_mode,
        size=source_stat.st_size,
        mtime_ns=source_stat.st_mtime_ns,
        ctime_ns=source_stat.st_ctime_ns,
    )


def _path_signature(path: Path) -> tuple[int, int, int, int, int] | None:
    try:
        path_stat = path.lstat()
    except FileNotFoundError:
        return None
    return (
        path_stat.st_dev,
        path_stat.st_ino,
        stat.S_IFMT(path_stat.st_mode),
        path_stat.st_size,
        path_stat.st_mtime_ns,
    )


def _fsync_directory(directory: Path) -> None:
    if os.name == "nt":
        # Windows 的 os.open() 不能打开目录，标准库也没有可移植的目录
        # fsync；文件本身已在写入后 fsync，再依赖同卷原子 replace。
        return
    directory_flags = os.O_RDONLY
    directory_flags |= getattr(os, "O_DIRECTORY", 0)
    directory_flags |= getattr(os, "O_NOFOLLOW", 0)
    directory_descriptor = os.open(directory, directory_flags)
    try:
        directory_stat = os.fstat(directory_descriptor)
        if (
            not stat.S_ISDIR(directory_stat.st_mode)
            or _is_reparse_point(directory_stat)
        ):
            raise OSError("unsafe transaction directory")
        os.fsync(directory_descriptor)
    finally:
        os.close(directory_descriptor)


def _fsync_regular_file(path: Path) -> None:
    open_flags = os.O_RDWR if os.name == "nt" else os.O_RDONLY
    open_flags |= getattr(os, "O_BINARY", 0)
    open_flags |= getattr(os, "O_NOFOLLOW", 0)
    file_descriptor = os.open(path, open_flags)
    try:
        file_stat = os.fstat(file_descriptor)
        if (
            not stat.S_ISREG(file_stat.st_mode)
            or _is_reparse_point(file_stat)
        ):
            raise OSError("unsafe transaction file")
        os.fsync(file_descriptor)
    finally:
        os.close(file_descriptor)


def _atomic_write_reverse_manifest(
    manifest_path: Path, manifest: dict,
) -> None:
    manifest_bytes = json.dumps(
        manifest,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8", errors="strict")
    if len(manifest_bytes) > _MAX_REVERSE_MANIFEST_BYTES:
        raise OSError("reverse transaction manifest too large")
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{manifest_path.name}.tmp-",
        dir=manifest_path.parent,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(file_descriptor, "wb") as output:
            output.write(manifest_bytes)
            output.flush()
            os.fsync(output.fileno())
        temporary_path.replace(manifest_path)
        _fsync_directory(manifest_path.parent)
    except Exception:
        try:
            temporary_path.unlink()
        except OSError as cleanup_error:
            logger.warning(
                "reverse manifest temporary cleanup failed "
                "path_hash=%s exception_type=%s",
                _reverse_sync_path_hash(temporary_path),
                type(cleanup_error).__name__,
            )
        raise


def _reverse_transaction_prefix(source_dir: Path) -> str:
    source_hash = _reverse_sync_path_hash(source_dir)
    return f".xskill-reverse-{source_hash}-"


def _pending_reverse_manifests(source_dir: Path) -> tuple[Path, ...]:
    prefix = _reverse_transaction_prefix(source_dir)
    manifests: list[Path] = []
    data_names: set[str] = set()
    temporary_paths: list[Path] = []
    with os.scandir(source_dir.parent) as entries:
        for entry in entries:
            if (
                entry.name.startswith(prefix)
                and entry.name.endswith(".json")
            ):
                entry_stat = entry.stat(follow_symlinks=False)
                if (
                    not stat.S_ISREG(entry_stat.st_mode)
                    or _is_reparse_point(entry_stat)
                ):
                    raise OSError("unsafe reverse transaction manifest")
                manifests.append(Path(entry.path))
                continue
            if (
                entry.name.startswith(prefix)
                and entry.name.endswith(".data")
            ):
                entry_stat = entry.stat(follow_symlinks=False)
                if (
                    not stat.S_ISDIR(entry_stat.st_mode)
                    or _is_reparse_point(entry_stat)
                ):
                    raise OSError("unsafe reverse transaction data")
                data_names.add(entry.name)
                continue
            if (
                entry.name.startswith(f".{prefix}")
                and ".json.tmp-" in entry.name
            ):
                entry_stat = entry.stat(follow_symlinks=False)
                if (
                    not stat.S_ISREG(entry_stat.st_mode)
                    or _is_reparse_point(entry_stat)
                ):
                    raise OSError("unsafe reverse transaction temporary")
                temporary_paths.append(Path(entry.path))
    manifests.sort()
    if len(temporary_paths) > 16:
        raise OSError("too many reverse transaction temporaries")
    for temporary_path in temporary_paths:
        temporary_path.unlink()
    expected_data_names = {
        f"{manifest_path.name[:-5]}.data"
        for manifest_path in manifests
    }
    if data_names - expected_data_names:
        raise OSError("orphan reverse transaction data")
    return tuple(manifests)


def _read_reverse_manifest(
    source_dir: Path, manifest_path: Path,
) -> dict:
    manifest_stat = manifest_path.lstat()
    if (
        not stat.S_ISREG(manifest_stat.st_mode)
        or _is_reparse_point(manifest_stat)
        or manifest_stat.st_size > _MAX_REVERSE_MANIFEST_BYTES
    ):
        raise OSError("unsafe reverse transaction manifest")
    open_flags = os.O_RDONLY
    open_flags |= getattr(os, "O_BINARY", 0)
    open_flags |= getattr(os, "O_NOFOLLOW", 0)
    file_descriptor = os.open(manifest_path, open_flags)
    try:
        opened_stat = os.fstat(file_descriptor)
        if (
            not stat.S_ISREG(opened_stat.st_mode)
            or (
                opened_stat.st_dev,
                opened_stat.st_ino,
                opened_stat.st_size,
            ) != (
                manifest_stat.st_dev,
                manifest_stat.st_ino,
                manifest_stat.st_size,
            )
        ):
            raise OSError("reverse transaction manifest changed")
        manifest_chunks: list[bytes] = []
        remaining = _MAX_REVERSE_MANIFEST_BYTES + 1
        while remaining > 0:
            manifest_chunk = os.read(
                file_descriptor, min(remaining, 8192),
            )
            if not manifest_chunk:
                break
            manifest_chunks.append(manifest_chunk)
            remaining -= len(manifest_chunk)
        raw_manifest = b"".join(manifest_chunks)
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
            raise OSError("reverse transaction manifest changed")
    finally:
        os.close(file_descriptor)
    if len(raw_manifest) > _MAX_REVERSE_MANIFEST_BYTES:
        raise OSError("reverse transaction manifest too large")
    manifest = json.loads(raw_manifest.decode("utf-8", errors="strict"))
    expected_prefix = _reverse_transaction_prefix(source_dir)
    if (
        not isinstance(manifest, dict)
        or manifest.get("schema_version") != _REVERSE_TRANSACTION_SCHEMA
        or manifest.get("source_hash") != _reverse_sync_path_hash(source_dir)
        or manifest.get("state") not in {
            "initializing", "prepared", "committing", "committed",
        }
        or not isinstance(manifest.get("data_name"), str)
        or not manifest["data_name"].startswith(expected_prefix)
        or not manifest["data_name"].endswith(".data")
        or Path(manifest["data_name"]).name != manifest["data_name"]
        or not isinstance(manifest.get("files"), list)
    ):
        raise OSError("invalid reverse transaction manifest")
    return manifest


def _manifest_relative_path(raw_path: object) -> Path:
    if not isinstance(raw_path, str):
        raise OSError("invalid reverse transaction path")
    relative_path = Path(raw_path)
    if (
        relative_path.is_absolute()
        or not relative_path.parts
        or any(path_part in {"", ".", ".."} for path_part in relative_path.parts)
    ):
        raise OSError("invalid reverse transaction path")
    return relative_path


def _manifest_signature(value: object) -> tuple[int, int, int, int, int] | None:
    if value is None:
        return None
    if (
        not isinstance(value, list)
        or len(value) != 5
        or any(
            isinstance(item, bool) or not isinstance(item, int)
            for item in value
        )
    ):
        raise OSError("invalid reverse transaction identity")
    return tuple(value)


def _cleanup_reverse_transaction(
    manifest_path: Path, data_dir: Path,
) -> bool:
    try:
        data_signature = _path_signature(data_dir)
        if data_signature is not None:
            data_stat = data_dir.lstat()
            if (
                not stat.S_ISDIR(data_stat.st_mode)
                or _is_reparse_point(data_stat)
            ):
                return False
            shutil.rmtree(data_dir)
            _fsync_directory(data_dir.parent)
        manifest_path.unlink()
        _fsync_directory(manifest_path.parent)
        return True
    except OSError:
        return False


def _rollback_reverse_transaction(
    source_dir: Path,
    manifest_path: Path,
    manifest: dict,
) -> bool:
    data_dir = source_dir.parent / manifest["data_name"]
    rollback_dir = data_dir / "rollback"
    try:
        for entry in reversed(manifest["files"]):
            if not isinstance(entry, dict):
                return False
            relative_path = _manifest_relative_path(entry.get("path"))
            original_signature = _manifest_signature(
                entry.get("original_signature"),
            )
            staged_signature = _manifest_signature(
                entry.get("staged_signature"),
            )
            if staged_signature is None:
                return False
            target_path = source_dir / relative_path
            backup_path = rollback_dir / relative_path
            current_signature = _path_signature(target_path)
            backup_signature = _path_signature(backup_path)
            if backup_signature is not None:
                if backup_signature != original_signature:
                    return False
                if current_signature is not None:
                    if current_signature != staged_signature:
                        return False
                    target_path.unlink()
                    _fsync_directory(target_path.parent)
                _ensure_real_directory(
                    source_dir, relative_path.parent,
                )
                backup_path.replace(target_path)
                _fsync_regular_file(target_path)
                _fsync_directory(target_path.parent)
                continue
            if original_signature is None:
                if current_signature is None:
                    continue
                if current_signature != staged_signature:
                    return False
                target_path.unlink()
                _fsync_directory(target_path.parent)
                continue
            if current_signature != original_signature:
                return False
        return _cleanup_reverse_transaction(
            manifest_path, data_dir,
        )
    except (OSError, ValueError):
        return False


def _recover_pending_reverse_transaction(source_dir: Path) -> bool:
    try:
        manifests = _pending_reverse_manifests(source_dir)
        if len(manifests) > _MAX_REVERSE_TRANSACTIONS:
            return False
        if not manifests:
            return True
        manifest_path = manifests[0]
        manifest = _read_reverse_manifest(source_dir, manifest_path)
        data_dir = source_dir.parent / manifest["data_name"]
        if manifest["state"] == "committed":
            return _cleanup_reverse_transaction(
                manifest_path, data_dir,
            )
        if manifest["state"] == "initializing" and not manifest["files"]:
            return _cleanup_reverse_transaction(
                manifest_path, data_dir,
            )
        return _rollback_reverse_transaction(
            source_dir, manifest_path, manifest,
        )
    except (OSError, UnicodeDecodeError, ValueError):
        return False


def _create_reverse_transaction(
    source_dir: Path,
) -> tuple[Path, Path, dict]:
    transaction_token = secrets.token_hex(12)
    transaction_name = (
        f"{_reverse_transaction_prefix(source_dir)}{transaction_token}"
    )
    manifest_path = source_dir.parent / f"{transaction_name}.json"
    data_dir = source_dir.parent / f"{transaction_name}.data"
    manifest = {
        "schema_version": _REVERSE_TRANSACTION_SCHEMA,
        "source_hash": _reverse_sync_path_hash(source_dir),
        "data_name": data_dir.name,
        "state": "initializing",
        "files": [],
    }
    _atomic_write_reverse_manifest(manifest_path, manifest)
    data_dir.mkdir()
    (data_dir / "staged").mkdir()
    (data_dir / "rollback").mkdir()
    _fsync_directory(data_dir)
    _fsync_directory(data_dir.parent)
    return manifest_path, data_dir, manifest


def _commit_reverse_transaction(
    source_dir: Path,
    manifest_path: Path,
    data_dir: Path,
    manifest: dict,
) -> bool:
    staged_dir = data_dir / "staged"
    rollback_dir = data_dir / "rollback"
    transaction_stage = "write_committing_manifest"
    try:
        manifest["state"] = "committing"
        _atomic_write_reverse_manifest(manifest_path, manifest)
        for entry in manifest["files"]:
            transaction_stage = "check_preconditions"
            relative_path = _manifest_relative_path(entry["path"])
            target_path = source_dir / relative_path
            staged_path = staged_dir / relative_path
            backup_path = rollback_dir / relative_path
            original_signature = _manifest_signature(
                entry["original_signature"],
            )
            staged_signature = _manifest_signature(
                entry["staged_signature"],
            )
            if (
                staged_signature is None
                or _path_signature(staged_path) != staged_signature
                or _path_signature(target_path) != original_signature
            ):
                raise OSError("reverse transaction precondition changed")
            transaction_stage = "prepare_directories"
            _ensure_real_directory(source_dir, relative_path.parent)
            if original_signature is not None:
                _ensure_real_directory(
                    rollback_dir, relative_path.parent,
                )
                transaction_stage = "sync_source"
                _fsync_regular_file(target_path)
                transaction_stage = "source_to_rollback"
                target_path.replace(backup_path)
                _fsync_directory(target_path.parent)
                _fsync_directory(backup_path.parent)
            transaction_stage = "sync_staged"
            _fsync_regular_file(staged_path)
            transaction_stage = "staged_to_source"
            staged_path.replace(target_path)
            transaction_stage = "sync_replaced_source"
            _fsync_regular_file(target_path)
            _fsync_directory(target_path.parent)
        transaction_stage = "write_committed_manifest"
        manifest["state"] = "committed"
        _atomic_write_reverse_manifest(manifest_path, manifest)
        transaction_stage = "cleanup"
        cleaned = _cleanup_reverse_transaction(
            manifest_path, data_dir,
        )
        if not cleaned:
            _log_reverse_transaction_failure(
                source_dir, transaction_stage,
            )
        return cleaned
    except (OSError, ValueError) as transaction_error:
        _log_reverse_transaction_failure(
            source_dir, transaction_stage, transaction_error,
        )
        return False


def reverse_sync_copy_dest(
    dest_dir: Path, source_dir: Path,
    *,
    exclude: frozenset[str] = _DEFAULT_REVERSE_SYNC_EXCLUDE,
    quiet_seconds: int = USER_EDIT_QUIET_SECONDS,
) -> ReverseSyncStatus:
    """通用 copy-mode 回流：把 dest 用户改灌回 source。

    任何 ``install_dir`` 走到 copy 路径的生态都能用（openclaw / ngagent /
    其它落到 copy fallback 的）。``exclude`` 默认只排 ``.git``；调用方
    可以加更多（如 openclaw 兼容路径加 ``_OPENCLAW_INSTALL_META``）。

    返回 :class:`ReverseSyncStatus`，明确区分无修改、仍在编辑、同步成功和失败。

    流程：
    1. ``has_pending_dest_edit`` 检查（dest 有改且静默 ≥quiet_seconds）
    2. 抢源仓 ``skill_repo_lock``——跟 CC absorb / canary flip 用同一把锁
    3. 遍历 dest 文件（跳 exclude 第一段路径），相对安装基线做三方比较：
       仅回流 dest 单边变更；source 单边变更保留；同文件双边分叉则失败
    4. 留意：**不删** source 里 dest 没有的文件（避免误删源仓里 ``.canary``
       等 xskill 自己产物；用户要删请在源仓直接删）
    5. 所有文件都先进入同盘 staging，再逐文件原子替换；中途失败恢复 source
       原内容，避免留下半写状态。

    并发：copy 期间 dest 也可能继续变化。锁只保护 source；任何扫描/复制异常
    返回 ``FAILED``，调用安装器必须保留 dest，供下一轮安全重试。
    """
    from xskill.ecosystems.installation import (
        adopt_orphan_copy_install,
        read_copy_install_baseline,
    )
    from xskill.skill.git import skill_repo_lock

    # 孤儿自愈：迁移失败留下的存量 dest 没有账本行，先按生态目录内老 meta
    # 的 source_sha 从 git 重建安装基线登记；失败则维持原冻结语义。
    adopt_orphan_copy_install(
        dest_dir, source_dir,
        legacy_meta_path=Path(dest_dir) / _OPENCLAW_INSTALL_META,
    )

    try:
        with skill_repo_lock(source_dir):
            source_stat = source_dir.lstat()
            if (
                not stat.S_ISDIR(source_stat.st_mode)
                or _is_reparse_point(source_stat)
                or not _recover_pending_reverse_transaction(source_dir)
            ):
                _log_reverse_sync_failure(
                    dest_dir, "REVERSE_SYNC_RECOVERY_FAILED",
                )
                return ReverseSyncStatus.FAILED

            full_exclude = frozenset(exclude)
            assessment = _dest_edit_status(
                dest_dir,
                quiet_seconds=quiet_seconds,
                exclude=full_exclude,
            )
            if assessment.status == _DestEditStatus.FAILED:
                _log_reverse_sync_failure(
                    dest_dir, "REVERSE_SYNC_STATE_FAILED",
                )
                return ReverseSyncStatus.FAILED
            if (
                assessment.status == _DestEditStatus.NO_EDIT
                and not assessment.files
            ):
                return ReverseSyncStatus.NO_EDIT
            try:
                baseline_fingerprints = read_copy_install_baseline(
                    dest_dir,
                )
            except Exception:  # pylint: disable=broad-exception-caught
                _log_reverse_sync_failure(
                    dest_dir, "REVERSE_SYNC_BASELINE_FAILED",
                )
                return ReverseSyncStatus.FAILED
            candidate_files: list[tuple[_SafeDestFile, str]] = []
            for file_info in assessment.files:
                dest_hash = _hash_verified_file(
                    dest_dir, file_info,
                )
                if dest_hash != baseline_fingerprints.get(
                    file_info.relative_path.as_posix(),
                ):
                    candidate_files.append((file_info, dest_hash))
            if not candidate_files:
                return ReverseSyncStatus.NO_EDIT
            last_change_time = max(
                max(file_info.mtime_ns, file_info.ctime_ns)
                / 1_000_000_000
                for file_info, _ in candidate_files
            )
            if time.time() - last_change_time < quiet_seconds:
                return ReverseSyncStatus.RECENT_EDIT

            changed_files: list[
                tuple[_SafeDestFile, str, str | None]
            ] = []
            for file_info, dest_hash in candidate_files:
                relative_name = file_info.relative_path.as_posix()
                baseline_hash = baseline_fingerprints.get(relative_name)
                source_file_info = _safe_source_file_info(
                    source_dir, file_info.relative_path,
                )
                source_hash = (
                    _hash_verified_file(source_dir, source_file_info)
                    if source_file_info is not None
                    else None
                )
                if dest_hash == baseline_hash:
                    continue
                # 基线缺该路径：视为 dest 单边新增/改动，允许回流（不再把
                # ``baseline is None`` 误判成三方冲突——Windows 孤儿领养曾
                # 因空基线在此处 100% FAILED）。
                if (
                    baseline_hash is not None
                    and source_hash != baseline_hash
                    and source_hash != dest_hash
                ):
                    # 历史孤儿基线可能按 Git blob 的 LF 字节计算，而 Windows
                    # worktree 是 CRLF。只在将要报冲突时补做一次 UTF-8 换行
                    # 规范化比较；二进制仍严格按字节冲突，正常轮询没有额外 IO。
                    normalized_source_hash = (
                        _hash_verified_file(
                            source_dir,
                            source_file_info,
                            normalize_utf8_newlines=True,
                        )
                        if source_file_info is not None
                        else None
                    )
                    if normalized_source_hash != baseline_hash:
                        _log_reverse_sync_failure(
                            dest_dir, "REVERSE_SYNC_CONTENT_CONFLICT",
                        )
                        return ReverseSyncStatus.FAILED
                if source_hash != dest_hash:
                    changed_files.append(
                        (file_info, dest_hash, source_hash),
                    )
            if not changed_files:
                return ReverseSyncStatus.NO_EDIT

            manifest_path, data_dir, manifest = (
                _create_reverse_transaction(source_dir)
            )
            staged_dir = data_dir / "staged"
            for (
                file_info,
                expected_dest_hash,
                expected_source_hash,
            ) in changed_files:
                staged_path = staged_dir / file_info.relative_path
                _ensure_real_directory(
                    staged_dir, file_info.relative_path.parent,
                )
                _copy_verified_file_to_stage(
                    dest_dir,
                    file_info,
                    staged_path,
                )
                _fsync_directory(staged_path.parent)
                staged_signature = _path_signature(staged_path)
                if staged_signature is None:
                    raise OSError("staged file missing")
                staged_file_info = _safe_source_file_info(
                    staged_dir, file_info.relative_path,
                )
                if (
                    staged_file_info is None
                    or _hash_verified_file(
                        staged_dir, staged_file_info,
                    ) != expected_dest_hash
                ):
                    raise OSError("staged content changed")
                current_source_info = _safe_source_file_info(
                    source_dir, file_info.relative_path,
                )
                current_source_hash = (
                    _hash_verified_file(
                        source_dir, current_source_info,
                    )
                    if current_source_info is not None
                    else None
                )
                if current_source_hash != expected_source_hash:
                    raise OSError("source changed during reverse staging")
                source_signature = _path_signature(
                    source_dir / file_info.relative_path,
                )
                manifest["files"].append({
                    "path": file_info.relative_path.as_posix(),
                    "original_signature": (
                        list(source_signature)
                        if source_signature is not None
                        else None
                    ),
                    "staged_signature": list(staged_signature),
                })
            manifest["state"] = "prepared"
            _atomic_write_reverse_manifest(manifest_path, manifest)

            rescan_ok, rescanned_files = _safe_dest_files(
                dest_dir, full_exclude,
            )
            if not rescan_ok:
                _log_reverse_sync_failure(
                    dest_dir, "REVERSE_SYNC_RESCAN_FAILED",
                )
                return ReverseSyncStatus.FAILED
            if rescanned_files != assessment.files:
                return ReverseSyncStatus.RECENT_EDIT

            if not _commit_reverse_transaction(
                source_dir,
                manifest_path,
                data_dir,
                manifest,
            ):
                rollback_ok = _recover_pending_reverse_transaction(
                    source_dir,
                )
                _log_reverse_sync_failure(
                    dest_dir,
                    (
                        "REVERSE_SYNC_COMMIT_FAILED"
                        if rollback_ok
                        else "REVERSE_SYNC_ROLLBACK_FAILED"
                    ),
                )
                return ReverseSyncStatus.FAILED
    except Exception:  # pylint: disable=broad-exception-caught
        _log_reverse_sync_failure(dest_dir, "REVERSE_SYNC_STAGE_FAILED")
        return ReverseSyncStatus.FAILED

    return ReverseSyncStatus.SYNCED


def reverse_sync_openclaw_dest(
    dest_dir: Path, source_dir: Path,
    *, quiet_seconds: int = USER_EDIT_QUIET_SECONDS,
) -> ReverseSyncStatus:
    """已弃用别名，新代码用 ``reverse_sync_copy_dest``。

    保留这个名字给外部调用方（team/client/daemon.py、pipeline/runner.py、
    openclaw.py）平滑迁移。语义与默认参数的 ``reverse_sync_copy_dest`` 等价
    （默认 exclude 已含 openclaw 老位置 meta）。
    """
    return reverse_sync_copy_dest(
        dest_dir, source_dir,
        quiet_seconds=quiet_seconds,
    )
