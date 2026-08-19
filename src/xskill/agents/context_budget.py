"""context_budget.py —— TaskAgent 弃窗单趟的上下文自管理（spec §4.5）
================================================================================

弃窗单趟拆分把"全轨迹 User 地图"喂一次,agent 用 ``look`` 工具按需读 assistant
正文。一条怪物轨迹（数万行）下,反复 ``look`` 的返回会把对话历史撑大。这里实现
上下文自管理：

1. **分母 max_context**：无统一查询 API。**配置优先**（``llm.max_context``）；
   缺省 **200K** 并打一条 warning（提醒用户按自己模型上限反注释配置）。
2. **可选 spill / 主动剪裁**（``enable_spill``, 默认关闭）：打开时，历史估到
   max_context 的 **85%** 才把旧 ``look`` 换成短标记，读文件类结果 spill 到
   实例隔离目录并留 ``spill_path``。默认关闭时不做任何 spill / 丢弃旧 tool
   结果，只靠下面的 compact 收敛。估算口径覆盖 ``content``、
   ``reasoning_content``、``tool_calls`` 的 arguments，同时用分类字符启发式
   乘以 `1.15` 安全边际偏高估。每次响应会把 ``usage.prompt_tokens`` 与估算
   raw 值做 EMA 方向校准。
3. **可选 compact**：估算超出 ``compact_token_limit`` 时，调用同一个 model
   写一份可续跑的 handoff 摘要（system、首轮 user、账本摘要、最近完整消息块
   保留）。spill 打开时 compact 阈值不会低于 spill@；关闭时按配置原值。
   失败会打原因并按 ``max_retries`` 重试；中间失败只缩小压缩请求副本；耗尽
   后抛出，不带着膨胀历史继续发主请求。compact 可走同步 ``invoke_stream``。
4. **最底层兜底（唯一）**：抓住后端抛的"上下文超长"报错 → compact 或
   （spill 开时）再剪一轮历史 → **重发一次**。就这一条,不做解析上限学分母、
   不做多触发统一。
5. **真实已用 token**：每次请求拿到 ``usage.prompt_tokens`` 写进 thread-local,
   供 TaskAgent 的 ``context_budget()`` 工具读"后端真实已用"。

线程模型：watcher 并发拆多条 traj,每条在各自线程跑一个 agent.run()。用
``threading.local`` 让每个线程的"已用 token / 上限"互不串。
"""
from __future__ import annotations

import json
import logging
import threading
import time
import traceback
import uuid
from collections import deque
from copy import copy
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger("xskill.context_budget")


class CompactFailedError(RuntimeError):
    """Compact did not succeed. Outer LLM retry must not treat this as a timeout."""


DEFAULT_MAX_CONTEXT = 200_000          # 配置缺省时的兜底上限（spec §4.5）
TRIM_TRIGGER_RATIO = 0.85              # 到上限 85% 触发主动剪裁
CHARS_PER_TOKEN = 4                    # 4 字符/token 粗估（仅估未发出去那部分）
WORD_CHARS_PER_TOKEN = 5.75            # ASCII 字母/空格/_ 的字符/token（跨家族实测 5.59~6.04，取偏保守）
DIGIT_RUN_CHARS_PER_TOKEN = 1.8        # 连续数字段的字符/token
PUNCT_RUN_CHARS_PER_TOKEN = 2.0        # 连续标点段的字符/token
OTHER_CHARS_PER_TOKEN = 2.5            # 其他 Unicode（emoji 等）按 byte-fallback 偏保守
ESTIMATE_SAFETY_MARGIN = 1.15          # 安全边际：方向宁可略高估、不可低估
MESSAGE_OVERHEAD_TOKENS = 4            # 每条消息的角色/分隔符结构开销
DEFAULT_CJK_TOKENS_PER_CHAR = 0.75     # 未知模型家族的 CJK 缺省（现代中文簇上界，偏保守）
CJK_TOKENS_PER_CHAR_BY_FAMILY = {
    "cl100k": 1.08,
    "o200k": 0.747,
    "llama3": 0.867,
    "deepseek": 0.573,
    "minimax": 0.573,
    "qwen": 0.72,
    "glm": 0.72,
    "kimi": 0.72,
}
_TRIMMABLE_TOOLS = (
    "look", "readfile", "read_file", "atom_task_read", "read_traj", "skill_read",
    "grep_files", "list_files", "edit",
)
_SPILLABLE_TOOLS = (
    "readfile", "read_file", "atom_task_read", "read_traj", "skill_read",
    "grep_files", "edit",
)
_TRIM_MARK = "[…look 旧结果已剪裁,需要可重新 look…]"
_COMPACT_MARK = "[compacted_agent_memory]"
_COMPACT_LEDGER_START = "[executed_work_ledger]"
_COMPACT_LEDGER_END = "[/executed_work_ledger]"
_COMPACT_SUMMARY_HEADER = "## Model handoff summary"
_COMPACT_LEDGER_VALUE_MAX_CHARS = 500
_COMPACT_ACTION_TOOLS = (
    "new_skill_folder",
    "write_file",
    "edit",
    "commit_generate_main",
    "commit_baby",
    "commit_baby_to_main",
    "commit_to_staging",
    "commit_update_main",
)
_COMPACT_SUCCESS_PREFIXES = {
    "new_skill_folder": ("created on baby branch:",),
    "write_file": ("wrote:",),
    "edit": ("edited:",),
    "commit_generate_main": ("committed to main:",),
    "commit_baby": ("created baby checkpoint ",),
    "commit_baby_to_main": ("graduated baby → main:",),
    "commit_to_staging": ("committed to staging:",),
    "commit_update_main": ("updated on main:",),
}

