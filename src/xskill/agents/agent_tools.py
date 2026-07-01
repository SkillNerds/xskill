"""
agent_tools.py — xskill agent tools and their runtime configuration
═════════════════════════════════════════════════════════════════════

This module owns the Agno tools registered on xskill agents. Runtime dependencies
are stored in ``agent_tool_config`` so callers can initialize once and tools can
read the current project/AtomTask dependencies through properties. LLM and
embedding clients are intentionally not stored here; non-tool workflows create
those from config at their own boundary.
"""

from __future__ import annotations

# ruff: noqa: BLE001,S110

import json, logging
from collections.abc import Mapping
from datetime import date, datetime
from pathlib import Path
from typing import Any

from agno.tools import tool

from xskill.config import XSkillConfig
from xskill.skill.frontmatter import (
    parse as fm_parse,
    parse_strict as fm_parse_strict,
    serialize as fm_serialize,
    FrontmatterError,
)

logger = logging.getLogger("xskill.agent_tools")


class AgentToolConfig:
    """Singleton runtime dependencies for agent tools."""

    def __init__(self):
        self._skill_dir: Path | None = None
        self._data_dir: Path | None = None
        self._config: XSkillConfig | Mapping[str, Any] | None = None
        self._atom_skill_dir: Path | None = None
        self._atom_store = None
        self._default_traj_root: Path | None = None

    def configure_skill_authoring(
        self, skill_dir, data_dir, config,
    ) -> None:
        self._skill_dir = Path(skill_dir)
        self._data_dir = Path(data_dir)
        self._config = config or {}

    def configure_atom_task(
        self, *, skill_dir, atom_store, default_traj_root,
    ) -> None:
        self._atom_skill_dir = Path(skill_dir)
        self._atom_store = atom_store
        self._default_traj_root = Path(default_traj_root)

    def snapshot(self) -> dict:
        return {
            "skill_dir": self._skill_dir,
            "data_dir": self._data_dir,
            "config": self._config,
            "atom_skill_dir": self._atom_skill_dir,
            "atom_store": self._atom_store,
            "default_traj_root": self._default_traj_root,
        }

    def restore(self, snapshot: dict) -> None:
        self._skill_dir = snapshot.get("skill_dir")
        self._data_dir = snapshot.get("data_dir")
        self._config = snapshot.get("config")
        self._atom_skill_dir = snapshot.get("atom_skill_dir")
        self._atom_store = snapshot.get("atom_store")
        self._default_traj_root = snapshot.get("default_traj_root")

    def clear_atom_task(self) -> None:
        self._atom_skill_dir = None
        self._atom_store = None
        self._default_traj_root = None

    @property
    def skill_dir(self) -> Path | None:
        return self._skill_dir

    @property
    def data_dir(self) -> Path | None:
        return self._data_dir

    @property
    def config(self) -> XSkillConfig | Mapping[str, Any] | None:
        return self._config

    @property
    def atom_skill_dir(self) -> Path | None:
        return self._atom_skill_dir

    @property
    def atom_store(self):
        return self._atom_store

    @property
    def default_traj_root(self) -> Path | None:
        return self._default_traj_root

    @property
    def writable_skill_dir(self) -> Path | None:
        return self._atom_skill_dir or self._skill_dir


agent_tool_config = AgentToolConfig()


def init_atom_task_tool_context(
    *,
    skill_dir,
    atom_store,
    default_traj_root,
):
    """Initialize tools that read AtomTask JSON and source trajectory text."""
    agent_tool_config.configure_atom_task(
        skill_dir=skill_dir,
        atom_store=atom_store,
        default_traj_root=default_traj_root,
    )


def init_skill_authoring_tool_context(
    skill_dir, data_dir, config,
):
    """Initialize general skill-authoring and description optimization tools."""
    agent_tool_config.configure_skill_authoring(
        skill_dir, data_dir, config,
    )


def _slugify(name: str) -> str:
    """Normalize a skill name to the slug form used in frontmatter.name."""
    return name.strip().lower().replace("_", "-").replace(" ", "-")


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

    results.sort(key=lambda x: x.get("similarity", 0), reverse=True)
    results = results[:top_k]
    return json.dumps(results, ensure_ascii=False, indent=2, default=str)


