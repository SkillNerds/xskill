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
from functools import partial
from typing import Any, Callable

from xskill.utils.logging import StreamLog
from xskill.utils.llm import ssl_verify

logger = logging.getLogger("xskill.agno_factory")

AGENT_LLM_STAGES = frozenset(("split", "cluster", "edit"))


def _discard_log(*args, **kwargs) -> None:
    del args, kwargs


def _inject_verify_off_if_requested(model_cls, model_kwargs: dict,
                                     log: StreamLog | None = None) -> None:
    """如果 T2S_SSL_VERIFY=false，把 verify=False 的 httpx client 塞进 model_kwargs。

    agno 不同版本接受的 kwarg 名不一致（观察到：http_client / client / async_client /
    async_http_client）。用 inspect.signature 只传实际接受的那几个，避免 TypeError。
    """
    if ssl_verify():
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
    msg_log = log or _discard_log
    if injected:
        msg_log(f"T2S_SSL_VERIFY=false → {model_cls.__name__} 注入 "
                f"{'+'.join(injected)} (verify=False)", "step")
    else:
        msg_log(f"T2S_SSL_VERIFY=false 但 {model_cls.__name__} 不接受 http_client "
                f"kwarg，改用 SSL_CERT_FILE=/path/to/ca.pem", "error")


def _wrap_with_context_mgmt(model, llm_cfg: dict, *, spill_root=None):
    """把弃窗单趟的上下文自管理（spec §4.5）套到 model.invoke 外层。

    - max_context 配置优先,缺省 200K + warning（``resolve_max_context``）。
    - 默认 enable_spill=false：不主动剪旧 tool 结果；配了 compact_token_limit
      则靠 compact 收敛。设 enable_spill=true 才恢复 85% spill/剪裁。
    - compact 走同步 invoke_stream（HTTP 流式，worker 等到摘要写完）。
    - 唯一底层兜底：抓后端"上下文超长"报错 → compact 或（spill 开时）狠剪 → 重发一次。
    - 记后端真实 prompt_tokens 供 ``context_budget()`` 工具读。

    套在 rate_limit 包装之外（最外层）：剪裁/重发后才进限流记账,语义正确。
    """
    from xskill.agents.context_budget import ContextManager, resolve_max_context
    max_ctx = resolve_max_context(llm_cfg)
    cm = ContextManager(max_ctx, spill_root=spill_root, config=llm_cfg)
    model.invoke = cm.wrap(
        model.invoke,
        invoke_stream=getattr(model, "invoke_stream", None),
    )
    return model


def _wrap_with_rate_limit(
    model, llm_cfg: dict, *, usage_ledger=None,
):
    """如果 llm_cfg['rate_limit'] 配置存在,monkey-patch model.invoke
    在调用 LLM 前先 acquire 共享桶。

    设计取舍:
    - 不子类化 agno model(agno 版本升级会接口变更,subclass 易腐)
    - monkey-patch 方法绑定 to instance,只影响这一个 model 实例
    - reasoning_content / tool_use 等 agno 内部逻辑完全保留
    """
    from xskill.usage import current_step, get_ledger
    ledger = (
        usage_ledger
        if usage_ledger is not None
        else get_ledger()
    )
    model_name = llm_cfg.get("model", "?")
    original_invoke = model.invoke
    rl_cfg = llm_cfg.get("rate_limit")

    if not rl_cfg:
        # 无限流也要记账(Issue #43):只包一层 record-only wrapper。
        def record_only_invoke(messages, **kwargs):
            resp = original_invoke(messages, **kwargs)
            ledger.record_llm(current_step(), model_name, resp)
            return resp
        model.invoke = record_only_invoke
        return model

    from xskill.utils.rate_limit import get_or_create_request_limiter
    limiter = get_or_create_request_limiter(
        "llm",
        llm_cfg.get("base_url", ""),
        rpm=rl_cfg.get("rpm"),
        tpm=rl_cfg.get("tpm"),
        request_burst=rl_cfg.get("request_burst", rl_cfg.get("burst")),
        token_burst=rl_cfg.get("token_burst", rl_cfg.get("burst")),
        max_inflight=rl_cfg.get("max_inflight"),
        weights=llm_cfg.get("_pool_weights"),
    )

    def rate_limited_invoke(messages, **kwargs):
        prompt_text = "\n".join(
            getattr(m, "content", str(m)) or "" for m in (messages or [])
        )
        resp = limiter.call(
            prompt=prompt_text,
            inner_call=partial(original_invoke, messages, **kwargs),
            timeout=60,
        )
        # 旁路记账;record_llm 内部 best-effort,绝不抛。
        ledger.record_llm(current_step(), model_name, resp)
        return resp

    model.invoke = rate_limited_invoke
    return model


