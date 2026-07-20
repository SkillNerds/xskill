from __future__ import annotations

import concurrent.futures
import sqlite3
import threading
import time

import pytest

from xskill.team.server.client_registry import ClientRegistry, client_id_from_name


def test_legacy_clients_schema_migrates_ingest_control_without_data_loss(tmp_path):
    db_path = tmp_path / "team_clients.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE clients (
                client_id TEXT PRIMARY KEY,
                label TEXT DEFAULT '',
                hostname TEXT DEFAULT '',
                user_name TEXT DEFAULT '',
                client_version TEXT DEFAULT '',
                dashboard_token TEXT DEFAULT '',
                joined_at TEXT NOT NULL,
                last_seen TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "INSERT INTO clients"
            " (client_id,label,hostname,user_name,client_version,dashboard_token,"
            "  joined_at,last_seen) VALUES (?,?,?,?,?,?,?,?)",
            (
                "legacy-client",
                "legacy-label",
                "legacy-host",
                "alice",
                "0.6.24",
                "dashboard-secret",
                "2026-01-01T00:00:00+00:00",
                "2026-01-02T00:00:00+00:00",
            ),
        )
        conn.commit()
    finally:
        conn.close()

    reg = ClientRegistry(db_path)
    row = reg.get("legacy-client")
    assert row["label"] == "legacy-label"
    assert row["client_version"] == "0.6.24"
    assert row["dashboard_token"] == "dashboard-secret"
    assert row["ingest_paused"] == 0
    assert row["ingest_paused_at"] is None
    assert row["ingest_paused_by"] == ""
    assert row["ingest_pause_reason"] == ""
    assert reg.is_ingest_paused("legacy-client") is False

    paused = reg.set_ingest_paused(
        "legacy-client", True, actor="boss", reason="quality review",
    )
    first_paused_at = paused["ingest_paused_at"]
    repeated = reg.set_ingest_paused(
        "legacy-client", True, actor="other", reason="must not overwrite",
    )
    assert repeated["ingest_paused_at"] == first_paused_at
    assert repeated["ingest_paused_by"] == "boss"
    assert repeated["ingest_pause_reason"] == "quality review"

    assert reg.close() is True
    restarted = ClientRegistry(db_path)
    assert restarted.is_ingest_paused("legacy-client") is True
    resumed = restarted.set_ingest_paused(
        "legacy-client", False, actor="boss",
    )
    assert resumed["ingest_paused"] == 0
    assert resumed["ingest_paused_at"] is None
    assert resumed["ingest_paused_by"] == ""
    assert resumed["ingest_pause_reason"] == ""


def test_register_returns_unique_ids(tmp_path):
    reg = ClientRegistry(tmp_path / "team_clients.db")
    a = reg.register(label="alice-laptop", hostname="alice")
    b = reg.register(label="bob-laptop", hostname="bob")
    assert a != b
    assert reg.exists(a) and reg.exists(b)
    assert not reg.exists("nonexistent")


def test_touch_updates_last_seen(tmp_path):
    reg = ClientRegistry(tmp_path / "team_clients.db")
    cid = reg.register(label="x", hostname="x")
    before = reg.get(cid)["last_seen"]
    reg.touch(cid)
    after = reg.get(cid)["last_seen"]
    assert after >= before


def test_list_returns_all(tmp_path):
    reg = ClientRegistry(tmp_path / "team_clients.db")
    reg.register(label="a", hostname="a")
    reg.register(label="b", hostname="b")
    rows = reg.list()
    assert len(rows) == 2
    assert {r["label"] for r in rows} == {"a", "b"}


def test_registry_connections_use_wal_normal_and_busy_timeout(tmp_path):
    reg = ClientRegistry(tmp_path / "team_clients.db")
    conn = reg._conn()
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 10_000
    finally:
        conn.close()


def test_authenticate_and_touch_is_atomic_and_preserves_version(tmp_path):
    reg = ClientRegistry(tmp_path / "team_clients.db")
    cid = reg.register(label="x", hostname="x", client_version="1.0")

    assert reg.authenticate_and_touch(cid, None) is True
    assert reg.get(cid)["client_version"] == "1.0"
    assert reg.authenticate_and_touch("nonexistent", "2.0") is False


def test_100_concurrent_authenticate_and_touch_calls_succeed(tmp_path):
    reg = ClientRegistry(tmp_path / "team_clients.db")
    cid = reg.register(label="x", hostname="x")
    worker_count = 100
    start = threading.Barrier(worker_count)

    def authenticate() -> bool:
        start.wait(timeout=30)
        return reg.authenticate_and_touch(cid, "1.2.3")

    with concurrent.futures.ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = [executor.submit(authenticate) for _ in range(worker_count)]
        results = [future.result(timeout=30) for future in futures]

    assert results == [True] * worker_count


