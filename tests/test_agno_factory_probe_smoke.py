"""agno 工厂 × 触发探针 集成冒烟（真 agno Agent，零网络）。

daemon 注入链路是：serve 启动 ``init_skill_authoring_tool_context``(llm/embed) → commit 钩子
``_run_description_optimization`` 现建 ``make_default_factory(config)`` →
``optimize_description`` → ``probe_trigger`` 真跑代理。此前所有单测用
``_FakeAgent`` 替身——没有任何测试证明 **真 agno Agent** 能：

  1. 把 ``probe_trigger`` 造的普通函数注册成工具；
  2. 执行工具时被 ``StopAgentRun`` 优雅终止（不当 error）；
  3. ``record["triggered"]`` 闭包正确捕获。

本文件用真 agno 类补这一段：模型层在最低点（``model.invoke``）换成脚本化
ModelResponse——**绝不打真 API**。
"""

from __future__ import annotations

import json
import socket
import threading
import time

import pytest

pytest.importorskip("agno")

from agno.agent import Agent  # noqa: E402
from agno.models.message import Message  # noqa: E402
from agno.models.response import ModelResponse  # noqa: E402

from xskill.agents.agno_factory import (  # noqa: E402
    build_chat_model, make_default_factory,
)
from xskill.skill import trigger_probe as tp  # noqa: E402

_CFG = {
    "llm": {
        "base_url": "http://127.0.0.1:9/v1",   # 黑洞地址：真发请求必失败
        "model": "stub-model",
        "api_key": "sk-test",
        "max_context": 200000,
        # 万一哪条链路漏 mock 真出网，让它秒级 fail-loud，而不是吊死测试：
        "request_timeout": 3,
        "connect_timeout": 2,
        "max_retries": 1,       # 关 _wrap_with_retry 的瞬时错误重试（首错即抛）
    },
}


def _scripted_invoke(tool_name: str | None):
    """造一个 model.invoke 替身：第一轮回指定工具调用，之后回纯文本。

    agno 2.x 的 ``Model.invoke`` 契约：填充 ``assistant_message`` 并返回
    ``ModelResponse``。
    """
    state = {"n": 0}

    def _tc(name):
        return [{
            "id": "call_1", "type": "function",
            "function": {"name": name,
                         "arguments": json.dumps({"reason": "fits"})},
        }]

    def invoke(messages=None, assistant_message=None, **kwargs):  # noqa: ARG001
        state["n"] += 1
        if state["n"] == 1 and tool_name:
            if assistant_message is not None:
                assistant_message.role = "assistant"
                assistant_message.content = None
                assistant_message.tool_calls = _tc(tool_name)
            return ModelResponse(role="assistant", content=None,
                                 tool_calls=_tc(tool_name))
        if assistant_message is not None:
            assistant_message.role = "assistant"
            assistant_message.content = "no skill fits"
        return ModelResponse(role="assistant", content="no skill fits")

    return invoke


def _smoke_factory(tool_name: str | None):
    """包真工厂：Agent 照常构造（验证 make_default_factory 全链路），只在
    最低层把 model.invoke 换成脚本——上层 agno 工具执行逻辑全是真的。"""
    real = make_default_factory(_CFG)

    def factory(*, instructions, tools, **kwargs):
        agent = real(instructions=instructions, tools=tools, **kwargs)
        agent.model.invoke = _scripted_invoke(tool_name)
        return agent

    return factory


def test_factory_builds_real_agno_agent():
    factory = make_default_factory(_CFG)
    agent = factory(instructions=["x"], tools=[tp._stub_read_file],
                    tool_call_limit=4)
    assert isinstance(agent, Agent)
    # 模型配置确实从 config.llm 读取
    assert agent.model.id == "stub-model"
    # 挂死缺陷回归 1：遥测必须关——agno 默认 telemetry=True，每次 run 结束
    # 同步 POST os-api.agno.com，无外网环境下挂死/拖慢每一次探针。
    assert agent.telemetry is False
    # 挂死缺陷回归 2：模型层必须带显式网络超时（不可达端点 fail-loud 的前提）
    assert agent.model.timeout is not None


