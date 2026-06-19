"""SkillEditAgent —— SKILL.md 自主整理 + git commit（baby→main 或 staging）
========================================================================

何时触发（``maybe_run`` 守门）：
  1. 该 skill 没有 staging 分支（灰度中不再触发新 SkillEdit）
  2. ``.candidates.yml`` 中所有 atom 的 ``weightscore`` 累加 ≥ 10
  3. 触发场景是"create staging"时（即 main 已存在），额外要求
     ``.ux_scores.jsonl`` 至少有 1 条 ``side=main`` 的真实评分——
     避免冷启动后 main 无人用就连开 staging 卡死灰度链路
  4. 调用方（watcher._check_pending_skill_edits）保证不在冷启动期触发

agent 写完 SKILL.md / scripts / references 后**必须**调以下两个 commit
工具之一（依当前分支状态）：
  - baby 分支：调 ``commit_baby_to_main(skill_name, message)`` 完成首版
  - main 分支：调 ``commit_to_staging(skill_name, message)`` 产出灰度候选

落盘成功（SKILL.md mtime 推进 + 非空）→ 清空 candidates buffer + 立即
install_to_claude_code 让 CC 立刻看到新版本。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from xskill.skill import candidates as C

logger = logging.getLogger("xskill.skill_edit_agent")


# ---------------------------------------------------------------------------
# 写作指导段（GUIDANCE）—— 白名单 / 反模式 / 泛化闸 / 参数化 / 失败挖掘 /
# 结构纪律 / 证据纪律 / 长度预算。这一段是**可切换**的：
#   - 默认（环境变量 XSKILL_SKILLEDIT_GUIDANCE_FILE 未设）：用下方 committed 文本，
#     与历史行为零差异。
#   - 若 XSKILL_SKILLEDIT_GUIDANCE_FILE 指向一个文件：用该文件内容**整体替换**本段，
#     管线契约段（场景块、SKILL.md schema、commit 工具协议、隐私守护、frontmatter
#     schema、工具清单、硬禁止）保持不动。
# 见 build_system_prompt() / _resolve_guidance()。
# ---------------------------------------------------------------------------
DEFAULT_GUIDANCE_BLOCK = """## 只允许写三类内容（白名单）

1. **领域规则**（domain rule）：该领域客观成立的事实与约束，写明机制原因。
   例式："X 会导致 Y，因为 Z；所以必须 W"。
2. **可复用工具**（tool）：参数化的脚本/代码片段，放 scripts/ 辅助文件；正文只写
   何时用、怎么调。**所有路径、文件名、单元格地址一律参数化**。
3. **坑位清单**（pitfall）：错误模式 → 症状 → 根因 → 修法，四元组俱全。

## 反模式（黑名单，出现即不合格）

- **任务流程复述**：把"读说明 → 写代码 → 跑验证"这类任何任务都一样的执行流程当内容写。
- **实例细节搬运**：把某次任务的具体文件名、具体数据值、具体题目要求抄进正文。
- **触发表述抄题面**：description 里引用的"典型触发表述"必须是**用户意图**的概括，
  不是任务提示词原文。

## 泛化自检闸（写完每条内容后必做）

自问："换一道同领域、不同题目的任务，这条还成立吗？"——不成立的删掉。
每条规则写明**适用条件**（什么场景下成立 / 不成立）。

## 参数化禁兜底

可复用脚本里所有路径、文件名、单元格地址一律走参数，**禁止任何具体值兜底**——
如 ``else 'somefile.xlsx'`` / ``path = path or 'input.xlsx'`` 这种硬编码默认值
写法一律算不合格，宁可让缺参直接报错。

## 失败轨迹是一等公民（成功教"怎么做"，失败教"哪里会死"，后者往往更值钱）

1. **死因回溯**：对每条失败轨迹，找到**死因**（结局信息里的失败原因，如评分读到了
   空值），向上回溯到导致它的具体动作，写成坑位四元组（错误模式 → 症状 → 根因 → 修法），
   根因必须解释**机制**（为什么这个动作导致这个结局），不许停在"做错了"层面。
