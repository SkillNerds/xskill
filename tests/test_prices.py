"""test_prices.py —— 模型价格 拉取/解析/运行时后台刷新(Issue #43 价格更新)"""
from __future__ import annotations

import json
import time

import pytest

from xskill import prices as P
from xskill.usage import load_price_table


# ── parse_litellm:chat / embedding / cache 字段 ──────────────────
def test_parse_litellm_chat_embed_and_cache():
    raw = {
        "gpt-x": {"mode": "chat", "input_cost_per_token": 1e-6,
                  "output_cost_per_token": 3e-6,
                  "cache_read_input_token_cost": 1e-7},
        "emb-x": {"mode": "embedding", "input_cost_per_token": 5e-8},
        "no-price": {"mode": "chat"},          # 无价 → 跳过
        "garbage": "not-a-dict",               # 非 dict → 跳过
    }
    out = P.parse_litellm(raw)
    assert out["gpt-x"] == {"input_per_1m": 1.0, "output_per_1m": 3.0,
                            "cache_hit_per_1m": 0.1, "cache_miss_per_1m": 1.0}
    assert out["emb-x"] == {"embed_per_1m": 0.05}
    assert "no-price" not in out and "garbage" not in out


class _FakeResp:
    def __init__(self, raw: dict):
        self._b = json.dumps(raw).encode("utf-8")

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_fetch_and_build_rejects_too_few(monkeypatch):
    # 解析条目过少(< MIN_ENTRIES)→ 抛错(模拟源格式变更),build 路径据此 exit 1
    monkeypatch.setattr(P.urllib.request, "urlopen",
                        lambda *a, **k: _FakeResp({"m": {"mode": "chat",
                                                         "input_cost_per_token": 1e-6}}))
    with pytest.raises(ValueError):
        P.fetch_and_build()


def test_fetch_and_build_ok_has_meta(monkeypatch):
    raw = {f"m{i}": {"mode": "chat", "input_cost_per_token": 1e-6,
                     "output_cost_per_token": 2e-6} for i in range(P.MIN_ENTRIES)}
    monkeypatch.setattr(P.urllib.request, "urlopen", lambda *a, **k: _FakeResp(raw))
    payload = P.fetch_and_build()
    assert payload["_meta"]["source"] == P.PRICES_URL
    assert payload["m0"] == {"input_per_1m": 1.0, "output_per_1m": 2.0}
    assert len(payload) - 1 == P.MIN_ENTRIES


# ── load_price_table:用户缓存优先于 vendored seed ────────────────
def test_load_prefers_user_cache_over_vendored(tmp_path):
    vendored = tmp_path / "seed.json"
    vendored.write_text(json.dumps({"m": {"input_per_1m": 9.0, "output_per_1m": 9.0}}))
    cache = tmp_path / "cache.json"
    cache.write_text(json.dumps({"m": {"input_per_1m": 1.0, "output_per_1m": 2.0}}))

    table = load_price_table(vendored_path=vendored, cache_path=cache)
    price, source = table.resolve("m")
    assert (price.input_per_1m, price.output_per_1m) == (1.0, 2.0)  # 用了缓存,不是 seed
    assert source == "static"


def test_load_falls_back_to_vendored_when_no_cache(tmp_path):
    vendored = tmp_path / "seed.json"
    vendored.write_text(json.dumps({"m": {"input_per_1m": 9.0, "output_per_1m": 9.0}}))
    missing = tmp_path / "nope.json"

    table = load_price_table(vendored_path=vendored, cache_path=missing)
    price, _ = table.resolve("m")
    assert price.input_per_1m == 9.0  # 缓存不存在 → 退回 seed


# ── maybe_refresh:不阻塞、不抛错、新鲜则跳过 ────────────────────
def test_refresh_writes_cache(tmp_path):
    cache = tmp_path / "model_prices.json"
    payload = {"_meta": {"source": "x"}, "m": {"input_per_1m": 1.0, "output_per_1m": 2.0}}
    t = P.maybe_refresh(cache_path=cache, fetcher=lambda: payload)
    t.join(timeout=5)
    assert json.loads(cache.read_text()) == payload


def test_refresh_swallows_network_error(tmp_path):
    cache = tmp_path / "model_prices.json"

    def boom():
        raise OSError("network down")

    # 网络炸了:不抛错、不写出半成品文件
    t = P.maybe_refresh(cache_path=cache, fetcher=boom)
    t.join(timeout=5)
    assert not cache.exists()


def test_refresh_skips_when_fresh(tmp_path):
    cache = tmp_path / "model_prices.json"
    cache.write_text("{}")  # 刚写 → 新鲜

    called = {"n": 0}

    def fetcher():
        called["n"] += 1
        return {}

    t = P.maybe_refresh(ttl_days=1, cache_path=cache, fetcher=fetcher)
    assert t is None            # 新鲜 → 不起线程
    assert called["n"] == 0


# ── watchdog:失效要能被看见(状态边车 + 分级)─────────────────────
import urllib.error  # noqa: E402


def _err_fetcher(exc):
    def f():
        raise exc
    return f


@pytest.mark.parametrize(
    "exc,kind",
    [
        (urllib.error.HTTPError("u", 404, "gone", {}, None), "source_moved"),
        (urllib.error.URLError("conn refused"), "unreachable"),
        (TimeoutError("slow"), "unreachable"),
        (ValueError("解析到的条目过少(3)"), "schema_changed"),   # MIN_ENTRIES / 非JSON
        (RuntimeError("???"), "unknown"),
    ],
    ids=[
        "http-error",
        "url-error",
        "timeout",
        "schema-changed",
        "unknown-error",
    ],
)
def test_refresh_records_failure_kind(tmp_path, exc, kind):
    cache = tmp_path / "model_prices.json"
    t = P.maybe_refresh(cache_path=cache, fetcher=_err_fetcher(exc))
    t.join(timeout=5)
    assert not cache.exists()                       # 失败不写半成品
    h = P.refresh_health(cache_path=cache)
    assert h["ok"] is False and h["kind"] == kind   # 状态边车记下了失败类型


def test_refresh_success_clears_and_stamps(tmp_path):
    cache = tmp_path / "model_prices.json"
    P._do_refresh(_err_fetcher(ValueError("bad")), cache)        # 先失败
    payload = {"m": {"input_per_1m": 1.0, "output_per_1m": 2.0}}
    P._do_refresh(lambda: payload, cache)                        # 再成功
    h = P.refresh_health(cache_path=cache)
    assert h["ok"] is True and h["kind"] is None and h["stale_days"] is not None


def test_refresh_health_none_when_never_attempted(tmp_path):
    assert P.refresh_health(cache_path=tmp_path / "nope.json") is None


def test_refresh_triggers_when_stale(tmp_path):
    cache = tmp_path / "model_prices.json"
    cache.write_text("{}")
    # 把 mtime 推到 2 天前 → 陈旧
    old = time.time() - 2 * 86400
    import os
    os.utime(cache, (old, old))

    payload = {"m": {"input_per_1m": 1.0, "output_per_1m": 2.0}}
    t = P.maybe_refresh(ttl_days=1, cache_path=cache, fetcher=lambda: payload)
    assert t is not None
    t.join(timeout=5)
    assert json.loads(cache.read_text()) == payload
