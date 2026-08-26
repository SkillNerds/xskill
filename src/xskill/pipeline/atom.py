"""AtomTask 数据模型 + 文件存储 + 向量索引 + UX 打分
==========================================================

每个 ``AtomTask`` 是一段 multi-chat-turn（1-10 轮），由 ``TaskAgent`` 从
轨迹按"用户意图切换"切出来。内容落盘到
``<root>/<traj_id>/tasks/atom_*.json``，``watcher`` 用 ``last_offset`` 决定
增量起点；隐藏 SQLite 保存可重建的 Atom→路径定位和向量检索投影，JSON 始终
是业务事实源。

向量索引（基于 atom 的 ``summary or intent`` 嵌入）持久化在隐藏 SQLite；
``<root>/index.pkl`` 仅保留兼容旧发现路径的小型标记。
``HybridSearch`` (在 ``xskill.utils.search``) 把本模块的 ``vector_search`` 跟
BM25 关键字检索做 union+dedup。

本模块还含 AtomTask 用户体验分打分器（原 ux_score.py）：灰度期间对每个
AtomTask 打分。读入 AtomTask 数据 + 该 atom 用过的 skills / side / commit_sha，
输出 1–10 整数 score + 简短中文 reasons。落盘通过 :class:`xskill.canary.AtomCanary`
完成幂等追加（``atom_id`` 为主键）。
"""
from __future__ import annotations

import json
import logging
import pickle
import re
import sqlite3
import threading
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterator

import numpy as np

from xskill._sqlite_connect import connect_with_lock
from xskill.pipeline.atom_vector_index import VECTOR_DB_FILE, AtomVectorProjection

logger = logging.getLogger("xskill.ux_score")

_LOCATION_LOCKS_GUARD = threading.Lock()
_LOCATION_LOCKS: dict[str, threading.RLock] = {}


def _location_lock_for(root: Path) -> threading.RLock:
    key = str(root.resolve(strict=False))
    with _LOCATION_LOCKS_GUARD:
        return _LOCATION_LOCKS.setdefault(key, threading.RLock())


@dataclass
class AtomTask:
    """一段完整用户意图的最小提炼单元。

    字段约定：
    - ``offset_start`` / ``offset_end``: 在 ``<traj_id>.md`` 中的 **1-based 行号**
      （半开区间 ``[start, end)``——end 这一行不含；末 atom 的 end = 末行号+1），
      便于 ``ReadTraj`` 按行号读原文。轨迹入库后不变，行号稳定。
    - ``pre_atom_id`` / ``post_atom_id``: 前后 atom 链表，给 cluster/edit
      agent 沿时间线游走。
    - ``context_prefix``: atom 起始行之前内容的省略表示（头 200 字 + 占位）。
    - ``raw_segment``: ``[offset_start, offset_end)`` 行区间内的原文片段。
    """
    atom_id: str
    traj_id: str
    offset_start: int
    offset_end: int
    intent: str
    summary: str
    tags: list[str] = field(default_factory=list)
    used_skills: list[str] = field(default_factory=list)
    ux_score: int | None = None
    pre_atom_id: str | None = None
    post_atom_id: str | None = None
    context_prefix: str = ""
    raw_segment: str = ""
    source_model: str = ""   # 产生该 atom 的用户 agent 模型，继承自所属轨迹的
    #                          <traj>.json sidecar "model"（canary 按模型分桶用）
    clustered: bool = False  # cluster 已消费标记（耐久）。cluster agent 成功把它
    #                          归进某 skill buffer 后由 process_atom_batch/_task 置真。
    #                          watcher 据此跨轨迹池化去重 + 判轨迹 done——比
    #                          .candidates.yml 成员更耐久：SkillEdit 晋升会清空
    #                          .candidates.yml，但本标记留在 atom JSON，跨进程重启
    #                          与 skill 晋升都不丢（rebuild 删 atom 时自然复位）。

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, indent=2)

    @classmethod
    def from_json(cls, s: str) -> "AtomTask":
        return cls(**json.loads(s))


