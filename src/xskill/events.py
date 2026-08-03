"""events.py — P3-3.1 事件流（D7:既有事实源的消费者,不引入新用户动作）
====================================================================

四类事件,全部由既有事实源在落盘点顺手发出:

- ``feedback``  他人触发我贡献的 skill + atom ux 打分即评价
                （runner 打分落盘点,按 traj 去重——同一轨迹多 atom 命中同一
                skill 只发一条,唯一索引 ``idx_events_feedback_dedup`` 兜底）
- ``push_edit`` client 手改 skill 推 ``user-staging/<client_id>`` 分支
                即修改意见（team server push-edit 端点）
- ``canary``    灰度裁决 promoted/rejected/timeout_discarded（canary 落盘点）
- ``pin``       skill 被 pin（控制面写入点;只记 pin,block 不是社交事件）

**扇出规则**（D7 评审采纳）:通知发给该 skill 累计 weightscore ≥
``CONTRIBUTOR_MIN_WEIGHT`` 的贡献者;本人触发本人贡献的 skill 不通知
（actor 从 targets 排除）。无人可通知的事件仍入库——世界消息 feed 要展示。

**旁路 telemetry 语义**:所有 ``emit_*`` 调用点都包 try/except——事件发送
失败绝不阻断打分/推送/裁决主链路（与 ``record_canary_decision`` 同款约定,
呼应"后台链路绝不阻塞"的既有裁决）。

已读状态:每用户一个游标（``event_reads.last_read_id``）,未读数 =
targets 命中且 id > 游标的行数。不做逐行 read 标记。
"""
from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Optional

from xskill.pipeline.registry import pooled_connection

logger = logging.getLogger("xskill.events")

# D7 扇出阈值:贡献者在该 skill 的累计 weightscore(atom_adoption 求和,
# 单 atom 量纲 1-10)达到该值才收通知——过滤 ws=1 的琐碎贡献者。
CONTRIBUTOR_MIN_WEIGHT = 3

# ux 分数段 → 评价措辞(3.3 口径;score_atom 量纲 1-10)
UX_GOOD_MIN = 7
UX_BAD_MAX = 4


def ux_band(score: float) -> str:
    """ux 分数段 → 好评/一般/差劲(通知与世界消息的语义徽章共用口径)。"""
    if score >= UX_GOOD_MIN:
        return "好评"
    if score <= UX_BAD_MAX:
        return "差劲"
    return "一般"


def _traj_of_atom(atom_id: str) -> str:
    """``atom_<traj_id>_NNNN`` → traj_id。不合式返回空串。"""
    if not atom_id.startswith("atom_"):
        return ""
    stem, _, idx = atom_id[len("atom_"):].rpartition("_")
    return stem if stem and idx.isdigit() else ""


_TRAJ_USER_TTL_SECONDS = 5.0
_traj_user_cache: dict[str, tuple[float, dict[str, str]]] = {}
_traj_user_lock = threading.Lock()


def _traj_user_map(db_path: Optional[Path] = None) -> dict[str, str]:
    """filename stem → user_key（5s TTL + 单飞）。

    本映射要全表扫 ``trajectories``，而「我的」页一次首屏会经多个端点
    各自调到这里；短窗缓存把一波请求收敛成一次扫描，锁内构建保证到期
    瞬间只有一个线程真扫。返回的是缓存内共享 dict，调用方只读不改写。
    """
    key = str(db_path or "")
    with _traj_user_lock:
        hit = _traj_user_cache.get(key)
        if hit and hit[0] > time.monotonic():
            return hit[1]
        if len(_traj_user_cache) > 8:
            _traj_user_cache.clear()
        with pooled_connection(db_path) as conn:
            mapping = {
                (r["filename"][:-3] if r["filename"].endswith(".md")
                 else r["filename"]): (r["user_key"] or "")
                for r in conn.execute(
                    "SELECT filename, user_key FROM trajectories").fetchall()
            }
        _traj_user_cache[key] = (
            time.monotonic() + _TRAJ_USER_TTL_SECONDS, mapping,
        )
        return mapping


