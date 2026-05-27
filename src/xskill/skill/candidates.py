"""
skill/candidates.py -- Candidate buffer for the Skill curation pipeline
===================================================================
Manages ``.candidates.yml`` under each skill directory. Candidates are patterns
(step / warning / decision_branch) proposed by the agent after seeing a
trajectory, but NOT yet written into SKILL.md. They accumulate across
trajectories; when at least ``threshold`` distinct trajectories support the
same pattern, it gets *promoted* into SKILL.md body.

Design notes
------------
- Fuzzy match: two patterns are considered "the same candidate" when
  lowercase-stripped first 60 chars match, OR one is a lowercased substring
  of the other. This is intentionally conservative — the agent's own
  ``list_candidates`` tool gives it the final de-dup judgement.
- We refuse to touch SKILL.md frontmatter; promotion only inserts body
  content, preserving the Stage A schema exactly.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime
from pathlib import Path

import yaml

from xskill.skill.frontmatter import parse as fm_parse, serialize as fm_serialize

logger = logging.getLogger("candidates")

CANDIDATES_FILENAME = ".candidates.yml"
FUZZY_PREFIX = 60

# v2 schema：以 atom_id 为单位累计 weightscore（cluster agent 给的 0-10 分）。
# buffer 中 pending（非 promoted）的 weightscore_total 累加 ≥ 这个阈值
# 就让 SkillEditAgent 触发一次 SKILL.md 整理。
# 单 atom 给 10 分立即触发——cluster agent 的提示词鼓励"非常确信时打高分"。
ATOM_PROMOTION_THRESHOLD = 10


# ═══════════════════════════════════════════════════════════════════
# I/O
# ═══════════════════════════════════════════════════════════════════

def _candidates_path(skill_dir: Path) -> Path:
    return Path(skill_dir) / CANDIDATES_FILENAME


def load_candidates(skill_dir: Path) -> dict:
    """Read .candidates.yml or return a fresh empty structure."""
    p = _candidates_path(skill_dir)
    if not p.exists():
        return {"candidates": []}
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception as e:
        logger.warning(f"failed to parse {p}: {e}; starting empty")
        return {"candidates": []}
    if not isinstance(data, dict) or "candidates" not in data:
        return {"candidates": []}
    if data.get("candidates") is None:
        data["candidates"] = []
    return data


def save_candidates(skill_dir: Path, data: dict) -> None:
    p = _candidates_path(skill_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


# ═══════════════════════════════════════════════════════════════════
# Fuzzy matching + merge
# ═══════════════════════════════════════════════════════════════════

def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def _fuzzy_equal(a: str, b: str) -> bool:
    a_n = _norm(a)
    b_n = _norm(b)
    if not a_n or not b_n:
        return False
    if a_n == b_n:
        return True
    # first-N prefix match
    if a_n[:FUZZY_PREFIX] == b_n[:FUZZY_PREFIX]:
        return True
    # substring (one contained in the other) — only when the shorter is
    # meaningfully long, to avoid collapsing unrelated short phrases.
    shorter, longer = (a_n, b_n) if len(a_n) <= len(b_n) else (b_n, a_n)
    if len(shorter) >= 20 and shorter in longer:
        return True
    return False


def add_or_merge(
    data: dict,
    pattern: str,
    pattern_type: str,
    traj_id: str,
    attach_to: str | None = None,
) -> tuple[dict, bool]:
    """Either merge ``traj_id`` into an existing fuzzy-matching candidate, or
    append a new one.

    Returns (data, was_new).
    """
    today = date.today().isoformat()
    candidates = data.setdefault("candidates", [])

    for cand in candidates:
        if _fuzzy_equal(cand.get("pattern", ""), pattern):
            supporters = cand.setdefault("supporting_trajs", [])
            if traj_id not in supporters:
                supporters.append(traj_id)
            cand["last_seen"] = today
            # backfill missing fields on legacy entries
            cand.setdefault("first_seen", today)
            cand.setdefault("type", pattern_type)
            if attach_to and not cand.get("attach_to"):
                cand["attach_to"] = attach_to
            cand.setdefault("promoted", False)
            cand.setdefault("promoted_at", None)
            return data, False

    entry = {
        "pattern": pattern,
        "type": pattern_type,
        "supporting_trajs": [traj_id],
        "first_seen": today,
        "last_seen": today,
        "promoted": False,
        "promoted_at": None,
    }
    if attach_to:
        entry["attach_to"] = attach_to
    candidates.append(entry)
    return data, True


# ═══════════════════════════════════════════════════════════════════
# v2 schema — atom_id + weightscore 累计
# ═══════════════════════════════════════════════════════════════════

def add_atom_contribution(
    data: dict,
    atom_id: str,
    weightscore: int,
    *,
    note: str = "",
) -> tuple[dict, bool]:
    """v2.1 简化 schema：candidates 是纯文件 buffer，不入 git 不做追溯。

    schema::

        candidates:
          - atom_id: str
            weightscore: int               # 0-10，cluster agent 评分
            note: str                      # 可选简短理由

    **同 atom + 同 skill 重复 add 时覆盖**（不累加；不保留 contributions 历史）。
    cluster agent 后续可"改主意"——比如初次打 5 后想清楚改成 8，直接覆盖。

    去掉的字段：``weightscore_total / contributions[] / promoted / promoted_at``。
    promoted 的语义改为"SkillEditAgent 成功后整批清空 candidates 文件"，
    不再用单条 marker。

    返回 ``(data, was_new)``。``was_new=True`` 表示新 atom，``False`` 表示
    覆盖既有条目。
    """
    candidates = data.setdefault("candidates", [])
    for c in candidates:
        if c.get("atom_id") == atom_id:
            # 覆盖语义
            c["weightscore"] = int(weightscore)
            if note:
                c["note"] = note
            return data, False
    entry = {"atom_id": atom_id, "weightscore": int(weightscore)}
    if note:
        entry["note"] = note
    candidates.append(entry)
    return data, True


def ready_for_promotion_v2(
    data: dict, threshold: int = ATOM_PROMOTION_THRESHOLD,
) -> list[dict]:
    """v2.1 简化：candidates 全是 pending（无 promoted 字段），sum 所有
    ``weightscore`` ≥ threshold 即 ready。

    返回 buffer 中所有 candidates iff 总分 ≥ threshold；否则空列表。
    """
    cands = data.get("candidates", []) or []
    total = sum(int(c.get("weightscore", 0)) for c in cands)
    if total >= threshold:
        return list(cands)
    return []


def clear_candidates(skill_dir: Path) -> None:
    """v2.1: SkillEditAgent 成功落盘 SKILL.md 后调用——清空 buffer。

    写入 ``{candidates: []}`` 而不是删文件，保留 yaml 形态便于下次 cluster
    直接追加。
    """
    save_candidates(skill_dir, {"candidates": []})


def find_atom_in_any_skill(skill_dir: Path, atom_id: str) -> str | None:
    """在所有 skill 的 ``.candidates.yml`` 里查 ``atom_id``，返回它所在的
    skill 名；找不到 → ``None``。

    用途：
    1) ``process_atom_task`` 返回结果时记录 ``skill_name`` 字段（per-atom 日志）。
    2) cluster 重试前去重——已经落地的 atom 不再投给 cluster_agent，避免
       重复花 LLM。

    单条 atom 理论上只属于一个 skill（cluster agent 不会重复 add），按
    扫描顺序返回第一个命中。
    """
    hit = find_atom_entry_in_any_skill(skill_dir, atom_id)
    return hit[0] if hit else None


def find_atom_entry_in_any_skill(
    skill_dir: Path, atom_id: str,
) -> tuple[str, int] | None:
    """``find_atom_in_any_skill`` 的扩展版：同时返回 ``(skill_name, weightscore)``。

    per-atom info 日志要打 ``ws=...``，需要这个 weightscore。
    """
    if not skill_dir or not Path(skill_dir).is_dir():
        return None
    for skill_path in sorted(Path(skill_dir).iterdir()):
        if not skill_path.is_dir() or skill_path.name.startswith("."):
            continue
        cand_yml = skill_path / CANDIDATES_FILENAME
        if not cand_yml.is_file():
            continue
        try:
            data = yaml.safe_load(cand_yml.read_text(encoding="utf-8")) or {}
            for c in data.get("candidates", []) or []:
                if c.get("atom_id") == atom_id:
                    return (skill_path.name, int(c.get("weightscore", 0)))
        except Exception:
            continue
    return None


# ═══════════════════════════════════════════════════════════════════
# v1 schema — 保留给旧 watcher / 旧 SkillAgent 用，下个 task 切换后再清
# ═══════════════════════════════════════════════════════════════════

def ready_for_promotion(data: dict, threshold: int = 3) -> list[dict]:
    out = []
    for c in data.get("candidates", []):
        if c.get("promoted"):
            continue
        if len(c.get("supporting_trajs", []) or []) >= threshold:
            out.append(c)
    return out


def _parse_iso_date(s: str | None) -> date | None:
    if not s:
        return None
    try:
        return date.fromisoformat(str(s)[:10])
    except Exception:
        return None


def stale_candidates(data: dict, days: int = 60, threshold: int = 3) -> list[dict]:
    today = date.today()
    out = []
    for c in data.get("candidates", []):
        if c.get("promoted"):
            continue
        if len(c.get("supporting_trajs", []) or []) >= threshold:
            continue
        first = _parse_iso_date(c.get("first_seen"))
        if first is None:
            continue
        if (today - first).days >= days:
            out.append(c)
    return out


def mark_promoted(data: dict, pattern: str) -> None:
    now = datetime.now().isoformat(timespec="seconds")
    for c in data.get("candidates", []):
        if _fuzzy_equal(c.get("pattern", ""), pattern):
            c["promoted"] = True
            c["promoted_at"] = now
            return


# ═══════════════════════════════════════════════════════════════════
# Promotion: candidate → SKILL.md body insertion
# ═══════════════════════════════════════════════════════════════════

def _read_skill_md(skill_dir: Path) -> tuple[dict, str, Path]:
    upper = skill_dir / "SKILL.md"
    lower = skill_dir / "skill.md"
    if upper.exists():
        fm, body = fm_parse(upper.read_text(encoding="utf-8"))
        return fm, body, upper
    if lower.exists():
        fm, body = fm_parse(lower.read_text(encoding="utf-8"))
        return fm, body, lower
    return {}, "", upper


def _find_section_bounds(body: str, section_name: str | None) -> tuple[int, int]:
    """Return (start_line_index_after_header, end_line_index_exclusive) for the
    section whose ``##`` header *contains* ``section_name`` (case-insensitive).
    If ``section_name`` is None/empty, targets the last ``##`` section.
    If no section found, returns (len(lines), len(lines)) → appends at end.
    """
    lines = body.split("\n")
    headers: list[tuple[int, str]] = []
    for i, ln in enumerate(lines):
        m = re.match(r"^##\s+(.*?)\s*$", ln)
        if m:
            headers.append((i, m.group(1).strip()))

    if not headers:
        return len(lines), len(lines)

    target_idx: int | None = None
    if section_name:
        needle = section_name.strip().lower()
        for k, (i, name) in enumerate(headers):
            if needle and (needle in name.lower() or name.lower() in needle):
                target_idx = k
                break
    if target_idx is None:
        target_idx = len(headers) - 1  # last section as fallback

    start = headers[target_idx][0] + 1
    end = headers[target_idx + 1][0] if target_idx + 1 < len(headers) else len(lines)
    return start, end


def _next_step_number(body: str) -> int:
    """Scan for the max `N.` list-item leading number in the body; return N+1."""
    max_n = 0
    for m in re.finditer(r"^(\d+)\.\s", body, flags=re.MULTILINE):
        n = int(m.group(1))
        if n > max_n:
            max_n = n
    return max_n + 1


def _evidence_tag(supporting_trajs: list[str]) -> str:
    total = len(supporting_trajs)
    sample = ", ".join(supporting_trajs[:3])
    return f"({total} trajectories: {sample})"


def _insert_warning(body: str, attach_to: str | None, pattern: str,
                    supporting_trajs: list[str]) -> str:
    lines = body.split("\n")
    start, end = _find_section_bounds(body, attach_to)
    tag = _evidence_tag(supporting_trajs)
    block = [
        "",
        f"   > ⚠️ {pattern} {tag}",
        "",
    ]
    # Insert right after the header (start), before any existing content.
    new_lines = lines[:start] + block + lines[start:end] + lines[end:]
    return "\n".join(new_lines)


def _insert_step(body: str, attach_to: str | None, pattern: str,
                 supporting_trajs: list[str]) -> str:
    lines = body.split("\n")
    start, end = _find_section_bounds(body, attach_to)
    n = _next_step_number(body)
    tag = _evidence_tag(supporting_trajs)
    block = [
        "",
        f"{n}. {pattern}  {tag}",
        "",
    ]
    # append at end of the chosen section
    # trim trailing blank lines inside the section
    tail = end
    while tail - 1 >= start and lines[tail - 1].strip() == "":
        tail -= 1
    new_lines = lines[:tail] + block + lines[tail:end] + lines[end:]
    return "\n".join(new_lines)


def _insert_decision_branch(body: str, attach_to: str | None, pattern: str,
                             supporting_trajs: list[str]) -> str:
    lines = body.split("\n")
    start, end = _find_section_bounds(body, attach_to)
    tag = _evidence_tag(supporting_trajs)
    block = [
        "",
        f"- {pattern} {tag}",
        "",
    ]
    tail = end
    while tail - 1 >= start and lines[tail - 1].strip() == "":
        tail -= 1
    new_lines = lines[:tail] + block + lines[tail:end] + lines[end:]
    return "\n".join(new_lines)


def _apply_candidate(body: str, cand: dict) -> str:
    ptype = (cand.get("type") or "step").strip().lower()
    pattern = cand.get("pattern", "").strip()
    attach_to = cand.get("attach_to")
    supporters = cand.get("supporting_trajs", []) or []
    if ptype == "warning":
        return _insert_warning(body, attach_to, pattern, supporters)
    if ptype == "decision_branch":
        return _insert_decision_branch(body, attach_to, pattern, supporters)
    return _insert_step(body, attach_to, pattern, supporters)


def promote_ready_candidates(skill_dir: Path, threshold: int = 3,
                              config: dict | None = None) -> list[dict]:
    """Promote ready candidates by running an autonomous skill-edit agent.

    The agent gets full tool access (read/write files, create scripts, etc.)
    and decides on its own how to incorporate the candidates. We only provide:
    - Which candidates reached threshold
    - Their supporting trajectory IDs
    - The skill directory path

    After the agent finishes, we git-commit the result.

    Returns the list of candidate dicts that were promoted.
    """
    skill_dir = Path(skill_dir)
    data = load_candidates(skill_dir)
    ready = ready_for_promotion(data, threshold=threshold)
    if not ready:
        return []

    # Run autonomous edit agent
    try:
        _run_skill_edit_agent(
            skill_dir=skill_dir,
            candidates=ready,
            config=config or {},
        )
    except Exception as e:
        logger.error("skill edit agent failed for %s: %s", skill_dir.name, e)
        # Fallback to rule-based insertion
        fm, body, _ = _read_skill_md(skill_dir)
        if fm or body:
            for cand in ready:
                body = _apply_candidate(body, cand)
            (skill_dir / "SKILL.md").write_text(fm_serialize(fm, body), encoding="utf-8")
            logger.info("fell back to rule-based promotion for %s", skill_dir.name)

    # Mark all as promoted
    promoted = []
    for cand in ready:
        mark_promoted(data, cand.get("pattern", ""))
        promoted.append(cand)

    save_candidates(skill_dir, data)
    logger.info("promoted %d candidate(s) in %s", len(promoted), skill_dir.name)
    return promoted


def _run_skill_edit_agent(skill_dir: Path, candidates: list[dict],
                          config: dict) -> None:
    """Launch an autonomous agent to edit a skill directory.

    The agent has full read/write access to the skill directory and can:
    - Read/write SKILL.md
    - Create/edit scripts/* and references/*
    - Read source trajectories
    - Decide its own approach

    Configurable via ``config["skill_edit_agent"]``:
      - ``tool_call_limit`` (default 20) -- hard cap on agent actions
      - ``timeout_seconds`` (default 600) -- wall-clock budget; on timeout we
        keep partial writes and return without raising (so promotion still
        marks candidates done — observed practice).
      - ``read_file_max_bytes`` (default 15000) -- per-read truncation
    """
    import json as _json
    import os
    import threading
    from agno.agent import Agent
    from agno.models.openai.like import OpenAILike
    from agno.tools import tool as _agno_tool

    llm_cfg = config.get("llm", {})
    if not llm_cfg.get("base_url") or not llm_cfg.get("model"):
        raise ValueError("LLM not configured — cannot run skill edit agent")

    agent_cfg = config.get("skill_edit_agent", {}) or {}
    tool_call_limit = int(agent_cfg.get("tool_call_limit", 20))
    timeout_seconds = int(agent_cfg.get("timeout_seconds", 600))
    read_max_bytes = int(agent_cfg.get("read_file_max_bytes", 15000))

    # ── Tools: full filesystem access within skill + data dirs ──

    @_agno_tool(name="read_file", description="Read any file (trajectory, skill, script, etc).\nArgs:\n    path: file path")
    def _read_file(path: str) -> str:
        p = Path(path)
        if not p.is_file():
            return f"Error: not found: {path}"
        try:
            return p.read_text(encoding="utf-8")[:read_max_bytes]
        except Exception as e:
            return f"Error: {e}"

    @_agno_tool(name="write_file", description="Write/overwrite a file in the skill directory.\nArgs:\n    path: file path\n    content: file content")
    def _write_file(path: str, content: str) -> str:
        p = Path(path)
        # Security: only allow writes within the skill dir
        try:
            p.resolve().relative_to(skill_dir.resolve())
        except ValueError:
            return f"Error: can only write within {skill_dir}"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return f"Written {len(content)} bytes to {p}"

    @_agno_tool(name="list_files", description="List files in a directory.\nArgs:\n    path: directory path")
    def _list_files(path: str) -> str:
        p = Path(path)
        if not p.is_dir():
            return f"Error: not a directory: {path}"
        entries = sorted(p.iterdir())
        return "\n".join(f"{'[dir] ' if e.is_dir() else ''}{e.name}" for e in entries) or "(empty)"

    # ── Build user message with context ──

    cand_info = []
    for c in candidates:
        cand_info.append({
            "pattern": c.get("pattern", ""),
            "type": c.get("type", "step"),
            "attach_to": c.get("attach_to", ""),
            "supporting_trajs": c.get("supporting_trajs", []),
        })

    # Find trajectory file paths — search across all registered watch dirs.
    # Missing IDs are skipped (warning logged by helper); the promote agent
    # then sees a partial map, which is better than the v1 reverse-derived
    # `skill_dir.parent.parent / "data"` path that silently returned {} once
    # trajectories moved out of the repo root.
    from xskill.pipeline.registry import find_traj_file
    traj_paths = {}
    for c in candidates:
        for tid in c.get("supporting_trajs", []):
            if tid in traj_paths:
                continue
            p = find_traj_file(tid, ".md")
            if p is not None:
                traj_paths[tid] = str(p)

    user_msg = f"""你是一个从 agent 执行轨迹中抽取复用模式的 Skill 编辑 Agent。

已有一个 skill 目录在 `{skill_dir}`，其中的 `.candidates.yml` 记录了从多条轨迹中提取的候选 pattern（步骤、警告、决策分支等）。以下 candidates 已经积累了足够多的独立轨迹支持，说明它们是跨轨迹的共性模式，而非单次偶然：

{_json.dumps(cand_info, ensure_ascii=False, indent=2)}

这些 candidates 对应的源轨迹文件路径如下，你可以用 read_file 查看完整的执行过程和上下文：
{_json.dumps(traj_paths, ensure_ascii=False, indent=2)}

你的目标是将这些共性模式合入到 skill 中，使其成为一份高质量的、可被其他 agent 直接加载使用的操作指南。具体来说：

- 先用 list_files 和 read_file 了解 skill 当前结构和内容
- 阅读你认为必要的源轨迹，理解每个 candidate 的实际执行上下文
- 自行决定如何组织：可以修改 SKILL.md 的正文、新建 scripts/ 下的辅助脚本、补充 references/ 下的参考材料
- 保持合理粒度——归纳共性而非堆砌细节，让消费者 agent 看完就能动手
- 用中文撰写内容，代码、命令、路径保持英文原文

完成所有编辑后说 done。"""

    model = OpenAILike(
        id=llm_cfg["model"],
        api_key=llm_cfg.get("api_key") or os.environ.get("LLM_API_KEY", ""),
        base_url=llm_cfg["base_url"],
    )

    agent = Agent(
        model=model,
        tools=[_read_file, _write_file, _list_files],
        tool_call_limit=tool_call_limit,
        markdown=True,
    )

    # Run inside a daemon thread with wall-clock timeout. agno's Agent.run is
    # blocking and not cancellable, so on timeout we let the thread leak (it
    # dies with the process) and keep whatever the agent already wrote.
    err_box: dict = {"err": None}

    def _runner():
        try:
            agent.run(user_msg, stream=False)
        except BaseException as exc:  # noqa: BLE001 — record everything
            err_box["err"] = exc

    t = threading.Thread(target=_runner, daemon=True)
    t.start()
    t.join(timeout=timeout_seconds)
    if t.is_alive():
        logger.warning(
            "skill edit agent timed out after %ds for %s; keeping partial writes",
            timeout_seconds, skill_dir.name,
        )
        return
    if err_box["err"] is not None:
        raise err_box["err"]
    logger.info("skill edit agent completed for %s", skill_dir.name)


# ═══════════════════════════════════════════════════════════════════
# Stale archival
# ═══════════════════════════════════════════════════════════════════

def archive_stale(skill_dir: Path, days: int = 60, threshold: int = 3) -> list[dict]:
    """Move stale candidates (older than ``days`` with < ``threshold`` supporters
    and not promoted) into ``references/stale_candidates.md`` and drop them
    from ``.candidates.yml``. Returns the archived entries.
    """
    skill_dir = Path(skill_dir)
    data = load_candidates(skill_dir)
    stale = stale_candidates(data, days=days, threshold=threshold)
    if not stale:
        return []

    refs_dir = skill_dir / "references"
    refs_dir.mkdir(parents=True, exist_ok=True)
    stale_file = refs_dir / "stale_candidates.md"

    lines: list[str] = []
    if stale_file.exists():
        lines.append(stale_file.read_text(encoding="utf-8").rstrip())
        lines.append("")
    else:
        lines.append("# Stale Candidates")
        lines.append("")
        lines.append("Patterns that never gathered enough trajectory support.")
        lines.append("")

    today = date.today().isoformat()
    lines.append(f"## Archived {today}")
    lines.append("")
    for c in stale:
        supporters = c.get("supporting_trajs", []) or []
        lines.append(
            f"- [{c.get('type', 'step')}] {c.get('pattern', '')} "
            f"(first_seen={c.get('first_seen', '?')}, "
            f"supporters={len(supporters)}: {', '.join(supporters)})"
        )
    lines.append("")

    stale_file.write_text("\n".join(lines), encoding="utf-8")

    # Drop from candidates list
    stale_patterns = [c.get("pattern", "") for c in stale]
    data["candidates"] = [
        c for c in data.get("candidates", [])
        if not any(_fuzzy_equal(c.get("pattern", ""), sp) for sp in stale_patterns)
    ]
    save_candidates(skill_dir, data)
    return stale
