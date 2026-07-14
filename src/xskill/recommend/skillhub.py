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
import os
import pickle
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

import numpy as np

# dashboard 可读的三方 skill 向量缓存文件名（落在 skillhub 目录内，dot 前缀故不被
# ``rglob("SKILL.md")`` 扫到）。dashboard 端无 embed_client，只能读此缓存把用户用过
# 的三方 skill 画成散点 ▲（D6:不现算不造假）。结构对齐自产 ``.skill_index.pkl``。
INDEX_CACHE_NAME = ".skillhub_index.pkl"

# 三方 skill description 的向量复用缓存（与 dashboard 只读产物分开存）。
EMBED_CACHE_NAME = ".skillhub_embed_cache.pkl"

from xskill.canary import aggregate_ux_by_version, load_ux_scores
from xskill.config import skillhub_config
from xskill.skill.frontmatter import parse as fm_parse
from xskill.utils.embed_store import EmbedStore


def _normalize(v: np.ndarray) -> np.ndarray:
    n = float(np.linalg.norm(v))
    return v / n if n > 0 else v


def _safe_id_part(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip(".-")
    return safe or "skill"


def _path_hash(source_path: str) -> str:
    return hashlib.sha256(source_path.encode("utf-8")).hexdigest()[:12]


@dataclass(frozen=True)
class _SkillFileMemo:
    """一次稳定文件版本对应的解析结果与内容哈希。"""

    mtime_ns: int
    size: int
    content_sha: str
    entry: dict


@dataclass(frozen=True)
class _SkillHubSnapshot:
    """一次全树扫描结果；条目和名称索引只在实例内部使用。"""

    entries: tuple[dict, ...]
    by_name: dict[str, dict]


class SkillHub:
    """三方 skill 扫描器 + ux 查询。``enabled=False``（缺省）时为 no-op。"""

    def __init__(
        self, *, enabled: bool, hub_dir: Path | str, embed_client,
        scan_ttl_seconds: float = 5.0,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.enabled = bool(enabled)
        self.dir = Path(hub_dir)
        self.embed_client = embed_client
        self._scan_ttl_seconds = max(0.0, float(scan_ttl_seconds))
        self._clock = clock
        self._scan_lock = threading.Lock()
        self._snapshot: _SkillHubSnapshot | None = None
        self._snapshot_expires_at = 0.0
        self._snapshot_generation = 0
        self._file_memo: dict[Path, _SkillFileMemo] = {}

    @classmethod
    def from_config(cls, config: dict, embed_client) -> "SkillHub":
        cfg = skillhub_config(config)
        return cls(enabled=cfg["enabled"], hub_dir=cfg["dir"], embed_client=embed_client)

    @staticmethod
    def _signature(stat_result: os.stat_result) -> tuple[int, int]:
        return stat_result.st_mtime_ns, stat_result.st_size

    def _read_entry(
        self, md: Path, rel: str, stat_result: os.stat_result,
    ) -> _SkillFileMemo:
        """读取变化过的 SKILL.md；若读取期间被替换则按新版本再读一次。"""
        content = md.read_bytes()
        stable_stat = md.stat()
        if self._signature(stable_stat) != self._signature(stat_result):
            content = md.read_bytes()
            stable_stat = md.stat()
        fm, _body = fm_parse(content.decode("utf-8"))
        sub = md.parent
        raw_name = fm.get("name") or sub.name
        display_name = str(raw_name).strip() or sub.name
        desc = (fm.get("description") or "").strip()
        content_sha = hashlib.sha256(content).hexdigest()[:16]
        path_hash = _path_hash(rel)
        skill_id = f"{_safe_id_part(display_name)}@{path_hash}"
        entry = {
            "source": "skillhub",
            "name": skill_id,
            "skill_id": skill_id,
            "display_name": display_name,
            "source_path": rel,
            "path_hash": path_hash,
            "content_sha": content_sha,
            "description": desc,
            "path": sub,
        }
        return _SkillFileMemo(
            mtime_ns=stable_stat.st_mtime_ns,
            size=stable_stat.st_size,
            content_sha=content_sha,
            entry=entry,
        )

    @staticmethod
    def _build_snapshot(entries: list[dict]) -> _SkillHubSnapshot:
        """构造 O(1) 精确身份/唯一定义名查询索引。"""
        by_name: dict[str, dict] = {}
        display_counts: dict[str, int] = {}
        for entry in entries:
            for key in (entry["skill_id"], entry["name"], entry["source_path"]):
                by_name.setdefault(key, entry)
            display = entry["display_name"]
            display_counts[display] = display_counts.get(display, 0) + 1
        for entry in entries:
            display = entry["display_name"]
            if display_counts[display] == 1:
                by_name.setdefault(display, entry)
        return _SkillHubSnapshot(entries=tuple(entries), by_name=by_name)

    def _scan_entries(self) -> _SkillHubSnapshot:
        """遍历 skillhub 一次；隐藏目录不下钻，未变化文件不再读取。"""
        if not self.dir.is_dir():
            raise FileNotFoundError(
                f"skillhub.dir 不存在: {self.dir}（启用 skillhub 前请放置三方 skill）"
            )
        next_memo: dict[Path, _SkillFileMemo] = {}
        entries: list[dict] = []
        for root, dir_names, file_names in os.walk(self.dir, topdown=True):
            dir_names[:] = sorted(
                name for name in dir_names if not name.startswith(".")
            )
            if "SKILL.md" not in file_names:
                continue
            md = Path(root) / "SKILL.md"
            try:
                stat_result = md.stat()
                rel = md.parent.relative_to(self.dir).as_posix()
            except FileNotFoundError:
                continue
            except ValueError:
                continue
            memo = self._file_memo.get(md)
            if (
                memo is None
                or (memo.mtime_ns, memo.size) != self._signature(stat_result)
            ):
                try:
                    memo = self._read_entry(md, rel, stat_result)
                except FileNotFoundError:
                    continue
            next_memo[md] = memo
            entries.append(memo.entry)
        entries.sort(key=lambda entry: entry["source_path"])
        self._file_memo = next_memo
        return self._build_snapshot(entries)

    def _current_snapshot(self, *, force_refresh: bool = False) -> _SkillHubSnapshot:
        """返回短 TTL 快照；同一代并发过期请求只允许一个线程真扫。"""
        observed_generation = self._snapshot_generation
        now = self._clock()
        if (
            not force_refresh
            and self._snapshot is not None
            and now < self._snapshot_expires_at
        ):
            return self._snapshot
        with self._scan_lock:
            # 等锁期间已有线程完成刷新时直接共享该结果，包括 TTL=0/force 场景。
            if (
                self._snapshot is not None
                and self._snapshot_generation != observed_generation
            ):
                return self._snapshot
            now = self._clock()
            if (
                not force_refresh
                and self._snapshot is not None
                and now < self._snapshot_expires_at
            ):
                return self._snapshot
            snapshot = self._scan_entries()
            self._snapshot = snapshot
            self._snapshot_expires_at = self._clock() + self._scan_ttl_seconds
            self._snapshot_generation += 1
            return snapshot

    def _entries(
        self, *, include_vec: bool, require_description: bool,
        force_refresh: bool = False,
    ) -> list[dict]:
        if not self.enabled:
            return []
        snapshot = self._current_snapshot(force_refresh=force_refresh)
        entries = [dict(entry) for entry in snapshot.entries]
        if require_description:
            entries = [entry for entry in entries if entry["description"]]
        if include_vec and entries:
            # 批量 + 按内容哈希复用：重启/改一个 SKILL.md 不再全量重 embed。
            embed_store = EmbedStore(
                self.dir / EMBED_CACHE_NAME, self.embed_client,
            )
            vectors = embed_store.encode_cached(
                [entry["description"] for entry in entries],
            )
            embed_store.flush_pruned()
            for entry, vector in zip(entries, vectors):
                entry["vec"] = _normalize(np.asarray(vector, dtype=float))
        return entries

    def fingerprint(self) -> tuple[tuple[str, str, str], ...]:
        """当前 skillhub 内容指纹，用于推荐引擎缓存自动失效。

        只包含稳定身份、相对路径和内容版本；新增、删除、改内容都会改变该值。
        """
        return tuple(
            (entry["skill_id"], entry["source_path"], entry["content_sha"])
            for entry in self._entries(include_vec=False, require_description=True)
        )

    @property
    def index_cache_path(self) -> Path:
        """dashboard 可读的三方 skill 向量缓存路径（``<skillhub_dir>/.skillhub_index.pkl``）。"""
        return self.dir / INDEX_CACHE_NAME

    def index(self) -> list[dict]:
        """返回三方 skill 索引。禁用 → 空 list；启用但目录缺失 → raise。

        skill 以 ``skillhub.dir`` 下任意层级中包含 ``SKILL.md`` 的目录为单位。
        ``name`` / ``skill_id`` 是稳定分发身份，展示名放 ``display_name``。

        算好向量的同时落盘一份 dashboard 可读缓存（画像散点靠它画三方 skill ▲，
        dashboard 端无 embed_client 不能现算，D6）。只在启用且有三方 skill 时写。
        """
        entries = self._entries(include_vec=True, require_description=True)
        if entries:
            self._persist_index(entries)
        return entries

    def _persist_index(self, entries: list[dict]) -> None:
        """把已算好的三方 skill 向量落盘成 dashboard 可读缓存。

        结构对齐自产 ``.skill_index.pkl``（``skill_names`` / ``embeddings`` /
        ``model``），profile_viz 只读不写、读不到就不画（D6，不现算不造假）。
        """
        names = [entry["name"] for entry in entries]
        embeddings = np.vstack([np.asarray(entry["vec"], dtype=float) for entry in entries])
        data = {
            "skill_names": names,
            "embeddings": embeddings,
            "model": getattr(self.embed_client, "model", None),
        }
        with open(self.index_cache_path, "wb") as index_file:
            pickle.dump(data, index_file)

    def search(self, query: str, limit: int = 5) -> list[dict]:
        """无画像关键词检索：只按 display_name/description 文本匹配打分。

        与 ``SkillRecommendEngine`` 完全无关——不读画像、不算向量，结果只由
        查询词与 skill 元数据的字面匹配决定（`xskill search` 的语义契约）。
        """
        needle = query.strip().lower()
        if not needle:
            return []
        tokens = [token for token in re.split(r"[^0-9a-z一-鿿]+", needle) if token]
        scored: list[tuple[float, dict]] = []
        for entry in self._entries(include_vec=False, require_description=False):
            name_text = str(entry["display_name"]).lower()
            desc_text = str(entry["description"]).lower()
            score = 0.0
            if needle in name_text:
                score += 6.0
            elif needle in desc_text:
                score += 3.0
            for token in tokens:
                if token in name_text:
                    score += 2.0
                if token in desc_text:
                    score += 1.0
            if score > 0:
                scored.append((score, entry))
        scored.sort(key=lambda pair: (-pair[0], pair[1]["skill_id"]))
        return [entry for _score, entry in scored[:limit]]

    def entry(self, name: str, *, force_refresh: bool = False) -> dict | None:
        """按 skill_id / source_path / 唯一 display_name 找当前磁盘上的 skill。"""
        if not self.enabled:
            return None
        snapshot = self._current_snapshot(force_refresh=force_refresh)
        entry = snapshot.by_name.get(name)
        if entry is None or not (Path(entry["path"]) / "SKILL.md").is_file():
            return None
        return dict(entry)

    # ── §7 三方 skill ux 定位 / 版本 / 查询 ──────────────────────
    # 三方 skill 无 git → 版本号用 SKILL.md 内容 sha256 前 16 位；side 恒 "main"
    # （无 staging 分支）。ux 分落盘到 ``skillhub_dir/<name>/.ux_scores.jsonl``，
    # 由 ``runner`` 经 ``AtomCanary(skill_dir=skillhub_dir/<name>).append`` 写入；
    # 本类只负责读回。
    def skill_path(self, name: str) -> Path | None:
        """返回三方 skill 目录路径（含 ``SKILL.md``）；未启用 / 不存在 → None。"""
        entry = self.entry(name)
        if entry is None:
            return None
        sub = Path(entry["path"])
        if not sub.is_dir() or not (sub / "SKILL.md").is_file():
            return None
        return sub

    def content_sha(self, name: str) -> str | None:
        """三方 skill 版本号 = ``SKILL.md`` 内容 sha256 前 16 位。无 git → 内容哈希。"""
        entry = self.entry(name)
        if entry is None:
            return None
        return entry["content_sha"]

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
