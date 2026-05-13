"""
install_fallback.py -- 跨平台目录安装的三阶 fallback
=====================================================

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
"""

from __future__ import annotations

import logging
import platform
import shutil
import subprocess
from pathlib import Path
from typing import Literal

logger = logging.getLogger("xskill.install_fallback")

InstallMode = Literal["symlink", "junction", "copy"]


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
    """终极 fallback：完整 copytree。"""
    shutil.copytree(src_dir, dest)


def install_dir(src_dir: Path, dest: Path) -> InstallMode:
    """把 ``src_dir`` 整目录安装到 ``dest``，按 symlink→junction→copy 顺序尝试。

    调用者负责：
    * 保证 ``src_dir`` 存在且是目录（本函数不校验，假设上层已校验）
    * 保证 ``dest`` 当前**不存在**（旧条目必须先删；本函数不会动旧文件）
    * 保证 ``dest.parent`` 已存在（``mkdir -p``）

    返回值是实际走的模式：``"symlink"`` / ``"junction"`` / ``"copy"``。
    上层（``ecosystems.install_to_claude_code``）可以据此决定要不要打
    warning、要不要在 metadata 上标 "live" vs "snapshot"。

    根据 CLAUDE.md "不写 fallback 逻辑，遇到问题 throw error"——这里的
    fallback **不是错误掩盖**，而是**平台能力差异的显式适配**：三阶都失败
    会让最后一阶 ``shutil.copytree`` 自己抛出 OSError，本函数不吞错。
    """
    if _try_symlink(src_dir, dest):
        return "symlink"

    if _try_junction(src_dir, dest):
        logger.info(
            "install_dir: symlink unavailable, used directory junction at %s "
            "(Windows non-DevMode path)",
            dest,
        )
        return "junction"

    # 终极 fallback——任何 OSError 会从 shutil.copytree 抛出去，符合 fail-loud
    _do_copy(src_dir, dest)
    logger.warning(
        "install_dir: fell back to copy at %s — "
        "source updates will NOT propagate live; user edits will NOT round-trip "
        "(re-run install to re-sync)",
        dest,
    )
    return "copy"
