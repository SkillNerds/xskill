"""reco_store.py — §5 推荐记录持久化（双向视图）

记录 ``(user_id, skill_name, side, sha)``，供：
- ``Skill.recommend_users[side]`` 反查（某 skill 某 side 被推给了哪些用户）
- ``ClientUser.recommended_skills`` 反查（某用户被推了哪些 skill/branch/hash）
"""
from __future__ import annotations

from datetime import datetime, timezone

from xskill.recommend._sqlite_base import _SqliteStore

RECO_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS recommendations (
    user_id     TEXT NOT NULL,
    skill_name  TEXT NOT NULL,
    side        TEXT NOT NULL,
    sha         TEXT NOT NULL,
    ts          TEXT NOT NULL,
    PRIMARY KEY (user_id, skill_name, side)
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class RecoStore(_SqliteStore):
    """``recommendations`` 表读写。与 ProfileStore 共用同一 db 文件。"""

    _SCHEMA = RECO_SCHEMA_SQL

    def record(self, *, user_id: str, skill_name: str, side: str, sha: str) -> None:
        """幂等 upsert 一条推荐记录。"""
        conn = self._conn()
        try:
            conn.execute(
                "INSERT INTO recommendations (user_id, skill_name, side, sha, ts)"
                " VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT(user_id, skill_name, side) DO UPDATE SET sha=excluded.sha, ts=excluded.ts",
                (user_id, skill_name, side, sha, _now()),
            )
            conn.commit()
        finally:
            conn.close()

    def users_for_skill(self, skill_name: str, side: str) -> list[str]:
        """``Skill.recommend_users[side]`` 视图。"""
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT user_id FROM recommendations WHERE skill_name=? AND side=?",
                (skill_name, side),
            ).fetchall()
            return [r["user_id"] for r in rows]
        finally:
            conn.close()

    def skills_for_user(self, user_id: str) -> list[dict]:
        """``ClientUser.recommended_skills`` 视图：``{skill, branch(=side), hash(=sha)}``。"""
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT skill_name, side, sha FROM recommendations WHERE user_id=?",
                (user_id,),
            ).fetchall()
            return [
                {"skill": r["skill_name"], "branch": r["side"], "hash": r["sha"]}
                for r in rows
            ]
        finally:
            conn.close()