def skill_contributors(skill: str, *, min_weight: int = CONTRIBUTOR_MIN_WEIGHT,
                       db_path: Optional[Path] = None) -> dict[str, int]:
    """某 skill 的贡献者 → 累计 weightscore(仅 ≥ min_weight 且 user_key 非空)。

    贡献关系 = ``atom_adoption``(atom 被聚进 skill) 经 atom_id 内嵌的
    traj_id 归到 ``trajectories.user_key``(D5 canonical 身份键)。
    """
    traj_user = _traj_user_map(db_path)
    with pooled_connection(db_path) as conn:
        rows = conn.execute(
            "SELECT atom_id, weightscore FROM atom_adoption WHERE skill=?",
            (skill,),
        ).fetchall()
    weights: dict[str, int] = {}
    for r in rows:
        user = traj_user.get(_traj_of_atom(r["atom_id"] or ""), "")
        if not user:
            continue
        weights[user] = weights.get(user, 0) + int(r["weightscore"] or 0)
    return {u: w for u, w in weights.items() if w >= min_weight}


def _producer_from_by_user(by_user: dict[str, set[str]]) -> Optional[dict]:
    if not by_user:
        return None
    user, trajs = max(by_user.items(), key=lambda kv: (len(kv[1]), kv[0]))
    return {"user": user, "traj_count": len(trajs)}


def skill_main_producer(skill: str, *,
                        db_path: Optional[Path] = None,
                        traj_user: Optional[dict[str, str]] = None,
                        ) -> Optional[dict]:
    """蒸馏资产主要贡献人：对该 skill 贡献来源轨迹数最多的 user_key。

    返回 ``{"user": str, "traj_count": int}``；无人贡献时返回 ``None``。
    批量场景请用 ``skill_main_producers``，避免反复全表扫 trajectories。
    """
    return skill_main_producers([skill], db_path=db_path,
                                traj_user=traj_user).get(skill)


def skill_main_producers(skills,
                         *,
                         db_path: Optional[Path] = None,
                         traj_user: Optional[dict[str, str]] = None,
                         ) -> dict[str, dict]:
    """批量主要贡献人：整次请求只扫一次 trajectories + 一次 IN 查询 adoption。

    返回 ``{skill: {"user", "traj_count"}}``；无贡献的 skill 不出现在结果里。
    """
    names = [s for s in dict.fromkeys(skills or ()) if s]
    if not names:
        return {}
    if traj_user is None:
        traj_user = _traj_user_map(db_path)
    placeholders = ",".join("?" * len(names))
    with pooled_connection(db_path) as conn:
        rows = conn.execute(
            f"SELECT skill, atom_id FROM atom_adoption"
            f" WHERE skill IN ({placeholders})",
            names,
        ).fetchall()
    by_skill: dict[str, dict[str, set[str]]] = {}
    for r in rows:
        traj = _traj_of_atom(r["atom_id"] or "")
        user = traj_user.get(traj, "") if traj else ""
        if not user or not traj:
            continue
        by_skill.setdefault(r["skill"], {}).setdefault(user, set()).add(traj)
    return {
        skill: prod
        for skill, by_user in by_skill.items()
        if (prod := _producer_from_by_user(by_user)) is not None
    }


