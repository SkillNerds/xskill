from __future__ import annotations

from fastapi.testclient import TestClient
import pytest
from dependency_injector import providers

from xskill import XSkillConfig, XSkillContainer
from xskill.core import XSkill


def test_xskill_uses_injected_container(tmp_path):
    config_object = XSkillConfig.from_dict(
        {
            "skill_dir": str(tmp_path / "skill"),
            "llm": {
                "base_url": "http://llm.example/v1",
                "model": "chat-model",
                "api_key": "llm-key",
            },
            "embedding": {
                "base_url": "http://embedding.example/v1",
                "model": "embedding-model",
                "api_key": "embedding-key",
            },
        }
    )
    fake_registry = object()
    fake_skill_repo = object()
    fake_llm_client = object()
    fake_embed_client = object()
    container = XSkillContainer()
    container.config.override(config_object)
    container.registry.override(providers.Object(fake_registry))
    container.skill_repo.override(providers.Object(fake_skill_repo))
    container.llm_client.override(providers.Object(fake_llm_client))
    container.embed_client.override(providers.Object(fake_embed_client))

    xskill_object = XSkill(container=container)

    assert xskill_object.config is config_object
    assert xskill_object.registry is fake_registry
    assert xskill_object.skill_repo is fake_skill_repo
    assert xskill_object.llm is fake_llm_client
    assert xskill_object.embed is fake_embed_client


def test_xskill_rejects_container_mixed_with_config(tmp_path):
    config_object = XSkillConfig.from_dict(
        {
            "skill_dir": str(tmp_path / "skill"),
            "llm": {
                "base_url": "http://llm.example/v1",
                "model": "chat-model",
                "api_key": "llm-key",
            },
            "embedding": {
                "base_url": "http://embedding.example/v1",
                "model": "embedding-model",
                "api_key": "embedding-key",
            },
        }
    )
    container = XSkillContainer()
    container.config.override(config_object)

    with pytest.raises(ValueError, match="container cannot be combined"):
        XSkill(config=config_object, container=container)


def test_xskill_serve_passes_same_container(monkeypatch, tmp_path):
    config_object = XSkillConfig.from_dict(
        {
            "skill_dir": str(tmp_path / "skill"),
            "llm": {
                "base_url": "http://llm.example/v1",
                "model": "chat-model",
                "api_key": "llm-key",
            },
            "embedding": {
                "base_url": "http://embedding.example/v1",
                "model": "embedding-model",
                "api_key": "embedding-key",
            },
        }
    )
    container = XSkillContainer()
    container.config.override(config_object)
    captured = {}

    def fake_create_app(**kwargs):
        captured.update(kwargs)
        return object()

    def fake_run(application, host, port):
        captured["application"] = application
        captured["host"] = host
        captured["port"] = port

    monkeypatch.setattr("xskill.api.create_app", fake_create_app)
    monkeypatch.setattr("uvicorn.run", fake_run)

    XSkill(container=container).serve(host="127.0.0.1", port=8123)

    assert captured["container"] is container
    assert captured["team_server"] is False
    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == 8123


def test_create_app_startup_passes_container_config_to_watcher(monkeypatch, tmp_path):
    from xskill.api import app as server_app

    config_object = XSkillConfig.from_dict(
        {
            "skill_dir": str(tmp_path / "skill"),
            "llm": {
                "base_url": "http://llm.example/v1",
                "model": "chat-model",
                "api_key": "llm-key",
            },
            "embedding": {
                "base_url": "http://embedding.example/v1",
                "model": "embedding-model",
                "api_key": "embedding-key",
            },
            "watcher": {"poll_interval": 30},
        }
    )
    config_object.skill_dir.mkdir()
    fake_llm_client = object()
    fake_embedding_client = object()
    fake_agent_factory = object()
    container = XSkillContainer()
    container.config.override(config_object)
    container.llm_client.override(providers.Object(fake_llm_client))
    container.embed_client.override(providers.Object(fake_embedding_client))
    container.agno_agent_factory.override(providers.Object(fake_agent_factory))
    captured = {}

    class FakeDirectoryWatcher:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def start(self):
            captured["started"] = True

        def stop(self):
            captured["stopped"] = True

    monkeypatch.setattr(
        "xskill.pipeline.runner.DirectoryWatcher",
        FakeDirectoryWatcher,
    )
    monkeypatch.setattr(
        "xskill.ecosystems.detect_known_ecosystems",
        lambda _home_root: [],
    )
    server_app._watcher_ref.clear()
    try:
        application = server_app.create_app(
            home_root=tmp_path,
            container=container,
        )
        with TestClient(application):
            assert captured["config"] is config_object
            assert captured["container"] is container
            assert captured["llm"] is fake_llm_client
            assert captured["embed_client"] is fake_embedding_client
            assert captured["agno_agent_factory"] is fake_agent_factory
            assert captured["skill_dir"] == config_object.skill_dir
            assert captured["started"] is True
    finally:
        server_app._watcher_ref.clear()
        server_app._config = None
        server_app._skill_dir = None


def test_agent_tools_description_opt_uses_passed_config(monkeypatch, tmp_path):
    from xskill.agents import agent_tools

    config_object = XSkillConfig.from_dict(
        {
            "skill_dir": str(tmp_path / "skill"),
            "llm": {
                "base_url": "http://llm.example/v1",
                "model": "chat-model",
                "api_key": "llm-key",
            },
            "embedding": {
                "base_url": "http://embedding.example/v1",
                "model": "embedding-model",
                "api_key": "embedding-key",
            },
            "skill_opt": {"enabled": True},
        }
    )
    captured = {}
    snapshot = agent_tools.agent_tool_config.snapshot()

    def fake_create_llm_client(config):
        captured["llm_config"] = config
        return None

    def fake_create_embed_client(config):
        captured["embedding_config"] = config
        return None

    monkeypatch.setattr(
        "xskill.config.get_config",
        lambda: pytest.fail("agent_tools should not read global config"),
    )
    monkeypatch.setattr(
        "xskill.utils.llm.create_llm_client",
        fake_create_llm_client,
    )
    monkeypatch.setattr(
        "xskill.utils.llm.create_embed_client",
        fake_create_embed_client,
    )
    try:
        agent_tools.init_skill_authoring_tool_context(
            tmp_path,
            tmp_path,
            config_object,
        )
        agent_tools._run_description_optimization(tmp_path, "sample")
    finally:
        agent_tools.agent_tool_config.restore(snapshot)

    assert captured["llm_config"] is config_object
    assert captured["embedding_config"] is config_object
