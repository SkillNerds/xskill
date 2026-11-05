import os
import sys
import threading
import time
from unittest.mock import MagicMock

import pytest
from starlette.testclient import TestClient

from xskill.api import app as srv
from xskill.pipeline.distillation import DistillationStore, WORKER_GENERATION_ENV, WORKER_ROOT_ENV
from xskill.pipeline.scheduler import IntervalSubprocessScheduler
from xskill.skill.catalog_store import upsert_native_skill
from xskill.utils.status_file import AGENT_WORKER_STATUS_FILE, write_status_file


API = "/api/v1/dashboard/distillation"


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    store = DistillationStore(tmp_path / "registry.db", tmp_path / "skill")
    monkeypatch.setattr("xskill.config.XSKILL_HOME", tmp_path)
    monkeypatch.setattr("xskill.dashboard.mount.XSKILL_HOME", tmp_path)
    monkeypatch.setattr("xskill.config.get_kernel_console_log_path", lambda: tmp_path / "kernel.log")
    monkeypatch.setattr(srv, "_skill_dir", store.skill_dir)
    monkeypatch.setattr(srv, "_config", {
        "dashboard": {"enabled": True, "public": True, "admins": ["owner"], "admin_password": "test-secret"},
    })
    monkeypatch.setattr(srv, "load_config", lambda: {"skill_dir": str(store.skill_dir)})
    monkeypatch.setattr(srv, "create_llm_client", MagicMock())
    monkeypatch.setattr(srv, "create_embed_client", MagicMock())
    monkeypatch.setattr(srv, "init_skill_authoring_tool_context", MagicMock())
    events = []
    schedulers = {}

    class Scheduler:
        def __init__(self, name, *args, **kwargs):
            self.name = name
            self.is_stopped = True
            self.child_pid = None
            schedulers[name] = self

        def start(self, *, env=None):
            policy = store.policy()
            if env is not None:
                assert env == {WORKER_ROOT_ENV: store.root_key, WORKER_GENERATION_ENV: str(policy.generation)}
            events.append(("start", self.name, policy.mode))
            self.is_stopped = False
            if self.name == "agent-worker":
                write_status_file(tmp_path / AGENT_WORKER_STATUS_FILE,
                                  {"distillation_generation": policy.generation,
                                   "distillation_root": policy.root_key}, ok=True, active=True)
                self.child_pid = os.getpid()

        def stop(self):
            events.append(("stop", self.name, store.policy().mode))
            self.is_stopped = True
            self.child_pid = None
            if self.name == "agent-worker":
                write_status_file(tmp_path / AGENT_WORKER_STATUS_FILE, {}, ok=True, active=False)

        def stop_gracefully(self):
            self.stop()
            return self.is_stopped

    monkeypatch.setattr("xskill.pipeline.scheduler.IntervalSubprocessScheduler", Scheduler)
    return store, events, schedulers


def login(client, secret="test-secret"):
    return client.post("/api/v1/dashboard/login", json={"user_name": "owner", "secret": secret})


def wait_for_switch(client):
    until = time.monotonic() + 5
    while time.monotonic() < until:
        response = client.get(API)
        assert response.status_code == 200
        data = response.json()
        if data["phase"] in {"ready", "failed"}:
            return data
        time.sleep(0.02)
    pytest.fail("dashboard switch did not finish")


def test_dashboard_switches_and_auth_still_works_in_restricted_mode(runtime):
    store, events, _ = runtime
    with TestClient(srv.create_app()) as client:
        assert client.get(API).status_code == 401
        assert login(client).status_code == 200
        events.clear()
        response = client.post(API, json={
            "mode": "create_one", "skill": "feishu-debug", "description": "Debug Feishu APIs",
            "generation": 0,
        })
        assert response.status_code == 202
        status = wait_for_switch(client)
        assert (status["phase"], status["mode"], status["target"]) == ("ready", "create_one", "feishu-debug")
        for name in ("agent-worker", "kernel-host", "ecosystem-ingest"):
            assert ("stop", name, "auto") in events
            assert ("start", name, "create_one") in events
        assert max(i for i, event in enumerate(events) if event[0] == "stop") < min(
            i for i, event in enumerate(events) if event[0] == "start")
        with pytest.raises(RuntimeError, match="lock|writer|running"):
            store.configure("auto")
        assert client.post("/api/v1/dashboard/logout").status_code == 200
        assert login(client, "wrong").status_code == 401
        assert login(client).status_code == 200
        assert client.post("/api/v1/dashboard/admin/config/reload", json={"raw": ""}).status_code == 409
        assert client.post(API, json={"mode": "auto", "generation": 0}).status_code == 409
        assert client.post(API, json={"mode": "auto", "generation": status["generation"]}).status_code == 202
        assert wait_for_switch(client)["mode"] == "auto"


