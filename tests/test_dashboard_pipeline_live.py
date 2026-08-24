"""流水线 Monitor：pipeline_live 只读整形 + router 端点（含公网白名单裁剪）。

原则：禁止 fallback 糊弄——状态文件缺失/老版 worker/日志不存在一律显式空态。
"""
from __future__ import annotations

import json
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from xskill.dashboard.pipeline_live import pipeline_live, tail_task_log
from xskill.dashboard.router import build_dashboard_router
from xskill.pipeline.registry import get_connection
from xskill.utils.status_file import AGENT_WORKER_STATUS_FILE


def _write_status(home, stats, *, ok=True, error=None):
    payload = {"ok": ok, "error": error, "ended_at": time.time(), "stats": stats}
    (home / AGENT_WORKER_STATUS_FILE).write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _full_stats():
    now = time.time()
    return {
        "pid": 1234,
        "started_at": now - 60,
        "heartbeat_at": now,
        "watcher": {"running": True, "polls": 9},
        "llm": {"inflight": 2, "waiting": 0, "rate_limit_waiting": 1, "retry_waiting": 0},
        "pool_config": {
            "split": {"workers": 2, "llm_weight": 6},
            "cluster": {"workers": 2, "batch_size": 8, "llm_weight": 3},
            "edit": {"workers": 3, "batch_size": 5, "llm_weight": 1},
        },
        "pools": {
            "split": {
                "workers": 2, "queued": 1, "completed": 10, "failed": 0,
                "seats": [
                    {"seat": 0, "started_at": now - 5,
                     "task": {"kind": "traj", "traj_id": "traj_a", "watch_dir": "cc"}},
                    None,
                ],
                "queue": [{"kind": "traj", "traj_id": "traj_b", "watch_dir": "cc"}],
            },
            "cluster": {
                "workers": 2, "queued": 0, "completed": 4, "failed": 1,
                "seats": [
                    {"seat": 0, "started_at": now - 8,
                     "task": {"kind": "atom_batch", "atom_ids": ["atom_1", "atom_2"]}},
                    None,
                ],
                "queue": [],
            },
            "edit": {
                "workers": 3, "queued": 0, "completed": 7, "failed": 0,
                "seats": [
                    {"seat": 0, "started_at": now - 12,
                     "task": {"kind": "skill", "skill_name": "alpha", "xfer": "baby_main",
                              "candidates": 2, "weightscore": 15, "branch": "baby"}},
                    None,
                    None,
                ],
                "queue": [],
            },
            # embed 不进监看（读库事实，不占席位色块）
            "embed": {"workers": 2, "queued": 0, "completed": 9, "failed": 0},
        },
        "cluster": {"pending_atoms": 6, "claimed_atoms": 2, "running_batches": 1},
    }


# ── pipeline_live 整形 ────────────────────────────────────────────

def test_missing_status_file_is_explicit_not_running(tmp_path):
    body = pipeline_live(tmp_path / "r.db")
    assert body["running"] is False
    assert "无状态文件" in body["message"]
    assert "pools" not in body  # 绝不编造占位池


def test_full_status_shapes_three_monitored_pools(tmp_path):
    _write_status(tmp_path, _full_stats())
    body = pipeline_live(tmp_path / "r.db")
    assert body["running"] is True
    assert body["pid"] == 1234
    assert body["pending_atoms"] == 6
    assert body["llm"]["inflight"] == 2
    assert set(body["pools"]) == {"split", "cluster", "edit", "generate"}

    split = body["pools"]["split"]
    assert split["workers"] == 2
    assert split["llm_weight"] == 6
    assert split["batch_size"] is None  # split 无批量配置
    assert len(split["seats"]) == 2
    assert split["seats"][0]["task"]["traj_id"] == "traj_a"
    assert split["seats"][1] is None  # 固定席位：空位不左挤
    assert split["queue"][0]["traj_id"] == "traj_b"

    cluster = body["pools"]["cluster"]
    assert cluster["batch_size"] == 8
    assert cluster["seats"][0]["task"]["atom_ids"] == ["atom_1", "atom_2"]

    edit = body["pools"]["edit"]
    assert edit["seats"][0]["task"]["xfer"] == "baby_main"
    assert edit["failed"] == 0

    generate = body["pools"]["generate"]
    assert generate["workers"] == 3
    assert generate["shared_pool"] == "edit"
    assert generate["seats"] == [None, None, None]
    assert generate.get("llm_priority") is True
    assert "llm_weight" not in generate


def test_legacy_worker_without_seat_bookkeeping_gets_explicit_empty_seats(tmp_path):
    stats = _full_stats()
    for pool in stats["pools"].values():
        pool.pop("seats", None)
        pool.pop("queue", None)
    _write_status(tmp_path, stats)
    body = pipeline_live(tmp_path / "r.db")
    edit = body["pools"]["edit"]
    assert edit["seats"] == [None, None, None]  # 显式空席，不伪造任务
    assert edit["queue"] == []


def test_empty_stats_means_worker_not_reporting(tmp_path):
    _write_status(tmp_path, {}, ok=False, error="boom")
    body = pipeline_live(tmp_path / "r.db")
    assert body["running"] is False
    assert body["ok"] is False
    assert body["error"] == "boom"


