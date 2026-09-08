"""team/client/privacy.py — 本机轨迹上传规则（issue #244）

按项目目录记 allow / deny；没写规则的项目按生效模式处理。生效模式取 server
下发的模式与本机设置中更严的那个（allowlist 严于 denylist）。规则只存本机
``~/.xskill/privacy.json``，不发送服务器；采集器在读正文之前判定。
"""
from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

MODE_ALLOWLIST = "allowlist"
MODE_DENYLIST = "denylist"
MODE_AUTO = "auto"
SERVER_MODES = (MODE_ALLOWLIST, MODE_DENYLIST)
LOCAL_MODES = (MODE_ALLOWLIST, MODE_DENYLIST, MODE_AUTO)
RULE_ALLOW = "allow"
RULE_DENY = "deny"
ACTION_UPLOAD = "upload"
ACTION_SKIP = "skip"

# 这两个 harness 的轨迹 sidecar 不记录工作目录，无法归属到项目。
HARNESSES_WITHOUT_CWD = frozenset({"cursor", "trae"})

_CASE_INSENSITIVE_FS = sys.platform in ("darwin", "win32")


def effective_mode(server_mode: Optional[str], local_mode: str) -> str:
    """两边只要有一方是 allowlist 就是 allowlist；server 未下发按 denylist。"""
    if MODE_ALLOWLIST in (server_mode, local_mode):
        return MODE_ALLOWLIST
    return MODE_DENYLIST


def mode_origin(server_mode: Optional[str], local_mode: str) -> str:
    """生效模式来自哪里：server_forced / local / server_default / server_missing。"""
    if server_mode == MODE_ALLOWLIST:
        return "server_forced"
    if local_mode != MODE_AUTO:
        return "local"
    return "server_default" if server_mode in SERVER_MODES else "server_missing"


def default_privacy_path(xskill_home: Path | str | None = None) -> Path:
    if xskill_home is None:
        from xskill.config import XSKILL_HOME
        xskill_home = XSKILL_HOME
    return Path(xskill_home) / "privacy.json"


def canonical_project_path(path: Path | str) -> str:
    """展示用绝对路径：展开 ``~``、解析符号链接，保留原始大小写。"""
    expanded = Path(os.path.expanduser(str(path)))
    try:
        expanded = expanded.resolve()
    except (OSError, RuntimeError):
        expanded = expanded.absolute()
    return str(expanded)


def normalize_project_path(path: Path | str) -> str:
    """比较用键：在展示路径之上，大小写不敏感的平台统一小写。"""
    canonical = canonical_project_path(path)
    return canonical.lower() if _CASE_INSENSITIVE_FS else canonical


def path_is_within(child: str, parent: str) -> bool:
    """``child`` 等于 ``parent`` 或位于其子目录下（两者均已规范化）。"""
    if child == parent:
        return True
    return child.startswith(parent.rstrip(os.sep) + os.sep)


@dataclass
class ProjectRule:
    rule: str            # allow | deny
    display: str         # 保留大小写的展示路径
    added_at: str        # ISO 8601 UTC


@dataclass
class Decision:
    action: str                    # upload | skip
    reason: str                    # allow | deny | default | no_cwd | broken_sidecar
    rule_key: Optional[str] = None


