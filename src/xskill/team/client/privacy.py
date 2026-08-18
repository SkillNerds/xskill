"""
team/client/privacy.py -- 客户端本地的轨迹上传排除规则（issue #244）
====================================================================

team 模式下客户端会自动采集本机 coding agent 的轨迹并上传。脱敏与
``ingest.mask_patterns`` 只能改上传的**内容**，无法表达「这条轨迹 / 这个
项目根本不许上传」。本模块提供在**读取正文之前**生效的排除闸门：

- 按项目目录排除：该目录及其子目录下产生的全部轨迹；
- 按轨迹 id 排除：某一条轨迹。

规则只保存在本机（默认 ``~/.xskill/privacy.json``），是「这台机器上用户的
隐私意愿」，与连接哪个 team server 无关；被排除的项目路径、轨迹 id 与原因
都不会发送到服务器。默认没有规则 = 行为与以前完全一致。

匹配语义：
- 项目路径存入时规范化为绝对路径并解析符号链接；匹配时同样规范化后判断
  「等于该目录，或位于其子目录下」；macOS / Windows 上不区分大小写。
- 项目归属来自轨迹旁边的元数据（同名 ``.json`` 的 ``cwd``）。Cursor 与
  Trae 两个来源不写 ``cwd``，按项目排除对它们不生效，只能按轨迹 id 排除；
  调用方应如实提示，而不是静默放行。
"""

from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger("xskill.team.client.privacy")

# 项目路径大小写：macOS（默认 APFS/HFS+）与 Windows 的文件系统不区分大小写。
_CASE_INSENSITIVE_FS = sys.platform in ("darwin", "win32")

# 这些来源的轨迹元数据没有 cwd，按项目排除对它们不生效。
SOURCES_WITHOUT_CWD = frozenset({"cursor", "trae"})


def default_privacy_path(xskill_home: Path | str | None = None) -> Path:
    """规则文件位置：``<XSKILL_HOME>/privacy.json``（全局，跨 server 生效）。"""
    if xskill_home is None:
        from xskill.config import XSKILL_HOME
        xskill_home = XSKILL_HOME
    return Path(xskill_home) / "privacy.json"


def canonical_project_path(path: Path | str) -> str:
    """规范化为**可展示**的绝对路径：展开 ``~``、转绝对路径、解析符号链接，
    保留原始大小写（回显给用户）。"""
    p = Path(os.path.expanduser(str(path)))
    try:
        p = p.resolve()
    except (OSError, RuntimeError):
        p = p.absolute()
    return str(p)


def normalize_project_path(path: Path | str) -> str:
    """规范化为**比较用**的键：在 ``canonical_project_path`` 之上，不区分
    大小写的平台再统一小写。存入与匹配都走这一函数，保证同一目录的不同
    写法相等。展示请用 ``canonical_project_path``。"""
    s = canonical_project_path(path)
    return s.lower() if _CASE_INSENSITIVE_FS else s


def _is_within(child: str, parent: str) -> bool:
    """``child`` 是否等于 ``parent`` 或位于其子目录下（两者均已规范化）。"""
    if child == parent:
        return True
    sep = os.sep
    return child.startswith(parent.rstrip(sep) + sep)


