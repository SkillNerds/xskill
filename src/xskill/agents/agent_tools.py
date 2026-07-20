"""
agent_tools.py — xskill agent tools and their runtime configuration
═════════════════════════════════════════════════════════════════════

This module owns the Agno tools registered on xskill agents. Runtime dependencies
live in an immutable ``AgentToolContext`` bound through ``ContextVar`` so
concurrent instances do not share project paths or ledgers. ``agent_tool_config``
is only a stateless property facade for existing callers. LLM and embedding
clients are intentionally not stored here; non-tool workflows create those from
config at their own boundary.
"""

from __future__ import annotations

import contextlib
import contextvars
import copy
import json
import logging
import os
import re
import shutil
import subprocess
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, replace
from datetime import date, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any

from agno.tools import tool
from pydantic import BaseModel, ConfigDict, Field

from xskill.skill.frontmatter import (
    parse as fm_parse,
    parse_strict as fm_parse_strict,
    serialize as fm_serialize,
    FrontmatterError,
)
from xskill.utils.proc import windowless_subprocess_kwargs

logger = logging.getLogger("xskill.agent_tools")

_MAPPING_PROXY_TYPE = type(MappingProxyType({}))


def _similarity_score(result: dict) -> float:
    return float(result.get("similarity", 0))


def _freeze_config(value):
    """Copy a config tree into recursively immutable containers."""
    if isinstance(value, _MAPPING_PROXY_TYPE):
        return value
    if isinstance(value, Mapping):
        return MappingProxyType({
            key: _freeze_config(item)
            for key, item in value.items()
        })
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_config(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(_freeze_config(item) for item in value)
    return copy.deepcopy(value)


@dataclass(frozen=True)
class AgentToolContext:
    """Dependencies visible to tools in one task execution context."""

    configured: bool = False
    skill_dir: Path | None = None
    data_dir: Path | None = None
    config: Mapping[str, Any] | None = None
    atom_skill_dir: Path | None = None
    atom_store: Any = None
    default_traj_root: Path | None = None
    spill_root: Path | None = None
    usage_ledger: Any = None
    grep_fallback_warned: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "config",
            _freeze_config(self.config or {}),
        )


_EMPTY_AGENT_TOOL_CONTEXT = AgentToolContext()
_AGENT_TOOL_CONTEXT: contextvars.ContextVar[AgentToolContext] = (
    contextvars.ContextVar(
        "xskill_agent_tool_context",
        default=_EMPTY_AGENT_TOOL_CONTEXT,
    )
)


def create_agent_tool_context(
    *,
    skill_dir=None,
    data_dir=None,
    config=None,
    atom_skill_dir=None,
    atom_store=None,
    default_traj_root=None,
    spill_root=None,
    usage_ledger=None,
) -> AgentToolContext:
    """Create an immutable context without changing the current task."""
    return AgentToolContext(
        configured=True,
        skill_dir=Path(skill_dir) if skill_dir is not None else None,
        data_dir=Path(data_dir) if data_dir is not None else None,
        config=config,
        atom_skill_dir=(
            Path(atom_skill_dir) if atom_skill_dir is not None else None
        ),
        atom_store=atom_store,
        default_traj_root=(
            Path(default_traj_root)
            if default_traj_root is not None
            else None
        ),
        spill_root=Path(spill_root) if spill_root is not None else None,
        usage_ledger=usage_ledger,
    )


def bind_agent_tool_context(
    context: AgentToolContext,
) -> contextvars.Token:
    """Bind a context to the current task/thread and return its reset token."""
    if not isinstance(context, AgentToolContext):
        raise TypeError("context 必须是 AgentToolContext")
    return _AGENT_TOOL_CONTEXT.set(context)


def current_agent_tool_context() -> AgentToolContext:
    """Return the immutable context bound to the current task/thread."""
    return _AGENT_TOOL_CONTEXT.get()


def reset_agent_tool_context(token: contextvars.Token) -> None:
    """Restore the context that preceded ``bind_agent_tool_context``."""
    _AGENT_TOOL_CONTEXT.reset(token)


@contextlib.contextmanager
def use_agent_tool_context(
    context: AgentToolContext,
) -> Iterator[AgentToolContext]:
    """Bind one task context and restore the previous context on every exit."""
    token = bind_agent_tool_context(context)
    try:
        yield context
    finally:
        reset_agent_tool_context(token)


class AgentToolConfig:
    """Stateless compatibility facade over the current task's context."""

    def configure_skill_authoring(
        self, skill_dir, data_dir, config, *, spill_root=None,
        usage_ledger=None,
    ) -> None:
        current = _AGENT_TOOL_CONTEXT.get()
        _AGENT_TOOL_CONTEXT.set(replace(
            current,
            configured=True,
            skill_dir=Path(skill_dir),
            data_dir=Path(data_dir),
            config=config,
            spill_root=(
                Path(spill_root) if spill_root is not None else None
            ),
            usage_ledger=usage_ledger,
        ))

    def configure_atom_task(
        self, *, skill_dir, atom_store, default_traj_root,
        spill_root=None, usage_ledger=None,
    ) -> None:
        current = _AGENT_TOOL_CONTEXT.get()
        _AGENT_TOOL_CONTEXT.set(replace(
            current,
            configured=True,
            atom_skill_dir=Path(skill_dir),
            atom_store=atom_store,
            default_traj_root=Path(default_traj_root),
            spill_root=(
                Path(spill_root) if spill_root is not None else None
            ),
            usage_ledger=usage_ledger,
        ))

    def snapshot(self) -> dict:
        current = _AGENT_TOOL_CONTEXT.get()
        return {
            "skill_dir": current.skill_dir,
            "configured": current.configured,
            "data_dir": current.data_dir,
            "config": current.config,
            "atom_skill_dir": current.atom_skill_dir,
            "atom_store": current.atom_store,
            "default_traj_root": current.default_traj_root,
            "spill_root": current.spill_root,
            "usage_ledger": current.usage_ledger,
            "grep_fallback_warned": current.grep_fallback_warned,
        }

    def restore(self, snapshot: dict) -> None:
        _AGENT_TOOL_CONTEXT.set(create_agent_tool_context(
            skill_dir=snapshot.get("skill_dir"),
            data_dir=snapshot.get("data_dir"),
            config=snapshot.get("config"),
            atom_skill_dir=snapshot.get("atom_skill_dir"),
            atom_store=snapshot.get("atom_store"),
            default_traj_root=snapshot.get("default_traj_root"),
            spill_root=snapshot.get("spill_root"),
            usage_ledger=snapshot.get("usage_ledger"),
        ))
        if not snapshot.get("configured", True):
            current = _AGENT_TOOL_CONTEXT.get()
            _AGENT_TOOL_CONTEXT.set(replace(current, configured=False))
        if snapshot.get("grep_fallback_warned"):
            current = _AGENT_TOOL_CONTEXT.get()
            _AGENT_TOOL_CONTEXT.set(replace(
                current, grep_fallback_warned=True
            ))

    def clear_atom_task(self) -> None:
        current = _AGENT_TOOL_CONTEXT.get()
        _AGENT_TOOL_CONTEXT.set(replace(
            current,
            atom_skill_dir=None,
            atom_store=None,
            default_traj_root=None,
        ))

    @property
    def skill_dir(self) -> Path | None:
        return _AGENT_TOOL_CONTEXT.get().skill_dir

    @property
    def data_dir(self) -> Path | None:
        return _AGENT_TOOL_CONTEXT.get().data_dir

    @property
    def config(self) -> Mapping[str, Any]:
        return _AGENT_TOOL_CONTEXT.get().config

    @property
    def atom_skill_dir(self) -> Path | None:
        return _AGENT_TOOL_CONTEXT.get().atom_skill_dir

    @property
    def atom_store(self):
        return _AGENT_TOOL_CONTEXT.get().atom_store

    @property
    def default_traj_root(self) -> Path | None:
        return _AGENT_TOOL_CONTEXT.get().default_traj_root

    @property
    def spill_root(self) -> Path | None:
        return _AGENT_TOOL_CONTEXT.get().spill_root

    @property
    def usage_ledger(self):
        return _AGENT_TOOL_CONTEXT.get().usage_ledger

    @property
    def grep_fallback_warned(self) -> bool:
        return _AGENT_TOOL_CONTEXT.get().grep_fallback_warned

    @grep_fallback_warned.setter
    def grep_fallback_warned(self, value: bool) -> None:
        current = _AGENT_TOOL_CONTEXT.get()
        _AGENT_TOOL_CONTEXT.set(replace(
            current, grep_fallback_warned=bool(value)
        ))

    @property
    def writable_skill_dir(self) -> Path | None:
        current = _AGENT_TOOL_CONTEXT.get()
        return current.atom_skill_dir or current.skill_dir