def build_chat_model(
    llm_cfg: dict,
    log: StreamLog | None = None,
    *,
    usage_ledger=None,
    spill_root=None,
):
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

    # ── 显式网络超时（fail-loud，绝不挂死）────────────────────────
    # 不可达端点（防火墙 DROP / 黑洞路由）下，openai SDK 缺省行为可能长时间
    # 阻塞在 connect/DNS；这里给底层 httpx 显式 connect + 总超时，数秒内抛
    # 清晰异常。agno 的 ``timeout`` kwarg 直通 openai client，httpx.Timeout
    # 对象合法（openai SDK 原生支持）。
    # 配置（``llm`` 段，全可选）：``request_timeout``(默认 60s 单次请求总上限)
    # / ``connect_timeout``(默认 10s 建连上限) / ``client_max_retries``
    # (默认 0——瞬时错误重试统一由 ``_wrap_with_retry`` 负责，client 层再
    #  retry 会跟它相乘，故缺省关掉)。
    import httpx as _httpx
    request_timeout = float(llm_cfg.get("request_timeout", 60.0) or 60.0)
    connect_timeout = float(llm_cfg.get("connect_timeout", 10.0) or 10.0)
    timeout = _httpx.Timeout(request_timeout,
                             connect=min(connect_timeout, request_timeout))
    client_max_retries = int(llm_cfg.get("client_max_retries", 0) or 0)

    # Keep the agentic Agno path aligned with ``LLMClient`` and the public
    # config template.  Previously these documented settings were silently
    # dropped, so providers used their own temperature/output defaults.
    max_tokens = int(llm_cfg.get("max_tokens", 10000))
    if max_tokens <= 0:
        raise ValueError("llm.max_tokens must be a positive integer")
    temperature = float(llm_cfg.get("temperature", 0.0))
    extra_body = llm_cfg.get("extra_body")
    if extra_body is not None and not isinstance(extra_body, dict):
        raise ValueError("llm.extra_body must be a mapping")

    common_kwargs = dict(
        id=model_id,
        base_url=llm_cfg.get("base_url", ""),
        api_key=api_key,
        timeout=timeout,
        max_retries=client_max_retries,
        max_tokens=max_tokens,
        temperature=temperature,
        role_map={
            "system": "system",
            "user": "user",
            "assistant": "assistant",
            "tool": "tool",
            "model": "assistant",
        },
    )
    if extra_body is not None:
        # ``extra_body`` is the OpenAI SDK's explicit extension point for
        # provider-owned options (for example llama.cpp/Qwen chat-template
        # flags).  Copy the top-level mapping so model construction cannot
        # mutate the caller's config object.
        common_kwargs["extra_body"] = dict(extra_body)

    if "api.deepseek.com" in base_url:
        from agno.models.deepseek import DeepSeek
        _inject_verify_off_if_requested(DeepSeek, common_kwargs, log)
        if log:
            log("使用 agno DeepSeek model class (base_url=api.deepseek.com)", "step")
        model = DeepSeek(**common_kwargs)
        model = _wrap_with_rate_limit(
            model, llm_cfg, usage_ledger=usage_ledger
        )
        model = _wrap_with_context_mgmt(
            model, llm_cfg, spill_root=spill_root,
        )
        model = _wrap_with_retry(model, llm_cfg)
        return _wrap_with_trace(model)

    from agno.models.openai import OpenAIChat
    _inject_verify_off_if_requested(OpenAIChat, common_kwargs, log)
    model = OpenAIChat(**common_kwargs)
    model = _wrap_with_rate_limit(
        model, llm_cfg, usage_ledger=usage_ledger
    )
    model = _wrap_with_context_mgmt(
        model, llm_cfg, spill_root=spill_root,
    )
    model = _wrap_with_retry(model, llm_cfg)
    return _wrap_with_trace(model)