def test_300_concurrent_authentications_use_one_persistence_batch(
    tmp_path, monkeypatch,
):
    reg = ClientRegistry(tmp_path / "team_clients.db")
    client_ids = [
        reg.register(label=f"client-{index}", hostname=f"host-{index}")
        for index in range(300)
    ]
    # 让测试自己决定写回时点，直接验证认证热路径没有 SQLite 操作。
    monkeypatch.setattr(reg, "_schedule_touch_flush_locked", lambda _delay: None)
    persisted_batches: list[dict] = []
    original_persist = reg._persist_touch_batch

    def record_persist(pending):
        persisted_batches.append(dict(pending))
        original_persist(pending)

    monkeypatch.setattr(reg, "_persist_touch_batch", record_persist)
    start = threading.Barrier(len(client_ids))

    def authenticate(client_id: str) -> tuple[bool, bool]:
        start.wait(timeout=30)
        # 同一合并窗口里的重复请求不能产生额外写回行。
        return (
            reg.authenticate_and_touch(client_id, "1.2.3"),
            reg.authenticate_and_touch(client_id, "1.2.3"),
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=300) as executor:
        futures = [executor.submit(authenticate, cid) for cid in client_ids]
        results = [future.result(timeout=30) for future in futures]

    assert results == [(True, True)] * 300
    assert persisted_batches == []
    assert reg.flush_pending_touches() is True
    assert len(persisted_batches) == 1
    assert set(persisted_batches[0]) == set(client_ids)


def test_authentication_touch_is_eventually_persisted(tmp_path):
    reg = ClientRegistry(tmp_path / "team_clients.db")
    cid = reg.register(label="x", hostname="x", client_version="1.0")
    conn = reg._conn()
    try:
        conn.execute(
            "UPDATE clients SET last_seen=? WHERE client_id=?",
            ("2000-01-01T00:00:00+00:00", cid),
        )
        conn.commit()
    finally:
        conn.close()
    assert reg.authenticate_and_touch(cid, "2.0") is True
    deadline = time.monotonic() + 2
    row = reg.get(cid)
    while row["client_version"] != "2.0" and time.monotonic() < deadline:
        time.sleep(0.01)
        row = reg.get(cid)
    assert row["last_seen"] > "2000-01-01T00:00:00+00:00"
    assert row["client_version"] == "2.0"


def test_delete_immediately_revokes_and_pending_touch_does_not_restore(
    tmp_path, monkeypatch,
):
    db_path = tmp_path / "team_clients.db"
    reg = ClientRegistry(db_path)
    cid = reg.register(label="x", hostname="x")
    monkeypatch.setattr(reg, "_schedule_touch_flush_locked", lambda _delay: None)

    assert reg.authenticate_and_touch(cid, "1.0") is True
    assert reg.delete(cid) is True
    assert reg.authenticate_and_touch(cid, "1.0") is False
    assert reg.flush_pending_touches() is True

    restarted = ClientRegistry(db_path)
    assert restarted.exists(cid) is False
    assert restarted.authenticate_and_touch(cid) is False


def test_failed_touch_batch_is_retained_for_retry(tmp_path, monkeypatch):
    reg = ClientRegistry(tmp_path / "team_clients.db")
    cid = reg.register(label="x", hostname="x", client_version="1.0")
    monkeypatch.setattr(reg, "_schedule_touch_flush_locked", lambda _delay: None)
    assert reg.authenticate_and_touch(cid, "2.0") is True

    original_persist = reg._persist_touch_batch

    def locked(_pending):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(reg, "_persist_touch_batch", locked)
    assert reg.flush_pending_touches() is False
    monkeypatch.setattr(reg, "_persist_touch_batch", original_persist)
    assert reg.flush_pending_touches() is True
    assert reg.get(cid)["client_version"] == "2.0"


def test_close_retries_transient_failure_and_joins_timer(tmp_path, monkeypatch):
    reg = ClientRegistry(tmp_path / "team_clients.db")
    cid = reg.register(label="x", hostname="x", client_version="1.0")
    monkeypatch.setattr(
        "xskill.team.server.client_registry._TOUCH_FLUSH_DELAY_SECONDS", 10.0,
    )
    assert reg.authenticate_and_touch(cid, "2.0") is True
    timer = reg._touch_timer
    assert timer is not None

    original_persist = reg._persist_touch_batch
    attempts = 0

    def fail_once(pending):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise sqlite3.OperationalError("database is locked")
        original_persist(pending)

    monkeypatch.setattr(reg, "_persist_touch_batch", fail_once)
    assert reg.close() is True
    assert attempts == 2
    assert timer.is_alive() is False
    assert reg._pending_touches == {}
    assert reg.get(cid)["client_version"] == "2.0"


