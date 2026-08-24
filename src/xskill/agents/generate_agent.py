"""GenerateAgent —— 用户点名、直接写主干的 skill 生成代理。

和 SkillEditAgent 同类（Agno + 同一套工具上下文），但由
``xskill generate`` 启动，不靠候选缓冲攒分。读轨迹靠 list/grep/read；
写完用 ``commit_generate_main`` 落到 main。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger("xskill.generate_agent")

ONHOLD_PROMPT_LINE = "不要参考 on hold 轨迹。"

SYSTEM_PROMPT = """你是 XSkill 的 GenerateAgent。用户通过 `xskill generate` 发来一条指令，
要你立刻新建或改写一个 skill，并且提交到主干分支 main。

skill 是一份给其他编码代理阅读的说明书：SKILL.md 必写，还可以在
scripts/ 下放可执行脚本、在 references/ 下放长参考材料。价值是少踩坑、
少试错，而不是把某一次操作过程复述一遍。

# 这次任务

发起人用户 ID：{user_id}

用户指令：
{instruction}

优先阅读范围：{name_hint}
这不是禁读名单。磁盘上你能读到的轨迹目录都可以搜；上面列出的人只是
用户希望你先看的范围。

不要参考 on hold 轨迹。

# 你可以读的目录

{read_roots_block}

用 list_files 摸清结构，用 grep_files 按关键词搜索，用 read_file 按行精读。
看某个已有 skill 的现状用 skill_read。

# 怎么写

- 新建：先 new_skill_folder(name, description)，再用 write_file 写出完整
  SKILL.md 和脚本。
- 改已有文件：必须先 read_file 或 skill_read，再 edit(path, old_string, new_string)。
  没读过会报错。新建尚不存在的文件用 write_file。
- 写完必须调用 commit_generate_main(skill_name, message)。它会把结果提交到
  main：没有 main 就创建，目录几乎是空的也允许提交。不要调用任何灰度
  （staging）提交工具。
- commit message 写清你新建还是改了哪个 skill、依据是什么。系统会自动在
  前面加上发起人 ID。
- 轨迹里若有密钥、token、密码、内网地址，不要原样写进 skill，用占位符。

# 可用工具

- list_files(path)：目录条目过多时完整列表写入 spill 文件，用 read_file 按行翻页。
- grep_files(pattern, path="", glob="", max_results=100)
- read_file(path, offset=1, limit=200)
- skill_read(skill_name)
- write_file(path, content)
- edit(path, old_string, new_string)
- new_skill_folder(skill_name, description)
- commit_generate_main(skill_name, message)
"""


def _name_hint(preferred_names: list[str]) -> str:
    names = [n.strip() for n in preferred_names if n and n.strip()]
    if not names:
        return "未指定，可以看全部轨迹。"
    return "请优先看这些人的轨迹：" + "、".join(names)


def _read_roots_block(roots: list[Path]) -> str:
    if not roots:
        return "（当前没有配置可读目录）"
    return "\n".join(f"- {path}" for path in roots)


@dataclass
class GenerateAgent:
    skill_dir: Path
    agno_agent_factory: Callable[..., Any]
    llm_cfg: dict
    logs_dir: Path | None
    extra_read_roots: tuple[Path, ...] = ()

    def _trace_path(self, user_id: str, job_id: str) -> Path | None:
        if self.logs_dir is None:
            return None
        return (
            self.logs_dir
            / "agents"
            / "generate_agents"
            / user_id
            / f"{job_id}.log"
        )

    def run(
        self,
        *,
        instruction: str,
        user_id: str,
        job_id: str,
        preferred_names: list[str] | None = None,
    ) -> str:
        from xskill.agents import agent_tools
        from xskill.agents.agent_trace import trace_to
        from xskill.agents.context_budget import (
            DEFAULT_MAX_CONTEXT,
            TRIM_TRIGGER_RATIO,
            _bool_or_default,
        )

        preferred_names = preferred_names or []
        sysprompt = SYSTEM_PROMPT.format(
            user_id=user_id,
            instruction=instruction.strip(),
            name_hint=_name_hint(preferred_names),
            read_roots_block=_read_roots_block(list(self.extra_read_roots)),
        )
        tools = [
            agent_tools.list_files,
            agent_tools.grep_files,
            agent_tools.read_file,
            agent_tools.skill_read,
            agent_tools.write_file,
            agent_tools.edit_file,
            agent_tools.new_skill_folder,
            agent_tools.commit_generate_main,
        ]
        agent = self.agno_agent_factory(instructions=[sysprompt], tools=tools)
        max_context = int(
            (self.llm_cfg or {}).get("max_context") or DEFAULT_MAX_CONTEXT
        )
        spill_limit = int(max_context * TRIM_TRIGGER_RATIO)
        compact_raw = (self.llm_cfg or {}).get("compact_token_limit")
        enable_spill = _bool_or_default(
            (self.llm_cfg or {}).get("enable_spill"), False,
        )
        if compact_raw in (None, ""):
            compact_limit = None
        elif enable_spill:
            compact_limit = max(int(compact_raw), spill_limit)
        else:
            compact_limit = int(compact_raw)
        user_msg = (
            f"发起人: {user_id}\n"
            f"指令: {instruction.strip()}\n"
            f"{_name_hint(preferred_names)}"
        )
        with trace_to(
            self._trace_path(user_id, job_id),
            append=True,
            spill_token_limit=spill_limit,
            compact_token_limit=compact_limit,
        ):
            result = agent.run(user_msg)
        return getattr(result, "content", "") or ""
