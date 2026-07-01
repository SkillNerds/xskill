from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from xskill import XSkillConfig
from xskill.core import XSkill
from xskill.utils.llm import EmbedClient, create_embed_client, create_llm_client


def _sample_config(skill_directory: Path) -> dict:
    return {
        "skill_dir": str(skill_directory),
        "llm": {
            "base_url": "http://llm.example/v1",
            "model": "chat-model",
            "api_key": "llm-key",
        },
        "embedding": {
            "base_url": "http://embedding.example/v1",
            "model": "embedding-model",
            "api_key": "embedding-key",
            "dim": 3,
        },
    }


def test_config_class_loads_yaml_and_exposes_sections(tmp_path):
    skill_directory = tmp_path / "skill"
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "\n".join(
            [
                f"skill_dir: {skill_directory}",
                "llm:",
                "  base_url: http://llm.example/v1",
                "  model: chat-model",
                "  api_key: llm-key",
                "embedding:",
                "  base_url: http://embedding.example/v1",
                "  model: embedding-model",
                "  api_key: embedding-key",
                "  dim: 3",
            ]
        ),
        encoding="utf-8",
    )

    config_object = XSkillConfig.from_yaml(config_path)

    assert config_object["llm"]["model"] == "chat-model"
    assert config_object.llm_config["base_url"] == "http://llm.example/v1"
    assert config_object.embedding_config["model"] == "embedding-model"
    assert config_object.skill_dir == skill_directory


def test_config_class_rejects_missing_required_keys(tmp_path):
    config_values = _sample_config(tmp_path / "skill")
    config_values["llm"]["api_key"] = ""

    with pytest.raises(KeyError, match="llm.api_key missing"):
        XSkillConfig.from_dict(config_values)


def test_xskill_accepts_config_object(tmp_path, monkeypatch):
    config_object = XSkillConfig.from_dict(_sample_config(tmp_path / "skill"))
    registry_path = tmp_path / "registry.db"

    monkeypatch.setattr(
        "xskill.pipeline.registry.REGISTRY_DB",
        registry_path,
        raising=False,
    )

    xskill_object = XSkill(config=config_object)

    assert xskill_object.config is config_object
    assert xskill_object.skill_repo.root == tmp_path / "skill"


def test_xskill_rejects_config_and_path_together(tmp_path):
    config_object = XSkillConfig.from_dict(_sample_config(tmp_path / "skill"))

    with pytest.raises(ValueError, match="cannot both be provided"):
        XSkill(config_path=tmp_path / "config.yaml", config=config_object)


def test_llm_factory_accepts_config_class(tmp_path):
    config_object = XSkillConfig.from_dict(_sample_config(tmp_path / "skill"))

    llm_client = create_llm_client(config_object)

    assert llm_client is not None
    assert llm_client.model == "chat-model"


def test_embed_factory_accepts_config_class(tmp_path, monkeypatch):
    config_object = XSkillConfig.from_dict(_sample_config(tmp_path / "skill"))
    monkeypatch.setattr(EmbedClient, "probe_dim", lambda self: self.dim)

    embed_client = create_embed_client(config_object)

    assert embed_client.model == "embedding-model"
    assert embed_client.dim == 3


def test_xskill_serve_passes_same_config_to_create_app(tmp_path, monkeypatch):
    config_object = XSkillConfig.from_dict(_sample_config(tmp_path / "skill"))
    captured_kwargs = {}

    def fake_create_app(**kwargs):
        captured_kwargs.update(kwargs)
        return MagicMock(name="fastapi-app")

    def fake_uvicorn_run(application, **kwargs):
        assert application is not None
        assert kwargs["host"] == "127.0.0.1"
        assert kwargs["port"] == 8765

    monkeypatch.setattr("xskill.api.create_app", fake_create_app)
    monkeypatch.setattr("uvicorn.run", fake_uvicorn_run)

    XSkill(config=config_object).serve(host="127.0.0.1", port=8765)

    assert captured_kwargs["config"] is config_object


def test_agent_tools_description_opt_uses_injected_config(tmp_path, monkeypatch):
    from xskill.agents import agent_tools

    config_values = _sample_config(tmp_path / "skill")
    config_values["skill_opt"] = {"enabled": True}
    config_object = XSkillConfig.from_dict(config_values)
    skill_dir = tmp_path / "skill"
    target_dir = skill_dir / "demo"
    target_dir.mkdir(parents=True)
    captured_configs = []
    saved_context = agent_tools.agent_tool_config.snapshot()

    def fake_get_config():
        raise AssertionError("agent_tools should not read global config")

    def fake_create_llm_client(runtime_config):
        captured_configs.append(runtime_config)
        return object()

    def fake_create_embed_client(runtime_config):
        captured_configs.append(runtime_config)
        return object()

    def fake_make_default_factory(runtime_config):
        captured_configs.append(runtime_config)
        return object()

    def fake_optimize_description(target, **kwargs):
        assert target == target_dir
        captured_configs.append(kwargs["config"])

    try:
        agent_tools.init_skill_authoring_tool_context(
            skill_dir=skill_dir,
            data_dir=skill_dir,
            config=config_object,
        )
        monkeypatch.setattr("xskill.config.get_config", fake_get_config)
        monkeypatch.setattr("xskill.utils.llm.create_llm_client", fake_create_llm_client)
        monkeypatch.setattr("xskill.utils.llm.create_embed_client", fake_create_embed_client)
        monkeypatch.setattr(
            "xskill.agents.agno_factory.make_default_factory",
            fake_make_default_factory,
        )
        monkeypatch.setattr(
            "xskill.skill.description_opt.optimize_description",
            fake_optimize_description,
        )

        agent_tools._run_description_optimization(target_dir, "demo")

        assert captured_configs
        assert all(runtime_config is config_object for runtime_config in captured_configs)
    finally:
        agent_tools.agent_tool_config.restore(saved_context)
