"""Bug #2 regression: server.py 启动时 LLM/embed 客户端构造失败必须直接抛、
不能让 daemon 带 None client 继续跑（CLAUDE.md 第 1 条：不写 fallback）。"""
from __future__ import annotations

import logging
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient


def _raise_bad_config(_, *a, **k):
    raise RuntimeError("invalid llm config (test)")


def _return_none(_, *a, **k):
    return None


@pytest.fixture
def _stub_loaded(monkeypatch, tmp_path):
    """预置 srv._config / _skill_dir，让 create_app 的 _ensure_loaded 短路。

    否则 _ensure_loaded → load_config() 会去读真实 ~/.xskill/config.yaml，
    测试就依赖机器本地状态——干净环境 / CI 上必挂（本测试此前正是如此）。
    """
    from xskill.api import app as srv

    skill_dir = tmp_path / "skill"
    skill_dir.mkdir()
    monkeypatch.setattr(srv, "_config", {
        "llm": {"base_url": "x", "model": "y", "api_key": "z"},
        "embedding": {},
        "watcher": {"poll_interval": 30},
    })
    monkeypatch.setattr(srv, "_skill_dir", skill_dir)


def test_startup_raises_when_create_llm_client_raises(monkeypatch, tmp_path, _stub_loaded):
    """create_llm_client 抛 → daemon startup 也应直接抛，不静默降级 llm=None。"""
    from xskill.api import app as srv

    monkeypatch.setattr(srv, "create_llm_client", _raise_bad_config)
    app = srv.create_app(home_root=tmp_path)

    with pytest.raises(RuntimeError, match="invalid llm config"):
        with TestClient(app):
            pass  # 触发 startup event


def test_startup_raises_when_create_llm_client_returns_none(monkeypatch, tmp_path, _stub_loaded):
    """create_llm_client 返回 None（内部 except 吞错的路径） → daemon 启动应显式断言失败。"""
    from xskill.api import app as srv

    monkeypatch.setattr(srv, "create_llm_client", _return_none)
    app = srv.create_app(home_root=tmp_path)

    with pytest.raises(RuntimeError, match="LLM client could not be created"):
        with TestClient(app):
            pass


def test_startup_raises_when_create_embed_client_fails(monkeypatch, tmp_path, _stub_loaded):
    """create_embed_client 抛 → daemon startup 也应直接抛。"""
    from xskill.api import app as srv

    # LLM 不抛、只让 embed 抛
    monkeypatch.setattr(srv, "create_embed_client", _raise_bad_config)
    app = srv.create_app(home_root=tmp_path)

    with pytest.raises(RuntimeError, match="invalid llm config"):
        with TestClient(app):
            pass


