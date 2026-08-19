"""client_registry.py — team server 的 client 注册表（SP1）

server 需要持久化的只有三样：client 注册表、skill git 仓、汇聚的
ux_score 明细。这个文件是第一样。

client_id 是 server 生成的 uuid——它同时是 ① canary 分桶 key（喂
pick_side）② 上传轨迹的落盘分桶（clients/<client_id>/sessions/）③
手改分支命名（user-staging/<client_id>）。
"""
from __future__ import annotations

import hashlib
import logging
import re
import secrets
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from xskill._sqlite_connect import connect_with_lock


logger = logging.getLogger(__name__)

# 认证请求只把 last_seen 合并到内存；短暂延迟后由一个事务批量写回。
# 这个窗口足以把同一波并发合并起来，同时远小于 last_seen 的秒级精度。
_TOUCH_FLUSH_DELAY_SECONDS = 0.05
_TOUCH_RETRY_DELAY_SECONDS = 0.25
_TOUCH_CLOSE_ATTEMPTS = 3
_TOUCH_RETRY_WAITER = threading.Event()


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


_SAFE_DIR_RE = re.compile(r"[^A-Za-z0-9._-]")


def safe_dir_name(user_name: str | None, client_id: str) -> str:
    """文件系统目录名：优先用 user_name 明文（安全转义），匿名用 client_id。

    - 有 user_name：转义为 ``[A-Za-z0-9._-]`` 集合（其余替换 ``_``）；
      转义后为空或含 ``..`` / 以 ``-`` 开头（git branch 非法）→ 抛 ValueError。
      支持 ``m00947023`` / ``02020222`` / 简单用户名（字母数字直接通过）。
    - 无 user_name（匿名）：返回 client_id（hex，天然安全）。

    这只决定**文件系统路径**（clients/<dir>/sessions）；canary 分桶 key、
    user-staging 分支名仍用 client_id（不可变哈希，不受 name 特殊字符影响）。
    """
    if not user_name:
        return client_id
    escaped = _SAFE_DIR_RE.sub("_", user_name.strip())
    if not escaped or escaped == "." or escaped == ".." or escaped.startswith("-"):
        raise ValueError(
            f"user_name {user_name!r} 转义后 {escaped!r} 不是安全目录名"
        )
    return escaped


def member_traj_tag(user_name: str | None, client_id: str) -> str:
    """成员标识，由服务器前缀到落盘轨迹文件名里，保证多成员轨迹不重名。

    形式为「用户名可读部分 + 8 位 client_id 前缀」（issue #234）：

    - 可读部分：用户名按 ``[A-Za-z0-9._-]`` 转义、去掉首尾下划线与点后
      截取前 16 字符；为空（匿名、纯中文用户名等）退化为 ``u``。
    - 哈希部分：client_id 前 8 位。client_id 本身是
      ``sha256("name:" + 规范化用户名)[:16]``，同名用户跨设备一致——同一人
      换电脑不会产生第二个身份；4 位截断在数十人团队即有可感的碰撞概率，
      故取 8 位。

    示例：``alice`` → ``alice_1a2b3c4d``；``小明`` → ``u_9f8e7d6c``。
    """
    readable = ""
    if user_name:
        # 与 client_id 的派生一致按小写规范化：同一人以「Bob」「bob」注册
        # 得到同一 client_id，成员标识也必须相同，否则同一人的语料会因
        # 大小写分裂成两套文件名。
        readable = _SAFE_DIR_RE.sub(
            "_", user_name.strip().lower()
        ).strip("_.")[:16]
    if not readable:
        readable = "u"
    return f"{readable}_{client_id[:8]}"


