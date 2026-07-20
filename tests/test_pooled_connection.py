"""pooled_connection 线程内连接复用的行为契约。"""
from __future__ import annotations

import os
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from xskill.pipeline import registry


def _pool_slots():
    return getattr(registry._REGISTRY_THREAD_POOL, "slots", {})


def test_same_thread_reuses_one_connection(tmp_path):
    db = tmp_path / "r.db"
    with registry.pooled_connection(db) as first:
        pass
    with registry.pooled_connection(db) as second:
        pass
    assert first is second


def test_threads_get_distinct_connections(tmp_path):
    db = tmp_path / "r.db"
    seen = {}

    def grab(tag):
        with registry.pooled_connection(db) as conn:
            seen[tag] = id(conn)

    grab("main")
    worker = threading.Thread(target=grab, args=("worker",))
    worker.start()
    worker.join()
    assert seen["main"] != seen["worker"]


def test_record_usage_reuses_connection_and_persists(tmp_path):
    db = tmp_path / "r.db"
    for _ in range(3):
        registry.record_usage(step="t", model="m", prompt=1, completion=1,
                              total=2, cost_usd=0.0, price_source="config",
                              db_path=db)
    with registry.pooled_connection(db) as conn:
        count = conn.execute("SELECT COUNT(*) FROM llm_usage").fetchone()[0]
    assert count == 3
    assert len(_pool_slots()) <= registry._REGISTRY_THREAD_POOL_CAPACITY


def test_exception_rolls_back_but_keeps_connection(tmp_path):
    db = tmp_path / "r.db"
    with pytest.raises(RuntimeError):
        with registry.pooled_connection(db) as conn:
            conn.execute("INSERT INTO llm_usage(step,model,prompt,completion,"
                         "total,cost_usd,price_source) VALUES('s','m',1,1,2,0,'x')")
            raise RuntimeError("boom")
    with registry.pooled_connection(db) as conn:
        count = conn.execute("SELECT COUNT(*) FROM llm_usage").fetchone()[0]
    assert count == 0  # 未提交的写被回滚
    assert conn is not None


def test_reentrant_acquire_falls_back_to_fresh_connection(tmp_path):
    db = tmp_path / "r.db"
    with registry.pooled_connection(db) as outer:
        outer.execute("INSERT INTO llm_usage(step,model,prompt,completion,"
                      "total,cost_usd,price_source) VALUES('s','m',1,1,2,0,'x')")
        with registry.pooled_connection(db) as inner:
            assert inner is not outer
            # 外层未提交的事务对内层连接不可见
            visible = inner.execute(
                "SELECT COUNT(*) FROM llm_usage").fetchone()[0]
            assert visible == 0
        outer.commit()


def test_deleted_db_file_triggers_reopen(tmp_path):
    db = tmp_path / "r.db"
    with registry.pooled_connection(db) as first:
        first.execute("INSERT INTO llm_usage(step,model,prompt,completion,"
                      "total,cost_usd,price_source) VALUES('s','m',1,1,2,0,'x')")
        first.commit()
    if os.name == "nt":
        # Windows 不允许删除仍由连接池持有句柄的 SQLite 文件；关闭句柄后
        # 仍验证连接池不会继续复用已失效的连接。
        first.close()
    db.unlink()
    for suffix in ("-wal", "-shm"):
        sidecar = db.with_name(db.name + suffix)
        if sidecar.exists():
            sidecar.unlink()
    with registry.pooled_connection(db) as reopened:
        count = reopened.execute("SELECT COUNT(*) FROM llm_usage").fetchone()[0]
    assert reopened is not first
    assert count == 0  # 新库,旧连接没有被继续写


def test_pool_capacity_evicts_oldest(tmp_path):
    paths = [tmp_path / f"r{i}.db" for i in
             range(registry._REGISTRY_THREAD_POOL_CAPACITY + 2)]
    conns = []
    for p in paths:
        with registry.pooled_connection(p) as conn:
            conns.append(conn)
    slots = _pool_slots()
    assert len(slots) == registry._REGISTRY_THREAD_POOL_CAPACITY
    kept_keys = set(slots)
    assert paths[0].resolve() not in kept_keys  # 最旧的被淘汰
    assert paths[-1].resolve() in kept_keys


def test_concurrent_record_usage_from_pool_threads(tmp_path):
    db = tmp_path / "r.db"
    def write(_i):
        registry.record_usage(step="t", model="m", prompt=1, completion=1,
                              total=2, cost_usd=0.0, price_source="config",
                              db_path=db)
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(write, range(200)))
    with registry.pooled_connection(db) as conn:
        count = conn.execute("SELECT COUNT(*) FROM llm_usage").fetchone()[0]
    assert count == 200