# 瞬时错误特征（可重试）；明确不可重试的(上下文超长/400 invalid)单独排除。
_TRANSIENT_HINTS = (
    "429", "rate limit", "ratelimit", "too many requests", "rpm exhausted",
    "timeout", "timed out", "connection", "connreset", "reset by peer",
    "temporarily", "overloaded", "unavailable", "502", "503", "504",
    "internal server error", "请求过于频繁",
)
_NON_RETRYABLE_HINTS = (
    "maximum input length", "reduce the length", "invalid_request",
    "context length", "context_length",
)


def _is_transient_error(exc: Exception) -> bool:
    # 本地限流桶耗尽只是"等一等就有令牌"，必须按瞬时错误重试——不能靠字符串
    # 匹配：消息是 "RPM bucket exhausted"，与 hint "rpm exhausted" 子串对不上，
    # 曾被误判为非瞬时 → 1/8 一击致命，高并发下 cluster 会话成片死亡。
    from xskill.agents.context_budget import CompactFailedError
    from xskill.utils.rate_limit import RateLimitExhausted
    if isinstance(exc, CompactFailedError):
        return False
    if isinstance(exc, RateLimitExhausted):
        return True
    t = f"{exc}".lower()
    if any(h in t for h in _NON_RETRYABLE_HINTS):
        return False
    status_code = getattr(exc, "status_code", None)
    if isinstance(status_code, int):
        if status_code == 429 or 500 <= status_code <= 599:
            return True
    return any(h in t for h in _TRANSIENT_HINTS)


def _wrap_with_retry(model, llm_cfg: dict):
    """对脆弱用户 API 做**强壮持续重试**：瞬时错误（429/5xx/超时/连接断）指数退避
    重试，次数/退避上限可配。

    设计取舍：
    - **同步在 worker 线程里 sleep + 重发**——不起子进程/不另开线程，**无僵尸进程**。
    - **有界**：到 ``max_retries`` 次或撞非瞬时错（400/上下文超长）即抛，绝不无限挂死
      （挂死会永占线程池 worker）。抛出后该 traj 标 error，watcher 下轮自然重排。
    - 退避 ``base * 2^(n-1)`` 封顶 ``retry_max_delay``。

    配置（``llm`` 段，全可选）：``max_retries``(默认 8) / ``retry_base_delay``(2.0s)
    / ``retry_max_delay``(60.0s)。
    """
    from xskill.utils.shutdown import SHUTTING_DOWN
    max_retries = int(llm_cfg.get("max_retries", 8) or 8)
    base = float(llm_cfg.get("retry_base_delay", 2.0) or 2.0)
    cap = float(llm_cfg.get("retry_max_delay", 60.0) or 60.0)
    original_invoke = model.invoke
    base_url = llm_cfg.get("base_url", "")

    def retrying_invoke(messages, **kwargs):
        from xskill.agents import agent_trace

        attempt = 0
        while True:
            try:
                return original_invoke(messages, **kwargs)
            except Exception as exc:  # noqa: BLE001
                attempt += 1
                error_text = str(exc).lower()
                status_code = getattr(exc, "status_code", None)
                if status_code == 429 or "429" in error_text or "rate limit" in error_text:
                    error_label = "429"
                elif isinstance(status_code, int) and 500 <= status_code <= 599:
                    error_label = str(status_code)
                elif any(h in error_text for h in _NON_RETRYABLE_HINTS):
                    error_label = "context-too-long"
                else:
                    error_label = type(exc).__name__
                error_detail = str(exc)[:160]
                if attempt >= max_retries or not _is_transient_error(exc):
                    agent_trace.event(
                        "ERROR",
                        f"LLM returned {error_label}; retries exhausted "
                        f"({attempt}/{max_retries}): {error_detail}",
                    )
                    raise
                delay = min(cap, base * (2 ** (attempt - 1)))
                agent_trace.event(
                    "WARN",
                    f"LLM returned {error_label}; retrying "
                    f"({attempt}/{max_retries})",
                )
                import logging
                logging.getLogger("xskill.agno_factory").warning(
                    "LLM 瞬时错误,第 %d/%d 次重试(%.0fs 后): %s",
                    attempt, max_retries, delay, str(exc)[:160])
                # 用 Event.wait 代替 time.sleep：进程优雅退出时立即放弃重试，
                # 否则退避睡眠会把 worker join 拖到分钟级 → supervisor SIGKILL
                from xskill.utils.rate_limit import begin_retry_wait, end_retry_wait
                begin_retry_wait("llm", base_url)
                try:
                    if SHUTTING_DOWN.wait(delay):
                        raise
                finally:
                    end_retry_wait("llm", base_url)

    model.invoke = retrying_invoke
    return model


