"""test_config_autoinit.py -- 首次运行 auto-init 配置模板

`ensure_config_exists` 在 config.yaml 缺失时写出 CONFIG_TEMPLATE，让首次
`xskill serve` 不直接抛 traceback。
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from xskill.config import (
    CONFIG_TEMPLATE,
    ensure_config_exists,
    get_traj_dir,
    interests_config,
    interests_fingerprint,
    read_interests_config,
)


def test_creates_template_when_missing(tmp_path):
    cfg = tmp_path / ".xskill" / "config.yaml"
    assert not cfg.exists()

    created = ensure_config_exists(cfg)

    assert created is False          # False = 刚创建
    assert cfg.is_file()
    assert cfg.read_text(encoding="utf-8") == CONFIG_TEMPLATE


def test_noop_when_already_exists(tmp_path):
    cfg = tmp_path / ".xskill" / "config.yaml"
    cfg.parent.mkdir(parents=True)
    cfg.write_text("skill_dir: /custom\n", encoding="utf-8")

    existed = ensure_config_exists(cfg)

    assert existed is True           # True = 已存在
    # 不覆盖用户已有内容
    assert cfg.read_text(encoding="utf-8") == "skill_dir: /custom\n"


def test_template_is_valid_yaml_with_required_sections():
    parsed = yaml.safe_load(CONFIG_TEMPLATE)
    # 必填段都在
    for key in (
        "skill_dir", "llm", "embedding", "canary", "watcher", "task_graph", "agent_worker",
    ):
        assert key in parsed, f"template missing {key}"
    # llm / embedding 带 api_key 占位符（用户要填）
    assert parsed["llm"]["api_key"] == "PUT_YOUR_LLM_API_KEY_HERE"
    assert parsed["embedding"]["api_key"] == "PUT_YOUR_EMBEDDING_API_KEY_HERE"
    assert parsed["task_graph"]["enabled"] is False


def test_template_top_level_keys_are_exactly_live_set():
    """顶层键锁定为「真有代码消费的配置段」集合——防死配置回潮。

    candidates.* 从不被代码读取（实际用硬编码 ATOM_PROMOTION_THRESHOLD），
    sandbox 段随 SWE 沙箱子系统一并删除。两者都不得再出现在模板里。
    """
    parsed = yaml.safe_load(CONFIG_TEMPLATE)
    assert set(parsed.keys()) == {
        "skill_dir", "llm", "embedding", "canary", "watcher", "task_graph", "agent_worker",
        "team", "dashboard",
        "skill_opt", "ingest", "interests", "server",
        # ingest 段由 config.ingest_config 消费（settle 屏障/去壳掩码）
        # recommend 段由 config.recommend_config 消费（推荐引擎参数）
        # skillhub 段由 config.skillhub_config 消费（三方 skill 选配）
        "recommend", "skillhub",
        "kernel",
    }, f"模板顶层键漂移：{sorted(parsed.keys())}"


class TestInterestsConfig:
    def test_missing_interests_disables_filter(self):
        assert interests_config({}) == []

    def test_empty_interests_disables_filter(self):
        assert interests_config({"interests": []}) == []

    def test_strips_blank_items_and_keeps_chinese(self):
        assert interests_config({
            "interests": [" infra ", "", "docker", " 中文 技能 "],
        }) == ["infra", "docker", "中文 技能"]

    def test_non_list_interests_raise(self):
        with pytest.raises(ValueError, match="interests"):
            interests_config({"interests": "infra"})

    def test_non_string_interest_item_raise(self):
        with pytest.raises(ValueError, match="interests"):
            interests_config({"interests": ["infra", 123]})

    def test_fingerprint_uses_normalized_order_sensitive_values(self):
        first_fingerprint = interests_fingerprint([" infra ", "", "docker"])
        second_fingerprint = interests_fingerprint(["infra", "docker"])
        reversed_fingerprint = interests_fingerprint(["docker", "infra"])

        assert first_fingerprint == second_fingerprint
        assert first_fingerprint != reversed_fingerprint

    def test_read_interests_config_reads_only_top_level_interests(self, tmp_path):
        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            "interests:\n  - 基础设施\n  - ' docker '\n",
            encoding="utf-8",
        )

        assert read_interests_config(config_path) == ["基础设施", "docker"]


def test_get_traj_dir_returns_first_registered_watch_dir(monkeypatch):
    """get_traj_dir 取第一个已注册 watch dir——轨迹来源真源是 Registry。"""
    monkeypatch.setattr(
        "xskill.pipeline.registry.list_watch_dirs",
        lambda: [{"path": "/tmp/wd-a"}, {"path": "/tmp/wd-b"}],
    )
    assert get_traj_dir() == Path("/tmp/wd-a")


def test_get_traj_dir_raises_when_no_watch_dir_registered(monkeypatch):
    """一个 watch dir 都没注册时直接抛错——不兜底到魔术目录（CLAUDE.md no-fallback）。"""
    monkeypatch.setattr("xskill.pipeline.registry.list_watch_dirs", lambda: [])
    with pytest.raises(RuntimeError, match="watch dir"):
        get_traj_dir()
