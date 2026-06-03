"""test_cli_stats.py — `xskill stats` 模型分布的 unknown 兜底标签

stats 的模型分布兜底标签复用 config.dashboard.default_model（与看板一致）：
没记到模型名的存量轨迹在 stats 里也按配置归类，而非永远 'unknown'。
纯展示，不碰库真实值、不影响 canary。
"""
from __future__ import annotations

import argparse
import json

import xskill.cli as cli


def _run_stats_json(monkeypatch, capsys, *, default_model: str) -> dict:
    """跑 cmd_stats --json，注入：config 的兜底模型 + 假的 model_share/usage/status。
    返回捕获到 model_share 的 unknown_label 和输出 JSON。"""
    captured: dict = {}

    def fake_model_share(db_path=None, *, unknown_label="unknown"):
        captured["unknown_label"] = unknown_label
        # 模拟真实 SQL `GROUP BY COALESCE(source_model, unknown_label)`：
        # 一条带名 MiniMax-M2.7(1) + 一条 NULL 归到 unknown_label(1)；
        # unknown_label 与已有名同名时合并成同一桶。
        counts: dict = {}
        counts["MiniMax-M2.7"] = counts.get("MiniMax-M2.7", 0) + 1
        counts[unknown_label] = counts.get(unknown_label, 0) + 1
        return [{"model": k, "trajs": v, "pct": 0.0} for k, v in counts.items()]

    monkeypatch.setattr("xskill.pipeline.registry.model_share", fake_model_share)
    monkeypatch.setattr("xskill.pipeline.registry.usage_summary",
                        lambda db_path=None: {})
    monkeypatch.setattr("xskill.runtime.read_status", lambda: {})
    monkeypatch.setattr("xskill.config.dashboard_attribution_defaults",
                        lambda: {"harness": "unknown", "model": default_model})

    rc = cli.cmd_stats(argparse.Namespace(json=True, watch=False))
    assert rc == 0
    captured["out"] = json.loads(capsys.readouterr().out)
    return captured


def test_stats_uses_dashboard_default_model_as_unknown_label(monkeypatch, capsys):
    cap = _run_stats_json(monkeypatch, capsys, default_model="MiniMax-M2.7")
    # 喂给 model_share 的兜底标签就是 config 的 default_model
    assert cap["unknown_label"] == "MiniMax-M2.7"
    # 存量 NULL 那条并进 MiniMax-M2.7，stats 里不再出现 unknown
    models = {m["model"]: m["trajs"] for m in cap["out"]["models"]}
    assert models == {"MiniMax-M2.7": 2}
    assert "unknown" not in models


def test_stats_default_label_stays_unknown_when_config_empty(monkeypatch, capsys):
    # config 没填 default_model → dashboard_attribution_defaults 退 'unknown'，
    # stats 行为与历史一致（瘦客户端无 config 也走这条）。
    cap = _run_stats_json(monkeypatch, capsys, default_model="unknown")
    assert cap["unknown_label"] == "unknown"
    models = {m["model"]: m["trajs"] for m in cap["out"]["models"]}
    assert models.get("unknown") == 1
