"""
ecosystems/cursor.py -- Cursor 生态适配
=======================================

把蒸馏出的 Skill 装进 Cursor 的 skill 目录（``~/.cursor/skills/<name>/``），
并把 Cursor 原生 agent-transcript JSONL（``~/.cursor/projects/<encoded-cwd>/
agent-transcripts/<sid>.jsonl``）桥接回 xskill 的标准 ``traj_*.md`` 格式。

本模块含 Cursor 平台的「读」（``_adapt_cursor_transcripts_jsonl`` +
``ingest_cursor_sessions``）与「写」（``install_to_cursor`` /
``install_all_to_cursor``）。
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Iterable, Optional

from xskill.ecosystems._shared import (
    EcosystemSpec,
    JsonlIngester,
    _install_all_with,
    _install_skill_into,
)

logger = logging.getLogger("xskill.ecosystems")


# ─────────────────────────────────────────────────────────────────
# Path helpers
# ─────────────────────────────────────────────────────────────────


def _cursor_projects_path(home: Path) -> Path:
    """Cursor agent-transcripts 根目录：``<home>/.cursor/projects``。

    实际文件在 ``<this>/<encoded-cwd>/agent-transcripts/<sid>.jsonl``——
    Cursor 把每个 project 按 encoded-cwd 分目录（slug 形如
    ``c-yzj-entrepreneurship-XSKILL-xskill``，是工作目录路径替换分隔符 + 小写）。
    """
    return home / ".cursor" / "projects"


def _cursor_skills_path(home: Path) -> Path:
    """Cursor skill discovery 根目录：``<home>/.cursor/skills``。

    每个 skill 落到 ``<this>/<name>/SKILL.md``。``scripts/cursor_setup.ps1``
    用 Windows junction 把这个目录链到 xskill 源仓 ``~/.xskill/skill/``——
    POSIX 上等效就是 ``install_to_cursor`` 给每个 skill 单独建 symlink。
    """
    return home / ".cursor" / "skills"


# ─────────────────────────────────────────────────────────────────
# Installer
# ─────────────────────────────────────────────────────────────────


def install_to_cursor(
    skill_path: Path | str,
    target_root: Path | str | None = None,
    side: str = "main",
) -> Path:
    """把一个 skill 装到 ``<target_root>/.cursor/skills/<name>``——Cursor 的
    skill 目录。

    ``scripts/cursor_setup.ps1`` 在 Windows 上用 ``mklink /J``（NTFS junction）
    把整个 ``~/.cursor/skills/`` 链到 ``~/.xskill/skill/``。本函数走 per-skill
    symlink-first 三阶 fallback（与 ``install_to_claude_code`` 同形），dest
    是 ``~/.cursor/skills/<name>`` 这一层而不是整个目录。两种方案不互相干扰
    —— Windows 用户跑 cursor_setup.ps1 是预装版（整目录 junction），daemon
    起来后这函数对每个 skill 重新装一次 symlink 等效更精细。
    """
    root = Path(target_root) if target_root else Path.home()
    return _install_skill_into(
        Path(skill_path),
        _cursor_skills_path(root),
        side,
        ecosystem_label="cursor",
    )


def install_all_to_cursor(
    skill_dir: Path | str,
    target_root: Path | str | None = None,
    names: Iterable[str] | None = None,
) -> list[Path]:
    """Install every skill under ``skill_dir`` (each subdir = one skill) to
    Cursor's skill root (``<target_root>/.cursor/skills``). If ``names`` is
    given, restrict to those.
    """
    return _install_all_with(install_to_cursor, skill_dir, target_root, names)


# ─────────────────────────────────────────────────────────────────
# Cursor-specific trajectory helpers
# ─────────────────────────────────────────────────────────────────


def _cursor_session_id_from_path(jsonl_path: Path) -> str:
    """``<sid>.jsonl`` → ``<sid>``。"""
    return jsonl_path.stem


def _read_cwd_from_cursor_jsonl(content: str) -> str:
    """Cursor JSONL 每行只有 ``{role, message}``，没有 cwd 字段。

    项目名在路径里，见 ``_cwd_from_cursor_path``。这里返回空串，ingester
    会再问 ``cwd_from_path``。
    """
    del content  # EcosystemSpec callback signature compatibility.
    return ""


def _cwd_from_cursor_path(jsonl_path: Path) -> str:
    """从 ``<encoded-cwd>/agent-transcripts/...`` 取出 Cursor 的项目 slug。

    本机两种布局都是这个结构：
      ~/.cursor/projects/<encoded-cwd>/agent-transcripts/<sid>.jsonl
      ~/.cursor/projects/<encoded-cwd>/agent-transcripts/<sid>/<sid>.jsonl
    encoded-cwd 就是工作目录把分隔符换成 ``-`` 再小写，例如
    ``home-admin-xskill``。拿它当 project 段，traj_id 才分得出项目。
    """
    parts = jsonl_path.resolve().parts
    for index, part in enumerate(parts):
        if part == "agent-transcripts" and index > 0:
            return parts[index - 1]
    return ""


def _decode_cursor_slug(slug: str) -> str:
    """把 ``home-admin-my-service`` 反解成磁盘上真实存在的 ``/home/admin/my-service``。

    slug 里的连字符既可能是路径分隔符也可能是目录名的一部分，且已被小写，
    只能逐级对照磁盘上的目录名试出来；对不上（项目已删除）返回空串。
    """
    tokens = [token for token in slug.split("-") if token]
    if not tokens:
        return ""
    if os.name == "nt":
        if len(tokens[0]) != 1 or not tokens[0].isalpha():
            return ""
        root = Path(f"{tokens[0].upper()}:\\")
        tokens = tokens[1:]
    else:
        root = Path("/")

    def walk(current: Path, index: int) -> Optional[Path]:
        if index == len(tokens):
            return current
        try:
            entries = {name.lower(): name for name in os.listdir(current)}
        except OSError:
            return None
        # 先试最长的候选名，让带连字符的目录名优先于把它拆成多级
        for end in range(len(tokens), index, -1):
            actual = entries.get("-".join(tokens[index:end]))
            if actual is None or not (current / actual).is_dir():
                continue
            found = walk(current / actual, end)
            if found is not None:
                return found
        return None

    decoded = walk(root, 0)
    return str(decoded) if decoded is not None else ""


def _project_dir_from_cursor_path(jsonl_path: Path) -> str:
    return _decode_cursor_slug(_cwd_from_cursor_path(jsonl_path))


# ─────────────────────────────────────────────────────────────────
# Ecosystem spec
# ─────────────────────────────────────────────────────────────────

CURSOR_SPEC = EcosystemSpec(
    name="cursor",
    source_kind="jsonl",
    sessions_path=_cursor_projects_path,
    # 两种布局：<encoded-cwd>/agent-transcripts/<sid>.jsonl
    # 以及本机 Cursor 实际用的 <encoded-cwd>/agent-transcripts/<sid>/<sid>.jsonl
    sessions_glob="*/agent-transcripts/**/*.jsonl",
    session_id_from_path=_cursor_session_id_from_path,
    cwd_from_content=_read_cwd_from_cursor_jsonl,
    cwd_from_path=_cwd_from_cursor_path,
    project_dir_from_path=_project_dir_from_cursor_path,
    adapter_format="cursor_transcripts_jsonl",
    traj_id_prefix="traj_cursor_",
    skills_install_path=_cursor_skills_path,  # ~/.cursor/skills/ — Cursor 自己的 skill 目录
    label="cursor",
)


_CURSOR_TOOL_KEYS = (
    "command", "file_path", "path", "pattern", "query", "glob", "url",
    "target_file", "relative_workspace_path",
)

_CURSOR_USER_QUERY_RE = re.compile(
    r"<user_query>\s*(.*?)\s*</user_query>",
    re.DOTALL,
)


def _cursor_visible_user_text(text: str, limit: int = 4000) -> str:
    """Cursor Agent 常把整份 skill 内联在 user 消息前，真正的问话在
    ``<user_query>`` 里。截断前先抽出这段，否则 ``traj search --local``
    只能扫到 skill 正文。
    """
    match = _CURSOR_USER_QUERY_RE.search(text)
    if match:
        query = " ".join(match.group(1).split()).strip()
        if query:
            return query[:limit]
    return text[:limit]


def _cursor_tool_snip(inp: dict[str, Any], limit: int = 160) -> str:
    """把工具入参收成短串，写进 md / timeline，给后面的卡片用。"""
    if not inp:
        return ""
    parts: list[str] = []
    keys = [k for k in _CURSOR_TOOL_KEYS if k in inp and inp[k] not in (None, "")]
    if not keys:
        keys = [k for k in inp if not str(k).startswith("_")][:3]
    for key in keys:
        val = re.sub(r"\s+", " ", str(inp[key]).strip())
        if len(val) > 80:
            val = val[:79] + "…"
        parts.append(f"{key}={val}")
        if len(" ".join(parts)) >= limit:
            break
    text = " ".join(parts)
    return text[:limit]


def _clip_cursor_tool_input(inp: dict[str, Any], max_val: int = 300) -> dict[str, Any]:
    clipped: dict[str, Any] = {}
    for index, (key, value) in enumerate(inp.items()):
        if index >= 8:
            break
        text = str(value)
        clipped[key] = text if len(text) <= max_val else text[:max_val] + "…"
    return clipped


# ─────────────────────────────────────────────────────────────────
# Trajectory adapter
# ─────────────────────────────────────────────────────────────────


def _adapt_cursor_transcripts_jsonl(content: str, metadata: dict) -> tuple[str, dict]:
    """Convert a Cursor agent-transcript JSONL (``~/.cursor/projects/<encoded-cwd>/
    agent-transcripts/<sid>.jsonl``) to markdown + metadata.

    每行格式（实测 + ``scripts/cursor_import.py:_jsonl_to_markdown`` 推断）：

    ```json
    {"role": "user|assistant", "message": {"content": [
        {"type": "text", "text": "..."},
        {"type": "tool_use", "name": "..."},
        ...
    ]}}
    ```

    Cursor 没有显式 ``sessionId`` / ``cwd`` 字段——sid 在文件名，cwd 在父目录名
    （encoded slug）。两者都由上层 ingester 从路径推断后放进 ``metadata`` 传入：
    ``cwd`` 只在 slug 能对照磁盘反解成真实目录时才有。
    """
    timeline: list[dict] = []
    tool_names: list[str] = []
    first_user_query = ""
    t = 0

    for raw_line in content.splitlines():
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        try:
            event = json.loads(raw_line)
        except json.JSONDecodeError:
            continue

        role = event.get("role", "unknown")
        msg = event.get("message") or {}
        parts = msg.get("content") or []
        if not isinstance(parts, list):
            continue

        text_chunks: list[str] = []
        pending_tools: list[dict] = []
        for part in parts:
            if not isinstance(part, dict):
                continue
            ptype = part.get("type")
            if ptype == "text":
                tx = part.get("text") or ""
                if tx:
                    text_chunks.append(str(tx))
            elif ptype == "tool_use":
                name = part.get("name", "tool")
                if name not in tool_names:
                    tool_names.append(name)
                inp = part.get("input") or part.get("arguments") or {}
                if not isinstance(inp, dict):
                    inp = {"value": inp}
                snip = _cursor_tool_snip(inp)
                text_chunks.append(
                    f"[tool_use: {name} {snip}]" if snip else f"[tool_use: {name}]"
                )
                pending_tools.append({
                    "tool": name,
                    "input": _clip_cursor_tool_input(inp),
                })

        body = "\n".join(text_chunks).strip()
        if not body:
            continue

        if role == "user":
            visible = _cursor_visible_user_text(body)
            if not first_user_query:
                first_user_query = visible[:500]
        else:
            visible = body[:2000]

        entry = {
            "t": t, "role": role, "content": visible,
        }
        if pending_tools:
            entry["tools"] = pending_tools
        timeline.append(entry)
        t += 1

    lines: list[str] = ["# Cursor Agent Trajectory", ""]
    if first_user_query:
        lines.append("## Initial Query")
        lines.append("")
        lines.append(first_user_query)
        lines.append("")
    for entry in timeline:
        role = entry["role"]
        if role == "user":
            lines.append("## User")
        elif role == "assistant":
            lines.append("## Assistant")
        else:
            lines.append(f"## {str(role).capitalize()}")
        lines.append("")
        lines.append(entry["content"])
        lines.append("")
    md = "\n".join(lines)

    meta = dict(metadata)
    meta.setdefault("source", "cursor_transcripts_jsonl")
    meta.setdefault("category", "cursor_session")
    meta["timeline"] = timeline
    meta["tool_names"] = tool_names
    meta["total_turns"] = len(timeline)
    if first_user_query:
        meta.setdefault("query", first_user_query)

    return md, meta


# ─────────────────────────────────────────────────────────────────
# Ingest — bridge Cursor agent-transcripts JSONL into xskill traj dir
# ─────────────────────────────────────────────────────────────────


def ingest_cursor_sessions(
    target_traj_dir: Path | str,
    *,
    home_root: Path | str | None = None,
    seen_sessions: Optional[set[str]] = None,
) -> list[dict]:
    """Bridge Cursor agent-transcripts JSONLs into xskill's trajectory directory.

    Scans ``<home_root>/.cursor/projects/<encoded-cwd>/agent-transcripts/``
    for both ``<sid>.jsonl`` and ``<sid>/<sid>.jsonl``, and submits any
    session whose stem is not in ``seen_sessions`` as a new
    trajectory under ``target_traj_dir`` using the
    ``cursor_transcripts_jsonl`` adapter.
    """
    return JsonlIngester(CURSOR_SPEC).scan_and_bridge(
        target_traj_dir=Path(target_traj_dir),
        home_root=Path(home_root) if home_root else None,
        seen_sessions=seen_sessions,
    )
