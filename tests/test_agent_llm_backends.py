"""Per-agent LLM backend resolution and watcher factory routing."""

from __future__ import annotations

from pathlib import Path

import pytest

from xskill.agents.agno_factory import resolve_agent_llm_config
from xskill.config import normalize_runtime_config
from xskill.pipeline.runner import DirectoryWatcher, _agent_context_llm_config


def _config() -> dict:
    return {
        "llm": {
            "base_url": "https://base.example.test/v1",
            "model": "base-model",
            "api_key": "base-key",
            "max_tokens": 1000,
            "rate_limit": {"rpm": 60, "request_burst": 10, "max_inflight": 4},
        },
        "llm_skill": {
            "model": "skill-model",
            "max_tokens": 2000,
            "enable_spill": False,
        },
        "llm_agents": {
            "split": {
                "base_url": "https://split.example.test/v1",
                "model": "split-model",
                "rate_limit": {"rpm": 30},
            },
            "cluster": {"model": "cluster-model"},
            "edit": {"max_tokens": 4000, "temperature": 0.0},
        },
        "agent_worker": {
            "pools": {
                "split": {"llm_weight": 6},
                "cluster": {"llm_weight": 3},
                "edit": {"llm_weight": 1},
            },
        },
    }


def test_stage_overrides_are_isolated_and_inherit_legacy_config():
    config = _config()

    split = resolve_agent_llm_config(config, "split")
    cluster = resolve_agent_llm_config(config, "cluster")
    edit = resolve_agent_llm_config(config, "edit")

    assert split["base_url"] == "https://split.example.test/v1"
    assert split["model"] == "split-model"
    assert split["api_key"] == "base-key"
    assert split["max_tokens"] == 2000
    assert split["rate_limit"] == {
        "rpm": 30,
        "request_burst": 10,
        "max_inflight": 4,
    }
    assert cluster["base_url"] == "https://base.example.test/v1"
    assert cluster["model"] == "cluster-model"
    assert edit["model"] == "skill-model"
    assert edit["max_tokens"] == 4000
    assert edit["temperature"] == 0.0
    assert split["_pool_weights"] == {"split": 6, "cluster": 3, "edit": 1}


def test_missing_stage_config_preserves_legacy_effective_values():
    config = _config()
    config.pop("llm_agents")

    legacy = resolve_agent_llm_config(config)

    for stage in ("split", "cluster", "edit"):
        assert resolve_agent_llm_config(config, stage) == legacy


def test_resolver_does_not_mutate_user_config():
    config = _config()

    resolve_agent_llm_config(config, "split")["model"] = "changed"

    assert config["llm_agents"]["split"]["model"] == "split-model"
    assert "_pool_weights" not in config["llm"]


def test_agent_context_config_preserves_stage_specific_legacy_inheritance():
    config = _config()
    config["llm"]["enable_spill"] = True

    edit = _agent_context_llm_config(config, "edit", inherit_skill=True)
    cluster = _agent_context_llm_config(config, "cluster", inherit_skill=False)

    assert edit["enable_spill"] is False
    assert edit["temperature"] == 0.0
    assert cluster["enable_spill"] is True
    assert cluster["model"] == "cluster-model"


def test_unknown_stage_is_rejected():
    with pytest.raises(ValueError, match="agent LLM stage"):
        resolve_agent_llm_config(_config(), "generate")


@pytest.mark.parametrize(
    ("config", "message"),
    [
        ({"llm_agents": []}, "llm_agents 必须是 mapping"),
        ({"llm_agents": {"split": "model"}}, "llm_agents.split 必须是 mapping"),
        ({"llm_agents": {"judge": {}}}, "llm_agents 包含未知阶段"),
        (
            {"llm_agents": {"edit": {"rate_limit": []}}},
            "llm_agents.edit.rate_limit 必须是 mapping",
        ),
    ],
)
def test_runtime_config_rejects_invalid_agent_overrides(config, message):
    with pytest.raises(ValueError, match=message):
        normalize_runtime_config({**config, "llm": {}, "embedding": {}})


def test_runtime_config_normalizes_stage_burst_without_filling_other_limits():
    effective = normalize_runtime_config({
        "llm": {},
        "embedding": {},
        "llm_agents": {
            "cluster": {"rate_limit": {"rpm": 30, "tpm": 6000, "burst": 5}},
        },
    })

    assert effective["llm_agents"]["cluster"]["rate_limit"] == {
        "rpm": 30,
        "tpm": 6000,
        "request_burst": 5,
        "token_burst": 5,
    }


def test_watcher_caches_one_factory_per_stage(monkeypatch, tmp_path):
    calls: list[str | None] = []

    def fake_make_default_factory(config, *, stage, usage_ledger, spill_root):
        del config, usage_ledger, spill_root
        calls.append(stage)
        return object()

    monkeypatch.setattr(
        "xskill.agents.agno_factory.make_default_factory",
        fake_make_default_factory,
    )
    watcher = object.__new__(DirectoryWatcher)
    watcher.agno_agent_factory = None
    watcher.config = _config()
    watcher.usage_ledger = object()
    watcher.spill_root = Path(tmp_path)

    split = watcher._factory("split")
    cluster = watcher._factory("cluster")
    edit = watcher._factory("edit")

    assert watcher._factory("split") is split
    assert watcher._factory("cluster") is cluster
    assert watcher._factory("edit") is edit
    assert calls == ["split", "cluster", "edit"]