agent_tool_config = AgentToolConfig()


def init_atom_task_tool_context(
    *,
    skill_dir,
    atom_store,
    default_traj_root,
    spill_root=None,
    usage_ledger=None,
):
    """Initialize tools that read AtomTask JSON and source trajectory text."""
    agent_tool_config.configure_atom_task(
        skill_dir=skill_dir,
        atom_store=atom_store,
        default_traj_root=default_traj_root,
        spill_root=spill_root,
        usage_ledger=usage_ledger,
    )


def init_skill_authoring_tool_context(
    skill_dir, data_dir, config, *, spill_root=None, usage_ledger=None,
):
    """Initialize general skill-authoring and description optimization tools."""
    agent_tool_config.configure_skill_authoring(
        skill_dir,
        data_dir,
        config,
        spill_root=spill_root,
        usage_ledger=usage_ledger,
    )


def _slugify(name: str) -> str:
    """Normalize a skill name to the slug form used in frontmatter.name."""
    return name.strip().lower().replace("_", "-").replace(" ", "-")


def _is_relative_to(path: Path, root: Path) -> bool:
    """Python 3.9 compatible Path.is_relative_to."""
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


SENSITIVE_FILENAMES = frozenset({
    "config.yaml", "team_client.json", "team_server.json", "team_clients.db",
    "dashboard_secret.json",
})
SENSITIVE_NAME_TOKENS = frozenset({
    "secret", "token", "credential", "key", "password",
})


def _allowed_read_roots() -> list[Path]:
    """探索类工具（read_file / list_files / grep_files）共用的只读根集合。"""
    roots: list[Path] = []
    configured_root = agent_tool_config.skill_dir or agent_tool_config.atom_skill_dir
    if configured_root is not None:
        roots.append(Path(configured_root).parent.resolve())
    elif not current_agent_tool_context().configured:
        from xskill.config import XSKILL_HOME
        roots.append(XSKILL_HOME.resolve())
    spill_root = current_agent_tool_context().spill_root
    if spill_root is not None:
        roots.append(Path(spill_root).resolve())
    return list(dict.fromkeys(roots))


def _is_sensitive_file(path: Path) -> bool:
    """密钥类文件不给 agent 读——skill 正文会分发全团队，蒸进去即泄密。"""
    lower_name = path.name.lower()
    if lower_name in SENSITIVE_FILENAMES:
        return True
    name_tokens = set(re.split(r"[^a-z0-9]+", lower_name))
    return bool(name_tokens & SENSITIVE_NAME_TOKENS)


def _sanitize_frontmatter_dates(fm: dict) -> dict:
    """不让 LLM 写的日期字段污染 frontmatter。
    - created: 必须是合法 ISO date 且 ≤ 今天；否则替换成今天（保留历史 created 优先）
    - last_updated: 一律覆盖成当前时间
    返回被修改过的 fm（同对象）。
    """
    meta = fm.setdefault("metadata", {})
    today = date.today()
    created = str(meta.get("created", "")).strip()
    valid_created = False
    try:
        parsed = date.fromisoformat(created[:10]) if created else None
        if parsed and parsed <= today:
            valid_created = True
    except (ValueError, TypeError):
        pass
    if not valid_created:
        meta["created"] = today.isoformat()
    meta["last_updated"] = datetime.now().isoformat(timespec="seconds")
    return fm


def _read_skill_md(skill_path: Path) -> tuple[dict, str, Path]:
    """Return (frontmatter_dict, body, path_of_SKILL.md). Supports legacy
    lowercase `skill.md` as a fallback read path (writes always go to
    SKILL.md)."""
    upper = skill_path / "SKILL.md"
    lower = skill_path / "skill.md"
    if upper.exists():
        fm, body = fm_parse(upper.read_text(encoding="utf-8"))
        return fm, body, upper
    if lower.exists():
        fm, body = fm_parse(lower.read_text(encoding="utf-8"))
        return fm, body, lower
    return {}, "", upper


# ═══════════════════════════════════════════════════════════════════
# Read tools
# ═══════════════════════════════════════════════════════════════════

@tool(name="search_similar_trajs")
def search_similar_trajs(query: str, top_k: int = 5, filter: str = "all") -> str:
    """
    Search historical trajectories for semantic matches.

    Args:
        query: natural-language description of the trajectory type you want
        top_k: number of results (default 5)
        filter: "all" | "success" | "failure"

    Returns:
        JSON string: list of {traj_id, similarity, meta (summary), md_path, dataset}
    """
    from xskill.utils.search import search as do_search
    data_dir = agent_tool_config.data_dir
    config = agent_tool_config.config

    results = []
    for d in sorted(data_dir.iterdir()):
        if not d.is_dir() or d.name == "raw":
            continue
        index_path = d / "index.pkl"
        if not index_path.exists():
            continue
        try:
            hits = do_search(d, query, top_k=top_k, min_similarity=0.1,
                             success_filter=filter, config=config)
            for h in hits:
                h["dataset"] = d.name
                h.pop("traj_json", None)
            results.extend(hits)
        except Exception as e:
            logger.warning(f"search failed on {d.name}: {e}")

    results.sort(key=_similarity_score, reverse=True)
    results = results[:top_k]
    return json.dumps(results, ensure_ascii=False, indent=2, default=str)


@tool(name="read_file")
def read_file(path: str, offset: int = 1, limit: int = 200) -> str:
    """Read a file under the skill workspace or current instance spill area.

    Use with list_files / grep_files output: they return paths this tool can
    read directly. Instance-owned spill files hold trimmed raw tool results;
    reload them in line windows when placeholders are not enough. Secret-bearing
    files (config.yaml, *token*, *key*, ...) are refused.

    Args:
        path: File path under an allowed read root.
        offset: 1-based start line.
        limit: Number of lines to read from offset.
    """
    try:
        line_offset = int(offset)
        line_limit = int(limit)
    except (TypeError, ValueError):
        return f"error: offset and limit must be integers (offset={offset!r}, limit={limit!r})"
    if line_offset < 1:
        return f"error: offset must be >= 1 (got {line_offset})"
    if line_limit < 1:
        return f"error: limit must be >= 1 (got {line_limit})"

    p = Path(path)
    roots = _allowed_read_roots()
    resolved = p.resolve()
    try:
        allowed = any(_is_relative_to(resolved, root) for root in roots)
    except OSError as e:
        return f"error: path resolution failed ({path}): {e}"
    if not allowed:
        allowed_block = "\n".join(f"- {root}" for root in roots)
        return (
            "error: outside allowed read roots\n"
            f"source_path: {path}\n"
            f"resolved_path: {resolved}\n"
            f"allowed_roots:\n{allowed_block}"
        )
    if _is_sensitive_file(resolved):
        return f"error: sensitive file, not readable by agent ({path})"

    if not resolved.exists():
        return f"error: file not found ({path})"

    try:
        content = resolved.read_text(encoding="utf-8")
        lines = content.splitlines(keepends=True)
        total_lines = len(lines)
        if line_offset > total_lines + 1:
            return (
                "error: offset outside file\n"
                f"source_path: {path}\n"
                f"resolved_path: {resolved}\n"
                f"line_offset: {line_offset}\n"
                f"total_lines: {total_lines}"
            )
        start = line_offset - 1
        selected_lines = lines[start:start + line_limit]
        selected = "".join(selected_lines)
        line_end_exclusive = line_offset + len(selected_lines)
        header = (
            f"source_path: {path}\n"
            f"resolved_path: {resolved}\n"
            f"line_range: [{line_offset}, {line_end_exclusive})\n"
            "--- file content ---\n"
        )
        if len(selected) > 10000:
            return (
                header + selected[:10000]
                + f"\n\n... (truncated, selected length {len(selected)} chars; "
                f"full file length {len(content)} chars)"
            )
        return header + selected
    except Exception as e:
        return f"error: read failed ({e})"


