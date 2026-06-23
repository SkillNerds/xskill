"""tests/test_agent_model_routing.py — agent.py 的 _build_chat_model 路由

历史 bug：``run_agent_agno`` 硬用 ``OpenAIChat``，DeepSeek 直连的 thinking
模型（v4-flash / reasoner 系）在 multi-turn 对话中**必须**把上一轮
``reasoning_content`` 原样回传，OpenAIChat 不会做这一步 → 第二轮请求
DeepSeek 返回 ``400 The reasoning_content in the thinking mode must be
passed back to the API.``，agent 整个崩。

agno 提供 ``DeepSeek`` 子类（``agno/models/deepseek/deepseek.py``），它的
``_format_message`` 会把 ``reasoning_content`` 包进 message dict round
trip 回去。所以 base_url 是 DeepSeek 直连时必须用这个类。

dashscope / together / openai 自家等其他 OpenAI 兼容 endpoint 即使挂的
是 deepseek 模型，协议层一般不强制 reasoning_content 回传，仍走通用
OpenAIChat 即可。
"""
from __future__ import annotations

import pytest

from xskill.agents.agno_factory import build_chat_model as _build_chat_model
from xskill.utils.logging import StreamLog


@pytest.fixture
def log():
    return StreamLog(verbose=False)


class TestBuildChatModelRouting:
    def test_deepseek_direct_uses_DeepSeek_class(self, log):
        m = _build_chat_model({
            "base_url": "https://api.deepseek.com",
            "model": "deepseek-v4-flash",
            "api_key": "sk-test",
        }, log)
        assert type(m).__name__ == "DeepSeek"
        assert m.id == "deepseek-v4-flash"

    def test_deepseek_subpath_still_routes_to_DeepSeek(self, log):
        # CLAUDE.md 文档里 deepseek 的 anthropic 兼容路径是
        # api.deepseek.com/anthropic；其他子路径同 host 也应走 DeepSeek 类
        m = _build_chat_model({
            "base_url": "https://api.deepseek.com/anthropic",
            "model": "deepseek-v4-flash",
            "api_key": "sk-test",
        }, log)
        assert type(m).__name__ == "DeepSeek"

    def test_dashscope_uses_OpenAIChat(self, log):
        # dashscope 上挂 deepseek 模型也走 OpenAI 兼容协议 → OpenAIChat
        m = _build_chat_model({
            "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "model": "deepseek-v3.2",
            "api_key": "sk-test",
        }, log)
        assert type(m).__name__ == "OpenAIChat"

    def test_openai_native_uses_OpenAIChat(self, log):
        m = _build_chat_model({
            "base_url": "https://api.openai.com/v1",
            "model": "gpt-4o",
            "api_key": "sk-test",
        }, log)
        assert type(m).__name__ == "OpenAIChat"

    def test_arbitrary_openai_compat_uses_OpenAIChat(self, log):
        m = _build_chat_model({
            "base_url": "https://api.together.xyz/v1",
            "model": "meta-llama/Llama-3-70b",
            "api_key": "sk-test",
        }, log)
        assert type(m).__name__ == "OpenAIChat"

    def test_local_vllm_uses_OpenAIChat(self, log):
        m = _build_chat_model({
            "base_url": "http://localhost:8000/v1",
            "model": "my-local-model",
            "api_key": "sk-no",
        }, log)
        assert type(m).__name__ == "OpenAIChat"

    def test_id_and_base_url_propagate(self, log):
        """构造完的 model 实例必须保留 config 里的 model id + base_url。"""
        m = _build_chat_model({
            "base_url": "https://api.deepseek.com",
            "model": "deepseek-reasoner",
            "api_key": "sk-test",
        }, log)
        assert m.id == "deepseek-reasoner"
        assert "deepseek.com" in m.base_url


class TestTemperatureDeterminism:
    """temperature 缺省钉死 0（消除聚类 run-to-run 抖动）；config 显式值可覆盖。"""

    def test_deepseek_defaults_temperature_zero(self, log):
        m = _build_chat_model({
            "base_url": "https://api.deepseek.com",
            "model": "deepseek-v4-flash",
            "api_key": "sk-test",
        }, log)
        assert m.temperature == 0.0

    def test_openai_defaults_temperature_zero(self, log):
        m = _build_chat_model({
            "base_url": "https://api.openai.com/v1",
            "model": "gpt-4o",
            "api_key": "sk-test",
        }, log)
        assert m.temperature == 0.0

    def test_config_temperature_overrides_default(self, log):
        m = _build_chat_model({
            "base_url": "https://api.deepseek.com",
            "model": "deepseek-v4-flash",
            "api_key": "sk-test",
            "temperature": 0.7,
        }, log)
        assert m.temperature == 0.7

    def test_config_temperature_zero_explicit(self, log):
        # 显式 0 与缺省 0 等价（不被 "is None" 误当未设）
        m = _build_chat_model({
            "base_url": "https://api.openai.com/v1",
            "model": "gpt-4o",
            "api_key": "sk-test",
            "temperature": 0,
        }, log)
        assert m.temperature == 0.0
