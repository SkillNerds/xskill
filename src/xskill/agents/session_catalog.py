"""会话级（白盒）轨迹目录：短卡片，给 Generate 一次参考几十条。"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from agno.tools import tool

_TRAJ_FILE = re.compile(r"^traj_[A-Za-z0-9_-]+\.(json|md)$")
_QUERY_SNIP = 160
_EVENT_SNIP = 180
_MAX_EVENTS = 8
_CARD_CHAR_BUDGET = 2200
_JSON_CARD_MAX_BYTES = 400_000
_SCAN_MAX_FILES = 400


def _one_line(text: str, limit: int) -> str:
    cleaned = re.sub(r"\s+", " ", (text or "").strip())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 1] + "…"


def _secret_scrub(text: str) -> str:
    text = re.sub(r"(sk-[A-Za-z0-9_-]{8,})", "sk-[REDACTED]", text)
    text = re.sub(
        r"(?i)(api[_-]?key|token|password)\s*[:=]\s*\S+",
        r"\1=[REDACTED]",
        text,
    )
    return text


def _session_roots() -> list[Path]:
    from xskill.agents.agent_tools import (
        _is_blocked_read_path,
        current_agent_tool_context,
    )

    ctx = current_agent_tool_context()
    roots: list[Path] = []
    seen: set[str] = set()
    candidates: list[Path] = []
    if ctx.default_traj_root is not None:
        candidates.append(Path(ctx.default_traj_root))
    candidates.extend(Path(p) for p in (ctx.extra_read_roots or ()))
    for raw in candidates:
        try:
            path = raw.resolve()
        except OSError:
            path = raw
        key = str(path)
        if key in seen or _is_blocked_read_path(path):
            continue
        seen.add(key)
        roots.append(path)
    return roots


def _iter_traj_files(roots: list[Path]) -> list[Path]:
    from xskill.agents.agent_tools import _is_blocked_read_path

    found: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        if not root.exists():
            continue
        candidates: list[Path] = []
        if root.is_file():
            candidates.append(root)
        else:
            try:
                for path in root.rglob("traj_*.*"):
                    if len(candidates) >= _SCAN_MAX_FILES:
                        break
                    if path.suffix not in {".json", ".md"}:
                        continue
                    if not _TRAJ_FILE.match(path.name):
                        continue
                    if path.is_file():
                        candidates.append(path)
            except OSError:
                continue
        for path in candidates:
            try:
                resolved = path.resolve()
            except OSError:
                resolved = path
            key = str(resolved)
            if key in seen or _is_blocked_read_path(resolved):
                continue
            seen.add(key)
            found.append(resolved)
            if len(found) >= _SCAN_MAX_FILES:
                return found
    return found


def _timeline_lines(events: list[dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    for ev in events[:_MAX_EVENTS]:
        role = str(ev.get("role") or ev.get("type") or "?")
        tool_name = ev.get("tool") or ev.get("name") or ""
        if role == "user":
            body = ev.get("content") or ev.get("text") or ""
            lines.append(f"- user: {_one_line(_secret_scrub(str(body)), _EVENT_SNIP)}")
        elif role in {"tool_call", "tool_use"}:
            inp = ev.get("input") or ev.get("arguments") or {}
            if isinstance(inp, dict):
                hint = (
                    inp.get("command")
                    or inp.get("file_path")
                    or inp.get("path")
                    or inp.get("pattern")
                    or ""
                )
            else:
                hint = str(inp)
            lines.append(
                f"- tool {tool_name}: {_one_line(_secret_scrub(str(hint)), _EVENT_SNIP)}"
            )
        elif role in {"tool_output", "tool_result"}:
            out = ev.get("output") or ev.get("content") or ""
            lines.append(
                f"- result {tool_name}: {_one_line(_secret_scrub(str(out)), _EVENT_SNIP)}"
            )
        elif role == "assistant":
            body = ev.get("content") or ev.get("text") or ""
            lines.append(
                f"- assistant: {_one_line(_secret_scrub(str(body)), _EVENT_SNIP)}"
            )
    if len(events) > _MAX_EVENTS:
        lines.append(f"- … 另有 {len(events) - _MAX_EVENTS} 步未写入卡片")
    return lines


def summarize_session_file(path: Path) -> dict[str, Any]:
    """把一条会话文件收成短摘要，不读 17MB 原文进上下文。"""
    traj_id = path.stem
    size = path.stat().st_size if path.is_file() else 0
    item: dict[str, Any] = {
        "traj_id": traj_id,
        "path": str(path),
        "bytes": size,
        "source": "session",
        "query": "",
        "turns": 0,
        "tools": [],
    }
    if path.suffix == ".json":
        if size > _JSON_CARD_MAX_BYTES:
            item["query"] = f"(文件 {size} 字节，只列目录，精读会截断)"
            return item
        try:
            obj = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            item["query"] = "(json 读失败)"
            return item
        if isinstance(obj, dict):
            item["source"] = str(obj.get("source") or obj.get("category") or "json")
            item["query"] = _one_line(_secret_scrub(str(obj.get("query") or "")), _QUERY_SNIP)
            timeline = obj.get("timeline") or []
            item["turns"] = int(obj.get("total_turns") or (len(timeline) if isinstance(timeline, list) else 0) or 0)
            tools = [str(x) for x in (obj.get("tool_names") or []) if x]
            if not tools and isinstance(timeline, list):
                tools = sorted({
                    str(ev.get("tool"))
                    for ev in timeline
                    if isinstance(ev, dict) and ev.get("tool")
                })
            item["tools"] = tools[:12]
            item["_timeline"] = [ev for ev in timeline if isinstance(ev, dict)] if isinstance(timeline, list) else []
        return item
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        item["query"] = "(md 读失败)"
        return item
    item["source"] = "markdown"
    for line in text.splitlines()[:40]:
        if line.startswith("traj_id:"):
            item["traj_id"] = line.split(":", 1)[1].strip() or traj_id
        if line.startswith("source:"):
            item["source"] = line.split(":", 1)[1].strip() or item["source"]
        if line.startswith("turns:"):
            try:
                item["turns"] = int(line.split(":", 1)[1].strip() or 0)
            except ValueError:
                pass
        if line.startswith("tools:"):
            item["tools"] = [x.strip() for x in line.split(":", 1)[1].split(",") if x.strip()][:12]
    if "# query" in text:
        after = text.split("# query", 1)[1]
        item["query"] = _one_line(_secret_scrub(after.split("#", 1)[0]), _QUERY_SNIP)
    elif not item["query"]:
        item["query"] = _one_line(_secret_scrub(text), _QUERY_SNIP)
    item["_md"] = text
    return item


def render_session_card(item: dict[str, Any]) -> str:
    tools = ", ".join(item.get("tools") or []) or "(none)"
    body = [
        "---",
        f"traj_id: {item.get('traj_id')}",
        f"source: {item.get('source')}",
        f"turns: {item.get('turns') or 0}",
        f"tools: {tools}",
        f"bytes: {item.get('bytes') or 0}",
        f"path: {item.get('path')}",
        "level: session-white",
        "---",
        "",
        "# query",
        item.get("query") or "(empty)",
        "",
        "# timeline",
    ]
    if item.get("_timeline"):
        body.extend(_timeline_lines(item["_timeline"]))
    elif item.get("_md"):
        # 已是短卡片就原样截断；长 md 只留头
        text = str(item["_md"])
        if len(text) > _CARD_CHAR_BUDGET:
            text = text[: _CARD_CHAR_BUDGET - 20] + "\n…[card truncated]\n"
        return text if text.endswith("\n") else text + "\n"
    else:
        body.append("- (no timeline on card)")
    text = "\n".join(body) + "\n"
    if len(text) > _CARD_CHAR_BUDGET:
        text = text[: _CARD_CHAR_BUDGET - 20] + "\n…[card truncated]\n"
    return text


def _find_by_id(traj_id: str) -> Path | None:
    tid = (traj_id or "").strip()
    if not tid:
        return None
    for path in _iter_traj_files(_session_roots()):
        if path.stem == tid:
            return path
    return None


@tool(name="list_sessions")
def list_sessions(offset: int = 0, limit: int = 60, query: str = "") -> str:
    """列出会话级白盒轨迹：id、来源、工具、query 摘要。这是扫面，不是精读。

    要看某一条的时间线，用 session_card 或 session_cards。不要 read_file 原始大 json。
    """
    files = _iter_traj_files(_session_roots())
    items = [summarize_session_file(path) for path in files]
    needle = (query or "").strip().lower()
    if needle:
        items = [
            item
            for item in items
            if needle in " ".join(
                [
                    str(item.get("traj_id") or ""),
                    str(item.get("source") or ""),
                    str(item.get("query") or ""),
                    " ".join(item.get("tools") or []),
                ]
            ).lower()
        ]
    try:
        start = max(0, int(offset))
        take = max(1, min(int(limit), 80))
    except (TypeError, ValueError):
        return "error: offset/limit 必须是整数"
    page = items[start : start + take]
    lines = [
        f"level=session-white total={len(files)} matched={len(items)} "
        f"showing={len(page)} offset={start}",
        "精读用 session_card(traj_id) 或 session_cards（一次最多 10 个 id）。",
    ]
    for item in page:
        tools = ",".join(item.get("tools") or []) or "-"
        lines.append(
            f"{item.get('traj_id')}\tsource={item.get('source')}\tturns={item.get('turns')}\t"
            f"tools={tools}\tquery={item.get('query')}"
        )
    if start + take < len(items):
        lines.append(
            f"continue: list_sessions(offset={start + take}, limit={take}, query={query!r})"
        )
    return "\n".join(lines)


@tool(name="session_card")
def session_card(traj_id: str) -> str:
    """读一条会话级白盒卡片：query、工具、截断时间线。不要把原始大 json 倒进上下文。"""
    tid = (traj_id or "").strip()
    if not tid:
        return "error: traj_id 为空"
    if "/" in tid or "\\" in tid or tid.endswith(".md") or tid.endswith(".json"):
        return "error: traj_id 只要 id 本身，不要带路径或后缀"
    path = _find_by_id(tid)
    if path is None:
        return f"error: 找不到会话 {tid}。先 list_sessions。"
    item = summarize_session_file(path)
    return f"traj_id={tid}\ncard={path}\n\n{render_session_card(item)}"


@tool(name="session_cards")
def session_cards(traj_ids: str) -> str:
    """一次读最多 10 条会话卡片。traj_ids 用逗号或空白分开。"""
    raw = (traj_ids or "").replace(",", " ").split()
    ids = [x.strip() for x in raw if x.strip()]
    if not ids:
        return "error: traj_ids 为空"
    if len(ids) > 10:
        return f"error: 一次最多 10 条，这次给了 {len(ids)}。拆成多次。"
    chunks = [session_card.entrypoint(traj_id=tid) for tid in ids]
    return f"batch={len(ids)}\n\n" + "\n\n----\n\n".join(chunks)
