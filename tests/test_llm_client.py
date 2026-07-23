"""tests/test_llm_client.py — LLMClient.from_config 字段读取行为

历史问题（修复前）：``from_config`` 只读 base_url / model / api_key 三个
字段，``max_tokens`` 即使在 config.yaml 写了也被丢掉，永远用 dataclass
默认的 1500。对 deepseek-v4-flash 这种 thinking model 偏小，导致 traj
meta 抽取频繁返回截断 / 空响应，validate_meta 失败 fallback 到 rule
extractor 把 LLM 投资浪费掉。

这一组 case 锁住：
  - 默认 max_tokens 是 10000（够 flash 的 thinking + 标准 XML 输出）
  - config 里的 max_tokens / temperature 会被读出来覆盖默认
  - 缺这些字段时回到默认（向后兼容）
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from xskill.utils.llm import LLMClient, EmbedClient, _resolve_embed_api_style


class TestLLMClientDefaults:
    def test_default_max_tokens_is_10k(self):
        c = LLMClient(base_url="http://x", model="m", api_key="k")
        assert c.max_tokens == 10000

    def test_default_temperature_is_zero(self):
        c = LLMClient(base_url="http://x", model="m", api_key="k")
        assert c.temperature == 0.0


class TestLLMClientFromConfig:
    def test_minimal_config_uses_dataclass_defaults(self):
        c = LLMClient.from_config({
            "base_url": "http://x", "model": "m", "api_key": "k",
        })
        assert c.max_tokens == 10000
        assert c.temperature == 0.0

    def test_max_tokens_override_from_config(self):
        c = LLMClient.from_config({
            "base_url": "http://x", "model": "m", "api_key": "k",
            "max_tokens": 4000,
        })
        assert c.max_tokens == 4000

    def test_temperature_override_from_config(self):
        c = LLMClient.from_config({
            "base_url": "http://x", "model": "m", "api_key": "k",
            "temperature": 0.7,
        })
        assert c.temperature == 0.7

    def test_max_tokens_string_coerced_to_int(self):
        """yaml 偶尔会把数字读成字符串，from_config 应当容错。"""
        c = LLMClient.from_config({
            "base_url": "http://x", "model": "m", "api_key": "k",
            "max_tokens": "5000",
        })
        assert c.max_tokens == 5000

    def test_missing_base_url_raises(self):
        with pytest.raises(ValueError):
            LLMClient.from_config({"model": "m", "api_key": "k"})

    def test_missing_model_raises(self):
        with pytest.raises(ValueError):
            LLMClient.from_config({"base_url": "http://x", "api_key": "k"})

    def test_chat_passes_all_rate_limit_dimensions(self):
        ledger = MagicMock()
        client = LLMClient.from_config(
            {
                "base_url": "http://x/",
                "model": "m",
                "api_key": "k",
                "rate_limit": {
                    "rpm": 60,
                    "tpm": 6000,
                    "request_burst": 4,
                    "token_burst": 400,
                    "max_inflight": 3,
                    "_pool_weights": {"split": 2},
                },
            },
            usage_ledger=ledger,
        )
        response = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))]
        )
        limiter = MagicMock()
        limiter.call.return_value = response

        with patch(
            "xskill.utils.rate_limit.get_or_create_request_limiter",
            return_value=limiter,
        ) as factory:
            assert client.chat("hello") == "ok"

        factory.assert_called_once_with(
            "llm",
            "http://x",
            rpm=60,
            tpm=6000,
            request_burst=4,
            token_burst=400,
            max_inflight=3,
            weights={"split": 2},
        )
        ledger.record_llm.assert_called_once()

    def test_chat_maps_legacy_burst_to_both_buckets(self):
        client = LLMClient(
            base_url="http://x",
            model="m",
            api_key="k",
            rate_limit_cfg={"rpm": 60, "tpm": 6000, "burst": 5},
            usage_ledger=MagicMock(),
        )
        response = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))]
        )
        limiter = MagicMock()
        limiter.call.return_value = response

        with patch(
            "xskill.utils.rate_limit.get_or_create_request_limiter",
            return_value=limiter,
        ) as factory:
            client.chat("hello")

        assert factory.call_args.kwargs["request_burst"] == 5
        assert factory.call_args.kwargs["token_burst"] == 5


class TestEmbedApiStyle:
    def test_text_model_defaults_openai(self):
        assert _resolve_embed_api_style({}, "doubao-embedding-large-text-250515") == "openai"

    def test_vision_model_defaults_multimodal(self):
        assert _resolve_embed_api_style({}, "doubao-embedding-vision-251215") == "multimodal"

    def test_explicit_api_override(self):
        cfg = {"api": "multimodal"}
        assert _resolve_embed_api_style(cfg, "doubao-embedding-large-text-250515") == "multimodal"

    def test_from_config_sets_api_style(self):
        c = EmbedClient.from_config({
            "base_url": "http://x", "model": "doubao-embedding-large-text-250515", "api_key": "k",
        })
        assert c.api_style == "openai"

    def test_embedding_uses_configured_max_inflight(self):
        client = EmbedClient.from_config(
            {
                "base_url": "http://x/",
                "model": "embed",
                "api_key": "k",
                "rate_limit": {"max_inflight": 2},
            }
        )
        limiter = MagicMock()
        limiter.call.side_effect = lambda **kwargs: kwargs["inner_call"]()

        with patch.object(
            client, "_call_api_single_unlimited", return_value=[1.0, 2.0]
        ):
            with patch(
                "xskill.utils.rate_limit.get_or_create_request_limiter",
                return_value=limiter,
            ) as factory:
                vector = client.encode("hello")

        factory.assert_called_once_with(
            "embedding", "http://x", max_inflight=2
        )
        assert vector.tolist() == [1.0, 2.0]
