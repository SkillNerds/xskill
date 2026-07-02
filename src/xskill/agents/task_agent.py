"""TaskAgent —— 弃窗单趟 agentic AtomTask 拆分器
================================================================================

输入：一条 ``traj.md`` + ``AtomTaskStore`` + 一个 agno agent 工厂。
输出：把 LLM（通过 ``submit_atom`` 工具）拆分得到的 AtomTask 落盘到 store，
前后 atom 链表化（含与 ``store.last_atom_id()`` 的衔接）。

设计要点（弃窗单趟）
====================

1. **坐标系是行号,不是字符 offset**。AtomTask 的 ``offset_start`` /
   ``offset_end`` 存 1-based 行号,半开区间 [start, end)（end 这一行不含；
   末 atom 的 end = 末行号 + 1）。轨迹一旦入库就不再变,行号稳定。

2. **弃窗：一趟把整条轨迹拆完**。不再按字符截窗逐窗循环。context-0 只放
   "带行号的全轨迹 User 提问地图"（``_extract_user_queries``）+ 元信息 +
   续拆衔接块；assistant 正文**不进 context-0**,agent 用 ``look`` 工具按需读。
   这样既省 token,又从根上消灭"超长意图被字符硬切"和"超长前言那一窗无 User
   导致整条静默漏拆"两类窗机制硬伤。

3. **EOF 覆盖硬校验（代码兜死,不靠 agent 自律）**：
   - 首原子 ``offset_start = 1``（把首个 User 前的前言并入）。
   - 末原子 ``offset_end = total_lines + 1``（强制盖到 EOF）。
   - 有 User 轮却 0 提交 → **抛错**（不静默产空）。
   - 落盘后断言区间无缝无叠地铺满 ``[resume_line, total+1)``。

4. **容噪过滤（F0）**：某 ``## User`` 块整块匹配纯机器签名（40-hex SHA /
   ``HH:MM:SS [tag]`` 日志 / 独立 JSON / ls 表）且无用户指令特征时,不当作
   拆分边界（确定性,零 LLM）——见 ``_is_machine_noise_block``。

5. **提交即校验,不解析 XML**（不写 fallback）。``submit_atom`` 工具在提交时
   校验：start_line 必须是真实 ``## User`` 行、必须 ≥ 续接点、必须严格大于
   上一条；不合法直接返回 error 字符串让 agent 自改。

6. **增量续拆（续写场景）**：``resume_line = last_offset``；agent 只对
   ``≥ resume_line`` 的新意图 ``submit_atom``,prior 块锚定,不全量重拆。

7. **绝不收静默空**：``agent.run()`` 后查 ``run_response.status``,error 即抛。
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from xskill.config import interests_config
from xskill.pipeline.atom import AtomTask, AtomTaskStore

logger = logging.getLogger("xskill.task_agent")


def _sidecar_model(traj_path: Path) -> str:
    """读 ``<traj>.md`` 同名 ``.json`` sidecar 的 ``model``；无则空串。"""
    jp = traj_path.with_suffix(".json")
    if not jp.is_file():
        return ""
    try:
        return str(json.loads(jp.read_text(encoding="utf-8")).get("model") or "")
    except (OSError, json.JSONDecodeError):
        return ""


class TrajectoryNotFit(RuntimeError):
    """Raised when TaskAgent marks a trajectory unrelated to configured interests."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


