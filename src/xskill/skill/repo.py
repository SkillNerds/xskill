"""
skill/repo.py — SkillRepo 集合 + 集合级 git 操作
═══════════════════════════════════════════════════
管理 ~/.xskill/skill/ 下所有 Skill 子目录。dict-like + iterable。
顶层 .git 已废弃，所有 git 操作走 <skill>/.git 子仓。

模块函数 ``list_skills`` / ``import_skill`` 是对整个 skill 仓库（集合）的
操作（原 skill_manager.py 的集合部分）。
"""

from __future__ import annotations

from xskill.skill.write_policy import require_management_write

import logging
import operator
import pickle
from pathlib import Path
from typing import TYPE_CHECKING, Iterator, Optional

import numpy

from xskill.skill.skill import Skill, _load_skill, _remove_tree
from xskill.skill.git import commit_changes

if TYPE_CHECKING:
    from xskill.pipeline.registry import Registry

logger = logging.getLogger("xskill.skill_manager")


class SkillRepo:
    """skill_dir 顶层视图。

    接口：
      repo["foo"]            → Skill | KeyError
      "foo" in repo          → bool
      for s in repo: ...     → 迭代所有 Skill
      len(repo)              → int
      repo.get("foo")        → Skill | None
      repo.rebuild_index()   → 重建 .skill_index.pkl
    """

    def __init__(self, root: Path, registry: Optional["Registry"] = None):
        self.root = Path(root).expanduser()
        self.root.mkdir(parents=True, exist_ok=True)
        self._registry = registry

    # ─── dict-like ─────────────────────────────────────────────
    def __getitem__(self, name: str) -> Skill:
        skill_path = self.root / name
        if not (skill_path / "SKILL.md").is_file():
            raise KeyError(f"skill not found: {name}")
        return Skill(path=skill_path, registry=self._registry)

    def get(self, name: str) -> Optional[Skill]:
        try:
            return self[name]
        except KeyError:
            return None

    def __contains__(self, name: str) -> bool:
        return (self.root / name / "SKILL.md").is_file()

    def __iter__(self) -> Iterator[Skill]:
        if not self.root.is_dir():
            return iter([])
        for sub in sorted(self.root.iterdir()):
            if not sub.is_dir():
                continue
            if sub.name.startswith(".") or sub.name == "references":
                continue
            if not (sub / "SKILL.md").is_file():
                continue
            yield Skill(path=sub, registry=self._registry)

    def __len__(self) -> int:
        return sum(1 for _ in self)

    # ─── 索引 ──────────────────────────────────────────────────
    def rebuild_index(
        self,
        *,
        atom_store_roots: list[Path] | None = None,
        scope: str = "search",
    ) -> None:
        """重建 .skill_index.pkl（向量检索用）。

        ``scope``：``search``（默认）只写 description embeddings；``full`` 另算
        ``atom_feats``（需 ``atom_store_roots``，会扫全部 atom，代价高）。
        ``atom_store_roots``：仅 ``scope=full`` 时使用；为 None 时 ``atom_feats``
        全部 absent。
        """
        from xskill.config import get_config
        from xskill.utils.llm import create_embed_client
        embed_client = create_embed_client(get_config())
        rebuild_skill_index(
            skill_dir=self.root, embed_client=embed_client,
            atom_store_roots=atom_store_roots, scope=scope,
        )

    # ─── 清空（rebuild --force 用）──────────────────────────────
    def wipe_all_skills(self, *, db_path: Path | None = None) -> tuple[int, list[str]]:
        """删除仓里蒸馏所得 skill 子目录（含各自 ``.git`` 子仓）。

        ``xskill rebuild --force`` 用：换强模型从零重建前先清空旧蒸馏产物。
        用户 ``xskill import`` 纳入的技能留下。``xskill upload`` 落在 skillhub
        目录，本来就不在这个仓里。
        只扫 skill 子目录（每个有 ``SKILL.md`` 或 ``.git`` 的子目录），保留
        仓根与 ``references`` / ``.skill_index.pkl`` 等非 skill 工件由 watcher
        后续自行重建。删完清掉过期索引，并只删被去掉的那些 catalog 行。
        返回 ``(删除个数, 留下的名字)``。
        """
        require_management_write(self.root, operation='wipe')
        n = 0
        kept: list[str] = []
        removed: list[str] = []
        if not self.root.is_dir():
            return 0, kept
        from xskill.skill.importer import skill_kept_on_rebuild
        for sub in sorted(self.root.iterdir()):
            if not sub.is_dir():
                continue
            if sub.name == "references":
                continue
            # 一个 skill 目录的判据：有 SKILL.md 或 .git 子仓（baby 态可能
            # 只有 .git 还没写 SKILL.md）。
            if not ((sub / "SKILL.md").is_file() or (sub / ".git").is_dir()):
                continue
            if skill_kept_on_rebuild(sub):
                kept.append(sub.name)
                continue
            _remove_tree(sub)
            removed.append(sub.name)
            n += 1
        # 索引已失效，删掉避免指向不存在的 skill
        idx = self.root / ".skill_index.pkl"
        if idx.is_file():
            idx.unlink()
        logger.info(
            "wipe_all_skills: removed %d skill(s), kept %d under %s",
            n, len(kept), self.root,
        )
        if removed:
            from xskill.skill.catalog_store import notify_native_delete
            for name in removed:
                notify_native_delete(name, db_path=db_path)
        return n, kept

    def __repr__(self) -> str:
        return f"SkillRepo({self.root}, n={len(self)})"