class AtomTaskStore:
    """JSON 事实源 + Atom 定位/向量 SQLite 投影 + 小型 ``index.pkl`` 标记。

    设计取舍：
    - **JSON 是事实源**: 文件读写直接、调试方便；SQLite 仅作可重建投影。
    - **每 traj 一个子目录**: 让 watcher 按 traj 粒度做增量处理，``list_by_traj``
      不需要全表扫。
    - **向量索引增量维护**: ``save_many`` 只登记变化 Atom，watcher 只 embed / 写
      这些行；模型变化和低频一致性核对才从 JSON 原子重建。
    """

    INDEX_FILE = "index.pkl"
    VECTOR_INDEX_FILE = VECTOR_DB_FILE
    LOCATION_INDEX_FILE = ".atom_locations.sqlite3"
    _LOCATION_SCHEMA = """
    CREATE TABLE IF NOT EXISTS atom_locations (
        atom_id       TEXT NOT NULL,
        traj_id       TEXT NOT NULL,
        relative_path TEXT NOT NULL,
        mtime_ns      INTEGER NOT NULL,
        size_bytes    INTEGER NOT NULL,
        PRIMARY KEY (atom_id, relative_path)
    );
    CREATE INDEX IF NOT EXISTS idx_atom_locations_traj
        ON atom_locations(traj_id);
    CREATE TABLE IF NOT EXISTS atom_location_meta (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    """

    def __init__(self, root: Path):
        self.root = Path(root)
        self._location_lock = _location_lock_for(self.root)
        self._location_schema_ready = False
        self._location_index_complete: bool | None = None
        self._vector_projection = AtomVectorProjection(
            self.root,
            self._location_lock,
        )

    # ── paths ─────────────────────────────────────────────────────

    def _traj_dir(self, traj_id: str) -> Path:
        return self.root / traj_id / "tasks"

    def _path(self, atom: AtomTask) -> Path:
        return self._traj_dir(atom.traj_id) / f"{atom.atom_id}.json"

    def _index_path(self) -> Path:
        return self.root / self.INDEX_FILE

    def _location_index_path(self) -> Path:
        return self.root / self.LOCATION_INDEX_FILE

    def _location_connection(self, path: Path | None = None):
        index_path = path or self._location_index_path()
        connection = connect_with_lock(
            sqlite3.connect,
            str(index_path),
            timeout=5.0,
        )
        connection.row_factory = sqlite3.Row
        if path is None and self._location_schema_ready:
            return connection
        try:
            connection.executescript(self._LOCATION_SCHEMA)
        except Exception:
            connection.close()
            raise
        if path is None:
            self._location_schema_ready = True
        return connection

    def _location_values(self, path: Path, traj_id: str) -> tuple:
        stat_result = path.stat()
        relative_path = path.relative_to(self.root).as_posix()
        return (
            path.stem,
            traj_id,
            relative_path,
            int(stat_result.st_mtime_ns),
            int(stat_result.st_size),
        )

    def _has_complete_location_index(self) -> bool:
        """返回投影是否来自一次完整事实源扫描；每个 store 实例只查库一次。"""
        if self._location_index_complete is not None:
            return self._location_index_complete
        if not self._location_index_path().is_file():
            self._location_index_complete = False
            return False
        connection = self._location_connection()
        try:
            row = connection.execute(
                "SELECT value FROM atom_location_meta WHERE key='complete'",
            ).fetchone()
        finally:
            connection.close()
        self._location_index_complete = bool(row and row["value"] == "1")
        return self._location_index_complete

    def ensure_location_index(self) -> None:
        """旧 store 首次使用时从 JSON 一次性建立完整、可增量维护的投影。"""
        with self._location_lock:
            try:
                if self._has_complete_location_index():
                    return
            except (OSError, sqlite3.DatabaseError):
                pass
            self.rebuild_location_index()

    @staticmethod
    def _upsert_location_row(connection, values: tuple) -> None:
        connection.execute(
            """
            INSERT INTO atom_locations(
                atom_id, traj_id, relative_path, mtime_ns, size_bytes
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(atom_id, relative_path) DO UPDATE SET
                traj_id=excluded.traj_id,
                mtime_ns=excluded.mtime_ns,
                size_bytes=excluded.size_bytes
            """,
            values,
        )

    def _upsert_atom_location(self, path: Path, traj_id: str) -> None:
        self._upsert_atom_locations([(path, traj_id)])

    def _upsert_atom_locations(self, locations: list[tuple[Path, str]]) -> None:
        """用单个事务更新一批 Atom 定位，避免拆分结果逐条提交。"""
        if not locations:
            return
        connection = self._location_connection()
        try:
            for path, traj_id in locations:
                self._upsert_location_row(
                    connection,
                    self._location_values(path, traj_id),
                )
            connection.commit()
        finally:
            connection.close()

    def _scan_atom_paths(self, atom_id: str) -> list[tuple[Path, str]]:
        hits: list[tuple[Path, str]] = []
        for traj_dir in self.root.iterdir():
            if not traj_dir.is_dir():
                continue
            candidate = traj_dir / "tasks" / f"{atom_id}.json"
            if candidate.is_file():
                hits.append((candidate, traj_dir.name))
        return hits

    def _replace_atom_locations(
        self,
        atom_id: str,
        hits: list[tuple[Path, str]],
    ) -> None:
        connection = self._location_connection()
        try:
            connection.execute(
                "DELETE FROM atom_locations WHERE atom_id=?",
                (atom_id,),
            )
            for path, traj_id in hits:
                self._upsert_location_row(
                    connection,
                    self._location_values(path, traj_id),
                )
            connection.commit()
        finally:
            connection.close()

    def _path_from_location_row(self, atom_id: str, row) -> Path | None:
        relative = str(row["relative_path"] or "")
        relative_path = Path(relative)
        if (
            not relative
            or relative_path.is_absolute()
            or ".." in relative_path.parts
            or relative_path.name != f"{atom_id}.json"
            or len(relative_path.parts) != 3
            or relative_path.parts[1] != "tasks"
        ):
            return None
        return self.root / relative_path

    def rebuild_location_index(self) -> int:
        """从 Atom JSON 事实源原子重建定位投影，返回记录数。"""
        with self._location_lock:
            return self._rebuild_location_index()

    def _rebuild_location_index(self) -> int:
        if not self.root.is_dir():
            return 0
        temporary_path = self.root / (
            f".{self.LOCATION_INDEX_FILE}.{uuid.uuid4().hex}.tmp"
        )
        connection = self._location_connection(temporary_path)
        count = 0
        try:
            for traj_dir in sorted(self.root.iterdir()):
                if not traj_dir.is_dir():
                    continue
                tasks_dir = traj_dir / "tasks"
                if not tasks_dir.is_dir():
                    continue
                for atom_path in sorted(tasks_dir.glob("*.json")):
                    try:
                        values = self._location_values(atom_path, traj_dir.name)
                    except (FileNotFoundError, OSError):
                        continue
                    self._upsert_location_row(connection, values)
                    count += 1
            connection.execute(
                """
                INSERT INTO atom_location_meta(key, value) VALUES ('complete', '1')
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
                """
            )
            connection.commit()
        except Exception:
            connection.close()
            if temporary_path.is_file():
                temporary_path.unlink()
            raise
        connection.close()
        temporary_path.replace(self._location_index_path())
        self._location_schema_ready = True
        self._location_index_complete = True
        return count

    def remove_locations_for_trajs(self, traj_ids) -> None:
        """Atom 文件删除/reset 后同步清理对应轨迹的定位行与向量行。"""
        ids = sorted({str(traj_id) for traj_id in traj_ids if traj_id})
        if not ids:
            return
        self._vector_projection.remove_trajs(ids)
        if not self._location_index_path().is_file():
            return
        try:
            connection = self._location_connection()
            try:
                connection.executemany(
                    "DELETE FROM atom_locations WHERE traj_id=?",
                    [(traj_id,) for traj_id in ids],
                )
                connection.commit()
            finally:
                connection.close()
        except (OSError, sqlite3.DatabaseError):
            # 投影损坏不影响事实删除；直接从剩余 JSON 重建。
            try:
                self.rebuild_location_index()
            except (OSError, sqlite3.DatabaseError):
                logger.warning(
                    "atom location projection reset failed: %s",
                    self._location_index_path(),
                    exc_info=True,
                )

    # ── IO ────────────────────────────────────────────────────────

    def save(self, atom: AtomTask) -> Path:
        return self.save_many([atom])[0]

    def save_many(self, atoms: list[AtomTask]) -> list[Path]:
        """保存一批 Atom；JSON 逐个原子替换，定位投影只提交一次。"""
        with self._location_lock:
            return self._save_many(atoms)

    def _save_many(self, atoms: list[AtomTask]) -> list[Path]:
        if not atoms:
            return []
        try:
            projection_complete = self._has_complete_location_index()
        except (OSError, sqlite3.DatabaseError):
            projection_complete = False
        saved: list[tuple[Path, str]] = []
        for atom in atoms:
            atom_path = self._path(atom)
            atom_path.parent.mkdir(parents=True, exist_ok=True)
            temporary_path = atom_path.with_name(
                f".{atom_path.name}.{uuid.uuid4().hex}.tmp"
            )
            temporary_path.write_text(atom.to_json(), encoding="utf-8")
            temporary_path.replace(atom_path)
            saved.append((atom_path, atom.traj_id))
        try:
            if projection_complete:
                self._upsert_atom_locations(saved)
            else:
                # Upgrade/legacy store: one full scan prevents every old Atom
                # from paying its own trajectory-directory fallback later.
                self.rebuild_location_index()
        except (OSError, sqlite3.DatabaseError):
            logger.warning(
                "atom location projection batch update failed: %s",
                [str(path) for path, _traj_id in saved],
                exc_info=True,
            )
        try:
            self._vector_projection.record_atoms(atoms)
        except (OSError, sqlite3.DatabaseError):
            # JSON 是事实源；向量投影失败不回滚事实写，但必须立刻标 dirty
            # 否则 complete 索引会干等 24h 才把新 atom 补进检索。
            self._vector_projection.mark_rebuild_needed()
            logger.warning(
                "atom vector projection batch update failed: %s",
                [atom.atom_id for atom in atoms],
                exc_info=True,
            )
        return [path for path, _traj_id in saved]

    def load(self, atom_id: str) -> AtomTask:
        """通过定位投影跨 traj_id 读取；找不到抛 ``FileNotFoundError``。"""
        path = self.path_for_atom(atom_id)
        if path is not None:
            return AtomTask.from_json(path.read_text(encoding="utf-8"))
        raise FileNotFoundError(f"atom not found: {atom_id}")

    def path_for_atom(self, atom_id: str) -> Path | None:
        """通过持久定位投影返回 Atom JSON；失效或 miss 时回退事实源自愈。"""
        if (
            not atom_id
            or atom_id in (".", "..")
            or "/" in atom_id
            or "\\" in atom_id
            or not self.root.is_dir()
        ):
            return None
        try:
            self.ensure_location_index()
        except (OSError, sqlite3.DatabaseError):
            logger.warning(
                "atom location projection initialization failed: %s",
                self._location_index_path(),
                exc_info=True,
            )
        projected = self.projected_path_for_atom(atom_id)
        if projected is not None:
            return projected

        # Legacy/external writes may not have emitted a projection event.  A miss
        # pays one fact-source scan, then subsequent lookups are indexed.
        hits = self._scan_atom_paths(atom_id)
        try:
            self._replace_atom_locations(atom_id, hits)
        except (OSError, sqlite3.DatabaseError):
            try:
                self.rebuild_location_index()
            except (OSError, sqlite3.DatabaseError):
                logger.warning(
                    "atom location projection repair failed: %s",
                    self._location_index_path(),
                    exc_info=True,
                )
        available_hits = [hit for hit in hits if hit[0].is_file()]
        if not available_hits:
            return None
        return max(
            available_hits,
            key=lambda hit: (hit[0].stat().st_mtime_ns, str(hit[0])),
        )[0]

    def projected_path_for_atom(self, atom_id: str) -> Path | None:
        """只查定位投影，不在 miss 时遍历轨迹目录（Multi-store 热路径用）。"""
        if (
            not atom_id
            or atom_id in (".", "..")
            or "/" in atom_id
            or "\\" in atom_id
            or not self.root.is_dir()
            or not self._location_index_path().is_file()
        ):
            return None
        try:
            connection = self._location_connection()
            try:
                rows = connection.execute(
                    """
                    SELECT traj_id, relative_path, mtime_ns, size_bytes
                    FROM atom_locations
                    WHERE atom_id=?
                    ORDER BY mtime_ns DESC, relative_path ASC
                    """,
                    (atom_id,),
                ).fetchall()
            finally:
                connection.close()
        except sqlite3.DatabaseError:
            logger.warning(
                "atom location projection damaged; rebuilding: %s",
                self._location_index_path(),
            )
            try:
                self.rebuild_location_index()
            except (OSError, sqlite3.DatabaseError):
                logger.warning(
                    "atom location projection rebuild failed: %s",
                    self._location_index_path(),
                    exc_info=True,
                )
                return None
            return self.projected_path_for_atom(atom_id)
        available: list[tuple[Path, int]] = []
        for row in rows:
            candidate = self._path_from_location_row(atom_id, row)
            if candidate is None or not candidate.is_file():
                continue
            try:
                stat_result = candidate.stat()
                available.append((candidate, int(stat_result.st_mtime_ns)))
                if (
                    int(row["mtime_ns"]) != int(stat_result.st_mtime_ns)
                    or int(row["size_bytes"]) != int(stat_result.st_size)
                ):
                    self._upsert_atom_location(candidate, str(row["traj_id"]))
            except (OSError, sqlite3.DatabaseError):
                pass
        if available:
            return max(available, key=lambda item: (item[1], str(item[0])))[0]
        if rows:
            # A projected path was moved/deleted externally.  Clean misses stay
            # O(1); only stale rows pay a fact-source scan and repair.
            hits = self._scan_atom_paths(atom_id)
            try:
                self._replace_atom_locations(atom_id, hits)
            except (OSError, sqlite3.DatabaseError):
                return None
            if hits:
                return max(
                    hits,
                    key=lambda hit: (hit[0].stat().st_mtime_ns, str(hit[0])),
                )[0]
        return None

    def list_by_traj(self, traj_id: str) -> list[AtomTask]:
        d = self._traj_dir(traj_id)
        if not d.is_dir():
            return []
        return [
            AtomTask.from_json(p.read_text(encoding="utf-8"))
            for p in sorted(d.glob("atom_*.json"))
        ]

    def all_atoms(self) -> Iterator[AtomTask]:
        if not self.root.is_dir():
            return
        for traj_dir in sorted(self.root.iterdir()):
            if not traj_dir.is_dir():
                continue
            tasks_dir = traj_dir / "tasks"
            if not tasks_dir.is_dir():
                continue
            for p in sorted(tasks_dir.glob("atom_*.json")):
                yield AtomTask.from_json(p.read_text(encoding="utf-8"))

    def iter_tags(self) -> Iterator[list[str]]:
        """只产出每个 atom 的 ``tags`` 列表——给标签云聚合等只看标签的调用方用。

        落盘结构与 :meth:`all_atoms` 相同，但只 ``json.loads`` 后取 ``tags``
        字段，省去构建完整 ``AtomTask`` 对象（``raw_segment`` 等大字段照旧被
        json 解析，但不再实例化 dataclass）。:meth:`all_atoms` 的契约不变。
        """
        if not self.root.is_dir():
            return
        for traj_dir in sorted(self.root.iterdir()):
            if not traj_dir.is_dir():
                continue
            tasks_dir = traj_dir / "tasks"
            if not tasks_dir.is_dir():
                continue
            for atom_path in sorted(tasks_dir.glob("atom_*.json")):
                data = json.loads(atom_path.read_text(encoding="utf-8"))
                yield list(data.get("tags", []) or [])

    # ── offset pointer ────────────────────────────────────────────

    def last_offset(self, traj_id: str) -> int:
        atoms = self.list_by_traj(traj_id)
        return max((a.offset_end for a in atoms), default=0)

    def last_atom_id(self, traj_id: str) -> str | None:
        atoms = self.list_by_traj(traj_id)
        return atoms[-1].atom_id if atoms else None

    # ── vector index ──────────────────────────────────────────────

    def rebuild_vector_index(self, embed_client, *, force_full: bool = False) -> dict:
        """消费增量向量行；模型变化、显式请求或低频到期时原子全量重建。"""
        return self._vector_projection.rebuild(
            embed_client,
            force_full=force_full,
        )

    def vector_index_reconcile_due(self) -> bool:
        """空闲 watcher 是否需执行启动迁移或低频事实源一致性核对。"""
        return self._vector_projection.reconcile_due()

    def vector_search(self, query: str, embed_client, top_k: int = 5) -> list[dict]:
        if top_k <= 0:
            return []
        projected = self._vector_projection.search(
            query,
            embed_client,
            top_k=top_k,
        )
        if projected is not None:
            return projected
        # 升级期间、首次 watcher 对账前兼容旧 index.pkl。
        p = self._index_path()
        if not p.is_file():
            return []
        with open(p, "rb") as f:
            data = pickle.load(f)
        if data.get("format") or (
            data.get("model") or ""
        ) != (getattr(embed_client, "model", "") or ""):
            return []
        q = embed_client.encode(query)
        qn = np.linalg.norm(q)
        if qn > 0:
            q = q / qn
        sims = data["embeddings"] @ q
        ranked = sorted(enumerate(sims), key=lambda x: x[1], reverse=True)[:top_k]
        return [
            {"atom_id": data["atom_ids"][i], "similarity": float(s)}
            for i, s in ranked
        ]