# ═══════════════════════════════════════════════════════════════════
# Write tools
# ═══════════════════════════════════════════════════════════════════

SKILL_MD_STUB = """---
name: {slug}
description: |
  (placeholder — the agent will fill this with a 2-5 sentence router-ready
  description including likely user phrasings and required tools.)
compatibility: |
  (placeholder — required environment, versions, and any NO-GO conditions.)
metadata:
  version: 1
  created: "{today}"
  last_updated: "{today}"
  source_trajs: []
  frozen: false
  use_count: 0
---

# {title}

(Write body here. Use `## <stage-name>` phase-based headers. Inline warnings as
`> ⚠️` blockquotes directly under the step that needs them, citing trajectory
evidence.)
"""


@tool(name="create_skill")
def create_skill(skill_name: str) -> str:
    """
    Scaffold a new skill directory in the v2 layout.

    Creates:
        ./skill/<name>/SKILL.md          (stub frontmatter + placeholder body)
        ./skill/<name>/scripts/.gitkeep
        ./skill/<name>/references/.gitkeep

    Args:
        skill_name: slug (lowercase dashes, e.g. "fix-orm-n-plus-one")

    Returns:
        Status message; the agent should overwrite SKILL.md via write_file.
    """
    skill_dir = agent_tool_config.skill_dir
    slug = _slugify(skill_name)
    target = skill_dir / slug

    if target.exists():
        return f"skill directory already exists: {target}. Use write_file to overwrite SKILL.md."

    target.mkdir(parents=True)
    (target / "scripts").mkdir()
    (target / "references").mkdir()
    (target / "scripts" / ".gitkeep").write_text("", encoding="utf-8")
    (target / "references" / ".gitkeep").write_text("", encoding="utf-8")

    today = date.today().isoformat()
    title = slug.replace("-", " ").capitalize()
    skill_md = SKILL_MD_STUB.format(slug=slug, today=today, title=title)
    (target / "SKILL.md").write_text(skill_md, encoding="utf-8")

    logger.info(f"📁 created skill scaffold: {target}")
    return (f"created: {target}\n"
            f"files: SKILL.md (stub), scripts/.gitkeep, references/.gitkeep\n"
            f"Next: overwrite {target}/SKILL.md with your full v2 content via write_file.")


# ═══════════════════════════════════════════════════════════════════
# Candidate buffer tools (agent-facing)
# ═══════════════════════════════════════════════════════════════════

@tool(name="add_candidate")
def add_candidate(skill_name: str, pattern: str, pattern_type: str,
                  traj_id: str, attach_to: str = "") -> str:
    """
    Add a proposed pattern to the skill's .candidates.yml buffer. If a
    fuzzy-matching pattern already exists, merges the traj_id into its
    supporters list (de-duplicated). Otherwise creates a new candidate.

    Args:
        skill_name: slug of the target skill (must already exist)
        pattern: the pattern text (concrete, evidence-style)
        pattern_type: one of "step" | "warning" | "decision_branch"
        traj_id: the trajectory id (e.g. "traj_0023") contributing this signal
        attach_to: SKILL.md stage-header section to attach to (for warnings
                   and branches). Empty means "end of body".

    Returns:
        Human-readable status including the current supporter count.
    """
    from xskill.skill import candidates as C

    skill_dir = agent_tool_config.skill_dir
    slug = _slugify(skill_name)
    target = skill_dir / slug
    if not target.exists():
        # try non-slug name as fallback
        target = skill_dir / skill_name
    if not target.exists():
        return f"error: skill directory not found ({skill_name})"

    ptype = (pattern_type or "step").strip().lower()
    if ptype not in ("step", "warning", "decision_branch"):
        return (f"error: pattern_type must be one of step|warning|decision_branch "
                f"(got '{pattern_type}')")

    data, was_new = C.add_pattern_candidate(
        target,
        pattern,
        ptype,
        traj_id,
        attach_to=attach_to or None,
    )

    # Look up the current supporter count for this pattern to report back.
    count = 0
    promoted = False
    for c in data.get("candidates", []):
        if C._fuzzy_equal(c.get("pattern", ""), pattern):
            count = len(c.get("supporting_trajs", []) or [])
            promoted = bool(c.get("promoted"))
            break

    verb = "new candidate" if was_new else "merged into existing candidate"
    tail = " [already PROMOTED]" if promoted else ""
    return (f"{verb} for skill '{slug}': '{pattern[:80]}' "
            f"type={ptype} supporters={count}{tail}")


@tool(name="list_candidates")
def list_candidates(skill_name: str) -> str:
    """
    List candidates in the skill's .candidates.yml buffer.

    Returns a compact human-readable listing; the agent should read this
    before calling ``add_candidate`` to avoid duplicate proposals.
    """
    from xskill.skill import candidates as C

    skill_dir = agent_tool_config.skill_dir
    slug = _slugify(skill_name)
    target = skill_dir / slug
    if not target.exists():
        target = skill_dir / skill_name
    if not target.exists():
        return f"error: skill directory not found ({skill_name})"

    data = C.load_candidates(target)
    cands = data.get("candidates", []) or []
    if not cands:
        return f"(no candidates in buffer for '{slug}')"

    lines = [f"candidates for '{slug}' ({len(cands)} total):"]
    for c in cands:
        tag = "[PROMOTED]" if c.get("promoted") else "[PENDING] "
        n = len(c.get("supporting_trajs", []) or [])
        lines.append(
            f"  {tag} ({n}) [{c.get('type','step')}] "
            f"{(c.get('pattern','') or '')[:100]}"
        )
    return "\n".join(lines)


@tool(name="write_file")
def write_file(path: str, content: str) -> str:
    """Write or overwrite a file under ./skill/ only.

    v2 行为：只做路径安全 + frontmatter 日期消毒。旧 v1 ``source_trajs ≥ 3``
    gate 和 ``N/M 条轨迹`` warning 消毒已删——v2 用 ``source_atoms`` 引用 atom
    而非 traj，且质量保障靠 candidates buffer 累计 weightscore ≥ 10 的硬门槛，
    不需要 SKILL.md 写入端再卡一道。
    """
    p = Path(path)
    if agent_tool_config.atom_skill_dir is not None:
        skill_dir = agent_tool_config.atom_skill_dir
    else:
        skill_dir = agent_tool_config.skill_dir

    try:
        resolved = p.resolve()
        resolved.relative_to(skill_dir.resolve())
    except ValueError:
        return f"error: writes restricted to ./skill/ (tried: {path})"

    # 拒写 .git/ —— agent LLM 撞到 git 错误时会试图"自己修 git"，把可恢复
    # 的 index race 变成永久 repo 损坏（实跑遇到 3 个 skill 仓被 LLM 写进
    # .git/HEAD / .git/refs / .git/config 而毁掉）。.git 严格归 git 自己管。
    if ".git" in resolved.parts:
        return f"error: writes into .git/ are forbidden (tried: {path})"

    # 写 SKILL.md：先做 frontmatter 写后校验（漏拦=静默放行坏 skill），
    # 非法**不写盘**，把富误差返回给 agent 让它当场改重写；合法再消毒日期 +
    # 重序列化写入。校验逻辑见 frontmatter.parse_strict（必填 name/description、
    # description 必须非空字符串、body 非空）。
    if p.name == "SKILL.md":
        try:
            fm, body = fm_parse_strict(content)
        except FrontmatterError as e:
            logger.warning(f"SKILL.md frontmatter 非法，拒写: {e}")
            return (
                f"error: SKILL.md frontmatter 非法，未写盘 —— {e}\n"
                "请修正 frontmatter 后重新调用 write_file。常见原因：多行 "
                "description 没用块标量 `|` 或引号、缺 name/description、正文为空。"
            )
        _sanitize_frontmatter_dates(fm)
        content = fm_serialize(fm, body)

    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    logger.info(f"✏️  wrote: {p} ({len(content)} bytes)")
    return f"wrote: {p} ({len(content)} chars)"