@dataclass
class PrivacyPolicy:
    """本机的排除规则集合。``projects`` / ``trajectories`` 的值是添加时间
    （ISO 8601，UTC），供 ``list`` 展示；键分别是规范化后的项目路径与轨迹 id。"""

    projects: dict[str, str] = field(default_factory=dict)
    trajectories: dict[str, str] = field(default_factory=dict)
    # 比较键 -> 用户输入解析后的可展示路径（保留大小写）。缺失时展示比较键。
    project_display: dict[str, str] = field(default_factory=dict)

    def display_project(self, key: str) -> str:
        return self.project_display.get(key, key)

    # ── 查询 ────────────────────────────────────────────────────

    def is_denied(self, trajectory_id: str, cwd: Optional[str]) -> bool:
        """轨迹是否被排除。``cwd`` 为 None（来源不记录工作目录）时只按 id 判。"""
        if trajectory_id in self.trajectories:
            return True
        if not cwd:
            return False
        norm = normalize_project_path(cwd)
        return any(_is_within(norm, proj) for proj in self.projects)

    def denied_by(self, trajectory_id: str, cwd: Optional[str]) -> Optional[str]:
        """返回命中的规则描述（``trajectory:<id>`` / ``project:<path>``），
        未命中返回 None。供日志与 ``list`` 的统计使用。"""
        if trajectory_id in self.trajectories:
            return f"trajectory:{trajectory_id}"
        if cwd:
            norm = normalize_project_path(cwd)
            for proj in self.projects:
                if _is_within(norm, proj):
                    return f"project:{proj}"
        return None

    @property
    def is_empty(self) -> bool:
        return not self.projects and not self.trajectories

    # ── 修改（返回 True 表示状态有变化） ─────────────────────────

    def deny_project(self, path: Path | str) -> tuple[bool, str]:
        norm = normalize_project_path(path)
        self.project_display.setdefault(norm, canonical_project_path(path))
        if norm in self.projects:
            return False, norm
        self.projects[norm] = _now_iso()
        return True, norm

    def allow_project(self, path: Path | str) -> tuple[bool, str]:
        norm = normalize_project_path(path)
        removed = self.projects.pop(norm, None) is not None
        if removed:
            self.project_display.pop(norm, None)
        return removed, norm

    def deny_trajectory(self, trajectory_id: str) -> bool:
        tid = trajectory_id.strip()
        if tid in self.trajectories:
            return False
        self.trajectories[tid] = _now_iso()
        return True

    def allow_trajectory(self, trajectory_id: str) -> bool:
        return self.trajectories.pop(trajectory_id.strip(), None) is not None

    # ── 持久化 ──────────────────────────────────────────────────

    def to_dict(self) -> dict:
        return {"version": 1, "projects": dict(self.projects),
                "trajectories": dict(self.trajectories),
                "project_display": dict(self.project_display)}

    @classmethod
    def from_dict(cls, data: dict) -> PrivacyPolicy:
        projects = data.get("projects") or {}
        trajectories = data.get("trajectories") or {}
        display = data.get("project_display") or {}
        if (not isinstance(projects, dict) or not isinstance(trajectories, dict)
                or not isinstance(display, dict)):
            raise ValueError("privacy.json: projects / trajectories must be objects")
        return cls(projects={str(k): str(v) for k, v in projects.items()},
                   trajectories={str(k): str(v) for k, v in trajectories.items()},
                   project_display={str(k): str(v) for k, v in display.items()})


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def load_policy(path: Path | str | None = None) -> PrivacyPolicy:
    """读取规则文件；不存在返回空策略（默认允许上传）。文件损坏时**告警并
    视为空策略**是不可接受的——那会让用户以为受保护的项目其实在上传——因此
    损坏时抛错，让调用方（CLI / 采集器）明确失败。"""
    p = Path(path) if path else default_privacy_path()
    if not p.is_file():
        return PrivacyPolicy()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read privacy rules {p}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"privacy rules {p}: top-level must be an object")
    return PrivacyPolicy.from_dict(data)


def save_policy(policy: PrivacyPolicy, path: Path | str | None = None) -> Path:
    """原子写入（先写临时文件再替换），避免采集器读到半截文件。"""
    p = Path(path) if path else default_privacy_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(policy.to_dict(), indent=2, ensure_ascii=False) + "\n",
                   encoding="utf-8")
    os.replace(tmp, p)
    return p


def read_trajectory_cwd(md_path: Path) -> Optional[str]:
    """从轨迹旁边的元数据（同名 ``.json``）读 ``cwd``。文件缺失、损坏或无
    该字段返回 None。这一步只读一个小文件，不读轨迹正文。"""
    jp = md_path.with_suffix(".json")
    if not jp.is_file():
        return None
    try:
        data = json.loads(jp.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    cwd = data.get("cwd") if isinstance(data, dict) else None
    return str(cwd) if cwd else None


def read_trajectory_source(md_path: Path) -> str:
    """从元数据读来源（``source`` 字段，如 ``claude_code_jsonl``）；缺失时按
    bridge 目录名推断。用于提示「该来源不记录工作目录」。"""
    jp = md_path.with_suffix(".json")
    if jp.is_file():
        try:
            data = json.loads(jp.read_text(encoding="utf-8"))
            src = data.get("source") if isinstance(data, dict) else None
            if src:
                return str(src)
        except (OSError, json.JSONDecodeError):
            pass
    return md_path.parent.name.replace("_sessions", "")


def source_lacks_cwd(source: str) -> bool:
    """来源是否属于不记录工作目录的那一类（Cursor / Trae）。"""
    s = source.lower()
    return any(s.startswith(k) for k in SOURCES_WITHOUT_CWD)


__all__ = [
    "SOURCES_WITHOUT_CWD",
    "PrivacyPolicy",
    "canonical_project_path",
    "default_privacy_path",
    "load_policy",
    "normalize_project_path",
    "read_trajectory_cwd",
    "read_trajectory_source",
    "save_policy",
    "source_lacks_cwd",
]
