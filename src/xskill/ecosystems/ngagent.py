"""
ecosystems/ngagent.py -- ngagent 生态适配（opencode 企业分支版本）
===================================================================

ngagent 是企业用户使用的 ``opencode`` fork，**schema 与 opencode 完全一致**
（同样的 ``session`` / ``message`` / ``part`` 表结构、同样的 drizzle ORM、
同样的 JSON-in-text ``message.data`` / ``part.data``），所以 SQLite 摄取层
（``SqliteIngester``）直接复用 ``opencode.py`` 的实现，本模块只描述路径差异
与安装目标差异。

与 opencode 的路径差异（用户机器上可能同时装两者，并存）：

================  ===========================================  ===================================
                  opencode                                      ngagent
================  ===========================================  ===================================
DB 文件           ``~/.local/share/opencode/opencode.db``       ``~/.local/share/opencode/db/ngagent.db``
Skill 安装目录    ``~/.agents/skills/<name>/``                  ``~/.config/opencode/skills/<name>/``
traj_id 前缀      ``traj_oc_``                                  ``traj_ng_``
================  ===========================================  ===================================

设计要点：

1. **复用 SqliteIngester 而非另写**：ingester 主体逻辑（cursor / immutable=1
   只读连接 / message+part 渲染）spec-driven，按 ``spec.path_resolver`` /
   ``spec.traj_id_prefix`` 派生。本模块只导出 ``NGAGENT_SPEC``。
2. **不写到 ``~/.agents/skills/``**：ngagent 企业分支 skill discovery 走
   ``$XDG_CONFIG_HOME/opencode/skills``（不读 ``~/.agents/skills``）；
   写错路径 → ngagent 看不到。
3. **与 opencode 并存**：``detect_known_ecosystems`` 同时检查两个 DB 文件，
   两者独立 detect / 独立装 skill / 独立 ingest（同样的 skill 会被装两份：
   一份到 ``~/.agents/skills``、一份到 ``~/.config/opencode/skills``）。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Iterable

from xskill.ecosystems._fallback import install_dir
from xskill.ecosystems._shared import (
    SqliteEcosystemSpec,
    _install_all_with,
    _source_md_for_side,
)

logger = logging.getLogger("xskill.ecosystems")


# ─────────────────────────────────────────────────────────────────
# Path helpers
# ─────────────────────────────────────────────────────────────────


def _ngagent_db_path(home: Path) -> Path:
    """ngagent DB 路径：``<home>/.local/share/opencode/db/ngagent.db``。

    注意是 ``db/`` 子目录下的独立文件，不是 opencode 默认的
    ``opencode.db``——这样 ngagent 与 opencode 可以并存于同一个用户家目录。
    """
    return home / ".local" / "share" / "opencode" / "db" / "ngagent.db"


def _ngagent_skills_path(home: Path) -> Path:
    """ngagent skill 安装目录：``<home>/.config/opencode/skills``。

    XDG_CONFIG_HOME 默认值（``$HOME/.config``）下的 ``opencode/skills``——
    ngagent 企业分支 discover skill 只扫此路径，不读 opencode 的
    ``~/.agents/skills``。两者完全隔离。
    """
    return home / ".config" / "opencode" / "skills"


# ─────────────────────────────────────────────────────────────────
# Ecosystem spec
# ─────────────────────────────────────────────────────────────────

NGAGENT_SPEC = SqliteEcosystemSpec(
    name="ngagent",
    source_kind="sqlite",
    path_resolver=_ngagent_db_path,
    cursor_strategy="sqlite_time_updated",
    label="ngagent",
    traj_id_prefix="traj_ng_",
)


# ─────────────────────────────────────────────────────────────────
# Installer: install_to_ngagent (writes to ~/.config/opencode/skills/)
# ─────────────────────────────────────────────────────────────────


def install_to_ngagent(
    skill_path: Path | str,
    target_root: Path | str | None = None,
    side: str = "main",
) -> Path:
    """把一个 skill 装到 ``<target_root>/.config/opencode/skills/<name>``。

    与 ``install_to_opencode`` 的区别：

    * **写到 ngagent 专属目录**：``~/.config/opencode/skills/<name>/``——
      不与 opencode/codex 共享 ``~/.agents/skills/``。
    * **强制 copy 模式**：ngagent 在 Windows non-DevMode 下走 directory
      junction → Node.js ``Dirent.isDirectory()`` 对 junction 返回 False
      → ngagent skill discovery 看不到（issue #34）。所以**所有平台**
      统一用 copy 模式，配套 ``reverse_sync_copy_dest`` 回流让用户改 dest
      时仍能往 source 仓灌回——和 openclaw 同一套机制。
    * **same source switch (main / staging)**：同样支持 ``side`` 参数，
      ``staging`` 链到 ``<skill_path>/../.canary/<name>/``。

    返回 dest 下的 SKILL.md 路径（约定，与 OpenCode 版一致）。
    """
    skill_path = Path(skill_path).resolve()
    if not skill_path.is_dir():
        raise NotADirectoryError(f"skill_path is not a directory: {skill_path}")

    # 校验 source 齐备（main: SKILL.md 必须有；staging: .canary/<name>/SKILL.md 必须有）
    _source_md_for_side(skill_path, side)

    if side == "main":
        src_dir = skill_path
    elif side == "staging":
        src_dir = (skill_path.parent / ".canary" / skill_path.name).resolve()
    else:
        raise ValueError(f"side must be 'main' or 'staging', got {side!r}")

    name = skill_path.name
    root = Path(target_root) if target_root else Path.home()
    skills_root = _ngagent_skills_path(root)
    skills_root.mkdir(parents=True, exist_ok=True)
    dest = skills_root / name

    # 强制 copy + 一站式完成 reverse_sync→reset→install→meta。
    # auto_reset=True 让 install_dir 内部处理"覆盖旧 dest"（含 reverse_sync
    # 保护：上一轮若是 copy 且 dest 有 pending user edit，先灌回 source 再覆盖）。
    install_dir(src_dir, dest, force_mode="copy", auto_reset=True)
    logger.info(
        "install_to_ngagent(%s): installed (copy + reverse_sync) at %s",
        name, dest,
    )
    return dest / "SKILL.md"


def install_all_to_ngagent(
    skill_dir: Path | str,
    target_root: Path | str | None = None,
    names: Iterable[str] | None = None,
) -> list[Path]:
    """Install every skill under ``skill_dir`` (each subdir = one skill) to
    ngagent's discovery root (``<target_root>/.config/opencode/skills``). If
    ``names`` is given, restrict to those.

    与 ``install_all_to_opencode`` 互不干扰——前者写 ``~/.agents/skills``，
    本函数写 ``~/.config/opencode/skills``。两者都可以对同一个 skill_dir
    跑，分别给 opencode / ngagent 装一份。
    """
    return _install_all_with(install_to_ngagent, skill_dir, target_root, names)
