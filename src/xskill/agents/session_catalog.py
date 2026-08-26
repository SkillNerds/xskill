"""Generate 用的会话预览：目录一行一条，卡片只帮选读点。

只给 GenerateAgent 挂工具。SkillEdit / TaskAgent 不导入、不注册。
卡片不是精读：没有 tool result、没有完整回传、不倒原始 json。
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from agno.tools import tool

_TRAJ_FILE = re.compile(r"^traj_[A-Za-z0-9_-]+\.(json|md)$")
_TOOL_USE_MARK = re.compile(r"\[tool_use:\s*([A-Za-z0-9._-]+)\s*([^\]]*)\]")
_USER_QUERY = re.compile(
    r"<user_query>\s*(.*?)\s*</user_query>",
    re.DOTALL,
)
_QUERY_SNIP = 160
_PARAM_SNIP = 80
_LISTING_PEEK_BYTES = 16_384
_CARD_PEEK_BYTES = 200_000
_CARD_CHAR_BUDGET = 2000
_MAX_CARD_TOOLS = 12
_SCAN_MAX_FILES = 2000
_TOOL_PARAM_KEYS = (
    "command", "file_path", "path", "pattern", "query", "glob", "url",
    "target_file", "relative_workspace_path",
)


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
    skip: set[str] = set()
    if ctx.wiki_root is not None:
        try:
            skip.add(str(Path(ctx.wiki_root).resolve()))
        except OSError:
            skip.add(str(Path(ctx.wiki_root)))
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
        if key in seen or key in skip or _is_blocked_read_path(path):
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
                break
        if len(found) >= _SCAN_MAX_FILES:
            break
    return _prefer_md_per_stem(found)


def _prefer_md_per_stem(paths: list[Path]) -> list[Path]:
    """同一 traj_id 只留一条。优先 md，方便卡片上的 L 对上 read_file。"""
    ordered: list[str] = []
    by_stem: dict[str, Path] = {}
    for path in paths:
        stem = path.stem
        if stem not in by_stem:
            by_stem[stem] = path
            ordered.append(stem)
        elif path.suffix == ".md":
            by_stem[stem] = path
    return [by_stem[stem] for stem in ordered]


def _read_prefix(path: Path, max_bytes: int) -> str:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            return handle.read(max_bytes)
    except OSError:
        return ""


def _heading_spans(text: str) -> list[tuple[str, int, int]]:
    lines = text.splitlines()
    heads: list[tuple[str, int]] = []
    for index, line in enumerate(lines):
        if line.startswith("## "):
            heads.append((line[3:].strip(), index + 1))
    spans: list[tuple[str, int, int]] = []
    for index, (heading, start) in enumerate(heads):
        end = heads[index + 1][1] if index + 1 < len(heads) else len(lines) + 1
        spans.append((heading, start, end))
    return spans


def _section_body(text: str, start: int, end: int) -> str:
    lines = text.splitlines()
    return "\n".join(lines[start - 1 : end - 1])


def _query_from_text(text: str) -> tuple[str, int | None]:
    for heading, start, end in _heading_spans(text):
        if heading.lower() != "initial query":
            continue
        body = _section_body(text, start, end)
        match = _USER_QUERY.search(body)
        snippet = match.group(1) if match else body
        line = start
        if match:
            prefix = body[: match.start()]
            line = start + prefix.count("\n")
        return _one_line(_secret_scrub(snippet), _QUERY_SNIP), line
    match = _USER_QUERY.search(text)
    if match:
        line = text[: match.start()].count("\n") + 1
        return _one_line(_secret_scrub(match.group(1)), _QUERY_SNIP), line
    if "# query" in text:
        after = text.split("# query", 1)[1]
        snippet = after.split("#", 1)[0]
        line = text.split("# query", 1)[0].count("\n") + 1
        return _one_line(_secret_scrub(snippet), _QUERY_SNIP), line
    for heading, start, end in _heading_spans(text):
        if heading.lower() != "user":
            continue
        body = _section_body(text, start, end)
        match = _USER_QUERY.search(body)
        snippet = match.group(1) if match else body
        return _one_line(_secret_scrub(snippet), _QUERY_SNIP), start
    return "", None


def _tools_from_text(text: str) -> list[str]:
    names: list[str] = []
    seen: set[str] = set()
    for match in _TOOL_USE_MARK.finditer(text):
        name = match.group(1)
        if name in seen:
            continue
        seen.add(name)
        names.append(name)
        if len(names) >= 12:
            break
    return names


def _tool_lines_from_md(text: str) -> list[str]:
    rows: list[str] = []
    for index, line in enumerate(text.splitlines(), 1):
        match = _TOOL_USE_MARK.search(line)
        if not match:
            continue
        name = match.group(1)
        params = _one_line(_secret_scrub(match.group(2) or ""), _PARAM_SNIP)
        rows.append(f"- L{index} {name} {params}".rstrip())
        if len(rows) >= _MAX_CARD_TOOLS:
            break
    return rows


def _format_tool_params(inp: Any) -> str:
    if isinstance(inp, dict):
        keys = [k for k in _TOOL_PARAM_KEYS if k in inp and inp[k] not in (None, "")]
        for key in inp:
            if str(key).startswith("_") or key in keys:
                continue
            if inp[key] in (None, ""):
                continue
            keys.append(key)
            if len(keys) >= 4:
                break
        parts = [
            f"{key}={_one_line(_secret_scrub(str(inp[key])), _PARAM_SNIP)}"
            for key in keys
        ]
        return " ".join(parts)
    if inp in (None, ""):
        return ""
    return _one_line(_secret_scrub(str(inp)), _PARAM_SNIP)


def _peek_json_meta(path: Path) -> dict[str, Any]:
    raw = _read_prefix(path, _LISTING_PEEK_BYTES)
    item: dict[str, Any] = {"source": "json", "query": "", "tools": []}
    try:
        size = path.stat().st_size
        obj = json.loads(
            path.read_text(encoding="utf-8") if size <= 80_000 else raw
        )
    except (OSError, json.JSONDecodeError, ValueError):
        return item
    if not isinstance(obj, dict):
        return item
    item["source"] = str(obj.get("source") or obj.get("category") or "json")
    item["query"] = _one_line(_secret_scrub(str(obj.get("query") or "")), _QUERY_SNIP)
    tools = [str(x) for x in (obj.get("tool_names") or []) if x]
    timeline = obj.get("timeline") or []
    if not tools and isinstance(timeline, list):
        found: list[str] = []
        for ev in timeline:
            if not isinstance(ev, dict):
                continue
            if ev.get("tool"):
                found.append(str(ev.get("tool")))
            for extra in ev.get("tools") or []:
                if isinstance(extra, dict) and extra.get("tool"):
                    found.append(str(extra.get("tool")))
        tools = list(dict.fromkeys(found))
    item["tools"] = tools[:12]
    if not item["query"] and isinstance(timeline, list):
        for ev in timeline:
            if not isinstance(ev, dict):
                continue
            if str(ev.get("role") or "") != "user":
                continue
            item["query"] = _one_line(
                _secret_scrub(str(ev.get("content") or ev.get("text") or "")),
                _QUERY_SNIP,
            )
            break
    return item


def _tool_lines_from_json(path: Path) -> list[str]:
    try:
        if path.stat().st_size > 400_000:
            return []
        obj = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(obj, dict):
        return []
    timeline = obj.get("timeline") or []
    if not isinstance(timeline, list):
        return []
    rows: list[str] = []
    for ev in timeline:
        if not isinstance(ev, dict):
            continue
        role = str(ev.get("role") or ev.get("type") or "")
        if role in {"tool_output", "tool_result"}:
            continue
        extras = ev.get("tools") if isinstance(ev.get("tools"), list) else []
        if role in {"tool_call", "tool_use"}:
            extras = extras or [ev]
        for item in extras:
            if not isinstance(item, dict):
                continue
            name = str(item.get("tool") or item.get("name") or ev.get("tool") or "tool")
            params = _format_tool_params(item.get("input") or item.get("arguments"))
            rows.append(f"- {name} {params}".rstrip())
            if len(rows) >= _MAX_CARD_TOOLS:
                return rows
    return rows


def _listing_item(path: Path) -> dict[str, Any]:
    item: dict[str, Any] = {
        "traj_id": path.stem,
        "path": str(path),
        "source": "markdown" if path.suffix == ".md" else "json",
        "query": "",
        "tools": [],
    }
    if path.suffix == ".json":
        item.update(_peek_json_meta(path))
        item["traj_id"] = path.stem
        return item
    text = _read_prefix(path, _LISTING_PEEK_BYTES)
    query, _line = _query_from_text(text)
    item["query"] = query
    item["tools"] = _tools_from_text(text)
    item["source"] = "markdown"
    for line in text.splitlines()[:40]:
        if line.startswith("source:"):
            item["source"] = line.split(":", 1)[1].strip() or item["source"]
    return item


def render_session_card(path: Path) -> str:
    traj_id = path.stem
    text = _read_prefix(path, _CARD_PEEK_BYTES)
    query, query_line = _query_from_text(text)
    tools = _tools_from_text(text)
    tool_rows = _tool_lines_from_md(text)
    source = "markdown" if path.suffix == ".md" else "json"
    if path.suffix == ".json":
        meta = _peek_json_meta(path)
        source = str(meta.get("source") or source)
        query = query or str(meta.get("query") or "")
        tools = tools or list(meta.get("tools") or [])
        if not tool_rows:
            tool_rows = _tool_lines_from_json(path)
    query_line_txt = f" L{query_line}" if query_line else ""
    body = [
        f"traj_id: {traj_id}",
        f"source: {source}",
        f"path: {path}",
        f"query{query_line_txt}: {query or '(empty)'}",
        "preview: 卡片只预览，不算读懂。精读用 read_file(path, offset=L)。",
        "toolcalls:",
    ]
    if tool_rows:
        body.extend(tool_rows)
    else:
        body.append("- (no toolcall on card)")
    if tools:
        body.append("tools: " + ", ".join(tools))
    text_out = "\n".join(body) + "\n"
    if len(text_out) > _CARD_CHAR_BUDGET:
        text_out = text_out[: _CARD_CHAR_BUDGET - 20] + "\n…[card truncated]\n"
    return text_out


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
    """列出会话目录：id、来源、工具名、query 一行。这是扫面，不是正文，不算精读。"""
    files = _iter_traj_files(_session_roots())
    items = [_listing_item(path) for path in files]
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
        f"total={len(files)} matched={len(items)} showing={len(page)} offset={start}",
        "这是目录，不是正文。预览用 session_card / session_cards（一次最多 10 条）。",
    ]
    for item in page:
        tools = ",".join(item.get("tools") or []) or "-"
        lines.append(
            f"{item.get('traj_id')}\tsource={item.get('source')}\t"
            f"tools={tools}\tquery={item.get('query')}"
        )
    if start + take < len(items):
        lines.append(
            f"continue: list_sessions(offset={start + take}, limit={take}, query={query!r})"
        )
    return "\n".join(lines)


@tool(name="session_card")
def session_card(traj_id: str) -> str:
    """一条预览卡：query+行号、截断 toolcall、path。没有 tool result，不算精读。"""
    tid = (traj_id or "").strip()
    if not tid:
        return "error: traj_id 为空"
    if "/" in tid or "\\" in tid or tid.endswith(".md") or tid.endswith(".json"):
        return "error: traj_id 只要 id 本身，不要带路径或后缀"
    path = _find_by_id(tid)
    if path is None:
        return f"error: 找不到会话 {tid}。先 list_sessions。"
    return f"traj_id={tid}\n\n{render_session_card(path)}"


@tool(name="session_cards")
def session_cards(traj_ids: str) -> str:
    """一次最多 10 条预览卡。traj_ids 用逗号或空白分开。看过卡片仍不算精读。"""
    raw = (traj_ids or "").replace(",", " ").split()
    ids = [x.strip() for x in raw if x.strip()]
    if not ids:
        return "error: traj_ids 为空"
    if len(ids) > 10:
        return f"error: 一次最多 10 条，这次给了 {len(ids)}。拆成多次。"
    chunks = [session_card.entrypoint(traj_id=tid) for tid in ids]
    return f"batch={len(ids)}\n\n" + "\n\n----\n\n".join(chunks)
