"""
pipeline/registry.py -- SQLite 路径注册表 + Registry 实体类
============================================================

管理 ``~/.xskill/registry.db``，**只存路径和状态**，不存内容。

两张表：

- ``watch_dirs``   用户注册的待监听目录
- ``trajectories`` 每条轨迹文件的发现/索引状态

模块底部的 ``Registry`` 类把上面的模块函数包装为 OOP 接口；所有
watch_dir + trajectory 反查走这个类。
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from xskill.config import get_registry_db_path
from xskill.types import WatchDir

logger = logging.getLogger("xskill.registry")

# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS watch_dirs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    path       TEXT UNIQUE NOT NULL,
    label      TEXT DEFAULT '',
    auto_index INTEGER DEFAULT 1,
    ecosystem  TEXT DEFAULT 'manual',
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS trajectories (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    watch_dir_id  INTEGER NOT NULL REFERENCES watch_dirs(id) ON DELETE CASCADE,
    filename      TEXT NOT NULL,
    has_meta      INTEGER DEFAULT 0,
    has_embedding INTEGER DEFAULT 0,
    status        TEXT DEFAULT 'discovered',
    process_action TEXT,
    skill_generated TEXT,
    skill_used    TEXT,
    canary_side   TEXT,
    ux_score      REAL,
    error_msg     TEXT,
    retry_count   INTEGER DEFAULT 0,
    file_mtime    REAL DEFAULT 0,
    discovered_at TEXT DEFAULT (datetime('now')),
    indexed_at    TEXT,
    updated_at    TEXT DEFAULT (datetime('now')),
    UNIQUE(watch_dir_id, filename)
);
"""