def test_failed_target_validation_keeps_workers_stopped(runtime):
    store, events, schedulers = runtime
    with TestClient(srv.create_app()) as client:
        assert login(client).status_code == 200
        events.clear()
        assert client.post(API, json={
            "mode": "update_only", "skill": "missing", "generation": 0,
        }).status_code == 202
        status = wait_for_switch(client)
        assert status["phase"] == "failed"
        assert status["error"]
        assert store.policy().mode == "auto"
        assert schedulers["agent-worker"].is_stopped
        assert not any(event[0] == "start" for event in events)


def test_update_uses_catalog_and_serializes_switch(runtime, monkeypatch):
    store, _, schedulers = runtime
    from xskill.skill.git import init_skill_repo_on_baby

    target = store.skill_dir / "feishu-debug"
    init_skill_repo_on_baby(str(target), target.name, "Debug Feishu APIs")
    upsert_native_skill(target, db_path=store.db_path)
    unprojected = store.skill_dir / "feishu-unindexed"
    unprojected.mkdir()
    (unprojected / "SKILL.md").write_text("---\nname: feishu-unindexed\n---\n", encoding="utf-8")
    with TestClient(srv.create_app()) as client:
        assert login(client).status_code == 200
        assert client.get(API + "/skills", params={"q": "feishu"}).json() == {
            "skills": [target.name], "has_more": False,
        }
        payload = {"mode": "update_only", "skill": target.name, "generation": 0}
        assert client.post(API, json=payload, headers={"Origin": "https://other.example"}).status_code == 403
        entered = threading.Event()
        release = threading.Event()
        worker = schedulers["agent-worker"]
        original_stop = worker.stop_gracefully

        def drain():
            entered.set()
            return release.wait(5) and original_stop()

        monkeypatch.setattr(worker, "stop_gracefully", drain)
        try:
            assert client.post(API, json=payload).status_code == 202
            assert entered.wait(3)
            assert client.get(API).json()["phase"] == "stopping"
            assert client.post(API, json=payload).status_code == 409
            assert client.post("/api/v1/dashboard/admin/config/reload", json={"raw": ""}).status_code == 409
            assert store.policy().mode == "auto"
        finally:
            release.set()
        status = wait_for_switch(client)
        assert (status["phase"], status["mode"], status["target"]) == ("ready", "update_only", target.name)


def test_writer_drain_timeout_prevents_switch(runtime, monkeypatch):
    store, events, schedulers = runtime
    with TestClient(srv.create_app()) as client:
        assert login(client).status_code == 200
        events.clear()
        monkeypatch.setattr(schedulers["agent-worker"], "stop_gracefully", lambda: False)
        assert client.post(API, json={
            "mode": "create_one", "skill": "chosen", "description": "Useful skill", "generation": 0,
        }).status_code == 202
        status = wait_for_switch(client)
        assert status["phase"] == "failed"
        assert store.policy().generation == 0
        assert not any(event[0] == "start" for event in events)


def test_switch_routes_are_absent_in_team_and_external_modes(runtime, monkeypatch):
    store, _, _ = runtime
    assert TestClient(srv.create_app(team_server=True)).get(API).status_code == 404
    client_state = store.db_path.parent / "team_client.json"
    client_state.write_text("{}", encoding="utf-8")
    assert TestClient(srv.create_app()).get(API).status_code == 404
    client_state.unlink()
    monkeypatch.setitem(srv._config, "kernel", {"active": "openearth"})
    assert TestClient(srv.create_app()).get(API).status_code == 404


def test_worker_rejects_a_different_library_before_start(tmp_path, monkeypatch):
    from tests.test_agent_worker import _patch_watcher
    from xskill import _workers

    built, _ = _patch_watcher(monkeypatch, tmp_path)
    monkeypatch.setenv(WORKER_ROOT_ENV, str(tmp_path / "different-library"))
    monkeypatch.setenv(WORKER_GENERATION_ENV, "0")
    assert _workers.run_agent_worker_forever(stop_event=threading.Event()) == 1
    assert not built


def test_scheduler_gracefully_stops_child_using_parent_pipe(tmp_path):
    marker = tmp_path / "drained"
    command = [
        sys.executable, "-c",
        "import os, sys; from pathlib import Path; os.read(0, 1); Path(sys.argv[1]).touch()",
        str(marker),
    ]
    scheduler = IntervalSubprocessScheduler(
        "cooperative-worker", command, interval=0.1, timeout=1,
        persistent=True, stop_on_stdin_close=True,
    )
    scheduler.start()
    try:
        deadline = time.monotonic() + 5
        while scheduler.child_pid is None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert scheduler.child_pid is not None
        assert scheduler.stop_gracefully()
        assert scheduler.is_stopped and marker.is_file()
    finally:
        scheduler.stop()
