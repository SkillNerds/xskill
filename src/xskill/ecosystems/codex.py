"""
ecosystems/codex.py -- Codex CLI 生态适配
=========================================

把蒸馏出的 Skill 装进 Codex 的 user-scope skill 目录
（``~/.agents/skills/<name>/``——与 OpenCode / OpenClaw 共享），并把 Codex CLI
原生活跃与已归档 rollout JSONL（``~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl``
与 ``~/.codex/archived_sessions/rollout-*.jsonl``）桥接回 xskill 的标准 ``traj_*.md`` 格式。

本模块含 Codex 平台的「读」（``_adapt_codex_rollout_jsonl`` +
``ingest_codex_sessions``）与「写」（``install_to_codex`` /
``install_all_to_codex``）。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from pathlib import Path
from typing import Iterable, Optional

from xskill.ecosystems._shared import (
    EcosystemSpec,
    JsonlIngester,
    _agents_skills_path,
    _install_all_with,
    _install_skill_into,
    _sanitize_for_filename,
)

logger = logging.getLogger("xskill.ecosystems")

_CODEX_TOOL_INPUT_LIMIT = 2000


# ─────────────────────────────────────────────────────────────────
# Path helpers
# ─────────────────────────────────────────────────────────────────


def _codex_sessions_path(home: Path) -> Path:
    """Codex CLI rollout JSONL 根目录：``<home>/.codex/sessions``。

    实际文件落在 ``<this>/YYYY/MM/DD/rollout-<ts>-<uuid>.jsonl``——codex-rs
    `recorder.rs::precompute_log_file_info()` 按日期分桶，文件名时间戳的 `:`
    已替换为 `-` 兼容 NTFS（Windows）。

    跨平台一致：macOS / Linux / Windows 都走 ``<HOME>/.codex/...``，不走 XDG
    （codex 是"传统 ~/.<app>/" 风格，参 docs/dev-plan/adapter-research.md
    "Codex CLI > 轨迹采集"段）。
    """
    return home / ".codex" / "sessions"


def _codex_rollout_root_path(home: Path) -> Path:
    """Codex 活跃与已归档 rollout 的共同根目录。"""
    return home / ".codex"


# ─────────────────────────────────────────────────────────────────
# Installer
# ─────────────────────────────────────────────────────────────────


def install_to_codex(
    skill_path: Path | str,
    target_root: Path | str | None = None,
    side: str = "main",
) -> Path:
    """把一个 skill 装到 ``<target_root>/.agents/skills/<name>``——codex 的 user
    scope skill 目录。

    **重要**：路径是 ``.agents``（跨生态共享）而非 ``.codex``。codex 0.130 的
    ``core-skills/src/loader.rs:294`` 已把 ``$CODEX_HOME/skills/`` 标 ``/* Deprecated */``
    ——首选 user-scope 路径是 ``$HOME/.agents/skills/``。OpenCode 也扫这里，
    所以 codex 与 opencode 装到同一个目录，xskill 不重复写。

    其它语义（main/staging、三阶 fallback、symlink no-op、replaced-by-symlink
    备份）与 ``install_to_claude_code`` 完全一致——共享底层 ``_install_skill_into``
    实现。
    """
    root = Path(target_root) if target_root else Path.home()
    return _install_skill_into(
        Path(skill_path),
        _agents_skills_path(root),
        side,
        ecosystem_label="codex",
    )


def install_all_to_codex(
    skill_dir: Path | str,
    target_root: Path | str | None = None,
    names: Iterable[str] | None = None,
) -> list[Path]:
    """Install every skill under ``skill_dir`` (each subdir = one skill) to
    Codex's discovery root (``<target_root>/.agents/skills``). If ``names`` is
    given, restrict to those.
    """
    return _install_all_with(install_to_codex, skill_dir, target_root, names)


# ─────────────────────────────────────────────────────────────────
# Codex-specific trajectory helpers
# ─────────────────────────────────────────────────────────────────


def _codex_session_id_from_path(jsonl_path: Path) -> str:
    """从 codex rollout 文件名抽 session UUID。

    文件名形如 ``rollout-2026-01-15T10-00-00-11111111-2222-3333-4444-555555555555.jsonl``。
    timestamp 字段（前 19 个字符 = ``YYYY-MM-DDTHH-MM-SS``）后 + ``-`` + UUID。

    我们用从右起的最后 5 个 ``-`` 段拼出 UUID（标准 UUID 含 4 个 ``-``，加上文件名
    里 UUID 之前的那一个 ``-``，所以从右数倒数 ``[-5:]`` 段就是 UUID 的全部）。
    """
    stem = jsonl_path.stem  # 去掉 .jsonl
    parts = stem.split("-")
    if len(parts) >= 5:
        # 标准 UUID 5 段：xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
        return "-".join(parts[-5:])
    # 文件名不符合预期，退化为整个 stem（避免崩，让上层去重照常）
    return stem


def _read_cwd_from_codex_jsonl(jsonl_content: str) -> str:
    """从 codex rollout JSONL 字符串抽 cwd。

    codex schema：首行（且仅首行）是 ``type=session_meta`` 行，``payload.cwd``
    即用户当时的 cwd。与 CC 不同——CC 每条事件都带 ``cwd``。
    """
    line_end = jsonl_content.find("\n")
    line = jsonl_content[:line_end if line_end >= 0 else len(jsonl_content)].strip()
    if not line:
        return ""
    try:
        ev = json.loads(line)
    except json.JSONDecodeError:
        return ""
    if ev.get("type") == "session_meta":
        payload = ev.get("payload") or {}
        cwd = payload.get("cwd")
        if cwd:
            return str(cwd)
    return ""


def _read_cwd_from_codex_path(path: Path) -> str:
    try:
        with path.open("r", encoding="utf-8", errors="ignore") as source:
            return _read_cwd_from_codex_jsonl(source.readline())
    except OSError:
        return ""


def _codex_traj_id(jsonl_path: Path, session_id: str) -> str:
    """codex bridged 轨迹 ID：``traj_codex_<projectname>_<sid8>``。

    与 ``_cc_traj_id`` 同形，前缀换成 ``traj_codex_`` 让 trajectory 元数据能
    一眼区分来源。cwd 从 codex JSONL 首行抽（不是 CC 的 per-event 字段）。
    """
    cwd = ""
    if jsonl_path.is_file():
        try:
            with jsonl_path.open("r", encoding="utf-8", errors="ignore") as source:
                cwd = _read_cwd_from_codex_jsonl(source.readline())
        except OSError:
            pass
    project = _sanitize_for_filename(Path(cwd).name if cwd else "", maxlen=32) or "unknown"
    sid_short = _sanitize_for_filename(session_id, maxlen=8) or "nosid"
    return f"traj_codex_{project}_{sid_short}"


# ─────────────────────────────────────────────────────────────────
# Ecosystem spec
# ─────────────────────────────────────────────────────────────────

def _codex_session_complete(content: str) -> bool:
    """Codex 低延迟入库只接受已结束或已中止的 turn。"""
    line_end = len(content)
    saw_lifecycle_event = False
    while line_end > 0:
        line_start = content.rfind("\n", 0, line_end) + 1
        line = content[line_start:line_end].strip()
        line_end = max(0, line_start - 1)
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") != "event_msg":
            continue
        payload = event.get("payload") or {}
        subtype = payload.get("type")
        if subtype in {"task_complete", "turn_aborted"}:
            return True
        if subtype == "task_started":
            saw_lifecycle_event = True
            return False
    # 0.147 及更早的 rollout 没有 task lifecycle 事件，保持兼容。
    return not saw_lifecycle_event


def _codex_session_complete_path(path: Path) -> bool:
    latest_lifecycle = ""
    try:
        for _line_number, event in _iter_codex_path_events(path):
            if event.get("type") != "event_msg":
                continue
            payload = event.get("payload") or {}
            subtype = payload.get("type")
            if subtype in {"task_started", "task_complete", "turn_aborted"}:
                latest_lifecycle = str(subtype)
    except OSError:
        return False
    if not latest_lifecycle:
        return True
    return latest_lifecycle in {"task_complete", "turn_aborted"}


def _adapt_codex_rollout_path(path: Path, metadata: dict) -> tuple[str, dict]:
    """Adapt a Codex rollout directly from disk with bounded source memory."""
    return _adapt_codex_events(_iter_codex_path_events(path), metadata)


CODEX_SPEC = EcosystemSpec(
    name="codex",
    source_kind="jsonl",
    sessions_path=_codex_rollout_root_path,
    sessions_glob=(
        "sessions/*/*/*/rollout-*.jsonl",
        "archived_sessions/rollout-*.jsonl",
    ),
    session_id_from_path=_codex_session_id_from_path,
    cwd_from_content=_read_cwd_from_codex_jsonl,
    adapter_format="codex_rollout_jsonl",
    traj_id_prefix="traj_codex_",
    skills_install_path=_agents_skills_path,
    label="codex",
    is_session_complete=_codex_session_complete,
    is_session_complete_path=_codex_session_complete_path,
    cwd_from_path=_read_cwd_from_codex_path,
    adapt_path=_adapt_codex_rollout_path,
    prefer_newest_session_source=True,
)


# ─────────────────────────────────────────────────────────────────
# Trajectory adapter
# ─────────────────────────────────────────────────────────────────


def _codex_message_text(content) -> str:
    """``response_item/message`` 的 ``content`` → 纯文本。0.148 的形态是
    ``[{"type": "input_text"|"output_text", "text": ...}]``；兼容 string。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                tx = block.get("text")
                if tx:
                    parts.append(str(tx))
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(parts)
    return ""