def test_close_keeps_pending_batch_after_all_retries_fail(tmp_path, monkeypatch):
    reg = ClientRegistry(tmp_path / "team_clients.db")
    cid = reg.register(label="x", hostname="x", client_version="1.0")
    monkeypatch.setattr(reg, "_schedule_touch_flush_locked", lambda _delay: None)
    assert reg.authenticate_and_touch(cid, "2.0") is True
    original_persist = reg._persist_touch_batch

    def locked(_pending):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(reg, "_persist_touch_batch", locked)
    assert reg.close() is False
    assert reg._pending_touches[cid][1] == "2.0"

    # close 失败不会丢数据；即便 registry 已停止认证，调用方仍可在数据库
    # 恢复后显式完成写回。
    monkeypatch.setattr(reg, "_persist_touch_batch", original_persist)
    assert reg.flush_pending_touches() is True
    assert reg.get(cid)["client_version"] == "2.0"


def test_close_rejects_registration_but_keeps_read_access(tmp_path):
    reg = ClientRegistry(tmp_path / "team_clients.db")
    cid = reg.register(label="x", hostname="x")
    assert reg.close() is True

    with pytest.raises(RuntimeError, match="closed"):
        reg.register(claimed_client_id=cid, label="x", hostname="x")
    assert reg.get(cid)["client_id"] == cid


def test_sync_touch_wins_over_older_pending_auth_version(tmp_path, monkeypatch):
    reg = ClientRegistry(tmp_path / "team_clients.db")
    cid = reg.register(label="x", hostname="x", client_version="1.0")
    monkeypatch.setattr(reg, "_schedule_touch_flush_locked", lambda _delay: None)

    assert reg.authenticate_and_touch(cid, "2.0") is True
    reg.touch(cid, version="3.0")
    assert reg.flush_pending_touches() is True
    assert reg.get(cid)["client_version"] == "3.0"


def test_failed_batch_merges_newer_version_before_retry(tmp_path, monkeypatch):
    reg = ClientRegistry(tmp_path / "team_clients.db")
    cid = reg.register(label="x", hostname="x", client_version="1.0")
    monkeypatch.setattr(reg, "_schedule_touch_flush_locked", lambda _delay: None)
    assert reg.authenticate_and_touch(cid, "2.0") is True

    persist_started = threading.Event()
    allow_failure = threading.Event()
    original_persist = reg._persist_touch_batch

    def fail_after_new_touch(_pending):
        persist_started.set()
        assert allow_failure.wait(timeout=5)
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(reg, "_persist_touch_batch", fail_after_new_touch)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        flush = executor.submit(reg.flush_pending_touches)
        assert persist_started.wait(timeout=5)
        assert reg.authenticate_and_touch(cid, "3.0") is True
        allow_failure.set()
        assert flush.result(timeout=5) is False

    assert reg._pending_touches[cid][1] == "3.0"
    monkeypatch.setattr(reg, "_persist_touch_batch", original_persist)
    assert reg.flush_pending_touches() is True
    assert reg.get(cid)["client_version"] == "3.0"


def test_register_and_delete_publish_one_consistent_identity(tmp_path, monkeypatch):
    reg = ClientRegistry(tmp_path / "team_clients.db")
    client_id = client_id_from_name("alice")
    remember_entered = threading.Event()
    allow_remember = threading.Event()
    original_remember = reg._remember_client

    def blocked_remember(value):
        remember_entered.set()
        assert allow_remember.wait(timeout=5)
        original_remember(value)

    monkeypatch.setattr(reg, "_remember_client", blocked_remember)
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        register_future = executor.submit(reg.register, user_name="alice")
        assert remember_entered.wait(timeout=5)
        delete_future = executor.submit(reg.delete, client_id)
        # register 在持有 write lock 时发布认证快照；delete 不能插到数据库
        # commit 与快照发布之间。
        time.sleep(0.05)
        assert delete_future.done() is False
        allow_remember.set()
        assert register_future.result(timeout=5) == client_id
        assert delete_future.result(timeout=5) is True

    assert reg.exists(client_id) is False
    assert reg.authenticate_and_touch(client_id) is False
    assert reg.get(client_id) is None
