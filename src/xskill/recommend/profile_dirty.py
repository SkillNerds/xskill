"""持久化用户画像脏队列；Atom JSON 是事实源，本表只保存待刷新事件。"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Iterable, Optional

from xskill.pipeline.registry import pooled_connection

PROFILE_ALGORITHM_VERSION = "profile-v1"
DEFAULT_RECONCILE_INTERVAL_SECONDS = 24 * 60 * 60


def mark_profile_dirty_on_connection(
    connection,
    user_key: str,
    *,
    reason: str = "",
    marked_at: float | None = None,
) -> int:
    """在调用方事务内合并事件；用于 Atom 删除与 registry 状态原子提交。"""
    key = str(user_key or "").strip()
    if not key:
        return 0
    timestamp = time.time() if marked_at is None else float(marked_at)
    connection.execute(
        """
        INSERT INTO profile_dirty(user_key, generation, reason, marked_at)
        VALUES (?, 1, ?, ?)
        ON CONFLICT(user_key) DO UPDATE SET
            generation=profile_dirty.generation + 1,
            reason=excluded.reason,
            marked_at=excluded.marked_at
        """,
        (key, str(reason or ""), timestamp),
    )
    row = connection.execute(
        "SELECT generation FROM profile_dirty WHERE user_key=?",
        (key,),
    ).fetchone()
    return int(row["generation"])


def profile_user_key_for_store_root(root: Path | str) -> str:
    """识别 ``clients/<user>/sessions`` team store；本地 watch dir 返回空。"""
    path = Path(root)
    if (
        path.name != "sessions"
        or path.parent.parent.name != "clients"
        or not path.parent.name
    ):
        return ""
    return path.parent.name


def mark_profile_dirty(
    user_key: str,
    *,
    reason: str = "",
    db_path: Optional[Path] = None,
    marked_at: float | None = None,
) -> int:
    """合并一次画像变化并返回新 generation。"""
    key = str(user_key or "").strip()
    if not key:
        return 0
    with pooled_connection(db_path) as conn:
        generation = mark_profile_dirty_on_connection(
            conn,
            key,
            reason=reason,
            marked_at=marked_at,
        )
        conn.commit()
    return generation


def mark_profile_dirty_for_store(
    root: Path | str,
    *,
    reason: str,
    db_path: Optional[Path] = None,
) -> int:
    return mark_profile_dirty(
        profile_user_key_for_store_root(root),
        reason=reason,
        db_path=db_path,
    )


def list_dirty_profiles(
    *,
    limit: int = 0,
    db_path: Optional[Path] = None,
) -> list[dict]:
    sql = (
        "SELECT user_key, generation, reason, marked_at FROM profile_dirty "
        "ORDER BY marked_at ASC, user_key ASC"
    )
    params: tuple = ()
    if limit > 0:
        sql += " LIMIT ?"
        params = (int(limit),)
    with pooled_connection(db_path) as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(row) for row in rows]


def clear_profile_dirty(
    user_key: str,
    generation: int,
    *,
    db_path: Optional[Path] = None,
) -> bool:
    """仅清理已处理的 generation；晚到事件已递增时保留新任务。"""
    with pooled_connection(db_path) as conn:
        cursor = conn.execute(
            "DELETE FROM profile_dirty WHERE user_key=? AND generation=?",
            (user_key, int(generation)),
        )
        conn.commit()
        return cursor.rowcount == 1


def reconcile_profile_dirty(
    user_keys: Iterable[str],
    *,
    input_fingerprint: str,
    db_path: Optional[Path] = None,
    now: float | None = None,
    interval_seconds: float = DEFAULT_RECONCILE_INTERVAL_SECONDS,
) -> str:
    """首次运行、输入版本变化或低频水位到期时，把全部现存用户标脏。"""
    timestamp = time.time() if now is None else float(now)
    keys = sorted({str(key).strip() for key in user_keys if str(key).strip()})
    with pooled_connection(db_path) as conn:
        row = conn.execute(
            """
            SELECT input_fingerprint, reconciled_at
            FROM profile_refresh_meta WHERE singleton=1
            """
        ).fetchone()
        if row is None:
            reason = "bootstrap"
        elif row["input_fingerprint"] != input_fingerprint:
            reason = "profile_input_changed"
        elif timestamp - float(row["reconciled_at"] or 0) >= interval_seconds:
            reason = "periodic_reconcile"
        else:
            return ""

        conn.executemany(
            """
            INSERT INTO profile_dirty(user_key, generation, reason, marked_at)
            VALUES (?, 1, ?, ?)
            ON CONFLICT(user_key) DO UPDATE SET
                generation=profile_dirty.generation + 1,
                reason=excluded.reason,
                marked_at=excluded.marked_at
            """,
            [(key, reason, timestamp) for key in keys],
        )
        conn.execute(
            """
            INSERT INTO profile_refresh_meta(
                singleton, input_fingerprint, reconciled_at
            ) VALUES (1, ?, ?)
            ON CONFLICT(singleton) DO UPDATE SET
                input_fingerprint=excluded.input_fingerprint,
                reconciled_at=excluded.reconciled_at
            """,
            (input_fingerprint, timestamp),
        )
        conn.commit()
    return reason
