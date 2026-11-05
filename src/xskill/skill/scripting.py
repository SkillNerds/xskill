"""详情页「脚本化（实验性）」请求标记。

看板进程写 ``.scripting_requested``，agent-worker 子进程扫到后丢进编辑池。
标记 gitignore，不进版本库。
"""
from __future__ import annotations

from xskill.skill.write_policy import require_skill_write

import logging
from pathlib import Path

from xskill.canary import has_staging
from xskill.skill.git import current_branch, run_git

logger = logging.getLogger("xskill.skill.scripting")

MARKER_NAME = ".scripting_requested"


def marker_path(skill_path: Path) -> Path:
    return Path(skill_path) / MARKER_NAME


def has_scripting_request(skill_path: Path) -> bool:
    return marker_path(skill_path).is_file()


def scripting_gate_reason(skill_path: Path) -> str:
    """不能跑脚本化的原因（不含「已有请求标记」）。空字符串表示可以跑。"""
    skill_path = Path(skill_path)
    if not (skill_path / ".git").is_dir():
        return "不是自产技能仓"
    code, _, _ = run_git(["rev-parse", "--verify", "main"], cwd=str(skill_path))
    branch = current_branch(str(skill_path)) or ""
    if code != 0 or branch == "baby":
        return "还在预备分支，等升到主干后再脚本化"
    if has_staging(skill_path):
        return "正在灰度对比，这一期不和灰度抢同一份主干"
    return ""


def scripting_status(skill_path: Path) -> dict:
    """给详情页按钮用：能否点、不能点的原因。"""
    skill_path = Path(skill_path)
    requested = has_scripting_request(skill_path)
    gate = scripting_gate_reason(skill_path)
    if gate:
        return {"enabled": False, "reason": gate, "requested": requested}
    if requested:
        return {
            "enabled": False,
            "reason": "脚本化已在排队或进行中",
            "requested": True,
        }
    return {"enabled": True, "reason": "", "requested": False}


def request_scripting(skill_path: Path) -> dict:
    """写入请求标记。条件不满足时抛 ValueError。"""
    require_skill_write(marker_path(skill_path))
    status = scripting_status(skill_path)
    if not status["enabled"]:
        raise ValueError(status["reason"] or "scripting not available")
    marker_path(skill_path).write_text("requested\n", encoding="utf-8")
    logger.info("scripting requested: %s", Path(skill_path).name)
    return {"ok": True, "requested": True}


def clear_scripting_request(skill_path: Path) -> None:
    require_skill_write(marker_path(skill_path))
    path = marker_path(skill_path)
    try:
        path.unlink()
    except FileNotFoundError:
        return