# ═══════════════════════════════════════════════════════════════════
# 集合级 git 操作（原 skill_manager.py 集合部分）
# ═══════════════════════════════════════════════════════════════════


def list_skills(skill_dir: Path) -> list[dict]:
    """List all skills with v2 metadata. Legacy skills are surfaced via the
    synthesized frontmatter in _load_skill."""
    results = []
    if not skill_dir.exists():
        return results

    for d in sorted(skill_dir.iterdir()):
        if not d.is_dir() or d.name.startswith("."):
            continue
        # Skip scaffold dirs without SKILL.md (gate-rejected, only have .candidates.yml)
        if not (d / "SKILL.md").is_file() and not (d / "skill.md").is_file():
            continue

        fm, _body, _p = _load_skill(d)
        meta = fm.get("metadata", {}) or {}
        eval_block = meta.get("eval", {}) or {}
        entry = {
            "name": d.name,
            "version": int(meta.get("version", 0) or 0),
            "eval_score": eval_block.get("eval_score") or eval_block.get("score"),
            "tags": meta.get("tags", []) or [],
            "frozen": bool(meta.get("frozen", False)),
        }
        results.append(entry)

    return results


def rebuild_skill_index(
    *,
    skill_dir: Path,
    embed_client,
    atom_store_roots: list[Path] | None = None,
    last_n_atoms: int = 5,
    scope: str = "search",
) -> None:
    """Rebuild ``<skill_dir>/.skill_index.pkl`` for skill semantic search.

    主特征 ``embeddings`` = **description-only** 向量（L2 归一）——不融合 tags/summary。

    ``scope``：
    - ``search``（默认）：只写 description embeddings；``atom_feats`` 全零、
      ``atom_feat_present`` 全 False；**不扫描** atom store。用于检索止血。
    - ``full``：另算 ``atom_feats`` = 每个 skill 最近 ``last_n_atoms`` 个被路由
      atom 摘要均值（独立字段，不并入 ``embeddings``）。会扫全部
      ``atom_store_roots`` 下 atom（代价高，O(atoms) 一次扫描 + summary embed）。
      无 ``atom_store_roots`` 或某 skill 无 atom 时该行零向量、present=False。

    非法 ``scope`` 抛 ``ValueError``，不做静默兜底。
    """
    require_management_write(skill_dir, operation='rebuild_index')
    skill_root = Path(skill_dir)
    if embed_client is None:
        raise RuntimeError("rebuild_skill_index: embed_client is required")
    if scope not in ("search", "full"):
        raise ValueError(
            f"rebuild_skill_index: invalid scope {scope!r}; "
            "expected 'search' or 'full'"
        )

    entries = []
    for skill_path in sorted(skill_root.iterdir()):
        if not skill_path.is_dir() or skill_path.name.startswith("."):
            continue
        frontmatter, _body, _path = _load_skill(skill_path)
        if not frontmatter:
            continue
        description = (frontmatter.get("description") or "").strip()
        # 空 description（含宽松 frontmatter 恢复失败后的空串）发给 embedding
        # 端点会 400 并拖死整轮 reindex（#200）。跳过并告警，局部降级。
        if not description:
            logger.warning(
                "rebuild_skill_index: skip skill %s with empty description",
                skill_path.name,
            )
            continue
        entries.append((skill_path.name, description))

    if not entries:
        logger.info("no skills to index")
        return

    skill_names, descriptions = zip(*entries)
    descriptions = list(descriptions)

    # 主特征：description-only 向量（EmbedStore 按内容哈希复用，只算变化项）
    from xskill.utils.embed_store import EmbedStore
    embed_store = EmbedStore(skill_root / ".skill_embed_cache.pkl", embed_client)
    embeddings = embed_store.encode_cached(descriptions)
    embeddings = numpy.asarray(embeddings, dtype=float)
    norms = numpy.linalg.norm(embeddings, axis=1, keepdims=True)
    norms[norms == 0] = 1
    embeddings = embeddings / norms

    # 辅助属性 atom_feat：仅 scope=full；search 路径明确跳过 atom 扫描
    dim = embeddings.shape[1]
    atom_feats = numpy.zeros((len(skill_names), dim), dtype=float)
    atom_present = [False] * len(skill_names)

    if scope == "full" and atom_store_roots:
        # 一次扫完全部 atom，按 skill 聚最近 N 条，再对去重 summary 组批 embed
        # ——避免旧实现「每 skill 重复全表扫」的 O(skills×atoms)。
        from xskill.pipeline.atom import AtomTaskStore

        skill_name_set = set(skill_names)
        collected_by_skill: dict[str, list[tuple[float, str]]] = {
            name: [] for name in skill_names
        }
        for root in atom_store_roots:
            store = AtomTaskStore(root=Path(root))
            for atom in store.all_atoms():
                if not atom.summary:
                    continue
                used_skills = atom.used_skills or []
                atom_path = (
                    Path(root) / atom.traj_id / "tasks" / f"{atom.atom_id}.json"
                )
                try:
                    mtime = atom_path.stat().st_mtime
                except OSError:
                    mtime = 0.0
                for used_name in used_skills:
                    if used_name in skill_name_set:
                        collected_by_skill[used_name].append(
                            (mtime, atom.summary),
                        )

        summary_lists: list[list[str]] = []
        unique_summaries: list[str] = []
        seen_summaries: set[str] = set()
        for name in skill_names:
            rows = collected_by_skill[name]
            rows.sort(key=operator.itemgetter(0), reverse=True)
            summaries = [text for _mtime, text in rows[:last_n_atoms]]
            summary_lists.append(summaries)
            for text in summaries:
                if text not in seen_summaries:
                    seen_summaries.add(text)
                    unique_summaries.append(text)

        summary_vectors: dict[str, numpy.ndarray] = {}
        if unique_summaries:
            vectors = numpy.asarray(
                embed_store.encode_cached(unique_summaries), dtype=float,
            )
            for text, vector in zip(unique_summaries, vectors):
                summary_vectors[text] = vector

        for index, summaries in enumerate(summary_lists):
            if not summaries:
                continue
            vecs = numpy.asarray(
                [summary_vectors[text] for text in summaries], dtype=float,
            )
            mean = vecs.mean(axis=0)
            norm = float(numpy.linalg.norm(mean))
            atom_feats[index] = mean / norm if norm > 0 else mean
            atom_present[index] = True
    elif scope == "full":
        logger.info(
            "rebuild_skill_index scope=full but atom_store_roots is empty; "
            "atom_feats left absent"
        )

    # 本轮已覆盖全部 description（+ full 时的 summary），压掉陈旧向量。
    embed_store.flush_pruned()

    index_data = {
        "skill_names": list(skill_names),
        "texts": descriptions,
        "embeddings": embeddings,
        "atom_feats": atom_feats,
        "atom_feat_present": atom_present,
        "model": str(getattr(embed_client, "model", "") or ""),
        "schema_version": 2,
        "method": "api",
    }

    index_path = skill_root / ".skill_index.pkl"
    with open(index_path, "wb") as index_file:
        pickle.dump(index_data, index_file)

    logger.info(
        "skill index rebuilt (scope=%s): %d entries -> %s",
        scope, len(skill_names), index_path,
    )


