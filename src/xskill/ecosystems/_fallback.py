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

import json
import logging
import os
import platform
import shutil
import subprocess
import time
from pathlib import Path
from typing import Literal

logger = logging.getLogger("xskill.install_fallback")

InstallMode = Literal["symlink", "junction", "copy"]

# install-meta 文件名前缀；完整名为 ``.xskill-install-meta-<dest.name>.json``。
# 写到 dest.parent（而非 dest 内部）——link/junction dest 内部就是 source 仓，
# 写进去会污染源；copy 模式 dest 内部也排除掉这个名字避免反向灌回 source。
_INSTALL_META_PREFIX = ".xskill-install-meta-"


def _install_meta_path(dest: Path) -> Path:
    """meta 文件路径：``dest.parent / .xskill-install-meta-<dest.name>.json``。

    统一所有模式都写到 dest.parent——避免 link vs copy 的分支判断；下游
    清理 dest 时也不需要单独处理 meta（meta 不在 dest 内部）。
    """
    return dest.parent / f"{_INSTALL_META_PREFIX}{dest.name}.json"


def _write_install_meta(dest: Path, source: Path, mode: InstallMode) -> None:
    """install_dir 落地成功后写一份 meta。失败仅 warn（meta 缺失不影响主流程）。"""
    meta = {
        "mode": mode,
        "source": str(source.resolve()),
        "installed_at": time.time(),
    }
    try:
        _install_meta_path(dest).write_text(
            json.dumps(meta, indent=2), encoding="utf-8",
        )
    except OSError as e:
        logger.warning("failed to write install-meta for %s: %s", dest, e)


def _is_link_or_junction(p: Path) -> bool:
    """是否为 symlink 或 Windows directory junction。

    ``Path.is_symlink()`` 在 Windows 上对 junction 返回 False（pathlib 已知
    行为）——junction 的 reparse tag 是 ``IO_REPARSE_TAG_MOUNT_POINT``，
    不是 symlink 标签。但 ``shutil.rmtree`` 内部依赖 ``is_symlink()`` 判定
    会误把 junction 当真目录处理而抛 OSError（issue #35）。

    这里把 symlink 与 junction 统一当"link"处理：清理 dest 用 ``unlink``
    而不是 ``rmtree``——``unlink`` 对 junction 也工作（删除 reparse point
    本身，不递归动 target 目录）。
    """
    try:
        if p.is_symlink():
            return True
    except OSError:
        return False
    if os.name == "nt":
        try:
            import stat
            return bool(p.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)
        except (OSError, AttributeError):
            return False
    return False


def _try_symlink(src_dir: Path, dest: Path) -> bool:
    """尝试建目录 symlink。成功 True，失败（OSError/NotImplementedError）False。

    分开抽出来是为了测试可以 monkeypatch 这一层。
    """
    try:
        dest.symlink_to(src_dir, target_is_directory=True)
        return True
    except (OSError, NotImplementedError) as e:
        logger.debug("symlink failed for %s -> %s: %s", dest, src_dir, e)
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
        )
        return True
    except (subprocess.CalledProcessError, FileNotFoundError, OSError) as e:
        logger.debug("junction failed for %s -> %s: %s", dest, src_dir, e)
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
        ),
    )


def _maybe_reverse_sync_before_overwrite(dest: Path, source: Path) -> None:
    """install_dir 准备覆盖 dest 前的回流保护。

    读 meta：如果上轮是 copy 模式且 dest 有用户改没回流到 source →
    先调 ``reverse_sync_copy_dest`` 灌回，再覆盖。link/junction 模式
    dest = source 自然同步，无需回流。

    meta 缺失（首次 install 或老版本写的 dest）、读取失败、mode 不是 copy、
    dest 不存在 → 全是 no-op。一切异常仅 warn，不阻塞主流程：reverse_sync
    本身是"尽力而为"的用户友好特性，不应因为它失败导致 install 链路阻塞。
    """
    meta_path = _install_meta_path(dest)
    if not meta_path.is_file():
        return
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if meta.get("mode") != "copy":
        return
    if not dest.exists() or _is_link_or_junction(dest) or not dest.is_dir():
        return
    # 延迟 import 避免 _fallback ↔ user_edit_absorb_agent 循环
    try:
        from xskill.agents.user_edit_absorb_agent import reverse_sync_copy_dest
        reverse_sync_copy_dest(dest, source)
    except Exception:
        logger.warning(
            "reverse_sync before overwrite failed for %s; proceeding",
            dest, exc_info=True,
        )


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
        except OSError as e:
            logger.warning("failed to unlink %s: %s", dest, e)
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
    """
    if auto_reset:
        _maybe_reverse_sync_before_overwrite(dest, src_dir)
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
