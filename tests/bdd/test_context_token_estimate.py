"""Executable BDD for token estimation coverage in ContextManager."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

import pytest
from pytest_bdd import given, scenario, then, when

from xskill.agents.context_budget import (
    CJK_TOKENS_PER_CHAR_BY_FAMILY,
    _TRIM_MARK,
    _COMPACT_MARK,
    _estimate_history_tokens,
    _estimate_text_tokens,
    _family_cjk_rate,
    ContextManager,
    _msg_content_str,
)


pytestmark = [pytest.mark.bdd]


@scenario(
    "features/context_budget/token_estimate.feature",
    "reasoning_content 计入估算",
)
def test_reasoning_content_is_counted() -> None:
    """ContextManager only uses historical estimation with richer coverage fields."""


@scenario(
    "features/context_budget/token_estimate.feature",
    "tool_calls arguments 计入估算（dict 与 object 两种结构）",
)
def test_tool_arguments_are_counted() -> None:
    """Argument payload from both dict/object tool calls counts in estimation."""


@scenario(
    "features/context_budget/token_estimate.feature",
    "每条消息计入结构开销",
)
def test_message_overhead_is_counted() -> None:
    """Every message contributes structure overhead."""


@scenario(
    "features/context_budget/token_estimate.feature",
    "已知模型家族的估算落在参考计数安全带内",
)
def test_family_band_reference_calibration() -> None:
    """Heuristic estimates remain within safety margins."""


@scenario(
    "features/context_budget/token_estimate.feature",
    "大 reasoning_content 使估算越过 85% 触发剪裁",
)
def test_reasoning_spills_old_look_result() -> None:
    """Large reasoning should trigger proactive trimming."""


@scenario(
    "features/context_budget/token_estimate.feature",
    "估算超过 compact_token_limit 触发历史压缩",
)
def test_compact_is_triggered_when_needed() -> None:
    """Compact path is entered when history still exceeds compact limit."""


@scenario(
    "features/context_budget/token_estimate.feature",
    "后端真实 usage 校准后续触发判定",
)
def test_usage_calibration_changes_future_thresholds() -> None:
    """EMA calibration uses response usage and influences next-round trimming."""


@scenario(
    "features/context_budget/token_estimate.feature",
    "未知模型家族使用保守缺省比率并打 warning",
)
def test_unknown_family_warns_and_uses_default_rate() -> None:
    """Unknown family route stays conservative and is observable."""


@dataclass
class TokenEstimateContext:
    messages: list[Any] = field(default_factory=list)
    dict_message: dict[str, Any] | None = None
    object_message: Any | None = None
    dict_estimate: int = 0
    object_estimate: int = 0
    dict_base_estimate: int = 0
    object_base_estimate: int = 0
    history_estimate: int = 0
    baseline_estimate: int = 0
    reference: dict[str, Any] | None = None
    estimated_by_text: dict[str, dict[str, int]] = field(default_factory=dict)
    estimated_family_sum: dict[str, int] = field(default_factory=dict)
    manager: ContextManager | None = None
    invoke_fn: Callable[[list[Any], Any], dict[str, Any]] | None = None
    compact_fn: Callable[[str], str] | None = None
    invoke_call_count: int = 0
    compact_call_count: int = 0
    invoke_snapshots: list[list[str]] = field(default_factory=list)
    warning_seen: bool = False
    family_name: str | None = None
    family_rate: float | None = None


@pytest.fixture
def estimate_context() -> TokenEstimateContext:
    return TokenEstimateContext()


def _deepseek_rate() -> float:
    return 0.573


def _history_reference_texts() -> dict[str, str]:
    return {
        "reasoning_text": "推" * 1000,
        "look_result": "旧结果" * 200,
        "tool_path": json.dumps({"path": "x" * 1200}, ensure_ascii=False),
    }


def _tool_call_arguments_text() -> str:
    return json.dumps({"path": "x" * 1200, "note": ""}, ensure_ascii=False)


@given("一条 content 很短但 reasoning_content 很长的 assistant 消息")
def given_reasoning_message(estimate_context: TokenEstimateContext) -> None:
    estimate_context.messages = [
        {
            "role": "assistant",
            "content": "好",
            "reasoning_content": _history_reference_texts()["reasoning_text"],
        }
    ]
    estimate_context.baseline_estimate = _estimate_history_tokens(
        [{"role": "assistant", "content": "好"}],
        cjk_rate=_deepseek_rate(),
        calibration=1.0,
    )


@given("一条带大参数 tool_calls 的 assistant 消息（dict 结构）")
def given_dict_tool_message(estimate_context: TokenEstimateContext) -> None:
    arguments_text = _tool_call_arguments_text()
    dict_message: dict[str, Any] = {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "function": {
                    "arguments": arguments_text,
                }
            }
        ],
    }
    estimate_context.dict_message = dict_message
    estimate_context.messages.append(dict_message)
    estimate_context.dict_base_estimate = _estimate_history_tokens(
        [{
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "function": {
                        "arguments": "",
                    }
                }
            ],
        }],
        cjk_rate=_deepseek_rate(),
        calibration=1.0,
    )


@given("一条带大参数 tool_calls 的 assistant 消息（object 结构）")
def given_object_tool_message(estimate_context: TokenEstimateContext) -> None:
    arguments_text = _tool_call_arguments_text()
    object_message = SimpleNamespace(
        role="assistant",
        content="",
        tool_calls=[
            SimpleNamespace(
                function=SimpleNamespace(arguments=arguments_text),
            )
        ],
    )
    estimate_context.object_message = object_message
    estimate_context.messages.append(object_message)
    estimate_context.object_base_estimate = _estimate_history_tokens(
        [
            SimpleNamespace(
                role="assistant",
                content="",
                tool_calls=[SimpleNamespace(function=SimpleNamespace(arguments=""))],
            )
        ],
        cjk_rate=_deepseek_rate(),
        calibration=1.0,
    )


@given("100 条 content 为空的消息")
def given_empty_messages(estimate_context: TokenEstimateContext) -> None:
    estimate_context.messages = [
        {"role": "assistant", "content": ""} for _ in range(100)
    ]


@given("真实分词器参考计数 fixture")
def given_token_reference(estimate_context: TokenEstimateContext) -> None:
    fixture_path = Path(__file__).parent.parent / "fixtures" / "context_token_reference.json"
    estimate_context.reference = json.loads(fixture_path.read_text(encoding="utf-8"))


@given("一个 max_context=1000 的 ContextManager")
def given_context_manager_default(estimate_context: TokenEstimateContext) -> None:
    estimate_context.manager = ContextManager(
        1000,
        config={"model": "deepseek-chat", "enable_spill": True},
    )
    def _invoke_stub(messages: list[Any], **_kwargs: Any) -> dict[str, Any]:
        estimate_context.invoke_call_count += 1
        estimate_context.invoke_snapshots.append(
            [_msg_content_str(message) for message in messages]
        )
        estimate_context.history_estimate = _estimate_history_tokens(
            messages,
            cjk_rate=_deepseek_rate(),
            calibration=1.0,
        )
        return {"usage": {"prompt_tokens": 0, "completion_tokens": 1}}

    estimate_context.invoke_fn = _invoke_stub
    estimate_context.messages = [
        {"role": "user", "content": _history_reference_texts()["reasoning_text"][:1]},
        {
            "role": "assistant",
            "content": "好",
            "reasoning_content": _history_reference_texts()["reasoning_text"],
        },
        {"role": "tool", "tool_name": "look", "content": _history_reference_texts()["look_result"]},
        {"role": "assistant", "content": "继续"},
    ]


@given("历史中 reasoning_content 占估算的大头且有一条可剪裁的 look 工具结果")
def given_spill_history(estimate_context: TokenEstimateContext) -> None:
    estimate_context.messages = [
        {"role": "user", "content": "任务"},
        {
            "role": "assistant",
            "content": "好",
            "reasoning_content": "推" * 4000,
        },
        {
            "role": "tool",
            "tool_name": "look",
            "content": "旧结果" * 200,
        },
        {"role": "assistant", "content": "继续"},
    ]


@given("一个 max_context=1000 且 compact_token_limit=900 的 ContextManager")
def given_context_manager_with_compact(estimate_context: TokenEstimateContext) -> None:
    estimate_context.compact_call_count = 0

    def _compact_stub(prompt: str) -> str:
        del prompt
        estimate_context.compact_call_count += 1
        return "工作记忆摘要"

    estimate_context.compact_fn = _compact_stub
    estimate_context.manager = ContextManager(
        1000,
        compact_token_limit=900,
        compact_fn=_compact_stub,
        config={"model": "deepseek-chat"},
    )


@given("一段估算超过 900 的历史")
def given_compact_history(estimate_context: TokenEstimateContext) -> None:
    estimate_context.messages = [
        {"role": "user", "content": "任务"},
        {
            "role": "assistant",
            "content": "好",
            "reasoning_content": "推" * 3500,
        },
        {
            "role": "tool",
            "tool_name": "look",
            "content": "旧结果" * 200,
        },
        {"role": "assistant", "content": "继续"},
    ]
    def _invoke_stub(messages: list[Any], **_kwargs: Any) -> dict[str, Any]:
        estimate_context.invoke_call_count += 1
        return {"usage": {"prompt_tokens": 0, "completion_tokens": 1}}

    estimate_context.invoke_fn = _invoke_stub


@given("桩 invoke 按收到消息估算值的一半返回 usage.prompt_tokens")
def given_calibration_half_stub(estimate_context: TokenEstimateContext) -> None:
    assert estimate_context.manager is not None
    assert estimate_context.manager.cjk_rate == _deepseek_rate()

    def _invoke_stub(messages: list[Any], **_kwargs: Any) -> dict[str, Any]:
        estimate_context.invoke_call_count += 1
        current_estimate = _estimate_history_tokens(
            messages,
            cjk_rate=estimate_context.manager.cjk_rate,
            calibration=1.0,
        )
        estimate_context.invoke_snapshots.append(
            [_msg_content_str(message) for message in messages]
        )
        return {
            "usage": {
                "prompt_tokens": int(current_estimate * 0.5),
                "completion_tokens": 1,
            }
        }

    estimate_context.invoke_fn = _invoke_stub


@when("估算这段历史的 token")
def when_estimate_history(estimate_context: TokenEstimateContext) -> None:
    rate = _deepseek_rate()
    estimate_context.history_estimate = _estimate_history_tokens(
        estimate_context.messages,
        cjk_rate=rate,
        calibration=1.0,
    )
    if estimate_context.dict_message is not None:
        estimate_context.dict_estimate = _estimate_history_tokens(
            [estimate_context.dict_message],
            cjk_rate=rate,
            calibration=1.0,
        )
    if estimate_context.object_message is not None:
        estimate_context.object_estimate = _estimate_history_tokens(
            [estimate_context.object_message],
            cjk_rate=rate,
            calibration=1.0,
        )


@when("对每条参考文本按对应模型家族估算")
def when_estimate_family_band(estimate_context: TokenEstimateContext) -> None:
    assert estimate_context.reference is not None
    texts = estimate_context.reference["texts"]
    references = estimate_context.reference["reference"]
    family_names = list(references["zh_reasoning"].keys())
    for family in family_names:
        estimate_context.estimated_family_sum[family] = 0
        estimate_context.estimated_by_text[family] = {}
    for text_name, text in texts.items():
        for family, family_rate in CJK_TOKENS_PER_CHAR_BY_FAMILY.items():
            if family not in family_names:
                continue
            estimate_value = _estimate_text_tokens(text, family_rate)
            estimate_context.estimated_by_text.setdefault(text_name, {})[family] = estimate_value
            estimate_context.estimated_family_sum[family] += estimate_value


@when("通过包装后的 invoke 发起请求")
def when_wrapped_invoke(estimate_context: TokenEstimateContext) -> None:
    assert estimate_context.manager is not None
    assert estimate_context.invoke_fn is not None
    if estimate_context.compact_fn is not None:
        estimate_context.manager.compact_fn = estimate_context.compact_fn
    wrapped_invoke = estimate_context.manager.wrap(estimate_context.invoke_fn)
    wrapped_invoke(estimate_context.messages)


@when("连续两次携带大估算历史发起请求")
def when_two_requests_with_calibration(estimate_context: TokenEstimateContext) -> None:
    assert estimate_context.manager is not None
    assert estimate_context.invoke_fn is not None
    estimate_context.messages = [
        {"role": "user", "content": "任务"},
        {
            "role": "assistant",
            "content": "好",
        },
        {
            "role": "tool",
            "tool_name": "look",
            "content": "日" * 1500,
        },
    ]
    wrapped_invoke = estimate_context.manager.wrap(estimate_context.invoke_fn)
    wrapped_invoke(estimate_context.messages)
    first_tool_content = _msg_content_str(estimate_context.messages[2])
    assert first_tool_content in {_TRIM_MARK, "日" * 1500}

    estimate_context.messages.append({"role": "assistant", "content": "继续"})
    estimate_context.messages.append({"role": "tool", "tool_name": "look", "content": "志" * 1500})
    wrapped_invoke(estimate_context.messages)


@when("用未知模型名构造 ContextManager")
def when_unknown_family(caplog: Any, estimate_context: TokenEstimateContext) -> None:
    with caplog.at_level(logging.WARNING, logger="xskill.context_budget"):
        manager = ContextManager(
            1000,
            config={"model": "some-random-model"},
        )
        estimate_context.warning_seen = any(
            "未识别模型家族" in record.getMessage() for record in caplog.records
        )
        estimate_context.family_rate, estimate_context.family_name = _family_cjk_rate(
            "some-random-model"
        )
        estimate_context.manager = manager


@then("估算值明显大于只按 content 估算的值")
def then_reasoning_increases_estimate(estimate_context: TokenEstimateContext) -> None:
    assert estimate_context.history_estimate - estimate_context.baseline_estimate >= 400


@then("两种结构的估算都包含 arguments 折算的 token")
def then_tool_calls_are_counted(estimate_context: TokenEstimateContext) -> None:
    assert estimate_context.dict_estimate >= estimate_context.dict_base_estimate + 250
    assert estimate_context.object_estimate >= estimate_context.object_base_estimate + 250


@then("估算值至少为 400")
def then_messages_overhead_counted(estimate_context: TokenEstimateContext) -> None:
    assert estimate_context.history_estimate >= 400


@then("每个文本每个家族的估算不小于参考值的 85% 且不大于 145%")
def then_family_band_ranges(estimate_context: TokenEstimateContext) -> None:
    assert estimate_context.reference is not None
    refs = estimate_context.reference["reference"]
    for text_name, family_estimates in estimate_context.estimated_by_text.items():
        for family_name, estimate_value in family_estimates.items():
            reference_value = refs[text_name][family_name]
            lower = int(reference_value * 0.85)
            upper = int(reference_value * 1.45)
            assert estimate_value >= lower
            assert estimate_value <= upper


@then("每个家族全部文本的估算总和不小于参考总和")
def then_family_band_sum(estimate_context: TokenEstimateContext) -> None:
    assert estimate_context.reference is not None
    refs = estimate_context.reference["reference"]
    family_names = list(next(iter(refs.values())).keys())
    for family_name in family_names:
        total_reference = 0
        for text_name in refs:
            total_reference += refs[text_name][family_name]
        assert estimate_context.estimated_family_sum[family_name] >= total_reference


@then("旧的 look 结果被剪裁标记替换")
def then_trimmed_tool_result(estimate_context: TokenEstimateContext) -> None:
    assert _TRIM_MARK in estimate_context.messages[2]["content"]


@then("桩 invoke 只被调用一次")
def then_stub_called_once(estimate_context: TokenEstimateContext) -> None:
    assert estimate_context.invoke_call_count == 1


@then("压缩函数被调用且历史中出现 compact 标记消息")
def then_compact_called_with_mark(estimate_context: TokenEstimateContext) -> None:
    assert estimate_context.compact_call_count >= 1
    assert any(
        isinstance(message, str)
        and message.startswith(_COMPACT_MARK)
        for message in [
            _msg_content_str(item) for item in estimate_context.messages
        ]
    )


@then("第一次请求触发剪裁")
def then_first_request_trims(estimate_context: TokenEstimateContext) -> None:
    tool_contents = [
        _msg_content_str(message)
        for message in estimate_context.messages
        if _msg_content_str(message) == _TRIM_MARK
    ]
    assert len(tool_contents) >= 1


@then("第二次请求因校准后估算低于阈值而未触发剪裁")
def then_second_request_not_trimmed(estimate_context: TokenEstimateContext) -> None:
    tool_contents = [
        _msg_content_str(message)
        for message in estimate_context.messages
        if isinstance(message, dict) and message.get("tool_name") == "look"
    ]
    assert len(tool_contents) == 2
    assert "志" * 1500 in tool_contents
    assert _TRIM_MARK in tool_contents


@then("日志出现未知家族 warning")
def then_unknown_family_warning(estimate_context: TokenEstimateContext) -> None:
    assert estimate_context.warning_seen is True


@then("家族路由返回缺省比率 0.75")
def then_unknown_family_default(estimate_context: TokenEstimateContext) -> None:
    assert estimate_context.family_rate == 0.75
    assert estimate_context.family_name is None
