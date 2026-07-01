from __future__ import annotations

from pathlib import Path

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

    monkeypatch.setattr("xskill.pipeline.registry.REGISTRY_DB", registry_path)

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
