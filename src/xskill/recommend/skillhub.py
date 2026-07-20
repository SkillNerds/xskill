"""skillhub.py — §6 三方 skill 目录扫描（CS 模式选配）+ §7 ux 查询

扫描 ``~/.xskill/skillhub_skills/`` 下的三方 ``SKILL.md``，按 **description** 向量化
（三方 skill 在本仓无被路由 atom，故无 ``atom_feat``），纳入 ``SkillRecommendEngine``
检索池。三方 skill 无 git 分支/灰度 → 仅参与相关性位，不进质量位/staging 达量。

被推荐 & 使用后的三方 skill 同样要被打 ux 分、可查询其 ux 分（与自有 skill 对齐）。
本模块提供查询接口（``ux_avg`` / ``recent_ux_scores``）与版本号（``content_sha``），
供 ``runner._score_atoms_for_traj`` 在自有 ``skill_dir`` 找不到该 skill 时回退定位。
"""
from __future__ import annotations

import concurrent.futures
import hashlib
import logging
import math
import os
import pickle
import re
import threading
import time
from collections import OrderedDict, deque
from datetime import datetime
from pathlib import Path

import numpy as np

from xskill.canary import aggregate_ux_by_version, load_ux_scores
from xskill.config import embedding_search_config, skillhub_config
from xskill.skill.frontmatter import parse as fm_parse
from xskill.utils.embed_store import EmbedStore

logger = logging.getLogger("xskill.skillhub")

# dashboard 可读的三方 skill 向量缓存文件名（落在 skillhub 目录内，dot 前缀故不被
# ``rglob("SKILL.md")`` 扫到）。dashboard 端无 embed_client，只能读此缓存把用户用过
# 的三方 skill 画成散点 ▲（D6:不现算不造假）。结构对齐自产 ``.skill_index.pkl``。
INDEX_CACHE_NAME = ".skillhub_index.pkl"

# 三方 skill description 的向量复用缓存（与 dashboard 只读产物分开存）。
EMBED_CACHE_NAME = ".skillhub_embed_cache.pkl"

BM25_K1 = 1.2
BM25_B = 0.75
SEARCH_RANK_CACHE_CAPACITY = 256
SEARCH_RESULT_CACHE_TTL_SECONDS = 10.0
RRF_RANK_CONSTANT = 60
QUERY_VECTOR_CACHE_CAPACITY = 256
QUERY_EMBED_FAILURE_COOLDOWN_SECONDS = 5.0
UX_AVG_CACHE_TTL_SECONDS = 60.0
SKILL_FILE_IO_RETRY_COOLDOWN_SECONDS = 30.0
INVALID_SKILL_RECHECK_COOLDOWN_SECONDS = 60.0
INVALID_SKILL_RECHECK_LIMIT = 32


class SkillFileSchemaError(TypeError):
    """A SKILL.md frontmatter field has a known-invalid type."""


def _normalize(v: np.ndarray) -> np.ndarray:
    n = float(np.linalg.norm(v))
    return v / n if n > 0 else v


def _tokenize(text: str) -> list[str]:
    """ASCII ``[a-z0-9]+`` 小写切词 + 中文 bigram（孤立单字自成一 token）。"""
    tokens: list[str] = []
    for chunk in re.findall(r"[a-z0-9]+|[一-鿿]+", text.lower()):
        if chunk.isascii():
            tokens.append(chunk)
        elif len(chunk) == 1:
            tokens.append(chunk)
        else:
            tokens.extend(chunk[offset:offset + 2] for offset in range(len(chunk) - 1))
    return tokens


def safe_id_part(value: str) -> str:
    """display_name → 可安全做目录/id 片段的字符串（跨模块公开:上传落盘也用它，
    必须与 skill_id 的生成保持同一实现，否则同名 skill 会落到两个目录）。"""
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip(".-")
    return safe or "skill"


def _path_hash(source_path: str) -> str:
    return hashlib.sha256(source_path.encode("utf-8")).hexdigest()[:12]