# ═══════════════════════════════════════════════════════════════════
# Frontmatter metadata update (post-eval bookkeeping)
# ═══════════════════════════════════════════════════════════════════

SUMMARY_PROMPT = """Summarize the following SKILL.md in exactly 2 sentences
(max 50 words total). Focus on what problem it solves and the core decision
point. No preamble. Output the 2 sentences only.

---
{skill_md}
---"""


@tool(name="update_frontmatter_metadata")
def update_frontmatter_metadata(skill_name: str, source_trajs: list[str] | None = None) -> str:
    """
    Update frontmatter.metadata on a skill's SKILL.md:
      - bump version if source_trajs changed
      - union-append new source_trajs
      - set last_updated = now
      - refresh metadata.summary via LLM (for better vector embeddings)
      - delete any legacy .abstract file lying around

    Body is preserved byte-exact.

    Returns:
        JSON blob of the new metadata, or an error message.
    """
    skill_dir = agent_tool_config.skill_dir
    slug = _slugify(skill_name)
    target = skill_dir / skill_name
    if not target.exists():
        # try slug variant
        target = skill_dir / slug
    if not target.exists():
        return f"error: skill directory not found ({skill_name})"

    fm, body, path = _read_skill_md(target)
    meta = fm.setdefault("metadata", {})

    # source_trajs union
    existing_trajs = list(meta.get("source_trajs") or [])
    new_trajs = list(source_trajs or [])
    changed_trajs = False
    for t in new_trajs:
        if t not in existing_trajs:
            existing_trajs.append(t)
            changed_trajs = True
    meta["source_trajs"] = existing_trajs

    # version bump only if source_trajs actually changed
    if changed_trajs:
        meta["version"] = int(meta.get("version", 0)) + 1

    _sanitize_frontmatter_dates(fm)  # 兜底：覆盖未来日期 / 不合法 created

    # LLM-generated 2-sentence summary (for embeddings)
    from xskill.utils.llm import create_llm_client
    llm_client = create_llm_client(
        agent_tool_config.config,
        usage_ledger=agent_tool_config.usage_ledger,
    )
    if llm_client:
        skill_text = (fm.get("description", "") + "\n\n" + body)[:4000]
        try:
            summary = llm_client.chat(SUMMARY_PROMPT.format(skill_md=skill_text)).strip()
            if summary:
                meta["summary"] = summary[:400]
        except Exception as e:
            logger.warning(f"summary generation failed for {skill_name}: {e}")

    # write back
    new_text = fm_serialize(fm, body)
    # Always land in SKILL.md (uppercase). If read came from legacy skill.md,
    # migrate-on-touch.
    upper = target / "SKILL.md"
    upper.write_text(new_text, encoding="utf-8")
    if path.name == "skill.md" and path.exists():
        try:
            path.unlink()
            logger.info(f"removed legacy {path}")
        except Exception:
            pass

    # delete legacy .abstract if present
    old_abstract = target / ".abstract"
    if old_abstract.exists():
        try:
            old_abstract.unlink()
            logger.info(f"removed legacy .abstract for {skill_name}")
        except Exception:
            pass

    logger.info(f"📋 frontmatter updated: {upper} (v{meta.get('version')})")
    return json.dumps(meta, ensure_ascii=False, indent=2, default=str)


# ═══════════════════════════════════════════════════════════════════
# AtomTask-era tools (v2) — consumed by TaskClusterAgent / SkillEditAgent
# ═══════════════════════════════════════════════════════════════════

@tool(name="atom_task_read")
def atom_task_read(atom_id: str) -> str:
    """读一个 AtomTask 的完整 JSON。

    用于 cluster / edit agent 在决定归类前查看 atom 的 intent / summary /
    raw_segment / used_skills。
    """
    store = agent_tool_config.atom_store
    if store is None:
        return "error: atom task tool context not initialized"
    try:
        return store.load(atom_id).to_json()
    except FileNotFoundError as e:
        return f"error: {e}"


@tool(name="read_traj")
def read_traj(traj_id: str, offset_start: int, offset_end: int) -> str:
    """按**行号**读 traj.md 片段。

    用法：agent 看了 atom 摘要后想确认细节时，传 atom 的 offset_start /
    offset_end（都是 1-based 行号，半开区间 ``[start, end)``）回来取原文。
    校验区间合法（``offset_end > offset_start`` 且区间在文件行数内），
    违反直接返回 error。
    """
    traj_root = agent_tool_config.default_traj_root
    if traj_root is None:
        return "error: atom task tool context not initialized"
    # team-CS 多 store：traj.md 落在 atom 所属 client 的 watch_dir 下，不一定是
    # 绑定的 traj_root。store 若支持 traj_root_for（MultiAtomTaskStore），按
    # traj_id 跨所有 root 解析；解析不到再退回绑定 traj_root（单 store 行为不变）。
    store = agent_tool_config.atom_store
    resolver = getattr(store, "traj_root_for", None)
    if callable(resolver):
        try:
            resolved = resolver(traj_id)
        except FileNotFoundError as e:
            return f"error: {e}"
        if resolved is not None:
            traj_root = resolved
    p = traj_root / f"{traj_id}.md"
    if not p.is_file():
        return f"error: traj file not found: {p}"
    if offset_end <= offset_start:
        return f"error: offset_end ({offset_end}) must be > offset_start ({offset_start})"
    lines = p.read_text(encoding="utf-8").splitlines(keepends=True)
    total = len(lines)
    # offset_end 是半开上界（行号），末 atom 可达 total + 1
    if offset_start < 1 or offset_end > total + 1:
        return (
            f"error: line range [{offset_start}..{offset_end}) outside file "
            f"line count {total}"
        )
    return "".join(lines[offset_start - 1:offset_end - 1])


@tool(name="new_skill_folder")
def new_skill_folder(skill_name: str, description: str) -> str:
    """v2: 创建 skill 目录 → git init → checkout baby 分支 → 首次 commit
    （含 stub SKILL.md + .gitignore）。

    description 必填，落到 stub SKILL.md 的 frontmatter 中。后续：
    - 路由表（``build_skill_catalog_block``）从 SKILL.md frontmatter 取 desc
      展示
    - state 由 git 分支决定（baby/main/staging），不再单写 .meta.yml
    - 后续 SkillEditAgent 触发时拿到 candidates 来填正文，调
      ``commit_baby_to_main`` graduate 到 main
    """
    skill_dir = agent_tool_config.atom_skill_dir
    if skill_dir is None:
        return "error: atom task tool context not initialized"
    desc = (description or "").strip()
    if not desc:
        return ("error: description 必填——简述这个 skill 服务于什么类型的 atom "
                "（2-3 句中文，让后续 cluster agent 能判断同类）")
    slug = _slugify(skill_name)
    target = skill_dir / slug
    if target.exists():
        return f"already exists: {target}"
    # 初始化 git + baby 分支 + stub SKILL.md
    from xskill.skill.git import init_skill_repo_on_baby
    init_skill_repo_on_baby(str(target), name=slug, description=desc)
    return f"created on baby branch: {target}  desc={desc[:60]!r}"


