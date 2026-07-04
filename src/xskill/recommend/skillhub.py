"""skillhub.py — §6 三方 skill 目录扫描（CS 模式选配）

扫描 ``~/.xskill/skillhub_skills/`` 下的三方 ``SKILL.md``，按 **description** 向量化
（三方 skill 在本仓无被路由 atom，故无 ``atom_feat``），纳入 ``SkillRecommendEngine``
检索池。三方 skill 无 git 分支/灰度 → 仅参与相关性位，不进质量位/staging 达量。
"""
from __future__ import annotations

from pathlib import Path

import numpy as np

from xskill.config import skillhub_config
from xskill.skill.frontmatter import parse as fm_parse


def _normalize(v: np.ndarray) -> np.ndarray:
    n = float(np.linalg.norm(v))
    return v / n if n > 0 else v


class SkillHub:
    """三方 skill 扫描器。``enabled=False``（缺省）时为 no-op。"""

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