@dataclass
class PrivacyPolicy:
    local_mode: str = MODE_AUTO
    projects: dict[str, ProjectRule] = field(default_factory=dict)

    def rule_for(self, cwd: str) -> Optional[tuple[str, ProjectRule]]:
        """最长前缀匹配：子目录规则优先于父目录规则。"""
        normalized = normalize_project_path(cwd)
        best: Optional[tuple[str, ProjectRule]] = None
        for key, project_rule in self.projects.items():
            if path_is_within(normalized, key) and (best is None or len(key) > len(best[0])):
                best = (key, project_rule)
        return best

    def decide(self, cwd: Optional[str], harness: str, mode: str) -> Decision:
        if cwd:
            matched = self.rule_for(cwd)
            if matched is not None:
                key, project_rule = matched
                action = ACTION_UPLOAD if project_rule.rule == RULE_ALLOW else ACTION_SKIP
                return Decision(action, project_rule.rule, key)
            reason = "default"
        else:
            reason = "no_cwd" if harness in HARNESSES_WITHOUT_CWD else "broken_sidecar"
        action = ACTION_UPLOAD if mode == MODE_DENYLIST else ACTION_SKIP
        return Decision(action, reason)

    def set_project(self, path: Path | str, rule: str) -> tuple[bool, str]:
        """写入规则；已是同一规则返回 (False, key)。"""
        key = normalize_project_path(path)
        existing = self.projects.get(key)
        if existing is not None and existing.rule == rule:
            return False, key
        self.projects[key] = ProjectRule(
            rule=rule, display=canonical_project_path(path),
            added_at=datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        )
        return True, key

    def clear_project(self, path: Path | str) -> tuple[bool, str]:
        key = normalize_project_path(path)
        return self.projects.pop(key, None) is not None, key

    def to_dict(self) -> dict:
        return {
            "version": 2,
            "mode": self.local_mode,
            "projects": {
                key: {"rule": project_rule.rule, "display": project_rule.display,
                      "added_at": project_rule.added_at}
                for key, project_rule in self.projects.items()
            },
        }

    @classmethod
    def from_dict(cls, data: dict) -> "PrivacyPolicy":
        local_mode = data.get("mode", MODE_AUTO)
        if local_mode not in LOCAL_MODES:
            raise ValueError(f"privacy.json: mode must be one of {LOCAL_MODES}, got {local_mode!r}")
        raw_projects = data.get("projects") or {}
        if not isinstance(raw_projects, dict):
            raise ValueError("privacy.json: projects must be an object")
        projects: dict[str, ProjectRule] = {}
        for key, raw_rule in raw_projects.items():
            if not isinstance(raw_rule, dict) or raw_rule.get("rule") not in (RULE_ALLOW, RULE_DENY):
                raise ValueError(f"privacy.json: bad project rule for {key!r}")
            projects[str(key)] = ProjectRule(
                rule=raw_rule["rule"], display=str(raw_rule.get("display") or key),
                added_at=str(raw_rule.get("added_at") or ""),
            )
        return cls(local_mode=local_mode, projects=projects)