@tool(name="skill_read")
def skill_read(skill_name: str) -> str:
    """读 skill 的 SKILL.md 正文 + 目录内其余文件树（路径可直接喂 read_file）。"""
    skill_dir = agent_tool_config.atom_skill_dir
    if skill_dir is None:
        return "error: atom task tool context not initialized"
    slug = _slugify(skill_name)
    skill_path = (skill_dir / slug).resolve()
    header = f"skill_dir: {skill_path}"
    try:
        from xskill.skill.git import current_branch
        header += f"   (branch: {current_branch(str(skill_path))})"
    except Exception:
        pass
    markdown_path = skill_path / "SKILL.md"
    if markdown_path.is_file():
        markdown_text = markdown_path.read_text(encoding="utf-8")
        body = (f"--- SKILL.md ({len(markdown_text.splitlines())} lines) ---\n"
                f"{markdown_text}")
    else:
        body = f"(skill {slug} has no SKILL.md yet — only candidates buffer)"
    tree_lines: list[str] = []
    if skill_path.is_dir():
        for current_dir, dir_names, file_names in os.walk(skill_path):
            walk_depth = len(Path(current_dir).relative_to(skill_path).parts)
            # 剪枝进不去 .git / 第 4 层以下，避免白遍历上千 git 对象。
            dir_names[:] = sorted(
                name for name in dir_names if name != ".git" and walk_depth < 3
            )
            for file_name in sorted(file_names):
                file_path = Path(current_dir) / file_name
                relative_path = file_path.relative_to(skill_path)
                if relative_path.as_posix() == "SKILL.md":
                    continue
                size_kb = file_path.stat().st_size / 1024
                annotation = ("  (用 list_candidates 读)"
                              if relative_path.name == "candidates.yml" else "")
                tree_lines.append(
                    f"{relative_path.as_posix()}  ({size_kb:.1f} KB){annotation}",
                )
        if len(tree_lines) > 100:
            tree_lines = tree_lines[:100] + ["(+more, use list_files)"]
    if not tree_lines:
        return f"{header}\n{body}"
    files_block = "\n".join(tree_lines)
    return (f"{header}\n{body}\n"
            f"--- other files（相对 {skill_path}；用 read_file(绝对路径) 精读）---\n"
            f"{files_block}")


@tool(name="add_task_to_skill")
def add_task_to_skill(skill_name: str, atom_id: str, weightscore: int) -> str:
    """v2.1: 把 atom 加进 skill 的 candidates buffer。

    同 atom 重复 add 时**覆盖**（不累加，cluster 可改主意）。返回末尾附该
    atom 的 weightscore + buffer 总分 / 10，让 agent 看到"还差多少到阈值"。
    """
    from xskill.skill import candidates as C
    skill_dir = agent_tool_config.atom_skill_dir
    if skill_dir is None:
        return "error: atom task tool context not initialized"
    slug = _slugify(skill_name)
    target = skill_dir / slug
    if not target.is_dir():
        return f"error: skill {slug} not found; call new_skill_folder first"
    try:
        weightscore_value = int(weightscore)
    except (TypeError, ValueError):
        return f"error: weightscore must be int 1..10 (got {weightscore!r})"
    if not (1 <= weightscore_value <= 10):
        return (
            "error: weightscore must be 1..10 "
            f"(got {weightscore_value})"
        )
    new_flags, buffer_total = C.add_atom_contributions(
        target,
        [(atom_id, weightscore_value, "")],
    )
    was_new = new_flags[0]
    verb = "new" if was_new else "overwrite"
    return (
        f"{verb}: skill={slug} atom={atom_id} "
        f"weightscore={weightscore_value} "
        f"buffer_total={buffer_total}/10"
    )


class CandidateTaskInput(BaseModel):
    """One atom contribution in the batch candidate tool schema."""

    model_config = ConfigDict(extra="forbid")

    atom_id: str = Field(
        min_length=1,
        description="Existing AtomTask identifier from the current batch.",
    )
    weightscore: int = Field(
        ge=1,
        le=10,
        description="Candidate relevance score from 1 through 10.",
    )
    note: str = Field(
        default="",
        description="Optional short routing reason.",
    )


@tool(name="add_tasks_to_skill")
def add_tasks_to_skill(
    skill_name: str,
    tasks: list[CandidateTaskInput],
) -> str:
    """Batch multiple atoms into one skill with one candidates file write.

    Each item requires ``atom_id`` and ``weightscore``; ``note`` is optional.
    Use this instead of repeated ``add_task_to_skill`` calls when several atoms
    in the current cluster batch belong to the same skill.
    """
    from xskill.skill import candidates as C

    skill_dir = agent_tool_config.atom_skill_dir
    if skill_dir is None:
        return "error: atom task tool context not initialized"
    slug = _slugify(skill_name)
    target = skill_dir / slug
    if not target.is_dir():
        return f"error: skill {slug} not found; call new_skill_folder first"
    if not isinstance(tasks, list) or not tasks:
        return "error: tasks must be a non-empty list"

    contributions: list[tuple[str, int, str]] = []
    seen_atom_ids: set[str] = set()
    for task in tasks:
        if isinstance(task, CandidateTaskInput):
            task_data = task.model_dump()
        elif isinstance(task, dict):
            task_data = task
        else:
            return "error: each task must be an object"
        atom_id = task_data.get("atom_id")
        if not isinstance(atom_id, str) or not atom_id.strip():
            return "error: each task.atom_id must be a non-empty string"
        if atom_id in seen_atom_ids:
            return f"error: duplicate atom_id in tasks ({atom_id})"
        seen_atom_ids.add(atom_id)
        weightscore = task_data.get("weightscore")
        try:
            weightscore_value = int(weightscore)
        except (TypeError, ValueError):
            return (
                "error: each task.weightscore must be int 1..10 "
                f"(got {weightscore!r})"
            )
        if not (1 <= weightscore_value <= 10):
            return (
                "error: each task.weightscore must be 1..10 "
                f"(got {weightscore_value})"
            )
        note = task_data.get("note", "")
        if note is None:
            note = ""
        if not isinstance(note, str):
            return "error: each task.note must be a string"
        contributions.append((atom_id, weightscore_value, note))

    new_flags, buffer_total = C.add_atom_contributions(
        target,
        contributions,
    )
    new_count = sum(new_flags)
    overwrite_count = len(new_flags) - new_count
    return (
        f"batched: skill={slug} atoms={len(new_flags)} "
        f"new={new_count} overwrite={overwrite_count} "
        f"buffer_total={buffer_total}/10"
    )


@tool(name="score_task")
def score_task(atom_id: str, score: int) -> str:
    """覆盖 AtomTask 的 ux_score（手动修正 / 灰度链路使用）。"""
    store = agent_tool_config.atom_store
    if store is None:
        return "error: atom task tool context not initialized"
    try:
        sc = int(score)
    except (TypeError, ValueError):
        return f"error: score must be int 1..10 (got {score!r})"
    if not (1 <= sc <= 10):
        return f"error: score must be 1..10 (got {sc})"
    try:
        a = store.load(atom_id)
    except FileNotFoundError as e:
        return f"error: {e}"
    a.ux_score = sc
    store.save(a)
    return f"scored: {atom_id} → {sc}"


@tool(name="add_task")
def add_task(
    atom_id: str, *, traj_id: str, offset_start: int, offset_end: int,
    intent: str, summary: str, tags: list, used_skills: list,
    ux_score: int | None = None,
) -> str:
    """手动创建一个 AtomTask（offline 脚本 / agent 合成 atom 用）。

    生产路径走 TaskAgent；这个工具是给需要补轨的 agent / 脚本兜底。
    """
    from xskill.pipeline.atom import AtomTask
    store = agent_tool_config.atom_store
    if store is None:
        return "error: atom task tool context not initialized"
    atom = AtomTask(
        atom_id=atom_id, traj_id=traj_id,
        offset_start=int(offset_start), offset_end=int(offset_end),
        intent=intent, summary=summary,
        tags=list(tags or []), used_skills=list(used_skills or []),
        ux_score=ux_score,
        pre_atom_id=None, post_atom_id=None,
        context_prefix="", raw_segment="",
    )
    store.save(atom)
    return f"added: {atom_id}"