class EventStore:
    """``events`` / ``event_targets`` / ``event_reads`` 三表的读写(registry.db)。"""

    def __init__(self, db_path: Optional[Path] = None):
        self._db_path = db_path

    # ── 写入 ─────────────────────────────────────────────────────

    def emit(self, *, kind: str, actor: str = "", skill: str = "",
             traj_id: str = "", payload: Optional[dict] = None,
             targets: tuple[str, ...] | list[str] = ()) -> Optional[int]:
        """插入一条事件 + 其通知对象。actor 一律从 targets 排除(D7)。

        feedback 命中 (skill, traj_id) 去重索引时返回 None(已发过,不重发)。
        """
        with pooled_connection(self._db_path) as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO events(kind,actor,skill,traj_id,payload)"
                " VALUES(?,?,?,?,?)",
                (kind, actor, skill, traj_id,
                 json.dumps(payload or {}, ensure_ascii=False)),
            )
            if cur.rowcount == 0:
                return None
            event_id = cur.lastrowid
            for user in dict.fromkeys(targets):  # 去重保序
                if user and user != actor:
                    conn.execute(
                        "INSERT OR IGNORE INTO event_targets(event_id,user_key)"
                        " VALUES(?,?)", (event_id, user))
            conn.commit()
            return event_id

    def emit_feedback(self, *, actor: str, skill: str, traj_id: str,
                      score_avg: float, n_atoms: int, side: str,
                      sha: str = "") -> Optional[int]:
        """他人触发+打分即评价。targets=该 skill 的达阈值贡献者。"""
        return self.emit(
            kind="feedback", actor=actor, skill=skill, traj_id=traj_id,
            payload={"score_avg": round(float(score_avg), 2),
                     "n_atoms": int(n_atoms), "band": ux_band(score_avg),
                     "side": side, "sha": sha},
            targets=tuple(skill_contributors(skill, db_path=self._db_path)),
        )

    def emit_push_edit(self, *, actor: str, skill: str, branch: str,
                       ref_sha: str) -> Optional[int]:
        """client 手改推分支即修改意见。payload 带分支引用,前端可点开 diff。"""
        return self.emit(
            kind="push_edit", actor=actor, skill=skill,
            payload={"branch": branch, "ref_sha": ref_sha},
            targets=tuple(skill_contributors(skill, db_path=self._db_path)),
        )

    def emit_canary(self, *, skill: str, action: str, main_avg: float,
                    staging_avg: float) -> Optional[int]:
        """灰度裁决。actor 为空(系统动作)。"""
        return self.emit(
            kind="canary", skill=skill,
            payload={"action": action, "main_avg": round(float(main_avg), 2),
                     "staging_avg": round(float(staging_avg), 2)},
            targets=tuple(skill_contributors(skill, db_path=self._db_path)),
        )

    def emit_pin(self, *, actor: str, skill: str, target_user: str,
                 scope: str) -> Optional[int]:
        """skill 被 pin。贡献者收通知;admin 代 pin 时被配置的用户也收。"""
        targets = list(skill_contributors(skill, db_path=self._db_path))
        if target_user and not target_user.startswith("*"):
            targets.append(target_user)
        return self.emit(
            kind="pin", actor=actor, skill=skill,
            payload={"target_user": target_user, "scope": scope},
            targets=tuple(targets),
        )

    # ── 查询 ─────────────────────────────────────────────────────

    @staticmethod
    def _row_to_event(row) -> dict:
        return {"id": row["id"], "ts": row["ts"], "kind": row["kind"],
                "actor": row["actor"], "skill": row["skill"],
                "traj_id": row["traj_id"],
                "payload": json.loads(row["payload"] or "{}")}

    def world_feed(self, *, limit: int = 50,
                   before_id: Optional[int] = None) -> list[dict]:
        """世界消息:全部事件,最新在前(Q6:登录可见;只读实例不挂该路由)。"""
        with pooled_connection(self._db_path) as conn:
            sql = "SELECT * FROM events"
            params: list = []
            if before_id is not None:
                sql += " WHERE id < ?"
                params.append(int(before_id))
            sql += " ORDER BY id DESC LIMIT ?"
            params.append(max(1, min(int(limit), 200)))
            return [self._row_to_event(r)
                    for r in conn.execute(sql, params).fetchall()]

    def for_user(self, user_key: str, *, limit: int = 50,
                 before_id: Optional[int] = None) -> list[dict]:
        """发给我的通知,最新在前,带 ``read`` 标记(id ≤ 游标 = 已读)。"""
        with pooled_connection(self._db_path) as conn:
            cursor_id = self._last_read_id(conn, user_key)
            sql = ("SELECT e.* FROM events e"
                   " JOIN event_targets t ON t.event_id=e.id"
                   " WHERE t.user_key=?")
            params: list = [user_key]
            if before_id is not None:
                sql += " AND e.id < ?"
                params.append(int(before_id))
            sql += " ORDER BY e.id DESC LIMIT ?"
            params.append(max(1, min(int(limit), 200)))
            out = []
            for r in conn.execute(sql, params).fetchall():
                ev = self._row_to_event(r)
                ev["read"] = ev["id"] <= cursor_id
                out.append(ev)
            return out

    def unread_count(self, user_key: str) -> int:
        with pooled_connection(self._db_path) as conn:
            cursor_id = self._last_read_id(conn, user_key)
            return conn.execute(
                "SELECT COUNT(*) FROM event_targets t"
                " JOIN events e ON e.id=t.event_id"
                " WHERE t.user_key=? AND e.id>?",
                (user_key, cursor_id),
            ).fetchone()[0]

    def mark_read(self, user_key: str, last_id: int) -> None:
        """推进已读游标(只前进不后退——重复/乱序标记幂等)。"""
        with pooled_connection(self._db_path) as conn:
            conn.execute(
                "INSERT INTO event_reads(user_key,last_read_id) VALUES(?,?)"
                " ON CONFLICT(user_key) DO UPDATE SET"
                " last_read_id=MAX(last_read_id, excluded.last_read_id)",
                (user_key, int(last_id)),
            )
            conn.commit()

    @staticmethod
    def _last_read_id(conn, user_key: str) -> int:
        row = conn.execute(
            "SELECT last_read_id FROM event_reads WHERE user_key=?",
            (user_key,),
        ).fetchone()
        return int(row["last_read_id"]) if row else 0