_SCHEMA = """
CREATE TABLE IF NOT EXISTS clients (
    client_id      TEXT PRIMARY KEY,
    label          TEXT DEFAULT '',
    hostname       TEXT DEFAULT '',
    user_name      TEXT DEFAULT '',
    client_version TEXT DEFAULT '',
    ingest_paused  INTEGER NOT NULL DEFAULT 0,
    ingest_paused_at TEXT,
    ingest_paused_by TEXT DEFAULT '',
    ingest_pause_reason TEXT DEFAULT '',
    joined_at      TEXT NOT NULL,
    last_seen      TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ClientRegistry:
    """SQLite 支撑的 client 注册表；认证读快照，触达按短窗口批量写回。"""

    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._write_lock = threading.RLock()
        self._state_lock = threading.RLock()
        self._touch_flush_lock = threading.Lock()
        self._client_ids: set[str] = set()
        self._client_user_names: dict[str, str] = {}
        self._paused_client_ids: set[str] = set()
        self._pending_touches: dict[str, tuple[str, str | None]] = {}
        self._touch_timer: threading.Timer | None = None
        self._closed = False
        self._init_schema()
        self._load_client_snapshot()

    def _conn(self) -> sqlite3.Connection:
        conn = connect_with_lock(sqlite3.connect, str(self.db_path), timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 10000")
        conn.execute("PRAGMA synchronous = NORMAL")
        return conn

    def _init_schema(self) -> None:
        with self._write_lock:
            conn = self._conn()
            try:
                conn.execute("PRAGMA journal_mode = WAL")
                conn.executescript(_SCHEMA)
                # 幂等迁移：CREATE TABLE IF NOT EXISTS 不会给已存在的表加列，
                # 老 db 缺 user_name 列时显式 ALTER 补上。
                cols = {row["name"] for row in conn.execute("PRAGMA table_info(clients)")}
                if "user_name" not in cols:
                    conn.execute("ALTER TABLE clients ADD COLUMN user_name TEXT DEFAULT ''")
                if "client_version" not in cols:
                    # P2-2.10:client 上报的 xskill 版本,register/sync 时 upsert
                    conn.execute(
                        "ALTER TABLE clients ADD COLUMN client_version TEXT DEFAULT ''")
                if "dashboard_token" not in cols:
                    # P2-2.2(Q2a):dashboard 登录凭证,--name 注册时发放并回传打印
                    conn.execute(
                        "ALTER TABLE clients ADD COLUMN dashboard_token TEXT DEFAULT ''")
                if "ingest_paused" not in cols:
                    conn.execute(
                        "ALTER TABLE clients ADD COLUMN "
                        "ingest_paused INTEGER NOT NULL DEFAULT 0"
                    )
                if "ingest_paused_at" not in cols:
                    conn.execute(
                        "ALTER TABLE clients ADD COLUMN ingest_paused_at TEXT"
                    )
                if "ingest_paused_by" not in cols:
                    conn.execute(
                        "ALTER TABLE clients ADD COLUMN "
                        "ingest_paused_by TEXT DEFAULT ''"
                    )
                if "ingest_pause_reason" not in cols:
                    conn.execute(
                        "ALTER TABLE clients ADD COLUMN "
                        "ingest_pause_reason TEXT DEFAULT ''"
                    )
                conn.commit()
            finally:
                conn.close()

    def _load_client_snapshot(self) -> None:
        """从持久化注册表加载认证快照（服务重启时恢复）。"""
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT client_id, user_name, ingest_paused FROM clients"
            ).fetchall()
        finally:
            conn.close()
        with self._state_lock:
            self._client_ids = {row["client_id"] for row in rows}
            self._client_user_names = {
                row["client_id"]: row["user_name"] or "" for row in rows
            }
            self._paused_client_ids = {
                row["client_id"] for row in rows if bool(row["ingest_paused"])
            }

    def _remember_client(self, client_id: str) -> None:
        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT user_name, ingest_paused FROM clients WHERE client_id=?",
                (client_id,),
            ).fetchone()
            user_name = (row["user_name"] or "") if row else ""
            ingest_paused = bool(row["ingest_paused"]) if row else False
        finally:
            conn.close()
        with self._state_lock:
            if not self._closed:
                self._client_ids.add(client_id)
                self._client_user_names[client_id] = user_name
                if ingest_paused:
                    self._paused_client_ids.add(client_id)
                else:
                    self._paused_client_ids.discard(client_id)

    def _raise_if_closed(self) -> None:
        """拒绝关闭后的新注册。"""
        with self._state_lock:
            if self._closed:
                raise RuntimeError("client registry is closed")

    def register(
        self, *,
        label: str = "",
        hostname: str = "",
        claimed_client_id: str | None = None,
        user_name: str | None = None,
        client_version: str = "",
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
        self._raise_if_closed()
        # 优先级 ① — user_name 派生确定性 id（--name 权威身份）
        if user_name:
            norm = _normalize_user_name(user_name)
            client_id = client_id_from_name(user_name)
            now = _now()
            with self._write_lock:
                self._raise_if_closed()
                conn = self._conn()
                try:
                    existing = conn.execute(
                        "SELECT 1 FROM clients WHERE client_id=?", (client_id,)
                    ).fetchone()
                    if existing:
                        conn.execute(
                            "UPDATE clients SET last_seen=?, hostname=?, user_name=?,"
                            " client_version=?"
                            " WHERE client_id=?",
                            (_now(), hostname or "", norm, client_version or "", client_id),
                        )
                    else:
                        conn.execute(
                            "INSERT INTO clients (client_id, label, hostname, user_name,"
                            " client_version, joined_at, last_seen)"
                            " VALUES (?, ?, ?, ?, ?, ?, ?)",
                            (client_id, label or norm, hostname or "", norm,
                             client_version or "", now, now),
                        )
                    conn.commit()
                finally:
                    conn.close()
                # 数据库写入与认证快照发布共用 write -> state 的锁顺序。
                # delete/close 因而不可能插入在这两步之间，让已删除 client
                # 被重新放回内存索引。
                self._remember_client(client_id)
            return client_id
        # 优先级 ② — claimed_client_id 命中
        if claimed_client_id and self.exists(claimed_client_id):
            self.touch(claimed_client_id, version=client_version or None)
            self._raise_if_closed()
            return claimed_client_id
        # 优先级 ③ — (hostname, label) 指纹回查
        existing = self._find_by_fingerprint(hostname=hostname, label=label)
        if existing:
            self.touch(existing, version=client_version or None)
            self._raise_if_closed()
            return existing
        # 优先级 ④ — 发新 uuid
        client_id = uuid.uuid4().hex
        now = _now()
        with self._write_lock:
            self._raise_if_closed()
            conn = self._conn()
            try:
                conn.execute(
                    "INSERT INTO clients (client_id, label, hostname, client_version,"
                    " joined_at, last_seen)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (client_id, label, hostname, client_version or "", now, now),
                )
                conn.commit()
            finally:
                conn.close()
            self._remember_client(client_id)
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

    def ensure_dashboard_token(self, client_id: str) -> str:
        """确保该 client 行有 dashboard token（无则生成）,返回 token。

        P2-2.2(Q2a):``--name`` 注册路径调用。token 与 user_name 1:1
        （命名用户的 client_id 由 name 确定性派生）。"""
        with self._write_lock:
            conn = self._conn()
            try:
                row = conn.execute(
                    "SELECT dashboard_token FROM clients WHERE client_id=?",
                    (client_id,),
                ).fetchone()
                if row is None:
                    raise ValueError(f"unknown client_id: {client_id}")
                existing = row["dashboard_token"] or ""
                if existing:
                    return existing
                token = secrets.token_hex(16)
                conn.execute(
                    "UPDATE clients SET dashboard_token=? WHERE client_id=?",
                    (token, client_id),
                )
                conn.commit()
                return token
            finally:
                conn.close()

    def dashboard_token_for(self, user_name: str) -> str | None:
        """按 user_name 取 dashboard token（登录校验用）。未注册/无 token → None。"""
        norm = _normalize_user_name(user_name)
        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT dashboard_token FROM clients WHERE user_name=?"
                " ORDER BY last_seen DESC LIMIT 1",
                (norm,),
            ).fetchone()
            return (row["dashboard_token"] or None) if row else None
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
        with self._state_lock:
            return not self._closed and client_id in self._client_ids

    def touch(self, client_id: str, *, version: str | None = None) -> None:
        """更新 last_seen（P2-2.10:携带 version 时顺带 upsert client_version）。

        client_id 不存在则静默 no-op。version=None（旧 client 未上报）不清
        既有值。显式 touch 保持同步落库语义；高频认证走下方的批量写回。"""
        # 与批量 flush 串行，确保较早排队的认证版本不会在显式 touch 之后
        # 反向覆盖；state lock 覆盖数据库写入，使同步 touch 与并发认证有明确
        # 的先后顺序。
        with self._touch_flush_lock:
            with self._write_lock:
                with self._state_lock:
                    if self._closed:
                        return
                    conn = self._conn()
                    try:
                        if version:
                            cursor = conn.execute(
                                "UPDATE clients SET last_seen=?, client_version=?"
                                " WHERE client_id=?",
                                (_now(), version, client_id),
                            )
                        else:
                            cursor = conn.execute(
                                "UPDATE clients SET last_seen=? WHERE client_id=?",
                                (_now(), client_id),
                            )
                        conn.commit()
                    finally:
                        conn.close()
                    # 所有尚未被 flush 取走的触达都早于本次同步写入。
                    self._pending_touches.pop(client_id, None)
                    if cursor.rowcount > 0:
                        self._client_ids.add(client_id)
                    else:
                        self._client_ids.discard(client_id)

    def authenticate_and_touch(
        self, client_id: str, version: str | None = None,
    ) -> bool:
        """确认 client 存在，并把 last_seen/version 合并后异步批量写回。

        认证判断与删除共用同一把内存状态锁：``delete`` 返回后，后续请求会
        立即失败。持久化只更新现有行，不会把已经删除的 client 重新插入。
        """
        now = _now()
        with self._state_lock:
            if self._closed or client_id not in self._client_ids:
                return False
            previous = self._pending_touches.get(client_id)
            previous_version = previous[1] if previous else None
            self._pending_touches[client_id] = (
                now,
                version or previous_version,
            )
            self._schedule_touch_flush_locked(_TOUCH_FLUSH_DELAY_SECONDS)
            return True

    def _schedule_touch_flush_locked(self, delay: float) -> None:
        """state lock 已持有时，确保最多只有一个待执行的批量写回。"""
        if self._closed or self._touch_timer is not None:
            return
        timer = threading.Timer(delay, self._flush_pending_touches)
        timer.name = "xskill-client-touch-flush"
        timer.daemon = True
        self._touch_timer = timer
        timer.start()

    @staticmethod
    def _merge_touch(
        older: tuple[str, str | None], newer: tuple[str, str | None],
    ) -> tuple[str, str | None]:
        """合并两次触达：保留较新时间，version 只增量覆盖、不被空值清除。"""
        return (max(older[0], newer[0]), newer[1] or older[1])

    def _persist_touch_batch(
        self, pending: dict[str, tuple[str, str | None]],
    ) -> None:
        if not pending:
            return
        rows = [
            (last_seen, version, version, client_id)
            for client_id, (last_seen, version) in pending.items()
        ]
        with self._write_lock:
            conn = self._conn()
            try:
                conn.executemany(
                    "UPDATE clients SET last_seen=?,"
                    " client_version=CASE WHEN ? IS NOT NULL"
                    " THEN ? ELSE client_version END"
                    " WHERE client_id=?",
                    rows,
                )
                conn.commit()
            finally:
                conn.close()

    def _flush_pending_touches(self) -> bool:
        """取走当前触达并用单事务写回；失败则保留并延迟重试。"""
        with self._touch_flush_lock:
            with self._state_lock:
                timer = self._touch_timer
                self._touch_timer = None
                if timer is not None and timer is not threading.current_thread():
                    timer.cancel()
                pending = self._pending_touches
                self._pending_touches = {}
                # 已被删除的 id 不应再参与持久化。
                pending = {
                    client_id: touch
                    for client_id, touch in pending.items()
                    if client_id in self._client_ids
                }
            try:
                self._persist_touch_batch(pending)
                return True
            except sqlite3.Error:
                logger.warning("failed to persist client last_seen batch", exc_info=True)
                with self._state_lock:
                    for client_id, touch in pending.items():
                        if client_id not in self._client_ids:
                            continue
                        newer = self._pending_touches.get(client_id)
                        self._pending_touches[client_id] = (
                            self._merge_touch(touch, newer) if newer else touch
                        )
                    if not self._closed:
                        self._schedule_touch_flush_locked(
                            _TOUCH_RETRY_DELAY_SECONDS,
                        )
                return False

    def flush_pending_touches(self) -> bool:
        """立即持久化已合并的触达，供生命周期关闭和确定性测试使用。"""
        return self._flush_pending_touches()

    def delete(self, client_id: str) -> bool:
        """删除 client；方法返回后认证快照与待写回触达均已失效。"""
        with self._write_lock:
            conn = self._conn()
            try:
                cursor = conn.execute(
                    "DELETE FROM clients WHERE client_id=?", (client_id,)
                )
                conn.commit()
            finally:
                conn.close()
            with self._state_lock:
                self._client_ids.discard(client_id)
                self._client_user_names.pop(client_id, None)
                self._paused_client_ids.discard(client_id)
                self._pending_touches.pop(client_id, None)
        return cursor.rowcount > 0

    def close(self) -> bool:
        """停止新的认证并同步写完已经接收的 last_seen 触达。"""
        # write -> state 与 register/delete/touch 保持一致。close 获得 write
        # lock 后，之前开始的注册已完整发布到快照；之后的注册会看到 closed。
        with self._write_lock:
            with self._state_lock:
                self._closed = True
                timer = self._touch_timer
                self._touch_timer = None
                if timer is not None:
                    timer.cancel()

        # Timer.cancel() 不等待已经进入回调的线程。先 join，既避免关闭后遗留
        # touch 线程，也让随后 flush 看见失败回调放回的 pending。
        if timer is not None and timer is not threading.current_thread():
            timer.join()

        # 关闭态不会再创建重试 Timer，所以在调用线程内做有限次重试。
        # 每次失败时 _flush_pending_touches 都会把完整批次合并回 pending。
        for attempt in range(_TOUCH_CLOSE_ATTEMPTS):
            if self._flush_pending_touches():
                return True
            if attempt + 1 < _TOUCH_CLOSE_ATTEMPTS:
                _TOUCH_RETRY_WAITER.wait(_TOUCH_RETRY_DELAY_SECONDS)
        return False

    def get(self, client_id: str) -> dict | None:
        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT * FROM clients WHERE client_id=?", (client_id,)
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def user_name_for(self, client_id: str) -> str:
        """从认证快照读取用户名，不在 sync 热路径重新打开 SQLite。"""
        with self._state_lock:
            if self._closed or client_id not in self._client_ids:
                raise ValueError(f"unknown client_id: {client_id}")
            return self._client_user_names.get(client_id, "")

    def is_ingest_paused(self, client_id: str) -> bool:
        """从内存快照读取该 client 是否暂停后续轨迹处理。"""
        with self._state_lock:
            if self._closed or client_id not in self._client_ids:
                raise ValueError(f"unknown client_id: {client_id}")
            return client_id in self._paused_client_ids

    def set_ingest_paused(
        self,
        client_id: str,
        paused: bool,
        *,
        actor: str = "",
        reason: str = "",
    ) -> dict:
        """幂等更新轨迹处理开关，并与认证快照按同一锁顺序发布。"""
        desired = bool(paused)
        actor = str(actor or "").strip()
        reason = str(reason or "").strip()
        with self._write_lock:
            self._raise_if_closed()
            conn = self._conn()
            try:
                row = conn.execute(
                    "SELECT * FROM clients WHERE client_id=?", (client_id,),
                ).fetchone()
                if row is None:
                    raise ValueError(f"unknown client_id: {client_id}")
                if bool(row["ingest_paused"]) != desired:
                    if desired:
                        conn.execute(
                            "UPDATE clients SET ingest_paused=1,"
                            " ingest_paused_at=?, ingest_paused_by=?,"
                            " ingest_pause_reason=? WHERE client_id=?",
                            (_now(), actor, reason, client_id),
                        )
                    else:
                        conn.execute(
                            "UPDATE clients SET ingest_paused=0,"
                            " ingest_paused_at=NULL, ingest_paused_by='',"
                            " ingest_pause_reason='' WHERE client_id=?",
                            (client_id,),
                        )
                    conn.commit()
                    row = conn.execute(
                        "SELECT * FROM clients WHERE client_id=?", (client_id,),
                    ).fetchone()
            finally:
                conn.close()
            with self._state_lock:
                if desired:
                    self._paused_client_ids.add(client_id)
                else:
                    self._paused_client_ids.discard(client_id)
            return dict(row)

    def list(self) -> list[dict]:
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT * FROM clients ORDER BY joined_at"
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def dir_name_for(self, client_id: str) -> str:
        """该 client 的文件系统目录名：有 user_name → 转义明文；匿名 → client_id。

        供 upload 落盘 / engine _client_store_root 用。client 不存在 → 抛 ValueError。
        """
        return safe_dir_name(self.user_name_for(client_id) or None, client_id)
