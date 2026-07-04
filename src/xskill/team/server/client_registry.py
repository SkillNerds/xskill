"""client_registry.py — team server 的 client 注册表（SP1）

server 需要持久化的只有三样：client 注册表、skill git 仓、汇聚的
ux_score 明细。这个文件是第一样。

client_id 是 server 生成的 uuid——它同时是 ① canary 分桶 key（喂
CanaryRouter.assign）② 上传轨迹的落盘分桶（clients/<client_id>/sessions/）③
手改分支命名（user-staging/<client_id>）。
"""
from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS clients (
    client_id  TEXT PRIMARY KEY,
    label      TEXT DEFAULT '',
    hostname   TEXT DEFAULT '',
    joined_at  TEXT NOT NULL,
    last_seen  TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ClientRegistry:
    """SQLite 支撑的 client 注册表。每次操作开新连接（规模小，几十个 client）。"""

    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        conn = self._conn()
        try:
            conn.executescript(_SCHEMA)
            conn.commit()
        finally:
            conn.close()

    def register(
        self, *,
        label: str = "",
        hostname: str = "",
        claimed_client_id: str | None = None,
    ) -> str:
        """注册或续用 client_id。

        三级优先级（显式判定，非 fallback）：
          ① client 自报 ``claimed_client_id`` 且 server DB 里还认得 → 续用，
             touch last_seen。覆盖 ``xskill connect <addr> --token`` 带参重连
             场景：本地 ``team_client.json`` 已存 client_id，不该换。
          ② client 没自报 / 自报的 server 不认得，但 (hostname, label) 指纹
             能查到唯一历史身份 → 续用。覆盖 state 文件丢失（重装、清家目录）
             但 server DB 还在的场景，让灰度/归属链路自愈。
          ③ 以上都不行 → 发新 uuid 入库。

        指纹查找仅在 hostname 或 label 至少一个非空时启用，防止匿名 client
        互相误匹配。
        """
        # 优先级 ① — claimed_client_id 命中
        if claimed_client_id and self.exists(claimed_client_id):
            self.touch(claimed_client_id)
            return claimed_client_id
        # 优先级 ② — (hostname, label) 指纹回查
        existing = self._find_by_fingerprint(hostname=hostname, label=label)
        if existing:
            self.touch(existing)
            return existing
        # 优先级 ③ — 发新 uuid
        client_id = uuid.uuid4().hex
        now = _now()
        conn = self._conn()
        try:
            conn.execute(
                "INSERT INTO clients (client_id, label, hostname, joined_at, last_seen)"
                " VALUES (?, ?, ?, ?, ?)",
                (client_id, label, hostname, now, now),
            )
            conn.commit()
        finally:
            conn.close()
        return client_id

    def _find_by_fingerprint(
        self, *, hostname: str, label: str,
    ) -> str | None:
        """按 (hostname, label) 查唯一历史身份。

        - hostname 和 label **同时为空** → 直接返回 None（不让匿名 client
          误匹配上历史空记录）。
        - 命中多条 → 返回 last_seen 最新的那条（最贴近"同一台机器最近的
          身份"语义）。
        - 没命中 → None。
        """
        if not hostname and not label:
            return None
        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT client_id FROM clients"
                " WHERE hostname=? AND label=?"
                " ORDER BY last_seen DESC LIMIT 1",
                (hostname, label),
            ).fetchone()
            return row["client_id"] if row else None
        finally:
            conn.close()

    def exists(self, client_id: str) -> bool:
        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT 1 FROM clients WHERE client_id=?", (client_id,)
            ).fetchone()
            return row is not None
        finally:
            conn.close()

    def touch(self, client_id: str) -> None:
        """更新 last_seen。client_id 不存在则静默 no-op。"""
        conn = self._conn()
        try:
            conn.execute(
                "UPDATE clients SET last_seen=? WHERE client_id=?",
                (_now(), client_id),
            )
            conn.commit()
        finally:
            conn.close()

    def get(self, client_id: str) -> dict | None:
        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT * FROM clients WHERE client_id=?", (client_id,)
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def list(self) -> list[dict]:
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT * FROM clients ORDER BY joined_at"
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()