SYSTEM_PROMPT = """你是 AtomTask 拆分员。给你一条 agent 与用户的对话轨迹（markdown）的
"用户提问地图"（每个 ``## User`` 回合的行号 + 首句摘要），你的任务是按
"用户意图切换"把整条轨迹切成 0~N 个 AtomTask。每个 AtomTask 是一段完整的
用户意图 episode——可以被独立检索、被独立提炼成 skill 的最小素材。

弃窗单趟（重要）
================
- 我**不会**把 assistant 正文塞进上下文,只给你"用户提问地图"。需要看某个
  回合附近的 assistant 原文（判"新意图 vs 同一意图的追问"）时,用 ``look`` 工具
  按行号去读。
- 一趟拆完整条轨迹：对每个新意图调一次 ``submit_atom`` 报 start_line。不要
  分批、不要只拆开头。

切分原则（按优先级）
====================
1. 只在**用户意图切换**处切。"意图切换"指用户从一个目标/任务/问题转到另一个：
   - "再帮我改一下前端" → 前后是两个 atom
   - "另外/接下来/对了" 这类衔接词后跟新动作 → 切
   - 同一目标的多轮调整（澄清、修正、加细节、催促）→ **不切**,留在同一 atom
2. **撤销/反悔不切**：用户说"算了/撤销/还是按原来的/不要了"回到上一个意图时,
   不是新意图,**不要**为这个撤销另起 atom——它属于被撤销那个意图的同一段。
   拿不准就用 ``look`` 读 assistant 看 agent 实际做了什么再判。
3. **不要**因为 tool 切换、代码块出现、子目录变化、agent 自我更正等结构性事件
   而切——避免过度拆分。
4. 如果整条轨迹只完成一件事,输出 1 个 atom 即可。不要硬凑数量。

边界与坐标（重要）
==================
- 用户提问地图里每个 ``## User`` 回合都给了行号。AtomTask 的边界**只能**落在
  这些 ``## User`` 行。
- 一个 atom = 从它的起始 ``## User`` 行,到下一个 atom 的起始 ``## User`` 行
  之前为止（**左闭右开**）。最后一个 atom 一直到轨迹末尾——你不用报终点。
- 你**只报每个 atom 的起始行号 ``start_line``**,终点自动推导。
- 多个 atom 的 ``start_line`` 必须严格递增。

增量重拆（续写场景）
====================
- 元信息里会给你"上次拆分到（续接点）resume_line"。续接点之前的内容**已经
  拆过 atom**,列在"本轨迹已经拆分的 Atom"里——**不要重复拆这部分**。
- 你只对续接点**之后的新增内容**切分。所有 submit_atom 的 start_line 必须
  ≥ 续接点。

可用工具
========
- ``look(line, before, after)`` —— 读某行附近的轨迹原文（含向前看 after 行,
  判"新意图 vs 追问"的主力）。before 默认 40、after 默认 20。
- ``submit_atom(start_line, intent, summary, tags, used_skills, ux_score)``
  —— 提交一个 atom。**提交即校验**：start_line 须是真实 ``## User`` 行、
  须 ≥ 续接点、须严格大于上一条；不合法返回 error,请改正后重提。
- ``context_budget()`` —— 返回已用 / 上限 / 剩余 token,自查上下文压力。
- ``my_atoms()`` —— 返回本轮已提交 atom 的行号区间,自查进度/覆盖。

字段含义（submit_atom 各参数）
==============================
- start_line（整数,真实 ``## User`` 行号；本 atom 从这里开始）
- intent（≤40 字,目标）
- summary（≤200 字,复盘：根因、关键动作、产出、验证）
- tags（3-5 个,小写下划线）
- used_skills（这段对话里 agent 实际触发了哪些 skill 名；没有就传空列表）
- ux_score（1~10 整数；只评这段 atom 的用户体验,不评整条 traj）

ux_score 严格分档表
====================
**永远不要凭"做了多少事"或"步数"打分。质量驱动,参考表如下：**

  10 一次到位：用户提需求 → agent 一步给出正确产出 → 用户接受无澄清/无负面情绪。
   9 接近一次到位：仅一处细节澄清,后续顺畅。
   8 正确完成但绕了 1 个小弯（多读一个文件、命令小修一次）,用户基本满意。
   7 正确完成,但用户做了 2-3 次澄清/修正；无明显不耐烦。
   6 完成度边界——用户对结果勉强可用,但已表达"这就行吧"之类不完全满意。
   5 部分完成：核心需求达成,但遗漏明显细节；用户不得不补一次说明。
   4 多次错误后才接近正确：用户已用否定词（"不对"/"错了"）2 次以上。
   3 任务勉强算完成但用户明显失望（"算了"/"我自己来"）,或 agent 在关键
     步骤上误判方向但侥幸跑通。
   2 任务未完成 / agent 反复触发 blocker / 用户明显放弃。
   1 完全失败 / 引发副作用（删错文件、改坏代码、误推送等）。

判 ux_score 时同时看：
- 这段 atom 是否真正调用了某个 used_skill；若调了 skill 且导致绕弯/错误 → 直接降到
  ≤5；若调了 skill 且一步到位 → 至少 8 起步。
- 这段 atom 的产出在用户后续 turn 是否被否定 / 撤销 / 重做。

工作流程
========
1. 读"用户提问地图",按用户意图切换决定每个 atom 的起点。
2. 拿不准某个回合是"新意图"还是"上一意图的追问/撤销"时,用 ``look`` 读那行附近
   的 assistant 原文再判。
3. 对每个新 atom 调一次 submit_atom（提交即校验,error 就改了重提）。完成后结束。
"""


