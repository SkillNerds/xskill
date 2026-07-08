"""
ecosystems/nga3.py -- nga3 / CodeAgent3 生态适配
================================================

把蒸馏出的 Skill 装进 CodeAgent3 的 skill discovery 目录
（``~/.cac/skills/<name>/``），并把 nga3 原生 JSONL
（``~/.cac/projects/<encoded-cwd>/<sid>.jsonl``）桥接回 xskill 的标准
``traj_*.md`` 格式。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Iterable, Optional

from xskill.ecosystems._shared import (
    EcosystemSpec,
    JsonlIngester,
    _install_all_with,
    _install_skill_into,
)

logger = logging.getLogger("xskill.ecosystems")

LOCAL_COMMAND_CAVEAT_OPEN = "<local-command-caveat>"


# ─────────────────────────────────────────────────────────────────
# Path helpers
# ─────────────────────────────────────────────────────────────────


def _nga3_projects_path(home: Path) -> Path:
    """nga3 / CodeAgent3 session JSONL 根目录：``<home>/.cac/projects``."""
    return home / ".cac" / "projects"


def _nga3_skills_path(home: Path) -> Path:
    """nga3 / CodeAgent3 skill discovery 根目录：``<home>/.cac/skills``."""
    return home / ".cac" / "skills"


# ─────────────────────────────────────────────────────────────────
# Installer
# ─────────────────────────────────────────────────────────────────


def install_to_nga3(
    skill_path: Path | str,
    target_root: Path | str | None = None,
    side: str = "main",
) -> Path:
    """把一个 skill 装到 ``<target_root>/.cac/skills/<name>``。

    与 Claude Code / Cursor 一样走 symlink-first 三阶 fallback；staging
    语义也复用 ``_install_skill_into``。
    """
    root = Path(target_root) if target_root else Path.home()
    return _install_skill_into(
        Path(skill_path),
        _nga3_skills_path(root),
        side,
        ecosystem_label="nga3",
    )


def install_all_to_nga3(
    skill_dir: Path | str,
    target_root: Path | str | None = None,
    names: Iterable[str] | None = None,
) -> list[Path]:
    """Install every skill under ``skill_dir`` to ``<target_root>/.cac/skills``."""
    return _install_all_with(install_to_nga3, skill_dir, target_root, names)


# ─────────────────────────────────────────────────────────────────
# nga3 trajectory helpers
# ─────────────────────────────────────────────────────────────────


def _nga3_session_id_from_path(jsonl_path: Path) -> str:
    """``<sid>.jsonl`` → ``<sid>``。"""
    return jsonl_path.stem


def _read_cwd_from_nga3_jsonl_content(content: str) -> str:
    """读 nga3 JSONL 第一条带 ``cwd`` 字段的事件。"""
    for line in content.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        cwd = ev.get("cwd")
        if cwd:
            return str(cwd).replace("\\", "/")
    return ""


NGA3_SPEC = EcosystemSpec(
    name="nga3",
    source_kind="jsonl",
    sessions_path=_nga3_projects_path,
    sessions_glob="*/*.jsonl",  # <projects>/<encoded-cwd>/<sid>.jsonl
    session_id_from_path=_nga3_session_id_from_path,
    cwd_from_content=_read_cwd_from_nga3_jsonl_content,
    adapter_format="nga3_jsonl",
    traj_id_prefix="traj_nga3_",
    skills_install_path=_nga3_skills_path,
    label="nga3",
)


def _is_local_command_caveat(content: object) -> bool:
    if isinstance(content, str):
        return LOCAL_COMMAND_CAVEAT_OPEN in content
    if isinstance(content, list):
        for part in content:
            if isinstance(part, dict):
                text = part.get("text") or part.get("content")
                if isinstance(text, str) and LOCAL_COMMAND_CAVEAT_OPEN in text:
                    return True
    return False


def _tool_result_to_text(result_content: object) -> str:
    if isinstance(result_content, str):
        return result_content
    if isinstance(result_content, list):
        parts: list[str] = []
        for item in result_content:
            if isinstance(item, dict):
                if item.get("type") == "text":
                    parts.append(str(item.get("text") or ""))
                else:
                    parts.append(json.dumps(item, ensure_ascii=False))
            else:
                parts.append(str(item))
        return "\n".join(p for p in parts if p)
    if isinstance(result_content, dict):
        return json.dumps(result_content, ensure_ascii=False)
    if result_content is None:
        return ""
    return str(result_content)


def _set_first(meta: dict, key: str, value: object) -> None:
    if value not in (None, "") and not meta.get(key):
        meta[key] = value


def _capture_event_metadata(event: dict, meta: dict) -> None:
    _set_first(meta, "provider", event.get("_cac_providerId"))
    _set_first(meta, "model", event.get("_cac_modelId"))
    _set_first(meta, "agent_type", event.get("_cac_agentType"))
    _set_first(meta, "internal_prompt_id", event.get("_cac_promptId"))
    _set_first(meta, "prompt_id", event.get("promptId"))
    _set_first(meta, "permission_mode", event.get("permissionMode"))
    _set_first(meta, "client_version", event.get("version"))
    _set_first(meta, "entrypoint", event.get("entrypoint"))
    _set_first(meta, "user_type", event.get("userType"))
    _set_first(meta, "slug", event.get("slug"))


def _append_meta_event(event: dict, meta_events: list[dict]) -> None:
    meta_events.append({
        "type": event.get("type") or "unknown",
        "uuid": event.get("uuid") or "",
        "message_id": event.get("messageId") or "",
        "timestamp": event.get("timestamp") or "",
        "is_snapshot_update": bool(event.get("isSnapshotUpdate")),
    })


def _adapt_nga3_jsonl(content: str, metadata: dict) -> tuple[str, dict]:
    """Convert a nga3 / CodeAgent3 JSONL session to markdown + metadata."""
    timeline: list[dict] = []
    tool_calls: list[dict] = []
    tool_names: list[str] = []
    meta_events: list[dict] = []
    session_id = ""
    cwd = ""
    git_branch = ""
    first_user_query = ""
    thinking_blocks = 0
    t = 0
    step = 0
    pending_tool_by_id: dict[str, str] = {}

    meta = dict(metadata)

    for raw_line in content.splitlines():
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        try:
            event = json.loads(raw_line)
        except json.JSONDecodeError:
            continue

        _capture_event_metadata(event, meta)

        ev_type = event.get("type")
        if ev_type not in ("user", "assistant"):
            _append_meta_event(event, meta_events)
            continue

        session_id = session_id or str(event.get("sessionId") or "")
        cwd = cwd or str(event.get("cwd") or "")
        git_branch = git_branch or str(event.get("gitBranch") or "")

        msg = event.get("message") or {}
        if ev_type == "assistant":
            _set_first(meta, "model", msg.get("model"))
        msg_content = msg.get("content")

        if ev_type == "user":
            if event.get("isMeta") and _is_local_command_caveat(msg_content):
                meta_events.append({
                    "type": "local-command-caveat",
                    "uuid": event.get("uuid") or "",
                    "timestamp": event.get("timestamp") or "",
                })
                continue

            if isinstance(msg_content, str):
                if _is_local_command_caveat(msg_content):
                    meta_events.append({
                        "type": "local-command-caveat",
                        "uuid": event.get("uuid") or "",
                        "timestamp": event.get("timestamp") or "",
                    })
                    continue
                if not first_user_query:
                    first_user_query = msg_content[:500]
                timeline.append({
                    "t": t, "role": "user",
                    "content": msg_content[:2000],
                    "uuid": event.get("uuid") or "",
                    "prompt_id": event.get("promptId") or "",
                })
                t += 1
            elif isinstance(msg_content, list):
                for part in msg_content:
                    if not isinstance(part, dict):
                        continue
                    ptype = part.get("type")
                    if ptype == "text":
                        text = (part.get("text") or "").strip()
                        if not text or _is_local_command_caveat(text):
                            continue
                        if not first_user_query:
                            first_user_query = text[:500]
                        timeline.append({
                            "t": t, "role": "user",
                            "content": text[:2000],
                            "uuid": event.get("uuid") or "",
                            "prompt_id": event.get("promptId") or "",
                        })
                        t += 1
                    elif ptype == "tool_result":
                        tc_id = part.get("tool_use_id", "")
                        tool_name = pending_tool_by_id.get(tc_id, "unknown")
                        output_text = _tool_result_to_text(part.get("content"))[:2000]
                        entry = {
                            "t": t, "role": "tool_output",
                            "tool": tool_name,
                            "tool_use_id": tc_id,
                            "output": output_text,
                            "is_error": bool(part.get("is_error")),
                            "source_tool_assistant_uuid": event.get("sourceToolAssistantUUID") or "",
                            "tool_use_result": event.get("toolUseResult") or {},
                        }
                        timeline.append(entry)
                        for call in reversed(tool_calls):
                            if call.get("_tc_id") == tc_id:
                                call["output"] = output_text
                                call["output_available"] = True
                                call["is_error"] = bool(part.get("is_error"))
                                call["source_tool_assistant_uuid"] = (
                                    event.get("sourceToolAssistantUUID") or ""
                                )
                                call["tool_use_result"] = event.get("toolUseResult") or {}
                                break
                        t += 1

        else:  # assistant
            if isinstance(msg_content, list):
                for part in msg_content:
                    if not isinstance(part, dict):
                        continue
                    ptype = part.get("type")
                    if ptype == "text":
                        text = (part.get("text") or "").strip()
                        if text:
                            timeline.append({
                                "t": t, "role": "assistant",
                                "content": text[:2000],
                                "uuid": event.get("uuid") or "",
                            })
                            t += 1
                    elif ptype == "tool_use":
                        tc_id = part.get("id", "")
                        tool_name = part.get("name", "unknown")
                        tool_input = part.get("input") or {}
                        caller = part.get("caller") or {}
                        if tool_name not in tool_names:
                            tool_names.append(tool_name)
                        pending_tool_by_id[tc_id] = tool_name
                        timeline.append({
                            "t": t, "role": "tool_call",
                            "tool": tool_name,
                            "input": tool_input,
                            "caller": caller,
                            "tool_use_id": tc_id,
                            "uuid": event.get("uuid") or "",
                        })
                        tool_calls.append({
                            "step": step,
                            "tool": tool_name,
                            "input": tool_input,
                            "caller": caller,
                            "output": "",
                            "output_available": False,
                            "_tc_id": tc_id,
                        })
                        step += 1
                        t += 1
                    elif ptype == "thinking":
                        thinking_blocks += 1

    for entry in tool_calls:
        entry.pop("_tc_id", None)

    lines: list[str] = ["# NGA3 Session Trajectory", ""]
    if session_id:
        lines.append(f"**session_id**: {session_id}")
    if cwd:
        lines.append(f"**cwd**: {cwd}")
    if git_branch:
        lines.append(f"**git_branch**: {git_branch}")
    if meta.get("model"):
        lines.append(f"**model**: {meta['model']}")
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
            lines.append(json.dumps(entry["input"], ensure_ascii=False)[:1000])
            lines.append("```")
            lines.append("")
        elif role == "tool_output":
            err_tag = " (error)" if entry.get("is_error") else ""
            lines.append(f"## Tool Output: {entry['tool']}{err_tag}")
            lines.append("```")
            lines.append(entry["output"])
            lines.append("```")
            lines.append("")

    md = "\n".join(lines)

    meta.setdefault("source", "nga3_session_jsonl")
    meta.setdefault("category", "nga3_session")
    if session_id:
        meta.setdefault("session_id", session_id)
    if cwd:
        meta.setdefault("cwd", cwd)
    if git_branch:
        meta.setdefault("git_branch", git_branch)
    meta["timeline"] = timeline
    meta["tool_calls"] = tool_calls
    meta["tool_names"] = tool_names
    meta["total_tool_calls"] = len(tool_calls)
    meta["total_turns"] = len(timeline)
    meta["thinking_blocks"] = thinking_blocks
    meta["meta_events"] = meta_events
    if first_user_query:
        meta.setdefault("query", first_user_query)

    return md, meta


def ingest_nga3_sessions(
    target_traj_dir: Path | str,
    *,
    home_root: Path | str | None = None,
    seen_sessions: Optional[set[str]] = None,
) -> list[dict]:
    """Bridge nga3 JSONLs into xskill's trajectory directory."""
    return JsonlIngester(NGA3_SPEC).scan_and_bridge(
        target_traj_dir=Path(target_traj_dir),
        home_root=Path(home_root) if home_root else None,
        seen_sessions=seen_sessions,
    )