2. **成败差分**：材料里存在"同一道题，一条过、一条挂"的对照时**必须**做差分——
   两条在哪一步分道扬镳、过的一侧做对了什么、挂的一侧做错了什么，写成一条带证据的
   领域规则（"同题对照：做 A 者过、做 B 者挂 → 必须 A 禁止 B"）。
3. **无症状死亡深挖**：agent 自信收工但结局是失败的轨迹，其根因往往是领域里最隐蔽、
   最值得沉淀的规则，优先深挖。
"""


# 写作指导的第二段（默认）：结构 / 证据 / 长度。env 切换时与上段一并被替换为空。
DEFAULT_GUIDANCE_BLOCK_2 = """# 正文结构纪律

正文四段顺序固定：``## 核心原则`` → ``## 领域规则`` → ``## 坑位清单`` → ``## 工具``。
- 核心原则：≤3 条，每条一句话 + 一句机制原因。
- 领域规则：逐条编号，每条三件套（规则本身 / 为什么（机制）/ 适用条件）。
- 坑位清单：表格，列为 错误模式 | 症状 | 根因 | 修法（坑位四元组）。
- 工具：每个 scripts/ 文件一行说明（何时用 + 调用方式 + 参数含义）。

# 证据纪律

每条规则 / 坑位末尾标注证据强度：``[实证：N 条轨迹]`` 或 ``[单例]`` 或 ``[推断]``。
仅靠 ``[推断]`` 支撑的内容总数 **≤ 2 条**。``[实证]`` 必须能指到具体 atom 证据。

# 长度预算（防灌水，但知识完整性优先）

- 正文总长 **≤ 200 行**。
- **删减顺序铁律**：超预算时先删可泛化性最弱的内容；带 ``[实证]`` 标注的规则和
  坑位四元组**不许为省行数而截断适用场景**——一条规则的全部适用场景必须写全，
  **宁可删掉整条弱规则，不许把强规则砍成半条**。
"""


SYSTEM_PROMPT_TEMPLATE = """你是 SkillEditAgent。某 skill 的 candidates buffer 累计
weightscore ≥ 10，需要你产出/更新它的 SKILL.md。

# 当前场景

{scenario_block}

# 你的目标

读 atom 内容（AtomTaskRead），必要时读 traj 原文（ReadTraj），从轨迹里
**提炼可泛化的知识**，写成 skill。skill 的价值 = 读它的人少踩多少坑、
少试多少次错，而不是把一次执行过程复述一遍。SKILL.md 是必产物，但你
**不限于**只写 SKILL.md——可以补充任何辅助文件，只要在 skill 目录范围内：