class SkillHub:
    """三方 skill 扫描器 + ux 查询。``enabled=False``（缺省）时为 no-op。"""

    def __init__(self, *, enabled: bool, hub_dir: Path | str, embed_client,
                 scan_ttl_seconds: float = 5.0,
                 search_max_embed: int = 2, search_timeout_s: float = 3.0):
        self.enabled = bool(enabled)
        self.dir = Path(hub_dir)
        self.embed_client = embed_client
        self.scan_ttl_seconds = float(scan_ttl_seconds)
        self.search_max_embed = int(search_max_embed)
        self.search_timeout_s = float(search_timeout_s)
        # L3 备忘录：SKILL.md 路径 →
        # (st_mtime_ns, st_size, sha16, display_name | None,
        #  description | None, retry_after)。display_name/description 均为 None
        # 表示该内容版本解码或字段校验失败；按限额定期重验可发现 metadata 未变
        # 的原位修复，sha16 则避免同一坏内容重复刷 warning。
        self._file_memo: dict[
            Path, tuple[int, int, str, str | None, str | None, float]
        ] = {}
        # stat/read 的 OSError 可能是瞬时故障，独立短时限频但不写入内容失败
        # 备忘录；冷却到期自动重试，成功或文件删除后清理。
        self._file_io_retry_after: dict[Path, float] = {}
        # 稳定内容错误的重验使用去重队列轮转；每轮只检查固定数量，既不集中
        # 重读全部坏文件，也不会让目录排序靠后的文件长期得不到重验。
        self._invalid_recheck_queue: deque[Path] = deque()
        self._invalid_recheck_paths: set[Path] = set()
        # L1 快照：require_description=False 的全集（不含 vec），single-flight 保护。
        self._scan_snapshot_entries: list[dict] | None = None
        self._scan_snapshot_expires_at: float = 0.0
        self._scan_lock = threading.Lock()
        # 派生结构随快照身份缓存：_snapshot 内容不变时列表身份不变，故 entry() /
        # fingerprint() 不变盘时零重建、不再每次调用深拷全量 N 条。single-flight
        # 由独立锁保护。strong_index：skill_id / source_path → entry(O(1) 定位)；
        # display_unique：display_name → entry(仅唯一时命中，兜底键)。
        self._derived_for: list[dict] | None = None
        self._strong_index: dict[str, dict] = {}
        self._display_unique: dict[str, dict | None] = {}
        self._fingerprint_cache: tuple[tuple[str, str, str], ...] = ()
        self._derived_lock = threading.Lock()
        # 混合检索索引：随 fingerprint 变化整体重建，在扫描 single-flight 之外自成一锁。
        self._search_index: dict | None = None
        self._search_index_lock = threading.Lock()
        # query embed 四护栏：非阻塞信号量 + 独立短超时线程 + fingerprint 感知 LRU
        # + 后端失败短时冷却。
        self._query_embed_semaphore = threading.Semaphore(max(self.search_max_embed, 0))
        self._query_embed_executor: concurrent.futures.ThreadPoolExecutor | None = None
        self._query_embed_executor_lock = threading.Lock()
        self._query_vector_cache: OrderedDict[str, tuple[tuple, np.ndarray]] = OrderedDict()
        self._query_vector_cache_lock = threading.Lock()
        self._query_embed_inflight: set[str] = set()
        self._corpus_embed_inflight: set[str] = set()
        self._query_embed_retry_after = 0.0
        self._ux_avg_cache: dict[
            tuple[str, int], tuple[float, str, float | None]
        ] = {}
        self._ux_avg_cache_lock = threading.Lock()

    @classmethod
    def from_config(cls, config: dict, embed_client) -> "SkillHub":
        cfg = skillhub_config(config)
        search_cfg = embedding_search_config(config)
        return cls(
            enabled=cfg["enabled"], hub_dir=cfg["dir"], embed_client=embed_client,
            search_max_embed=search_cfg["max_embed"],
            search_timeout_s=search_cfg["search_timeout_s"],
        )

    def _queue_invalid_recheck(self, skill_file: Path) -> None:
        if skill_file not in self._invalid_recheck_paths:
            self._invalid_recheck_paths.add(skill_file)
            self._invalid_recheck_queue.append(skill_file)

    def _read_skill_file(
        self, skill_file: Path, skill_dir: Path, relative_path: str,
        stat_result: os.stat_result, scan_time: float,
        memo: tuple[int, int, str, str | None, str | None, float] | None,
    ) -> bool:
        try:
            raw_bytes = skill_file.read_bytes()
        except OSError as scan_error:
            self._file_io_retry_after[skill_file] = (
                scan_time + SKILL_FILE_IO_RETRY_COOLDOWN_SECONDS
            )
            logger.warning(
                "skillhub scan skipped SKILL.md path=%s error_type=%s",
                f"{relative_path}/SKILL.md" if relative_path != "." else "SKILL.md",
                type(scan_error).__name__,
            )
            return False
        self._file_io_retry_after.pop(skill_file, None)
        refreshed_content_sha = hashlib.sha256(raw_bytes).hexdigest()[:16]
        previous_invalid_sha = (
            memo[2] if memo is not None and memo[3] is None else None
        )
        try:
            decoded_text = raw_bytes.decode("utf-8", errors="strict")
        except UnicodeDecodeError as decode_error:
            self._file_memo[skill_file] = (
                stat_result.st_mtime_ns, stat_result.st_size,
                refreshed_content_sha, None, None,
                scan_time + INVALID_SKILL_RECHECK_COOLDOWN_SECONDS,
            )
            self._queue_invalid_recheck(skill_file)
            if previous_invalid_sha != refreshed_content_sha:
                invalid_bytes = raw_bytes[
                    decode_error.start:min(
                        len(raw_bytes), decode_error.start + 4,
                    )
                ].hex()
                logger.warning(
                    "skillhub scan skipped invalid UTF-8 SKILL.md "
                    "path=%s offset=%d bytes=%s",
                    f"{relative_path}/SKILL.md"
                    if relative_path != "." else "SKILL.md",
                    decode_error.start, invalid_bytes,
                )
            return False
        frontmatter, _body = fm_parse(decoded_text)
        raw_name = frontmatter.get("name", skill_dir.name)
        raw_description = frontmatter.get("description", "")
        try:
            if not isinstance(raw_name, str):
                raise SkillFileSchemaError(
                    "frontmatter name must be a string"
                )
            if not isinstance(raw_description, str):
                raise SkillFileSchemaError(
                    "frontmatter description must be a string"
                )
        except SkillFileSchemaError as schema_error:
            self._file_memo[skill_file] = (
                stat_result.st_mtime_ns, stat_result.st_size,
                refreshed_content_sha, None, None,
                scan_time + INVALID_SKILL_RECHECK_COOLDOWN_SECONDS,
            )
            self._queue_invalid_recheck(skill_file)
            if previous_invalid_sha != refreshed_content_sha:
                logger.warning(
                    "skillhub scan skipped SKILL.md path=%s error_type=%s",
                    f"{relative_path}/SKILL.md"
                    if relative_path != "." else "SKILL.md",
                    type(schema_error).__name__,
                )
            return False
        display_name = raw_name.strip() or skill_dir.name
        self._file_memo[skill_file] = (
            stat_result.st_mtime_ns, stat_result.st_size,
            refreshed_content_sha, display_name, raw_description.strip(), 0.0,
        )
        self._invalid_recheck_paths.discard(skill_file)
        return True

    def _recheck_invalid_files(self, scan_time: float) -> None:
        checks_remaining = min(
            INVALID_SKILL_RECHECK_LIMIT, len(self._invalid_recheck_queue),
        )
        for _index in range(checks_remaining):
            skill_file = self._invalid_recheck_queue.popleft()
            if skill_file not in self._invalid_recheck_paths:
                continue
            self._invalid_recheck_paths.remove(skill_file)
            try:
                memo = self._file_memo.get(skill_file)
                if memo is None or memo[3] is not None:
                    continue
                if scan_time < memo[5]:
                    continue
                io_retry_after = self._file_io_retry_after.get(skill_file)
                if io_retry_after is not None and scan_time < io_retry_after:
                    continue
                skill_dir = skill_file.parent
                try:
                    relative_path = skill_dir.relative_to(self.dir).as_posix()
                except ValueError:
                    self._file_memo.pop(skill_file, None)
                    self._file_io_retry_after.pop(skill_file, None)
                    continue
                try:
                    stat_result = skill_file.stat()
                except FileNotFoundError:
                    self._file_memo.pop(skill_file, None)
                    self._file_io_retry_after.pop(skill_file, None)
                    continue
                except OSError as scan_error:
                    self._file_io_retry_after[skill_file] = (
                        scan_time + SKILL_FILE_IO_RETRY_COOLDOWN_SECONDS
                    )
                    logger.warning(
                        "skillhub scan skipped SKILL.md path=%s error_type=%s",
                        f"{relative_path}/SKILL.md"
                        if relative_path != "." else "SKILL.md",
                        type(scan_error).__name__,
                    )
                    continue
                self._file_io_retry_after.pop(skill_file, None)
                self._read_skill_file(
                    skill_file, skill_dir, relative_path,
                    stat_result, scan_time, memo,
                )
            finally:
                refreshed_memo = self._file_memo.get(skill_file)
                if refreshed_memo is not None and refreshed_memo[3] is None:
                    self._queue_invalid_recheck(skill_file)

    def _walk_snapshot(self) -> list[dict]:
        """L2 剪枝遍历 + L3 备忘录，产出 require_description=False 的全集（无 vec）。"""
        entries: list[dict] = []
        seen_paths: set[Path] = set()
        scan_time = time.monotonic()
        self._recheck_invalid_files(scan_time)
        for dirpath, dirnames, filenames in os.walk(self.dir):
            dirnames[:] = [name for name in dirnames if not name.startswith(".")]
            if "SKILL.md" not in filenames:
                continue
            skill_dir = Path(dirpath)
            skill_file = skill_dir / "SKILL.md"
            try:
                relative_path = skill_dir.relative_to(self.dir).as_posix()
            except ValueError:
                continue
            if any(part.startswith(".") for part in Path(relative_path).parts):
                continue
            seen_paths.add(skill_file)
            io_retry_after = self._file_io_retry_after.get(skill_file)
            if io_retry_after is not None and scan_time < io_retry_after:
                continue
            try:
                stat_result = skill_file.stat()
            except OSError as scan_error:
                self._file_io_retry_after[skill_file] = (
                    scan_time + SKILL_FILE_IO_RETRY_COOLDOWN_SECONDS
                )
                logger.warning(
                    "skillhub scan skipped SKILL.md path=%s error_type=%s",
                    f"{relative_path}/SKILL.md"
                    if relative_path != "." else "SKILL.md",
                    type(scan_error).__name__,
                )
                continue
            self._file_io_retry_after.pop(skill_file, None)
            memo = self._file_memo.get(skill_file)
            memo_matches_metadata = (
                memo is not None
                and memo[0] == stat_result.st_mtime_ns
                and memo[1] == stat_result.st_size
            )
            if memo_matches_metadata and memo[3] is None:
                continue
            if not memo_matches_metadata:
                if not self._read_skill_file(
                    skill_file, skill_dir, relative_path,
                    stat_result, scan_time, memo,
                ):
                    continue
                memo = self._file_memo[skill_file]
            (
                _mtime, _size, content_sha, display_name, description,
                _retry_after,
            ) = memo
            path_hash = _path_hash(relative_path)
            skill_id = f"{safe_id_part(display_name)}@{path_hash}"
            entries.append({
                "source": "skillhub",
                "name": skill_id,
                "skill_id": skill_id,
                "display_name": display_name,
                "source_path": relative_path,
                "path_hash": path_hash,
                "content_sha": content_sha,
                "description": description,
                "path": skill_dir,
            })
        stale_memo_paths = [
            path for path in self._file_memo if path not in seen_paths
        ]
        for stale_path in stale_memo_paths:
            del self._file_memo[stale_path]
            self._invalid_recheck_paths.discard(stale_path)
        for stale_path in [
            path for path in self._file_io_retry_after if path not in seen_paths
        ]:
            del self._file_io_retry_after[stale_path]
        if len(self._invalid_recheck_queue) != len(self._invalid_recheck_paths):
            self._invalid_recheck_queue = deque(
                path for path in self._invalid_recheck_queue
                if path in self._invalid_recheck_paths
            )
        entries.sort(key=lambda entry: entry["source_path"])
        return entries

    def _snapshot(self) -> list[dict]:
        """L1 TTL 快照 + single-flight：过期时只有一个线程真扫盘。"""
        now = time.monotonic()
        if self._scan_snapshot_entries is not None and now < self._scan_snapshot_expires_at:
            return self._scan_snapshot_entries
        with self._scan_lock:
            now = time.monotonic()
            if self._scan_snapshot_entries is not None and now < self._scan_snapshot_expires_at:
                return self._scan_snapshot_entries
            refreshed_entries = self._walk_snapshot()
            # TTL 到期只代表需要重新核对磁盘。内容未变化时保留原列表身份，
            # 让依赖该快照的搜索索引和缓存继续有效。
            if (
                self._scan_snapshot_entries is None
                or self._scan_snapshot_entries != refreshed_entries
            ):
                self._scan_snapshot_entries = refreshed_entries
            self._scan_snapshot_expires_at = time.monotonic() + self.scan_ttl_seconds
            return self._scan_snapshot_entries

    def _entries(self, *, include_vec: bool, require_description: bool) -> list[dict]:
        if not self.enabled:
            return []
        if not self.dir.is_dir():
            raise FileNotFoundError(
                f"skillhub.dir 不存在: {self.dir}（启用 skillhub 前请放置三方 skill）"
            )
        entries = [
            dict(entry) for entry in self._snapshot()
            if not require_description or entry["description"]
        ]
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

    def _ensure_derived(self) -> None:
        """按快照身份缓存派生结构：``strong_index`` / ``display_unique`` / 指纹。

        ``_snapshot`` 内容不变时列表身份不变，故不变盘时零重建；1 万 skill 下把
        ``entry()`` 从"每次深拷+线性扫全量"降到 O(1)，把 ``fingerprint()`` 从
        "每请求重建万元素 tuple"降到"命中即返回同一 tuple 对象"。
        """
        if not self.enabled:
            self._strong_index = {}
            self._display_unique = {}
            self._fingerprint_cache = ()
            self._derived_for = None
            return
        if not self.dir.is_dir():
            raise FileNotFoundError(
                f"skillhub.dir 不存在: {self.dir}（启用 skillhub 前请放置三方 skill）"
            )
        snapshot = self._snapshot()
        if self._derived_for is snapshot:
            return
        with self._derived_lock:
            if self._derived_for is snapshot:
                return
            strong_index: dict[str, dict] = {}
            display_unique: dict[str, dict | None] = {}
            fingerprint_items: list[tuple[str, str, str]] = []
            for entry in snapshot:
                # skill_id == entry["name"]（见 _walk_snapshot），故 skill_id 键
                # 同时覆盖 name 匹配；source_path 相对路径亦唯一。
                strong_index[entry["skill_id"]] = entry
                strong_index[entry["source_path"]] = entry
                display_name = entry["display_name"]
                display_unique[display_name] = (
                    entry if display_name not in display_unique else None
                )
                if entry["description"]:
                    fingerprint_items.append(
                        (entry["skill_id"], entry["source_path"], entry["content_sha"])
                    )
            self._strong_index = strong_index
            self._display_unique = display_unique
            self._fingerprint_cache = tuple(fingerprint_items)
            self._derived_for = snapshot

    def fingerprint(self) -> tuple[tuple[str, str, str], ...]:
        """当前 skillhub 内容指纹，用于推荐引擎缓存自动失效。

        只包含稳定身份、相对路径和内容版本；新增、删除、改内容都会改变该值。
        随快照身份缓存：不变盘时返回同一 tuple 对象，让调用方可用 ``is`` 做 O(1)
        失效判断，不再每次重建万元素 tuple。
        """
        self._ensure_derived()
        return self._fingerprint_cache

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
        """BM25 关键词 + 语义向量 RRF 融合检索（无画像，query↔description）。

        与 ``SkillRecommendEngine`` 完全无关——不读画像。语义通道只读 ``EmbedStore``
        已缓存向量、绝不现算 corpus；query embed 受并发信号量 + 短超时 + LRU 三护栏
        约束，慢/挂的 embed API 最多让语义位失效，请求自然退化为纯 BM25。
        """
        normalized_query = query.strip().lower()
        if not normalized_query:
            return []
        index_bundle = self._search_index_bundle()
        entries = index_bundle["entries"]
        if not entries:
            return []
        query_tokens = _tokenize(normalized_query)
        bm25_rank_by_index = self._bm25_ranks(index_bundle, query_tokens)
        semantic_rank_by_index = self._semantic_ranks(
            index_bundle, normalized_query,
        )
        result_cache_key = (
            normalized_query, int(limit), bool(semantic_rank_by_index),
        )
        cached_results = self._cached_search_results(
            index_bundle, result_cache_key,
        )
        if cached_results is not None:
            return cached_results
        score_groups = self._fusion_score_groups(
            index_bundle, normalized_query,
            bm25_rank_by_index, semantic_rank_by_index,
        )
        ordered_indices = self._rank_score_groups(
            score_groups, entries, limit,
        )
        results: list[dict] = []
        for entry_index in ordered_indices:
            result_entry = dict(entries[entry_index])
            result_entry["bm25_rank"] = bm25_rank_by_index.get(entry_index)
            result_entry["semantic_rank"] = semantic_rank_by_index.get(entry_index)
            result_entry["ux_avg"] = self._ux_avg_for_entry(result_entry)
            results.append(result_entry)
        self._cache_search_results(index_bundle, result_cache_key, results)
        return results

    def cached_search(self, query: str, limit: int = 5) -> list[dict] | None:
        """Return a current final-result cache hit without scanning or ranking.

        This method is safe to call on the event-loop thread: it only reads the
        current snapshot/index identities and a bounded in-memory cache.  A
        stale snapshot or cache miss returns ``None`` so the caller can offload
        :meth:`search` to a worker thread.
        """
        normalized_query = query.strip().lower()
        if not normalized_query:
            return []
        now = time.monotonic()
        index_bundle = self._search_index
        if (
            index_bundle is None
            or index_bundle["snapshot_entries"] is not self._scan_snapshot_entries
            or now >= self._scan_snapshot_expires_at
        ):
            return None
        # Prefer the cached hybrid result when both a prior degraded result
        # and a later semantic result exist for the same query.
        for has_semantic_rank in (True, False):
            cached_results = self._cached_search_results(
                index_bundle,
                (normalized_query, int(limit), has_semantic_rank),
            )
            if cached_results is not None:
                return cached_results
        return None

    @staticmethod
    def _cached_search_results(
        index_bundle: dict, cache_key: tuple[str, int, bool],
    ) -> list[dict] | None:
        """读取短 TTL 最终结果缓存并返回浅拷贝，调用方修改不会污染缓存。"""
        cache = index_bundle["search_result_cache"]
        cache_lock = index_bundle["search_result_cache_lock"]
        with cache_lock:
            cached = cache.get(cache_key)
            if cached is None:
                return None
            expires_at, cached_results = cached
            if time.monotonic() >= expires_at:
                del cache[cache_key]
                return None
            cache.move_to_end(cache_key)
            return [dict(result) for result in cached_results]

    @staticmethod
    def _cache_search_results(
        index_bundle: dict, cache_key: tuple[str, int, bool], results: list[dict],
    ) -> None:
        """短时缓存最终排序；索引 bundle 替换时缓存自然整体失效。"""
        cache = index_bundle["search_result_cache"]
        cache_lock = index_bundle["search_result_cache_lock"]
        cached_results = tuple(dict(result) for result in results)
        with cache_lock:
            cache[cache_key] = (
                time.monotonic() + SEARCH_RESULT_CACHE_TTL_SECONDS,
                cached_results,
            )
            cache.move_to_end(cache_key)
            while len(cache) > SEARCH_RANK_CACHE_CAPACITY:
                cache.popitem(last=False)

    def _fuse_and_rank(self, reciprocal_scores: dict[int, float],
                       entries: list[dict], limit: int) -> list[int]:
        """RRF 排序，同分 tie-break ux_avg 降序（None 最低）再 skill_id；ux 只在同分组内算。"""
        return self._rank_score_groups(
            self._score_groups(reciprocal_scores, entries), entries, limit,
        )

    def _fusion_score_groups(
        self, index_bundle: dict, normalized_query: str,
        bm25_rank_by_index: dict[int, int],
        semantic_rank_by_index: dict[int, int],
    ) -> tuple[tuple[int, ...], ...]:
        """复用缓存热查询的 RRF 分组；UX 仍在每次请求排序时读取。"""
        cache = index_bundle["fusion_group_cache"]
        cache_lock = index_bundle["fusion_group_cache_lock"]
        # 语义通道临时降级时不能复用成功语义请求的分组，否则 match 字段会不一致。
        if semantic_rank_by_index:
            with cache_lock:
                cached = cache.get(normalized_query)
                if cached is not None:
                    cache.move_to_end(normalized_query)
                    return cached

        reciprocal_scores: dict[int, float] = {}
        for rank_by_index in (bm25_rank_by_index, semantic_rank_by_index):
            for entry_index, rank in rank_by_index.items():
                reciprocal_scores[entry_index] = (
                    reciprocal_scores.get(entry_index, 0.0)
                    + 1.0 / (RRF_RANK_CONSTANT + rank)
                )
        score_groups = self._score_groups(reciprocal_scores, index_bundle["entries"])
        if not semantic_rank_by_index:
            return score_groups
        with cache_lock:
            cached = cache.get(normalized_query)
            if cached is not None:
                cache.move_to_end(normalized_query)
                return cached
            cache[normalized_query] = score_groups
            while len(cache) > SEARCH_RANK_CACHE_CAPACITY:
                cache.popitem(last=False)
        return score_groups

    @staticmethod
    def _score_groups(
        reciprocal_scores: dict[int, float], entries: list[dict],
    ) -> tuple[tuple[int, ...], ...]:
        """按 RRF 分数和 skill_id 排序，保留同分组供实时 UX 排序。"""
        by_score_then_id = sorted(
            reciprocal_scores,
            key=lambda entry_index: (
                -reciprocal_scores[entry_index], entries[entry_index]["skill_id"],
            ),
        )
        score_groups: list[tuple[int, ...]] = []
        run_start = 0
        while run_start < len(by_score_then_id):
            run_end = run_start
            while (run_end < len(by_score_then_id)
                   and reciprocal_scores[by_score_then_id[run_end]]
                   == reciprocal_scores[by_score_then_id[run_start]]):
                run_end += 1
            score_groups.append(tuple(by_score_then_id[run_start:run_end]))
            run_start = run_end
        return tuple(score_groups)

    def _rank_score_groups(
        self, score_groups: tuple[tuple[int, ...], ...],
        entries: list[dict], limit: int,
    ) -> list[int]:
        """对同分组实时应用 UX tie-break，避免缓存陈旧的 UX 排名。"""
        ordered_indices: list[int] = []
        for score_group in score_groups:
            run = list(score_group)
            if len(run) > 1:
                run.sort(key=lambda entry_index: (
                    -self._ux_sort_key(entries[entry_index]),
                    entries[entry_index]["skill_id"],
                ))
            ordered_indices.extend(run)
            if len(ordered_indices) >= limit:
                break
        return ordered_indices[:limit]

    def _ux_sort_key(self, entry: dict) -> float:
        """ux_avg 用作 tie-break 的数值键；无评分视为最低（-inf）。"""
        if "path" in entry and "content_sha" in entry:
            ux_value = self._ux_avg_for_entry(entry)
        else:
            # 保留给测试和第三方调用方构造的最小 entry。
            ux_value = self.ux_avg(entry["skill_id"])
        return ux_value if ux_value is not None else float("-inf")

    def _bm25_ranks(self, index_bundle: dict, query_tokens: list[str]) -> dict[int, int]:
        """对命中文档按 BM25 分数降序给出 1-based 排名（k1=1.2, b=0.75）。"""
        cache_key = tuple(sorted(set(query_tokens)))
        rank_cache = index_bundle["bm25_rank_cache"]
        rank_cache_lock = index_bundle["bm25_rank_cache_lock"]
        with rank_cache_lock:
            cached = rank_cache.get(cache_key)
            if cached is not None:
                rank_cache.move_to_end(cache_key)
        if cached is not None:
            return cached

        entries = index_bundle["entries"]
        term_postings = index_bundle["term_postings"]
        document_frequencies = index_bundle["document_frequencies"]
        document_lengths = index_bundle["document_lengths"]
        average_document_length = index_bundle["average_document_length"]
        document_count = len(entries)
        scores: dict[int, float] = {}
        for token in cache_key:
            document_frequency = document_frequencies.get(token, 0)
            if document_frequency == 0 or average_document_length == 0:
                continue
            inverse_document_frequency = math.log(
                1 + (document_count - document_frequency + 0.5)
                / (document_frequency + 0.5)
            )
            for entry_index, token_frequency in term_postings[token]:
                denominator = token_frequency + BM25_K1 * (
                    1 - BM25_B
                    + BM25_B * document_lengths[entry_index] / average_document_length
                )
                scores[entry_index] = scores.get(entry_index, 0.0) + (
                    inverse_document_frequency
                    * token_frequency * (BM25_K1 + 1) / denominator
                )
        ranked = sorted(scores, key=lambda entry_index: (
            -scores[entry_index], entries[entry_index]["skill_id"],
        ))
        ranks = {
            entry_index: rank
            for rank, entry_index in enumerate(ranked, start=1)
        }
        with rank_cache_lock:
            cached = rank_cache.get(cache_key)
            if cached is not None:
                rank_cache.move_to_end(cache_key)
                return cached
            rank_cache[cache_key] = ranks
            while len(rank_cache) > SEARCH_RANK_CACHE_CAPACITY:
                rank_cache.popitem(last=False)
            return ranks

    def _semantic_ranks(self, index_bundle: dict,
                        normalized_query: str) -> dict[int, int]:
        """对有缓存向量的文档按 query↔description cosine 降序给出 1-based 排名。"""
        rank_cache = index_bundle["semantic_rank_cache"]
        rank_cache_lock = index_bundle["semantic_rank_cache_lock"]
        with rank_cache_lock:
            cached = rank_cache.get(normalized_query)
            if cached is not None:
                rank_cache.move_to_end(normalized_query)
                return cached

        present_indices = index_bundle["vector_present_indices"]
        corpus_matrix = index_bundle["corpus_matrix"]
        if not present_indices:
            return {}
        query_vector = self._embed_query(normalized_query, index_bundle["fingerprint"])
        if query_vector is None or query_vector.shape[0] != corpus_matrix.shape[1]:
            return {}
        similarities = corpus_matrix @ query_vector
        order = np.argsort(-similarities)
        ranks = {
            present_indices[position]: rank
            for rank, position in enumerate(order.tolist(), start=1)
        }
        with rank_cache_lock:
            cached = rank_cache.get(normalized_query)
            if cached is not None:
                rank_cache.move_to_end(normalized_query)
                return cached
            rank_cache[normalized_query] = ranks
            while len(rank_cache) > SEARCH_RANK_CACHE_CAPACITY:
                rank_cache.popitem(last=False)
        return ranks

    def _embed_query(self, normalized_query: str,
                     fingerprint: tuple) -> np.ndarray | None:
        """query 向量护栏：LRU、并发上限、短超时，以及后端失败短时冷却。"""
        if self.embed_client is None or self.search_max_embed <= 0:
            return None
        with self._query_vector_cache_lock:
            cached = self._query_vector_cache.get(normalized_query)
            if cached is not None and cached[0] == fingerprint:
                self._query_vector_cache.move_to_end(normalized_query)
                return cached[1]
            elif cached is not None:
                del self._query_vector_cache[normalized_query]
            if time.monotonic() < self._query_embed_retry_after:
                return None
            if normalized_query in self._query_embed_inflight:
                return None
        if not self._query_embed_semaphore.acquire(blocking=False):
            return None
        with self._query_vector_cache_lock:
            # The cache or in-flight set may have changed between releasing the
            # cache lock and acquiring the global embedding slot.
            cached = self._query_vector_cache.get(normalized_query)
            if cached is not None and cached[0] == fingerprint:
                self._query_vector_cache.move_to_end(normalized_query)
                self._query_embed_semaphore.release()
                return cached[1]
            if normalized_query in self._query_embed_inflight:
                self._query_embed_semaphore.release()
                return None
            self._query_embed_inflight.add(normalized_query)
        # 超时后 embed 线程仍在跑（同步 60s httpx），信号量由该线程结束时释放，泄漏受 max_embed 约束。
        try:
            future = self._query_embed_pool().submit(
                self._encode_and_cache_query, normalized_query, fingerprint,
            )
        except Exception:
            with self._query_vector_cache_lock:
                self._query_embed_inflight.discard(normalized_query)
                self._query_embed_retry_after = max(
                    self._query_embed_retry_after,
                    time.monotonic() + QUERY_EMBED_FAILURE_COOLDOWN_SECONDS,
                )
            self._query_embed_semaphore.release()
            logger.warning("skillhub semantic search degraded to BM25: submit failed",
                           exc_info=True)
            return None
        try:
            query_vector = future.result(timeout=self.search_timeout_s)
            with self._query_vector_cache_lock:
                self._query_embed_retry_after = 0.0
            return query_vector
        except Exception as embed_error:  # 超时/API 异常只降级语义位，不冒泡到 search
            with self._query_vector_cache_lock:
                self._query_embed_retry_after = max(
                    self._query_embed_retry_after,
                    time.monotonic() + QUERY_EMBED_FAILURE_COOLDOWN_SECONDS,
                )
            logger.warning(
                "skillhub semantic search degraded to BM25: query embedding %s: %s",
                type(embed_error).__name__, embed_error,
            )
            return None

    def _query_embed_pool(self) -> concurrent.futures.ThreadPoolExecutor:
        if self._query_embed_executor is None:
            with self._query_embed_executor_lock:
                if self._query_embed_executor is None:
                    self._query_embed_executor = concurrent.futures.ThreadPoolExecutor(
                        max_workers=self.search_max_embed,
                        thread_name_prefix="skillhub-query-embed",
                    )
        return self._query_embed_executor

    def _encode_and_cache_query(self, normalized_query: str,
                                fingerprint: tuple) -> np.ndarray:
        """在信号量保护下发一次 query embed，成功写 LRU，finally 必释放信号量。"""
        try:
            query_vector = _normalize(
                np.asarray(self.embed_client.encode(normalized_query), dtype=float)
            )
            with self._query_vector_cache_lock:
                self._query_vector_cache[normalized_query] = (fingerprint, query_vector)
                self._query_vector_cache.move_to_end(normalized_query)
                while len(self._query_vector_cache) > QUERY_VECTOR_CACHE_CAPACITY:
                    self._query_vector_cache.popitem(last=False)
            return query_vector
        finally:
            with self._query_vector_cache_lock:
                self._query_embed_inflight.discard(normalized_query)
            self._query_embed_semaphore.release()

    def backfill_description_embedding(self, description: str) -> bool:
        """Best-effort, bounded corpus-vector backfill used after an upload.

        It shares the same configured embedding slots and executor as query
        embeddings. If all slots are busy, the upload stays successful and the
        skill remains available through BM25 until the normal index refresh
        fills its vector.
        """
        if (not description or self.embed_client is None
                or self.search_max_embed <= 0):
            return False
        description_key = hashlib.sha256(description.encode("utf-8")).hexdigest()
        with self._query_vector_cache_lock:
            if description_key in self._corpus_embed_inflight:
                return True
        if not self._query_embed_semaphore.acquire(blocking=False):
            return False
        with self._query_vector_cache_lock:
            if description_key in self._corpus_embed_inflight:
                self._query_embed_semaphore.release()
                return True
            self._corpus_embed_inflight.add(description_key)
        try:
            self._query_embed_pool().submit(
                self._encode_corpus_description, description, description_key,
            )
        except Exception:
            with self._query_vector_cache_lock:
                self._corpus_embed_inflight.discard(description_key)
            self._query_embed_semaphore.release()
            logger.warning("skillhub upload embedding backfill submit failed",
                           exc_info=True)
            return False
        return True

    def _encode_corpus_description(self, description: str,
                                   description_key: str) -> None:
        try:
            EmbedStore(self.dir / EMBED_CACHE_NAME, self.embed_client).encode_cached(
                [description],
            )
            # A search may have built an index while this vector was absent.
            # Rebuild lazily on the next request so the new vector is visible.
            with self._search_index_lock:
                self._search_index = None
        except Exception as embed_error:
            logger.warning("skill_hub upload embed backfill failed: %s", embed_error)
        finally:
            with self._query_vector_cache_lock:
                self._corpus_embed_inflight.discard(description_key)
            self._query_embed_semaphore.release()

    def _search_index_bundle(self) -> dict:
        """随内容 fingerprint 变化整体重建 BM25 倒排 + 只读 corpus 向量，几百 skill <10ms。"""
        if not self.dir.is_dir():
            raise FileNotFoundError(
                f"skillhub.dir 不存在: {self.dir}（启用 skillhub 前请放置三方 skill）"
            )
        snapshot_entries = self._snapshot()
        bundle = self._search_index
        if bundle is not None and bundle["snapshot_entries"] is snapshot_entries:
            return bundle
        with self._search_index_lock:
            bundle = self._search_index
            if bundle is not None and bundle["snapshot_entries"] is snapshot_entries:
                return bundle
            entries = [dict(entry) for entry in snapshot_entries]
            current_fingerprint = tuple(
                (entry["skill_id"], entry["source_path"], entry["content_sha"])
                for entry in entries
            )
            rebuilt_bundle = self._build_search_index(entries, current_fingerprint)
            rebuilt_bundle["snapshot_entries"] = snapshot_entries
            self._search_index = rebuilt_bundle
            return rebuilt_bundle

    def _build_search_index(self, entries: list[dict], fingerprint: tuple) -> dict:
        term_frequencies: list[dict[str, int]] = []
        term_postings: dict[str, list[tuple[int, int]]] = {}
        document_frequencies: dict[str, int] = {}
        for entry_index, entry in enumerate(entries):
            frequency_map: dict[str, int] = {}
            for token in _tokenize(str(entry["display_name"])):
                frequency_map[token] = frequency_map.get(token, 0) + 2
            for token in _tokenize(str(entry["description"])):
                frequency_map[token] = frequency_map.get(token, 0) + 1
            term_frequencies.append(frequency_map)
            for token, token_frequency in frequency_map.items():
                document_frequencies[token] = document_frequencies.get(token, 0) + 1
                term_postings.setdefault(token, []).append(
                    (entry_index, token_frequency)
                )
        document_lengths = [sum(frequency_map.values()) for frequency_map in term_frequencies]
        average_document_length = (
            sum(document_lengths) / len(document_lengths) if document_lengths else 0.0
        )
        vector_present_indices, corpus_matrix = self._read_cached_corpus_vectors(entries)
        return {
            "fingerprint": fingerprint,
            "entries": entries,
            "term_postings": term_postings,
            "document_frequencies": document_frequencies,
            "document_lengths": document_lengths,
            "average_document_length": average_document_length,
            "bm25_rank_cache": OrderedDict(),
            "bm25_rank_cache_lock": threading.Lock(),
            "semantic_rank_cache": OrderedDict(),
            "semantic_rank_cache_lock": threading.Lock(),
            "fusion_group_cache": OrderedDict(),
            "fusion_group_cache_lock": threading.Lock(),
            "search_result_cache": OrderedDict(),
            "search_result_cache_lock": threading.Lock(),
            "vector_present_indices": vector_present_indices,
            "corpus_matrix": corpus_matrix,
        }

    def _read_cached_corpus_vectors(self, entries: list[dict]) -> tuple[list[int], np.ndarray]:
        """只读 EmbedStore 已缓存向量（cached_vectors，绝不现算）；缺向量的 entry 本轮只进 BM25。"""
        if self.embed_client is None or self.search_max_embed <= 0 or not entries:
            return [], np.empty((0, 0), dtype=float)
        embed_store = EmbedStore(self.dir / EMBED_CACHE_NAME, self.embed_client)
        cached = embed_store.cached_vectors([entry["description"] for entry in entries])
        present_indices = [
            entry_index for entry_index, vector in enumerate(cached) if vector is not None
        ]
        if not present_indices:
            return [], np.empty((0, 0), dtype=float)
        corpus_matrix = np.vstack([
            _normalize(np.asarray(cached[entry_index], dtype=float))
            for entry_index in present_indices
        ])
        return present_indices, corpus_matrix

    def entry(self, name: str, *, force_refresh: bool = False) -> dict | None:
        """按 skill_id / source_path / 唯一 display_name 找当前磁盘上的 skill。

        ``force_refresh=True`` 跳过 TTL 立即重扫（single-flight 内），供上传后即时可见。
        """
        if not self.enabled:
            return None
        if force_refresh:
            # Upload replacement must bypass both the L1 TTL snapshot and the
            # L3 mtime/size memo. Preserved timestamps or coarse filesystems can
            # otherwise hide a same-size SKILL.md replacement.
            with self._scan_lock:
                self._scan_snapshot_expires_at = 0.0
                self._file_memo.clear()
                self._file_io_retry_after.clear()
                self._invalid_recheck_queue.clear()
                self._invalid_recheck_paths.clear()
        # strong_index / display_unique 做 O(1) 定位，语义与旧线性扫等价：
        # 强键(skill_id / name / source_path)唯一，命中即定；display_name 仅在
        # 全局唯一时作兜底，重名一律 None(与旧代码 len(matches)!=1 → 无强键匹配
        # 时返回 None 一致)。不再每次深拷全量快照。
        self._ensure_derived()
        strong = self._strong_index.get(name)
        if strong is not None:
            return dict(strong)
        unique = self._display_unique.get(name)
        return dict(unique) if unique is not None else None

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
        entry = self.entry(name)
        if entry is None:
            return None
        return self._ux_avg_for_entry(entry, days=days)

    def _ux_avg_for_entry(self, entry: dict, days: int = 30) -> float | None:
        """按已解析 entry 读取近期均分，并用短 TTL 避免搜索热路径重复扫文件。"""
        sha = str(entry["content_sha"])
        cache_key = (str(entry["skill_id"]), int(days))
        now = time.monotonic()
        with self._ux_avg_cache_lock:
            cached = self._ux_avg_cache.get(cache_key)
            if cached is not None and now < cached[0] and cached[1] == sha:
                return cached[2]

            rows = load_ux_scores(Path(entry["path"]))
            if days > 0:
                cutoff = datetime.utcnow().timestamp() - days * 86400
                recent_rows: list[dict] = []
                for row in rows:
                    scored_at = row.get("scored_at", "")
                    try:
                        if datetime.fromisoformat(scored_at.rstrip("Z")).timestamp() >= cutoff:
                            recent_rows.append(row)
                    except Exception:
                        recent_rows.append(row)
                rows = recent_rows
            scores = [r.get("score") for r in rows
                      if r.get("commit_sha") == sha
                      and isinstance(r.get("score"), (int, float))]
            value = sum(scores) / len(scores) if scores else None
            self._ux_avg_cache[cache_key] = (
                time.monotonic() + UX_AVG_CACHE_TTL_SECONDS,
                sha,
                value,
            )
            return value

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