class MultiAtomTaskStore:
    """跨多个 ``AtomTaskStore`` 路由的只读门面（team-CS 多 client 场景）。

    背景：team server 把每个 client 上传的轨迹注册成一个独立 ``watch_dir``
    （``label=client_id``），各自一份 ``AtomTaskStore``（root 各不相同）。
    SkillEditAgent 蒸馏某个 skill 时，其 ``.candidates.yml`` 里的 atom 可能
    来自**任意** client 的 store——单 store 的 ``load`` 只在自己 root 下找，
    跨 client 的 atom 必然 ``FileNotFoundError`` → ``atom_task_read`` 返回
    not found → 规则只能凭通用知识标 ``[推断]``，技能质量塌方。

    本类把多个底层 store 串起来：``load`` 依次问每个 store，唯一命中即返回；
    ``read_traj`` 用的 traj 文件路径由 ``traj_root_for(traj_id)`` 跨所有 root
    解析。若多个 store 同时命中同一个 atom/traj，记录 warning 后返回 mtime
    最新的内容；mtime 相同时按 store 顺序取第一个。**单 store（单机 /
    cold_flush）路径不经过本类**——runner 仅在
    ``len(stores) > 1`` 时才包一层，行为零回归。

    只暴露 SkillEdit / cluster 工具链实际用到的读接口（``load`` /
    ``list_by_traj`` / ``all_atoms`` / ``save`` / ``roots``）。``save`` 路由到
    atom 所属 traj 已存在的那个 store，找不到则落首个 store（``score_task``
    改 ux_score 时用——atom 必已存在于某 store）。
    """

    def __init__(self, stores: list["AtomTaskStore"]):
        if not stores:
            raise ValueError("MultiAtomTaskStore 需要至少一个底层 store")
        self.stores = list(stores)
        for store in self.stores:
            try:
                store.ensure_location_index()
            except (OSError, sqlite3.DatabaseError):
                logger.warning(
                    "atom location projection initialization failed: %s",
                    store._location_index_path(),
                    exc_info=True,
                )

    @property
    def roots(self) -> list[Path]:
        return [s.root for s in self.stores]

    @property
    def root(self) -> Path:
        """兼容单 store 接口的 ``.root`` 读取方（如 process_atom_task 取 traj_root）。
        多 store 下返回首个 store 的 root——调用方若需跨 root 解析应改用
        ``traj_root_for`` / ``roots``。"""
        return self.stores[0].root

    @staticmethod
    def _choose_latest(
        hits: list[tuple[int, "AtomTaskStore", Path]],
        *,
        label: str,
    ) -> tuple["AtomTaskStore", Path] | None:
        if not hits:
            return None
        if len(hits) > 1:
            logger.warning(
                "%s duplicated across atom stores; using newest: %s",
                label,
                [str(path) for _, _, path in hits],
            )
        _idx, store, path = max(
            hits,
            key=lambda h: (h[2].stat().st_mtime_ns, -h[0]),
        )
        return store, path

    def _atom_hits(self, atom_id: str) -> list[tuple[int, "AtomTaskStore", Path]]:
        hits = []
        for idx, store in enumerate(self.stores):
            path = store.projected_path_for_atom(atom_id)
            if path is not None:
                hits.append((idx, store, path))
        if hits:
            return hits
        # Legacy or external writes without a projection event: only a complete
        # projected miss pays the old fact-source scan and repairs each store.
        for idx, s in enumerate(self.stores):
            path = s.path_for_atom(atom_id)
            if path is not None:
                hits.append((idx, s, path))
        return hits

    def _traj_hits(
        self,
        traj_id: str,
        *,
        require_atoms: bool = False,
    ) -> list[tuple[int, "AtomTaskStore", Path]]:
        hits: list[tuple[int, "AtomTaskStore", Path]] = []
        for idx, s in enumerate(self.stores):
            marker = s.root / f"{traj_id}.md"
            if marker.is_file() and not require_atoms:
                hits.append((idx, s, marker))
                continue

            atoms = s.list_by_traj(traj_id)
            if require_atoms and not atoms:
                continue

            if marker.is_file():
                hits.append((idx, s, marker))
                continue
            if atoms:
                tasks_dir = s.root / traj_id / "tasks"
                hits.append((idx, s, tasks_dir))
        return hits

    def load(self, atom_id: str) -> AtomTask:
        hit = self._choose_latest(
            self._atom_hits(atom_id),
            label=f"atom id {atom_id}",
        )
        if hit is not None:
            _store, path = hit
            return AtomTask.from_json(path.read_text(encoding="utf-8"))
        raise FileNotFoundError(f"atom not found in any store: {atom_id}")

    def save(self, atom: AtomTask) -> Path:
        hit = self._choose_latest(
            self._atom_hits(atom.atom_id),
            label=f"atom id {atom.atom_id}",
        )
        target = hit[0] if hit is not None else self.stores[0]
        return target.save(atom)

    def list_by_traj(self, traj_id: str) -> list[AtomTask]:
        hit = self._choose_latest(
            self._traj_hits(traj_id, require_atoms=True),
            label=f"traj id {traj_id}",
        )
        if hit is not None:
            store, _path = hit
            return store.list_by_traj(traj_id)
        return []

    def all_atoms(self) -> Iterator[AtomTask]:
        for s in self.stores:
            yield from s.all_atoms()

    def traj_root_for(self, traj_id: str) -> Path | None:
        """返回含 ``<traj_id>.md`` 的 store root；找不到返回 None。

        ``read_traj`` 用它定位轨迹原文：atom 来自哪个 client 的 watch_dir，
        其 traj.md 就落在那个 root 下。"""
        hit = self._choose_latest(
            self._traj_hits(traj_id),
            label=f"traj id {traj_id}",
        )
        if hit is not None:
            store, _path = hit
            return store.root
        return None


