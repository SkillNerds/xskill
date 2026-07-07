"""client_registry.py — team server 的 client 注册表（SP1）

server 需要持久化的只有三样：client 注册表、skill git 仓、汇聚的
ux_score 明细。这个文件是第一样。

client_id 是 server 生成的 uuid——它同时是 ① canary 分桶 key（喂
pick_side）② 上传轨迹的落盘分桶（clients/<client_id>/sessions/）③
手改分支命名（user-staging/<client_id>）。
"""
from __future__ import annotations

import hashlib
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path


def _normalize_user_name(name: str) -> str:
    """规范化 user_name：去首尾空白、内部连续空白压一、转小写。

    规范化保证 ``--name Alice`` / ``--name alice`` / ``--name "  alice  "`` 派生
    出同一 client_id（跨设备/跨会话稳定身份）。空串抛 ValueError（fail-loud）。
    """
    if not isinstance(name, str):
        raise ValueError(
            f"user_name 必须是字符串，got {type(name).__name__}"
        )
    norm = re.sub(r"\s+", " ", name).strip().lower()
    if not norm:
        raise ValueError("user_name 不能为空")
    return norm


def client_id_from_name(name: str) -> str:
    """从 user_name 派生确定性 client_id：``sha256("name:" + norm)[:16]``。

    同 name（规范化后相同）→ 同 id；不发新 uuid。这是跨设备稳定身份的根基。
    """
    norm = _normalize_user_name(name)
    return hashlib.sha256(("name:" + norm).encode("utf-8")).hexdigest()[:16]


_SCHEMA = """
CREATE TABLE IF NOT EXISTS clients (
    client_id   TEXT PRIMARY KEY,
    label       TEXT DEFAULT '',
    hostname    TEXT DEFAULT '',
    user_name   TEXT DEFAULT '',
    joined_at   TEXT NOT NULL,
    last_seen   TEXT NOT NULL
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
            # 幂等迁移：CREATE TABLE IF NOT EXISTS 不会给已存在的表加列，
            # 老 db 缺 user_name 列时显式 ALTER 补上。
            cols = {row["name"] for row in conn.execute("PRAGMA table_info(clients)")}
            if "user_name" not in cols:
                conn.execute("ALTER TABLE clients ADD COLUMN user_name TEXT DEFAULT ''")
            conn.commit()
        finally:
            conn.close()

    def register(
        self, *,
        label: str = "",
        hostname: str = "",
        claimed_client_id: str | None = None,
        user_name: str | None = None,
    ) -> str:
        """注册或续用 client_id。

        身份解析优先级（显式判定，非 fallback）：
          ① ``user_name`` 非空 → 派生确定性 id（``client_id_from_name``），跨设备
             同 name 续用同一身份、touch last_seen。``--name`` 是权威身份键，
             **不**走 claimed/指纹路径。
          ② client 自报 ``claimed_client_id`` 且 server DB 里还认得 → 续用，
             touch last_seen。覆盖 ``xskill connect <addr> --token`` 带参重连
             场景：本地 ``team_client.json`` 已存 client_id，不该换。
          ③ client 没自报 / 自报的 server 不认得，但 (hostname, label) 指纹
             能查到唯一历史身份 → 续用。覆盖 state 文件丢失（重装、清家目录）
             但 server DB 还在的场景，让灰度/归属链路自愈。
          ④ 以上都不行 → 发新 uuid 入库（匿名 hashid，既有逻辑）。

        指纹查找仅在 hostname 或 label 至少一个非空时启用，防止匿名 client
        互相误匹配。
        """
        # 优先级 ① — user_name 派生确定性 id（--name 权威身份）
        if user_name:
            norm = _normalize_user_name(user_name)
            client_id = client_id_from_name(user_name)
            now = _now()
            conn = self._conn()
            try:
                existing = conn.execute(
                    "SELECT 1 FROM clients WHERE client_id=?", (client_id,)
                ).fetchone()
                if existing:
                    conn.execute(
                        "UPDATE clients SET last_seen=?, hostname=?, user_name=?"
                        " WHERE client_id=?",
                        (_now(), hostname or "", norm, client_id),
                    )
                else:
                    conn.execute(
                        "INSERT INTO clients (client_id, label, hostname, user_name,"
                        " joined_at, last_seen) VALUES (?, ?, ?, ?, ?, ?)",
                        (client_id, label or norm, hostname or "", norm, now, now),
                    )
                conn.commit()
            finally:
                conn.close()
            return client_id
        # 优先级 ② — claimed_client_id 命中
        if claimed_client_id and self.exists(claimed_client_id):
            self.touch(claimed_client_id)
            return claimed_client_id
        # 优先级 ③ — (hostname, label) 指纹回查
        existing = self._find_by_fingerprint(hostname=hostname, label=label)
        if existing:
            self.touch(existing)
            return existing
        # 优先级 ④ — 发新 uuid
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

    def find_by_user_name(self, user_name: str) -> str | None:
        """按明文 user_name 反查 client_id（"按名找人"）。

        输入经 ``_normalize_user_name`` 规范化后精确匹配 ``user_name`` 列
        （注册时存的就是规范化形式，与 ``client_id_from_name`` 同口径）。
        命中多条 → 返回 last_seen 最新者；未命中 → None。空名抛 ValueError。
        """
        norm = _normalize_user_name(user_name)
        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT client_id FROM clients WHERE user_name=?"
                " ORDER BY last_seen DESC LIMIT 1",
                (norm,),
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
