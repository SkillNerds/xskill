"""SkillScriptingAgent —— 把已有主干技能改得更偏可执行脚本（#213 实验性）。

形态接近 SkillEditAgent，提示词不同：可执行步骤进 scripts/，正文只留何时
调用、怎么调、参数和坑。提交走 commit_update_main，不开灰度、不用 jam 门。
"""
from __future__ import annotations

from xskill.skill.write_policy import require_skill_write

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger("xskill.agents.skill_scripting_agent")

_SYSTEM_PROMPT = """你是 SkillScriptingAgent。用户点了「脚本化（实验性）」，要把
当前这份已经在主干上的技能改得更偏可执行脚本。

# 你的目标

读现有 SKILL.md 和目录里的脚本、参考材料，把能机械执行的步骤写成
``scripts/`` 下的参数化脚本（Python 或 shell）。不要把本机路径、用户名、
一次运行里的文件名写死；用参数或环境变量。正文 SKILL.md 只保留：

- 何时该用这个技能
- 调用哪个脚本、传什么参数
- 关键坑和验证方式

不要新开灰度分支，不要调 commit_to_staging。写完必须调
``commit_update_main(skill_name, message)`` 把改动直接提交到主干。

# 目录约定

- ``<skill_dir>/SKILL.md`` — 必产物，frontmatter schema 不变
- ``<skill_dir>/scripts/`` — 可执行脚本（参数化）
- ``<skill_dir>/references/`` — 长参考材料（可选）

# 纪律

- 不要删掉仍有用的 when / 坑；只是把步骤从正文搬进脚本。
- 正文仍要合法 frontmatter（name、description）。
- 不要改 ``.candidates.yml``、``.ux_scores.jsonl`` 这些运行时辅助文件。
"""


@dataclass
class SkillScriptingAgent:
    skill_dir: Path
    agno_agent_factory: Callable[..., Any]
    llm_cfg: dict
    logs_dir: Path | None = None

    def __post_init__(self) -> None:
        self.skill_dir = Path(self.skill_dir)
        if self.logs_dir is not None:
            self.logs_dir = Path(self.logs_dir)

    def _trace_path(self) -> Path | None:
        if self.logs_dir is None:
            return None
        return (
            self.logs_dir
            / "agents"
            / "skill_edit_agents"
            / "skills"
            / f"{self.skill_dir.name}.log"
        )

    def run(self) -> bool:
        """把当前主干技能脚本化并提交。成功返回 True。"""
        require_skill_write(self.skill_dir)
        from xskill.agents import agent_tools
        from xskill.canary import has_staging
        from xskill.skill.frontmatter import FrontmatterError, parse_strict
        from xskill.skill.git import (
            commit_update_main_branch,
            current_branch,
            ensure_head_on_main,
            run_git,
        )

        if has_staging(self.skill_dir):
            logger.info("scripting skip (staging exists): %s", self.skill_dir.name)
            return False
        branch = current_branch(str(self.skill_dir)) or ""
        if branch == "baby":
            logger.info("scripting skip (baby): %s", self.skill_dir.name)
            return False
        ensure_head_on_main(self.skill_dir)

        skill_md = self.skill_dir / "SKILL.md"
        mtime_before = skill_md.stat().st_mtime if skill_md.is_file() else 0
        size_before = skill_md.stat().st_size if skill_md.is_file() else 0
        code, sha_before, _ = run_git(["rev-parse", "HEAD"], cwd=str(self.skill_dir))
        sha_before = sha_before.strip() if code == 0 else ""

        lines = [
            f"skill_name: {self.skill_dir.name}",
            f"skill_base_path: {self.skill_dir}",
            "当前在主干。写完调用 commit_update_main。",
            "",
            "# 当前 skill 文件树",
        ]
        entries = 0
        for path in sorted(self.skill_dir.rglob("*")):
            rel = path.relative_to(self.skill_dir)
            if ".git" in rel.parts:
                continue
            if rel.name in {".candidates.yml", ".ux_scores.jsonl", ".lock",
                            ".scripting_requested"}:
                continue
            suffix = "/" if path.is_dir() else ""
            lines.append(f"- {rel.as_posix()}{suffix}")
            entries += 1
            if entries >= 80:
                lines.append("- ... truncated")
                break
        user_msg = "\n".join(lines)
        tools = [
            agent_tools.skill_read,
            agent_tools.read_file,
            agent_tools.list_files,
            agent_tools.grep_files,
            agent_tools.write_file,
            agent_tools.commit_update_main,
        ]
        agent = self.agno_agent_factory(instructions=[_SYSTEM_PROMPT], tools=tools)
        from xskill.agents.agent_trace import trace_to
        from xskill.agents.context_budget import (
            DEFAULT_MAX_CONTEXT,
            TRIM_TRIGGER_RATIO,
        )

        max_context = int(
            (self.llm_cfg or {}).get("max_context") or DEFAULT_MAX_CONTEXT
        )
        spill_limit = int(max_context * TRIM_TRIGGER_RATIO)
        with trace_to(self._trace_path(), append=True, spill_token_limit=spill_limit):
            agent.run(user_msg)

        if not skill_md.is_file() or skill_md.stat().st_size == 0:
            logger.warning("scripting left empty SKILL.md: %s", self.skill_dir.name)
            return False
        try:
            parse_strict(skill_md.read_text(encoding="utf-8"))
        except FrontmatterError as exc:
            logger.warning(
                "scripting wrote invalid SKILL.md: %s — %s",
                self.skill_dir.name, exc,
            )
            return False

        wrote = (
            skill_md.stat().st_mtime > mtime_before
            or skill_md.stat().st_size != size_before
        )
        code, sha_after, _ = run_git(["rev-parse", "HEAD"], cwd=str(self.skill_dir))
        sha_after = sha_after.strip() if code == 0 else ""
        if sha_after and sha_after != sha_before:
            logger.info("scripting committed on main: %s %s", self.skill_dir.name, sha_after[:8])
            return True
        if wrote:
            ok = commit_update_main_branch(
                str(self.skill_dir),
                f"scripting: {self.skill_dir.name}",
            )
            if ok:
                return True
            logger.warning(
                "scripting wrote files but commit_update_main_branch returned false: %s",
                self.skill_dir.name,
            )
            return False
        logger.warning("scripting made no file or commit change: %s", self.skill_dir.name)
        return False
