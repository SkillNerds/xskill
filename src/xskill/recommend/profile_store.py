"""profile_store.py — §4 用户画像 SQLite 持久化

server 端按 ``user_id`` 持久化每个用户的 ``ClientInterest``（feature_tensor / mean_tensor）
与 ``used_skills``。tensor 以 pickle BLOB 存（numpy 数组）。client（瘦）不存画像。
"""
from __future__ import annotations

import json
import pickle
from datetime import datetime, timezone
from typing import Optional

import numpy as np

from xskill.recommend._sqlite_base import _SqliteStore

_SCHEMA = """
CREATE TABLE IF NOT EXISTS client_interest (
    user_id         TEXT PRIMARY KEY,
    feature_tensor  BLOB,
    mean_tensor     BLOB,
    used_skills     TEXT DEFAULT '[]',
    updated_at      TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ProfileStore(_SqliteStore):
    """``client_interest`` 表的读写。"""

    _SCHEMA = _SCHEMA

    def upsert(
        self,
        user_id: str,
        *,
        feature_tensor: Optional[np.ndarray],
        mean_tensor: Optional[np.ndarray],
        used_skills: list[dict],
    ) -> None:
        ft_blob = pickle.dumps(feature_tensor) if feature_tensor is not None else None
        mt_blob = pickle.dumps(mean_tensor) if mean_tensor is not None else None
        used_json = json.dumps(used_skills, ensure_ascii=False)
        conn = self._conn()
        try:
            conn.execute(
                "INSERT INTO client_interest (user_id, feature_tensor, mean_tensor, used_skills, updated_at)"
                " VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT(user_id) DO UPDATE SET"
                " feature_tensor=excluded.feature_tensor,"
                " mean_tensor=excluded.mean_tensor,"
                " used_skills=excluded.used_skills,"
                " updated_at=excluded.updated_at",
                (user_id, ft_blob, mt_blob, used_json, _now()),
            )
            conn.commit()
        finally:
            conn.close()

    def load(self, user_id: str) -> Optional[dict]:
        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT feature_tensor, mean_tensor, used_skills, updated_at"
                " FROM client_interest WHERE user_id=?",
                (user_id,),
            ).fetchone()
            if row is None:
                return None
            ft = pickle.loads(row["feature_tensor"]) if row["feature_tensor"] else None
            mt = pickle.loads(row["mean_tensor"]) if row["mean_tensor"] else None
            return {
                "user_id": user_id,
                "feature_tensor": ft,
                "mean_tensor": mt,
                "used_skills": json.loads(row["used_skills"] or "[]"),
                "updated_at": row["updated_at"],
            }
        finally:
            conn.close()

    def all_means(self) -> list[tuple[str, "np.ndarray"]]:
        """所有有画像用户的 ``(user_id, mean_tensor)``，供 find_friend 检索。"""
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT user_id, mean_tensor FROM client_interest WHERE mean_tensor IS NOT NULL"
            ).fetchall()
            out: list[tuple[str, np.ndarray]] = []
            for r in rows:
                if r["mean_tensor"]:
                    out.append((r["user_id"], pickle.loads(r["mean_tensor"])))
            return out
        finally:
            conn.close()
