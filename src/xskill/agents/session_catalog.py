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
_MD_CARD_MAX_BYTES = 100_000
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


def _is_within_root(path: Path, root: Path) -> bool:
    if root.is_file():
        return path == root
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _iter_traj_files(roots: list[Path]) -> list[Path]:
    from xskill.agents.agent_tools import _is_blocked_read_path

    found: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        if not root.exists():
            continue
        candidates: list[Path] = []
        if root.is_file():
            if root.suffix in {".json", ".md"} and _TRAJ_FILE.match(root.name):
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
        for path in sorted(candidates):
            try:
                resolved = path.resolve()
            except OSError:
                continue
            key = str(resolved)
            if (
                key in seen
                or not _is_within_root(resolved, root)
                or _is_blocked_read_path(resolved)
            ):
                continue
            seen.add(key)
            found.append(resolved)
            if len(found) >= _SCAN_MAX_FILES:
                return found
    return found


def _session_file_index(roots: list[Path]) -> dict[str, dict[str, Path]]:
    index: dict[str, dict[str, Path]] = {}
    for path in _iter_traj_files(roots):
        sidecars = index.setdefault(path.stem, {})
        sidecars.setdefault(path.suffix, path)
    return index


def _read_text_prefix(path: Path, max_bytes: int) -> tuple[str, bool]:
    with path.open("rb") as handle:
        raw = handle.read(max_bytes + 1)
    truncated = len(raw) > max_bytes
    return raw[:max_bytes].decode("utf-8", errors="replace"), truncated


