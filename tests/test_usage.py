"""test_usage.py —— LLM token/成本统计核心逻辑(Issue #43)"""
from __future__ import annotations

import pytest

from xskill import usage as U
from xskill.usage import (
    ModelPrice, PriceTable, Usage, UsageLedger, cost_usd, extract_usage,
    load_price_table,
)


# ── extract_usage ────────────────────────────────────────────────
class _Obj:
    def __init__(self, **kw):
        self.usage = type("u", (), kw)()


def test_extract_usage_dict_with_cache():
    resp = {"usage": {"prompt_tokens": 1100, "completion_tokens": 140,
                      "total_tokens": 1240, "prompt_cache_hit_tokens": 900,
                      "prompt_cache_miss_tokens": 200}}
    u = extract_usage(resp)
    assert (u.prompt, u.completion, u.total, u.cache_hit, u.cache_miss) == (1100, 140, 1240, 900, 200)


def test_extract_usage_sdk_object_total_derived():
    u = extract_usage(_Obj(prompt_tokens=10, completion_tokens=5))
    assert u.total == 15  # total_tokens 缺失 → prompt+completion


def test_extract_usage_missing_usage():
    assert extract_usage({"choices": []}) == Usage()
    assert extract_usage(None) == Usage()


# ── PriceTable 解析优先级 ────────────────────────────────────────
def _table():
    vendored = {"deepseek-v4-flash": {"input_per_1m": 0.14, "output_per_1m": 0.28,
                                      "cache_hit_per_1m": 0.014},
                "text-embedding-v4": {"embed_per_1m": 0.02}}
    cfg = {"default": {"input_per_1m": 2.0, "output_per_1m": 6.0},
           "deepseek-v4-flash": {"input_per_1m": 0.10, "output_per_1m": 0.20}}
    return PriceTable(vendored, cfg)


def test_resolve_config_overrides_static():
    p, src = _table().resolve("deepseek-v4-flash")
    assert src == "config" and p.input_per_1m == 0.10


def test_resolve_static_then_default():
    t = PriceTable({"text-embedding-v4": {"embed_per_1m": 0.02}}, None)
    p, src = t.resolve("text-embedding-v4")
    assert src == "static" and p.embed_per_1m == 0.02
    p2, src2 = t.resolve("never-heard-of-it")
    assert src2 == "default" and p2.input_per_1m == U._FALLBACK_DEFAULT["input_per_1m"]


# ── cost_usd ─────────────────────────────────────────────────────
def test_cost_plain_prompt_completion():
    price = ModelPrice(input_per_1m=1.0, output_per_1m=2.0)
    c = cost_usd(Usage(prompt=1_000_000, completion=500_000), price)
    assert c == pytest.approx(1.0 + 1.0)  # 1M×$1 + 0.5M×$2


def test_cost_cache_split_pricing():
    price = ModelPrice(input_per_1m=1.0, output_per_1m=0.0,
                       cache_hit_per_1m=0.1, cache_miss_per_1m=1.0)
    # 全是 cache hit → 便宜十倍
    c = cost_usd(Usage(prompt=1_000_000, cache_hit=1_000_000, cache_miss=0), price)
    assert c == pytest.approx(0.1)


def test_cost_embedding_uses_embed_price():
    price = ModelPrice(input_per_1m=99.0, output_per_1m=99.0, embed_per_1m=0.02)
    c = cost_usd(Usage(total=1_000_000), price, is_embed=True)
    assert c == pytest.approx(0.02)


# ── UsageLedger ──────────────────────────────────────────────────
def test_ledger_accumulates_and_flags_estimated(monkeypatch):
    monkeypatch.setattr(U, "_persist", lambda *a, **k: None)  # 不碰真实 registry
    led = UsageLedger(_table())
    led.record_llm("atom_split", "deepseek-v4-flash",
                   {"usage": {"prompt_tokens": 1000, "completion_tokens": 200,
                              "total_tokens": 1200}})
    led.record_embed("text-embedding-v4", {"usage": {"total_tokens": 800}})
    snap = led.snapshot()
    assert snap["total_calls"] == 2
    assert snap["total_tokens"] == 2000
    assert snap["by_step"]["atom_split"]["calls"] == 1
    assert snap["by_step"]["embedding"]["tokens"] == 800
    # embedding 命中 static 价 → estimated=True
    assert snap["estimated"] is True
    assert snap["total_usd"] > 0


def test_ledger_record_never_raises(monkeypatch):
    monkeypatch.setattr(U, "_persist", lambda *a, **k: None)
    led = UsageLedger(_table())
    led.record_llm("x", "m", object())      # 垃圾 resp,不应抛
    led.record_llm("x", "m", None)
    assert led.snapshot()["total_calls"] == 2  # 仍记账(token=0)


