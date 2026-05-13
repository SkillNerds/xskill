"""
adapters.py -- Trajectory submission and format adaptation layer
=================================================================
Convert various input formats to the standard xskill format
(markdown + json metadata) and handle submission to the traj directory.
"""

import json
import re
from pathlib import Path
from typing import Optional

from xskill.config import get_traj_dir


def generate_traj_id(traj_dir: Path = None) -> str:
    """
    Auto-generate a traj ID like ``traj_0301`` based on existing files
    in *traj_dir*.  Scans for ``traj_*.md`` and picks max + 1.
    """
    traj_dir = traj_dir or get_traj_dir()
    traj_dir.mkdir(parents=True, exist_ok=True)

    existing_ids: list[int] = []
    for f in traj_dir.glob("traj_*.md"):
        m = re.match(r"traj_(\d+)", f.stem)
        if m:
            existing_ids.append(int(m.group(1)))

    next_id = max(existing_ids) + 1 if existing_ids else 1
    return f"traj_{next_id:04d}"


def adapt_trajectory(
    content: str,
    format: str,
    metadata: Optional[dict] = None,
) -> tuple[str, dict]:
    """
    Convert various input formats to the standard xskill representation.

    Supported *format* values:

    - ``markdown`` -- passthrough; content is already ``traj_*.md`` format.
    - ``json`` -- JSON object with fields like ``messages``, ``tool_calls``, etc.
      Converted to a markdown trajectory.
    - ``raw`` -- plain text; wrapped in a basic trajectory markdown template.

    Returns ``(md_content, json_metadata)``.
    """
    metadata = metadata or {}

    if format == "markdown":
        return content, metadata

    if format == "json":
        return _adapt_json(content, metadata)

    if format == "raw":
        return _adapt_raw(content, metadata)

    if format == "claude_code_jsonl":
        return _adapt_claude_code_jsonl(content, metadata)

    if format == "codex_rollout_jsonl":
        return _adapt_codex_rollout_jsonl(content, metadata)

    raise ValueError(f"unsupported trajectory format: {format!r}")


# ------------------------------------------------------------------
# Internal converters
# ------------------------------------------------------------------

def _adapt_json(content: str, metadata: dict) -> tuple[str, dict]:
    """Convert a JSON trajectory to markdown + metadata."""
    data = json.loads(content)

    # Merge top-level keys (except messages/tool_calls) into metadata
    meta = dict(metadata)
    for key in ("model", "instance_id", "repo", "task", "result", "exit_status"):
        if key in data and key not in meta:
            meta[key] = data[key]

    # Build markdown from messages / tool_calls
    lines: list[str] = []
    lines.append(f"# Trajectory")
    if meta.get("instance_id"):
        lines.append(f"\n**instance_id**: {meta['instance_id']}")
    if meta.get("model"):
        lines.append(f"**model**: {meta['model']}")
    lines.append("")

    messages = data.get("messages", [])
    tool_calls = data.get("tool_calls", [])

    for msg in messages:
        role = msg.get("role", "unknown")
        text = msg.get("content", "")
        lines.append(f"## {role.capitalize()}")
        lines.append("")
        if isinstance(text, str):
            lines.append(text)
        elif isinstance(text, list):
            # multi-part content
            for part in text:
                if isinstance(part, dict):
                    lines.append(part.get("text", str(part)))
                else:
                    lines.append(str(part))
        lines.append("")

    if tool_calls:
        lines.append("## Tool Calls")
        lines.append("")
        for tc in tool_calls:
            name = tc.get("name", tc.get("function", {}).get("name", "unknown"))
            args = tc.get("arguments", tc.get("function", {}).get("arguments", ""))
            lines.append(f"### {name}")
            lines.append("```")
            lines.append(args if isinstance(args, str) else json.dumps(args, ensure_ascii=False))
            lines.append("```")
            if tc.get("output"):
                lines.append(f"\n**output**:\n```\n{tc['output']}\n```")
            lines.append("")

    md_content = "\n".join(lines)
    return md_content, meta


