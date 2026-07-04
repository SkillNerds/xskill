"""skill_feature.py — §3 Skill 向量特征

主特征 ``vec`` = description 向量（唯一，不融合）；辅助属性 ``atom_feat`` = 最近 N 个
被路由 atom 摘要向量均值（独立，不并入 ``vec``，无 atom 时 None）。

- ``vec``：优先从 ``.skill_index.pkl["embeddings"]`` 读；缺则用 embed_client 现算
  ``normalize(embed(description))``。
- ``atom_feat``：**只**从 ``.skill_index.pkl["atom_feats"]`` 读（由 ``rebuild_skill_index``
  预计算）。索引缺 ``atom_feats`` 字段 → raise（跑 ``xskill rebuild`` 重建），不静默兜底。

不存在 skill 级 tag 概念（此前从未设计过）；不做任何融合。
"""
from __future__ import annotations

import pickle
from pathlib import Path
from typing import TYPE_CHECKING, Optional

import numpy as np

if TYPE_CHECKING:
    from xskill.skill.skill import Skill


def _get_embed_client():
    """懒构造 embed client（仅 vec 现算路径需要；服务端有 config）。可被测试 monkeypatch。"""
    from xskill.config import get_config
    from xskill.utils.llm import create_embed_client
    return create_embed_client(get_config())


def _normalize(v: np.ndarray) -> np.ndarray:
    n = float(np.linalg.norm(v))
    return v / n if n > 0 else v


def last_n_atom_summaries(
    skill_name: str, atom_store_roots: list[Path] | None, n: int = 5,
) -> list[str]:
    """收集「用过该 skill」的最近 N 个 atom 摘要，按 atom 文件 mtime 降序。

    跨所有 ``atom_store_roots`` 扫描 ``AtomTaskStore.all_atoms()``，筛
    ``skill_name in atom.used_skills`` 的 atom，按其落盘文件 mtime 取最近 N 个。
    无则返回空 list。
    """
    if not atom_store_roots:
        return []
    from xskill.pipeline.atom import AtomTaskStore

    collected: list[tuple[float, str]] = []
    for root in atom_store_roots:
        store = AtomTaskStore(root=Path(root))
        for atom in store.all_atoms():
            if skill_name not in (atom.used_skills or []):
                continue
            atom_path = Path(root) / atom.traj_id / "tasks" / f"{atom.atom_id}.json"
            try:
                mtime = atom_path.stat().st_mtime
            except OSError:
                mtime = 0.0
            if atom.summary:
                collected.append((mtime, atom.summary))
    collected.sort(key=lambda x: x[0], reverse=True)
    return [s for _t, s in collected[:n]]


class SkillFeature:
    """skill 的向量特征视图。

    主特征 ``vec`` = description 向量；辅助 ``atom_feat`` = 最近 atom 摘要均值（独立）。
    两者均懒加载并缓存于本实例。
    """

    def __init__(
        self,
        skill: "Skill",
        *,
        embed_client=None,
        skill_index: dict | None = None,
    ):
        self.skill = skill
        self._embed_client = embed_client
        self._skill_index = skill_index
        self._vec: Optional[np.ndarray] = None
        self._atom_feat: Optional[np.ndarray] = None
        self._atom_feat_resolved = False

    # ── 索引读取 ──────────────────────────────────────────────────
    def _index(self) -> dict | None:
        if self._skill_index is not None:
            return self._skill_index
        idx_path = self.skill.path.parent / ".skill_index.pkl"
        if idx_path.is_file():
            with open(idx_path, "rb") as f:
                return pickle.load(f)
        return None

    def _row_of(self, idx: dict) -> int | None:
        names = idx.get("skill_names") or []
        try:
            return names.index(self.skill.name)
        except ValueError:
            return None

    def _embed(self, text: str) -> np.ndarray:
        client = self._embed_client if self._embed_client is not None else _get_embed_client()
        return np.asarray(client.encode(text), dtype=float)

    # ── 主特征 vec ────────────────────────────────────────────────
    @property
    def vec(self) -> np.ndarray:
        """主特征 = description 向量（L2 归一）。优先读索引，缺则现算。"""
        if self._vec is None:
            idx = self._index()
            row = self._row_of(idx) if idx is not None else None
            if row is not None and "embeddings" in idx:
                self._vec = np.asarray(idx["embeddings"][row], dtype=float)
            else:
                desc = (self.skill.description or "").strip()
                if not desc:
                    raise ValueError(
                        f"skill {self.skill.name!r} 无 description，无法计算 vec"
                    )
                self._vec = _normalize(self._embed(desc))
        return self._vec

    # ── 辅助属性 atom_feat ────────────────────────────────────────
    @property
    def atom_feat(self) -> Optional[np.ndarray]:
        """辅助属性 = 最近 N atom 摘要均值（L2 归一），独立不并入 vec；无 atom 时 None。

        只从预计算索引读。索引缺 ``atom_feats`` 字段 → raise（需 ``xskill rebuild``）。
        """
        if not self._atom_feat_resolved:
            idx = self._index()
            row = self._row_of(idx) if idx is not None else None
            if idx is None or row is None:
                raise RuntimeError(
                    f"skill {self.skill.name!r}: 无 skill 索引，无法读 atom_feat；"
                    f"请跑 `xskill rebuild` 重建索引"
                )
            if "atom_feats" not in idx:
                raise RuntimeError(
                    f"skill {self.skill.name!r}: 索引缺 atom_feats 字段；"
                    f"请跑 `xskill rebuild` 重建索引"
                )
            present = idx.get("atom_feat_present") or []
            if row < len(present) and present[row]:
                self._atom_feat = np.asarray(idx["atom_feats"][row], dtype=float)
            else:
                self._atom_feat = None
            self._atom_feat_resolved = True
        return self._atom_feat