def test_standalone_worker_commands_share_resolved_ecosystem_home(
    monkeypatch, tmp_path, _stub_loaded,
):
    """standalone 的 watcher/轻量 ingester 都只能访问同一个显式生态 HOME。"""
    from xskill.api import app as srv
    from xskill.pipeline import scheduler as scheduler_module

    scheduler_records = []

    class TrackedScheduler:
        def __init__(self, name, command, **keyword_arguments):
            self.name = name
            self.command = list(command)
            self.keyword_arguments = keyword_arguments
            self.started = False
            self.stopped = False
            scheduler_records.append(self)

        def start(self):
            self.started = True

        def stop(self, timeout=5.0):
            del timeout
            self.stopped = True

    ecosystem_home = tmp_path / "isolated-harness-home"
    ecosystem_home.mkdir()
    monkeypatch.setattr(srv, "_schedulers", [])
    monkeypatch.setattr(
        srv, "create_llm_client", MagicMock(return_value=object()),
    )
    monkeypatch.setattr(
        srv, "create_embed_client", MagicMock(return_value=object()),
    )
    monkeypatch.setattr(
        srv, "init_skill_authoring_tool_context", MagicMock(),
    )
    monkeypatch.setattr(
        scheduler_module,
        "IntervalSubprocessScheduler",
        TrackedScheduler,
    )

    app = srv.create_app(home_root=ecosystem_home)
    with TestClient(app):
        assert all(record.started for record in scheduler_records)

    records_by_name = {
        record.name: record for record in scheduler_records
    }
    expected_home_arguments = [
        "--home",
        str(ecosystem_home.resolve()),
    ]
    assert set(records_by_name) == {
        "agent-worker", "ecosystem-ingest", "kernel-host", "ux-scores-sync",
    }
    assert records_by_name["agent-worker"].command[-2:] == expected_home_arguments
    assert records_by_name["agent-worker"].keyword_arguments["persistent"] is True
    kernel_command = records_by_name["kernel-host"].command
    assert kernel_command[-1] == "kernel-host"
    assert "--server" not in kernel_command
    assert records_by_name["kernel-host"].keyword_arguments["persistent"] is True
    ingest_command = records_by_name["ecosystem-ingest"].command
    home_argument_index = ingest_command.index("--home")
    assert ingest_command[
        home_argument_index:home_argument_index + 2
    ] == expected_home_arguments
    assert "--server" not in ingest_command
    ux_cmd = records_by_name["ux-scores-sync"].command
    assert ux_cmd[-1] == "ux-scores-sync"
    assert "persistent" not in records_by_name["ux-scores-sync"].keyword_arguments
    assert all(record.stopped for record in scheduler_records)


def test_team_server_schedules_only_server_watcher(
    monkeypatch, tmp_path, _stub_loaded,
):
    """team server 只跑 server watcher，不得启动本机生态采集子进程。"""
    from xskill import config as config_module
    from xskill.api import app as srv
    from xskill.pipeline import scheduler as scheduler_module
    from xskill.recommend import engine as recommend_engine
    from xskill.team.server import api as server_api
    from xskill.team.server import client_registry as registry_module
    from xskill.team.server import skill_manifest
    from xskill.team.server import state as server_state

    scheduler_records = []

    class TrackedScheduler:
        def __init__(self, name, command, **keyword_arguments):
            self.name = name
            self.command = list(command)
            self.keyword_arguments = keyword_arguments
            self.started = False
            self.stopped = False
            scheduler_records.append(self)

        def start(self):
            self.started = True

        def stop(self, timeout=5.0):
            del timeout
            self.stopped = True

    class TrackedRegistry:
        def __init__(self, database_path):
            self.database_path = database_path
            self.closed = False

        def list(self):
            return []

        def close(self):
            self.closed = True
            return True

    class SuccessfulEngine:
        def __init__(self, **_keyword_arguments):
            self.skillhub = None

    server_api.clear_team_context(profile_refresh_shutdown_timeout=0)
    skill_manifest.set_recommend_engine(None)
    monkeypatch.setattr(srv, "_schedulers", [])
    monkeypatch.setattr(
        srv, "create_llm_client", MagicMock(return_value=object()),
    )
    monkeypatch.setattr(
        srv, "create_embed_client", MagicMock(return_value=object()),
    )
    monkeypatch.setattr(
        srv, "init_skill_authoring_tool_context", MagicMock(),
    )
    monkeypatch.setattr(
        scheduler_module,
        "IntervalSubprocessScheduler",
        TrackedScheduler,
    )
    monkeypatch.setattr(
        registry_module, "ClientRegistry", TrackedRegistry,
    )
    monkeypatch.setattr(
        recommend_engine, "SkillRecommendEngine", SuccessfulEngine,
    )
    monkeypatch.setattr(
        server_state, "ensure_join_token", MagicMock(return_value="token"),
    )
    monkeypatch.setattr(
        config_module,
        "get_team_clients_db_path",
        MagicMock(return_value=tmp_path / "team_clients.db"),
    )
    monkeypatch.setattr(
        config_module,
        "get_team_server_state_path",
        MagicMock(return_value=tmp_path / "team_server.json"),
    )
    monkeypatch.setattr(
        config_module,
        "get_team_trajectories_dir",
        MagicMock(return_value=tmp_path / "team_trajectories"),
    )

    app = srv.create_app(
        home_root=tmp_path / "must-not-be-scanned",
        team_server=True,
    )
    with TestClient(app):
        assert all(record.started for record in scheduler_records)

    records_by_name = {
        record.name: record for record in scheduler_records
    }
    assert set(records_by_name) == {
        "recommend-heavy", "agent-worker", "kernel-host", "ux-scores-sync",
    }
    watcher_command = records_by_name["agent-worker"].command
    assert watcher_command[-1] == "--server"
    assert "--home" not in watcher_command
    assert records_by_name["agent-worker"].keyword_arguments["persistent"] is True
    kernel_command = records_by_name["kernel-host"].command
    assert kernel_command[-2:] == ["kernel-host", "--server"]
    assert records_by_name["kernel-host"].keyword_arguments["persistent"] is True
    assert "persistent" not in records_by_name["recommend-heavy"].keyword_arguments
    assert records_by_name["recommend-heavy"].command[-1] == "recommend-heavy"
    assert "persistent" not in records_by_name["ux-scores-sync"].keyword_arguments
    assert records_by_name["ux-scores-sync"].command[-1] == "ux-scores-sync"
    assert "ecosystem-ingest" not in records_by_name
    assert all(record.stopped for record in scheduler_records)