def make_task_agent_tools(
    *,
    submitted: list[dict],
    valid_lines,
    resume_line: int,
    total_lines: int,
    all_lines: list[str],
    user_msg: str,
    not_fit_reasons: list[str] | None = None,
):
    """Create TaskAgent run-scoped tools bound to one trajectory."""
    valid = set(valid_lines)
    ordered_valid = sorted(valid_lines)

    @tool(name="submit_atom")
    def submit_atom(start_line: int, intent: str, summary: str,
                    tags: list | None = None,
                    used_skills: list | None = None,
                    ux_score: int | None = None) -> str:
        """提交一个新 AtomTask（提交即校验,不合法返 error 让你自改）。

        Args:
            start_line: 本 atom 起始行号,必须是真实 ## User 行。
            intent: ≤40 字目标。
            summary: ≤200 字复盘。
            tags: 3-5 个小写下划线标签。
            used_skills: agent 实际触发的 skill 名列表,没有传 []。
            ux_score: 1~10 整数。
        """
        if not_fit_reasons:
            return (
                "error: 已调用 mark_not_fit，不能再调用 submit_atom；"
                "如果轨迹相关，请不要调用 mark_not_fit"
            )
        try:
            sl = int(start_line)
        except (TypeError, ValueError):
            return f"error: start_line 必须是整数 (got {start_line!r})"
        if sl not in valid:
            return (f"error: start_line {sl} 不是可切的 ## User 回合 "
                    f"(合法行号: {ordered_valid})")
        if sl < resume_line:
            return (f"error: start_line {sl} < 续接点 {resume_line}；"
                    "只能拆续接点之后的新增内容")
        if submitted and sl <= submitted[-1]["start_line"]:
            return (f"error: start_line 必须严格大于上一条 "
                    f"({submitted[-1]['start_line']})，本次 {sl}")
        if not (intent or "").strip() or not (summary or "").strip():
            return "error: intent 和 summary 必填"
        submitted.append({
            "start_line": sl,
            "intent": intent.strip(),
            "summary": summary.strip(),
            "tags": [str(t).strip() for t in (tags or []) if str(t).strip()],
            "used_skills": [str(s).strip() for s in (used_skills or [])
                            if str(s).strip()],
            "ux_score": ux_score if isinstance(ux_score, int)
            and 1 <= ux_score <= 10 else None,
        })
        return f"ok: 已记录 atom #{len(submitted)} (start_line={sl})"

    @tool(name="mark_not_fit")
    def mark_not_fit(reason: str) -> str:
        """标记整条轨迹不符合配置的 interests，并结束拆分。

        Args:
            reason: 简短说明为什么整条轨迹与 interests 无关。
        """
        if not_fit_reasons is None:
            return "error: 当前未启用 interests 过滤"
        if submitted:
            return (
                "error: 已经提交过 submit_atom，不能再调用 mark_not_fit；"
                "部分相关的轨迹应继续正常拆分"
            )
        normalized_reason = str(reason or "").strip()
        if not normalized_reason:
            normalized_reason = "trajectory does not match configured interests"
        not_fit_reasons.append(normalized_reason)
        return f"ok: not_fit 已记录 ({normalized_reason})"

    @tool(name="look")
    def look(line: int, before: int = 40, after: int = 20) -> str:
        """读轨迹某行附近的原文（含向前看,判新意图 vs 追问的主力）。

        Args:
            line: 中心行号（1-based）。
            before: 向前看多少行（默认 40）。
            after: 向后看多少行（默认 20）。
        """
        try:
            ctr = int(line)
            bef = max(0, int(before))
            aft = max(0, int(after))
        except (TypeError, ValueError):
            return "error: line/before/after 必须是整数"
        lo = max(1, ctr - bef)
        hi = min(total_lines, ctr + aft)
        out = []
        for ln in range(lo, hi + 1):
            out.append(f"{ln}: {all_lines[ln - 1].rstrip(chr(10))}")
        return "\n".join(out) or "(empty range)"

    @tool(name="context_budget")
    def context_budget() -> str:
        """返回当前上下文 token 预算：已用 / 上限 / 剩余。"""
        from xskill.agents.context_budget import (
            get_used_tokens, get_max_context, CHARS_PER_TOKEN)
        used = get_used_tokens()
        if used <= 0:
            used = len(user_msg) // CHARS_PER_TOKEN
        cap = get_max_context()
        return json.dumps({
            "used_tokens": used,
            "max_tokens": cap,
            "remaining_tokens": max(0, cap - used),
        }, ensure_ascii=False)

    @tool(name="my_atoms")
    def my_atoms() -> str:
        """返回本轮已提交 atom 的行号区间（自查进度/覆盖）。"""
        if not submitted:
            return "(本轮尚未提交任何 atom)"
        starts = [s["start_line"] for s in submitted]
        spans = []
        for i, st in enumerate(starts):
            end = starts[i + 1] if i + 1 < len(starts) else total_lines + 1
            spans.append(f"[{st},{end})")
        return " ".join(spans)

    tools = [look, submit_atom, context_budget, my_atoms]
    if not_fit_reasons is not None:
        tools.append(mark_not_fit)
    return tools


@tool(name="list_files")
def list_files(path: str) -> str:
    """列目录下的文件 / 子目录，返回可直接传给 read_file 的完整路径。

    可列 skill 仓、~/.xskill、/tmp spill 三个只读根内的任意目录——摸清 skill
    已有文件、轨迹 / atom 数据布局都用它。越界返回 error。
    """
    target_directory = Path(path).resolve()
    roots = _allowed_read_roots()
    if not any(_is_relative_to(target_directory, root) for root in roots):
        allowed_block = ", ".join(str(root) for root in roots)
        return f"error: list_files restricted to {allowed_block} (tried: {path})"
    if not target_directory.is_dir():
        return f"error: not a directory: {path}"
    entries = sorted(target_directory.iterdir())
    if not entries:
        return "(empty)"
    return "\n".join(
        f"{'[dir] ' if e.is_dir() else '[file] '}{e.resolve()}{'/' if e.is_dir() else ''}"
        for e in entries
    )


@tool(name="grep_files")
def grep_files(pattern: str, path: str = "", glob: str = "",
               max_results: int = 100) -> str:
    """在允许的只读根内全文检索，返回「文件:行号:内容」，路径可直接喂 read_file。

    Args:
        pattern: 正则表达式（ripgrep / grep -E 语法）。
        path: 检索根目录，缺省为 skill 仓所在根；须在允许的只读根内。
        glob: 可选文件名过滤，如 "*.md"。
        max_results: 命中行数上限（1-500）。
    """
    max_results = max(1, min(int(max_results), 500))
    roots = _allowed_read_roots()
    search_root = (Path(path) if path else roots[0]).resolve()
    if not any(_is_relative_to(search_root, root) for root in roots):
        allowed_block = ", ".join(str(root) for root in roots)
        return f"error: grep_files restricted to {allowed_block} (tried: {path})"
    if not search_root.exists():
        return f"error: path not found ({path})"

    if shutil.which("rg"):
        engine = "rg"
        command = ["rg", "-n", "--no-heading", "--color", "never", "--smart-case"]
        if glob:
            command += ["--glob", glob]
        command += ["-e", pattern, str(search_root)]
    elif shutil.which("grep"):
        engine = "grep (ripgrep 不可用，已降级；建议安装 rg 提速)"
        command = ["grep", "-rnIE"]
        if glob:
            command += [f"--include={glob}"]
        command += ["-e", pattern, str(search_root)]
    else:
        engine = "python (ripgrep/grep 均不可用，已降级纯扫描)"
        command = None
    if command is None or engine != "rg":
        if not agent_tool_config.grep_fallback_warned:
            agent_tool_config.grep_fallback_warned = True
            logger.warning("grep_files: ripgrep 不可用，降级为 %s",
                           "grep" if command else "python 纯扫描")

    if command is not None:
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
                **windowless_subprocess_kwargs(),
            )
        except subprocess.TimeoutExpired:
            return "error: grep timed out after 30s — narrow path/glob"
        # rg/grep 退出码：0=命中，1=无命中，≥2=真错误
        if completed.returncode > 1:
            return f"error: {engine} failed: {completed.stderr.strip()[:500]}"
        output_lines = completed.stdout.splitlines()
    else:
        try:
            compiled_pattern = re.compile(pattern)
        except re.error as regex_error:
            return f"error: invalid pattern: {regex_error}"
        output_lines = []
        scanned_count = 0
        for file_path in search_root.rglob(glob or "*"):
            if not file_path.is_file() or ".git" in file_path.parts:
                continue
            scanned_count += 1
            if scanned_count > 2000 or len(output_lines) >= max_results:
                break
            try:
                file_text = file_path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            for line_number, line_text in enumerate(file_text.splitlines(), 1):
                if compiled_pattern.search(line_text):
                    output_lines.append(f"{file_path}:{line_number}:{line_text}")

    hit_line_pattern = re.compile(r"^(.*?):(\d+):")
    filtered_lines: list[str] = []
    for output_line in output_lines:
        hit_match = hit_line_pattern.match(output_line)
        # resolve() 后再判敏感：防符号链接用无害文件名包装密钥文件绕过过滤。
        if hit_match and _is_sensitive_file(Path(hit_match.group(1)).resolve()):
            continue
        filtered_lines.append(output_line)
        if len(filtered_lines) >= max_results:
            break
    header = f"engine: {engine}\nroot: {search_root}\n"
    if not filtered_lines:
        return header + f"(no matches for {pattern!r})"
    body = "\n".join(filtered_lines)
    if len(body) > 10000:
        body = body[:10000] + "\n... (truncated — narrow pattern/glob or lower max_results)"
    return header + body


