"""
ecosystems/_fallback.py -- 跨平台目录安装的三阶 fallback + install-meta + dest 回流钩子
================================================================================

xskill 把一个 skill 装到外部 agent 的 discovery 目录时，**首选 symlink**：

  ~/.claude/skills/<name>  →  ~/.xskill/skill/<name>/

symlink 是 0-copy 的 view，源仓更新（SkillEditAgent 写 SKILL.md）外部 agent
立刻看见，用户在 ``~/.claude/skills/<name>/`` 直接改文件实际改的是源仓——
UserEditAbsorbAgent 才能 round-trip 收编。但 symlink 不是所有 OS 都默认能
建：

* **Linux / macOS** — 用户态默认就能建目录 symlink，几乎不会失败。
* **Windows** — 默认账户**没有** ``SeCreateSymbolicLinkPrivilege``。要么
  开 Developer Mode，要么是 admin shell，否则 ``Path.symlink_to`` 抛
  ``OSError(WinError 1314)``。

第二阶 fallback 用 **directory junction**：``mklink /J``。junction 在 Win
不需要任何特权，是 NTFS reparse point，对绝大多数读取端表现等同 symlink
（Claude Code / Codex / OpenCode 扫目录都能跟过去）。但 junction **只对
目录有效**，且只在本卷内有用——跨盘符建会失败。

第三阶 fallback 是 **shutil.copytree**：把源整目录复制过去。代价是

1. xskill 之后 ``SkillEditAgent`` 写新版到源仓，外部 agent 看到的还是旧副本
   ——必须等下一次 ``install`` 重新 copy 才同步。
2. ``UserEditAbsorbAgent`` 完全失效——用户改的是副本，源仓 mtime 不会动。

所以 copy 模式下我们打 warning，告诉用户"你装的是快照、不是 live mount"。

三阶 fallback 在三平台的预期行为：

  Linux  : 永远走 symlink
  macOS  : 永远走 symlink
  Windows: Dev Mode 开 → symlink ；
           关 → junction （同卷）；
           跨盘 / FAT32 → copy

# install-meta + dest→source 回流

每次 ``install_dir`` 成功落地后会在 ``dest.parent`` 旁边写一份
``.xskill-install-meta-<dest.name>.json``，记 ``{mode, source, installed_at}``。

  * **link/junction 模式**：dest 是个 link → meta 不能写进 dest 内部
    （那相当于污染 source 仓）。
  * **copy 模式**：理论上可以写 dest 内部，但为统一约定与读取路径
    **永远写 dest.parent**——也免去判断 copy/link 分支的复杂度。

下次重装时 ``_maybe_reverse_sync_before_overwrite`` 先读 meta：
若上轮是 copy 且 dest 里有用户改没回流（``has_pending_dest_edit``）→
调 ``reverse_sync_copy_dest`` 把改灌回 source，再覆盖 dest。这把 openclaw
单独实现的"copy + reverse_sync"模式**通用化**给所有走到 copy fallback 的
生态（issue #34 的 ngagent / openclaw 等）。

# Windows junction 兼容辅助

``Path.is_symlink()`` 在 Windows 上对 directory junction **返回 False**
（pathlib 已知行为：junction 是 reparse point 但不是 SYMLINK 标签）。
直接信 ``is_symlink()`` 会把 junction 当真目录走 ``shutil.rmtree``
触发 ``OSError: Cannot call rmtree on a symbolic link``（issue #35）。
``_is_link_or_junction`` 显式查 ``FILE_ATTRIBUTE_REPARSE_POINT`` 位，
把 symlink 与 junction 统一当 link 处理。
"""

from __future__ import annotations

import logging
import platform
import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from xskill.ecosystems.installation import (
    COPY_INSTALL_MARKER_NAME,
    InstallSafetyError,
    InstallMode,
    copy_install_is_current,
    install_metadata_path,
    is_link_or_junction,
    read_install_metadata,
    read_skill_head_sha,
    write_install_metadata,
)

logger = logging.getLogger("xskill.install_fallback")

if TYPE_CHECKING:
    from xskill.agents.user_edit_absorb_agent import ReverseSyncStatus

# 兼容历史内部调用；真实实现只存在于公开 installation 模块。
_install_meta_path = install_metadata_path
_read_skill_head_sha = read_skill_head_sha
_write_install_meta = write_install_metadata
_is_link_or_junction = is_link_or_junction
_copy_install_is_current = copy_install_is_current


