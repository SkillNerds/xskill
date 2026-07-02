"""server 启动必起 watcher —— 即便 registry 为空。

回归锚点:watcher 的 ``_loop`` 每轮跑 ``on_poll_hook`` 做生态再检测,daemon
空 home 启动后新装的 agent 全靠这个 poll 循环接管(Bug #5)。历史上 watcher
启动有个 ``if dirs`` 门,靠 startup 必注册 chat_archive 凑出 ≥1 个 watch dir
才不踩坑;chat_archive 随 web 面板移除后,空 home 下 watcher 不再启动 ——
本测试锁死"无条件起 watcher"。
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from xskill import XSkillConfig


def test_watcher_starts_even_with_empty_registry(tmp_path):
    from xskill.api import app as srv
    from starlette.testclient import TestClient

    srv._config = {
        "llm": {"base_url": "x", "model": "y", "api_key": "z"},
        "embedding": {},
        "watcher": {"poll_interval": 30},
    }
    srv._skill_dir = tmp_path / "skill"
    srv._skill_dir.mkdir()
    srv._watcher_ref.clear()
    try:
        with patch("xskill.api.app.create_llm_client", return_value=MagicMock()), \
             patch("xskill.api.app.create_embed_client", return_value=MagicMock()), \
             patch("xskill.api.app.init_skill_authoring_tool_context"), \
             patch("xskill.ecosystems.detect_known_ecosystems", return_value=[]):
            from xskill.api import create_app
            app = create_app()
            # 进入 context = startup 事件已跑完;退出 = shutdown 停掉 watcher
            with TestClient(app):
                watcher = srv._watcher_ref.get("instance")
                assert watcher is not None, (
                    "空 registry 下 watcher 未启动 —— poll-hook 生态再检测会失效,"
                    "daemon 启动后新装的 agent 永远接管不了"
                )
                assert watcher.is_running
    finally:
        w = srv._watcher_ref.get("instance")
        if w is not None:
            w.stop()
        srv._watcher_ref.clear()
        srv._config = None
        srv._skill_dir = None


def test_create_app_startup_passes_same_config_to_watcher(tmp_path, monkeypatch):
    from starlette.testclient import TestClient
    from xskill.api import app as srv

    skill_dir = tmp_path / "skill"
    skill_dir.mkdir()
    config_object = XSkillConfig.from_dict({
        "skill_dir": str(skill_dir),
        "llm": {"base_url": "x", "model": "y", "api_key": "z"},
        "embedding": {
            "base_url": "embed",
            "model": "vector",
            "api_key": "key",
        },
        "watcher": {"poll_interval": 30},
    })
    captured_kwargs = {}

    class FakeDirectoryWatcher:
        def __init__(self, **kwargs):
            captured_kwargs.update(kwargs)
            self.stopped = False

        @property
        def stats(self):
            return {"running": True}

        def start(self):
            captured_kwargs["started"] = True

        def stop(self):
            self.stopped = True

    srv._watcher_ref.clear()
    try:
        def fake_create_llm_client(runtime_config):
            assert runtime_config is config_object
            return object()

        def fake_create_embed_client(runtime_config):
            assert runtime_config is config_object
            return object()

        def fake_init_skill_authoring_tool_context(**kwargs):
            assert kwargs["config"] is config_object

        def fake_detect_known_ecosystems(home_root):
            assert home_root == tmp_path.resolve()
            return []

        monkeypatch.setattr(srv, "create_llm_client", fake_create_llm_client)
        monkeypatch.setattr(srv, "create_embed_client", fake_create_embed_client)
        monkeypatch.setattr(srv, "init_skill_authoring_tool_context", fake_init_skill_authoring_tool_context)
        monkeypatch.setattr("xskill.ecosystems.detect_known_ecosystems", fake_detect_known_ecosystems)
        monkeypatch.setattr("xskill.pipeline.runner.DirectoryWatcher", FakeDirectoryWatcher)

        app = srv.create_app(home_root=tmp_path, config=config_object)
        with TestClient(app):
            assert captured_kwargs["config"] is config_object
            assert captured_kwargs["skill_dir"] == skill_dir
            assert captured_kwargs["started"] is True
    finally:
        srv._watcher_ref.clear()
        srv._config = None
        srv._skill_dir = None


def test_team_server_startup_uses_runtime_config_for_traj_root(tmp_path, monkeypatch):
    from starlette.testclient import TestClient
    from xskill.api import app as srv

    skill_dir = tmp_path / "skill"
    skill_dir.mkdir()
    team_traj_root = tmp_path / "runtime-team-trajs"
    config_object = XSkillConfig.from_dict({
        "skill_dir": str(skill_dir),
        "llm": {"base_url": "x", "model": "y", "api_key": "z"},
        "embedding": {
            "base_url": "embed",
            "model": "vector",
            "api_key": "key",
        },
        "team": {"server": {"traj_root": str(team_traj_root)}},
        "watcher": {"poll_interval": 30},
    })
    captured_context = {}

    class FakeDirectoryWatcher:
        def __init__(self, **kwargs):
            captured_context["watcher_config"] = kwargs["config"]

        def start(self):
            captured_context["watcher_started"] = True

        def stop(self):
            captured_context["watcher_stopped"] = True

    class FakeClientRegistry:
        def __init__(self, path):
            captured_context["clients_db_path"] = path

    def fail_get_config():
        raise AssertionError("team startup should not read global config")

    def fake_init_team_context(**kwargs):
        captured_context.update(kwargs)

    def fake_register_dir(path, label, ecosystem=None):
        captured_context["registered_path"] = path
        captured_context["registered_label"] = label
        captured_context["registered_ecosystem"] = ecosystem

    def fake_create_llm_client(runtime_config):
        assert runtime_config is config_object
        return object()

    def fake_create_embed_client(runtime_config):
        assert runtime_config is config_object
        return object()

    def fake_init_skill_authoring_tool_context(**kwargs):
        assert kwargs["config"] is config_object

    def fake_ensure_join_token(path):
        captured_context["state_path"] = path
        return "token"

    srv._watcher_ref.clear()
    try:
        monkeypatch.setattr("xskill.config.get_config", fail_get_config)
        monkeypatch.setattr(srv, "create_llm_client", fake_create_llm_client)
        monkeypatch.setattr(srv, "create_embed_client", fake_create_embed_client)
        monkeypatch.setattr(srv, "init_skill_authoring_tool_context", fake_init_skill_authoring_tool_context)
        monkeypatch.setattr("xskill.team.server.state.ensure_join_token", fake_ensure_join_token)
        monkeypatch.setattr("xskill.team.server.client_registry.ClientRegistry", FakeClientRegistry)
        monkeypatch.setattr("xskill.team.server.api.init_team_context", fake_init_team_context)
        monkeypatch.setattr("xskill.pipeline.registry.register_dir", fake_register_dir)
        monkeypatch.setattr("xskill.pipeline.runner.DirectoryWatcher", FakeDirectoryWatcher)

        app = srv.create_app(home_root=tmp_path, config=config_object, team_server=True)
        with TestClient(app):
            assert captured_context["traj_root"] == team_traj_root
            assert captured_context["watcher_config"] is config_object
            assert team_traj_root.is_dir()
    finally:
        srv._watcher_ref.clear()
        srv._config = None
        srv._skill_dir = None
