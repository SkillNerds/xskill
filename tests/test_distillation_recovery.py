"""Lifecycle admission and recovery checks never run real ingestion or models."""
import threading

import pytest

from xskill import _workers
from xskill.pipeline.distillation import DistillationStore
from xskill.tasks.locking import root_run_lock


def test_restricted_server_worker_refuses_before_model_construction(tmp_path, monkeypatch):
    from tests.test_agent_worker import _patch_watcher
    built, _ = _patch_watcher(monkeypatch, tmp_path)
    store = DistillationStore(tmp_path / "registry.db", tmp_path / "skill")
    store.configure("create_one", "chosen", "Useful methods")
    event = threading.Event()
    event.set()
    assert _workers.run_agent_worker_forever(server=True, stop_event=event) == 1
    assert built == []
    assert not (store.skill_dir / "chosen").exists()


def test_native_writer_holds_lock_until_watcher_stop_finishes(tmp_path, monkeypatch):
    from tests.test_agent_worker import _patch_watcher, _FakeWatcher
    _patch_watcher(monkeypatch, tmp_path)
    seen = []
    def stop(watcher):
        # Probe from another thread; the lifecycle lock must still be held.
        def probe():
            try:
                with root_run_lock(tmp_path, tmp_path / "skill"):
                    seen.append("incorrectly acquired")
            except RuntimeError:
                seen.append("locked")
        thread = threading.Thread(target=probe)
        thread.start()
        thread.join(2)
        watcher.stopped = True
    monkeypatch.setattr(_FakeWatcher, "stop", stop)
    event = threading.Event()
    event.set()
    assert _workers.run_agent_worker_forever(stop_event=event) == 0
    assert seen == ["locked"]
    with root_run_lock(tmp_path, tmp_path / "skill"):
        pass


def test_startup_rechecks_create_target_and_rejects_replaced_update(tmp_path):
    store = DistillationStore(tmp_path / "registry.db", tmp_path / "skills")
    store.configure("create_one", "chosen", "Useful methods")
    path = store.skill_dir / "chosen"
    path.mkdir(parents=True)
    (path / "SKILL.md").write_text("unowned", encoding="utf-8")
    with pytest.raises(ValueError, match="ownership"):
        store.ensure_target()
    assert (path / "SKILL.md").read_text() == "unowned"


def test_owned_create_restart_retains_mode_and_target(tmp_path, monkeypatch):
    monkeypatch.setattr("xskill.skill.catalog_store.notify_native_upsert", lambda *a, **k: None)
    store = DistillationStore(tmp_path / "registry.db", tmp_path / "skills")
    run = store.configure("create_one", "chosen", "Useful methods")
    path = store.ensure_target()
    assert store.run()["owns_target"] == 1
    assert path.name == "chosen"
    restarted = DistillationStore(store.db_path, store.skill_dir)
    assert restarted.ensure_target() == path
    assert restarted.policy().run_id == run["run_id"]
    assert restarted.policy().mode == "create_one"


def test_status_does_not_create_registry_or_skill_root(tmp_path):
    store = DistillationStore(tmp_path / "state" / "registry.db", tmp_path / "skills")
    assert store.status()["mode"] == "auto"
    assert not (tmp_path / "state").exists()
    assert not store.skill_dir.exists()


def test_restricted_app_rejects_server_before_clients(tmp_path, monkeypatch):
    from xskill.api import app as srv
    from starlette.testclient import TestClient
    monkeypatch.setattr("xskill.config.XSKILL_HOME", tmp_path)
    monkeypatch.setattr(srv, "_config", {"watcher": {}})
    monkeypatch.setattr(srv, "_skill_dir", tmp_path / "skill")
    DistillationStore(tmp_path / "registry.db", tmp_path / "skill").configure("create_one", "chosen", "Useful methods")
    def forbidden(*args, **kwargs):
        raise AssertionError("unsupported startup reached model construction")
    monkeypatch.setattr(srv, "create_llm_client", forbidden)
    with pytest.raises(RuntimeError, match="standalone|native"):
        with TestClient(srv.create_app(team_server=True)):
            pass


def test_restricted_app_rejects_management_request_before_handler(tmp_path, monkeypatch):
    from xskill.api import app as srv
    from starlette.testclient import TestClient
    monkeypatch.setattr("xskill.config.XSKILL_HOME", tmp_path)
    monkeypatch.setattr(srv, "_config", {"watcher": {}})
    monkeypatch.setattr(srv, "_skill_dir", tmp_path / "skill")
    DistillationStore(tmp_path / "registry.db", tmp_path / "skill").configure("create_one", "chosen", "Useful methods")
    app = srv.create_app()
    calls = []
    @app.post("/api/v1/test-administrative-write")
    async def write():
        calls.append("side effect")
        return {"ok": True}
    # Omitting the context deliberately avoids background startup.
    response = TestClient(app).post("/api/v1/test-administrative-write")
    assert response.status_code == 409
    assert calls == []