def _iter_codex_events(content: str):
    """逐行解析 rollout，避免 ``splitlines()`` 为整个大文件复制字符串。"""
    line_start = 0
    line_number = 0
    content_length = len(content)
    while line_start < content_length:
        line_end = content.find("\n", line_start)
        if line_end < 0:
            line_end = content_length
        raw_line = content[line_start:line_end].strip()
        line_start = line_end + 1
        line_number += 1
        if not raw_line:
            continue
        try:
            event = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            yield line_number, event


def _iter_codex_path_events(path: Path):
    """从文件流式解析 rollout，不把整份 UTF-8 历史解码成一个巨大字符串。"""
    with path.open("rb") as source:
        for line_number, raw_line in enumerate(source, 1):
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            try:
                event = json.loads(raw_line)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if isinstance(event, dict):
                yield line_number, event


def _codex_tool_input(payload: dict):
    raw = (
        payload.get("input")
        if payload.get("type") == "custom_tool_call"
        else payload.get("arguments")
    )
    if isinstance(raw, str) and payload.get("type") == "function_call":
        if len(raw) > _CODEX_TOOL_INPUT_LIMIT:
            return _bounded_codex_tool_input(raw)
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError:
            decoded = raw
        raw = decoded
    if isinstance(raw, str):
        return _bounded_codex_tool_input(raw)
    if isinstance(raw, (dict, list)):
        encoded = json.dumps(
            raw,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        if len(encoded) <= _CODEX_TOOL_INPUT_LIMIT:
            return raw
        return _bounded_codex_tool_input(encoded)
    return "" if raw is None else _bounded_codex_tool_input(str(raw))


def _bounded_codex_tool_input(value: str) -> str:
    if len(value) <= _CODEX_TOOL_INPUT_LIMIT:
        return value
    hasher = hashlib.sha256()
    for offset in range(0, len(value), 4096):
        hasher.update(value[offset : offset + 4096].encode("utf-8"))
    digest = hasher.hexdigest()
    suffix = f"...[truncated original_chars={len(value)} sha256={digest}]"
    return value[: _CODEX_TOOL_INPUT_LIMIT - len(suffix)] + suffix


def _codex_tool_output_text(output) -> str:
    """将 Codex 的 string / content-block 工具输出有界转成文本。"""
    parts: list[str] = []
    remaining = 2000
    items = output if isinstance(output, list) else [output]
    for item in items:
        if remaining <= 0:
            break
        text = ""
        if isinstance(item, str):
            text = item
        elif isinstance(item, dict):
            block_type = item.get("type")
            if block_type in {"input_text", "output_text", "text"}:
                text = str(item.get("text") or "")
            elif block_type in {"input_image", "output_image", "image"}:
                text = "[image output]"
            else:
                text = json.dumps(item, ensure_ascii=False)
        elif item is not None:
            text = str(item)
        if not text:
            continue
        if parts:
            parts.append("\n")
            remaining -= 1
        if remaining <= 0:
            break
        piece = text[:remaining]
        parts.append(piece)
        remaining -= len(piece)
    return "".join(parts)


_CODEX_EXIT_CODE_RE = re.compile(r'"exit_code"\s*:\s*(-?\d+)')
_CODEX_IS_ERROR_RE = re.compile(r'"isError"\s*:\s*(true|false)', re.IGNORECASE)


def _codex_tool_output_is_error(payload: dict) -> bool:
    if str(payload.get("status") or "").lower() in {"failed", "error", "cancelled"}:
        return True
    output = payload.get("output")
    items = output if isinstance(output, list) else [output]
    for item in items:
        if isinstance(item, dict):
            text = item.get("text")
            if item.get("is_error") is True or item.get("isError") is True:
                return True
        else:
            text = item
        if not isinstance(text, str):
            continue
        structured_prefix = text[:4096]
        is_error = _CODEX_IS_ERROR_RE.search(structured_prefix)
        if is_error and is_error.group(1).lower() == "true":
            return True
        exit_code = _CODEX_EXIT_CODE_RE.search(structured_prefix)
        if exit_code and int(exit_code.group(1)) != 0:
            return True
    return False


def _adapt_codex_events(events, metadata: dict) -> tuple[str, dict]:
    """Convert a Codex CLI rollout JSONL (``~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl``)
    to markdown + metadata.

    Codex rollout schema（来自 ``codex-rs/protocol/src/protocol.rs::RolloutItem``）：
    每行是 ``{"timestamp", "type", "payload"}`` 三件套，``type`` 是 tagged-union 标签：

    - ``session_meta`` —— 首行，``payload`` 含 ``id``/``cwd``/``originator``/
      ``cli_version``/``model_provider`` 等
    - ``event_msg`` —— 事件流。``payload.type=user_message`` 携带用户输入
    - ``response_item`` —— 模型响应（message / tool call / function output）
    - ``turn_context`` —— 每 turn 的 cwd / approval / sandbox / model
    - ``compacted`` —— 上下文压缩事件

    适配器保留用户/助手文本、结构化工具调用与输出、模型及 Token 事件。
    Codex 新版会把同一条用户或助手消息以两种相邻事件同时记录，这里只去重
    相邻、文本完全相同且来源不同的一对，不会吞掉用户真实重复输入。
    """
    timeline: list[dict] = []
    session_id = ""
    cwd = ""
    originator = ""
    cli_version = ""
    model_provider = ""
    source_model = ""
    current_model = ""
    first_user_query = ""
    execution_usage_events: list[dict] = []
    tool_calls: list[dict] = []
    tool_names: list[str] = []
    pending_tool_by_id: dict[str, str] = {}
    previous_cumulative_usage: dict = {}
    last_message: tuple[int, str, str, str] | None = None
    t = 0
    step = 0
    response_count = 0
    usage_ordinal = 0

    def append_message(line_number: int, role: str, text: str, origin: str) -> None:
        nonlocal first_user_query, last_message, t
        duplicate = (
            last_message is not None
            and last_message[0] + 1 == line_number
            and last_message[1] == role
            and last_message[2] != origin
            and last_message[3] == text
        )
        last_message = (line_number, role, origin, text)
        if duplicate:
            return
        if role == "user" and not first_user_query:
            first_user_query = text[:500]
        timeline.append({"t": t, "role": role, "content": text[:2000]})
        t += 1

    for line_number, event in events:

        ev_type = event.get("type")
        payload = event.get("payload") or {}

        if ev_type == "session_meta":
            session_id = session_id or str(payload.get("id") or "")
            cwd = cwd or str(payload.get("cwd") or "")
            originator = originator or str(payload.get("originator") or "")
            cli_version = cli_version or str(payload.get("cli_version") or "")
            mp = payload.get("model_provider")
            if mp:
                model_provider = model_provider or str(mp)
            continue

        if ev_type == "event_msg":
            sub_type = payload.get("type")
            if sub_type == "user_message":
                msg = str(payload.get("message") or "")
                if msg:
                    append_message(line_number, "user", msg, "event_msg")
            elif sub_type == "agent_message":
                msg = str(payload.get("message") or "")
                if msg:
                    append_message(line_number, "assistant", msg, "event_msg")
            elif sub_type == "token_count":
                info = payload.get("info") or {}
                usage = info.get("last_token_usage") or {}
                usage_ordinal += 1
                if isinstance(usage, dict) and usage:
                    execution_usage_events.append({
                        "source_event_id": (
                            f"{session_id or 'codex'}:token-last:"
                            f"{event.get('ordinal') or usage_ordinal}"
                        ),
                        "usage": {
                            "input_tokens": usage.get("input_tokens"),
                            "output_tokens": usage.get("output_tokens"),
                            "total_tokens": usage.get("total_tokens"),
                            "cached_input_tokens": usage.get(
                                "cached_input_tokens"
                            ),
                        },
                        "model": {
                            "model_id": current_model or source_model or None,
                            "provider": model_provider or None,
                        },
                        "observed_at": event.get("timestamp"),
                    })
                    if isinstance(info.get("total_token_usage"), dict):
                        previous_cumulative_usage = dict(
                            info["total_token_usage"]
                        )
                elif isinstance(info.get("total_token_usage"), dict):
                    cumulative_usage = info["total_token_usage"]
                    delta_usage = {}
                    for field_name in (
                        "input_tokens", "output_tokens", "total_tokens",
                        "cached_input_tokens",
                    ):
                        current_value = cumulative_usage.get(field_name)
                        previous_value = previous_cumulative_usage.get(field_name)
                        if not isinstance(current_value, int):
                            delta_usage[field_name] = None
                        elif (
                            isinstance(previous_value, int)
                            and current_value >= previous_value
                        ):
                            delta_usage[field_name] = current_value - previous_value
                        else:
                            delta_usage[field_name] = current_value
                    execution_usage_events.append({
                        "source_event_id": (
                            f"{session_id or 'codex'}:token-delta:"
                            f"{event.get('ordinal') or usage_ordinal}"
                        ),
                        "usage": delta_usage,
                        "model": {
                            "model_id": current_model or source_model or None,
                            "provider": model_provider or None,
                        },
                        "observed_at": event.get("timestamp"),
                    })
                    previous_cumulative_usage = dict(cumulative_usage)
            continue

        if ev_type == "turn_context":
            model = payload.get("model")
            if model:
                current_model = str(model)
                source_model = source_model or current_model
            continue

        if ev_type == "response_item":
            response_count += 1
            # codex-cli ≥0.148 把用户输入也写成 response_item/message/role=user，
            # 并把注入上下文混在同一形态里：
            # role=developer 的 skills 说明、role=user 的 <environment_context>
            # 环境快照——只有不以 "<" 开头的 user 文本才是用户真正说的话。
            response_type = payload.get("type")
            if response_type == "message":
                role = payload.get("role")
                text = _codex_message_text(payload.get("content"))
                if role == "user" and text and not text.lstrip().startswith("<"):
                    append_message(line_number, "user", text, "response_item")
                    continue
                if role == "assistant" and text:
                    append_message(line_number, "assistant", text, "response_item")
                    continue
                if role in ("user", "developer"):
                    continue  # 注入的运行时上下文 / 系统说明，不进 timeline
            if response_type in {"custom_tool_call", "function_call"}:
                tool_name = str(payload.get("name") or "unknown")
                call_id = str(payload.get("call_id") or payload.get("id") or "")
                tool_input = _codex_tool_input(payload)
                if tool_name not in tool_names:
                    tool_names.append(tool_name)
                if call_id:
                    pending_tool_by_id[call_id] = tool_name
                timeline.append({
                    "t": t,
                    "role": "tool_call",
                    "tool": tool_name,
                    "input": tool_input,
                })
                tool_calls.append({
                    "step": step,
                    "tool": tool_name,
                    "input": tool_input,
                    "output": "",
                    "output_available": False,
                    "is_error": False,
                    "_tc_id": call_id,
                })
                step += 1
                t += 1
                continue
            if response_type in {"custom_tool_call_output", "function_call_output"}:
                call_id = str(payload.get("call_id") or payload.get("id") or "")
                tool_name = str(
                    payload.get("name") or pending_tool_by_id.get(call_id) or "unknown"
                )
                output_text = _codex_tool_output_text(payload.get("output"))
                is_error = _codex_tool_output_is_error(payload)
                timeline.append({
                    "t": t,
                    "role": "tool_output",
                    "tool": tool_name,
                    "output": output_text,
                    "is_error": is_error,
                })
                for entry in reversed(tool_calls):
                    if call_id and entry.get("_tc_id") == call_id:
                        entry["output"] = output_text
                        entry["output_available"] = True
                        entry["is_error"] = is_error
                        break
                t += 1
                continue
            if response_type in {"reasoning", "agent_message"}:
                continue
            # 旧版无 tagged payload 的 response_item 保留占位以兼容旧轨迹。
            timeline.append({
                "t": t, "role": "assistant",
                "content": f"[codex response_item #{payload.get('index', response_count - 1)}]",
            })
            t += 1
            continue

        # compacted / 未来变体：透传，不深析
        timeline.append({
            "t": t, "role": "event",
            "kind": ev_type or "unknown",
        })
        t += 1

    for entry in tool_calls:
        entry.pop("_tc_id", None)

    # Build markdown
    lines: list[str] = ["# Codex Rollout Trajectory", ""]
    if session_id:
        lines.append(f"**session_id**: {session_id}")
    if cwd:
        lines.append(f"**cwd**: {cwd}")
    if originator:
        lines.append(f"**originator**: {originator}")
    if cli_version:
        lines.append(f"**cli_version**: {cli_version}")
    if model_provider:
        lines.append(f"**model_provider**: {model_provider}")
    lines.append("")
    if first_user_query:
        lines.append("## Initial Query")
        lines.append("")
        lines.append(first_user_query)
        lines.append("")

    for entry in timeline:
        role = entry["role"]
        if role == "user":
            lines.append("## User")
            lines.append("")
            lines.append(entry["content"])
            lines.append("")
        elif role == "assistant":
            lines.append("## Assistant")
            lines.append("")
            lines.append(entry["content"])
            lines.append("")
        elif role == "tool_call":
            lines.append(f"## Tool Call: {entry['tool']}")
            lines.append("```json")
            tool_input = entry["input"]
            encoded_input = (
                tool_input
                if isinstance(tool_input, str)
                else json.dumps(tool_input, ensure_ascii=False)
            )
            lines.append(encoded_input[:1000])
            lines.append("```")
            lines.append("")
        elif role == "tool_output":
            err_tag = " (error)" if entry.get("is_error") else ""
            lines.append(f"## Tool Output: {entry['tool']}{err_tag}")
            lines.append("```")
            lines.append(entry["output"])
            lines.append("```")
            lines.append("")
        elif role == "event":
            lines.append(f"## Event: {entry['kind']}")
            lines.append("")

    md = "\n".join(lines)

    meta = dict(metadata)
    meta.setdefault("source", "codex_rollout_jsonl")
    meta.setdefault("category", "codex_session")
    if session_id:
        meta.setdefault("session_id", session_id)
    if cwd:
        meta.setdefault("cwd", cwd)
    if originator:
        meta.setdefault("originator", originator)
    if cli_version:
        meta.setdefault("cli_version", cli_version)
    if model_provider:
        meta.setdefault("model_provider", model_provider)
        meta.setdefault("source_provider", model_provider)
    if source_model:
        meta.setdefault("source_model", source_model)
    if execution_usage_events:
        for usage_event in execution_usage_events:
            usage_event.setdefault("model", {
                "model_id": source_model or None,
                "provider": model_provider or None,
            })
            usage_event["harness"] = {
                "name": "codex",
                "version": cli_version or None,
            }
        meta["execution_usage_events"] = execution_usage_events
    meta["timeline"] = timeline
    meta["tool_calls"] = tool_calls
    meta["tool_names"] = tool_names
    meta["total_tool_calls"] = len(tool_calls)
    meta["total_turns"] = len(timeline)
    meta["response_items"] = response_count
    if first_user_query:
        meta.setdefault("query", first_user_query)

    return md, meta


def _adapt_codex_rollout_jsonl(content: str, metadata: dict) -> tuple[str, dict]:
    """Adapt an in-memory Codex rollout JSONL snapshot."""
    return _adapt_codex_events(_iter_codex_events(content), metadata)


# ─────────────────────────────────────────────────────────────────
# Ingest — bridge Codex rollout JSONL into xskill traj dir
# ─────────────────────────────────────────────────────────────────


def ingest_codex_sessions(
    target_traj_dir: Path | str,
    *,
    home_root: Path | str | None = None,
    seen_sessions: Optional[set[str]] = None,
) -> list[dict]:
    """Bridge Codex CLI rollout JSONLs into xskill's trajectory directory.

    Scans active ``<home_root>/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl``
    and flat ``<home_root>/.codex/archived_sessions/rollout-*.jsonl`` files,
    then submits any session whose UUID is not in ``seen_sessions`` as a new
    trajectory under ``target_traj_dir`` using the ``codex_rollout_jsonl``
    adapter. ``seen_sessions`` is updated in place so repeat calls are
    idempotent. Returns the list of submission results (each augmented with
    ``session_id``, ``source_jsonl``, ``session_start_t``).

    与 ``ingest_claude_code_sessions`` 同形——同一 ``JsonlIngester`` 基类，
    只是 spec 不同。
    """
    return JsonlIngester(CODEX_SPEC).scan_and_bridge(
        target_traj_dir=Path(target_traj_dir),
        home_root=Path(home_root) if home_root else None,
        seen_sessions=seen_sessions,
    )
