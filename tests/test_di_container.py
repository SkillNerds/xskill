from __future__ import annotations

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