@tool(name="read_file")
def read_file(path: str) -> str:
    """Read an arbitrary file under the project root."""
    p = Path(path)
    root = agent_tool_config.skill_dir.parent
    try:
        p.resolve().relative_to(root.resolve())
    except ValueError:
        return f"error: outside project root ({path})"

    if not p.exists():
        return f"error: file not found ({path})"

    try:
        content = p.read_text(encoding="utf-8")
        if len(content) > 10000:
            return content[:10000] + f"\n\n... (truncated, full length {len(content)} chars)"
        return content
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

    data = C.load_candidates(target)
    data, was_new = C.add_or_merge(
        data, pattern, ptype, traj_id,
        attach_to=attach_to or None,
    )
    C.save_candidates(target, data)

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
    runtime_config = agent_tool_config.config
    llm_client = create_llm_client(runtime_config) if runtime_config is not None else None
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
    """读 skill 的 SKILL.md；没有则返回 placeholder 提示。"""
    skill_dir = agent_tool_config.atom_skill_dir
    if skill_dir is None:
        return "error: atom task tool context not initialized"
    slug = _slugify(skill_name)
    p = skill_dir / slug / "SKILL.md"
    if not p.is_file():
        return f"(skill {slug} has no SKILL.md yet — only candidates buffer)"
    return p.read_text(encoding="utf-8")


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
        ws = int(weightscore)
    except (TypeError, ValueError):
        return f"error: weightscore must be int 1..10 (got {weightscore!r})"
    if not (1 <= ws <= 10):
        return f"error: weightscore must be 1..10 (got {ws})"
    data = C.load_candidates(target)
    data, was_new = C.add_atom_contribution(data, atom_id, ws)
    C.save_candidates(target, data)
    buffer_total = sum(int(c.get("weightscore", 0)) for c in data["candidates"])
    verb = "new" if was_new else "overwrite"
    return (f"{verb}: skill={slug} atom={atom_id} weightscore={ws} "
            f"buffer_total={buffer_total}/10")


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

    return [look, submit_atom, context_budget, my_atoms]


@tool(name="list_files")
def list_files(path: str) -> str:
    """列目录下的文件 / 子目录。

    给 SkillEditAgent 用来摸清当前 skill 已有什么文件（避免重复写、便于增量
    更新）。路径必须在 skill_dir 范围内；越界返回 error。
    """
    p = Path(path)
    skill_root = agent_tool_config.atom_skill_dir if agent_tool_config.atom_skill_dir is not None else agent_tool_config.skill_dir
    if skill_root is None:
        return "error: skill_dir context not initialized"
    try:
        p.resolve().relative_to(skill_root.resolve())
    except ValueError:
        return f"error: list_files restricted to skill_dir (tried: {path})"
    if not p.is_dir():
        return f"error: not a directory: {path}"
    entries = sorted(p.iterdir())
    if not entries:
        return "(empty)"
    return "\n".join(
        f"{'[dir] ' if e.is_dir() else ''}{e.name}" for e in entries
    )


# ═══════════════════════════════════════════════════════════════════
# SkillEditAgent 专用 commit 工具（不要给 ClusterAgent）
# ═══════════════════════════════════════════════════════════════════

def _run_description_optimization(target: Path, slug: str) -> None:
    """commit 前跑 description 触发优化（D1：硬编码进 workflow，不做 agent tool）。

    gating on ``config.skill_opt.enabled``；任何失败只 log，绝不阻断 commit
    （退回 agent 写的 description 继续提交）。LLM/embed 客户端在这个确定性
    workflow 内从 config 创建，不从 agent tool context 借对象。
    """
    config = agent_tool_config.config
    if config is None:
        logger.warning("skip description_opt: agent tool config not initialized")
        return
    if not (config.get("skill_opt", {}) or {}).get("enabled", True):
        return
    from xskill.utils.llm import create_embed_client, create_llm_client
    try:
        import httpx
        client_init_errors = (ValueError, RuntimeError, OSError, httpx.HTTPError)
    except ImportError:
        client_init_errors = (ValueError, RuntimeError, OSError)
    try:
        llm_client = create_llm_client(config)
        embed_client = create_embed_client(config)
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
            agno_agent_factory=make_default_factory(config),
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
        return f"error: commit_baby_to_main 失败（不在 baby 分支？看 daemon 日志）"
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

    from_data = C.load_candidates(from_path)
    cands_from = from_data.get("candidates", []) or []
    target_atom = None
    new_cands_from: list = []
    for c in cands_from:
        if c.get("atom_id") == atom_id:
            target_atom = c
        else:
            new_cands_from.append(c)
    if target_atom is None:
        return f"error: atom_id {atom_id} 不在 {from_slug} buffer 中"

    from_data["candidates"] = new_cands_from
    C.save_candidates(from_path, from_data)

    to_data = C.load_candidates(to_path)
    weightscore = int(target_atom.get("weightscore", 0))
    note = target_atom.get("note", "")
    to_data, _ = C.add_atom_contribution(to_data, atom_id, weightscore, note=note)
    C.save_candidates(to_path, to_data)

    logger.info(f"moved task: atom={atom_id} {from_slug} → {to_slug}")
    return (f"moved: atom={atom_id} from {from_slug} to {to_slug} "
            f"(weightscore={weightscore})")
