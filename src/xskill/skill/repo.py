"""
skill/repo.py — SkillRepo 集合 + 集合级 git 操作
═══════════════════════════════════════════════════
管理 ~/.xskill/skill/ 下所有 Skill 子目录。dict-like + iterable。
顶层 .git 已废弃，所有 git 操作走 <skill>/.git 子仓。

模块函数 ``list_skills`` / ``import_skill`` 是对整个 skill 仓库（集合）的
操作（原 skill_manager.py 的集合部分）。
"""

from __future__ import annotations

import logging
import pickle
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Iterator, Optional

import numpy

from xskill.skill.skill import Skill, _load_skill
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
    def rebuild_index(self) -> None:
        """重建 .skill_index.pkl（向量检索用）。

        显式给 ``rebuild_skill_index`` 传参，**不**走工具上下文初始化——后者
        会要求 ``data_dir`` / ``llm_client`` 等本路径用不到的字段（早先版本
        传 ``None`` 进去会触发 ``Path(None)`` TypeError，rebuild 直接挂）。
        """
        from xskill.config import get_config
        from xskill.utils.llm import create_embed_client
        embed_client = create_embed_client(get_config())
        rebuild_skill_index(skill_dir=self.root, embed_client=embed_client)

    # ─── 清空（rebuild --force 用）──────────────────────────────
    def wipe_all_skills(self) -> int:
        """删除仓里所有 skill 子目录（含各自 ``.git`` 子仓），返回删除个数。

        ``xskill rebuild --force`` 用：换强模型从零重建前先清空旧 skill。
        只删 skill 子目录（每个有 ``SKILL.md`` 或 ``.git`` 的子目录），保留
        仓根与 ``references`` / ``.skill_index.pkl`` 等非 skill 工件由 watcher
        后续自行重建。删完一并清掉过期索引。
        """
        n = 0
        if not self.root.is_dir():
            return 0
        for sub in sorted(self.root.iterdir()):
            if not sub.is_dir():
                continue
            if sub.name == "references":
                continue
            # 一个 skill 目录的判据：有 SKILL.md 或 .git 子仓（baby 态可能
            # 只有 .git 还没写 SKILL.md）。
            if (sub / "SKILL.md").is_file() or (sub / ".git").is_dir():
                shutil.rmtree(sub)
                n += 1
        # 索引已失效，删掉避免指向不存在的 skill
        idx = self.root / ".skill_index.pkl"
        if idx.is_file():
            idx.unlink()
        logger.info("wipe_all_skills: removed %d skill(s) under %s", n, self.root)
        return n

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
) -> None:
    """Rebuild ``<skill_dir>/.skill_index.pkl`` for skill semantic search.

    主特征 ``embeddings`` = **description-only** 向量（L2 归一）——不融合 tags/summary。
    辅助 ``atom_feats`` = 每个 skill 最近 ``last_n_atoms`` 个被路由 atom 摘要均值向量
    （独立存 ``atom_feats`` 字段，不并入 ``embeddings``）；无 atom 的 skill 该行为零向量、
    ``atom_feat_present`` 标 False。``atom_store_roots`` 给定时才算 atom_feat，否则全部
    不存在（present=False）。
    """
    skill_root = Path(skill_dir)
    if embed_client is None:
        raise RuntimeError("rebuild_skill_index: embed_client is required")

    from xskill.recommend.skill_feature import last_n_atom_summaries

    entries = []
    for skill_path in sorted(skill_root.iterdir()):
        if not skill_path.is_dir() or skill_path.name.startswith("."):
            continue
        frontmatter, _body, _path = _load_skill(skill_path)
        if not frontmatter:
            continue
        description = (frontmatter.get("description") or "").strip()
        entries.append((skill_path.name, description))

    if not entries:
        logger.info("no skills to index")
        return

    skill_names, descriptions = zip(*entries)
    descriptions = list(descriptions)

    # 主特征：description-only 向量
    embeddings = embed_client.encode_batch(descriptions)
    embeddings = numpy.asarray(embeddings, dtype=float)
    norms = numpy.linalg.norm(embeddings, axis=1, keepdims=True)
    norms[norms == 0] = 1
    embeddings = embeddings / norms

    # 辅助属性 atom_feat：每个 skill 最近 N atom 摘要均值（独立，不并入 embeddings）
    dim = embeddings.shape[1]
    atom_feats = numpy.zeros((len(skill_names), dim), dtype=float)
    atom_present = [False] * len(skill_names)
    for i, name in enumerate(skill_names):
        summaries = last_n_atom_summaries(
            name, atom_store_roots, n=last_n_atoms,
        )
        if not summaries:
            continue
        vecs = numpy.asarray(embed_client.encode_batch(summaries), dtype=float)
        mean = vecs.mean(axis=0)
        n = float(numpy.linalg.norm(mean))
        atom_feats[i] = mean / n if n > 0 else mean
        atom_present[i] = True

    index_data = {
        "skill_names": list(skill_names),
        "texts": descriptions,
        "embeddings": embeddings,
        "atom_feats": atom_feats,
        "atom_feat_present": atom_present,
        "method": "api",
    }

    index_path = skill_root / ".skill_index.pkl"
    with open(index_path, "wb") as index_file:
        pickle.dump(index_data, index_file)

    logger.info("skill index rebuilt: %d entries -> %s", len(skill_names), index_path)


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


def import_skill(skill_dir: Path, source_path: Path) -> str:
    """Copy a skill directory into ./skill/ and commit."""
    source = Path(source_path)
    if not source.is_dir():
        raise FileNotFoundError(f"source not found: {source}")

    name = source.name
    target = skill_dir / name
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(source, target)

    commit_changes(str(skill_dir), f"import skill: {name}")
    logger.info(f"imported: {name}")
    return name
