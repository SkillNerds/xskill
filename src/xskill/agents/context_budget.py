"""context_budget.py —— TaskAgent 弃窗单趟的上下文自管理（spec §4.5）
================================================================================

弃窗单趟拆分把"全轨迹 User 地图"喂一次,agent 用 ``look`` 工具按需读 assistant
正文。一条怪物轨迹（数万行）下,反复 ``look`` 的返回会把对话历史撑大。这里实现
上下文自管理：

1. **分母 max_context**：无统一查询 API。**配置优先**（``llm.max_context``）；
   缺省 **200K** 并打一条 warning（提醒用户按自己模型上限反注释配置）。
2. **主动剪裁**：每次发请求前,若历史估算 token 到 max_context 的 **85%**,把旧的
   ``look`` 工具结果替换成短标记；把 SkillEdit 的读文件类工具结果 spill 到
   当前 XSkill 实例的隔离目录，message 里只保留摘要 + ``spill_path``。
   从最旧的开始剪,剪到回落 85% 以下为止。
3. **可选 compact**：如果本轮剪裁后仍超过 ``compact_token_limit``，调用同一个
   model 做一次工作记忆摘要，然后保留 system、首轮 user、摘要和最近完整消息块。
4. **最底层兜底（唯一）**：抓住后端抛的"上下文超长"报错 → 再剪一轮历史 →
   **重发一次**。就这一条,不做解析上限学分母、不做多触发统一。
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
import uuid
from copy import copy
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger("xskill.context_budget")

DEFAULT_MAX_CONTEXT = 200_000          # 配置缺省时的兜底上限（spec §4.5）
TRIM_TRIGGER_RATIO = 0.85              # 到上限 85% 触发主动剪裁
CHARS_PER_TOKEN = 4                    # 4 字符/token 粗估（仅估未发出去那部分）
_TRIMMABLE_TOOLS = (
    "look", "readfile", "read_file", "atom_task_read", "read_traj", "skill_read",
)
_SPILLABLE_TOOLS = ("readfile", "read_file", "atom_task_read", "read_traj", "skill_read")
_TRIM_MARK = "[…look 旧结果已剪裁,需要可重新 look…]"
_COMPACT_MARK = "[compacted_skill_edit_agent_memory]"

COMPACT_PROMPT_TEMPLATE = """You are compacting SkillEditAgent working memory.

Keep only information needed to continue editing the skill safely.

You must preserve:
1. The original task scenario and target skill name.
2. Candidate atom_ids, weightscore, intent, summary, and unresolved work.
3. Evidence already read from atom_task_read/read_traj/read_file/skill_read.
4. Any concrete rules, pitfalls, commands, or file edits already inferred.
5. Files already read, files already written, and pending files to edit.
6. All spill_path values needed to reload full evidence later.

Do not invent evidence.
Do not drop unresolved candidates.
Do not copy long raw trajectory text; summarize it and keep spill_path.

Output sections:
## Current Goal
## Candidate Status
## Evidence Summary
## Derived Rules
## File State
## Pending Actions
## Reloadable Spill Paths

Conversation history to compact:
{history}
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


def _new_summary_message(template: Any, content: str) -> Any:
    """Create a compact-summary message shaped like existing history messages."""
    if isinstance(template, dict):
        return {"role": "assistant", "content": content}
    try:
        msg = copy(template)
    except Exception:  # pylint: disable=broad-exception-caught
        from types import SimpleNamespace
        return SimpleNamespace(role="assistant", content=content)
    _set_msg_role(msg, "assistant")
    _replace_tool_content(msg, content)
    for attr in ("tool_name", "name", "tool_call_id", "tool_calls"):
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


def _estimate_history_tokens(messages: list) -> int:
    """字符粗估整段历史 token（4 字符/token）。仅作"未发出去那部分"的估值。"""
    chars = 0
    for m in messages or []:
        chars += len(_msg_content_str(m))
    return chars // CHARS_PER_TOKEN


