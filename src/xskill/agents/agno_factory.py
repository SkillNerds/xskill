"""agno_factory.py —— 构造 agno Agent / Model 的工厂函数
=========================================================

把旧 ``agent.py`` 里的 ``_build_chat_model`` + ``_inject_verify_off_if_requested``
搬过来独立成模块，供：
- ``tasks.py`` 后台任务（提交单条 traj 跑完整 atom 流水线）
- TaskClusterAgent / SkillEditAgent 实例化时作为 ``agno_agent_factory`` 注入
- ``test_agent_model_routing.py`` 单测覆盖（DeepSeek 直连必须用 DeepSeek 子类
  避免 reasoning_content 丢失）

设计：``make_default_factory(config)`` 返回 callable，签名
``(*, instructions, tools) -> agno.agent.Agent``。生产代码调用它把 cluster/edit
agent 跑起来；测试代码注入 stub callable。
"""
from __future__ import annotations

import inspect
import logging
import os
from typing import Any, Callable

from xskill.utils.logging import StreamLog
from xskill.utils.llm import _ssl_verify

logger = logging.getLogger("xskill.agno_factory")


def _inject_verify_off_if_requested(model_cls, model_kwargs: dict,
                                     log: StreamLog | None = None) -> None:
    """如果 T2S_SSL_VERIFY=false，把 verify=False 的 httpx client 塞进 model_kwargs。

    agno 不同版本接受的 kwarg 名不一致（观察到：http_client / client / async_client /
    async_http_client）。用 inspect.signature 只传实际接受的那几个，避免 TypeError。
    """
    if _ssl_verify():
        return
    import httpx
    try:
        accepted = set(inspect.signature(model_cls.__init__).parameters.keys())
    except (TypeError, ValueError):
        accepted = set()
    sync_client = httpx.Client(verify=False)
    async_client = httpx.AsyncClient(verify=False)
    injected = []
    for name in ("http_client", "client"):
        if name in accepted:
            model_kwargs[name] = sync_client
            injected.append(name)
            break
    for name in ("async_client", "async_http_client"):
        if name in accepted:
            model_kwargs[name] = async_client
            injected.append(name)
            break
    msg_log = log or (lambda *a, **kw: None)
    if injected:
        msg_log(f"T2S_SSL_VERIFY=false → {model_cls.__name__} 注入 "
                f"{'+'.join(injected)} (verify=False)", "step")
    else:
        msg_log(f"T2S_SSL_VERIFY=false 但 {model_cls.__name__} 不接受 http_client "
                f"kwarg，改用 SSL_CERT_FILE=/path/to/ca.pem", "error")


def _wrap_with_rate_limit(model, llm_cfg: dict):
    """如果 llm_cfg['rate_limit'] 配置存在,monkey-patch model.invoke
    在调用 LLM 前先 acquire 共享桶。

    设计取舍:
    - 不子类化 agno model(agno 版本升级会接口变更,subclass 易腐)
    - monkey-patch 方法绑定 to instance,只影响这一个 model 实例
    - reasoning_content / tool_use 等 agno 内部逻辑完全保留
    """
    from xskill.usage import current_step, get_ledger
    model_name = llm_cfg.get("model", "?")
    original_invoke = model.invoke
    rl_cfg = llm_cfg.get("rate_limit")

    if not rl_cfg:
        # 无限流也要记账(Issue #43):只包一层 record-only wrapper。
        def record_only_invoke(messages, **kwargs):
            resp = original_invoke(messages, **kwargs)
            get_ledger().record_llm(current_step(), model_name, resp)
            return resp
        model.invoke = record_only_invoke
        return model

    from xskill.utils.rate_limit import (
        get_or_create_bucket, estimate_tokens, _extract_total_tokens,
    )
    bucket = get_or_create_bucket(
        llm_cfg.get("base_url", ""),
        rpm=rl_cfg.get("rpm"),
        tpm=rl_cfg.get("tpm"),
        burst=rl_cfg.get("burst"),
    )

    def rate_limited_invoke(messages, **kwargs):
        # agno 把 messages 列表传进来,估算用拼起来的总字符
        prompt_text = "\n".join(
            getattr(m, "content", str(m)) or "" for m in (messages or [])
        )
        wait = bucket.acquire_rpm(timeout=60)
        if wait > 0:
            raise RuntimeError(f"RPM exhausted, wait {wait:.1f}s")
        estimated = estimate_tokens(prompt_text)
        wait = bucket.acquire_tpm(estimated, timeout=60)
        if wait > 0:
            raise RuntimeError(f"TPM exhausted, wait {wait:.1f}s")
        resp = original_invoke(messages, **kwargs)
        actual = _extract_total_tokens(resp)
        if actual is not None:
            bucket.reconcile_tpm(estimated=estimated, actual=actual)
        # 旁路记账;record_llm 内部 best-effort,绝不抛。
        get_ledger().record_llm(current_step(), model_name, resp)
        return resp

    model.invoke = rate_limited_invoke
    return model