def test_context_management_reads_compact_config_and_calls_compactor(tmp_path):
    """生产包装从 llm_cfg 读取 compact 配置,并用原 model.invoke 做摘要请求。"""
    from agno.models.response import ModelResponse
    from xskill.agents.agno_factory import _wrap_with_context_mgmt

    calls: list[dict] = []

    class _FakeModel:
        def invoke(self, invoke_messages, assistant_message=None, **_kwargs):
            calls.append({
                "messages": invoke_messages,
                "assistant_message": assistant_message,
            })
            if len(calls) == 1:
                assert len(invoke_messages) == len(source_messages) + 1
                assert all(
                    actual is original
                    for actual, original in zip(invoke_messages, source_messages)
                )
                compact_request = invoke_messages[-1]
                assert compact_request.role == "user"
                assert "CONTEXT CHECKPOINT COMPACTION" in compact_request.content
                assert "Keep only information needed" not in compact_request.content
                assert "SkillEditAgent system prompt" not in compact_request.content
                if assistant_message is not None:
                    assistant_message.role = "assistant"
                    assistant_message.content = "COMPACT SUMMARY"
                return ModelResponse(role="assistant", content="COMPACT SUMMARY")
            return ModelResponse(role="assistant", content="final", input_tokens=10)

    model = _wrap_with_context_mgmt(
        _FakeModel(),
        {
            "max_context": 1000,
            "compact_token_limit": 20,
            "compact_keep_recent_messages": 2,
            "enable_spill": True,
        },
        spill_root=tmp_path / "spill",
    )
    assistant_message = Message(role="assistant", content=None)
    source_messages = [
        Message(
            role="system",
            content="SkillEditAgent system prompt\n" + ("S" * 4000),
        ),
        Message(role="user", content="turn0 scenario"),
        Message(role="assistant", content="old reasoning"),
        Message(role="tool", content="OLD_ATOM_RESULT\n" + ("x" * 8000),
                tool_name="atom_task_read", tool_call_id="call_old"),
        Message(role="assistant", content="recent reasoning"),
        Message(role="user", content="continue"),
    ]

    model.invoke(messages=source_messages, assistant_message=assistant_message)

    assert len(calls) == 2
    final_messages = calls[1]["messages"]
    assert [m.role for m in final_messages] == [
        "system", "user", "user", "assistant", "user",
    ]
    assert "COMPACT SUMMARY" in final_messages[2].content
    assert "spill_path:" in calls[0]["messages"][3].content


def _tarpit_server():
    """开一个本地"陷阱"端口：接受 TCP 连接但永不回包——模拟防火墙 DROP /
    黑洞路由式的不可达端点（连接成功但响应永远不来）。纯本机，零外网。"""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)
    conns: list = []

    def _accept_loop():
        try:
            while True:
                c, _ = srv.accept()
                conns.append(c)   # 拿住不回——读端只能等到超时
        except OSError:
            pass

    threading.Thread(target=_accept_loop, daemon=True).start()
    return srv, srv.getsockname()[1]


def test_blackhole_unresponsive_endpoint_fails_loud_within_seconds():
    """挂死缺陷回归（根治验证）：对"连得上但永不响应"的端点真发请求，
    必须在 request_timeout 量级内抛清晰异常，绝不吊死。"""
    srv, port = _tarpit_server()
    try:
        llm_cfg = {**_CFG["llm"], "base_url": f"http://127.0.0.1:{port}/v1"}
        model = build_chat_model(llm_cfg)
        t0 = time.monotonic()
        # 关键字传参——与 agno Agent 内部对 model.invoke 的调用约定一致
        # （工厂的包装链 traced_invoke(messages, **kwargs) 只收一个位置参数）
        with pytest.raises(Exception) as exc_info:
            model.invoke(
                messages=[Message(role="user", content="hi")],
                assistant_message=Message(role="assistant", content=None),
            )
        elapsed = time.monotonic() - t0
        # request_timeout=3s + 协议/收尾余量；远小于旧行为的 120s+ 吊死
        assert elapsed < 15, f"fail-loud 超时兜底失效：耗时 {elapsed:.1f}s"
        # 异常信息可读（fail-loud 而非裸挂）：至少不为空
        assert f"{exc_info.value}".strip()
    finally:
        srv.close()


def test_blackhole_refused_endpoint_fails_fast():
    """挂死缺陷回归：拒绝连接的黑洞地址（127.0.0.1:9）真发请求必须秒级抛错
    （connect_timeout + client 零重试 + wrapper max_retries=1 首错即抛）。"""
    model = build_chat_model(dict(_CFG["llm"]))
    t0 = time.monotonic()
    with pytest.raises(Exception):
        model.invoke(
            messages=[Message(role="user", content="hi")],
            assistant_message=Message(role="assistant", content=None),
        )
    assert time.monotonic() - t0 < 10


def test_probe_trigger_self_through_real_agno():
    catalog = [{"name": "decoy-skill", "description": "deploy to k8s"}]
    out = tp.probe_trigger(
        "fix my django migration", "my-skill", "Use this for django fixes",
        catalog,
        agno_agent_factory=_smoke_factory(tp._slug_to_tool("my-skill")),
        desc_cap=256,
    )
    assert out == "my-skill"


def test_probe_trigger_decoy_through_real_agno():
    catalog = [{"name": "decoy-skill", "description": "deploy to k8s"}]
    out = tp.probe_trigger(
        "deploy my app", "my-skill", "django fixes", catalog,
        agno_agent_factory=_smoke_factory(tp._slug_to_tool("decoy-skill")),
        desc_cap=256,
    )
    assert out == "decoy-skill"


def test_probe_trigger_none_through_real_agno():
    out = tp.probe_trigger(
        "explain merge sort", "my-skill", "django fixes", [],
        agno_agent_factory=_smoke_factory(None), desc_cap=256,
    )
    assert out == "NONE"