def _strip_frontmatter(text: str) -> str:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return text
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            return "\n".join(lines[index + 1 :]).lstrip()
    return text


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
    try:
        size = path.stat().st_size if path.is_file() else 0
    except OSError:
        size = 0
    item: dict[str, Any] = {
        "traj_id": traj_id,
        "bytes": size,
        "source": "session",
        "query": "",
        "turns": 0,
        "tools": [],
        "_provided": set(),
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
            source = obj.get("source") or obj.get("category")
            if source:
                item["source"] = str(source)
                item["_provided"].add("source")
            else:
                item["source"] = "json"
            if obj.get("query"):
                item["query"] = _one_line(
                    _secret_scrub(str(obj["query"])), _QUERY_SNIP
                )
                item["_provided"].add("query")
            timeline = obj.get("timeline") or []
            timeline = timeline if isinstance(timeline, list) else []
            total_turns = obj.get("total_turns")
            if (
                isinstance(total_turns, int)
                and not isinstance(total_turns, bool)
                and total_turns >= 0
            ):
                item["turns"] = total_turns
                item["_provided"].add("turns")
            else:
                item["turns"] = len(timeline)
            raw_tools = obj.get("tool_names")
            tools = (
                [str(value) for value in raw_tools if isinstance(value, str) and value]
                if isinstance(raw_tools, list)
                else []
            )
            if isinstance(raw_tools, list):
                item["_provided"].add("tools")
            if not tools and isinstance(timeline, list):
                tools = sorted({
                    str(ev.get("tool"))
                    for ev in timeline
                    if isinstance(ev, dict) and ev.get("tool")
                })
            item["tools"] = tools[:12]
            item["_timeline"] = [ev for ev in timeline if isinstance(ev, dict)]
        return item
    try:
        text, truncated = _read_text_prefix(path, _MD_CARD_MAX_BYTES)
    except OSError:
        item["query"] = "(md 读失败)"
        return item
    item["source"] = "markdown"
    for line in text.splitlines()[:40]:
        if line.startswith("traj_id:"):
            item["traj_id"] = line.split(":", 1)[1].strip() or traj_id
        if line.startswith("source:"):
            item["source"] = line.split(":", 1)[1].strip() or item["source"]
            item["_provided"].add("source")
        if line.startswith("turns:"):
            try:
                item["turns"] = int(line.split(":", 1)[1].strip() or 0)
                item["_provided"].add("turns")
            except ValueError:
                pass
        if line.startswith("tools:"):
            item["tools"] = [x.strip() for x in line.split(":", 1)[1].split(",") if x.strip()][:12]
            item["_provided"].add("tools")
    if "# query" in text:
        after = text.split("# query", 1)[1]
        item["query"] = _one_line(_secret_scrub(after.split("#", 1)[0]), _QUERY_SNIP)
    elif not item["query"]:
        item["query"] = _one_line(_secret_scrub(text), _QUERY_SNIP)
    if item["query"]:
        item["_provided"].add("query")
    item["_md"] = text
    item["_md_truncated"] = truncated
    return item


def summarize_session_files(
    traj_id: str, sidecars: dict[str, Path]
) -> dict[str, Any]:
    summaries = {
        suffix: summarize_session_file(path) for suffix, path in sidecars.items()
    }
    json_item = summaries.get(".json")
    md_item = summaries.get(".md")
    primary = dict(json_item or md_item or {})
    primary["traj_id"] = traj_id
    primary["bytes"] = sum(int(item.get("bytes") or 0) for item in summaries.values())
    primary["formats"] = [suffix[1:] for suffix in sorted(summaries)]
    if json_item and md_item:
        provided = json_item.get("_provided") or set()
        for field in ("source", "query", "turns", "tools"):
            if field not in provided:
                primary[field] = md_item.get(field)
        primary["_md"] = md_item.get("_md")
        primary["_md_truncated"] = md_item.get("_md_truncated", False)
    return primary


def render_session_card(item: dict[str, Any]) -> str:
    tools = ", ".join(item.get("tools") or []) or "(none)"
    body = [
        "---",
        f"traj_id: {item.get('traj_id')}",
        f"source: {item.get('source')}",
        f"turns: {item.get('turns') or 0}",
        f"tools: {tools}",
        f"bytes: {item.get('bytes') or 0}",
        f"formats: {','.join(item.get('formats') or []) or 'unknown'}",
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
    else:
        body.append("- (no timeline on card)")
    if item.get("_md"):
        body.extend(["", "# session body", _strip_frontmatter(str(item["_md"]))])
        if item.get("_md_truncated"):
            body.append("…[source truncated]")
    text = _secret_scrub("\n".join(body) + "\n")
    if len(text) > _CARD_CHAR_BUDGET:
        text = text[: _CARD_CHAR_BUDGET - 20] + "\n…[card truncated]\n"
    return text


def _session_card_from_index(
    traj_id: str, index: dict[str, dict[str, Path]]
) -> str:
    tid = (traj_id or "").strip()
    if not tid:
        return "error: traj_id 为空"
    if "/" in tid or "\\" in tid or tid.endswith(".md") or tid.endswith(".json"):
        return "error: traj_id 只要 id 本身，不要带路径或后缀"
    sidecars = index.get(tid)
    if sidecars is None:
        return f"error: 找不到会话 {tid}。先 list_sessions。"
    item = summarize_session_files(tid, sidecars)
    formats = ",".join(item.get("formats") or []) or "unknown"
    return f"traj_id={tid}\nformats={formats}\n\n{render_session_card(item)}"


@tool(name="list_sessions")
def list_sessions(offset: int = 0, limit: int = 60, query: str = "") -> str:
    """列出会话级白盒轨迹：id、来源、工具、query 摘要。这是扫面，不是精读。

    要看某一条的时间线，用 session_card 或 session_cards。不要 read_file 原始大 json。
    """
    try:
        start = max(0, int(offset))
        take = max(1, min(int(limit), 80))
    except (TypeError, ValueError):
        return "error: offset/limit 必须是整数"
    index = _session_file_index(_session_roots())
    traj_ids = sorted(index)
    needle = (query or "").strip().lower()
    if needle:
        matched = []
        for traj_id in traj_ids:
            item = summarize_session_files(traj_id, index[traj_id])
            haystack = " ".join(
                [
                    str(item.get("traj_id") or ""),
                    str(item.get("source") or ""),
                    str(item.get("query") or ""),
                    " ".join(item.get("tools") or []),
                ]
            ).lower()
            if needle in haystack:
                matched.append(item)
        page = matched[start : start + take]
        matched_count = len(matched)
    else:
        page_ids = traj_ids[start : start + take]
        page = [summarize_session_files(tid, index[tid]) for tid in page_ids]
        matched_count = len(traj_ids)
    lines = [
        f"level=session-white total={len(index)} matched={matched_count} "
        f"showing={len(page)} offset={start}",
        "精读用 session_card(traj_id) 或 session_cards（一次最多 10 个 id）。",
    ]
    for item in page:
        tools = ",".join(item.get("tools") or []) or "-"
        lines.append(
            f"{item.get('traj_id')}\tsource={item.get('source')}\tturns={item.get('turns')}\t"
            f"tools={tools}\tquery={item.get('query')}"
        )
    if start + take < matched_count:
        lines.append(
            f"continue: list_sessions(offset={start + take}, limit={take}, query={query!r})"
        )
    return "\n".join(lines)


@tool(name="session_card")
def session_card(traj_id: str) -> str:
    """读一条会话级白盒卡片：query、工具、截断时间线。不要把原始大 json 倒进上下文。"""
    index = _session_file_index(_session_roots())
    return _session_card_from_index(traj_id, index)


@tool(name="session_cards")
def session_cards(traj_ids: str) -> str:
    """一次读最多 10 条会话卡片。traj_ids 用逗号或空白分开。"""
    raw = (traj_ids or "").replace(",", " ").split()
    ids = [x.strip() for x in raw if x.strip()]
    if not ids:
        return "error: traj_ids 为空"
    if len(ids) > 10:
        return f"error: 一次最多 10 条，这次给了 {len(ids)}。拆成多次。"
    index = _session_file_index(_session_roots())
    chunks = [_session_card_from_index(tid, index) for tid in ids]
    return f"batch={len(ids)}\n\n" + "\n\n----\n\n".join(chunks)