# ═══════════════════════════════════════════════════════════════════
# SkillEditAgent 专用 commit 工具（不要给 ClusterAgent）
# ═══════════════════════════════════════════════════════════════════

def _run_description_optimization(target: Path, slug: str) -> None:
    """commit 前跑 description 触发优化（D1：硬编码进 workflow，不做 agent tool）。

    gating on ``config.skill_opt.enabled``；任何失败只 log，绝不阻断 commit
    （退回 agent 写的 description 继续提交）。LLM/embed 客户端在这个确定性
    workflow 内从 config 创建，不从 agent tool context 借对象。
    """
    from xskill.config import get_config
    current_context = current_agent_tool_context()
    config = (
        current_context.config
        if current_context.configured
        else get_config()
    )
    if not (config.get("skill_opt", {}) or {}).get("enabled", True):
        return
    from xskill.utils.llm import create_embed_client, create_llm_client
    try:
        import httpx
        client_init_errors = (ValueError, RuntimeError, OSError, httpx.HTTPError)
    except ImportError:
        client_init_errors = (ValueError, RuntimeError, OSError)
    try:
        llm_client = create_llm_client(
            config, usage_ledger=agent_tool_config.usage_ledger
        )
        embed_client = create_embed_client(
            config, usage_ledger=agent_tool_config.usage_ledger
        )
    except client_init_errors as error:
        logger.warning(
            "skip description_opt: client init failed for %s: %s", slug, error,
        )
        return
    if llm_client is None or embed_client is None:
        logger.warning(
            "skip description_opt: llm/embed client unavailable (%s)", slug,
        )
        return
    try:
        from xskill.agents.agno_factory import make_default_factory
        from xskill.skill.description_opt import optimize_description
        skill_root = agent_tool_config.atom_skill_dir
        optimize_description(
            target, llm=llm_client, config=config,
            agno_agent_factory=make_default_factory(
                config,
                usage_ledger=agent_tool_config.usage_ledger,
                spill_root=agent_tool_config.spill_root,
            ),
            embed_client=embed_client, skill_root=skill_root,
        )
    except Exception:
        logger.exception(
            "description_opt 失败（不阻断 commit，沿用 agent 写的 desc）: %s", slug,
        )


@tool(name="commit_baby_to_main")
def commit_baby_to_main(skill_name: str, message: str) -> str:
    """SkillEditAgent 首次为某 skill 出版本时调用。

    前提：该 skill 当前在 baby 分支（cluster 创建后未 graduate）。
    行为：git add . + commit + git branch -m baby main → 该 skill 第一次
    有 main 版本。**之后** SkillEditAgent 再触发只能调 commit_to_staging。

    Args:
        skill_name: 目标 skill 的 slug（如 ``django-fix``）
        message: commit message，应该写明本次基于哪些 atom_id

    Returns:
        成功："graduated baby → main: <skill_name>"
        失败："error: ..."
    """
    from xskill.skill.git import commit_baby_to_main_branch
    skill_dir = agent_tool_config.atom_skill_dir
    if skill_dir is None:
        return "error: atom task tool context not initialized"
    slug = _slugify(skill_name)
    target = skill_dir / slug
    if not target.is_dir():
        return f"error: skill {slug} not found"
    if not (target / ".git").is_dir():
        return f"error: skill {slug} 没 git 仓库（new_skill_folder 出问题？）"
    msg = (message or "").strip()
    if not msg:
        return "error: commit message 必填"
    # commit 前先跑 description 触发优化（best desc 写回 frontmatter），优化产物
    # 随 add . 一起进 commit。失败只 log，不阻断 commit。
    _run_description_optimization(target, slug)
    ok = commit_baby_to_main_branch(str(target), msg)
    if not ok:
        return "error: commit_baby_to_main 失败（不在 baby 分支？看 daemon 日志）"
    return f"graduated baby → main: {slug}"


@tool(name="commit_to_staging")
def commit_to_staging(skill_name: str, message: str) -> str:
    """SkillEditAgent 在 skill 已有 main 时调用——产出灰度候选。

    前提：该 skill 当前在 main 且不存在 staging。
    行为：从 main 切 staging 分支 + add . + commit + 物化到
    ``<skill_dir>/../.canary/<name>/`` 让 install_to_claude_code(side='staging')
    能装到。

    Args:
        skill_name: 目标 skill 的 slug
        message: commit message

    Returns:
        成功："committed to staging: <skill_name>"
        失败："error: ..."
    """
    from xskill.skill.git import commit_to_staging_branch
    skill_dir = agent_tool_config.atom_skill_dir
    if skill_dir is None:
        return "error: atom task tool context not initialized"
    slug = _slugify(skill_name)
    target = skill_dir / slug
    if not target.is_dir():
        return f"error: skill {slug} not found"
    if not (target / ".git").is_dir():
        return f"error: skill {slug} 没 git 仓库"
    msg = (message or "").strip()
    if not msg:
        return "error: commit message 必填"
    # commit 前先跑 description 触发优化（best desc 写回 frontmatter）。失败只
    # log，不阻断 commit。
    _run_description_optimization(target, slug)
    ok = commit_to_staging_branch(str(target), msg)
    if not ok:
        return ("error: commit_to_staging 失败"
                "（不在 main 分支 / staging 已存在 / commit 出错——看日志）")
    return f"committed to staging: {slug}"


@tool(name="commit_update_main")
def commit_update_main(skill_name: str, message: str) -> str:
    """SkillEditAgent jam-merge 场景专用：把更新直接提交回 main。

    前提：该 skill 当前在 main 分支。行为：跑 description 触发优化，然后
    ``git add .`` + commit；不开 staging、不物化 canary。
    """
    from xskill.skill.git import commit_update_main_branch

    skill_dir = agent_tool_config.atom_skill_dir
    if skill_dir is None:
        return "error: atom task tool context not initialized"
    slug = _slugify(skill_name)
    target = skill_dir / slug
    if not target.is_dir():
        return f"error: skill {slug} not found"
    if not (target / ".git").is_dir():
        return f"error: skill {slug} 没 git 仓库"
    msg = (message or "").strip()
    if not msg:
        return "error: commit message 必填"
    _run_description_optimization(target, slug)
    ok = commit_update_main_branch(str(target), msg)
    if not ok:
        return "error: commit_update_main 失败（不在 main 分支 / 无改动可提交——看日志）"
    return f"updated on main: {slug}"


