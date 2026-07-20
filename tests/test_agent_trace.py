"""tests/test_agent_trace.py — 每次 agent 调用按 traj/atom/skill 落独立 trace 文件"""
from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from xskill.agents import agent_trace
from xskill.agents.agent_trace import trace_to, record


def _fake_resp(reasoning="想一下", content="", tools=(("look", '{"line": 42}'),)):
    """造一个 OpenAI ChatCompletion 形态的响应。"""
    msg = SimpleNamespace(
        reasoning_content=reasoning,
        content=content,
        tool_calls=[
            SimpleNamespace(function=SimpleNamespace(name=n, arguments=a))
            for n, a in tools
        ],
    )
    return SimpleNamespace(choices=[SimpleNamespace(message=msg)])


def _msgs(n=3):
    return [SimpleNamespace(content="x" * 40) for _ in range(n)]


def test_record_writes_round_with_cot_and_tools(tmp_path):
    sink = tmp_path / "task_agents" / "traj_x.log"
    with trace_to(sink):
        record(_msgs(), _fake_resp(reasoning="第一步看上下文", tools=(("look", '{"line":1}'),)))
        record(_msgs(5), _fake_resp(reasoning="提交原子",
                                    tools=(("submit_atom", '{"start_line":1}'),)))
    assert sink.is_file()
    txt = sink.read_text(encoding="utf-8")
    assert "round 1" in txt and "round 2" in txt
    assert "第一步看上下文" in txt and "提交原子" in txt
    assert "look(" in txt and "submit_atom(" in txt
    assert "tokens" in txt  # 每轮带 token 估算


def test_no_sink_is_noop(tmp_path):
    """没 with trace_to → record 不写任何文件（普通 agent 调用零开销）。"""
    record(_msgs(), _fake_resp())
    assert not any(tmp_path.rglob("*.log"))


def test_trace_to_clears_after_exit(tmp_path):
    sink = tmp_path / "a.log"
    with trace_to(sink):
        record(_msgs(), _fake_resp())
    # 退出后 sink 清空,再 record 不应再写进旧文件
    before = sink.read_text(encoding="utf-8")
    record(_msgs(), _fake_resp())
    assert sink.read_text(encoding="utf-8") == before


def test_run_overwrites_previous(tmp_path):
    """同一 traj 再跑一次 → 文件被清空覆盖（每次 run 一份干净 trace）。"""
    sink = tmp_path / "traj_y.log"
    with trace_to(sink):
        record(_msgs(), _fake_resp(reasoning="第一次run"))
    with trace_to(sink):
        record(_msgs(), _fake_resp(reasoning="第二次run"))
    txt = sink.read_text(encoding="utf-8")
    assert "第二次run" in txt and "第一次run" not in txt


def test_nested_trace_restores_outer_sink_and_round(tmp_path):
    outer = tmp_path / "outer.log"
    inner = tmp_path / "inner.log"

    with trace_to(outer):
        record(_msgs(), _fake_resp(reasoning="outer-before"))
        with trace_to(inner):
            record(_msgs(), _fake_resp(reasoning="inner"))
        record(_msgs(), _fake_resp(reasoning="outer-after"))

    outer_text = outer.read_text(encoding="utf-8")
    inner_text = inner.read_text(encoding="utf-8")
    assert "outer-before" in outer_text
    assert "outer-after" in outer_text
    assert "inner" not in outer_text
    assert "round 1" in outer_text
    assert "round 2" in outer_text
    assert "inner" in inner_text
    assert "round 1" in inner_text
    assert "round 2" not in inner_text


def test_nested_none_temporarily_disables_outer_trace(tmp_path):
    outer = tmp_path / "outer.log"

    with trace_to(outer):
        record(_msgs(), _fake_resp(reasoning="outer-before"))
        with trace_to(None):
            record(_msgs(), _fake_resp(reasoning="suppressed"))
        record(_msgs(), _fake_resp(reasoning="outer-after"))

    text = outer.read_text(encoding="utf-8")
    assert "outer-before" in text
    assert "suppressed" not in text
    assert "outer-after" in text
    assert "round 2" in text


def test_trace_disk_errors_warn_once_without_path_or_error_text(
    tmp_path, monkeypatch, caplog,
):
    sink = tmp_path / "private" / "trace.log"

    def fail_open(*args, **kwargs):
        del args, kwargs
        raise PermissionError("secret disk detail")

    with agent_trace._WARNING_LOCK:
        agent_trace._WARNED_FAILURES.clear()
    with trace_to(sink):
        monkeypatch.setattr("builtins.open", fail_open)
        with caplog.at_level(logging.WARNING, logger="xskill.agent_trace"):
            record(_msgs(), _fake_resp(reasoning="first"))
            record(_msgs(), _fake_resp(reasoning="second"))

    warnings = [
        record_item.getMessage()
        for record_item in caplog.records
        if record_item.name == "xskill.agent_trace"
    ]
    assert len(warnings) == 1
    assert "error_type=PermissionError" in warnings[0]
    assert "sink_hash=" in warnings[0]
    assert str(sink) not in warnings[0]
    assert "secret disk detail" not in warnings[0]


def test_wrap_with_trace_records_each_invoke(tmp_path):
    """工厂的 _wrap_with_trace：包装后每次 model.invoke 自动写进当前 sink。"""
    from xskill.agents.agno_factory import _wrap_with_trace

    class _FakeModel:
        def invoke(self, messages, **kw):
            return _fake_resp(reasoning="wrapped-round")

    m = _wrap_with_trace(_FakeModel())
    sink = tmp_path / "wrapped.log"
    with trace_to(sink):
        m.invoke(_msgs())
        m.invoke(_msgs())
    txt = sink.read_text(encoding="utf-8")
    assert txt.count("wrapped-round") == 2 and "round 2" in txt


def test_wrap_with_trace_does_not_swallow_unexpected_trace_bug(tmp_path):
    from xskill.agents.agno_factory import _wrap_with_trace

    class _BrokenResponse:
        @property
        def choices(self):
            raise RuntimeError("unexpected response implementation bug")

    class _FakeModel:
        def invoke(self, messages, **kwargs):
            del messages, kwargs
            return _BrokenResponse()

    model = _wrap_with_trace(_FakeModel())
    with trace_to(tmp_path / "broken.log"):
        with pytest.raises(
            RuntimeError,
            match="unexpected response implementation bug",
        ):
            model.invoke(_msgs())


def test_dict_tool_call_shape(tmp_path):
    """tool_call 是 dict 形态也能解析（防御式）。"""
    msg = SimpleNamespace(reasoning_content="r", content="",
                          tool_calls=[{"function": {"name": "grep", "arguments": "{}"}}])
    resp = SimpleNamespace(choices=[SimpleNamespace(message=msg)])
    sink = tmp_path / "d.log"
    with trace_to(sink):
        record(_msgs(), resp)
    assert "grep(" in sink.read_text(encoding="utf-8")
