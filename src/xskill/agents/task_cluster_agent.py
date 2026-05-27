"""TaskClusterAgent —— AtomTask → skill 归类
============================================

输入：单个 AtomTask（由 watcher / process 喂进来）
副作用：往一个或多个 skill 的 .candidates.yml 写 atom 贡献（含 weightscore 0-10）；
       如有必要先 new_skill_folder 建空 skill 目录。

sysprompt 设计
==============
把当前 skill_dir 下所有 skill 的 ``name: description`` 注进去作为路由表，让
cluster agent 决定 atom 归到哪个已存在 skill 或新建。预算总额约
20% × LLM context window，参考 CC 的做法（``analysis/04c-skills-implementation.md``）。

budget 控制（``build_skill_catalog_block``）：
- name 不限：超 budget 也全部保留（otherwise 模型完全看不到候选）
- 剩余预算 / 条数 ≥ 75 字符（≈25 token） → desc 按 min(per_desc, 300) 截断
- 剩余预算 / 条数 < 75 字符 → 全部丢 desc 只留 ``- <name>``
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from xskill.pipeline.atom import AtomTask, AtomTaskStore
from xskill.skill.frontmatter import parse as fm_parse

logger = logging.getLogger("xskill.task_cluster_agent")


_DESC_MIN_CHARS = 75   # 约 25 token；低于这个值就只留 name
_DESC_HARD_CAP = 300   # 单条 desc 上限（即使预算够也别全塞）


def _scan_skill_state(skill_dir: Path) -> list[tuple[str, str, str]]:
    """扫所有 skill 子目录，返回 ``[(name, state, desc), ...]``。

    state 用 git 分支作为单一事实源（不再读 .meta.yml）：
    - ``baby``: 仅 baby 分支存在（cluster 刚创建，SkillEditAgent 还没跑过）
    - ``main``: main 分支存在 + 无 staging
    - ``staging``: main + staging 双分支
    - ``unknown``: 没 .git（异常状态，理论上不该出现）

    desc 从该 skill 的 SKILL.md（baby 分支也有 stub SKILL.md）frontmatter
    取，确保 cluster agent 看到的 desc 是 cluster 自己当初在 new_skill_folder
    时填的语义边界——和 main/staging 状态 desc 信息密度对等。
    """
    from xskill.skill.git import run_git
    out: list[tuple[str, str, str]] = []
    if not skill_dir.is_dir():
        return out
    for d in sorted(skill_dir.iterdir()):
        if not d.is_dir() or d.name.startswith("."):
            continue
        name = d.name
        if not (d / ".git").is_dir():
            # 没初始化 git——历史遗留或异常
            out.append((name, "unknown", "(skill 无 git 仓库)"))
            continue

        # 用 git branch --list 判 state
        _, branches_out, _ = run_git(["branch", "--list"], cwd=str(d))
        branches = {
            line.lstrip("* ").strip()
            for line in branches_out.splitlines() if line.strip()
        }
        if "staging" in branches:
            state = "staging"  # 包含 main+staging 情况；staging 中的是新候选
        elif "main" in branches:
            state = "main"
        elif "baby" in branches:
            state = "baby"
        else:
            state = "unknown"

        # 读当前 SKILL.md frontmatter 取 desc（baby/main/staging 的 SKILL.md
        # 都至少含 stub frontmatter）
        desc = ""
        skill_md = d / "SKILL.md"
        if skill_md.is_file():
            try:
                fm, _ = fm_parse(skill_md.read_text(encoding="utf-8"))
                desc = (fm.get("description") or "").strip().replace("\n", " ")
            except Exception:
                pass

        # baby 状态额外拼 buffer 计数让 agent 看到"还差多少分到阈值"
        if state == "baby":
            n_cand = 0
            cand_yml = d / ".candidates.yml"
            if cand_yml.is_file():
                try:
                    import yaml
                    data = yaml.safe_load(cand_yml.read_text(encoding="utf-8")) or {}
                    n_cand = len(data.get("candidates", []) or [])
                except Exception:
                    pass
            desc = f"{desc} ({n_cand} cand in buffer)" if desc else f"({n_cand} cand)"
        out.append((name, state, desc))
    return out


def build_skill_catalog_block(skill_dir: Path, max_chars: int) -> str:
    """构造 sysprompt 中的 skill 路由表块。

    展示**所有** skill 目录（含 wip 空骨架），让 cluster agent 看到完整画
    像——避免对近义场景反复 ``new_skill_folder`` 创建近义 slug。

    每行格式：``- <name>[<state>]: <desc>``
    state 有 main / staging / main+staging / wip 四种。

    无 skill 时返回 ``(no skills yet)``。
    """
    entries = _scan_skill_state(skill_dir)
    if not entries:
        return "(no skills yet)"

    # name 头永远完整保留；desc 按预算压
    head_lines = [f"- {n}[{s}]" for n, s, _ in entries]
    head_cost = sum(len(l) + 1 for l in head_lines)
    remaining = max_chars - head_cost
    per_desc = remaining // len(entries) if entries else 0

    if per_desc < _DESC_MIN_CHARS:
        return "\n".join(head_lines)

    cap = min(per_desc, _DESC_HARD_CAP)
    out_lines: list[str] = []
    for n, s, d in entries:
        if not d:
            out_lines.append(f"- {n}[{s}]")
        else:
            truncated = d[:cap] + ("…" if len(d) > cap else "")
            out_lines.append(f"- {n}[{s}]: {truncated}")
    return "\n".join(out_lines)


SYSTEM_PROMPT_TEMPLATE = """你是 TaskClusterAgent。我会给你一个 AtomTask（用户的一段
完整意图 + agent 的执行复盘）；你决定它是否值得被某个 skill 收录，应该归到哪个
已有 skill（用 add_task_to_skill），或者应该新建一个 skill 容纳它（用
NewSkillFolder 再 add_task_to_skill）。