def _adapt_claude_code_jsonl(content: str, metadata: dict) -> tuple[str, dict]:
    """Convert a Claude Code session JSONL (``~/.claude/projects/.../*.jsonl``) to
    markdown + metadata.

    Each line is one event. Recognised event types:

    - ``user``: ``message.content`` may be a string (real user input) or a list
      containing ``tool_result`` parts.
    - ``assistant``: ``message.content`` is a list of parts -- ``text``,
      ``tool_use``, ``thinking``.
    - Anything else (``permission-mode``, ``file-history-snapshot``,
      ``system``, ``attachment``, ``last-prompt``) is skipped.

    Produces a markdown body with ``## User`` / ``## Assistant`` / ``## Tool
    Call`` / ``## Tool Output`` sections and a metadata dict containing
    ``session_id``, ``cwd``, ``git_branch``, ``timeline`` (structured), and
    ``tool_names``.
    """
    timeline: list[dict] = []
    tool_calls: list[dict] = []
    tool_names: list[str] = []
    session_id = ""
    cwd = ""
    git_branch = ""
    first_user_query = ""
    t = 0
    step = 0
    pending_tool_by_id: dict[str, str] = {}

    for raw_line in content.splitlines():
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        try:
            event = json.loads(raw_line)
        except json.JSONDecodeError:
            continue

        ev_type = event.get("type")
        if ev_type not in ("user", "assistant"):
            continue

        session_id = session_id or event.get("sessionId", "") or ""
        cwd = cwd or event.get("cwd", "") or ""
        git_branch = git_branch or event.get("gitBranch", "") or ""

        msg = event.get("message") or {}
        msg_content = msg.get("content")

        if ev_type == "user":
            if isinstance(msg_content, str):
                if not first_user_query:
                    first_user_query = msg_content[:500]
                timeline.append({
                    "t": t, "role": "user",
                    "content": msg_content[:2000],
                })
                t += 1
            elif isinstance(msg_content, list):
                for part in msg_content:
                    if not isinstance(part, dict):
                        continue
                    if part.get("type") == "tool_result":
                        tc_id = part.get("tool_use_id", "")
                        tool_name = pending_tool_by_id.get(tc_id, "unknown")
                        result_content = part.get("content")
                        if isinstance(result_content, list):
                            parts_text = []
                            for rp in result_content:
                                if isinstance(rp, dict) and rp.get("type") == "text":
                                    parts_text.append(rp.get("text") or "")
                            output_text = "\n".join(parts_text)
                        else:
                            output_text = str(result_content) if result_content else ""
                        output_text = output_text[:2000]
                        timeline.append({
                            "t": t, "role": "tool_output",
                            "tool": tool_name,
                            "output": output_text,
                            "is_error": bool(part.get("is_error")),
                        })
                        # Backfill the matching tool_calls entry
                        for entry in reversed(tool_calls):
                            if entry.get("_tc_id") == tc_id:
                                entry["output"] = output_text
                                entry["output_available"] = True
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
                            })
                            t += 1
                    elif ptype == "tool_use":
                        tc_id = part.get("id", "")
                        tool_name = part.get("name", "unknown")
                        tool_input = part.get("input") or {}
                        if tool_name not in tool_names:
                            tool_names.append(tool_name)
                        pending_tool_by_id[tc_id] = tool_name
                        timeline.append({
                            "t": t, "role": "tool_call",
                            "tool": tool_name,
                            "input": tool_input,
                        })
                        tool_calls.append({
                            "step": step,
                            "tool": tool_name,
                            "input": tool_input,
                            "output": "",
                            "output_available": False,
                            "_tc_id": tc_id,
                        })
                        step += 1
                        t += 1
                    # `thinking` parts are intentionally skipped.

    # Strip internal _tc_id from tool_calls before returning
    for entry in tool_calls:
        entry.pop("_tc_id", None)

    # Build markdown body
    lines: list[str] = ["# Claude Code Session Trajectory", ""]
    if session_id:
        lines.append(f"**session_id**: {session_id}")
    if cwd:
        lines.append(f"**cwd**: {cwd}")
    if git_branch:
        lines.append(f"**git_branch**: {git_branch}")
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

    meta = dict(metadata)
    meta.setdefault("source", "claude_code_session_jsonl")
    meta.setdefault("category", "claude_code_session")
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
    if first_user_query:
        meta.setdefault("query", first_user_query)

    return md, meta


