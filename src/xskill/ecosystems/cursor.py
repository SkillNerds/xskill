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
from pathlib import Path
from typing import Iterable, Optional

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
    """Cursor JSONL 每行只有 ``{role, message}``，**没有 cwd 字段**——cwd 被
    encoded 进父目录名 ``<encoded-cwd>/agent-transcripts/``，content 看不到。

    返回空串让 traj_id 退化到 ``traj_cursor_unknown_<sid8>``。功能上不影响，
    只是 traj_id 里 project 段不可读。如果以后要从 encoded slug 反解 cwd，
    需要扩 spec 协议让 cwd_from_content 也接 path（暂不动）。
    """
    return ""


# ─────────────────────────────────────────────────────────────────
# Ecosystem spec
# ─────────────────────────────────────────────────────────────────

CURSOR_SPEC = EcosystemSpec(
    name="cursor",
    source_kind="jsonl",
    sessions_path=_cursor_projects_path,
    sessions_glob="*/agent-transcripts/*.jsonl",  # <encoded-cwd>/agent-transcripts/<sid>.jsonl
    session_id_from_path=_cursor_session_id_from_path,
    cwd_from_content=_read_cwd_from_cursor_jsonl,
    adapter_format="cursor_transcripts_jsonl",
    traj_id_prefix="traj_cursor_",
    skills_install_path=_cursor_skills_path,  # ~/.cursor/skills/ — Cursor 自己的 skill 目录
    label="cursor",
)


# ─────────────────────────────────────────────────────────────────
# Trajectory adapter
# ─────────────────────────────────────────────────────────────────


def _cursor_text_chunks(parts: object, tool_names: list[str]) -> list[str]:
    if not isinstance(parts, list):
        return []
    text_chunks: list[str] = []
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
            text_chunks.append(f"[tool_use: {name}]")
    return text_chunks


def _cursor_heading(role: object) -> str:
    if role == "user":
        return "## User"
    if role == "assistant":
        return "## Assistant"
    return f"## {str(role).capitalize()}"


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
    （encoded slug，本 adapter 不反解）。所以 meta 里 ``session_id`` / ``cwd``
    都为空，由上层 ingester 用 ``source_jsonl`` 推断（如有需要）。
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
        body = "\n".join(_cursor_text_chunks(msg.get("content") or [], tool_names)).strip()
        if not body:
            continue

        if role == "user" and not first_user_query:
            first_user_query = body[:500]

        timeline.append({
            "t": t, "role": role, "content": body[:2000],
        })
        t += 1

    lines: list[str] = ["# Cursor Agent Trajectory", ""]
    if first_user_query:
        lines.append("## Initial Query")
        lines.append("")
        lines.append(first_user_query)
        lines.append("")
    for entry in timeline:
        lines.append(_cursor_heading(entry["role"]))
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

    Scans ``<home_root>/.cursor/projects/<encoded-cwd>/agent-transcripts/*.jsonl``
    and submits any session whose stem is not in ``seen_sessions`` as a new
    trajectory under ``target_traj_dir`` using the
    ``cursor_transcripts_jsonl`` adapter.
    """
    return JsonlIngester(CURSOR_SPEC).scan_and_bridge(
        target_traj_dir=Path(target_traj_dir),
        home_root=Path(home_root) if home_root else None,
        seen_sessions=seen_sessions,
    )
