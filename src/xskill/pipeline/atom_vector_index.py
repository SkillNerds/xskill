"""Atom JSON 事实源的可重建、增量 SQLite 向量投影。"""
from __future__ import annotations

import hashlib
import heapq
import json
import logging
import pickle
import sqlite3
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import numpy as np

from xskill._sqlite_connect import connect_with_lock

logger = logging.getLogger("xskill.atom_vector_index")

VECTOR_DB_FILE = ".atom_vectors.sqlite3"
LEGACY_INDEX_FILE = "index.pkl"
FORMAT = "atom-vector-sqlite-v1"
RECONCILE_INTERVAL_SECONDS = 24 * 60 * 60
EMBED_BATCH_SIZE = 128
SEARCH_FETCH_SIZE = 512

_SCHEMA = """
CREATE TABLE IF NOT EXISTS atom_vectors (
    atom_id      TEXT PRIMARY KEY,
    traj_id      TEXT NOT NULL,
    vector_text  TEXT NOT NULL,
    text_sha     TEXT NOT NULL,
    generation   INTEGER NOT NULL DEFAULT 1,
    embedding    BLOB,
    dim          INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_atom_vectors_traj ON atom_vectors(traj_id);
CREATE INDEX IF NOT EXISTS idx_atom_vectors_pending
    ON atom_vectors(atom_id) WHERE embedding IS NULL;
CREATE TABLE IF NOT EXISTS atom_vector_traj_state (
    traj_id        TEXT PRIMARY KEY,
    tasks_mtime_ns INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS atom_vector_meta (
    singleton     INTEGER PRIMARY KEY CHECK(singleton=1),
    format        TEXT NOT NULL,
    model         TEXT NOT NULL,
    complete      INTEGER NOT NULL DEFAULT 0,
    reconciled_at REAL NOT NULL DEFAULT 0
);
"""


def _text_sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _vector_input(atom) -> tuple[str, str, str, str]:
    text = atom.summary or atom.intent
    return atom.atom_id, atom.traj_id, text, _text_sha(text)