# 可用工具
- AtomTaskRead(atom_id) — 读 atom 完整 JSON（intent / summary / raw_segment 全字段）
- AtomTaskSearch(query) — 混合检索其他 atom（语义向量 + BM25 关键字 union）
- ReadTraj(traj_id, offset_start, offset_end) — 按行号读 traj.md 原文片段（offset 即 1-based 行号）
- SkillRead(skill_name) — 读 skill 的 SKILL.md（baby 返回 stub，main/staging 返回正版）
- ReadSkillTasks(skill_name) — **看某 skill 的 candidates buffer 内已有哪些 atom**
  （和 SkillRead 不同——SkillRead 看 SKILL.md，ReadSkillTasks 看正在攒分的 atom
  列表）。判断"该不该把当前 atom 也归到这个 baby"的关键工具。
- NewSkillFolder(skill_name, description) — 新建 baby 分支 skill。description
  必填（2-3 句中文）。**最后才考虑**——能复用就别开新的。
- add_task_to_skill(skill_name, atom_id, weightscore) — 把 atom 加进 buffer。
  weightscore 1-10 整数。累计满 10 触发 SkillEditAgent 写 SKILL.md。
- RenameSkill(old_name, new_name) — **仅 baby 状态可改名**。合并近义 slug 的关键
  工具：发现两个 baby 同义但 new_name 还没存在 → 把 less-specific 的改成
  more-specific。如果 new_name 已存在 → 用 MoveTaskTo 而不是 RenameSkill。
- MoveTaskTo(skill_from, skill_to, atom_id) — 把 atom 从一个 buffer 移到另一个。
  合并近义 baby 的第二步：两个 baby 都已存在 → MoveTaskTo 把 atom 全搬到主 slug。
  之后 from baby 空 buffer 但仍存在（保留以防后续 cluster 又往里写）。
- score_task(atom_id, score) — 修改 atom 自身的 ux_score

# 当前可见的 skill 路由表
格式：``- <name>[<state>]: <desc>``，state ∈ {{baby, main, staging}}：
- ``baby`` = 刚被 cluster 创建的草稿，SKILL.md 是 stub；candidates 在 buffer 攒分
  中。如果新来的 atom 跟它的 desc 同类，**优先复用** baby 加分而不是新建近义 slug。
- ``main`` = 已正式产出的 skill 主版本（消费者 agent 可加载）
- ``staging`` = main + 灰度候选并存（注意此状态下 SkillEditAgent 暂停在该 skill
  上触发——但你 cluster 仍可继续 add_task_to_skill 累积 buffer，灰度结束后会消费）

{skill_catalog}

# weightscore 严格分档表
# 永远质量驱动，不要凑条数。**每个 atom 都必须 add_task_to_skill；任何分数都不允许直接 return**。

  10  单 atom 就强支撑该 skill 核心场景。罕见——仅当 atom 含可机械执行、
      跨多类相似问题都成立的修复决策时才给。立即触发 SkillEdit。

  8-9 高质量：atom 完整覆盖该 skill 的关键阶段，含具体命令/路径/函数名
      + 可核验产出 + 用户成功反馈。两个 8 分相加即触发 SkillEdit。

  6-7 中等：atom 在该 skill 范围内但只覆盖一个子阶段或单个 warning；
      需要别的 atom 补齐才完整。

  4-5 弱：atom 提到该 skill 的相关问题，但执行细节模糊或不完整；
      具体命令/路径不清，产出难以核验。

  2-3 边缘：atom 与 skill 同领域但停留在概念/语境层面。典型形态：
      - 用户 query 里提到术语后马上转向别的话题
      - 任务铺背景时引用一句该 skill 的关键词
      - "X 是什么"这类概念性问答，非操作性步骤
      - session 中途偏题瞄一眼然后回主线
      特征：没有具体命令/路径/可核验产出，没有跨场景成立的决策模式。
      仍要 add_task_to_skill。

  1   atom 跟路由表所有 skill 都没明显交叉。挑 desc 最不远的那个强 add，
      weightscore=1。不要为 ws=1 atom 新开 skill（守住"≥7 才新建"门槛）。