INTEREST_FILTER_SECTION_TEMPLATE = """\

兴趣过滤（重要）
================
本次运行配置了兴趣列表：
{interests_block}

在拆分前先判断**整条轨迹**是否与这些兴趣相关。判断标准：
- 只要轨迹中有一部分能为这些兴趣之一提供可复用经验，就继续正常 submit_atom。
- 如果整条轨迹都无关，调用 ``mark_not_fit(reason)``，说明原因，然后结束。
- 调用 ``mark_not_fit`` 后不要再调用 ``submit_atom``。
- 已经调用过 ``submit_atom`` 后，不允许再调用 ``mark_not_fit``。
"""


WHOLE_SYSTEM_PROMPT = """你是 AtomTask 拆分员（**整轨模式 / whole**）。给你一条 agent 与用户的
对话轨迹（markdown）的"用户提问地图"（每个 ``## User`` 回合的行号 + 首句摘要）。

整轨模式（重要——与默认按意图切分相反）
========================================
- 你的任务是**把续接点（resume_line）之后的整条新增轨迹当作恰好 1 个
  AtomTask**——**只调一次 ``submit_atom``**，``start_line = resume_line``
  （首轮 resume_line=1）。这一个 atom 从续接点一直覆盖到 EOF（末行）。
- **绝不**按用户意图切分、**绝不**产出多个 atom。哪怕地图里有多个 ``## User``
  回合、多个不同意图,也全部并进这唯一一个 atom。
- 续写场景：若续接点之前已经拆过 atom（列在"已经拆分的 Atom"里）,你这次仍只对
  **续接点之后的新增段**产 1 个 atom（start_line = resume_line）,不重拆旧段。

边界与坐标（重要）
==================
- ``start_line`` **必须等于续接点 resume_line**（地图里 ≥ resume_line 的第一个
  合法 ``## User`` 行；首轮通常是第 1 个 User 回合,代码会把它前面的前言并入）。
- 终点自动推导到 EOF,你不用报终点。

可用工具
========
- ``look(line, before, after)`` —— 读某行附近的轨迹原文（判 agent 实际触发了
  哪些 skill、整体 UX 如何）。before 默认 40、after 默认 20。
- ``submit_atom(start_line, intent, summary, tags, used_skills, ux_score)``
  —— 提交那唯一一个 atom。**提交即校验**：start_line 须是真实 ``## User`` 行、
  须 ≥ 续接点；不合法返回 error,请改正后重提。**只调一次。**
- ``context_budget()`` —— 返回已用 / 上限 / 剩余 token。
- ``my_atoms()`` —— 返回本轮已提交 atom 的行号区间。

字段含义（submit_atom 各参数——评的是"整条轨迹"）
=================================================
- start_line（整数 = 续接点 resume_line；本 atom 从这里开始覆盖到 EOF）
- intent（≤40 字,概括**整条轨迹**做的事）
- summary（≤200 字,复盘**整条轨迹**：根因、关键动作、产出、验证）
- tags（3-5 个,小写下划线,概括整条）
- used_skills（**整条轨迹**里 agent 实际触发/用到的所有 skill 名;没有就传 []）
- ux_score（1~10 整数;评**整条轨迹**的用户体验,沿用下方分档表）

ux_score 严格分档表
====================
**永远不要凭"做了多少事"或"步数"打分。质量驱动,参考表如下：**

  10 一次到位：用户提需求 → agent 一步给出正确产出 → 用户接受无澄清/无负面情绪。
   9 接近一次到位：仅一处细节澄清,后续顺畅。
   8 正确完成但绕了 1 个小弯（多读一个文件、命令小修一次）,用户基本满意。
   7 正确完成,但用户做了 2-3 次澄清/修正；无明显不耐烦。
   6 完成度边界——用户对结果勉强可用,但已表达"这就行吧"之类不完全满意。
   5 部分完成：核心需求达成,但遗漏明显细节；用户不得不补一次说明。
   4 多次错误后才接近正确：用户已用否定词（"不对"/"错了"）2 次以上。
   3 任务勉强算完成但用户明显失望（"算了"/"我自己来"）,或 agent 在关键
     步骤上误判方向但侥幸跑通。
   2 任务未完成 / agent 反复触发 blocker / 用户明显放弃。
   1 完全失败 / 引发副作用（删错文件、改坏代码、误推送等）。

判 ux_score 时同时看：
- 整条轨迹是否真正调用了某个 used_skill；若调了 skill 且导致绕弯/错误 → 直接降到
  ≤5；若调了 skill 且一步到位 → 至少 8 起步。
- 整条轨迹的产出在用户后续 turn 是否被否定 / 撤销 / 重做。

工作流程
========
1. 读"用户提问地图",用 ``look`` 按需读 assistant 原文,判断整条轨迹做了什么、
   触发了哪些 skill、整体 UX 如何。
2. **只调一次 ``submit_atom``**：start_line = 续接点 resume_line,把整条新增轨迹
   并成这一个 atom。完成后结束。
"""


