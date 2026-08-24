"""skills_catalog → 向量索引的持久化增量队列。"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Optional

from xskill.pipeline.registry import pooled_connection


def mark_catalog_vector_dirty_on_connection(
    conn,
    catalog_key: str,
    *,
    operation: str,
    content_sha: str = "",
    marked_at: float | None = None,
) -> None:
    """在调用方事务中合并一个目标状态，并递增 generation。"""
    if operation not in {"upsert", "delete"}:
        raise ValueError(f"invalid catalog vector operation: {operation!r}")
    conn.execute(
        """
        INSERT INTO catalog_vector_dirty(
            catalog_key, generation, dirty, operation, content_sha, marked_at
        ) VALUES (?, 1, 1, ?, ?, ?)
        ON CONFLICT(catalog_key) DO UPDATE SET
            generation=catalog_vector_dirty.generation + 1,
            dirty=1,
            operation=excluded.operation,
            content_sha=excluded.content_sha,
            marked_at=excluded.marked_at
        """,
        (catalog_key, operation, content_sha, time.time() if marked_at is None else marked_at),
    )


def list_catalog_vector_dirty(
    *,
    db_path: Optional[Path] = None,
    limit: int = 256,
) -> list[dict]:
    with pooled_connection(db_path) as conn:
        rows = conn.execute(
            """
            SELECT catalog_key, generation, operation, content_sha, marked_at
            FROM catalog_vector_dirty
            WHERE dirty=1
            ORDER BY marked_at, catalog_key
            LIMIT ?
            """,
            (max(1, int(limit)),),
        ).fetchall()
    return [dict(row) for row in rows]


def list_all_catalog_vector_generations(
    *, db_path: Optional[Path] = None,
) -> dict[str, int]:
    with pooled_connection(db_path) as conn:
        rows = conn.execute(
            """
            SELECT catalog_key, generation FROM catalog_vector_dirty
            WHERE dirty=1
            """
        ).fetchall()
    return {row["catalog_key"]: int(row["generation"]) for row in rows}


def catalog_vector_event_is_current(
    catalog_key: str,
    generation: int,
    *,
    db_path: Optional[Path] = None,
) -> bool:
    with pooled_connection(db_path) as conn:
        row = conn.execute(
            """
            SELECT generation FROM catalog_vector_dirty
            WHERE catalog_key=? AND dirty=1
            """,
            (catalog_key,),
        ).fetchone()
    return row is not None and int(row["generation"]) == int(generation)


def clear_catalog_vector_dirty(
    catalog_key: str,
    generation: int,
    *,
    db_path: Optional[Path] = None,
) -> bool:
    """只确认观察到的 generation；晚到更新不会被旧 worker 删除。"""
    with pooled_connection(db_path) as conn:
        cursor = conn.execute(
            """
            UPDATE catalog_vector_dirty SET dirty=0
            WHERE catalog_key=? AND generation=? AND dirty=1
            """,
            (catalog_key, int(generation)),
        )
        conn.commit()
        return cursor.rowcount > 0


def finish_catalog_vector_reconcile(
    generations: dict[str, int],
    *,
    model_fingerprint: str,
    reconciled_at: float | None = None,
    db_path: Optional[Path] = None,
) -> None:
    """提交全量对账水位，并按 generation 清理开始时观察到的事件。"""
    with pooled_connection(db_path) as conn:
        for catalog_key, generation in generations.items():
            conn.execute(
                "UPDATE catalog_vector_dirty SET dirty=0 "
                "WHERE catalog_key=? AND generation=? AND dirty=1",
                (catalog_key, int(generation)),
            )
        conn.execute(
            """
            INSERT INTO catalog_vector_sync_meta(
                singleton, model_fingerprint, reconciled_at
            ) VALUES (1, ?, ?)
            ON CONFLICT(singleton) DO UPDATE SET
                model_fingerprint=excluded.model_fingerprint,
                reconciled_at=excluded.reconciled_at
            """,
            (
                model_fingerprint,
                time.time() if reconciled_at is None else reconciled_at,
            ),
        )
        conn.commit()


def catalog_vector_reconcile_reason(
    model_fingerprint: str,
    *,
    db_path: Optional[Path] = None,
    now: float | None = None,
    interval_seconds: float = 24 * 60 * 60,
) -> str:
    """返回 bootstrap/model/periodic；空串表示本轮走增量。"""
    with pooled_connection(db_path) as conn:
        row = conn.execute(
            """
            SELECT model_fingerprint, reconciled_at
            FROM catalog_vector_sync_meta WHERE singleton=1
            """
        ).fetchone()
    if row is None:
        return "bootstrap"
    if (row["model_fingerprint"] or "") != model_fingerprint:
        return "model_changed"
    current = time.time() if now is None else float(now)
    if current - float(row["reconciled_at"] or 0) >= interval_seconds:
        return "periodic"
    return ""