def load_policy(path: Path | str | None = None) -> PrivacyPolicy:
    """不存在返回默认策略；损坏时抛错——静默放行会让用户以为受保护的项目其实在上传。"""
    policy_path = Path(path) if path else default_privacy_path()
    if not policy_path.is_file():
        return PrivacyPolicy()
    try:
        data = json.loads(policy_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"cannot read privacy rules {policy_path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"privacy rules {policy_path}: top-level must be an object")
    return PrivacyPolicy.from_dict(data)


def save_policy(policy: PrivacyPolicy, path: Path | str | None = None) -> Path:
    policy_path = Path(path) if path else default_privacy_path()
    policy_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = policy_path.with_suffix(policy_path.suffix + ".tmp")
    temp_path.write_text(
        json.dumps(policy.to_dict(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8",
    )
    os.replace(temp_path, policy_path)
    return policy_path


@dataclass
class SidecarMetadata:
    cwd: Optional[str]
    model: str
    present: bool
    readable: bool


def read_sidecar_metadata(md_path: Path) -> SidecarMetadata:
    """只读轨迹旁边的同名 ``.json``，不读正文。Windows 工具可能以 GBK 写入，故降级解码。"""
    sidecar_path = md_path.with_suffix(".json")
    if not sidecar_path.is_file():
        return SidecarMetadata(cwd=None, model="", present=False, readable=False)
    try:
        data = json.loads(sidecar_path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return SidecarMetadata(cwd=None, model="", present=True, readable=False)
    if not isinstance(data, dict):
        return SidecarMetadata(cwd=None, model="", present=True, readable=False)
    cwd = data.get("cwd")
    return SidecarMetadata(
        cwd=str(cwd) if cwd else None, model=str(data.get("model") or ""),
        present=True, readable=True,
    )


@dataclass
class LocalTrajectory:
    traj_id: str
    path: Path
    cwd: Optional[str]
    harness: str


def scan_local_trajectories(
    bridge_root: Path, *, time_budget_seconds: Optional[float] = None,
) -> tuple[list[LocalTrajectory], bool]:
    """列出本机 bridge 目录下的轨迹（只读 sidecar）。超出时间预算提前返回，第二项为 False。"""
    from xskill.team.client.collector import harness_for_bridge
    started = time.monotonic()
    rows: list[LocalTrajectory] = []
    for md_path in sorted(Path(bridge_root).glob("*_sessions/traj_*.md")):
        if not md_path.is_file():
            continue
        if time_budget_seconds is not None and time.monotonic() - started > time_budget_seconds:
            return rows, False
        rows.append(LocalTrajectory(
            traj_id=md_path.stem, path=md_path,
            cwd=read_sidecar_metadata(md_path).cwd, harness=harness_for_bridge(md_path),
        ))
    return rows, True


@dataclass
class ProjectSummary:
    path: str
    key: str
    traj: int
    harnesses: list[str]
    rule: Optional[str]
    effective: str


@dataclass
class PrivacyReport:
    mode: str
    origin: str
    server_mode: Optional[str]
    local_mode: str
    projects: list[ProjectSummary]
    no_cwd: ProjectSummary
    broken_sidecar: ProjectSummary
    upload: int
    skip: int
    complete: bool

    def to_dict(self) -> dict:
        def summary_dict(summary: ProjectSummary) -> dict:
            return {"path": summary.path, "traj": summary.traj, "harnesses": summary.harnesses,
                    "rule": summary.rule, "effective": summary.effective}
        return {
            "mode": self.mode, "mode_origin": self.origin,
            "server_mode": self.server_mode, "local_mode": self.local_mode,
            "projects": [summary_dict(summary) for summary in self.projects],
            "unattributed": summary_dict(self.no_cwd),
            "broken_sidecar": summary_dict(self.broken_sidecar),
            "upload": self.upload, "skip": self.skip, "scan_complete": self.complete,
        }


def build_report(
    policy: PrivacyPolicy, server_mode: Optional[str], rows: list[LocalTrajectory],
    *, complete: bool = True,
) -> PrivacyReport:
    """把本机轨迹按项目归组，每组给出规则与生效判定；无轨迹的规则也列出（traj=0）。"""
    mode = effective_mode(server_mode, policy.local_mode)
    grouped: dict[str, ProjectSummary] = {}
    for key, project_rule in policy.projects.items():
        grouped[key] = ProjectSummary(
            path=project_rule.display, key=key, traj=0, harnesses=[], rule=project_rule.rule,
            effective=ACTION_UPLOAD if project_rule.rule == RULE_ALLOW else ACTION_SKIP,
        )
    default_action = ACTION_UPLOAD if mode == MODE_DENYLIST else ACTION_SKIP
    no_cwd = ProjectSummary("(无法归属项目)", "", 0, [], None, default_action)
    broken = ProjectSummary("(sidecar 缺失或损坏)", "", 0, [], None, default_action)
    for row in rows:
        decision = policy.decide(row.cwd, row.harness, mode)
        if decision.reason == "no_cwd":
            target = no_cwd
        elif decision.reason == "broken_sidecar":
            target = broken
        elif decision.rule_key is not None:
            target = grouped[decision.rule_key]
        else:
            key = normalize_project_path(row.cwd)
            target = grouped.setdefault(key, ProjectSummary(
                path=canonical_project_path(row.cwd), key=key, traj=0, harnesses=[],
                rule=None, effective=default_action,
            ))
        target.traj += 1
        if row.harness not in target.harnesses:
            target.harnesses.append(row.harness)
    projects = sorted(grouped.values(), key=lambda summary: summary.path)
    everything = projects + [no_cwd, broken]
    upload = sum(summary.traj for summary in everything if summary.effective == ACTION_UPLOAD)
    skip = sum(summary.traj for summary in everything if summary.effective == ACTION_SKIP)
    return PrivacyReport(
        mode=mode, origin=mode_origin(server_mode, policy.local_mode),
        server_mode=server_mode, local_mode=policy.local_mode, projects=projects,
        no_cwd=no_cwd, broken_sidecar=broken, upload=upload, skip=skip, complete=complete,
    )