# ── 用户消息模板（与 SYSTEM_PROMPT 放一起,便于整体审计）──────────────
USER_MSG_TEMPLATE = """\
本轨迹元信息：
  trajid: {traj_id}
  trajpath: {traj_path}
  source_model: {source_model}
  total_lines: {total_lines}
  上次拆分到（续接点）: 第 {resume_line} 行

本轨迹已经拆分的 Atom（仅作衔接参考,勿重复拆分；需要细节用 look 按行号读）：
{prior_block}
用户提问地图（全轨迹 ``## User`` 回合：行号 + 首句摘要。按用户意图切换对每个
新意图调 submit_atom 报 start_line；只切续接点 {resume_line} 之后的）：
{query_map}

提示：assistant 正文不在这里,需要某行附近的原文用 look(line) 读。"""

PRIOR_ATOM_TEMPLATE = """\
  ----------------
  atomid: {atom_id}
  atom-path: {atom_path}
  atom-line: {offset_start}-{offset_end}
  atom-summary: {summary}
"""


def _inject(template: str, **vals: str) -> str:
    """把模板里的 ``{name}`` 占位一次性替成 vals[name]（单遍,不重扫注入值）。"""
    return re.sub(r"\{(\w+)\}", lambda m: vals[m.group(1)], template)


def _is_user_header(body: str) -> bool:
    """该行（已 rstrip）是否是 ``## User`` 或 ``## Initial Query`` 回合标题。

    ``## Initial Query`` 是所有 ecosystem adapter 写入的首条用户消息标题，
    语义等同于 ``## User``，应作为合法拆分边界。
    """
    return (body == "## User" or body.startswith("## User ")
            or body == "## Initial Query" or body.startswith("## Initial Query "))


# ── 容噪过滤（F0）：纯机器签名块判别（确定性,零 LLM）─────────────────
_SHA40_RE = re.compile(r"^[0-9a-f]{40}$")
_SHA_HEX_RE = re.compile(r"^[0-9a-f]{7,64}$")
_LOG_LINE_RE = re.compile(r"^\d{2}:\d{2}:\d{2}\s+\[[^\]]+\]")
_LS_LINE_RE = re.compile(
    r"^[\-dlrwxs]{10}\s")  # ls -l 行首 perm 串,如 -rw-r--r--
# 用户指令特征：祈使/请求/疑问/口语动词。命中则不当机器噪声。
_INSTRUCTION_HINT_RE = re.compile(
    r"(请|帮我|帮忙|给我|麻烦|我想|我要|需要|能不能|可不可以|怎么|如何|为什么|"
    r"为啥|改一下|加一个|换成|部署|实现|修复|优化|检查|分析|写个|写一个|生成|"
    r"\?|？|please|help|fix|add|deploy|implement|change|write|why|how|can you)")


def _block_after_user(lines: list[str], user_idx: int) -> list[str]:
    """取某 ``## User`` 行（0-based user_idx）后、到下一个 ``## `` 标题前的正文行。"""
    out: list[str] = []
    for j in range(user_idx + 1, len(lines)):
        body = lines[j].rstrip("\r\n").rstrip()
        if body.startswith("## "):
            break
        out.append(lines[j])
    return out


