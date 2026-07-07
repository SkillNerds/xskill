"""skillhub.py — §6 三方 skill 目录扫描（CS 模式选配）+ §7 ux 查询

扫描 ``~/.xskill/skillhub_skills/`` 下的三方 ``SKILL.md``，按 **description** 向量化
（三方 skill 在本仓无被路由 atom，故无 ``atom_feat``），纳入 ``SkillRecommendEngine``
检索池。三方 skill 无 git 分支/灰度 → 仅参与相关性位，不进质量位/staging 达量。

被推荐 & 使用后的三方 skill 同样要被打 ux 分、可查询其 ux 分（与自有 skill 对齐）。
本模块提供查询接口（``ux_avg`` / ``recent_ux_scores``）与版本号（``content_sha``），
供 ``runner._score_atoms_for_traj`` 在自有 ``skill_dir`` 找不到该 skill 时回退定位。
"""
from __future__ import annotations

import hashlib
from datetime import datetime
from pathlib import Path

import numpy as np

from xskill.canary import aggregate_ux_by_version, load_ux_scores
from xskill.config import skillhub_config
from xskill.skill.frontmatter import parse as fm_parse


def _normalize(v: np.ndarray) -> np.ndarray:
    n = float(np.linalg.norm(v))
    return v / n if n > 0 else v


class SkillHub:
    """三方 skill 扫描器 + ux 查询。``enabled=False``（缺省）时为 no-op。"""

    def __init__(self, *, enabled: bool, hub_dir: Path | str, embed_client):
        self.enabled = bool(enabled)
        self.dir = Path(hub_dir)
        self.embed_client = embed_client

    @classmethod
    def from_config(cls, config: dict, embed_client) -> "SkillHub":
        cfg = skillhub_config(config)
        return cls(enabled=cfg["enabled"], hub_dir=cfg["dir"], embed_client=embed_client)

    def index(self) -> list[dict]:
        """返回 ``[{name, vec, description}]``。禁用 → 空 list；启用但目录缺失 → raise。"""
        if not self.enabled:
            return []
        if not self.dir.is_dir():
            raise FileNotFoundError(
                f"skillhub.dir 不存在: {self.dir}（启用 skillhub 前请放置三方 skill）"
            )
        entries: list[dict] = []
        for sub in sorted(self.dir.iterdir()):
            if not sub.is_dir() or sub.name.startswith("."):
                continue
            md = sub / "SKILL.md"
            if not md.is_file():
                continue
            fm, _body = fm_parse(md.read_text(encoding="utf-8"))
            desc = (fm.get("description") or "").strip()
            if not desc:
                continue
            vec = _normalize(np.asarray(self.embed_client.encode(desc), dtype=float))
            entries.append({"name": sub.name, "vec": vec, "description": desc})
        return entries

    # ── §7 三方 skill ux 定位 / 版本 / 查询 ──────────────────────
    # 三方 skill 无 git → 版本号用 SKILL.md 内容 sha256 前 16 位；side 恒 "main"
    # （无 staging 分支）。ux 分落盘到 ``skillhub_dir/<name>/.ux_scores.jsonl``，
    # 由 ``runner`` 经 ``AtomCanary(skill_dir=skillhub_dir/<name>).append`` 写入；
    # 本类只负责读回。
    def skill_path(self, name: str) -> Path | None:
        """返回三方 skill 目录路径（含 ``SKILL.md``）；未启用 / 不存在 → None。"""
        if not self.enabled:
            return None
        sub = self.dir / name
        if not sub.is_dir() or not (sub / "SKILL.md").is_file():
            return None
        return sub

    def content_sha(self, name: str) -> str | None:
        """三方 skill 版本号 = ``SKILL.md`` 内容 sha256 前 16 位。无 git → 内容哈希。"""
        sub = self.skill_path(name)
        if sub is None:
            return None
        return hashlib.sha256((sub / "SKILL.md").read_bytes()).hexdigest()[:16]

    def recent_ux_scores(self, name: str, days: int = 30) -> list[dict]:
        """读三方 skill 的 ``.ux_scores.jsonl``，按 ``days`` 截断近期。无数据 → []。"""
        sub = self.skill_path(name)
        if sub is None:
            return []
        scores = load_ux_scores(sub)
        if days > 0:
            cutoff = datetime.utcnow().timestamp() - days * 86400
            kept: list[dict] = []
            for s in scores:
                ts = s.get("scored_at", "")
                try:
                    if datetime.fromisoformat(ts.rstrip("Z")).timestamp() >= cutoff:
                        kept.append(s)
                except Exception:
                    kept.append(s)
            scores = kept
        return scores

    def ux_avg(self, name: str, days: int = 30) -> float | None:
        """三方 skill 近期 ux 均分；无评分 → None。与 ``Skill.ux_avg`` 同口径。

        按**当前版本 content_sha** 过滤（三方 skill 无 git，版本号 = SKILL.md
        内容哈希前 16 位）；旧版本的分留在 append-only 文件里不再混算。
        """
        sha = self.content_sha(name)
        if sha is None:
            return None
        rows = self.recent_ux_scores(name, days=days)
        scores = [r.get("score") for r in rows
                  if r.get("commit_sha") == sha
                  and isinstance(r.get("score"), (int, float))]
        if not scores:
            return None
        return sum(scores) / len(scores)

    def ux_scores_by_version(self, name: str, days: int = 30) -> list[dict]:
        """按 ``commit_sha`` 分组聚合三方 skill ux 分（side 恒 ``main``）。

        返回结构与 ``Skill.ux_scores_by_version`` 一致：
        ``[{"commit_sha", "side", "count", "avg", "first_scored_at",
        "last_scored_at"}]``，按 ``last_scored_at`` 降序。skill 不存在或无数据
        → 空列表。
        """
        sub = self.skill_path(name)
        if sub is None:
            return []
        rows = self.recent_ux_scores(name, days=days)
        return aggregate_ux_by_version(rows)

    def ux_scores_with_atoms(self, name: str, *,
                             commit_sha: str | None = None,
                             days: int = 30,
                             traj_root: Path | None = None) -> list[dict]:
        """每条 ux 分关联其 atom 内容（三方 skill 版本）。

        与 ``Skill.ux_scores_with_atoms`` 同结构；``traj_root`` 给定时按 team
        server 落盘结构反查 atom，不给则 ``atom=None``。skill 不存在 → 空列表。
        """
        from xskill.pipeline.atom import load_atom_by_id

        sub = self.skill_path(name)
        if sub is None:
            return []
        rows = self.recent_ux_scores(name, days=days)
        if commit_sha is not None:
            rows = [r for r in rows if r.get("commit_sha") == commit_sha]
        out: list[dict] = []
        for r in rows:
            atom_id = r.get("atom_id") or ""
            atom = (load_atom_by_id(traj_root, atom_id)
                    if traj_root is not None and atom_id else None)
            out.append({
                "atom_id": atom_id,
                "commit_sha": r.get("commit_sha", ""),
                "side": r.get("side", ""),
                "score": r.get("score"),
                "reasons": r.get("reasons", ""),
                "scored_at": r.get("scored_at", ""),
                "user_model": r.get("user_model", ""),
                "atom": atom,
            })
        out.sort(key=lambda d: d["scored_at"], reverse=True)
        return out