def build_compact_prompt(messages: list) -> str:
    """Build the LLM prompt used to compact SkillEditAgent working memory."""
    parts: list[str] = []
    for i, msg in enumerate(messages or [], 1):
        role = _msg_role(msg) or "unknown"
        tool = _tool_name(msg)
        title = f"### message {i}: role={role}"
        if tool:
            title += f" tool={tool}"
        parts.append(title)
        parts.append(_msg_content_str(msg))
    return COMPACT_PROMPT_TEMPLATE.replace("{history}", "\n\n".join(parts))


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
                           spill_root: Path | None = None) -> int:
    """从最旧的 look/readfile 工具返回开始纯截断,直到估算回落 target 以下。

    ``force_all=True`` 时无视估算,把所有可剪的 look/readfile 返回一律剪掉
    （超长兜底场景：后端已明确报超长,狠剪一轮再重发）。
    返回被剪裁的 message 数。已是剪裁标记的不重复剪。
    """
    trimmed = 0
    for m in messages or []:
        if not force_all and _estimate_history_tokens(messages) <= target_tokens:
            break
        if not _is_trimmable_tool_msg(m):
            continue
        original = _msg_content_str(m)
        if _is_already_trimmed(original):
            continue
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
    tail = _safe_recent_tail(messages, keep_recent_messages)
    prompt = build_compact_prompt(messages)
    summary = (compact_fn(prompt) or "").strip()
    if not summary:
        return False
    template = system_msg or first_user or messages[0]
    summary_msg = _new_summary_message(
        template,
        _COMPACT_MARK + "\n" + summary,
    )
    new_messages: list = []
    for msg in (system_msg, first_user):
        if msg is not None and msg not in new_messages:
            new_messages.append(msg)
    new_messages.append(summary_msg)
    for msg in tail:
        if msg not in new_messages:
            new_messages.append(msg)
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


def _model_compact_fn(original_invoke: Callable[..., Any]) -> Callable[[str], str]:
    def compact(prompt: str) -> str:
        from agno.models.message import Message

        assistant_message = Message(role="assistant", content=None)
        resp = original_invoke(
            [Message(role="user", content=prompt)],
            assistant_message=assistant_message,
        )
        return _response_text(resp, assistant_message)

    return compact


class ContextManager:
    """把 model.invoke 包成"发请求前主动剪裁 + 超长兜底重发 + 记真实已用"。

    构造时拿 ``max_context``（已 resolve）。``wrap(original_invoke)`` 返回新的
    invoke,生产/测试都能套。剪裁直接改传进来的 ``messages`` 列表里：
    ``look`` 旧结果替换成短标记；SkillEdit 读文件类工具结果先 spill 到
    当前 XSkill 实例的 spill 目录，上下文只留摘要和可回读路径。
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
        self.compact_token_limit = (
            int(compact_token_limit) if compact_token_limit is not None else None
        )
        self.compact_keep_recent_messages = int(compact_keep_recent_messages)
        self.compact_fn = compact_fn

    @staticmethod
    def _is_overlong_error(exc: Exception) -> bool:
        text = f"{exc}".lower()
        return any(h in text for h in _OVERLONG_HINTS)

    def wrap(self, original_invoke):
        """返回包好上下文自管理的 invoke。"""
        def managed_invoke(messages, **kwargs):
            set_max_context(self.max_context)
            # 1) 主动剪裁：到 85% 就剪旧工具结果（纯截断/spill,不调模型）。
            est = _estimate_history_tokens(messages)
            trimmed = 0
            if est >= self.trigger:
                trimmed = _trim_old_look_results(
                    messages, self.trigger, spill_root=self.spill_root,
                )
                if trimmed:
                    logger.info("上下文到 %d/%d token,主动剪裁 %d 条旧工具结果",
                                est, self.max_context, trimmed)
            if (
                self.compact_token_limit is not None
                and _estimate_history_tokens(messages) > self.compact_token_limit
            ):
                compact_fn = self.compact_fn or _model_compact_fn(original_invoke)
                try:
                    compacted = _compact_history_in_place(
                        messages,
                        compact_fn=compact_fn,
                        keep_recent_messages=self.compact_keep_recent_messages,
                    )
                except Exception as exc:  # noqa: BLE001
                    compacted = False
                    logger.warning("上下文 compact 失败,继续使用剪裁后的历史：%s", exc)
                if compacted:
                    logger.info(
                        "上下文仍超过 compact_token_limit=%d,已压缩历史到 %d 条消息",
                        self.compact_token_limit, len(messages or []),
                    )
            try:
                resp = original_invoke(messages, **kwargs)
            except Exception as exc:  # noqa: BLE001 — 唯一底层兜底
                if not self._is_overlong_error(exc):
                    raise
                # 2) 超长兜底（唯一）：再狠剪一轮 → 重发一次。
                logger.warning("后端报上下文超长,剪裁历史后重发一次：%s", exc)
                _trim_old_look_results(messages, self.max_context // 2,
                                       force_all=True,
                                       spill_root=self.spill_root)
                resp = original_invoke(messages, **kwargs)
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