def _is_machine_noise_block(block_lines: list[str]) -> bool:
    """某 ``## User`` 块的正文是否整块是纯机器签名（无用户指令特征）。

    判据（确定性）：去掉空行后非空行 ≥ 1,且**每一非空行**都匹配机器签名
    之一（40-hex SHA / 短 hex / ``HH:MM:SS [tag]`` 日志 / ls -l 行 /
    独立 JSON 对象/数组）,且**整块无任何用户指令特征**（祈使/疑问/口语）。
    任一行不像机器签名,或出现指令特征 → 不是噪声,照常当拆分边界。
    """
    nonblank = [ln.rstrip("\r\n").strip() for ln in block_lines]
    nonblank = [ln for ln in nonblank if ln]
    if not nonblank:
        return False
    joined = "\n".join(nonblank)
    if _INSTRUCTION_HINT_RE.search(joined):
        return False
    # 整块就是一个 JSON 对象/数组？
    stripped = joined.strip()
    if stripped[:1] in "{[" and stripped[-1:] in "}]":
        try:
            json.loads(stripped)
            return True
        except (json.JSONDecodeError, ValueError):
            pass
    # 否则要求每一非空行都是机器签名
    for ln in nonblank:
        if (_SHA40_RE.match(ln) or _SHA_HEX_RE.match(ln)
                or _LOG_LINE_RE.match(ln) or _LS_LINE_RE.match(ln)):
            continue
        return False
    return True


def _extract_user_queries(lines: list[str]) -> list[tuple[int, str]]:
    """抽全文件每个 ``## User`` 回合的 (行号, 首条非空正文摘要)。

    **容噪过滤（F0）已并入**：若某 ``## User`` 块整块是纯机器签名（无用户指令
    特征,见 ``_is_machine_noise_block``）,该回合**不进地图**——它不是真正的
    用户意图边界,不让 agent 误切。摘要取该回合标题后第一条非空、非新标题的
    行,截 80 字。
    """
    out: list[tuple[int, str]] = []
    n = len(lines)
    for i, line in enumerate(lines):
        body = line.rstrip("\r\n").rstrip()
        if not _is_user_header(body):
            continue
        block = _block_after_user(lines, i)
        if _is_machine_noise_block(block):
            continue
        snippet = ""
        for j in range(i + 1, n):
            t = lines[j].strip()
            if t.startswith("## "):
                break
            if t:
                snippet = t
                break
        out.append((i + 1, snippet[:80]))
    return out


