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


def _wrap_with_context_mgmt(model, llm_cfg: dict):
    """把弃窗单趟的上下文自管理（spec §4.5）套到 model.invoke 外层。

    - max_context 配置优先,缺省 200K + warning（``resolve_max_context``）。
    - 发请求前到 85% 主动剪裁旧 look/readfile 工具返回（纯截断,不调模型）。
    - 唯一底层兜底：抓后端"上下文超长"报错 → 再剪 → 重发一次。
    - 记后端真实 prompt_tokens 供 ``context_budget()`` 工具读。

    套在 rate_limit 包装之外（最外层）：剪裁/重发后才进限流记账,语义正确。
    """
    from xskill.agents.context_budget import ContextManager, resolve_max_context
    max_ctx = resolve_max_context(llm_cfg)
    cm = ContextManager(max_ctx)
    model.invoke = cm.wrap(model.invoke)
    return model


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

    # ── 确定化：temperature 缺省 0（消除 run-to-run 抖动）──────────────
    # DeepSeek / OpenAI 兼容端点默认 temperature=1.0（DeepSeek 直连尤甚）。聚类
    # agent（TaskClusterAgent）"第一个技能 description 写宽还是写窄"在 temp>0 下
    # 纯随机，导致同样输入有时聚成 1 个技能、有时 7 个。给所有 agent（split /
    # cluster / edit）统一钉死 temperature=0 → 确定化输出，干净对照。
    # 用户仍可在 ``llm`` config 显式给 temperature 覆盖（含 0 以外的值）。
    temperature = llm_cfg.get("temperature")
    if temperature is None:
        temperature = 0.0
    temperature = float(temperature)

    common_kwargs = dict(
        id=model_id,
        base_url=llm_cfg.get("base_url", ""),
        api_key=api_key,
        timeout=timeout,
        max_retries=client_max_retries,
        temperature=temperature,
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
        return _wrap_with_trace(_wrap_with_retry(_wrap_with_context_mgmt(
            _wrap_with_rate_limit(model, llm_cfg), llm_cfg), llm_cfg))

    from agno.models.openai import OpenAIChat
    _inject_verify_off_if_requested(OpenAIChat, common_kwargs, log)
    model = OpenAIChat(**common_kwargs)
    return _wrap_with_trace(_wrap_with_retry(_wrap_with_context_mgmt(
        _wrap_with_rate_limit(model, llm_cfg), llm_cfg), llm_cfg))


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
    t = f"{exc}".lower()
    if any(h in t for h in _NON_RETRYABLE_HINTS):
        return False
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
    import time as _time
    max_retries = int(llm_cfg.get("max_retries", 8) or 8)
    base = float(llm_cfg.get("retry_base_delay", 2.0) or 2.0)
    cap = float(llm_cfg.get("retry_max_delay", 60.0) or 60.0)
    original_invoke = model.invoke

    def retrying_invoke(messages, **kwargs):
        attempt = 0
        while True:
            try:
                return original_invoke(messages, **kwargs)
            except Exception as exc:  # noqa: BLE001
                attempt += 1
                if attempt >= max_retries or not _is_transient_error(exc):
                    raise
                delay = min(cap, base * (2 ** (attempt - 1)))
                import logging
                logging.getLogger("xskill.agno_factory").warning(
                    "LLM 瞬时错误,第 %d/%d 次重试(%.0fs 后): %s",
                    attempt, max_retries, delay, str(exc)[:160])
                _time.sleep(delay)

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
        resp = original_invoke(messages, **kwargs)
        try:
            agent_trace.record(messages, resp)
        except Exception:  # pylint: disable=broad-exception-caught
            pass
        return resp

    model.invoke = traced_invoke
    return model


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