# Handoff prompt: Pi's structured checkpoint + Codex's "another LLM resumes"
# framing. The old SkillEdit-only "Keep only …" wording made GenerateAgent
# summaries collapse to empty sections, so the next turn forgot executed work.
COMPACT_PROMPT_TEMPLATE = """You are performing a CONTEXT CHECKPOINT COMPACTION.

The messages before this request are the original working memory for an XSkill agent (GenerateAgent, SkillEditAgent, or similar). Another LLM will resume the SAME task from your summary, plus the original system prompt and a short recent-message tail. If you drop executed work, that next agent will not know what it already did.

Do NOT continue the task. Do NOT call tools. Do NOT answer the user. ONLY output the handoff summary.

Be dense, not empty. Prefer a longer accurate handoff over a short one that forgets progress. A good summary is usually hundreds to a few thousand words. Empty or near-empty sections are a failure when the history shows real tool calls, file edits, or findings.

If the history already contains a compacted summary, carry every still-relevant fact forward. Newer messages update progress; they do not license deleting earlier decisions, findings, file paths, skill names, or unfinished work.

The deterministic ledger below was extracted from tool calls and results by code. Treat it as authoritative executed-work state. A directory successfully created by new_skill_folder belongs to THIS run and was unfinished immediately after creation; it is not somebody else's completed skill. A later commit entry with status=success overrides that initial unfinished state. Preserve these facts even if nearby prose conflicts or is incomplete.

Preserve, with exact names, paths, ids, commands, and error text whenever present:
1. The original user request, constraints, and target skill.
2. What was already executed: tools called, files/trajectories read, files written or edited, searches run, commits attempted, and each outcome.
3. Concrete findings: rules, pitfalls, commands, errors, function names, line-level evidence, examples, and any skill text already drafted.
4. SkillEdit candidates when present (atom_id, weightscore, intent, summary) and any still unresolved items. If this run has no candidates, do not invent a Candidate section — put the real work under Progress and Evidence.
5. All spill_path values, with tool_name and how to reload them.
6. Current file and skill state, and the next concrete actions.
7. Enough operational detail to draft or resume SKILL.md: who performed the work, when or in what sequence, exact steps and commands, environment assumptions, observed pitfalls, and recoveries.

Do not invent evidence.
Do not omit unfinished work just to look concise.
Do not paste huge raw trajectory dumps; extract the concrete facts listed above and keep every spill_path so full text can be reloaded.

Write in the same language as the conversation.

Use this format:

## Goal
## Constraints
## Progress
### Done
### In Progress
### Blocked
## Key Decisions
## Evidence And Artifacts
## Reloadable Spill Paths
## Next Steps
## Critical Context

Deterministic executed-work ledger:
{ledger}
"""

# 上下文超长报错的特征关键词（不同后端措辞不一,只做子串命中即兜底重发）。
_OVERLONG_HINTS = (
    "context length", "context_length", "maximum context",
    "too long", "exceeds", "context window", "reduce the length",
    "string too long", "tokens. however",
)

# thread-local：当前线程最近一次请求后端真实 prompt_tokens + 该线程的上限。
_STATE = threading.local()


def set_max_context(max_tokens: int) -> None:
    """记本线程的上下文 token 上限（invoke 包装层每次请求前写）。"""
    _STATE.max_context = int(max_tokens)


def get_max_context() -> int:
    """读本线程的上下文 token 上限；未设过则返回缺省 200K。"""
    return int(getattr(_STATE, "max_context", DEFAULT_MAX_CONTEXT))


def set_used_tokens(used: int) -> None:
    """记本线程最近一次请求后端真实 prompt_tokens。"""
    _STATE.used_tokens = int(used)


def get_used_tokens() -> int:
    """读本线程最近一次请求后端真实 prompt_tokens；未发过请求则 0。"""
    return int(getattr(_STATE, "used_tokens", 0))


def resolve_max_context(llm_cfg: dict) -> int:
    """配置优先解析 max_context；缺省 200K 并打 warning（spec §4.5）。"""
    raw = (llm_cfg or {}).get("max_context")
    if raw:
        try:
            val = int(raw)
            if val > 0:
                return val
        except (TypeError, ValueError):
            pass
    logger.warning(
        "llm.max_context 未配置,弃窗单趟拆分按缺省 %d token 估上下文上限；"
        "请在 config.yaml 的 llm 段反注释 max_context 改成你模型的真实上限。",
        DEFAULT_MAX_CONTEXT)
    return DEFAULT_MAX_CONTEXT


def _msg_content_str(msg: Any) -> str:
    c = getattr(msg, "content", None)
    if c is None and isinstance(msg, dict):
        c = msg.get("content")
    if isinstance(c, str):
        return c
    if c is None:
        return ""
    return str(c)


def _msg_reasoning_str(msg: Any) -> str:
    reasoning = getattr(msg, "reasoning_content", None)
    if reasoning is None and isinstance(msg, dict):
        reasoning = msg.get("reasoning_content")
    if reasoning is None:
        return ""
    if isinstance(reasoning, str):
        return reasoning
    return str(reasoning)


def _msg_tool_args_str(msg: Any) -> str:
    calls = getattr(msg, "tool_calls", None)
    if calls is None and isinstance(msg, dict):
        calls = msg.get("tool_calls")
    segments: list[str] = []
    for call in calls or []:
        if isinstance(call, dict):
            function_data = call.get("function") or {}
            arguments = function_data.get("arguments")
        else:
            function_data = getattr(call, "function", None)
            arguments = getattr(function_data, "arguments", None)
        if arguments is None:
            continue
        if isinstance(arguments, str):
            segments.append(arguments)
        else:
            segments.append(json.dumps(arguments, ensure_ascii=False))
    return "".join(segments)


def _family_cjk_rate(model: Any) -> tuple[float, str | None]:
    if not isinstance(model, str) or not model:
        return DEFAULT_CJK_TOKENS_PER_CHAR, None
    lowered = model.lower()
    if any(token in lowered for token in ("gpt-4o", "gpt-4.1", "o1", "o3", "o4", "gpt-5")):
        return CJK_TOKENS_PER_CHAR_BY_FAMILY["o200k"], "o200k"
    if "gpt-4" in lowered or "gpt-3.5" in lowered:
        return CJK_TOKENS_PER_CHAR_BY_FAMILY["cl100k"], "cl100k"
    if "llama" in lowered:
        return CJK_TOKENS_PER_CHAR_BY_FAMILY["llama3"], "llama3"
    if "deepseek" in lowered:
        return CJK_TOKENS_PER_CHAR_BY_FAMILY["deepseek"], "deepseek"
    if "minimax" in lowered:
        return CJK_TOKENS_PER_CHAR_BY_FAMILY["minimax"], "minimax"
    if "qwen" in lowered:
        return CJK_TOKENS_PER_CHAR_BY_FAMILY["qwen"], "qwen"
    if "glm" in lowered or "chatglm" in lowered or "zhipu" in lowered:
        return CJK_TOKENS_PER_CHAR_BY_FAMILY["glm"], "glm"
    if "kimi" in lowered or "moonshot" in lowered:
        return CJK_TOKENS_PER_CHAR_BY_FAMILY["kimi"], "kimi"
    return DEFAULT_CJK_TOKENS_PER_CHAR, None


