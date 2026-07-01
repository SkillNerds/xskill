"""Dependency injection container for xskill runtime services."""

from __future__ import annotations

from dependency_injector import containers, providers

from xskill.config import XSkillConfig
from xskill.pipeline.registry import Registry
from xskill.skill.repo import SkillRepo
from xskill.utils.llm import create_embed_client, create_llm_client


class XSkillContainer(containers.DeclarativeContainer):
    """Container wiring config, storage, repositories, and model clients."""

    config = providers.Dependency(instance_of=XSkillConfig)
    registry = providers.Singleton(Registry)
    skill_repo = providers.Singleton(
        SkillRepo,
        root=config.provided.skill_dir,
        registry=registry,
    )
    llm_client = providers.Singleton(create_llm_client, config=config)
    embed_client = providers.Singleton(create_embed_client, config=config)