# 处理流程（v2.2 重点：复用 > 整合 > 新建）

## Step 1: 看路由表 + 搜相似 atom
- 路由表里所有 baby/main/staging skill 全看一遍，重点找 desc 同类的
- AtomTaskSearch 找语义/关键字相似 atom，看它们归在哪些 skill 上

## Step 2: 复用判断
- 找到 1 个 desc 精准匹配的 → 直接 add_task_to_skill（流程结束）
- 找到 ≥2 个 desc 同类的 baby（近义 slug 泛滥）→ 进入"整合"步骤
- 没找到合适候选 → 跳到 Step 4

## Step 3: 整合近义 baby（用 RenameSkill / MoveTaskTo）
- ReadSkillTasks 查每个 baby 的 buffer 里都有什么 atom——判断哪个 slug 是"主"
- 选 desc 最精准的 baby 当主 slug
- **场景 A**：主 slug 名字还不存在 → 把次要 baby 用 RenameSkill 改成主 slug
- **场景 B**：主 slug 名字已被另一个 baby 占用 → 用 MoveTaskTo 把次要 baby
  的 atom 全搬到主 slug。次要 baby 留下空 buffer（不删，避免后续 cluster
  又往里塞重复 atom）
- 整合完再 add_task_to_skill 把当前 atom 加进主 slug

## Step 4: 实在没合适候选 → NewSkillFolder
- **门槛**：单 atom weightscore < 7 不要新建（防污染 skill 列表）
- description 必填，2-3 句中文写清服务于什么类型的 atom

## Step 5: 边缘 atom 兜底（**绝不允许直接 return 不调工具**）
- weightscore 2-3：仍要 add_task_to_skill 把 atom 灌进 desc 最贴近的 skill；
  ws=2/3 不会单独触发 SkillEdit（buffer 阈值 10），但 atom→skill 归属必须留底
- weightscore 1：跟路由表所有 skill 都没明显交叉时，挑 desc 最不远的那个 add，
  weightscore=1；**不要**为 ws=1 atom 新开 skill（守住"≥7 才新建"门槛）
- 任何分数都不允许"什么都不调，直接说明理由结束"——atom 不能静默蒸发

# 渐进收敛策略

冷启动期间 cluster **串行**跑（系统在 watcher 层强制 max_concurrent=1），你
**逐条**看到 catalog 演化，每条新 atom 都能看见上一条 cluster 的产物。这避免
了并发创建近义 slug。

# 硬禁止
- 不要为了"做点事"乱打高分。低质 atom 就别加，会污染 candidates 触发劣质 skill。
- 不要伪造 atom_id；只用我给的或 AtomTaskSearch 返回的真实 id。
- 不要直接写 SKILL.md——那是 SkillEditAgent 的职责。
- RenameSkill 只对 baby 用；main/staging 工具会拒绝。
- 两个 baby 都已存在时 → 用 MoveTaskTo 而不是 RenameSkill（避免冲突）。
"""


@dataclass
class TaskClusterAgent:
    skill_dir: Path
    store: AtomTaskStore
    agno_agent_factory: Callable[..., Any]
    llm_cfg: dict
    tools: list
    # ~20% of 128k token context window（保守起点；DeepSeek v4-flash 实际容量
    # 更大，按 plan 用 20% 作为软门槛）
    sysprompt_budget_chars: int = 25000

    def process(self, atom: AtomTask) -> str:
        """跑一次 cluster 决策，返回 agent 的 final content（日志用）。"""
        catalog = build_skill_catalog_block(
            self.skill_dir, self.sysprompt_budget_chars,
        )
        sysprompt = SYSTEM_PROMPT_TEMPLATE.format(skill_catalog=catalog)

        user_msg = (
            f"待分类 AtomTask:\n"
            f"  atom_id:   {atom.atom_id}\n"
            f"  traj_id:   {atom.traj_id}\n"
            f"  intent:    {atom.intent}\n"
            f"  summary:   {atom.summary}\n"
            f"  tags:      {atom.tags}\n"
            f"  used_skills (agent 自报): {atom.used_skills}\n"
            f"  ux_score:  {atom.ux_score}\n"
            f"  lines:     [{atom.offset_start}..{atom.offset_end}) (1-based 行号)\n\n"
            f"按系统指令处理这个 atom，做出归类决策。"
        )

        agent = self.agno_agent_factory(
            instructions=[sysprompt],
            tools=self.tools,
        )
        result = agent.run(user_msg)
        return getattr(result, "content", "") or ""