def test_team_context_init_failure_aborts_startup(
    monkeypatch, tmp_path, _stub_loaded, caplog,
):
    """team 上下文初始化失败必须清理后终止 startup，不能带 None context 服务。"""
    from xskill.api import app as srv
    from xskill.team.server import api as server_api
    from xskill.team.server import state as server_state

    private_error = "broken join token file at /root/private/team-state.json"

    def fail_join_token(_state_path):
        raise RuntimeError(private_error)

    monkeypatch.setattr(srv, "create_llm_client", MagicMock(return_value=object()))
    monkeypatch.setattr(srv, "create_embed_client", MagicMock(return_value=object()))
    monkeypatch.setattr(srv, "init_skill_authoring_tool_context", MagicMock())
    monkeypatch.setattr(server_state, "ensure_join_token", fail_join_token)
    caplog.set_level(logging.ERROR, logger="xskill.server")
    app = srv.create_app(home_root=tmp_path, team_server=True)

    with pytest.raises(RuntimeError, match="broken join token file"):
        with TestClient(app):
            pass

    assert "team server context init failed" in caplog.text
    assert private_error in caplog.text
    assert any(record.exc_info for record in caplog.records)
    assert server_api.team_context().client_registry is None


def test_engine_init_failure_closes_unattached_registry_once(
    monkeypatch, tmp_path, _stub_loaded, caplog,
):
    """engine 构造失败时局部 registry 尚未注入 context，也必须且只能关闭一次。"""
    from xskill import config as config_module
    from xskill.api import app as srv
    from xskill.recommend import engine as recommend_engine
    from xskill.team.server import api as server_api
    from xskill.team.server import client_registry as registry_module
    from xskill.team.server import skill_manifest
    from xskill.team.server import state as server_state

    registries = []
    private_error = "engine failed for /root/private/team-profile.db"

    class TrackedRegistry:
        def __init__(self, _database_path):
            self.close_calls = 0
            registries.append(self)

        def list(self):
            return []

        def close(self):
            self.close_calls += 1
            return True

    class FailingEngine:
        def __init__(self, **_kwargs):
            raise RuntimeError(private_error)

    server_api.clear_team_context(profile_refresh_shutdown_timeout=0)
    skill_manifest.set_recommend_engine(None)
    monkeypatch.setattr(srv, "create_llm_client", MagicMock(return_value=object()))
    monkeypatch.setattr(srv, "create_embed_client", MagicMock(return_value=object()))
    monkeypatch.setattr(srv, "init_skill_authoring_tool_context", MagicMock())
    monkeypatch.setattr(server_state, "ensure_join_token", MagicMock(return_value="token"))
    monkeypatch.setattr(
        config_module,
        "get_team_trajectories_dir",
        MagicMock(return_value=tmp_path / "team_trajectories"),
    )
    monkeypatch.setattr(registry_module, "ClientRegistry", TrackedRegistry)
    monkeypatch.setattr(recommend_engine, "SkillRecommendEngine", FailingEngine)
    caplog.set_level(logging.ERROR, logger="xskill.server")
    app = srv.create_app(home_root=tmp_path, team_server=True)

    with pytest.raises(RuntimeError, match="engine failed"):
        with TestClient(app):
            pass

    assert len(registries) == 1
    assert registries[0].close_calls == 1
    assert server_api.team_context().client_registry is None
    assert skill_manifest.get_recommend_engine() is None
    assert private_error in caplog.text
    assert "team server context init failed" in caplog.text
    assert any(record.exc_info for record in caplog.records)