def _estimate_text_tokens(text: str, cjk_rate: float) -> int:
    if not text:
        return 0
    word_chars = 0.0
    digit_run = 0
    punct_run = 0
    run_cost = 0.0
    cjk_chars = 0.0
    other_chars = 0.0

    for char in text:
        code_point = ord(char)
        if code_point < 128:
            if char.isdigit():
                if punct_run > 0:
                    run_cost += max(1.0, punct_run / PUNCT_RUN_CHARS_PER_TOKEN)
                    punct_run = 0
                digit_run += 1
                continue
            if char.isalpha() or char == " " or char == "_":
                if digit_run > 0:
                    run_cost += max(1.0, digit_run / DIGIT_RUN_CHARS_PER_TOKEN)
                    digit_run = 0
                if punct_run > 0:
                    run_cost += max(1.0, punct_run / PUNCT_RUN_CHARS_PER_TOKEN)
                    punct_run = 0
                word_chars += 1
                continue
            if digit_run > 0:
                run_cost += max(1.0, digit_run / DIGIT_RUN_CHARS_PER_TOKEN)
                digit_run = 0
            punct_run += 1
            continue
        if digit_run > 0:
            run_cost += max(1.0, digit_run / DIGIT_RUN_CHARS_PER_TOKEN)
            digit_run = 0
        if punct_run > 0:
            run_cost += max(1.0, punct_run / PUNCT_RUN_CHARS_PER_TOKEN)
            punct_run = 0
        if (0x4E00 <= code_point <= 0x9FFF
                or 0x3000 <= code_point <= 0x303F
                or 0xFF00 <= code_point <= 0xFFEF):
            cjk_chars += 1
        else:
            other_chars += 1

    if digit_run > 0:
        run_cost += max(1.0, digit_run / DIGIT_RUN_CHARS_PER_TOKEN)
    if punct_run > 0:
        run_cost += max(1.0, punct_run / PUNCT_RUN_CHARS_PER_TOKEN)

    return int(
        (
            word_chars / WORD_CHARS_PER_TOKEN
            + run_cost
            + cjk_chars * cjk_rate
            + other_chars / OTHER_CHARS_PER_TOKEN
        )
        * ESTIMATE_SAFETY_MARGIN
    )


def _msg_role(msg: Any) -> str:
    role = getattr(msg, "role", None)
    if role is None and isinstance(msg, dict):
        role = msg.get("role")
    return str(role or "")


def _msg_tool_call_id(msg: Any) -> str:
    value = getattr(msg, "tool_call_id", None)
    if value is None and isinstance(msg, dict):
        value = msg.get("tool_call_id")
    return str(value or "")


def _tool_name(msg: Any) -> str:
    name = getattr(msg, "tool_name", None) or getattr(msg, "name", None)
    if name is None and isinstance(msg, dict):
        name = msg.get("tool_name") or msg.get("name")
    return str(name or "")


def _assistant_tool_call_ids(msg: Any) -> list[str]:
    calls = getattr(msg, "tool_calls", None)
    if calls is None and isinstance(msg, dict):
        calls = msg.get("tool_calls")
    ids: list[str] = []
    for call in calls or []:
        value = None
        if isinstance(call, dict):
            value = call.get("id")
        else:
            value = getattr(call, "id", None)
        if value:
            ids.append(str(value))
    return ids


def _set_msg_role(msg: Any, role: str) -> None:
    if isinstance(msg, dict):
        msg["role"] = role
        return
    try:
        msg.role = role
    except (AttributeError, TypeError):
        pass


def _new_user_message(template: Any, content: str) -> Any:
    """Create a plain user message shaped like existing history messages."""
    if isinstance(template, dict):
        return {"role": "user", "content": content}
    try:
        return type(template)(role="user", content=content)
    except (TypeError, ValueError):
        pass
    try:
        msg = copy(template)
    except Exception:  # pylint: disable=broad-exception-caught
        from types import SimpleNamespace
        return SimpleNamespace(role="user", content=content)
    _set_msg_role(msg, "user")
    _replace_tool_content(msg, content)
    for attr in (
        "tool_name", "name", "tool_call_id", "tool_calls", "reasoning_content",
    ):
        if hasattr(msg, attr):
            try:
                setattr(msg, attr, None)
            except (AttributeError, TypeError):
                pass
    return msg