# ═══════════════════════════════════════════════════════════════════
# AtomTask 用户体验分打分器（原 ux_score.py）
# ═══════════════════════════════════════════════════════════════════
# 灰度期间对每个 AtomTask（一段完整用户意图的对话片段）打分。读入：
#   - AtomTask 数据（context_prefix + raw_segment）
#   - 该 atom 用过的 skills，以及当前 side (main|staging) / commit_sha
# 输出：
#   - score:   1–10 整数（按严格分档表）
#   - reasons: 简短中文归因
# 落盘通过 :class:`xskill.canary.AtomCanary` 完成幂等追加（``atom_id`` 为主键）。
# 判定由 ``canary.check_and_decide`` 在每次入库后事件触发。


def _truncate(text: str, max_chars: int = 6000) -> str:
    if len(text) <= max_chars:
        return text
    head = text[: max_chars // 2]
    tail = text[-max_chars // 2 :]
    return head + "\n\n...（中间省略）...\n\n" + tail


_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


def _parse_score(raw: str) -> dict:
    """从 LLM 输出提取 JSON；容错：抽第一个 {...} 块再 json.loads。"""
    raw = raw.strip()
    try:
        return json.loads(raw)
    except Exception:
        logger.debug("score response was not a standalone JSON object",
                     exc_info=True)
    m = _JSON_RE.search(raw)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception as e:
            logger.warning(f"ux_score JSON 解析失败: {e}; raw={raw[:200]}")
    return {}


SYSTEM_PROMPT_ATOM = """你是用户体验评审员。给你一段 AtomTask（一个完整用户意图下的
对话片段 + 在哪些 skill 加载下做的）。请按下面的严格分档表打 1-10 分。

# 严格分档表（永远质量驱动，不要凭"做了多少事"打）
  10 一次到位：用户提需求 → agent 一步给出正确产出 → 用户接受无澄清。
   9 接近一次到位：仅一处细节澄清。
   8 正确完成但绕了 1 个小弯。
   7 正确完成，2-3 次澄清/修正；无明显不耐烦。
   6 完成度边界（"这就行吧"）。
   5 核心需求达成但遗漏明显细节。
   4 多次错误后才接近正确，用户≥2 次否定词。
   3 任务勉强完成但用户明显失望。
   2 任务未完成 / 反复 blocker / 用户放弃。
   1 完全失败或副作用。

# 如果 used_skills 非空（agent 调过 skill）
- skill 一步到位 → 起步 8 分。
- skill 调了但导致绕弯/错误 → 直接降到 ≤5。

# 输出格式（严格 JSON，不要任何 JSON 以外的文字）
{"score": 7, "reasons": "<简短中文归因>"}
"""


def score_atom(llm, *, atom: "AtomTask", side: str) -> dict:
    """调一次 LLM，按严格分档表给 atom 打分。

    返回 ``{"score": int|None, "reasons": str}``。
    解析失败或越界时 score=None，让上层（watcher / AtomCanary）跳过记录。
    """
    body = _truncate((atom.context_prefix or "") + "\n\n" + (atom.raw_segment or ""))
    prompt = (
        f"side={side}\n"
        f"used_skills={atom.used_skills}\n"
        f"intent={atom.intent}\n"
        f"summary={atom.summary}\n\n"
        f"# 对话片段\n{body}\n\n请按系统指令打分。"
    )
    raw = llm.chat(prompt, system=SYSTEM_PROMPT_ATOM)
    data = _parse_score(raw)
    score = data.get("score")
    reasons = (data.get("reasons") or "").strip()
    try:
        score = int(score)
    except (TypeError, ValueError):
        score = None
    if score is None or not (1 <= score <= 10):
        logger.warning(f"score_atom 非法分数 ({score})；raw={raw[:200]}")
        return {"score": None, "reasons": reasons or raw[:200]}
    return {"score": score, "reasons": reasons}


def score_and_record_atoms(*, llm, skill_dir, store, traj_id, skill_name,
                           side, commit_sha, canary_config=None) -> dict:
    """对 store 中该 traj 的所有 atom 端到端打分并按 atom_id 落盘。

    每个 atom 独立调 ``score_atom``；幂等去重交给 ``AtomCanary.append``。
    所有 atom 处理完后调一次 ``check_and_decide`` 触发翻牌判定。

    返回：
      {
        "scored":   int,    # 本次实际新落盘的分数条数
        "skipped":  int,    # 因幂等跳过 / 越界 / LLM 失败跳过
        "decision": dict,   # 最后一次 check_and_decide 返回；无 atom 时空 dict
      }
    """
    from xskill.canary import AtomCanary

    skill_dir = Path(skill_dir)
    ac = AtomCanary(skill_dir=skill_dir)
    atoms = store.list_by_traj(traj_id)
    scored = 0
    skipped = 0
    for atom in atoms:
        result = score_atom(llm=llm, atom=atom, side=side)
        if result["score"] is None:
            skipped += 1
            continue
        written = ac.append(
            atom_id=atom.atom_id, skill_name=skill_name,
            side=side, commit_sha=commit_sha,
            score=result["score"], reasons=result["reasons"],
        )
        if written:
            scored += 1
        else:
            skipped += 1
    decision = ac.check_and_decide(config=canary_config) if atoms else {}
    return {"scored": scored, "skipped": skipped, "decision": decision}


# ═══════════════════════════════════════════════════════════════════
# Atom 反查 helper —— 给 ux 分查询用，从 atom_id 反查回 atom 内容
# ═══════════════════════════════════════════════════════════════════
# ux 分落盘时只记 ``atom_id``（不内联 atom 内容，避免冗余 + 陈旧）；查询侧
# 需要展示 atom 摘要/intent 时再按 id 反查。team server 落盘结构为
# ``traj_root/clients/<client_id>/sessions/<traj_id>/tasks/<atom_id>.json``
# （每个 client 一个独立 ``AtomTaskStore``，root = ``traj_root/clients/<cid>/sessions``）。
# 本 helper 在 team server 的 traj_root 下 glob 跨 client 桶找 atom 文件，
# 命中第一条即返回精简字段 dict；找不到（rebuild 已删 / atom_id 错）返回 None。

def load_atom_by_id(traj_root: Path | str, atom_id: str) -> dict | None:
    """按 atom_id 反查 atom 内容（team server 落盘结构）。

    扫描 ``traj_root/clients/*/sessions/*/tasks/<atom_id>.json``，命中第一条
    即返回 ``{"atom_id", "traj_id", "summary", "intent", "tags", "used_skills"}``；
    找不到返回 None。JSON 反序列化失败抛 ``ValueError``（不静默吞 corrupt 文件）。
    """
    root = Path(traj_root)
    if not root.is_dir() or not atom_id:
        return None
    pattern = f"clients/*/sessions/*/tasks/{atom_id}.json"
    for p in root.glob(pattern):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise ValueError(f"atom file corrupt: {p}: {e}") from e
        return {
            "atom_id": data.get("atom_id", atom_id),
            "traj_id": data.get("traj_id", ""),
            "summary": data.get("summary", ""),
            "intent": data.get("intent", ""),
            "tags": list(data.get("tags", []) or []),
            "used_skills": list(data.get("used_skills", []) or []),
        }
    return None