def _adapt_codex_rollout_jsonl(content: str, metadata: dict) -> tuple[str, dict]:
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

    P2 阶段我们抽 ``session_meta``（session_id + cwd + originator + cli_version）+
    ``event_msg::user_message``（user query），其它行**透传到 timeline 但不深度解析**
    （codex 的 ``response_item`` 内部结构与 CC 不同，等 P4 再深化）。
    """
    timeline: list[dict] = []
    session_id = ""
    cwd = ""
    originator = ""
    cli_version = ""
    model_provider = ""
    first_user_query = ""
    t = 0
    response_count = 0

    for raw_line in content.splitlines():
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        try:
            event = json.loads(raw_line)
        except json.JSONDecodeError:
            continue

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
                    if not first_user_query:
                        first_user_query = msg[:500]
                    timeline.append({
                        "t": t, "role": "user",
                        "content": msg[:2000],
                    })
                    t += 1
            continue

        if ev_type == "response_item":
            # 占位：codex response_item 的内部 schema 复杂（含 message / tool_use /
            # function_call_output 等），P2 不深化。这里只计数 + 透传一条 timeline
            # 让下游能看到"这条 session 确实有 N 条响应"。
            response_count += 1
            timeline.append({
                "t": t, "role": "assistant",
                "content": f"[codex response_item #{payload.get('index', response_count - 1)}]",
            })
            t += 1
            continue

        # turn_context / compacted / 未来变体：透传，不深析
        timeline.append({
            "t": t, "role": "event",
            "kind": ev_type or "unknown",
        })
        t += 1

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
    meta["timeline"] = timeline
    meta["total_turns"] = len(timeline)
    meta["response_items"] = response_count
    if first_user_query:
        meta.setdefault("query", first_user_query)

    return md, meta


def _adapt_raw(content: str, metadata: dict) -> tuple[str, dict]:
    """Wrap plain text in a basic trajectory markdown template."""
    lines = [
        "# Trajectory",
        "",
        "## Raw Content",
        "",
        content,
        "",
    ]
    md_content = "\n".join(lines)
    return md_content, dict(metadata)


# ------------------------------------------------------------------
# Submission
# ------------------------------------------------------------------

def submit_trajectory(
    content: str,
    format: str = "markdown",
    metadata: Optional[dict] = None,
    traj_id: Optional[str] = None,
    traj_dir: Optional[Path] = None,
) -> dict:
    """
    Complete submission flow:

    1. Resolve *traj_dir* (from param or ``get_traj_dir()``).
    2. Generate *traj_id* if not provided.
    3. Adapt the input format to standard markdown + JSON metadata.
    4. Write ``traj_{id}.md`` and optionally ``traj_{id}.json``.
    5. Return ``{"traj_id": ..., "path": ..., "status": "stored"}``.
    """
    traj_dir = Path(traj_dir) if traj_dir else get_traj_dir()
    traj_dir.mkdir(parents=True, exist_ok=True)

    if not traj_id:
        traj_id = generate_traj_id(traj_dir)

    md_content, json_metadata = adapt_trajectory(content, format, metadata)

    # Write markdown
    md_path = traj_dir / f"{traj_id}.md"
    md_path.write_text(md_content, encoding="utf-8")

    # Write JSON metadata if non-empty
    if json_metadata:
        json_path = traj_dir / f"{traj_id}.json"
        json_path.write_text(
            json.dumps(json_metadata, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    return {
        "traj_id": traj_id,
        "path": str(md_path),
        "status": "stored",
    }