def _json_vector_input(path: Path, traj_id: str) -> tuple[str, str, str, str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    text = str(data.get("summary") or data.get("intent") or "")
    return str(data["atom_id"]), traj_id, text, _text_sha(text)


def _normalized_rows(vectors, expected: int) -> np.ndarray:
    fresh = np.asarray(vectors, dtype=np.float32)
    if fresh.ndim == 1 and expected == 1:
        fresh = fresh.reshape(1, -1)
    if fresh.ndim != 2 or fresh.shape[0] != expected or fresh.shape[1] <= 0:
        raise ValueError(
            f"embedding batch shape mismatch: expected {expected} rows, got {fresh.shape}"
        )
    norms = np.linalg.norm(fresh, axis=1, keepdims=True)
    norms[norms == 0] = 1
    return fresh / norms


class AtomVectorProjection:
    """每个 Atom store 一份 SQLite 投影；调用方传入与 JSON 写路径共享的锁。"""

    def __init__(self, root: Path, lock) -> None:
        self.root = Path(root)
        self.lock = lock
        self._schema_ready = False
        self._pending_hint = False
        self._next_reconcile_at: float | None = None

    def mark_rebuild_needed(self) -> None:
        """投影写入失败后强制 watcher 在下一轮重建，避免干等 24h 对账。"""
        self._pending_hint = True

    @property
    def path(self) -> Path:
        return self.root / VECTOR_DB_FILE

    @property
    def marker_path(self) -> Path:
        return self.root / LEGACY_INDEX_FILE

    def _connect(self, path: Path | None = None):
        target = path or self.path
        connection = connect_with_lock(sqlite3.connect, str(target), timeout=5.0)
        connection.row_factory = sqlite3.Row
        if path is None and self._schema_ready:
            return connection
        try:
            connection.executescript(_SCHEMA)
        except Exception:
            connection.close()
            raise
        if path is None:
            self._schema_ready = True
        return connection

    @staticmethod
    def _upsert_target(connection, target: tuple[str, str, str, str]) -> None:
        connection.execute(
            """
            INSERT INTO atom_vectors(
                atom_id, traj_id, vector_text, text_sha, generation,
                embedding, dim
            ) VALUES (?, ?, ?, ?, 1, NULL, 0)
            ON CONFLICT(atom_id) DO UPDATE SET
                traj_id=excluded.traj_id,
                vector_text=excluded.vector_text,
                text_sha=excluded.text_sha,
                generation=CASE
                    WHEN atom_vectors.text_sha != excluded.text_sha
                    THEN atom_vectors.generation + 1
                    ELSE atom_vectors.generation
                END,
                embedding=CASE
                    WHEN atom_vectors.text_sha = excluded.text_sha
                    THEN atom_vectors.embedding ELSE NULL
                END,
                dim=CASE
                    WHEN atom_vectors.text_sha = excluded.text_sha
                    THEN atom_vectors.dim ELSE 0
                END
            """,
            target,
        )

    def _tasks_mtime(self, traj_id: str) -> int:
        try:
            return int((self.root / traj_id / "tasks").stat().st_mtime_ns)
        except OSError:
            return 0

    def record_atoms(self, atoms: list) -> None:
        """JSON 已落盘后记录目标文本；非向量字段保存不会让 embedding 失效。"""
        if not atoms:
            return
        self.root.mkdir(parents=True, exist_ok=True)
        targets = [_vector_input(atom) for atom in atoms]
        traj_ids = sorted({target[1] for target in targets})
        with self.lock:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                for target in targets:
                    self._upsert_target(connection, target)
                for traj_id in traj_ids:
                    connection.execute(
                        """
                        INSERT INTO atom_vector_traj_state(traj_id, tasks_mtime_ns)
                        VALUES (?, ?)
                        ON CONFLICT(traj_id) DO UPDATE SET
                            tasks_mtime_ns=excluded.tasks_mtime_ns
                        """,
                        (traj_id, self._tasks_mtime(traj_id)),
                    )
                self._pending_hint = connection.execute(
                    "SELECT 1 FROM atom_vectors WHERE embedding IS NULL LIMIT 1"
                ).fetchone() is not None
                connection.commit()
            except Exception:
                self._pending_hint = True
                raise
            finally:
                connection.close()

    def remove_trajs(self, traj_ids) -> None:
        ids = sorted({str(traj_id) for traj_id in traj_ids if traj_id})
        if not ids or not self.path.is_file():
            return
        with self.lock:
            try:
                connection = self._connect()
                try:
                    connection.execute("BEGIN IMMEDIATE")
                    connection.executemany(
                        "DELETE FROM atom_vectors WHERE traj_id=?",
                        [(traj_id,) for traj_id in ids],
                    )
                    connection.executemany(
                        "DELETE FROM atom_vector_traj_state WHERE traj_id=?",
                        [(traj_id,) for traj_id in ids],
                    )
                    self._pending_hint = connection.execute(
                        "SELECT 1 FROM atom_vectors WHERE embedding IS NULL LIMIT 1"
                    ).fetchone() is not None
                    connection.commit()
                finally:
                    connection.close()
                self._write_marker()
            except (OSError, sqlite3.DatabaseError):
                logger.warning("atom vector reset failed: %s", self.path, exc_info=True)

    def _meta(self) -> dict | None:
        if not self.path.is_file():
            return None
        connection = self._connect()
        try:
            row = connection.execute(
                """
                SELECT format, model, complete, reconciled_at
                FROM atom_vector_meta WHERE singleton=1
                """
            ).fetchone()
        finally:
            connection.close()
        return dict(row) if row else None

    def reconcile_due(self, *, now: float | None = None) -> bool:
        """供 watcher 空闲轮询 O(1) 判断是否需启动/低频事实源核对。"""
        current = time.time() if now is None else float(now)
        if self._pending_hint:
            return True
        if (
            self._next_reconcile_at is not None
            and current < self._next_reconcile_at
        ):
            return False
        if not self.path.is_file():
            return self.marker_path.is_file()
        try:
            connection = self._connect()
            try:
                row = connection.execute(
                    """
                    SELECT format, model, complete, reconciled_at
                    FROM atom_vector_meta WHERE singleton=1
                    """
                ).fetchone()
                pending = connection.execute(
                    "SELECT 1 FROM atom_vectors WHERE embedding IS NULL LIMIT 1"
                ).fetchone()
            finally:
                connection.close()
        except (OSError, sqlite3.DatabaseError):
            return True
        meta = dict(row) if row else None
        if not meta or meta["format"] != FORMAT or not meta["complete"]:
            return True
        if pending is not None:
            self._pending_hint = True
            return True
        self._next_reconcile_at = (
            float(meta["reconciled_at"] or 0) + RECONCILE_INTERVAL_SECONDS
        )
        return current >= self._next_reconcile_at

    def _iter_task_dirs(self) -> Iterator[tuple[str, Path]]:
        if not self.root.is_dir():
            return
        for traj_dir in sorted(self.root.iterdir()):
            if not traj_dir.is_dir():
                continue
            tasks = traj_dir / "tasks"
            if tasks.is_dir():
                yield traj_dir.name, tasks

    @staticmethod
    def _iter_task_paths(tasks: Path) -> Iterator[Path]:
        yield from sorted(tasks.glob("atom_*.json"))

    def _reconcile_changed_trajs(self, connection) -> tuple[int, int]:
        stored = {
            row["traj_id"]: int(row["tasks_mtime_ns"])
            for row in connection.execute(
                "SELECT traj_id, tasks_mtime_ns FROM atom_vector_traj_state"
            ).fetchall()
        }
        current = {}
        for traj_id, tasks in self._iter_task_dirs():
            try:
                current[traj_id] = (tasks, int(tasks.stat().st_mtime_ns))
            except OSError:
                # A direct external reset may remove the directory between
                # discovery and stat.  Treat it as absent in this snapshot.
                continue
        changed = 0
        deleted = 0
        for traj_id in sorted(stored.keys() - current.keys()):
            cursor = connection.execute(
                "DELETE FROM atom_vectors WHERE traj_id=?", (traj_id,),
            )
            deleted += max(0, int(cursor.rowcount or 0))
            connection.execute(
                "DELETE FROM atom_vector_traj_state WHERE traj_id=?", (traj_id,),
            )
        for traj_id, (tasks, mtime_ns) in current.items():
            if stored.get(traj_id) == mtime_ns:
                continue
            changed += 1
            connection.execute(
                "CREATE TEMP TABLE IF NOT EXISTS atom_vector_seen(atom_id TEXT PRIMARY KEY)"
            )
            connection.execute("DELETE FROM atom_vector_seen")
            for atom_path in self._iter_task_paths(tasks):
                try:
                    target = _json_vector_input(atom_path, traj_id)
                except OSError:
                    # The shared lock serializes xskill writers.  This guard is
                    # for unsupported-but-possible direct filesystem deletes.
                    continue
                self._upsert_target(connection, target)
                connection.execute(
                    "INSERT OR IGNORE INTO atom_vector_seen(atom_id) VALUES (?)",
                    (target[0],),
                )
            cursor = connection.execute(
                """
                DELETE FROM atom_vectors
                WHERE traj_id=? AND atom_id NOT IN (SELECT atom_id FROM atom_vector_seen)
                """,
                (traj_id,),
            )
            deleted += max(0, int(cursor.rowcount or 0))
            connection.execute(
                """
                INSERT INTO atom_vector_traj_state(traj_id, tasks_mtime_ns)
                VALUES (?, ?)
                ON CONFLICT(traj_id) DO UPDATE SET
                    tasks_mtime_ns=excluded.tasks_mtime_ns
                """,
                (traj_id, mtime_ns),
            )
        return changed, deleted

    @staticmethod
    def _legacy_cache(path: Path, model: str) -> dict[str, np.ndarray]:
        if not path.is_file():
            return {}
        try:
            with path.open("rb") as stream:
                data = pickle.load(stream)
            if data.get("format") == FORMAT or (data.get("model") or "") != model:
                return {}
            atom_ids = data.get("atom_ids") or []
            embeddings = data.get("embeddings")
            if embeddings is None:
                return {}
            return {
                atom_id: np.asarray(embeddings[index], dtype=np.float32)
                for index, atom_id in enumerate(atom_ids)
                if atom_id and index < len(embeddings)
            }
        except (OSError, pickle.PickleError, EOFError, AttributeError, ValueError):
            logger.warning("legacy atom vector index unreadable: %s", path, exc_info=True)
            return {}

    def _reuse_vector(
        self,
        old_connection,
        legacy: dict[str, np.ndarray],
        target: tuple[str, str, str, str],
    ) -> tuple[bytes, int] | None:
        atom_id, _traj_id, _text, text_sha = target
        if old_connection is not None:
            row = old_connection.execute(
                """
                SELECT embedding, dim FROM atom_vectors
                WHERE atom_id=? AND text_sha=? AND embedding IS NOT NULL
                """,
                (atom_id, text_sha),
            ).fetchone()
            if row is not None:
                blob = bytes(row["embedding"])
                dim = int(row["dim"])
                if dim > 0 and len(blob) == dim * np.dtype(np.float32).itemsize:
                    return blob, dim
        vector = legacy.get(atom_id)
        if vector is None or vector.ndim != 1:
            return None
        norm = float(np.linalg.norm(vector)) or 1.0
        normalized = np.asarray(vector / norm, dtype=np.float32)
        return normalized.tobytes(), int(normalized.shape[0])

    def _full_rebuild(self, embed_client, model: str, *, now: float) -> dict:
        self.root.mkdir(parents=True, exist_ok=True)
        temporary = self.root / f".{VECTOR_DB_FILE}.{uuid.uuid4().hex}.tmp"
        old_connection = None
        old_meta = None
        if self.path.is_file():
            try:
                old_meta = self._meta()
                if old_meta and old_meta["model"] == model:
                    old_connection = self._connect()
            except (OSError, sqlite3.DatabaseError):
                old_connection = None
        legacy = self._legacy_cache(self.marker_path, model)
        connection = self._connect(temporary)
        scanned = reused = 0
        try:
            connection.execute("BEGIN IMMEDIATE")
            for traj_id, tasks in self._iter_task_dirs():
                try:
                    before_mtime_ns = int(tasks.stat().st_mtime_ns)
                except OSError:
                    continue
                unstable = False
                for atom_path in self._iter_task_paths(tasks):
                    try:
                        target = _json_vector_input(atom_path, traj_id)
                    except OSError:
                        unstable = True
                        continue
                    self._upsert_target(connection, target)
                    cached = self._reuse_vector(old_connection, legacy, target)
                    if cached is not None:
                        connection.execute(
                            """
                            UPDATE atom_vectors SET embedding=?, dim=? WHERE atom_id=?
                            """,
                            (cached[0], cached[1], target[0]),
                        )
                        reused += 1
                    scanned += 1
                try:
                    after_mtime_ns = int(tasks.stat().st_mtime_ns)
                except OSError:
                    # Do not carry rows from a trajectory removed during the
                    # unlocked streaming pass into the replacement database.
                    connection.execute(
                        "DELETE FROM atom_vectors WHERE traj_id=?", (traj_id,),
                    )
                    continue
                connection.execute(
                    """
                    INSERT INTO atom_vector_traj_state(traj_id, tasks_mtime_ns)
                    VALUES (?, ?)
                    """,
                    (
                        traj_id,
                        -1 if unstable or before_mtime_ns != after_mtime_ns
                        else after_mtime_ns,
                    ),
                )
            connection.commit()
            embedded = self._embed_pending(connection, embed_client)
            with self.lock:
                connection.execute("BEGIN IMMEDIATE")
                late_changed, late_deleted = self._reconcile_changed_trajs(connection)
                connection.execute(
                    """
                    INSERT INTO atom_vector_meta(
                        singleton, format, model, complete, reconciled_at
                    ) VALUES (1, ?, ?, 1, ?)
                    ON CONFLICT(singleton) DO UPDATE SET
                        format=excluded.format,
                        model=excluded.model,
                        complete=1,
                        reconciled_at=excluded.reconciled_at
                    """,
                    (FORMAT, model, now),
                )
                connection.commit()
                connection.close()
                connection = None
                if old_connection is not None:
                    old_connection.close()
                    old_connection = None
                temporary.replace(self.path)
                self._schema_ready = True
            embedded += self._embed_pending_path(embed_client)
            self._refresh_pending_hint()
            self._next_reconcile_at = now + RECONCILE_INTERVAL_SECONDS
            self._write_marker()
            return {
                "mode": "full",
                "scanned": scanned,
                "reused": reused,
                "embedded": embedded,
                "changed_trajs": late_changed,
                "deleted": late_deleted,
            }
        finally:
            if old_connection is not None:
                old_connection.close()
            if connection is not None:
                connection.close()
            if temporary.is_file():
                temporary.unlink()

    @staticmethod
    def _embed_pending(connection, embed_client) -> int:
        embedded = 0
        while True:
            rows = connection.execute(
                """
                SELECT atom_id, vector_text, generation FROM atom_vectors
                WHERE embedding IS NULL
                ORDER BY atom_id LIMIT ?
                """,
                (EMBED_BATCH_SIZE,),
            ).fetchall()
            if not rows:
                return embedded
            fresh = _normalized_rows(
                embed_client.encode_batch([row["vector_text"] for row in rows]),
                len(rows),
            )
            connection.execute("BEGIN IMMEDIATE")
            for row, vector in zip(rows, fresh):
                cursor = connection.execute(
                    """
                    UPDATE atom_vectors SET embedding=?, dim=?
                    WHERE atom_id=? AND generation=? AND embedding IS NULL
                    """,
                    (
                        np.asarray(vector, dtype=np.float32).tobytes(),
                        int(vector.shape[0]),
                        row["atom_id"],
                        int(row["generation"]),
                    ),
                )
                embedded += max(0, int(cursor.rowcount or 0))
            connection.commit()

    def _embed_pending_path(self, embed_client) -> int:
        embedded = 0
        while True:
            connection = self._connect()
            try:
                rows = connection.execute(
                    """
                    SELECT atom_id, vector_text, generation FROM atom_vectors
                    WHERE embedding IS NULL ORDER BY atom_id LIMIT ?
                    """,
                    (EMBED_BATCH_SIZE,),
                ).fetchall()
            finally:
                connection.close()
            if not rows:
                return embedded
            fresh = _normalized_rows(
                embed_client.encode_batch([row["vector_text"] for row in rows]),
                len(rows),
            )
            with self.lock:
                connection = self._connect()
                try:
                    connection.execute("BEGIN IMMEDIATE")
                    for row, vector in zip(rows, fresh):
                        cursor = connection.execute(
                            """
                            UPDATE atom_vectors SET embedding=?, dim=?
                            WHERE atom_id=? AND generation=? AND embedding IS NULL
                            """,
                            (
                                np.asarray(vector, dtype=np.float32).tobytes(),
                                int(vector.shape[0]),
                                row["atom_id"],
                                int(row["generation"]),
                            ),
                        )
                        embedded += max(0, int(cursor.rowcount or 0))
                    connection.commit()
                finally:
                    connection.close()

    def _refresh_pending_hint(self) -> None:
        with self.lock:
            connection = self._connect()
            try:
                self._pending_hint = connection.execute(
                    "SELECT 1 FROM atom_vectors WHERE embedding IS NULL LIMIT 1"
                ).fetchone() is not None
            finally:
                connection.close()

    def rebuild(self, embed_client, *, force_full: bool = False) -> dict:
        model = str(getattr(embed_client, "model", "") or "")
        now = time.time()
        try:
            meta = self._meta()
        except (OSError, sqlite3.DatabaseError):
            meta = None
        full = (
            force_full
            or meta is None
            or meta.get("format") != FORMAT
            or not meta.get("complete")
            or (meta.get("model") or "") != model
            or now - float(meta.get("reconciled_at") or 0)
            >= RECONCILE_INTERVAL_SECONDS
        )
        if full:
            return self._full_rebuild(embed_client, model, now=now)

        embedded = self._embed_pending_path(embed_client)
        self._refresh_pending_hint()
        if embedded or not self.marker_path.is_file():
            self._write_marker()
        return {
            "mode": "incremental",
            "scanned": 0,
            "reused": 0,
            "embedded": embedded,
            "changed_trajs": 0,
            "deleted": 0,
        }

    def _write_marker(self) -> None:
        if not self.path.is_file():
            return
        connection = self._connect()
        try:
            meta = connection.execute(
                "SELECT model, complete FROM atom_vector_meta WHERE singleton=1"
            ).fetchone()
            row = connection.execute(
                """
                SELECT COUNT(*) AS n, COALESCE(MAX(dim), 0) AS dim
                FROM atom_vectors WHERE embedding IS NOT NULL
                """
            ).fetchone()
        finally:
            connection.close()
        if not meta or not meta["complete"] or not row or not row["n"]:
            if self.marker_path.is_file():
                self.marker_path.unlink()
            return
        temporary = self.marker_path.with_name(
            f".{self.marker_path.name}.{uuid.uuid4().hex}.tmp"
        )
        temporary.write_bytes(pickle.dumps({
            "format": FORMAT,
            "model": meta["model"],
            "dim": int(row["dim"]),
            "count": int(row["n"]),
        }))
        temporary.replace(self.marker_path)

    def search(self, query: str, embed_client, *, top_k: int) -> list[dict] | None:
        """返回 SQLite 结果；投影不存在时返回 None 让调用方兼容 legacy pickle。"""
        if top_k <= 0:
            return []
        if not self.path.is_file():
            return None
        model = str(getattr(embed_client, "model", "") or "")
        with self.lock:
            try:
                connection = self._connect()
                try:
                    meta = connection.execute(
                        "SELECT model, complete FROM atom_vector_meta WHERE singleton=1"
                    ).fetchone()
                finally:
                    connection.close()
            except (OSError, sqlite3.DatabaseError):
                logger.warning("atom vector projection search failed", exc_info=True)
                return []
        if not meta or not meta["complete"]:
            return None
        if meta["model"] != model:
            return []
        query_vector = np.asarray(embed_client.encode(query), dtype=np.float32)
        norm = float(np.linalg.norm(query_vector))
        if norm > 0:
            query_vector = query_vector / norm
        with self.lock:
            connection = None
            try:
                connection = self._connect()
                meta = connection.execute(
                    "SELECT model, complete FROM atom_vector_meta WHERE singleton=1"
                ).fetchone()
                if not meta or not meta["complete"]:
                    return None
                if meta["model"] != model:
                    return []
                cursor = connection.execute(
                    """
                    SELECT atom_id, embedding, dim FROM atom_vectors
                    WHERE embedding IS NOT NULL ORDER BY traj_id, atom_id
                    """
                )
                heap: list[tuple[float, int, str]] = []
                ordinal = 0
                while True:
                    rows = cursor.fetchmany(SEARCH_FETCH_SIZE)
                    if not rows:
                        break
                    for row in rows:
                        dim = int(row["dim"])
                        if dim != query_vector.shape[0]:
                            ordinal += 1
                            continue
                        vector = np.frombuffer(row["embedding"], dtype=np.float32)
                        if vector.shape[0] != dim:
                            ordinal += 1
                            continue
                        score = float(vector @ query_vector)
                        item = (score, -ordinal, row["atom_id"])
                        if len(heap) < max(0, top_k):
                            heapq.heappush(heap, item)
                        elif top_k > 0 and item > heap[0]:
                            heapq.heapreplace(heap, item)
                        ordinal += 1
            except (OSError, sqlite3.DatabaseError, ValueError):
                logger.warning("atom vector projection search failed", exc_info=True)
                return []
            finally:
                if connection is not None:
                    connection.close()
        ranked = sorted(heap, key=lambda item: (-item[0], -item[1]))
        return [
            {"atom_id": atom_id, "similarity": score}
            for score, _neg_ordinal, atom_id in ranked
        ]