@dataclass
class TaskAgent:
    """弃窗单趟 agentic AtomTask 拆分员。

    ``agno_agent_factory`` 契约同 cluster/edit agent：
    ``(*, instructions, tools) -> agent``,其 ``run(user_msg)`` 跑工具调用循环,
    返回的对象（run_response）若有 ``status`` 字段则用于查 error。
    """

    agno_agent_factory: Callable[..., Any]
    store: AtomTaskStore
    # look 白名单根：默认取 store.root（轨迹文件所在目录）。
    traj_root: Path | None = None
    skill_dir: Path | None = None
    # 拆分模式：``agentic``（按意图切 0~N 个 atom，默认）或 ``whole``（整条
    # 新增轨迹并成恰好 1 个 atom）。两者都走本 agent（LLM），只是选不同 system
    # prompt——used_skills / ux_score 等字段一律照常由 LLM 产出。
    split_mode: str = "agentic"
    # 顶层 config.interests 规范化后传入；空列表禁用兴趣过滤。
    interests: list[str] | None = None

    def __post_init__(self) -> None:
        if self.traj_root is None:
            self.traj_root = Path(self.store.root)
        else:
            self.traj_root = Path(self.traj_root)
        if self.skill_dir is not None:
            self.skill_dir = Path(self.skill_dir)
        if self.split_mode not in ("agentic", "whole"):
            raise ValueError(
                f"TaskAgent.split_mode={self.split_mode!r} 非法，"
                "必须是 'agentic' 或 'whole'")
        self.interests = interests_config({"interests": self.interests or []})

    @property
    def _system_prompt(self) -> str:
        """按 split_mode 选 system prompt：whole → 整轨 1 atom，否则按意图切分。"""
        base_prompt = WHOLE_SYSTEM_PROMPT if self.split_mode == "whole" else SYSTEM_PROMPT
        if not self.interests:
            return base_prompt
        interests_block = "\n".join(f"- {interest}" for interest in self.interests)
        return base_prompt + INTEREST_FILTER_SECTION_TEMPLATE.format(
            interests_block=interests_block
        )

    # ── public API ────────────────────────────────────────────────

    def run(self, *, traj_id: str, traj_path: Path) -> list[AtomTask]:
        """弃窗单趟拆分整条轨迹：抽全轨迹 User 地图喂一次,agent 一趟出全部 atom。

        EOF 硬校验：首 atom offset_start=1、末 atom offset_end=total+1；有 User
        轮却 0 提交 → 抛错；落盘后断言区间铺满 [resume, total+1)。
        增量续拆：resume_line = last_offset,只切 ≥ resume 的新意图。
        """
        traj_path = Path(traj_path)
        text = traj_path.read_text(encoding="utf-8")
        lines = text.splitlines(keepends=True)
        total_lines = len(lines)
        source_model = _sidecar_model(traj_path)

        resume_line = self.store.last_offset(traj_id) or 1
        if resume_line > total_lines:
            return []  # 没有新增行,省一次 LLM 调用

        queries = _extract_user_queries(lines)
        # 续拆：只保留 ≥ resume_line 的 User 回合作为可切边界。
        new_queries = [(ln, snip) for ln, snip in queries if ln >= resume_line]
        if not new_queries:
            # 续接点之后没有真正的用户意图回合（全是机器噪声 / 无 User）→
            # 无新 atom。但若是首轮（resume==1）且全文确实无任何 User 回合,
            # 则整条无可拆边界,合法返回空（无 User 轮,不触发 0 提交抛错）。
            return []

        prior_atoms = self.store.list_by_traj(traj_id)
        prior_atom = prior_atoms[-1] if prior_atoms else None
        valid_lines = [ln for ln, _ in new_queries]

        submitted = self._run_agent(
            traj_id=traj_id, traj_path=traj_path, source_model=source_model,
            resume_line=resume_line, prior_atoms=prior_atoms,
            all_lines=lines, total_lines=total_lines,
            queries=queries, valid_lines=valid_lines,
        )
        if not submitted:
            # 有 User 轮却 0 提交 → 静默空,绝不收下（设计 §4.4）。
            raise RuntimeError(
                f"TaskAgent: traj {traj_id} 有 {len(new_queries)} 个待拆 User "
                f"回合却 0 提交（疑似 LLM 静默失败 / 限流空返）,标记重拆")

        parsed = self._derive_ranges(
            submitted, floor_line=resume_line, eof_line=total_lines + 1)

        next_idx = len(prior_atoms) + 1
        new_atoms: list[AtomTask] = []
        for i, p in enumerate(parsed):
            atom_id = f"atom_{traj_id}_{next_idx + i:04d}"
            os_line = p["offset_start"]
            oe_line = p["offset_end"]
            new_atoms.append(AtomTask(
                atom_id=atom_id,
                traj_id=traj_id,
                offset_start=os_line,
                offset_end=oe_line,
                intent=p["intent"],
                summary=p["summary"],
                tags=p["tags"],
                used_skills=p["used_skills"],
                ux_score=p.get("ux_score"),
                pre_atom_id=None,
                post_atom_id=None,
                context_prefix=self._context_prefix(text, lines, os_line),
                raw_segment="".join(lines[os_line - 1:oe_line - 1]),
                source_model=source_model,
            ))

        # 本批内部相邻 atom 互填 pre/post
        for i in range(1, len(new_atoms)):
            new_atoms[i].pre_atom_id = new_atoms[i - 1].atom_id
            new_atoms[i - 1].post_atom_id = new_atoms[i].atom_id

        # 与 store 末尾 atom 衔接（续拆场景）
        if prior_atom is not None:
            new_atoms[0].pre_atom_id = prior_atom.atom_id
            prior_atom.post_atom_id = new_atoms[0].atom_id
            self.store.save(prior_atom)

        for a in new_atoms:
            self.store.save(a)

        self._assert_eof_coverage(traj_id, resume_line=resume_line,
                                  total_lines=total_lines)
        return new_atoms

    # ── EOF 覆盖硬校验 ────────────────────────────────────────────

    def _assert_eof_coverage(self, traj_id: str, *, resume_line: int,
                             total_lines: int) -> None:
        """落盘后断言本 traj 全部 atom 区间无缝无叠地铺满 [resume, total+1)。

        续拆场景下 [1, resume) 已由历史 atom 覆盖,本次只校验
        [resume_line, total_lines+1) 这段被新 atom 无缝铺满。
        """
        atoms = sorted(self.store.list_by_traj(traj_id),
                       key=lambda a: a.offset_start)
        # 取覆盖 [resume, ...) 的尾段（新 atom 起点 ≥ floor=resume 或并入首 atom）。
        floor = resume_line
        seg = [a for a in atoms if a.offset_end > floor]
        if not seg:
            raise RuntimeError(
                f"TaskAgent: traj {traj_id} 落盘后无 atom 覆盖 [{floor}, "
                f"{total_lines + 1})")
        cursor = min(seg[0].offset_start, floor)
        if cursor > floor:
            raise RuntimeError(
                f"TaskAgent: traj {traj_id} 覆盖起点 {cursor} > 续接点 {floor}")
        for a in seg:
            if a.offset_start != cursor:
                raise RuntimeError(
                    f"TaskAgent: traj {traj_id} atom 区间不连续：期望起点 "
                    f"{cursor},实得 {a.offset_start}（atom {a.atom_id}）")
            cursor = a.offset_end
        if cursor != total_lines + 1:
            raise RuntimeError(
                f"TaskAgent: traj {traj_id} 末 atom 未覆盖到 EOF：终点 "
                f"{cursor} != total+1 {total_lines + 1}")

    # ── agentic 拆分（单趟）───────────────────────────────────────

    def _run_agent(self, *, traj_id, traj_path, source_model, resume_line,
                   prior_atoms, all_lines, total_lines, queries,
                   valid_lines) -> list[dict]:
        """构造 agent + run-scoped 工具,跑一趟工具调用循环。

        返回按提交顺序的 ``submitted`` 列表。``submit_atom`` 把校验通过的 atom
        append 进闭包捕获的本地列表——每次 run 各自一份,线程安全（watcher 并发
        拆多条 traj 不串）。``agent.run()`` 后查 run_response.status,error 即抛。
        """
        submitted: list[dict] = []
        not_fit_reason: str | None = None
        valid = set(valid_lines)
        ordered_valid = sorted(valid_lines)

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
            if not_fit_reason is not None:
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
            if self.split_mode == "whole" and submitted:
                return ("error: whole 模式下整条新增轨迹只产 1 个 atom，"
                        "不要再调 submit_atom（已提交 1 个）")
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

        def mark_not_fit(reason: str) -> str:
            """标记整条轨迹不符合配置的 interests，并结束拆分。

            Args:
                reason: 简短说明为什么整条轨迹与 interests 无关。
            """
            nonlocal not_fit_reason
            if submitted:
                return (
                    "error: 已经提交过 submit_atom，不能再调用 mark_not_fit；"
                    "部分相关的轨迹应继续正常拆分"
                )
            normalized_reason = str(reason or "").strip()
            if not normalized_reason:
                normalized_reason = "trajectory does not match configured interests"
            not_fit_reason = normalized_reason
            return f"ok: not_fit 已记录 ({normalized_reason})"

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

        def context_budget() -> str:
            """返回当前上下文 token 预算：已用 / 上限 / 剩余。

            已用以**后端真实 ``usage.prompt_tokens``** 为准（由 invoke 包装层在
            每次请求后写进 thread-local）；首次调用（还没发过请求）时退化为
            4 字符/token 估当前 user 消息体量。上限取 resolve 后的 max_context。
            """
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

        user_msg = self._build_user_msg(
            traj_id=traj_id, traj_path=traj_path, source_model=source_model,
            resume_line=resume_line, prior_atoms=prior_atoms,
            total_lines=total_lines, queries=queries,
        )
        agent_tools = [look, submit_atom, context_budget, my_atoms]
        if self.interests:
            agent_tools.append(mark_not_fit)
        agent = self.agno_agent_factory(
            instructions=[self._system_prompt],
            tools=agent_tools,
        )
        # 把这次拆分的逐轮 CoT/工具调用流式写进 logs/agents/task_agents/<traj_id>.log
        from xskill.agents.agent_trace import trace_to
        from xskill.config import get_logs_dir
        sink = get_logs_dir() / "agents" / "task_agents" / f"{traj_id}.log"
        with trace_to(sink):
            run_response = agent.run(user_msg)
        self._check_run_status(traj_id, run_response)
        if not_fit_reason is not None and not submitted:
            raise TrajectoryNotFit(not_fit_reason)
        return submitted

    @staticmethod
    def _check_run_status(traj_id: str, run_response: Any) -> None:
        """查 agno run_response.status,error 即抛（绝不把静默失败当成功收下）。

        stub / 旧返回对象没有 status 字段时视为正常（测试注入的假 run 结果）。
        """
        status = getattr(run_response, "status", None)
        if status is None:
            return
        sval = getattr(status, "value", status)
        if str(sval).upper() == "ERROR":
            raise RuntimeError(
                f"TaskAgent: traj {traj_id} agent.run() 返回 status=ERROR,"
                "标记重拆（不静默收空）")

    @staticmethod
    def _derive_ranges(submitted: list[dict], *,
                       floor_line: int, eof_line: int) -> list[dict]:
        """把按序提交的 start_line 推成 [start, end) 行区间。

        首 atom 从 ``floor_line`` 起（含续接点到首 ## User 之间的衔接行/前言）；
        其余从各自 start_line 起。终点 = 下一 atom 起点 / EOF（total+1）。
        ``submit_atom`` 已保证严格递增 + 合法行,这里只做纯几何推导,并由
        ``run()`` 末尾的 ``_assert_eof_coverage`` 兜死覆盖。
        """
        out: list[dict] = []
        for i, p in enumerate(submitted):
            os_line = floor_line if i == 0 else p["start_line"]
            if i + 1 < len(submitted):
                oe_line = submitted[i + 1]["start_line"]
            else:
                oe_line = eof_line
            out.append({**p, "offset_start": os_line, "offset_end": oe_line})
        return out

    # ── prompt helpers ────────────────────────────────────────────

    def _context_prefix(self, text: str, lines: list[str],
                        start_line: int) -> str:
        """生成 atom 起始行之前内容的省略表示（给 prompt / ux 评分用）。"""
        char_off = sum(len(ln) for ln in lines[:start_line - 1])
        if char_off <= 200:
            return text[:char_off]
        return text[:200] + f"\n\n[省略 {char_off - 200} 字符]\n\n"

    def _build_user_msg(self, *, traj_id, traj_path, source_model, resume_line,
                        prior_atoms, total_lines, queries) -> str:
        """构造 user 消息（discover / update 共用一份模板）。

        context-0 只放 User 提问地图（不含 assistant 正文）+ 元信息 + 续拆衔接块。
        """
        if prior_atoms:
            prior_block = "".join(
                _inject(
                    PRIOR_ATOM_TEMPLATE,
                    atom_id=str(a.atom_id),
                    atom_path=str(traj_path),
                    offset_start=str(a.offset_start),
                    offset_end=str(a.offset_end),
                    summary=str(a.summary),
                )
                for a in prior_atoms
            )
        else:
            prior_block = "  （无——本轨迹首次拆分）\n"

        if queries:
            query_map = "\n".join(
                f"- [line:{ln}] ## User — {snip}"
                for ln, snip in queries
            )
        else:
            query_map = "  （无 ## User 回合）"

        return _inject(
            USER_MSG_TEMPLATE,
            traj_id=str(traj_id),
            traj_path=str(traj_path),
            source_model=source_model or "(unknown)",
            total_lines=str(total_lines),
            resume_line=str(resume_line),
            prior_block=prior_block,
            query_map=query_map,
        )

    # ── 白名单（供潜在外部读工具复用）────────────────────────────

    def _within_whitelist(self, p: Path) -> bool:
        """路径白名单：只允许 traj_root 子树 + skill_dir 子树。"""
        try:
            rp = p.resolve()
        except OSError:
            return False
        roots: list[Path] = []
        if self.traj_root is not None:
            roots.append(self.traj_root.resolve())
        if self.skill_dir is not None:
            roots.append(self.skill_dir.resolve())
        return any(rp == r or rp.is_relative_to(r) for r in roots)