def _positive_int_or_none(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _msg_estimate_text(msg: Any) -> str:
    """Return all message text fields included in the token estimate."""
    return (
        _msg_content_str(msg)
        + _msg_reasoning_str(msg)
        + _msg_tool_args_str(msg)
    )


def _non_negative_float_or_default(value: Any, default: float) -> float:
    if value in (None, ""):
        return default
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 0 else default


def _bool_or_default(value: Any, default: bool) -> bool:
    if value in (None, ""):
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off"):
        return False
    return default


def _estimate_history_tokens(
    messages: list,
    *,
    cjk_rate: float,
    calibration: float = 1.0,
    cache: dict | None = None,
) -> int:
    """字符粗估整段历史 token。仅作"未发出去那部分"的估值。"""
    total = 0
    for msg in messages or []:
        counted_text = _msg_estimate_text(msg)
        key = (id(msg), len(counted_text))
        if cache is not None and key in cache:
            single = cache[key]
        else:
            single = (
                _estimate_text_tokens(counted_text, cjk_rate)
                + MESSAGE_OVERHEAD_TOKENS
            )
            if cache is not None:
                # message id 复用撞 key 的概率可忽略。
                cache[key] = single
        total += single
    return int(total * calibration)


def _truncate_for_compact(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    omitted = len(text) - max_chars
    return f"{text[:max_chars]}\n... [{omitted} chars truncated]"


def _tool_calls(msg: Any) -> list:
    calls = getattr(msg, "tool_calls", None)
    if calls is None and isinstance(msg, dict):
        calls = msg.get("tool_calls")
    return list(calls or [])


def _tool_call_parts(call: Any) -> tuple[str, str, Any]:
    """Return ``(id, name, arguments)`` for dict and Agno tool calls."""
    if isinstance(call, dict):
        call_id = call.get("id")
        function_data = call.get("function") or {}
        if isinstance(function_data, dict):
            name = function_data.get("name") or call.get("name")
            arguments = function_data.get("arguments")
        else:
            name = getattr(function_data, "name", None) or call.get("name")
            arguments = getattr(function_data, "arguments", None)
    else:
        call_id = getattr(call, "id", None)
        function_data = getattr(call, "function", None)
        if isinstance(function_data, dict):
            name = function_data.get("name") or getattr(call, "name", None)
            arguments = function_data.get("arguments")
        else:
            name = (
                getattr(function_data, "name", None)
                or getattr(call, "name", None)
            )
            arguments = getattr(function_data, "arguments", None)
    return str(call_id or ""), str(name or ""), arguments


def _tool_call_arguments(arguments: Any) -> tuple[dict, str]:
    if isinstance(arguments, dict):
        return arguments, ""
    if isinstance(arguments, str):
        try:
            parsed = json.loads(arguments)
        except (TypeError, ValueError):
            return {}, arguments
        if isinstance(parsed, dict):
            return parsed, ""
        return {}, arguments
    if arguments is None:
        return {}, ""
    try:
        encoded = json.dumps(arguments, ensure_ascii=False)
    except (TypeError, ValueError):
        encoded = str(arguments)
    return {}, encoded


def _ledger_value(value: Any) -> str:
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError):
            text = str(value)
    text = text.replace("\r", "\\r").replace("\n", "\\n")
    text = _truncate_for_compact(
        text,
        _COMPACT_LEDGER_VALUE_MAX_CHARS,
    ).replace("\n", " ")
    return json.dumps(text, ensure_ascii=False)


def _compact_ledger_text(msg: Any) -> str | None:
    """Read the one code-owned ledger block from a compact memory message."""
    if _msg_role(msg) != "user":
        return None
    content = _msg_content_str(msg)
    prefix = f"{_COMPACT_MARK}\n{_COMPACT_LEDGER_START}\n"
    suffix = f"\n{_COMPACT_LEDGER_END}\n{_COMPACT_SUMMARY_HEADER}\n"
    if not content.startswith(prefix):
        return None
    end = content.find(suffix, len(prefix))
    if end < 0:
        return None
    return content[len(prefix):end]


def _previous_ledger_lines(messages: list) -> list[str]:
    """Recover facts from the single canonical prior compact-memory slot."""
    history = messages or []
    first_user_index = next(
        (i for i, msg in enumerate(history) if _msg_role(msg) == "user"),
        None,
    )
    if first_user_index is None or first_user_index + 1 >= len(history):
        return []
    # Compaction always inserts its synthetic user message immediately after the
    # original user request.  Never trust marker-shaped content from the original
    # request, assistant/tool output, or a later user continuation.
    ledger = _compact_ledger_text(history[first_user_index + 1])
    if ledger is None:
        return []
    return [
        line
        for raw_line in ledger.splitlines()
        if (line := raw_line.strip()).startswith("- ")
        and "no call recorded" not in line
    ]


def _ledger_result_status(name: str, result: str) -> str:
    if not result:
        return "result-not-recorded"
    lowered = result.lstrip().lower()
    if lowered.startswith(("error", "failed")):
        return "error"
    if lowered.startswith(_COMPACT_SUCCESS_PREFIXES.get(name, ())):
        return "success"
    return "returned"


def _ledger_action_line(record: dict[str, Any]) -> str:
    name = record["name"]
    arguments = record["arguments"]
    raw_arguments = record["raw_arguments"]
    result = record.get("result", "")
    fields: list[str] = []
    if record["call_id"]:
        fields.append(f"call_id={_ledger_value(record['call_id'])}")
    if name == "new_skill_folder":
        lowered = result.lstrip().lower()
        if lowered.startswith("created on baby branch:"):
            fields.extend(("origin=this-run", "state_after_creation=unfinished"))
        elif lowered.startswith("already exists:"):
            fields.extend(("origin=pre-existing", "create_outcome=not-created"))
        elif lowered.startswith(("error", "failed")):
            fields.extend(("origin=not-created", "create_outcome=failed"))
        else:
            fields.extend(("origin=unknown", "create_outcome=result-not-recorded"))
        keys = ("skill_name", "description")
    elif name == "write_file":
        keys = ("path",)
        if "content" in arguments:
            fields.append(f"content_chars={len(str(arguments['content']))}")
    elif name == "edit":
        keys = ("path",)
        for key in ("old_string", "new_string"):
            if key in arguments:
                fields.append(f"{key}_chars={len(str(arguments[key]))}")
    else:
        keys = ("skill_name", "message")
    for key in keys:
        if key in arguments:
            fields.append(f"{key}={_ledger_value(arguments[key])}")
    if raw_arguments:
        fields.append(f"raw_arguments={_ledger_value(raw_arguments)}")
    status = _ledger_result_status(name, result)
    fields.append(f"status={status}")
    if result:
        fields.append(f"result={_ledger_value(result)}")
    return f"- {name}: " + "; ".join(fields)


def _build_execution_ledger(messages: list) -> str:
    """Extract durable Generate/SkillEdit mutation facts without an LLM."""
    records: list[dict[str, Any]] = []
    pending_by_id: dict[str, dict[str, Any]] = {}
    pending_by_name: dict[str, deque[dict[str, Any]]] = {}
    for msg in messages or []:
        for call in _tool_calls(msg):
            call_id, name, raw = _tool_call_parts(call)
            if name not in _COMPACT_ACTION_TOOLS:
                continue
            arguments, raw_arguments = _tool_call_arguments(raw)
            record = {
                "call_id": call_id,
                "name": name,
                "arguments": arguments,
                "raw_arguments": raw_arguments,
                "result": "",
                "matched": False,
            }
            records.append(record)
            pending_by_name.setdefault(name, deque()).append(record)
            if call_id:
                pending_by_id[call_id] = record
        if _msg_role(msg) != "tool":
            continue
        tool_call_id = _msg_tool_call_id(msg)
        record = pending_by_id.pop(tool_call_id, None) if tool_call_id else None
        if record is None and not tool_call_id:
            tool_name = _tool_name(msg)
            queue = pending_by_name.get(tool_name)
            while queue and queue[0]["matched"]:
                queue.popleft()
            record = queue.popleft() if queue else None
        if record is None:
            continue
        record["result"] = _msg_content_str(msg)
        record["matched"] = True
        if record["call_id"]:
            pending_by_id.pop(record["call_id"], None)

    lines = _previous_ledger_lines(messages)
    lines.extend(_ledger_action_line(record) for record in records)
    lines = list(dict.fromkeys(lines))
    categories = {
        "new_skill_folder": any(
            line.startswith("- new_skill_folder:") for line in lines
        ),
        "write_or_edit": any(
            line.startswith(("- write_file:", "- edit:")) for line in lines
        ),
        "commit": any(
            line.startswith("- commit_") for line in lines
        ),
    }
    if not categories["new_skill_folder"]:
        lines.append("- new_skill_folder: no call recorded.")
    if not categories["write_or_edit"]:
        lines.append("- write_file/edit: no call recorded.")
    if not categories["commit"]:
        lines.append("- commit: no call recorded.")
    return "\n".join(lines)


def build_compact_prompt(messages: list) -> str:
    """Build the single instruction appended after the original history."""
    ledger = _build_execution_ledger(messages)
    return COMPACT_PROMPT_TEMPLATE.replace("{ledger}", ledger)


def _safe_recent_tail(messages: list, keep_recent_messages: int) -> list:
    """Return a recent tail without breaking assistant tool_calls/tool messages."""
    tail_count = max(0, int(keep_recent_messages))
    if not messages or not tail_count:
        return []
    blocks: list[list] = []
    i = 0
    while i < len(messages):
        msg = messages[i]
        role = _msg_role(msg)
        if role == "tool":
            i += 1
            continue
        tool_call_ids = _assistant_tool_call_ids(msg)
        if role == "assistant" and tool_call_ids:
            block = [msg]
            remaining = set(tool_call_ids)
            j = i + 1
            while j < len(messages) and _msg_role(messages[j]) == "tool":
                tool_id = _msg_tool_call_id(messages[j])
                if tool_id and remaining and tool_id not in remaining:
                    break
                block.append(messages[j])
                if tool_id:
                    remaining.discard(tool_id)
                j += 1
                if not remaining:
                    break
            if not remaining and len(block) > 1:
                blocks.append(block)
            elif block:
                # incomplete block: at minimum keep the assistant message
                # so it isn't silently dropped from the compact tail
                blocks.append([block[0]])
            i = max(j, i + 1)
            continue
        blocks.append([msg])
        i += 1

    selected: list = []
    count = 0
    for block in reversed(blocks):
        selected = block + selected
        count += len(block)
        if count >= tail_count:
            break
    return selected


def _is_compact_memory(msg: Any) -> bool:
    return _compact_ledger_text(msg) is not None


def _is_trimmable_tool_msg(msg: Any) -> bool:
    role = getattr(msg, "role", None)
    if role is None and isinstance(msg, dict):
        role = msg.get("role")
    if role != "tool":
        return False
    return _tool_name(msg) in _TRIMMABLE_TOOLS


def _is_already_trimmed(content: str) -> bool:
    return content == _TRIM_MARK or content.startswith("[trimmed_tool_result]")


def _safe_tool_filename(tool_name: str) -> str:
    chars = []
    for ch in tool_name:
        chars.append(ch if ch.isalnum() or ch in ("-", "_") else "_")
    return "".join(chars) or "tool"


def _atom_task_summary(content: str) -> list[str]:
    try:
        data = json.loads(content)
    except Exception:  # pylint: disable=broad-exception-caught
        return []
    if not isinstance(data, dict):
        return []
    lines: list[str] = []
    for key in ("atom_id", "traj_id", "offset_start", "offset_end", "intent", "summary"):
        val = data.get(key)
        if val not in (None, ""):
            lines.append(f"{key}: {str(val).replace(chr(10), ' ')[:500]}")
    raw = data.get("raw_segment")
    if isinstance(raw, str):
        lines.append(f"raw_segment_chars: {len(raw)}")
    return lines


def _spill_tool_result(msg: Any, content: str, spill_root: Path) -> str:
    """把旧工具结果写到 spill 文件,返回留在上下文里的短占位。"""
    tool_name = _tool_name(msg)
    run_dir = spill_root / time.strftime("%Y%m%d-%H%M%S")
    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / f"{uuid.uuid4().hex}_{_safe_tool_filename(tool_name)}.txt"
    path.write_text(content, encoding="utf-8")
    lines = [
        "[trimmed_tool_result]",
        f"tool_name: {tool_name}",
        f"spill_path: {path}",
        f"chars: {len(content)}",
        "reload: call read_file(spill_path, offset=1, limit=200) and page as needed",
    ]
    if tool_name == "atom_task_read":
        lines.extend(_atom_task_summary(content))
    return "\n".join(lines)


def _replace_tool_content(msg: Any, content: str) -> bool:
    try:
        msg.content = content
        return True
    except (AttributeError, TypeError):
        if isinstance(msg, dict):
            msg["content"] = content
            return True
    return False


def _trim_old_look_results(messages: list, target_tokens: int,
                           *, force_all: bool = False,
                           spill_root: Path | None = None,
                           cjk_rate: float,
                           calibration: float = 1.0,
                           cache: dict | None = None) -> int:
    """从最旧的 look/readfile 工具返回开始纯截断,直到估算回落 target 以下。

    ``force_all=True`` 时无视估算,把所有可剪的 look/readfile 返回一律剪掉
    （超长兜底场景：后端已明确报超长,狠剪一轮再重发）。
    返回被剪裁的 message 数。已是剪裁标记的不重复剪。
    """
    trimmed = 0
    raw_tokens = None
    if not force_all:
        # Estimate the full history once, then maintain the raw total with
        # per-message deltas. Re-estimating all messages for every candidate
        # makes a long history with late tool results quadratic.
        raw_tokens = _estimate_history_tokens(
            messages,
            cjk_rate=cjk_rate,
            calibration=1.0,
            cache=cache,
        )
    for m in messages or []:
        if (
            raw_tokens is not None
            and int(raw_tokens * calibration) <= target_tokens
        ):
            break
        if not _is_trimmable_tool_msg(m):
            continue
        original = _msg_content_str(m)
        if _is_already_trimmed(original):
            continue
        old_cache_key = (id(m), len(_msg_estimate_text(m)))
        original_tokens = None
        if raw_tokens is not None:
            original_tokens = _estimate_history_tokens(
                [m],
                cjk_rate=cjk_rate,
                calibration=1.0,
                cache=cache,
            )
        name = _tool_name(m)
        if name in _SPILLABLE_TOOLS:
            if spill_root is None:
                raise RuntimeError(
                    "spill_root 未绑定到当前 XSkill 实例，不能安全落盘工具结果"
                )
            replacement = _spill_tool_result(m, original, spill_root)
        else:
            replacement = _TRIM_MARK
        if _replace_tool_content(m, replacement):
            if cache is not None:
                # The cache key includes text length rather than content. Drop
                # the old value even when the replacement has the same length.
                cache.pop(old_cache_key, None)
            if raw_tokens is not None and original_tokens is not None:
                replacement_tokens = _estimate_history_tokens(
                    [m],
                    cjk_rate=cjk_rate,
                    calibration=1.0,
                    cache=cache,
                )
                raw_tokens += replacement_tokens - original_tokens
            trimmed += 1
    return trimmed


def _compact_history_in_place(
    messages: list,
    *,
    compact_fn: Callable[[str], str],
    keep_recent_messages: int,
) -> bool:
    """Compact old history while preserving system, turn0 user, and recent tail."""
    if not messages:
        return False
    system_msg = next((m for m in messages if _msg_role(m) == "system"), None)
    first_user = next((m for m in messages if _msg_role(m) == "user"), None)
    tail = [
        msg
        for msg in _safe_recent_tail(messages, keep_recent_messages)
        if not _is_compact_memory(msg)
    ]
    kept_ids = {
        id(msg)
        for msg in (system_msg, first_user, *tail)
        if msg is not None
    }
    dropped = [msg for msg in messages if id(msg) not in kept_ids]
    if not dropped:
        return False
    ledger = _build_execution_ledger(messages)
    prompt = COMPACT_PROMPT_TEMPLATE.replace("{ledger}", ledger)
    summary = (compact_fn(prompt) or "").strip()
    if not summary:
        raise RuntimeError("compact produced empty summary")
    template = system_msg or first_user or messages[0]
    summary_msg = _new_user_message(
        template,
        "\n".join((
            _COMPACT_MARK,
            _COMPACT_LEDGER_START,
            ledger,
            _COMPACT_LEDGER_END,
            _COMPACT_SUMMARY_HEADER,
            summary,
        )),
    )
    new_messages: list = []
    kept_new_ids: set[int] = set()
    for msg in (system_msg, first_user):
        if msg is not None and id(msg) not in kept_new_ids:
            new_messages.append(msg)
            kept_new_ids.add(id(msg))
    new_messages.append(summary_msg)
    for msg in tail:
        if id(msg) not in kept_new_ids:
            new_messages.append(msg)
            kept_new_ids.add(id(msg))
    messages[:] = new_messages
    return True


def _response_text(resp: Any, assistant_message: Any | None = None) -> str:
    content = getattr(resp, "content", None)
    if content is None and isinstance(resp, dict):
        content = resp.get("content")
    if content is None and assistant_message is not None:
        content = getattr(assistant_message, "content", None)
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    return str(content)


def _copy_message(msg: Any) -> Any:
    if isinstance(msg, dict):
        return dict(msg)
    try:
        return copy(msg)
    except Exception:  # pylint: disable=broad-exception-caught
        return msg


def _shrink_copied_tool_results(
    messages: list,
    target_tokens: int,
    *,
    force_all: bool = False,
    spill_root: Path | None = None,
    cjk_rate: float,
    calibration: float = 1.0,
    cache: dict | None = None,
) -> int:
    """Trim tool bodies on a message copy used only for the compact LLM call.

    Live history is not mutated. If spill_root is missing, use the short
    look-trim mark even for spillable tools — this copy is discarded after
    the compact request.
    """
    trimmed = 0
    for m in messages or []:
        if (
            not force_all
            and _estimate_history_tokens(
                messages,
                cjk_rate=cjk_rate,
                calibration=calibration,
                cache=cache,
            )
            <= target_tokens
        ):
            break
        if not _is_trimmable_tool_msg(m):
            continue
        original = _msg_content_str(m)
        if _is_already_trimmed(original):
            continue
        name = _tool_name(m)
        if name in _SPILLABLE_TOOLS and spill_root is not None:
            replacement = _spill_tool_result(m, original, spill_root)
        else:
            replacement = _TRIM_MARK
        if _replace_tool_content(m, replacement):
            trimmed += 1
    return trimmed


def _wait_compact_retry(delay: float) -> None:
    """Sleep between compact retries; abort immediately on process shutdown."""
    if delay <= 0:
        return
    from xskill.utils.shutdown import SHUTTING_DOWN
    if SHUTTING_DOWN.wait(delay):
        raise CompactFailedError(
            "working-memory compact did not succeed: process is shutting down"
        )


def _stream_delta_text(chunk: Any) -> str:
    """Extract one streaming delta. Do not fall back to assistant_message."""
    content = getattr(chunk, "content", None)
    if content is None and isinstance(chunk, dict):
        content = chunk.get("content")
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    return str(content)


def _consume_compact_stream(invoke_stream, compact_messages: list, assistant_message) -> str:
    parts: list[str] = []
    for chunk in invoke_stream(
        list(compact_messages),
        assistant_message=assistant_message,
    ):
        piece = _stream_delta_text(chunk)
        if piece:
            parts.append(piece)
    text = "".join(parts).strip()
    if text:
        return text
    return _response_text(None, assistant_message)


def _model_compact_fn(
    original_invoke: Callable[..., Any],
    prefix_messages: list | Callable[[], list],
    invoke_stream: Callable[..., Any] | None = None,
) -> Callable[[str], str]:
    def _prefix() -> list:
        if callable(prefix_messages):
            return list(prefix_messages())
        return list(prefix_messages)

    def compact(prompt: str) -> str:
        from agno.models.message import Message

        assistant_message = Message(role="assistant", content=None)
        compact_messages = [
            *_prefix(),
            Message(role="user", content=prompt),
        ]
        if invoke_stream is not None:
            return _consume_compact_stream(
                invoke_stream, compact_messages, assistant_message,
            )
        resp = original_invoke(
            compact_messages,
            assistant_message=assistant_message,
        )
        return _response_text(resp, assistant_message)

    return compact


class ContextManager:
    """把 model.invoke 包成"可选 spill + compact 重试 + 超长兜底重发 + 记真实已用"。

    构造时拿 ``max_context``（已 resolve）。``wrap(original_invoke)`` 返回新的
    invoke。默认 ``enable_spill=false``：不主动剪旧 tool 结果、不落盘 spill，
    只按 ``compact_token_limit`` 压缩工作记忆。打开 spill 后才恢复 85% 剪裁 /
    spill_path 回读。
    """

    def __init__(
        self,
        max_context: int,
        spill_root: Path | str | None = None,
        compact_token_limit: int | None = None,
        compact_keep_recent_messages: int = 6,
        compact_fn: Callable[[str], str] | None = None,
        config: dict | None = None,
    ):
        cfg = config or {}
        if compact_token_limit is None:
            compact_token_limit = _positive_int_or_none(cfg.get("compact_token_limit"))
        if cfg.get("compact_keep_recent_messages") not in (None, ""):
            configured_keep = _positive_int_or_none(cfg.get("compact_keep_recent_messages"))
            if configured_keep is not None:
                compact_keep_recent_messages = configured_keep

        self.max_context = int(max_context)
        self.trigger = int(self.max_context * TRIM_TRIGGER_RATIO)
        self.spill_root = (
            Path(spill_root).expanduser().resolve()
            if spill_root is not None
            else None
        )
        # Default off: rely on compact. Set llm.enable_spill: true to restore
        # proactive trim/spill of old tool results.
        self.enable_spill = _bool_or_default(cfg.get("enable_spill"), False)
        if self.enable_spill:
            # Spill-on: compact stays a fallback after the spill@ boundary.
            self.compact_token_limit = (
                max(int(compact_token_limit), self.trigger)
                if compact_token_limit is not None
                else None
            )
        else:
            self.compact_token_limit = (
                int(compact_token_limit)
                if compact_token_limit is not None
                else None
            )
        self.compact_keep_recent_messages = int(compact_keep_recent_messages)
        self.compact_fn = compact_fn
        # Compact invoke 不走最外层 LLM retry 包装，这里单独做有界重试。
        self.compact_max_retries = (
            _positive_int_or_none(cfg.get("compact_max_retries"))
            or _positive_int_or_none(cfg.get("max_retries"))
            or 8
        )
        self.compact_retry_base_delay = _non_negative_float_or_default(
            cfg.get("compact_retry_base_delay")
            if cfg.get("compact_retry_base_delay") not in (None, "")
            else cfg.get("retry_base_delay"),
            2.0,
        )
        self.compact_retry_max_delay = _non_negative_float_or_default(
            cfg.get("compact_retry_max_delay")
            if cfg.get("compact_retry_max_delay") not in (None, "")
            else cfg.get("retry_max_delay"),
            60.0,
        )
        self.cjk_rate, self.family = _family_cjk_rate(cfg.get("model"))
        if self.family is None:
            logger.warning(
                "model=%s 未识别模型家族,CJK 比率按缺省 %.2f 估算(偏高估方向);"
                "支持家族: %s",
                cfg.get("model"),
                self.cjk_rate,
                ", ".join(sorted(CJK_TOKENS_PER_CHAR_BY_FAMILY)),
            )
        self._calibration = 1.0
        self._est_cache: dict = {}

    @staticmethod
    def _is_overlong_error(exc: Exception) -> bool:
        text = f"{exc}".lower()
        return any(h in text for h in _OVERLONG_HINTS)

    def _trace_compact(self, message: str) -> None:
        from xskill.agents import agent_trace
        try:
            agent_trace.event("CONTEXT", message, include_timestamp=False)
        except Exception:  # noqa: BLE001 — tracing must not abort compact retry
            logger.debug("compact trace write failed", exc_info=True)

    def _compact_invoke_fn(
        self,
        compact_fn: Callable[[str], str],
        *,
        attempt: int,
        prefix_box: dict,
        source_messages: list,
    ) -> Callable[[str], str]:
        """Wrap compact_fn: after a timeout, shrink only the compact request copy."""

        def invoke(prompt: str) -> str:
            if attempt > 1:
                work = [_copy_message(m) for m in source_messages]
                limit = self.compact_token_limit or self.trigger
                target = max(1, int(limit * 0.5))
                trimmed = _shrink_copied_tool_results(
                    work,
                    target,
                    force_all=attempt > 2,
                    spill_root=self.spill_root,
                    cjk_rate=self.cjk_rate,
                    calibration=self._calibration,
                    cache=self._est_cache,
                )
                prefix_box["msgs"] = work
                if trimmed:
                    self._trace_compact(
                        f"Shrunk compact request: spilled/trimmed {trimmed} "
                        f"tool result(s) before retry {attempt}/"
                        f"{self.compact_max_retries}."
                    )
            return compact_fn(prompt)

        return invoke

    def _compact_until_success(
        self,
        messages: list,
        compact_fn: Callable[[str], str],
        prefix_box: dict,
    ) -> bool:
        """Retry compact. Do not swallow the last failure.

        Compact is inside context_mgmt, so it does not inherit the outer LLM
        retry wrapper. First failure used to dump a full OpenAI traceback and
        look like a crash; intermediate attempts now log one line. Exhaustion
        raises CompactFailedError (not the raw timeout) so the outer retry
        wrapper does not multiply 8×8 timed-out compact calls.
        """
        last_exc: Exception | None = None
        for attempt in range(1, self.compact_max_retries + 1):
            try:
                compacted = _compact_history_in_place(
                    messages,
                    compact_fn=self._compact_invoke_fn(
                        compact_fn,
                        attempt=attempt,
                        prefix_box=prefix_box,
                        source_messages=messages,
                    ),
                    keep_recent_messages=self.compact_keep_recent_messages,
                )
            except CompactFailedError:
                raise
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                detail = f"{type(exc).__name__}: {exc}"
                last_attempt = attempt >= self.compact_max_retries
                tb = traceback.format_exc()
                if last_attempt:
                    logger.warning(
                        "上下文 compact 失败 (%d/%d),不再重试: %s\n%s",
                        attempt,
                        self.compact_max_retries,
                        detail,
                        tb,
                    )
                    self._trace_compact(
                        f"Compact failed ({attempt}/"
                        f"{self.compact_max_retries}): {detail}\n{tb}"
                    )
                    raise CompactFailedError(
                        "working-memory compact did not succeed after "
                        f"{self.compact_max_retries} attempts"
                    ) from exc
                logger.warning(
                    "上下文 compact 失败 (%d/%d): %s；缩小压缩请求后重试",
                    attempt,
                    self.compact_max_retries,
                    detail,
                )
                delay = min(
                    self.compact_retry_max_delay,
                    self.compact_retry_base_delay * (2 ** (attempt - 1)),
                )
                self._trace_compact(
                    f"Compact failed ({attempt}/{self.compact_max_retries}): "
                    f"{detail}. Shrinking compact request, retrying in "
                    f"{delay:.0f}s."
                )
                _wait_compact_retry(delay)
                continue
            return compacted
        if last_exc is not None:
            raise CompactFailedError(
                "working-memory compact did not succeed after "
                f"{self.compact_max_retries} attempts"
            ) from last_exc
        return False

    def wrap(self, original_invoke, invoke_stream=None):
        """返回包好上下文自管理的 invoke。

        ``invoke_stream`` 若有，compact 走同步流式（分片刷新读超时），
        主请求仍用非流式 ``original_invoke``。
        """
        def managed_invoke(messages, **kwargs):
            from xskill.agents import agent_trace
            from xskill.usage import extract_usage

            set_max_context(self.max_context)
            if len(self._est_cache) > 8192:
                self._est_cache.clear()
            prefix_box = {"msgs": messages}
            default_compact_fn = _model_compact_fn(
                original_invoke,
                lambda: prefix_box["msgs"],
                invoke_stream=invoke_stream,
            )
            # 1) 可选 spill：仅 enable_spill=true 且到 85% 时剪旧工具结果。
            est = _estimate_history_tokens(
                messages,
                cjk_rate=self.cjk_rate,
                calibration=self._calibration,
                cache=self._est_cache,
            )
            trimmed = 0
            if self.enable_spill and est >= self.trigger:
                trimmed = _trim_old_look_results(
                    messages,
                    self.trigger,
                    spill_root=self.spill_root,
                    cjk_rate=self.cjk_rate,
                    calibration=self._calibration,
                    cache=self._est_cache,
                )
                if trimmed:
                    after_spill = _estimate_history_tokens(
                        messages,
                        cjk_rate=self.cjk_rate,
                        calibration=self._calibration,
                        cache=self._est_cache,
                    )
                    logger.info(
                        "上下文到 %d/%d token,calibration=%.2f,主动剪裁 %d 条旧工具结果",
                        est,
                        self.max_context,
                        self._calibration,
                        trimmed,
                    )
                    agent_trace.event(
                        "CONTEXT",
                        f"Spilled {trimmed} old tool result(s): "
                        f"{est:,} -> {after_spill:,} tokens.",
                        include_timestamp=False,
                    )
                else:
                    agent_trace.event(
                        "CONTEXT",
                        "No more eligible tool results could be spilled.",
                        include_timestamp=False,
                    )
            elif (not self.enable_spill) and est >= self.trigger:
                agent_trace.event(
                    "CONTEXT",
                    "Spill disabled; skipping trim of old tool results.",
                    include_timestamp=False,
                )
            after_spill = _estimate_history_tokens(
                messages,
                cjk_rate=self.cjk_rate,
                calibration=self._calibration,
                cache=self._est_cache,
            )
            if (
                self.compact_token_limit is not None
                and after_spill > self.compact_token_limit
            ):
                compact_fn = self.compact_fn or default_compact_fn
                compacted = self._compact_until_success(
                    messages, compact_fn, prefix_box,
                )
                if compacted:
                    after_compact = _estimate_history_tokens(
                        messages,
                        cjk_rate=self.cjk_rate,
                        calibration=self._calibration,
                        cache=self._est_cache,
                    )
                    logger.info(
                        "上下文仍超过 compact_token_limit=%d,calibration=%.2f,已压缩历史到 %d 条消息",
                        self.compact_token_limit,
                        self._calibration,
                        len(messages or []),
                    )
                    agent_trace.event(
                        "CONTEXT",
                        f"Compacted context: {after_spill:,} -> "
                        f"{after_compact:,} tokens.",
                        include_timestamp=False,
                    )
            elif (
                self.compact_token_limit is not None
                and est >= self.trigger
            ):
                agent_trace.event(
                    "CONTEXT",
                    "Compact was not needed.",
                    include_timestamp=False,
                )
            est_raw = _estimate_history_tokens(
                messages,
                cjk_rate=self.cjk_rate,
                calibration=1.0,
                cache=self._est_cache,
            )
            try:
                resp = original_invoke(messages, **kwargs)
            except Exception as exc:  # noqa: BLE001 — 唯一底层兜底
                if not self._is_overlong_error(exc):
                    raise
                # 2) 超长兜底（唯一）：尽量 compact /（可选）狠剪 → 重发一次。
                logger.warning("后端报上下文超长,收敛历史后重发一次：%s", exc)
                before_retry = _estimate_history_tokens(
                    messages,
                    cjk_rate=self.cjk_rate,
                    calibration=self._calibration,
                    cache=self._est_cache,
                )
                if self.enable_spill:
                    _trim_old_look_results(
                        messages, self.max_context // 2,
                        force_all=True,
                        spill_root=self.spill_root,
                        cjk_rate=self.cjk_rate,
                        calibration=self._calibration,
                        cache=self._est_cache,
                    )
                    agent_trace.event(
                        "CONTEXT",
                        "Backend reported context too long; forced spill "
                        "before one retry.",
                        include_timestamp=False,
                    )
                else:
                    compact_fn = self.compact_fn or default_compact_fn
                    compacted = self._compact_until_success(
                        messages, compact_fn, prefix_box,
                    )
                    agent_trace.event(
                        "CONTEXT",
                        (
                            "Backend reported context too long; "
                            + (
                                "compacted before one retry."
                                if compacted
                                else "compact unavailable, retrying as-is."
                            )
                        ),
                        include_timestamp=False,
                    )
                after_retry = _estimate_history_tokens(
                    messages,
                    cjk_rate=self.cjk_rate,
                    calibration=self._calibration,
                    cache=self._est_cache,
                )
                logger.info(
                    "超长兜底收敛 %d -> %d tokens (enable_spill=%s)",
                    before_retry,
                    after_retry,
                    self.enable_spill,
                )
                est_raw = _estimate_history_tokens(
                    messages,
                    cjk_rate=self.cjk_rate,
                    calibration=1.0,
                    cache=self._est_cache,
                )
                resp = original_invoke(messages, **kwargs)
            usage = extract_usage(resp)
            if usage.prompt > 0 and est_raw > 0:
                ratio = usage.prompt / est_raw
                ratio = max(0.3, min(3.0, ratio))
                self._calibration = 0.5 * self._calibration + 0.5 * ratio
            # 3) 记后端真实 prompt_tokens（context_budget 工具读这个）。
            self._record_prompt_tokens(resp)
            return resp

        return managed_invoke

    @staticmethod
    def _record_prompt_tokens(resp: Any) -> None:
        from xskill.usage import extract_usage
        usage = extract_usage(resp)
        if usage.prompt:
            set_used_tokens(usage.prompt)