def _try_symlink(src_dir: Path, dest: Path) -> bool:
    """尝试建目录 symlink。成功 True，失败（OSError/NotImplementedError）False。

    分开抽出来是为了测试可以 monkeypatch 这一层。
    """
    try:
        dest.symlink_to(src_dir, target_is_directory=True)
        return True
    except (OSError, NotImplementedError) as link_error:
        logger.debug(
            "symlink failed for %s -> %s: %s",
            dest, src_dir, link_error,
        )
        return False


def _try_junction(src_dir: Path, dest: Path) -> bool:
    """Windows-only：尝试用 ``cmd /c mklink /J`` 建 directory junction。

    其他平台直接返回 False（不该走这条路）。
    """
    if platform.system() != "Windows":
        return False
    try:
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(dest), str(src_dir)],
            check=True,
            capture_output=True,
            # CREATE_NO_WINDOW：无 console 的父进程下不弹 cmd 黑窗
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        return True
    except (
        subprocess.CalledProcessError, FileNotFoundError, OSError,
    ) as junction_error:
        logger.debug(
            "junction failed for %s -> %s: %s",
            dest, src_dir, junction_error,
        )
        return False


def _do_copy(src_dir: Path, dest: Path) -> None:
    """终极 fallback：完整 copytree。

    显式排除 .git 与 .xskill-install-meta* —— 前者是源仓 git 元数据
    （不应拷到 dest）；后者是上一轮 install 写进 dest 旁边的 meta 文件，
    如果上层把 source 拼成 ``dest.parent`` 这种特殊用法（不常见但理论可能），
    避免 meta 跨级污染。
    """
    shutil.copytree(
        src_dir, dest,
        ignore=shutil.ignore_patterns(
            ".git", ".git/*", ".xskill-install-meta*",
            COPY_INSTALL_MARKER_NAME,
        ),
    )


def _maybe_reverse_sync_before_overwrite(
    dest: Path, source: Path,
) -> ReverseSyncStatus:
    """install_dir 准备覆盖 dest 前的回流保护。

    读 meta：如果上轮是 copy 模式且 dest 有用户改没回流到 source →
    先调 ``reverse_sync_copy_dest`` 灌回，再覆盖。link/junction 模式
    dest = source 自然同步，无需回流。

    dest 不存在或是 link/junction 时返回 ``NO_EDIT``。真实目录必须能判定为
    ``NO_EDIT`` 或成功 ``SYNCED`` 才允许后续覆盖；仍在编辑、元数据未知或回流
    失败都抛安全异常，保留原 dest。
    """
    from xskill.agents.user_edit_absorb_agent import ReverseSyncStatus

    if not dest.exists() or _is_link_or_junction(dest) or not dest.is_dir():
        return ReverseSyncStatus.NO_EDIT
    meta = read_install_metadata(dest)
    if meta is not None and meta.get("mode") != "copy":
        raise InstallSafetyError("REVERSE_SYNC_STATE_FAILED") from None
    # 延迟 import 避免 _fallback ↔ user_edit_absorb_agent 循环
    from xskill.agents.user_edit_absorb_agent import reverse_sync_copy_dest
    reverse_status = reverse_sync_copy_dest(dest, source)
    if reverse_status == ReverseSyncStatus.RECENT_EDIT:
        raise InstallSafetyError("REVERSE_SYNC_RECENT_EDIT") from None
    if reverse_status == ReverseSyncStatus.FAILED:
        raise InstallSafetyError("REVERSE_SYNC_FAILED") from None
    return reverse_status


def _reset_dest(dest: Path) -> None:
    """清理 dest 旧条目（symlink / junction / 真目录 / 文件都干掉）。

    用 ``_is_link_or_junction`` 而非 ``is_symlink`` —— 后者在 Windows 对
    junction 返回 False，会让 ``shutil.rmtree`` 误把 junction 当真目录走，
    撞 issue #35 的 ``OSError: Cannot call rmtree on a symbolic link``。

    顺手把 dest 旁边的旧 meta 文件也删掉，避免 install 失败后留下指向
    错误 source 的 meta。
    """
    is_link = _is_link_or_junction(dest)
    # is_link 的 dest 可能 ``exists()`` 返回 False（断链 / 指向已删 source），
    # 但 lstat 仍能拿到 reparse point 属性。所以两个条件都要看。
    if not dest.exists() and not is_link:
        return
    if is_link or dest.is_file():
        try:
            dest.unlink()
        except OSError as unlink_error:
            logger.warning(
                "failed to unlink %s: %s", dest, unlink_error,
            )
    elif dest.is_dir():
        shutil.rmtree(dest)
    # 删旧 meta（即使 dest 已经清理，遗留 meta 也不该留）
    meta_path = _install_meta_path(dest)
    if meta_path.is_file():
        try:
            meta_path.unlink()
        except OSError:
            pass