def _wrap_with_trace(model):
    """最外层包装：每次 ``model.invoke`` 后把该轮交互写进当前线程的 agent trace sink
    （由 ``agent_trace.trace_to`` 设定）。没设 sink 时零开销。放最外层 → 看到的是
    实际发出的请求（rate_limit/裁剪之后）+ 真实响应。
    """
    from xskill.agents import agent_trace
    original_invoke = model.invoke

    def traced_invoke(messages, **kwargs):
        agent_trace.begin_round(messages)
        resp = original_invoke(messages, **kwargs)
        agent_trace.record_response(resp)
        return resp

    model.invoke = traced_invoke
    return model


def resolve_agent_llm_config(config: dict, stage: str | None = None) -> dict:
    """Resolve one Agno agent's effective LLM configuration.

    ``llm`` and ``llm_skill`` keep their existing inheritance contract.  An
    optional ``llm_agents.<stage>`` mapping is the final partial override, so
    old configurations produce byte-for-byte equivalent effective values while
    split, cluster, and edit can opt into different endpoints or models.
    """
    if stage is not None and stage not in AGENT_LLM_STAGES:
        raise ValueError(
            f"agent LLM stage must be one of {sorted(AGENT_LLM_STAGES)!r}, "
            f"got {stage!r}"
        )

    base_cfg = config.get("llm", {}) or {}
    skill_cfg = config.get("llm_skill", {}) or {}
    llm_cfg = {
        **base_cfg,
        **{key: value for key, value in skill_cfg.items() if value},
    }
    if stage is not None:
        stage_cfg = ((config.get("llm_agents", {}) or {}).get(stage, {}) or {})
        stage_override = {
            key: value
            for key, value in stage_cfg.items()
            if value not in (None, "")
        }
        stage_rate_limit = stage_override.pop("rate_limit", None)
        llm_cfg.update(stage_override)
        if stage_rate_limit:
            llm_cfg["rate_limit"] = {
                **(llm_cfg.get("rate_limit", {}) or {}),
                **stage_rate_limit,
            }

    pool_cfg = (config.get("agent_worker", {}) or {}).get("pools", {}) or {}
    llm_cfg["_pool_weights"] = {
        name: int((pool_cfg.get(name, {}) or {}).get("llm_weight", 1))
        for name in ("split", "cluster", "edit")
    }
    return llm_cfg


def make_default_factory(
    config: dict, *, stage: str | None = None, usage_ledger=None, spill_root=None,
) -> Callable[..., Any]:
    """生产环境的 agno Agent 工厂。

    返回的 callable 签名 ``(*, instructions, tools) -> agno.agent.Agent``，
    匹配 ``TaskClusterAgent`` / ``SkillEditAgent`` / ``process_atom_task``
    对 ``agno_agent_factory`` 的契约。

    LLM 配置：先按现有契约从 ``llm`` 合并 ``llm_skill``，再应用可选的
    ``llm_agents.<stage>`` 局部覆盖。未传 ``stage`` 或未配置覆盖时与旧行为一致。
    """
    from agno.agent import Agent

    llm_cfg = resolve_agent_llm_config(config, stage)

    def factory(*, instructions, tools, **kwargs):
        model = build_chat_model(
            llm_cfg,
            usage_ledger=usage_ledger,
            spill_root=spill_root,
        )
        # 弃窗单趟拆分必须开重试 + 指数退避：agno 默认 retries=0,限流时工具调用
        # 会静默返回空 submitted（被 TaskAgent 的 0 提交抛错兜住,但白白丢一趟）。
        # 调用方显式传 retries 时尊重其值,否则给安全缺省。
        kwargs.setdefault("retries", 3)
        kwargs.setdefault("exponential_backoff", True)
        kwargs.setdefault("delay_between_retries", 2)
        # agno 遥测默认开（telemetry=True）：每次 agent.run() 结束会同步 POST
        # https://os-api.agno.com/telemetry/runs。无外网/丢包环境下该请求长时间
        # 阻塞甚至挂死（实测单次 run 多挂 3~60s+，这正是探针冒烟测试/脚本
        # "吊死"的根因）。生产/探针都不该把运行数据报给厂商——一律关掉。
        kwargs.setdefault("telemetry", False)
        return Agent(
            model=model,
            instructions=instructions,
            tools=tools,
            system_message_role="system",
            markdown=True,
            **kwargs,
        )

    return factory