def test_generate_seats_are_projected_out_of_edit_pool(tmp_path):
    stats = _full_stats()
    now = stats["heartbeat_at"]
    stats["pools"]["edit"]["seats"][1] = {
        "seat": 1, "started_at": now - 3,
        "task": {"kind": "generate", "job_id": "abc123", "user_id": "alice",
                 "instruction": "写一个发票技能"},
    }
    stats["pools"]["edit"]["queue"] = [
        {"kind": "generate", "job_id": "def456", "user_id": "bob",
         "instruction": "改现有 skill"},
    ]
    stats["generate"] = {"completed": 4, "failed": 1}
    _write_status(tmp_path, stats)
    body = pipeline_live(tmp_path / "r.db")
    edit = body["pools"]["edit"]
    generate = body["pools"]["generate"]
    assert edit["seats"][0]["task"]["skill_name"] == "alpha"
    assert edit["seats"][1] is None
    assert generate["seats"][1]["task"]["job_id"] == "abc123"
    assert generate["seats"][0] is None
    assert generate["queue"][0]["job_id"] == "def456"
    assert generate["completed"] == 4
    assert generate["failed"] == 1


def test_tail_log_reads_generate_job(tmp_path):
    log_dir = tmp_path / "logs" / "agents" / "generate_agents" / "alice"
    log_dir.mkdir(parents=True)
    (log_dir / "jobdeadbeef.log").write_text("TURN 1\nthinking\n", encoding="utf-8")
    body = tail_task_log(tmp_path / "r.db", kind="generate", name="jobdeadbeef")
    assert body["exists"] is True
    assert body["lines"] == ["TURN 1", "thinking"]

def test_tail_log_missing_file_is_explicit_empty_state(tmp_path):
    body = tail_task_log(tmp_path / "r.db", kind="skill", name="nope")
    assert body["exists"] is False
    assert body["lines"] == []
    assert "暂无日志" in body["message"]


def test_tail_log_reads_last_lines(tmp_path):
    log_dir = tmp_path / "logs" / "agents" / "skill_edit_agents" / "skills"
    log_dir.mkdir(parents=True)
    (log_dir / "alpha.log").write_text(
        "\n".join(f"line-{index}" for index in range(10)), encoding="utf-8")
    body = tail_task_log(tmp_path / "r.db", kind="skill", name="alpha", tail=3)
    assert body["exists"] is True
    assert body["lines"] == ["line-7", "line-8", "line-9"]


def test_tail_log_rejects_path_traversal_and_bad_kind(tmp_path):
    with pytest.raises(ValueError):
        tail_task_log(tmp_path / "r.db", kind="skill", name="../secret")
    with pytest.raises(ValueError):
        tail_task_log(tmp_path / "r.db", kind="skill", name="a/b")
    with pytest.raises(ValueError):
        tail_task_log(tmp_path / "r.db", kind="atom_batch", name="x")


# ── router 端点 ───────────────────────────────────────────────────

def _client(home, *, expose_sensitive=True):
    db = home / "r.db"
    conn = get_connection(db)
    conn.close()
    app = FastAPI()
    app.include_router(build_dashboard_router(
        db_path=db, expose_sensitive=expose_sensitive))
    return TestClient(app)


def test_live_endpoint_not_running_explicit_state(tmp_path):
    r = _client(tmp_path).get("/api/v1/dashboard/pipeline/live")
    assert r.status_code == 200
    assert r.json()["running"] is False


def test_live_endpoint_serves_full_monitor_shape(tmp_path):
    _write_status(tmp_path, _full_stats())
    body = _client(tmp_path).get("/api/v1/dashboard/pipeline/live").json()
    assert body["running"] is True
    assert body["pools"]["edit"]["seats"][0]["task"]["skill_name"] == "alpha"
    assert body["pools"]["split"]["queue"][0]["traj_id"] == "traj_b"


def test_live_endpoint_standalone_strips_task_identity(tmp_path):
    """公网只读实例：席位只留占用/计时，任务身份（skill/traj/atom 名）剥掉。"""
    _write_status(tmp_path, _full_stats())
    body = _client(tmp_path, expose_sensitive=False).get(
        "/api/v1/dashboard/pipeline/live").json()
    assert body["running"] is True
    seat = body["pools"]["edit"]["seats"][0]
    assert seat is not None and "started_at" in seat
    assert "task" not in seat
    assert body["pools"]["split"]["queue"] == []
    assert body["pools"]["split"]["queued"] == 1  # 计数仍在


def test_log_endpoint_only_on_sensitive_router(tmp_path):
    log_dir = tmp_path / "logs" / "agents" / "task_agents"
    log_dir.mkdir(parents=True)
    (log_dir / "traj_a.log").write_text("TURN 1\nTOOL read\n", encoding="utf-8")

    r = _client(tmp_path).get("/api/v1/dashboard/pipeline/log",
                              params={"kind": "traj", "name": "traj_a"})
    assert r.status_code == 200
    assert r.json()["lines"] == ["TURN 1", "TOOL read"]

    r = _client(tmp_path).get("/api/v1/dashboard/pipeline/log",
                              params={"kind": "bogus", "name": "x"})
    assert r.status_code == 400

    # 公网只读实例物理不注册（内容级端点 404）
    r = _client(tmp_path, expose_sensitive=False).get(
        "/api/v1/dashboard/pipeline/log", params={"kind": "traj", "name": "traj_a"})
    assert r.status_code == 404