def install_dir(
    src_dir: Path, dest: Path, *,
    force_mode: InstallMode | None = None,
    auto_reset: bool = False,
    preflight_reverse_sync_status: ReverseSyncStatus | None = None,
) -> InstallMode:
    """把 ``src_dir`` 整目录安装到 ``dest``，按 symlink→junction→copy 顺序尝试。

    调用者负责：
    * 保证 ``src_dir`` 存在且是目录（本函数不校验，假设上层已校验）
    * 保证 ``dest.parent`` 已存在（``mkdir -p``）
    * 若 ``auto_reset=False``（默认，向后兼容）：保证 ``dest`` 当前
      **不存在**（旧条目必须先删；本函数不会动旧文件）。
    * 若 ``auto_reset=True``：本函数会先调 ``_maybe_reverse_sync_before_overwrite``
      读 install-meta 判断要不要回流，再 ``_reset_dest`` 清掉旧 link/dir/file，
      再装新的——一站式完成 reverse_sync + reset + install。新代码推荐用
      ``auto_reset=True``，旧调用方（``_install_skill_into``、``install_to_openclaw``）
      迁移完之后这个开关将变成默认 True。

    返回值是实际走的模式：``"symlink"`` / ``"junction"`` / ``"copy"``。
    上层（``ecosystems.install_to_claude_code``）可以据此决定要不要打
    warning、要不要在 metadata 上标 "live" vs "snapshot"。

    根据 CLAUDE.md "不写 fallback 逻辑，遇到问题 throw error"——这里的
    fallback **不是错误掩盖**，而是**平台能力差异的显式适配**：三阶都失败
    会让最后一阶 ``shutil.copytree`` 自己抛出 OSError，本函数不吞错。

    Args:
        src_dir: 源目录（skill working copy）
        dest: 安装目标路径
        force_mode: 可选——强制走指定模式而不试三阶 fallback。
            ``"copy"`` 用于 ngagent / openclaw 等已知 link/junction 不工作
            的生态（issue #34）；``"symlink"`` / ``"junction"`` 也支持
            但目前没人用。强制模式失败会把底层异常抛上去。
        auto_reset: 是否自动处理"覆盖旧 dest"。打开后函数会先 reverse_sync
            （若上轮是 copy 且有 pending edit）再 reset_dest，安全覆盖。
        preflight_reverse_sync_status: 调用方已执行同一目标回流检查时传入其
            显式结果，避免在破坏性覆盖前重复检查产生竞态。
    """
    if auto_reset:
        from xskill.agents.user_edit_absorb_agent import ReverseSyncStatus
        reverse_status = preflight_reverse_sync_status
        if reverse_status is None:
            reverse_status = _maybe_reverse_sync_before_overwrite(
                dest, src_dir,
            )
        if reverse_status == ReverseSyncStatus.RECENT_EDIT:
            raise InstallSafetyError("REVERSE_SYNC_RECENT_EDIT") from None
        if reverse_status == ReverseSyncStatus.FAILED:
            raise InstallSafetyError("REVERSE_SYNC_FAILED") from None
        if reverse_status not in {
            ReverseSyncStatus.NO_EDIT,
            ReverseSyncStatus.SYNCED,
        }:
            raise InstallSafetyError("REVERSE_SYNC_STATE_FAILED") from None
        _reset_dest(dest)

    if force_mode == "copy":
        _do_copy(src_dir, dest)
        _write_install_meta(dest, src_dir, "copy")
        return "copy"
    if force_mode == "symlink":
        if not _try_symlink(src_dir, dest):
            raise OSError(f"forced symlink install failed: {dest}")
        _write_install_meta(dest, src_dir, "symlink")
        return "symlink"
    if force_mode == "junction":
        if not _try_junction(src_dir, dest):
            raise OSError(f"forced junction install failed: {dest}")
        _write_install_meta(dest, src_dir, "junction")
        return "junction"

    if _try_symlink(src_dir, dest):
        _write_install_meta(dest, src_dir, "symlink")
        return "symlink"

    if _try_junction(src_dir, dest):
        logger.info(
            "install_dir: symlink unavailable, used directory junction at %s "
            "(Windows non-DevMode path)",
            dest,
        )
        _write_install_meta(dest, src_dir, "junction")
        return "junction"

    # 终极 fallback——任何 OSError 会从 shutil.copytree 抛出去，符合 fail-loud
    _do_copy(src_dir, dest)
    logger.warning(
        "install_dir: fell back to copy at %s — "
        "source updates will NOT propagate live; user edits will NOT round-trip "
        "(re-run install to re-sync)",
        dest,
    )
    _write_install_meta(dest, src_dir, "copy")
    return "copy"