def test_external_kernel_host_refuses_persisted_restriction_before_catalog(tmp_path, monkeypatch):
    monkeypatch.setattr("xskill.config.XSKILL_HOME", tmp_path)
    monkeypatch.setattr("xskill.config.load_config", lambda: {"kernel": {"active": "openearth"}})
    DistillationStore(tmp_path / "registry.db", tmp_path / "skill").configure("create_one", "chosen", "Useful methods")
    def forbidden(*args, **kwargs):
        raise AssertionError("external kernel catalog constructed")
    monkeypatch.setattr("xskill.kernels.catalog.KernelCatalog", forbidden)
    assert _workers.run_kernel_host(stop_event=threading.Event(), max_cycles=1) == 1


def test_host_lock_blocks_configuration_between_child_restarts(tmp_path):
    from xskill.tasks.locking import root_host_lock
    store = DistillationStore(tmp_path / "registry.db", tmp_path / "skill")
    with root_host_lock(tmp_path, store.skill_dir):
        with pytest.raises(RuntimeError, match="lock|running|writer"):
            store.configure("create_one", "chosen", "Useful methods")
    assert store.run() is None


def test_rejected_second_worker_does_not_change_running_run_state(tmp_path, monkeypatch):
    from tests.test_agent_worker import _patch_watcher
    built, _ = _patch_watcher(monkeypatch, tmp_path)
    store = DistillationStore(tmp_path / "registry.db", tmp_path / "skill")
    store.configure("create_one", "chosen", "Useful methods")
    store.set_state("running")
    event = threading.Event()
    event.set()
    with root_run_lock(tmp_path, store.skill_dir):
        assert _workers.run_agent_worker_forever(stop_event=event) == 1
    assert built == []
    assert store.run()["state"] == "running"


def test_restricted_discovery_keeps_ingestion_without_install_or_rotation(tmp_path, monkeypatch):
    from xskill.pipeline.watcher_factory import ingest_detected_ecosystems_once
    from xskill import ecosystems
    detections = [{"ecosystem": name, "bridge": tmp_path / name}
                  for name in ("codex", "claude_code", "openclaw")]
    monkeypatch.setattr(ecosystems, "detect_known_ecosystems", lambda **kwargs: detections)
    calls = []
    class FakeIngester:
        def __init__(self, *args, **kwargs):
            calls.append(kwargs)
        def run_once(self):
            pass
    monkeypatch.setattr(ecosystems, "CCSessionIngester", FakeIngester)
    monkeypatch.setattr(ecosystems, "JsonlIngester", FakeIngester)
    def forbidden(*args, **kwargs):
        raise AssertionError("restricted discovery invoked installation or canary rotation")
    for name in ("install_all_to_codex", "install_all_to_claude_code", "install_all_to_openclaw", "make_openclaw_canary_flip_hook"):
        monkeypatch.setattr(ecosystems, name, forbidden)
    ingest_detected_ecosystems_once({}, tmp_path / "home", tmp_path / "skill",
                                   registry_db_path=tmp_path / "registry.db",
                                   install_history_path=tmp_path / "install_history.jsonl",
                                   install_skills=False)
    assert len(calls) == 3
    claude = next(call for call in calls if call["target_traj_dir"].name == "claude_code")
    assert claude["skill_dir"] is None
    assert claude["history_path"] is None
    assert claude["assignments_path"] is None
    openclaw = next(call for call in calls if call["target_traj_dir"].name == "openclaw")
    assert openclaw["on_new_sessions"] is None


def test_restricted_app_host_guard_cleans_up_after_shutdown(tmp_path, monkeypatch):
    from unittest.mock import MagicMock
    from starlette.testclient import TestClient
    from xskill.api import app as srv
    monkeypatch.setattr("xskill.config.XSKILL_HOME", tmp_path)
    monkeypatch.setattr("xskill.config.get_kernel_console_log_path", lambda: tmp_path / "kernel.log")
    monkeypatch.setattr(srv, "_config", {"watcher": {"poll_interval": 30}})
    monkeypatch.setattr(srv, "_skill_dir", tmp_path / "skill")
    monkeypatch.setattr(srv, "create_llm_client", lambda *a, **k: MagicMock())
    monkeypatch.setattr(srv, "create_embed_client", lambda *a, **k: MagicMock())
    monkeypatch.setattr(srv, "init_skill_authoring_tool_context", MagicMock())
    monkeypatch.setattr("xskill.pipeline.scheduler.IntervalSubprocessScheduler.start", MagicMock())
    store = DistillationStore(tmp_path / "registry.db", tmp_path / "skill")
    store.configure("create_one", "chosen", "Useful methods")
    with TestClient(srv.create_app()):
        with pytest.raises(RuntimeError, match="lock|writer|running"):
            store.configure("auto")
    store.configure("auto")
    assert store.policy().mode == "auto"
