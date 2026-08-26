"""GenerateAgent —— 用户点名、直接写主干的 skill 生成代理。

和 SkillEditAgent 同类（Agno + 同一套工具上下文），但由
``xskill generate`` 启动，不靠候选缓冲攒分。读轨迹先预览卡再精读，
进度写进磁盘 wiki；写完用 ``commit_generate_main`` 落到 main。
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

# 怎么读轨迹（省上下文，先预览再精读）

会话很多，禁止 list_files 或 grep_files 去扫 team_trajectories、sessions。
那会把几百个文件名和命中行倒进上下文，上一轮就是这样烧掉一百多万 token，
却只精读了几条。

session_card / session_cards 只是预览，很省上下文：
- 有用户 query 和行号 L
- 有截断的 toolcall 参数（command、path 等收短）
- 没有 tool result，没有完整回传，没有原始 json
看过卡片只知道「大概在干什么、该从哪一行精读」，不算读懂这条轨迹。
不要把只看过卡片的 traj_id 写进 survey，也不要写进 SKILL.md 的依据。

步骤（箭头串起来）：

扫面→预览卡→写计划→精读⇄更新wiki→写skill（卡片只预览；commit 至少 10 条）

精读和更新 wiki 可以小循环几轮：读一批 → 改 read-plan 状态 → 把要点写入 survey → 把做法或坑写入 knowledge → 页顶更新「必要信息是否读完」。必要信息未读完就继续按计划精读，不要急着 new_skill_folder。

list_sessions 翻页；session_cards 一次最多 10 条。立刻写 read-plan（默认至少计划 20 条，commit 至少精读 10 条）。按卡片 path 和 L 做 read_file。压缩后先读 plan、survey、knowledge，只补未读，不准重扫目录。依据只引用精读过的 traj_id。

知识一旦写进 SKILL.md，同一手 wiki_write knowledge，把「已入 skill」改成是，并注明章节。commit 前再 wiki_read 核对：必要信息已读完，且入 skill 的知识都有标注。

# 怎么改文件

THINK 里一旦想到要改某个已有文件，下一手不要整文件 write_file。本趟
new_skill_folder 刚建出来的 stub SKILL.md、刚 write_file 过的脚本、磁盘上
已有的 skill，都算已有文件。按这个顺序做：

1. 用 list_files 或 skill_read 看目录里有哪些文件
2. 用 read_file 或 skill_read 读到要改的原文（辅助文件必须单独 read_file；
   skill_read 只算读过 SKILL.md）
3. 用 edit(path, old_string, new_string) 只替换那一处；old_string 必须在
   文件里唯一

write_file 只用于：文件还不存在、SKILL.md 仍是 init stub、或现有正文必须
整篇改写。已经有正式正文之后只 edit 有差异的段落，禁止无故全篇覆盖。
edit 前必须先读过该文件，否则工具会报错。

相对路径按当前 skill 仓解析：SKILL.md、scripts/foo.py。不要加 ./skill/
或技能文件夹名当第一段。

# 怎么写

- 新建：先 new_skill_folder(name, description)，它会建目录并放下 stub
  SKILL.md。填正文用 edit（stub 可一次 write_file 换成正式稿）。新脚本
  还不存在时才 write_file。
- 本趟已经 new_skill_folder 过的名字就是你自己的半成品，不是别人的 skill。
  压缩上下文之后仍以「本趟已执行动作」为准：那个目录的 stub SKILL.md
  只说明你还没写完，继续 edit，最后 commit_generate_main。
  不要再调一次 new_skill_folder，也不要另开一个近义名字的新目录。
- 改已有 skill：先 skill_read，再 edit。不要整篇 write_file 覆盖丢掉已有内容。
- 写完必须调用 commit_generate_main(skill_name, message)。它会把结果提交到
  main：没有 main 就创建，目录几乎是空的也允许提交。不要调用任何灰度
  （staging）提交工具。
- commit message 写清你新建还是改了哪个 skill、依据是什么。系统会自动在
  前面加上发起人 ID。
- 轨迹里若有密钥、token、密码、内网地址，不要原样写进 skill，用占位符。

# 可用工具

- list_sessions(offset=0, limit=60, query="")：会话目录，一行一条，不是正文
- session_card(traj_id) / session_cards(traj_ids)：预览卡，一次最多 10 条，不算精读
- wiki_status / wiki_read / wiki_write / wiki_search / wiki_log
- list_files(path)：只列 skill 目录或 spill。会话目录用 list_sessions
- grep_files(pattern, path="", glob="", max_results=100)
- read_file(path, offset=1, limit=200)：精读。会话请带卡片上的 path 和 L
- skill_read(skill_name)
- write_file / edit / new_skill_folder / commit_generate_main
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
        from xskill.agents import llm_wiki, session_catalog

        tools = [
            session_catalog.list_sessions,
            session_catalog.session_card,
            session_catalog.session_cards,
            llm_wiki.wiki_status,
            llm_wiki.wiki_read,
            llm_wiki.wiki_write,
            llm_wiki.wiki_search,
            llm_wiki.wiki_log,
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
        from xskill.obs.tracing import setup

        setup()
        with trace_to(
            self._trace_path(user_id, job_id),
            append=True,
            spill_token_limit=spill_limit,
            compact_token_limit=compact_limit,
        ):
            result = agent.run(user_msg)
        return getattr(result, "content", "") or ""