def test_profile_scheduler_start_failure_cleans_attached_resources(
    monkeypatch, tmp_path, _stub_loaded, caplog,
):
    """context/executor/engine 已接线后再失败，startup 回滚必须逐项且不重复清理。"""
    from xskill import config as config_module
    from xskill.api import app as srv
    from xskill.pipeline import scheduler as scheduler_module
    from xskill.recommend import engine as recommend_engine
    from xskill.team.server import api as server_api
    from xskill.team.server import client_registry as registry_module
    from xskill.team.server import skill_manifest
    from xskill.team.server import state as server_state

    registries = []
    schedulers = []
    private_error = "scheduler failed for /root/private/profile-worker"

    class TrackedRegistry:
        def __init__(self, _database_path):
            self.close_calls = 0
            registries.append(self)

        def list(self):
            return []

        def close(self):
            self.close_calls += 1
            return True

    class SuccessfulEngine:
        def __init__(self, **_kwargs):
            self.skillhub = None

    class FailingScheduler:
        def __init__(self, *_args, **_kwargs):
            self.stop_calls = 0
            schedulers.append(self)

        def start(self):
            raise RuntimeError(private_error)

        def stop(self):
            self.stop_calls += 1

    server_api.clear_team_context(profile_refresh_shutdown_timeout=0)
    skill_manifest.set_recommend_engine(None)
    monkeypatch.setattr(srv, "create_llm_client", MagicMock(return_value=object()))
    monkeypatch.setattr(srv, "create_embed_client", MagicMock(return_value=object()))
    monkeypatch.setattr(srv, "init_skill_authoring_tool_context", MagicMock())
    monkeypatch.setattr(server_state, "ensure_join_token", MagicMock(return_value="token"))
    monkeypatch.setattr(
        config_module,
        "get_team_trajectories_dir",
        MagicMock(return_value=tmp_path / "team_trajectories"),
    )
    monkeypatch.setattr(registry_module, "ClientRegistry", TrackedRegistry)
    monkeypatch.setattr(recommend_engine, "SkillRecommendEngine", SuccessfulEngine)
    monkeypatch.setattr(
        scheduler_module, "IntervalSubprocessScheduler", FailingScheduler,
    )
    caplog.set_level(logging.ERROR, logger="xskill.server")
    app = srv.create_app(home_root=tmp_path, team_server=True)

    with pytest.raises(RuntimeError, match="scheduler failed"):
        with TestClient(app):
            pass

    assert len(registries) == 1
    assert registries[0].close_calls == 1
    assert len(schedulers) == 1
    assert schedulers[0].stop_calls == 1
    assert not hasattr(app.state, "xskill_team_sync_executor")
    assert not hasattr(app.state, "xskill_team_telemetry_executor")
    assert server_api.team_context().client_registry is None
    assert skill_manifest.get_recommend_engine() is None
    assert private_error in caplog.text
    assert any(record.exc_info for record in caplog.records)