def get_connection(db_path: Optional[Path] = None) -> sqlite3.Connection:
    """打开（或创建）注册表 DB。首次调用自动建表。"""
    if db_path is None:
        db_path = get_registry_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(_SCHEMA_SQL)
    # Migrate existing DBs that lack new columns
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns missing from older schema versions."""
    # ── trajectories ──
    cur = conn.execute("PRAGMA table_info(trajectories)")
    cols = {row[1] for row in cur.fetchall()}
    migrations = [
        ("status", "TEXT DEFAULT 'discovered'"),
        ("process_action", "TEXT"),
        ("skill_generated", "TEXT"),
        ("ux_score", "REAL"),
        ("error_msg", "TEXT"),
        ("retry_count", "INTEGER DEFAULT 0"),
        ("updated_at", "TEXT"),
        ("process_log", "TEXT"),
        # v2: AtomTask 流水线状态
        ("tasks_extracted", "INTEGER DEFAULT 0"),
        ("last_offset", "INTEGER DEFAULT 0"),
        ("last_atom_id", "TEXT"),
    ]
    for col, typedef in migrations:
        if col not in cols:
            conn.execute(f"ALTER TABLE trajectories ADD COLUMN {col} {typedef}")

    # ── watch_dirs ──
    cur = conn.execute("PRAGMA table_info(watch_dirs)")
    wd_cols = {row[1] for row in cur.fetchall()}
    if "ecosystem" not in wd_cols:
        conn.execute(
            "ALTER TABLE watch_dirs ADD COLUMN ecosystem TEXT DEFAULT 'manual'"
        )
        # 已有行历史上都是用户手动 register，标 'manual'
        conn.execute("UPDATE watch_dirs SET ecosystem='manual' WHERE ecosystem IS NULL")
    # Backfill status from has_meta/has_embedding for pre-existing rows
    conn.execute(
        "UPDATE trajectories SET status='indexed'"
        " WHERE has_embedding=1 AND (status IS NULL OR status='discovered')"
    )
    conn.execute(
        "UPDATE trajectories SET status='meta_done'"
        " WHERE has_meta=1 AND has_embedding=0 AND (status IS NULL OR status='discovered')"
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Watch directory management
# ---------------------------------------------------------------------------

def register_dir(
    dir_path: str | Path,
    label: str = "",
    auto_index: bool = True,
    ecosystem: str = "manual",
    *,
    db_path: Optional[Path] = None,
) -> int:
    """注册一个目录。幂等：已存在则更新 label/auto_index/ecosystem，返回 id。

    ``ecosystem`` 标记目录来源，便于 list / search 时区分：
      - ``manual`` (默认)：用户手动 ``xskill registry add`` 注册的
      - ``claude_code``：daemon 启动时自动发现的 Claude Code 会话桥接目录
      - 未来：``codex``、``opencode`` 等
    """
    dir_path = str(Path(dir_path).resolve())
    conn = get_connection(db_path)
    try:
        conn.execute(
            "INSERT INTO watch_dirs (path, label, auto_index, ecosystem)"
            " VALUES (?, ?, ?, ?)"
            " ON CONFLICT(path) DO UPDATE SET"
            "   label=excluded.label,"
            "   auto_index=excluded.auto_index,"
            "   ecosystem=excluded.ecosystem",
            (dir_path, label, int(auto_index), ecosystem),
        )
        conn.commit()
        row = conn.execute("SELECT id FROM watch_dirs WHERE path=?", (dir_path,)).fetchone()
        return row["id"]
    finally:
        conn.close()


def unregister_dir(dir_path: str | Path, *, db_path: Optional[Path] = None) -> bool:
    """移除目录及其轨迹记录。返回 True 表示找到并删除。"""
    dir_path = str(Path(dir_path).resolve())
    conn = get_connection(db_path)
    try:
        cur = conn.execute("DELETE FROM watch_dirs WHERE path=?", (dir_path,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def list_watch_dirs(*, db_path: Optional[Path] = None) -> list[dict]:
    """返回所有注册目录及统计信息。"""
    conn = get_connection(db_path)
    try:
        rows = conn.execute(
            "SELECT w.*, "
            "  (SELECT COUNT(*) FROM trajectories t WHERE t.watch_dir_id=w.id) AS traj_count,"
            "  (SELECT COUNT(*) FROM trajectories t WHERE t.watch_dir_id=w.id AND t.has_embedding=1) AS indexed_count"
            " FROM watch_dirs w ORDER BY w.id"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_watch_dir(dir_path: str | Path, *, db_path: Optional[Path] = None) -> dict | None:
    """查询单个目录记录。"""
    dir_path = str(Path(dir_path).resolve())
    conn = get_connection(db_path)
    try:
        row = conn.execute("SELECT * FROM watch_dirs WHERE path=?", (dir_path,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Trajectory tracking
# ---------------------------------------------------------------------------

def discover_trajectories(
    watch_dir_id: int,
    dir_path: Path,
    *,
    db_path: Optional[Path] = None,
) -> list[str]:
    """扫描目录中的 traj_*.md，upsert 到 DB。返回新发现的文件名列表。"""
    dir_path = Path(dir_path)
    conn = get_connection(db_path)
    new_files: list[str] = []
    try:
        existing = {
            row["filename"]
            for row in conn.execute(
                "SELECT filename FROM trajectories WHERE watch_dir_id=?",
                (watch_dir_id,),
            ).fetchall()
        }

        for md in sorted(dir_path.glob("traj_*.md")):
            if md.name.endswith(".meta"):
                continue
            mtime = md.stat().st_mtime
            if md.name not in existing:
                conn.execute(
                    "INSERT INTO trajectories (watch_dir_id, filename, file_mtime)"
                    " VALUES (?, ?, ?)",
                    (watch_dir_id, md.name, mtime),
                )
                new_files.append(md.name)
            else:
                # 更新 mtime（用于变更检测）
                conn.execute(
                    "UPDATE trajectories SET file_mtime=? WHERE watch_dir_id=? AND filename=?",
                    (mtime, watch_dir_id, md.name),
                )

        conn.commit()
        return new_files
    finally:
        conn.close()


def mark_meta_done(
    watch_dir_id: int, filename: str, *, db_path: Optional[Path] = None
) -> None:
    conn = get_connection(db_path)
    try:
        conn.execute(
            "UPDATE trajectories SET has_meta=1 WHERE watch_dir_id=? AND filename=?",
            (watch_dir_id, filename),
        )
        conn.commit()
    finally:
        conn.close()


def mark_indexed(
    watch_dir_id: int, filename: str, *, db_path: Optional[Path] = None
) -> None:
    conn = get_connection(db_path)
    try:
        conn.execute(
            "UPDATE trajectories SET has_embedding=1, indexed_at=?"
            " WHERE watch_dir_id=? AND filename=?",
            (datetime.now(timezone.utc).isoformat(timespec="seconds"), watch_dir_id, filename),
        )
        conn.commit()
    finally:
        conn.close()


def mark_skill_used(
    watch_dir_id: int,
    filename: str,
    skill_used: str,
    canary_side: str,
    *,
    db_path: Optional[Path] = None,
) -> None:
    conn = get_connection(db_path)
    try:
        conn.execute(
            "UPDATE trajectories SET skill_used=?, canary_side=?"
            " WHERE watch_dir_id=? AND filename=?",
            (skill_used, canary_side, watch_dir_id, filename),
        )
        conn.commit()
    finally:
        conn.close()


def get_unindexed(
    watch_dir_id: int, *, db_path: Optional[Path] = None
) -> list[str]:
    """返回缺少 meta 或 embedding 的文件名。"""
    conn = get_connection(db_path)
    try:
        rows = conn.execute(
            "SELECT filename FROM trajectories"
            " WHERE watch_dir_id=? AND (has_meta=0 OR has_embedding=0)"
            " ORDER BY filename",
            (watch_dir_id,),
        ).fetchall()
        return [r["filename"] for r in rows]
    finally:
        conn.close()


def get_needs_meta(
    watch_dir_id: int, *, db_path: Optional[Path] = None
) -> list[str]:
    """返回缺少 meta 的文件名。"""
    conn = get_connection(db_path)
    try:
        rows = conn.execute(
            "SELECT filename FROM trajectories"
            " WHERE watch_dir_id=? AND has_meta=0"
            " ORDER BY filename",
            (watch_dir_id,),
        ).fetchall()
        return [r["filename"] for r in rows]
    finally:
        conn.close()


def get_needs_embedding(
    watch_dir_id: int, *, db_path: Optional[Path] = None
) -> list[str]:
    """返回有 meta 但缺 embedding 的文件名。"""
    conn = get_connection(db_path)
    try:
        rows = conn.execute(
            "SELECT filename FROM trajectories"
            " WHERE watch_dir_id=? AND has_meta=1 AND has_embedding=0"
            " ORDER BY filename",
            (watch_dir_id,),
        ).fetchall()
        return [r["filename"] for r in rows]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Cross-dataset search support
# ---------------------------------------------------------------------------

def all_index_paths(*, db_path: Optional[Path] = None) -> list[Path]:
    """返回所有注册目录中实际存在 index.pkl 的路径。"""
    conn = get_connection(db_path)
    try:
        rows = conn.execute("SELECT path FROM watch_dirs ORDER BY id").fetchall()
        result = []
        for r in rows:
            p = Path(r["path"])
            if (p / "index.pkl").is_file():
                result.append(p)
        return result
    finally:
        conn.close()


def find_traj_file(
    traj_id: str,
    suffix: str = ".md",
    *,
    db_path: Optional[Path] = None,
) -> Path | None:
    """跨所有注册的 watch dir 查找 ``<traj_id><suffix>``。

    各 dir 先按"扁平布局"（``<wd>/<traj_id><suffix>``）直查，未命中再递归
    rglob。返回第一个命中；都没有就返回 None 并打 warning。

    用于替代历史上写死的 ``skill_dir.parent.parent / "data"`` 反推路径。
    那条 v1 假设在轨迹搬到 Registry 注册任意目录后已失效，会让
    eval / candidate / SWE-bench 收集等多处静默拿不到源轨迹。
    """
    filename = f"{traj_id}{suffix}"
    watch_dirs = list_watch_dirs(db_path=db_path)
    if not watch_dirs:
        logger.warning(
            "find_traj_file(%s): no watch dirs registered; "
            "run `xskill registry add <path>` to register a trajectory directory",
            filename,
        )
        return None
    searched: list[str] = []
    for wd in watch_dirs:
        wd_path = Path(wd["path"])
        if not wd_path.is_dir():
            continue
        searched.append(str(wd_path))
        direct = wd_path / filename
        if direct.is_file():
            return direct
        for hit in wd_path.rglob(filename):
            return hit
    logger.warning(
        "find_traj_file(%s): not found in any registered watch dir "
        "(searched %d dir(s): %s)",
        filename, len(searched), ", ".join(searched) or "(none reachable)",
    )
    return None


# ---------------------------------------------------------------------------
# Status management
# ---------------------------------------------------------------------------

_NOW = "datetime('now')"


def update_traj_status(
    watch_dir_id: int,
    filename: str,
    status: str,
    *,
    process_action: str | None = None,
    skill_generated: str | None = None,
    ux_score: float | None = None,
    error_msg: str | None = None,
    retry_count: int | None = None,
    db_path: Optional[Path] = None,
) -> None:
    """更新轨迹状态及关联字段。

    ``retry_count`` 显式传入时覆盖列上的值——cluster 阶段 partial-fail
    会算好"重试次数 + 1"再回写，沿着 ``retry_count < max_retries``
    继续重试，超过门槛后兜底标 done + WARNING。
    """
    conn = get_connection(db_path)
    try:
        sets = ["updated_at=datetime('now')"]
        vals: list = []
        if status is not None:
            sets.append("status=?")
            vals.append(status)
        if process_action is not None:
            sets.append("process_action=?")
            vals.append(process_action)
        if skill_generated is not None:
            sets.append("skill_generated=?")
            vals.append(skill_generated)
        if ux_score is not None:
            sets.append("ux_score=?")
            vals.append(ux_score)
        if error_msg is not None:
            sets.append("error_msg=?")
            vals.append(error_msg)
        if retry_count is not None:
            sets.append("retry_count=?")
            vals.append(int(retry_count))
        vals.extend([watch_dir_id, filename])
        conn.execute(
            f"UPDATE trajectories SET {', '.join(sets)}"
            " WHERE watch_dir_id=? AND filename=?",
            vals,
        )
        conn.commit()
    finally:
        conn.close()


def get_traj_retry_count(
    watch_dir_id: int, filename: str, *, db_path: Optional[Path] = None,
) -> int:
    """返回 ``trajectories.retry_count``。行不存在 / 列为 NULL → 0。

    cluster partial-fail 重试用：先读当前 retry_count，+1 后回写
    ``update_traj_status(..., retry_count=N+1)``。和 ``increment_retry``
    的差异是这里**只读不写**，由调用方决定何时 +1。
    """
    conn = get_connection(db_path)
    try:
        row = conn.execute(
            "SELECT retry_count FROM trajectories"
            " WHERE watch_dir_id=? AND filename=?",
            (watch_dir_id, filename),
        ).fetchone()
        if row is None:
            return 0
        return int(row["retry_count"] or 0)
    finally:
        conn.close()


def update_traj_log(
    watch_dir_id: int,
    filename: str,
    log_json: str,
    *,
    db_path: Optional[Path] = None,
) -> None:
    """Store process log (JSON string) for a trajectory."""
    conn = get_connection(db_path)
    try:
        conn.execute(
            "UPDATE trajectories SET process_log=?, updated_at=datetime('now')"
            " WHERE watch_dir_id=? AND filename=?",
            (log_json, watch_dir_id, filename),
        )
        conn.commit()
    finally:
        conn.close()


def get_traj_log(
    watch_dir_id: int,
    filename: str,
    *,
    db_path: Optional[Path] = None,
) -> str | None:
    """Retrieve the stored process log JSON for a trajectory."""
    conn = get_connection(db_path)
    try:
        row = conn.execute(
            "SELECT process_log FROM trajectories WHERE watch_dir_id=? AND filename=?",
            (watch_dir_id, filename),
        ).fetchone()
        return row["process_log"] if row else None
    finally:
        conn.close()


def update_traj_offset(
    watch_dir_id: int,
    filename: str,
    *,
    last_offset: int,
    last_atom_id: str | None,
    tasks_extracted: int,
    db_path: Optional[Path] = None,
) -> None:
    """更新轨迹的 AtomTask 增量进度指针。

    watcher 每次跑完 TaskAgent 后调，让下次 scan 用最新的 offset 决定 delta。
    ``last_atom_id`` 为 None 表示当前轨迹还没切出任何 atom（罕见——通常拆出
    至少 1 个）。
    """
    conn = get_connection(db_path)
    try:
        conn.execute(
            "UPDATE trajectories SET last_offset=?, last_atom_id=?, "
            "tasks_extracted=?, updated_at=datetime('now')"
            " WHERE watch_dir_id=? AND filename=?",
            (int(last_offset), last_atom_id, int(tasks_extracted),
             watch_dir_id, filename),
        )
        conn.commit()
    finally:
        conn.close()


def get_trajs_by_status(
    watch_dir_id: int,
    status: str,
    *,
    limit: int = 0,
    max_retries: int = 3,
    db_path: Optional[Path] = None,
) -> list[str]:
    """按状态查询文件名。error 状态自动过滤超过 max_retries 的。"""
    conn = get_connection(db_path)
    try:
        sql = "SELECT filename FROM trajectories WHERE watch_dir_id=? AND status=?"
        params: list = [watch_dir_id, status]
        if status == "error":
            sql += " AND retry_count < ?"
            params.append(max_retries)
        sql += " ORDER BY filename"
        if limit > 0:
            sql += f" LIMIT {limit}"
        rows = conn.execute(sql, params).fetchall()
        return [r["filename"] for r in rows]
    finally:
        conn.close()


def increment_retry(
    watch_dir_id: int, filename: str, *, db_path: Optional[Path] = None
) -> None:
    conn = get_connection(db_path)
    try:
        conn.execute(
            "UPDATE trajectories SET retry_count = retry_count + 1"
            " WHERE watch_dir_id=? AND filename=?",
            (watch_dir_id, filename),
        )
        conn.commit()
    finally:
        conn.close()


def get_status_counts(
    watch_dir_id: int | None = None, *, db_path: Optional[Path] = None
) -> dict[str, int]:
    """返回各状态的轨迹数量。watch_dir_id=None 时统计全部。"""
    conn = get_connection(db_path)
    try:
        if watch_dir_id is not None:
            rows = conn.execute(
                "SELECT status, COUNT(*) as cnt FROM trajectories"
                " WHERE watch_dir_id=? GROUP BY status",
                (watch_dir_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT status, COUNT(*) as cnt FROM trajectories GROUP BY status"
            ).fetchall()
        return {r["status"]: r["cnt"] for r in rows}
    finally:
        conn.close()


# =============================================================================
# Registry 实体类 —— 包装上面的模块函数为 OOP 接口
# =============================================================================
# 所有 watch_dir + trajectory 反查走这个类。


class Registry:
    """监听目录注册表 + 轨迹处理状态查询。

    数据存于 ~/.xskill/registry.db。所有方法直接代理本模块函数；
    本类只负责 Pythonic 接口与 dataclass 包装。
    """

    def __init__(self, db_path: Optional[Path] = None):
        self._db_path = db_path  # None = 用 config 默认

    # ─── watch_dir 管理 ───────────────────────────────────────────
    def add(self, path: str | Path, label: str = "",
            ecosystem: str = "manual") -> WatchDir:
        p = Path(path).expanduser().resolve()
        if not p.is_dir():
            raise NotADirectoryError(f"not a directory: {p}")
        register_dir(p, label=label, ecosystem=ecosystem,
                     db_path=self._db_path)
        row = get_watch_dir(p, db_path=self._db_path)
        if not row:
            raise RuntimeError(f"register_dir succeeded but row missing: {p}")
        return self._row_to_watch_dir(row, traj_count=0, indexed_count=0)

    def remove(self, path: str | Path) -> bool:
        p = Path(path).expanduser().resolve()
        return unregister_dir(p, db_path=self._db_path)

    def list(self) -> list[WatchDir]:
        rows = list_watch_dirs(db_path=self._db_path)
        return [self._row_to_watch_dir(r) for r in rows]

    def get(self, path: str | Path) -> Optional[WatchDir]:
        p = Path(path).expanduser().resolve()
        row = get_watch_dir(p, db_path=self._db_path)
        return self._row_to_watch_dir(row) if row else None

    @staticmethod
    def _row_to_watch_dir(row: dict, **overrides) -> WatchDir:
        return WatchDir(
            id=row["id"],
            path=Path(row["path"]),
            label=row.get("label", ""),
            auto_index=bool(row.get("auto_index", 1)),
            traj_count=overrides.get("traj_count", row.get("traj_count", 0)),
            indexed_count=overrides.get("indexed_count", row.get("indexed_count", 0)),
            ecosystem=row.get("ecosystem", "manual"),
        )

    # ─── trajectory 反查 ────────────────────────────────────────
    def trajectory_status(self, traj_path: str | Path) -> Optional[dict]:
        """返回某条 traj 在 trajectories 表里的全部字段（含 skill_used / canary_side / ux_score）。
        未找到返回 None。"""
        traj_path = Path(traj_path).resolve()
        wd_path = str(traj_path.parent)
        conn = get_connection(self._db_path)
        try:
            row = conn.execute(
                "SELECT t.* FROM trajectories t "
                "JOIN watch_dirs w ON t.watch_dir_id = w.id "
                "WHERE w.path = ? AND t.filename = ?",
                (wd_path, traj_path.name),
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def trajectories_using(self, skill_name: str) -> list[Path]:
        """反查：曾用过某个 skill 的所有轨迹路径。
        skill_used 字段是逗号分隔，故 LIKE 匹配。"""
        conn = get_connection(self._db_path)
        try:
            rows = conn.execute(
                "SELECT w.path AS wd_path, t.filename "
                "FROM trajectories t JOIN watch_dirs w ON t.watch_dir_id=w.id "
                "WHERE t.skill_used = ? OR t.skill_used LIKE ? "
                "   OR t.skill_used LIKE ? OR t.skill_used LIKE ?",
                (skill_name,
                 f"{skill_name},%",
                 f"%,{skill_name}",
                 f"%,{skill_name},%"),
            ).fetchall()
            return [Path(r["wd_path"]) / r["filename"] for r in rows]
        finally:
            conn.close()