- ``<skill_dir>/SKILL.md`` — 必产物，frontmatter + body
- ``<skill_dir>/scripts/*.py`` / ``*.sh`` — 可机械执行、参数化的脚本
- ``<skill_dir>/references/*.md`` — 长参考材料（trace / 配置 / 文档摘录）
- ``<skill_dir>/templates/*`` 等任意子目录

{guidance_block}

# SKILL.md schema

```
---
name: <英文 slug>
description: <2-5 句中文：干什么、典型触发表述（引号引原文）、需要的工具/权限>
compatibility: <环境/版本/权限 + 至少一条负向硬约束>
metadata:
  version: <自增整数；新建从 1 开始，更新版本号+1>
  created: "<AUTO>"
  last_updated: "<AUTO>"
  source_atoms: ["atom_xxx_0001", ...]
---

# <中文标题>

<开头一段：这个 skill 解决什么>

## 核心原则
- <≤3 条。该领域最高优先级的不变式，每条一句话 + 一句机制原因。>

## 领域规则
1. <逐条编号。每条三件套——**规则本身 / 为什么（机制）/ 适用条件**。>
   末尾标证据强度：``[实证：N 条轨迹]`` / ``[单例]`` / ``[推断]``。

   > ⚠️ <坑位/警告：引用 atom 证据 "见 atom_xxx_0001" 或 "3/5 atoms 表明..."，
   > 不要凭空猜；末尾同样标证据强度。>

## 坑位清单
| 错误模式 | 症状 | 根因（机制） | 修法 | 证据 |
|---|---|---|---|---|
| <做了什么> | <表现> | <为什么会死> | <怎么改> | [实证：N 条] |

## 工具
- ``scripts/<file>`` — <何时用 + 调用方式 + 参数含义；路径/文件名全参数化>
```

{guidance_block_2}

# 写完文件**必须 commit**

写完所有文件后**必须**调以下其中一个工具完成版本化——否则改动只是工作目录
脏文件，watcher 会判定本次 SkillEdit 失败重试。

## 当前在 {branch_now} 分支，按场景调对应工具：

- **如果在 baby 分支**：调 ``commit_baby_to_main(skill_name, message)``
  这是该 skill 第一次出版本，graduate 到 main 分支。
- **如果在 main 分支**（已有 main，本次是更新）：调
  ``commit_to_staging(skill_name, message)`` 把更新作为灰度候选放进 staging。
  staging 会被灰度系统 vs main 对比，胜出才升级。

commit message 写明本次基于哪些 atom_id 整理，例如：
``"v2: 合并 atom_traj_x_0001/0003 的 zombie cleanup 步骤；新增 references/pidns_pitfall.md"``

# 可用工具
- AtomTaskRead(atom_id) — 读 atom JSON
- ReadTraj(traj_id, offset_start, offset_end) — 按行号取轨迹原文（offset 即 1-based 行号）
- SkillRead(skill_name) — 读现有 SKILL.md（更新场景用）
- list_files(path) — 列目录文件
- write_file(path, content) — 写任意文件到 skill_dir 下
- commit_baby_to_main(skill_name, message) — 仅 baby 分支可用
- commit_to_staging(skill_name, message) — 仅 main 分支可用（灰度路径）
- commit_update_main(skill_name, message) — main 分支冷启动在线进化：原地 commit 回 main

# 隐私守护

source atom / traj 原文来自真实开发轨迹，即便上传时已脱敏，仍可能残留
**敏感信息**——API key、token、密码、私钥、内网地址、个人邮箱/姓名等。
整理 skill 时：

- **绝不**把这些原样抄进 SKILL.md / scripts / references 任何产物。skill 要
  沉淀的是「做法」，不是「某次跑用的具体密钥」。
- 引用命令/配置/代码时，凡凭证位置一律用占位符——``API_KEY=<your-api-key>``、
  ``--token <TOKEN>``、``password: <password>``、``ssh user@<host>``。
- 看到 ``[REDACTED]``（上传脱敏留下的）保持原样，不要试图"还原"或编一个值。
- 拿不准某串是否敏感 → 一律当敏感处理、占位符化。

# 提交前质量闸（写完 SKILL.md，commit 前**必须逐条自检**，并把结论写进 commit message）

1. **价值自检**：这个 skill 替用户解决了什么具体问题——精简流程 / 发现问题 /
   解决问题 / 统计问题？在 commit message 里写出一句"替用户发现/解决了 X"。
   说不出具体价值的 skill 不该出版本。
2. **渐进式披露（progressive disclosure）**：description 只放"是什么 + 何时用"
   （触发信息）；执行细节、命令、判据进正文/辅助文件，**正文里不要再放触发判据**。
3. **无孤立脚本**：每个 ``scripts/`` / ``references/`` 下的脚本或辅助文件**必须**
   被 SKILL.md 正文引用并说明用途——没有任何正文引用的孤儿文件不合格，要么在
   正文里写清怎么用，要么删掉。
4. **description 可触发**：祈使语气（"Use this skill for…"而非"this skill does…"）
   + 聚焦用户意图 + 主动列出典型触发场景（防 undertrigger 漏触发）
   + 100–200 词 + 严格 <1024 字符。

（注：第 4 条最终由系统的硬编码 description 优化器在 commit 时兜底重写并按
held-out test 集选优；你只需先写个像样的初稿。）

# 硬禁止
- 不要随便引用没在 atom 中出现过的命令/函数；以 AtomTaskRead 为唯一可信来源
- 不要在描述里发明用户群体或场景
- 正文超长按上面的「长度预算」铁律删减；过长的参考材料拆到 references/ 或 scripts/
- 不要写 ``## trigger`` / ``## 触发条件`` 段——触发信号只在 frontmatter.description
  （坑位写进上面的 ``## 坑位清单`` 表格，规则附带的警告用 ``> ⚠️`` 内联 blockquote）
- **不要自己用 write_file 写 ``.git/`` 下文件**或 ``.candidates.yml``——前者会
  破坏 git 状态，后者是 cluster 的 buffer 由系统管理
"""


GUIDANCE_ENV = "XSKILL_SKILLEDIT_GUIDANCE_FILE"


def _resolve_guidance() -> tuple[str, str]:
    """返回 (guidance_block, guidance_block_2)。

    默认（env 未设）：committed 文本，行为零改变。
    若 XSKILL_SKILLEDIT_GUIDANCE_FILE 指向可读文件：用该文件内容整体替换写作
    指导段（block_1），block_2 置空（外部 guidance 文件自含全部结构/证据/长度规则）。
    env 指向不存在/读不了的文件 → 记 warning 并退回默认，绝不静默用空指导。
    """
    import os

    path = os.environ.get(GUIDANCE_ENV, "").strip()
    if not path:
        return DEFAULT_GUIDANCE_BLOCK, DEFAULT_GUIDANCE_BLOCK_2
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8").strip()
    except OSError as e:
        logger.warning(
            "%s=%r 读不了（%s）——退回默认 committed 写作指导段",
            GUIDANCE_ENV, path, e,
        )
        return DEFAULT_GUIDANCE_BLOCK, DEFAULT_GUIDANCE_BLOCK_2
    if not text:
        logger.warning(
            "%s=%r 内容为空——退回默认 committed 写作指导段", GUIDANCE_ENV, path
        )
        return DEFAULT_GUIDANCE_BLOCK, DEFAULT_GUIDANCE_BLOCK_2
    logger.info("SkillEdit 写作指导段由 %s 替换为 %s（%d 字符）",
                GUIDANCE_ENV, path, len(text))
    return text, ""


def build_system_prompt(scenario_block: str, branch_now: str) -> str:
    """组装 SkillEdit system prompt：管线契约段固定，写作指导段按 env 可切换。"""
    guidance, guidance2 = _resolve_guidance()
    return SYSTEM_PROMPT_TEMPLATE.format(
        scenario_block=scenario_block,
        branch_now=branch_now,
        guidance_block=guidance,
        guidance_block_2=guidance2,
    )


@dataclass
class SkillEditAgent:
    """每个实例服务**一个**具体 skill 子目录。"""
    skill_dir: Path
    store: Any  # AtomTaskStore (only needed by atom_task_read tool indirectly)
    agno_agent_factory: Callable[..., Any]
    llm_cfg: dict
    traj_root: Path
    threshold: int = C.ATOM_PROMOTION_THRESHOLD
    cold_flush: bool = False

    def maybe_run(self) -> bool:
        """检查所有守门条件 → 触发 agent → 验证落盘 → 清 buffer。

        守门顺序（任一失败即 return False）：
          1. 该 skill 有 staging 分支 → 灰度中，不触发
          2. candidates 累计 weightscore < threshold → 没攒够
          3. 触发场景是 "create staging"（main 已存在）：
             - .ux_scores.jsonl 必须至少有 1 条 side=main → 证明 main 真有人用
             - 否则保留 candidates 等用户用过 main 再触发

        全过 → 跑 agent → 验证 SKILL.md mtime 推进 + 非空 → 清 candidates。
        agent 没落盘 SKILL.md → 保留 candidates 等下轮重试（Bug 2 修复）。
        """
        from xskill.skill.git import current_branch, run_git

        # 守门 1: 灰度中（有 staging）不触发
        code, _, _ = run_git(["rev-parse", "--verify", "staging"],
                             cwd=str(self.skill_dir))
        if code == 0:
            return False
        # 守门 2: 阈值
        data = C.load_candidates(self.skill_dir)
        ready = C.ready_for_promotion_v2(data, threshold=self.threshold)
        if not ready:
            return False
        # 守门 3: 若场景是 "create staging"（即在 main 上）→ 额外要求 main 真有人用过
        cur = current_branch(str(self.skill_dir))
        if cur == "main":
            # cold_flush 在线进化：训练容器没真实用户 → 没有 ux_score；此模式下
            # 跳过守门 3，允许 main 技能基于新 epoch candidates 原地重新精炼。
            if not self.cold_flush and not self._main_has_ux_score():
                logger.info(
                    "skip SkillEdit: %s main 还没真实 ux_score，"
                    "保留 candidates 等用户用 main 后再产 staging",
                    self.skill_dir.name,
                )
                return False
        elif cur != "baby":
            logger.warning(
                "skip SkillEdit: %s 在异常分支 %r (期望 baby 或 main)",
                self.skill_dir.name, cur,
            )
            return False

        skill_md = self.skill_dir / "SKILL.md"
        mtime_before = skill_md.stat().st_mtime if skill_md.is_file() else 0.0
        size_before = skill_md.stat().st_size if skill_md.is_file() else 0

        try:
            self._run(ready, current_branch_name=cur)
        except Exception:
            logger.exception("SkillEditAgent _run failed: %s", self.skill_dir.name)

        # 实测落盘：mtime 推进 + 非空 = agent 真写了
        wrote = (
            skill_md.is_file()
            and skill_md.stat().st_size > 0
            and (
                skill_md.stat().st_mtime > mtime_before
                or skill_md.stat().st_size != size_before
            )
        )
        if not wrote:
            logger.warning(
                "SkillEditAgent ran but SKILL.md not written/empty: %s — "
                "保留 candidates 等下轮重试",
                self.skill_dir.name,
            )
            return False

        # 发布门兜底：write_file 已挡住非法 frontmatter，但 agent 可能绕开
        # write_file（或别的路径）落了坏 SKILL.md。commit 前再跑一次 parse_strict，
        # 非法 → 不清 buffer、标重试，绝不把坏 skill 静默发布出去。
        from xskill.skill.frontmatter import (
            parse_strict as fm_parse_strict,
            FrontmatterError,
        )
        try:
            fm_parse_strict(skill_md.read_text(encoding="utf-8"))
        except FrontmatterError as e:
            logger.warning(
                "SkillEditAgent 落了非法 SKILL.md: %s — %s；保留 candidates 重试",
                self.skill_dir.name, e,
            )
            return False

        # commit 工具的成功效应（baby→main 或 main→staging）通过当前分支变化
        # 自然反映，不需要在这里做额外检查
        C.clear_candidates(self.skill_dir)
        logger.info("SkillEditAgent done + candidates cleared: %s",
                    self.skill_dir.name)
        return True

    def _main_has_ux_score(self) -> bool:
        """检查该 skill 的 .ux_scores.jsonl 是否有至少 1 条 side=main 记录。

        冷启动后没人用过 main 时，避免立刻产生 staging 卡死灰度链路（B 守门）。
        """
        from xskill.canary import load_ux_scores
        try:
            scores = load_ux_scores(self.skill_dir)
        except Exception:
            return False
        return any(s.get("side") == "main" for s in scores)

    def _run(self, ready: list[dict], current_branch_name: str) -> None:
        from xskill.agents import skill_tools as ST
        from xskill.skill.frontmatter import parse as fm_parse

        # 构造 scenario_block + branch_now 给 prompt 用
        skill_md = self.skill_dir / "SKILL.md"
        scenario_lines: list[str] = []
        if current_branch_name == "baby":
            scenario_lines.append(
                "skill_name: " + self.skill_dir.name + "（**baby 分支**——首次出版本）"
            )
            scenario_lines.append(
                "写完 SKILL.md 后调 ``commit_baby_to_main(skill_name, message)`` "
                "graduate 到 main 分支。"
            )
        elif self.cold_flush:
            # 冷启动在线进化：main 上的技能基于"现有正文 + 新 epoch candidates"
            # 原地重写，直接 commit 回 main（不开 staging / 不走灰度）。
            scenario_lines.append(
                "skill_name: " + self.skill_dir.name
                + "（**main 分支 · 冷启动在线进化** —— 原地更新现有 skill）"
            )
            scenario_lines.append(
                "先用 ``skill_read(skill_name)`` 读现有 SKILL.md 正文，**在现有"
                "正文基础上**融合本轮新 candidates 的 atom 知识重写正文（version "
                "号 +1），写完后调 ``commit_update_main(skill_name, message)`` "
                "**直接 commit 回 main**——这是本 epoch 的在线进化，不开 staging、"
                "不走灰度。"
            )
        else:
            scenario_lines.append(
                "skill_name: " + self.skill_dir.name + "（**main 分支** —— 更新现有 skill）"
            )
            scenario_lines.append(
                "写完 SKILL.md 后调 ``commit_to_staging(skill_name, message)`` "
                "把更新作为灰度候选 commit 到 staging。"
            )

        # 现有 SKILL.md 是 stub (baby 时) 或正式版 (main 时)
        if skill_md.is_file():
            try:
                fm, _ = fm_parse(skill_md.read_text(encoding="utf-8"))
                cur_desc = (fm.get("description") or "").strip().replace("\n", " ")
                cur_ver = (fm.get("metadata", {}) or {}).get("version", "?")
                scenario_lines.append("")
                scenario_lines.append(f"现有 SKILL.md description: {cur_desc[:200]}")
                scenario_lines.append(f"现有 SKILL.md version: {cur_ver}")
            except Exception:
                pass
        scenario_lines.append("")
        scenario_lines.append("# 待整理 candidates（按 weightscore 倒序）")
        for c in sorted(
            ready, key=lambda x: x.get("weightscore", 0), reverse=True,
        ):
            note = c.get("note", "")
            ext = f"  note: {note}" if note else ""
            scenario_lines.append(
                f"- atom_id={c['atom_id']}  weightscore={c['weightscore']}{ext}"
            )
        scenario_lines.append("")
        scenario_lines.append(f"目标 skill 目录: {self.skill_dir}")
        scenario_lines.append(f"目标 SKILL.md 路径: {skill_md}")

        scenario_block = "\n".join(scenario_lines)
        sysprompt = build_system_prompt(
            scenario_block=scenario_block,
            branch_now=current_branch_name,
        )
        user_msg = scenario_block  # 同时也作为 user 消息（agno 两端都看）

        agent = self.agno_agent_factory(
            instructions=[sysprompt],
            tools=[
                ST.atom_task_read,
                ST.read_traj,
                ST.skill_read,
                ST.list_files,
                ST.write_file,
                ST.commit_baby_to_main,
                ST.commit_to_staging,
                ST.commit_update_main,
            ],
        )
        # 逐轮 CoT/工具调用 → logs/agents/skill_edit_agents/skills/<skill>_<ts>.log
        import time as _time
        from xskill.agents.agent_trace import trace_to
        from xskill.config import get_logs_dir
        _ts = _time.strftime("%Y%m%d-%H%M%S")
        sink = (get_logs_dir() / "agents" / "skill_edit_agents" / "skills"
                / f"{self.skill_dir.name}_{_ts}.log")
        with trace_to(sink):
            agent.run(user_msg)