def build_chat_model(llm_cfg: dict, log: StreamLog | None = None):
    """根据 ``llm_cfg.base_url`` 路由到合适的 agno model 类。

    为什么不一律用 ``OpenAIChat``：DeepSeek 直连（``api.deepseek.com``）的
    thinking 类模型（``deepseek-v4-flash`` / ``deepseek-reasoner``）在
    multi-turn 对话中**要求**把上一轮 assistant 的 ``reasoning_content``
    原样回传给下一轮请求，否则 400 invalid_request_error。``OpenAIChat`` 不
    会做这步，agent 多轮 tool 调用必崩。``agno`` 提供 ``DeepSeek`` 子类
    （继承 ``OpenAILike``），它的 ``_format_message`` 会把 ``reasoning_content``
    一并塞进发回去的 message dict —— 用这个类就解决 round-trip 问题。

    其他 OpenAI 兼容 endpoint（dashscope / together / 自建 vLLM 等）即使
    挂的是 deepseek 模型，协议层一般不强制 reasoning_content 回传，仍走
    通用 ``OpenAIChat``。判别按 ``base_url`` 不按 ``model`` 名字。
    """
    base_url = (llm_cfg.get("base_url") or "").lower()
    model_id = llm_cfg.get("model", "gpt-4o")
    api_key = llm_cfg.get("api_key") or os.environ.get("LLM_API_KEY", "")

    common_kwargs = dict(
        id=model_id,
        base_url=llm_cfg.get("base_url", ""),
        api_key=api_key,
        role_map={
            "system": "system",
            "user": "user",
            "assistant": "assistant",
            "tool": "tool",
            "model": "assistant",
        },
    )

    if "api.deepseek.com" in base_url:
        from agno.models.deepseek import DeepSeek
        _inject_verify_off_if_requested(DeepSeek, common_kwargs, log)
        if log:
            log(f"使用 agno DeepSeek model class (base_url=api.deepseek.com)", "step")
        model = DeepSeek(**common_kwargs)
        return _wrap_with_rate_limit(model, llm_cfg)

    from agno.models.openai import OpenAIChat
    _inject_verify_off_if_requested(OpenAIChat, common_kwargs, log)
    model = OpenAIChat(**common_kwargs)
    return _wrap_with_rate_limit(model, llm_cfg)


def make_default_factory(config: dict) -> Callable[..., Any]:
    """生产环境的 agno Agent 工厂。

    返回的 callable 签名 ``(*, instructions, tools) -> agno.agent.Agent``，
    匹配 ``TaskClusterAgent`` / ``SkillEditAgent`` / ``process_atom_task``
    对 ``agno_agent_factory`` 的契约。

    LLM 配置：优先 ``config['llm_skill']``（质量敏感的 cluster/edit），缺
    项 fall back 到 ``config['llm']``。这与旧 ``run_agent`` 的行为一致。
    """
    from agno.agent import Agent

    base_cfg = config.get("llm", {}) or {}
    override_cfg = config.get("llm_skill", {}) or {}
    llm_cfg = {**base_cfg, **{k: v for k, v in override_cfg.items() if v}}

    def factory(*, instructions, tools, **kwargs):
        model = build_chat_model(llm_cfg)
        return Agent(
            model=model,
            instructions=instructions,
            tools=tools,
            system_message_role="system",
            markdown=True,
            **kwargs,
        )

    return factory