@tool(name="absorb_user_edit_to_main")
def absorb_user_edit_to_main(skill_name: str, message: str) -> str:
    """UserEditAbsorbAgent 专用：把用户手改吸收为 main 分支一次 commit。

    无论当前在 baby / main / staging：
      - baby: rename baby→main 后 commit
      - main: 直接 commit
      - 同时存在 staging: 删除 staging (用户手改优先级压过灰度候选)
    """
    from xskill.skill.git import (
        run_git, current_branch, commit_changes,
    )
    skill_dir = agent_tool_config.atom_skill_dir
    if skill_dir is None:
        return "error: atom task tool context not initialized"
    slug = _slugify(skill_name)
    target = skill_dir / slug
    if not target.is_dir() or not (target / ".git").is_dir():
        return f"error: skill {slug} 没 git 仓库"
    msg = (message or "").strip()
    if not msg:
        return "error: commit message 必填"
    # 确保 message 含 "absorb user edit" 标记便于回流检测
    if "absorb user edit" not in msg.lower():
        msg = f"absorb user edit: {msg}"

    cur = current_branch(str(target))
    cwd = str(target)
    if cur == "baby":
        # baby 阶段被用户改了——直接 graduate + commit
        run_git(["add", "-A"], cwd=cwd)
        run_git(["commit", "-m", msg], cwd=cwd)
        run_git(["branch", "-m", "baby", "main"], cwd=cwd)
        result = f"absorbed on main (graduated from baby): {slug}"
    elif cur == "main":
        committed = commit_changes(cwd, msg)
        if not committed:
            return f"error: 没有改动可 commit ({slug})"
        result = f"absorbed on main: {slug}"
    elif cur == "staging":
        # 用户改了 staging 内容？罕见但理论上可能。先切回 main 再 commit
        # diff 不一定是从 main 来的，但策略仍是"用户改的 commit 到 main"。
        run_git(["checkout", "main"], cwd=cwd)
        run_git(["add", "-A"], cwd=cwd)
        run_git(["commit", "-m", msg], cwd=cwd)
        result = f"absorbed on main (was on staging): {slug}"
    else:
        return f"error: 异常分支 {cur!r}"

    # 如果有 staging 分支也删——用户手改优先级压过灰度候选
    code, _, _ = run_git(["rev-parse", "--verify", "staging"], cwd=cwd)
    if code == 0:
        run_git(["checkout", "main"], cwd=cwd)
        run_git(["branch", "-D", "staging"], cwd=cwd)
        # .canary 物化目录也清
        canary_md = target.parent / ".canary" / slug
        if canary_md.is_dir():
            import shutil
            shutil.rmtree(canary_md, ignore_errors=True)
        result += " (deleted in-flight staging)"

    return result


# ═══════════════════════════════════════════════════════════════════
# v2.2 渐进收敛工具（ClusterAgent 用，处理近义 slug 整合）
# ═══════════════════════════════════════════════════════════════════

@tool(name="rename_skill")
def rename_skill(old_name: str, new_name: str) -> str:
    """ClusterAgent 专用：把 **baby 分支** 的 skill 重命名（合并近义 slug）。

    只允许 baby 状态的 skill 重命名；main/staging 状态拒绝（已有 git 历史
    + symlink 已装到 CC，改名会破坏一致性）。

    用例：cluster 发现两个 baby skill 实际同义（``3gpp-crawl-routine`` vs
    ``3gpp-crawler-routine``），调 RenameSkill 把 less-precise 的归到
    more-precise；之后 add_task_to_skill 都打到统一 slug 上。

    实现：mv 目录 + 更新 SKILL.md.name + 改 body 标题 + commit on baby。
    """
    from xskill.skill.git import current_branch, run_git
    skill_dir = agent_tool_config.atom_skill_dir
    if skill_dir is None:
        return "error: atom task tool context not initialized"
    old_slug = _slugify(old_name)
    new_slug = _slugify(new_name)
    if old_slug == new_slug:
        return f"noop: {old_slug} 已是目标名"
    old_path = skill_dir / old_slug
    new_path = skill_dir / new_slug
    if not old_path.is_dir():
        return f"error: skill {old_slug} not found"
    if new_path.exists():
        return f"error: target {new_slug} 已存在，无法重命名（先 MoveTaskTo 合并 atom）"
    if not (old_path / ".git").is_dir():
        return f"error: skill {old_slug} 没 git 仓库"
    cur = current_branch(str(old_path))
    if cur != "baby":
        return (f"error: 仅 baby 分支可重命名 (当前 {cur!r}); "
                "main/staging 已有 git 历史 + symlink 装到 CC，不可改名")

    old_path.rename(new_path)
    # 更新 SKILL.md frontmatter.name + body 标题
    skill_md = new_path / "SKILL.md"
    if skill_md.is_file():
        text = skill_md.read_text(encoding="utf-8")
        fm, body = fm_parse(text)
        fm["name"] = new_slug
        body = body.replace(f"# {old_slug}", f"# {new_slug}", 1)
        skill_md.write_text(fm_serialize(fm, body), encoding="utf-8")
    run_git(["add", "-A"], cwd=str(new_path))
    run_git(["commit", "-m", f"rename: {old_slug} → {new_slug}"], cwd=str(new_path))
    logger.info(f"renamed baby skill: {old_slug} → {new_slug}")
    return f"renamed: {old_slug} → {new_slug}"


@tool(name="read_skill_tasks")
def read_skill_tasks(skill_name: str) -> str:
    """读取某 skill 的 candidates buffer 列表。

    cluster agent 用来"看这个 baby skill 在攒什么类型的 atom"——决定该不该
    把当前 atom 也归过去。返回 yaml-like 文本，每条 atom 一行含 weightscore。
    """
    from xskill.skill import candidates as C
    skill_dir = agent_tool_config.atom_skill_dir
    if skill_dir is None:
        return "error: atom task tool context not initialized"
    slug = _slugify(skill_name)
    target = skill_dir / slug
    if not target.is_dir():
        return f"error: skill {slug} not found"
    data = C.load_candidates(target)
    cands = data.get("candidates", []) or []
    if not cands:
        return f"(skill {slug}: 0 candidates in buffer)"
    total = sum(int(c.get("weightscore", 0)) for c in cands)
    lines = [f"skill {slug} candidates buffer ({len(cands)} atoms, total={total}/10):"]
    for c in cands:
        note = c.get("note", "")
        ext = f"  note: {note}" if note else ""
        lines.append(
            f"  - atom_id={c['atom_id']}  weightscore={c.get('weightscore', 0)}{ext}"
        )
    return "\n".join(lines)


@tool(name="move_task_to")
def move_task_to(skill_from: str, skill_to: str, atom_id: str) -> str:
    """把 atom 从 ``skill_from`` 的 candidates buffer 移到 ``skill_to``。

    用例：cluster 发现某 atom 当初被错放进 skill_A，看完后觉得应当归到
    skill_B → 调本工具完成迁移。覆盖语义（skill_to 已有该 atom → 覆盖
    weightscore）。

    源 buffer 为空时不删空骨架（保留 baby skill，cluster 后续可能再填）。
    """
    from xskill.skill import candidates as C
    skill_dir = agent_tool_config.atom_skill_dir
    if skill_dir is None:
        return "error: atom task tool context not initialized"
    from_slug = _slugify(skill_from)
    to_slug = _slugify(skill_to)
    if from_slug == to_slug:
        return f"noop: 源和目标是同一 skill ({from_slug})"
    from_path = skill_dir / from_slug
    to_path = skill_dir / to_slug
    if not from_path.is_dir():
        return f"error: source skill {from_slug} not found"
    if not to_path.is_dir():
        return f"error: target skill {to_slug} not found"

    weightscore = C.move_atom_contribution(
        from_path,
        to_path,
        atom_id,
    )
    if weightscore is None:
        return f"error: atom_id {atom_id} 不在 {from_slug} buffer 中"

    logger.info(f"moved task: atom={atom_id} {from_slug} → {to_slug}")
    return (f"moved: atom={atom_id} from {from_slug} to {to_slug} "
            f"(weightscore={weightscore})")
