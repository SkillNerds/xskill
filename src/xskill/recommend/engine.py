"""engine.py — §5 SkillRecommendEngine

面向对象的推荐引擎：维护用户画像 vec_store（ProfileStore）+ skill vec_store
（``.skill_index.pkl``，仅 main+staging 可分发 skill，排除 baby）。

- ``update_user_interest``：atom 触发 → 重扫用户 atom 摘要 → 重新聚类 → upsert 画像。
- ``get_skill_for_client``：80% 质量（ux）+ 20% 相关性（向量 KNN），质量不足相关性回填。
- ``resolve_side``：staging 优先达量（未达量→staging；staging 达量 main 未达量→main；
  双侧达量→``CanaryRouter.assign``），修复 pickside 饿死。记录双向推荐。
- ``find_friend`` / ``find_tag_for_user`` / ``find_tag_for_skill``。
"""
from __future__ import annotations

import logging
import math
from pathlib import Path
from typing import TYPE_CHECKING, Optional

import numpy as np

from xskill.canary import CanaryConfig, has_staging, main_sha, pick_side, staging_sha
from xskill.config import recommend_config
from xskill.pipeline.atom import AtomTaskStore
from xskill.recommend.profile_store import ProfileStore
from xskill.recommend.reco_store import RecoStore
from xskill.recommend.skillhub import SkillHub
from xskill.skill.repo import SkillRepo

if TYPE_CHECKING:
    from xskill.recommend.client_interest import ClientInterest
    from xskill.recommend.client_user import ClientUser
    from xskill.skill.skill import Skill

logger = logging.getLogger("xskill.recommend.engine")


def _normalize_rows(v: np.ndarray) -> np.ndarray:
    n = np.linalg.norm(v, axis=1, keepdims=True)
    n[n == 0] = 1
    return v / n


