"""test_dashboard_config.py —— config.dashboard 段解析"""
from __future__ import annotations

from xskill.config import dashboard_config, dashboard_attribution_defaults


def test_dashboard_config_defaults_when_absent():
    assert dashboard_config({}) == {
        "enabled": False, "public": False, "password": "",
        "default_harness": "unknown", "default_model": "unknown"}


def test_dashboard_config_reads_values():
    cfg = {"dashboard": {"enabled": True, "public": True, "password": "s3cret",
                         "default_harness": "claude_code", "default_model": "deepseek-v4-flash"}}
    assert dashboard_config(cfg) == {
        "enabled": True, "public": True, "password": "s3cret",
        "default_harness": "claude_code", "default_model": "deepseek-v4-flash"}


def test_dashboard_config_partial_fills_defaults():
    assert dashboard_config({"dashboard": {"enabled": True}}) == {
        "enabled": True, "public": False, "password": "",
        "default_harness": "unknown", "default_model": "unknown"}


def test_attribution_empty_string_resolves_to_unknown():
    # 留空（空串/全空白）→ 'unknown'，保持历史行为
    cfg = {"dashboard": {"default_harness": "", "default_model": "   "}}
    dc = dashboard_config(cfg)
    assert dc["default_harness"] == "unknown"
    assert dc["default_model"] == "unknown"


def test_attribution_value_is_stripped():
    cfg = {"dashboard": {"default_harness": " codex ", "default_model": " gpt-4o "}}
    dc = dashboard_config(cfg)
    assert dc["default_harness"] == "codex"
    assert dc["default_model"] == "gpt-4o"


def test_attribution_defaults_reads_yaml_file(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text("dashboard:\n  default_harness: codex\n  default_model: my-model\n",
                 encoding="utf-8")
    assert dashboard_attribution_defaults(p) == {"harness": "codex", "model": "my-model"}


def test_attribution_defaults_missing_file_is_unknown(tmp_path):
    # config 不存在 → 显式缺省 'unknown'（不读 api_key，独立看板可用）
    assert dashboard_attribution_defaults(tmp_path / "nope.yaml") == \
        {"harness": "unknown", "model": "unknown"}