def search_skill_index(*, skill_dir: Path, query: str, embed_client, top_k: int = 5) -> list[dict]:
    """Search ``<skill_dir>/.skill_index.pkl`` by semantic similarity."""
    skill_root = Path(skill_dir)
    index_path = skill_root / ".skill_index.pkl"

    if not index_path.exists():
        return []
    if embed_client is None:
        raise RuntimeError("search_skill_index: embed_client is required")

    with open(index_path, "rb") as index_file:
        index_data = pickle.load(index_file)

    embeddings = index_data["embeddings"]
    skill_names = index_data["skill_names"]
    query_embedding = embed_client.encode(query)
    norm = numpy.linalg.norm(query_embedding)
    if norm > 0:
        query_embedding = query_embedding / norm

    similarities = embeddings @ query_embedding
    ranked = sorted(
        enumerate(similarities), key=lambda item: item[1], reverse=True,
    )

    results = []
    for skill_index, similarity in ranked[:top_k]:
        skill_name = skill_names[skill_index]
        skill_path = skill_root / skill_name
        frontmatter, _body, _path = _load_skill(skill_path)
        metadata = frontmatter.get("metadata", {}) or {}
        results.append({
            "skill_name": skill_name,
            "similarity": round(float(similarity), 4),
            "description": (frontmatter.get("description") or "").strip(),
            "tags": metadata.get("tags", []),
            "version": metadata.get("version", 0),
        })

    return results


from xskill.skill.importer import import_skill_path


def import_skill(skill_dir: Path, source_path: Path) -> str:
    """把源目录纳入自有仓。返回第一个纳入的技能名（兼容旧 API）。"""
    require_management_write(skill_dir, operation='import')
    results = import_skill_path(skill_dir, source_path, install=False)
    if not results:
        raise FileNotFoundError(f"no skill imported from {source_path}")
    return results[0].name