class SkillRecommendEngine:
    """推荐引擎。team server 进程持有单例。"""

    def __init__(
        self,
        *,
        config: dict,
        skill_dir: Path | str,
        traj_root: Path | str,
        embed_client,
        profile_db: Path | str,
        canary_config: Optional[CanaryConfig] = None,
    ):
        self.config = config
        self.skill_dir = Path(skill_dir)
        self.traj_root = Path(traj_root)
        self.embed_client = embed_client
        self.rcfg = recommend_config(config)
        self.profile_store = ProfileStore(profile_db)
        self.reco_store = RecoStore(profile_db)
        self.canary_cfg = canary_config or CanaryConfig.from_dict(config.get("canary", {}))
        self.staging_need = self.rcfg["staging_need"] or self.canary_cfg.total_samples
        self._skill_index_cache: Optional[dict] = None
        self._skillhub_cache: Optional[list[dict]] = None
        self.skillhub = SkillHub.from_config(config, embed_client)

    # ─§6 三方 skill 检索池 ────────────────────────────────────────
    def _skillhub_entries(self) -> list[dict]:
        """三方 skill ``{name, vec}``（缓存）。禁用时为空。"""
        if self._skillhub_cache is None:
            self._skillhub_cache = self.skillhub.index()
        return self._skillhub_cache

    def _combined_relevance(self) -> tuple[list[str], np.ndarray, dict[str, bool]]:
        """合并检索池：可分发 skill 的 desc 向量 + 三方 skill 向量。

        返回 ``(names, embeddings, is_skillhub)``。三方 skill 标记 True（仅相关性位）。
        """
        idx = self._skill_index()
        repo_names = list(idx.get("skill_names") or [])
        repo_embs = np.asarray(idx["embeddings"], dtype=float)
        distributable = {s.name for s in self._distributable_skills()}
        # 仅保留可分发 skill（排除 baby / 已删）
        keep = [i for i, n in enumerate(repo_names) if n in distributable]
        names = [repo_names[i] for i in keep]
        embs = repo_embs[keep] if keep else np.zeros((0, repo_embs.shape[1] if repo_embs.ndim == 2 else 0))
        is_hub = {n: False for n in names}
        for e in self._skillhub_entries():
            if e["name"] in is_hub:
                continue  # 自有 skill 同名优先
            names.append(e["name"])
            embs = np.vstack([embs, np.asarray(e["vec"], dtype=float)]) if len(embs) else \
                np.asarray([e["vec"]], dtype=float)
            is_hub[e["name"]] = True
        return names, embs, is_hub

    # ── skill 索引 / 池 ───────────────────────────────────────────
    def _skill_index(self) -> dict:
        if self._skill_index_cache is None:
            import pickle
            idx_path = self.skill_dir / ".skill_index.pkl"
            if not idx_path.is_file():
                raise RuntimeError(
                    f"skill 索引不存在: {idx_path}；请跑 `xskill rebuild` 重建"
                )
            with open(idx_path, "rb") as f:
                self._skill_index_cache = pickle.load(f)
        return self._skill_index_cache

    def _repo(self) -> SkillRepo:
        return SkillRepo(self.skill_dir)

    def _distributable_skills(self) -> list["Skill"]:
        """可分发 skill = 有 main 分支（baby-only 不入池）。"""
        return [s for s in self._repo() if main_sha(s.path)]

    # ── 用户 atom 派生 ────────────────────────────────────────────
    def _client_store_root(self, user_id: str) -> Path:
        return self.traj_root / "clients" / user_id / "sessions"

    def _user_atoms(self, user_id: str):
        root = self._client_store_root(user_id)
        if not root.is_dir():
            return []
        return list(AtomTaskStore(root=root).all_atoms())

    def _user_used_skills(self, user_id: str) -> list[dict]:
        """从用户 atom 聚合 ``{name, use_count, avg_score}``。"""
        agg: dict[str, list[float]] = {}
        for atom in self._user_atoms(user_id):
            for name in (atom.used_skills or []):
                agg.setdefault(name, []).append(
                    float(atom.ux_score) if atom.ux_score is not None else 0.0
                )
        out: list[dict] = []
        for name, scores in agg.items():
            out.append({
                "name": name,
                "use_count": len(scores),
                "avg_score": sum(scores) / len(scores),
            })
        out.sort(key=lambda d: d["use_count"], reverse=True)
        return out

    # ── 5.2 update_user_interest ─────────────────────────────────
    def update_user_interest(
        self, client_interest: "ClientInterest", task_atom=None,
    ) -> None:
        """atom 触发：重扫用户 atom 摘要 → 重新聚类 → upsert 画像。

        ``task_atom`` 为触发事件（增量优化预留）；点集以 atom store 为单一真源重扫。
        """
        from xskill.recommend.client_interest import ClientInterest  # noqa: F401
        user_id = client_interest.user_id
        atoms = self._user_atoms(user_id)
        summaries = [a.summary for a in atoms if a.summary]
        used_skills = self._user_used_skills(user_id)
        if not summaries:
            self.profile_store.upsert(
                user_id, feature_tensor=None, mean_tensor=None, used_skills=used_skills,
            )
            return
        vecs = _normalize_rows(np.asarray(
            self.embed_client.encode_batch(summaries), dtype=float,
        ))
        client_interest._points = vecs
        client_interest._feature_tensor = None
        client_interest._mean_tensor = None
        ft = client_interest.feature_tensor
        mt = client_interest.mean_tensor
        self.profile_store.upsert(
            user_id, feature_tensor=ft, mean_tensor=mt, used_skills=used_skills,
        )

    # ── 5.3 get_skill_for_client ─────────────────────────────────
    def get_skill_for_client(
        self, client_user: "ClientUser", skill_num: int,
        *, exclude_names: Optional[set[str]] = None,
    ) -> list["Skill"]:
        """80% 质量 + 20% 相关性，质量不足相关性回填；记录推荐 + resolve side。

        ``exclude_names``：从候选池排除的 skill 名（如已占 ranked 槽位的），供
        ``_pick_recommended`` 在 ranked 之外选 recommended 位用。
        """
        pool = self._distributable_skills()
        if exclude_names:
            pool = [s for s in pool if s.name not in exclude_names]
        if not pool:
            return []

        quality_ratio = self.rcfg["quality_ratio"]
        qn = min(math.ceil(skill_num * quality_ratio), len(pool))
        quality = sorted(
            pool,
            key=lambda s: (s.ux_avg(side="main", days=30) or 0.0, s.use_count),
            reverse=True,
        )[:qn]
        quality_names = {s.name for s in quality}

        relevance: list["Skill"] = []
        ci = client_user.client_interest
        if ci is not None and ci.feature_tensor is not None:
            names, embs, _is_hub = self._combined_relevance()
            by_name = {s.name: s for s in pool}  # pool 已排除 exclude_names
            picked = set(quality_names)
            for center in ci.feature_tensor:
                if len(quality) + len(relevance) >= skill_num:
                    break
                if embs.shape[0] == 0:
                    break
                sims = embs @ np.asarray(center, dtype=float)
                order = np.argsort(-sims)
                for i in order:
                    nm = names[i]
                    # 仅返回可分发 skill（skillhub-only 无 git，不能作为 slot 分发）
                    if nm in by_name and nm not in picked:
                        relevance.append(by_name[nm])
                        picked.add(nm)
                        if len(quality) + len(relevance) >= skill_num:
                            break

        chosen = quality + relevance
        # 回填：质量池不足时从 pool（ux 序）补齐至 skill_num
        if len(chosen) < skill_num:
            for s in sorted(
                pool,
                key=lambda s: (s.ux_avg(side="main", days=30) or 0.0, s.use_count),
                reverse=True,
            ):
                if len(chosen) >= skill_num:
                    break
                if s not in chosen:
                    chosen.append(s)

        chosen = chosen[:skill_num]
        # 记录推荐 + resolve side（双向）
        client_user.recommended_skills = []
        for s in chosen:
            side = self.resolve_side(s, client_user)
            sha = staging_sha(s.path) if side == "staging" else (main_sha(s.path) or "")
            self.reco_store.record(
                user_id=client_user.user_id, skill_name=s.name, side=side, sha=sha,
            )
            client_user.recommended_skills.append(
                {"skill": s.name, "branch": side, "hash": sha}
            )
        return chosen

    # ── 5.4 resolve_side：staging 优先达量 ───────────────────────
    def _side_count(self, skill_dir: Path, side: str, sha: str) -> int:
        from xskill.canary import recent_scores
        return len(recent_scores(skill_dir, side=side, commit_sha=sha, n=self.staging_need + 1))

    def resolve_side(self, skill: "Skill", client_user: "ClientUser") -> str:
        """staging 优先达量：未达量→staging；staging 达量 main 未达量→main；双侧达量→pick_side。

        双侧达量时用 ``pick_side(user_id, skill_name, probability)`` 做确定性分流
        （main 分支上的既有机制；``CanaryRouter`` 的有状态均衡在其合入后可替换此处）。
        """
        if not has_staging(skill.path):
            return "main"
        s_sha = staging_sha(skill.path) or ""
        m_sha = main_sha(skill.path) or ""
        staging_n = self._side_count(skill.path, "staging", s_sha)
        if staging_n < self.staging_need:
            return "staging"
        main_n = self._side_count(skill.path, "main", m_sha)
        if main_n < self.staging_need:
            return "main"
        return pick_side(client_user.user_id, skill.name, self.canary_cfg.probability)

    # ── 5.6 find_friend ──────────────────────────────────────────
    def relevance_search(self, query_vec, top_k: int = 5) -> list[tuple[str, bool]]:
        """在合并检索池（可分发 + 三方 skill）做 KNN，返回 ``(name, is_skillhub)``。"""
        names, embs, is_hub = self._combined_relevance()
        if embs.shape[0] == 0:
            return []
        sims = embs @ np.asarray(query_vec, dtype=float)
        order = np.argsort(-sims)[:top_k]
        return [(names[i], is_hub.get(names[i], False)) for i in order]

    def load_client_user(self, user_id: str) -> "ClientUser":
        """从持久化加载 ``ClientUser``（画像 + used_skills + recommended_skills）。

        无画像行 → 冷启动 ``ClientUser``（client_interest=None）。
        """
        from xskill.recommend.client_interest import ClientInterest
        from xskill.recommend.client_user import ClientUser
        row = self.profile_store.load(user_id)
        if row is None:
            return ClientUser(user_id)
        ci = ClientInterest(
            user_id,
            feature_tensor=row["feature_tensor"],
            mean_tensor=row["mean_tensor"],
        )
        return ClientUser(
            user_id, client_interest=ci,
            used_skills=row["used_skills"],
            recommended_skills=self.reco_store.skills_for_user(user_id),
        )

    def find_friend(self, client_user: "ClientUser", top_k: int = 5) -> list[str]:
        """按 mean_tensor 相似度检索其他用户。"""
        ci = client_user.client_interest
        if ci is None or ci.mean_tensor is None:
            return []
        mine = np.asarray(ci.mean_tensor, dtype=float)
        others = [(uid, m) for uid, m in self.profile_store.all_means()
                  if uid != client_user.user_id]
        if not others:
            return []
        scored = sorted(
            others,
            key=lambda um: float(np.asarray(um[1], dtype=float) @ mine),
            reverse=True,
        )
        return [uid for uid, _m in scored[:top_k]]

    # ── 5.7 find_tag_for_user / find_tag_for_skill ────────────────
    def _all_tags_with_embeds(self) -> list[tuple[str, np.ndarray]]:
        """收集 traj_root 下所有 atom 的 tag → embedding（去重）。"""
        seen: set[str] = set()
        tags: list[str] = []
        clients_dir = self.traj_root / "clients"
        if not clients_dir.is_dir():
            return []
        for client_dir in sorted(clients_dir.iterdir()):
            if not client_dir.is_dir():
                continue
            root = client_dir / "sessions"
            if not root.is_dir():
                continue
            for atom in AtomTaskStore(root=root).all_atoms():
                for t in (atom.tags or []):
                    if t not in seen:
                        seen.add(t)
                        tags.append(t)
        if not tags:
            return []
        vecs = _normalize_rows(np.asarray(
            self.embed_client.encode_batch(tags), dtype=float,
        ))
        return list(zip(tags, vecs))

    def find_tag_for_user(self, client_user: "ClientUser", top_k: int = 5) -> list[str]:
        ci = client_user.client_interest
        if ci is None or ci.mean_tensor is None:
            return []
        mine = np.asarray(ci.mean_tensor, dtype=float)
        tag_vecs = self._all_tags_with_embeds()
        if not tag_vecs:
            return []
        scored = sorted(
            tag_vecs,
            key=lambda tv: float(np.asarray(tv[1], dtype=float) @ mine),
            reverse=True,
        )
        return [t for t, _v in scored[:top_k]]

    def find_tag_for_skill(self, skill: "Skill", top_k: int = 10) -> list[str]:
        """该 skill 被路由 atom 的 ``AtomTask.tags`` 去重（atom 级 tag）。"""
        seen: set[str] = set()
        clients_dir = self.traj_root / "clients"
        if not clients_dir.is_dir():
            return []
        for client_dir in sorted(clients_dir.iterdir()):
            if not client_dir.is_dir():
                continue
            root = client_dir / "sessions"
            if not root.is_dir():
                continue
            for atom in AtomTaskStore(root=root).all_atoms():
                if skill.name in (atom.used_skills or []):
                    for t in (atom.tags or []):
                        seen.add(t)
        return list(seen)[:top_k]