def test_ledger_persists_only_to_explicit_instance_db(
    tmp_path, monkeypatch,
):
    from unittest.mock import Mock

    from xskill.pipeline import registry

    instance_db_path = tmp_path / "instance" / "registry.db"
    global_db_path = tmp_path / "global" / "registry.db"
    monkeypatch.setattr(
        registry,
        "get_registry_db_path",
        Mock(return_value=global_db_path),
    )
    ledger = UsageLedger(_table(), db_path=instance_db_path)

    ledger.record_llm(
        "atom_split",
        "deepseek-v4-flash",
        {
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 5,
                "total_tokens": 15,
            }
        },
    )

    assert registry.usage_summary(instance_db_path)["total_calls"] == 1
    assert not global_db_path.exists()


# ── vendored 表加载 ──────────────────────────────────────────────
def test_load_vendored_price_table():
    t = load_price_table(None)
    p, src = t.resolve("deepseek-v4-flash")
    assert src == "static" and p.input_per_1m > 0


# ── batch1: render header + runtime ──────────────────────────────
from xskill.usage import render_stats


def test_render_stats_running_with_models():
    out = render_stats(
        {"today_usd": 0.5, "total_usd": 1.0, "total_tokens": 1200, "total_calls": 3,
         "estimated": True,
         "by_step": [{"step": "atom_split", "tokens": 1000, "cost": 0.4, "calls": 2}]},
        status={"running": True, "pid": 42, "port": 8000, "role": "standalone",
                "llm_model": "deepseek-v4-flash",
                "llm_base_url": "https://api.deepseek.com",
                "embed_model": "text-embedding-v4"})
    assert "serve 运行中" in out and "pid 42" in out and "standalone" in out
    assert "deepseek-v4-flash @ api.deepseek.com" in out
    assert "atom_split" in out and "今日 $0.5000" in out


def test_render_stats_not_running():
    out = render_stats(
        {"today_usd": 0, "total_usd": 0, "total_tokens": 0, "total_calls": 0,
         "estimated": False, "by_step": []},
        status={"running": False, "role": "client"})
    assert "serve 未运行" in out and "· client" in out


def _empty_summary():
    return {"today_usd": 0, "total_usd": 0, "total_tokens": 0, "total_calls": 0,
            "estimated": False, "by_step": []}


def test_render_price_warn_shown_when_failing():
    out = render_stats(_empty_summary(), status={"running": False, "role": "x"},
                       price_health={"ok": False, "kind": "schema_changed",
                                     "stale_days": 12.0, "error": "..."})
    assert "价格表" in out and "上游格式变更" in out and "12d" in out


def test_render_price_warn_hidden_when_healthy():
    out = render_stats(_empty_summary(), status={"running": False, "role": "x"},
                       price_health={"ok": True, "kind": None, "stale_days": 0.1})
    assert "价格表" not in out


def test_runtime_alive():
    import os
    from xskill.runtime import _alive
    assert _alive(os.getpid()) is True
    assert _alive(2_000_000_000) is False
    assert _alive(None) is False


# ── batch2: source_model / model_share / 模型占比渲染 ────────────
def test_sidecar_model(tmp_path):
    import json as _j
    from xskill.pipeline.registry import _sidecar_model
    md = tmp_path / "traj_x.md"; md.write_text("x", encoding="utf-8")
    (tmp_path / "traj_x.json").write_text(_j.dumps({"model": "qwen-max"}), encoding="utf-8")
    assert _sidecar_model(md) == "qwen-max"
    md2 = tmp_path / "traj_y.md"; md2.write_text("y", encoding="utf-8")
    assert _sidecar_model(md2) is None


def test_model_share(tmp_path):
    from xskill.pipeline import registry as R
    db = tmp_path / "r.db"
    conn = R.get_connection(db)
    conn.execute("INSERT INTO watch_dirs(path) VALUES('/x')")
    wid = conn.execute("SELECT id FROM watch_dirs").fetchone()[0]
    for fn, m in [("a.md", "qwen"), ("b.md", "qwen"), ("c.md", "deepseek"), ("d.md", None)]:
        conn.execute("INSERT INTO trajectories(watch_dir_id,filename,source_model)"
                     " VALUES(?,?,?)", (wid, fn, m))
    conn.commit(); conn.close()
    d = {x["model"]: x for x in R.model_share(db)}
    assert d["qwen"]["trajs"] == 2 and d["qwen"]["pct"] == 50.0
    assert d["unknown"]["trajs"] == 1


def test_render_stats_model_share():
    out = render_stats(
        {"today_usd": 0, "total_usd": 0, "total_tokens": 0, "total_calls": 0,
         "estimated": False, "by_step": []},
        status={"running": False, "role": "server"},
        models=[{"model": "qwen", "trajs": 50, "pct": 50.0},
                {"model": "unknown", "trajs": 50, "pct": 50.0}])
    assert "用户模型" in out and "qwen" in out and "50.0%" in out
