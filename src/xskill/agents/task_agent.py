"""TaskAgent —— 从 trajectory.md 增量拆分 AtomTask（agentic + submit_atom 工具）
================================================================================

输入：一条 ``traj.md`` + ``AtomTaskStore`` + 一个 agno agent 工厂。
输出：把 LLM（通过 ``submit_atom`` 工具）拆分得到的 AtomTask 落盘到 store，
前后 atom 链表化（含与 ``store.last_atom_id()`` 的衔接）。

设计要点
========

1. **坐标系是行号,不是字符 offset**。AtomTask 的 ``offset_start`` /
   ``offset_end`` 存的是 1-based 行号,半开区间 [start, end)（end 这一行
   不含；末 atom 的 end = 末行号 + 1）。轨迹一旦入库就不再变,行号稳定。

2. **切分边界 cut signal 只考虑"用户意图切换"**,且边界**只能落在
   ``## User`` 回合的开头**。预处理给喂进 LLM 的轨迹里每个 ``## User``
   标题行打 ``[line:<行号>]`` 标记。Agent 通过 ``submit_atom`` 只报每个
   atom 的起始行号 ``start_line``,终点由下一个 atom 起点推导——重叠/缝隙/
   非单调三类错误结构上不可能发生。

3. **增量重拆（续写场景）**：watcher 每次扫到 traj 变化（mtime 变更 →
   ``updated`` 状态）时调 ``run()``。
   - 若 store 已有 atom：``start_line = store.last_offset(traj_id)``（续接点）。
   - 若新 traj：``start_line = 1``。
   - 窗口内没有 ``## User`` → 没有可切分的新 atom,直接返回 ``[]``。
   - **discover 与 update 共享同一份 SYSTEM_PROMPT**（缓存友好）；区别只在
     user 消息里是否带"本轨迹已拆分的 Atom"衔接块。

4. **提交即校验,不解析 XML**（CLAUDE.md 第 1 条：不写 fallback）。
   ``submit_atom`` 工具在提交时校验：start_line 必须是带 ``[line:]`` 标记的
   ``## User`` 行、必须 ≥ 续接点、必须严格大于上一条;不合法直接返回 error
   字符串让 agent 自改。整条无新意图时可一个都不提交（0 个 atom 合法）。

5. **agentic 读取省上下文**：续拆时把"已拆 atom 的行号范围 + 宿主机路径"喂进
   user 消息,agent 可用 ``readfile`` / ``grep``（走白名单：仅 trajpath +
   skill 目录）按需去读旧内容,不必把旧正文全塞进 prompt。

6. **ux_score 严格分档表**：SYSTEM_PROMPT 内列 10/9/.../1 每档语义；
   ``used_skills`` 非空时降档/起步规则明列。永远质量驱动。
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

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


SYSTEM_PROMPT = """你是 AtomTask 拆分员。给你一段 agent 与用户的对话轨迹（markdown），
你的任务是按"用户意图切换"切成 0~N 个 AtomTask。每个 AtomTask 是一段完整的
用户意图 episode——可以被独立检索、被独立提炼成 skill 的最小素材。

切分原则（按优先级）
====================
1. 只在**用户意图切换**处切。"意图切换"指用户从一个目标/任务/问题转到另一个，例如：
   - "再帮我改一下前端" → 前后是两个 atom
   - "另外/接下来/对了" 这类衔接词后跟新动作 → 切
   - 同一目标的多轮调整（澄清、修正、加细节）→ **不切**，留在同一 atom
2. **不要**因为 tool 切换、代码块出现、子目录变化、agent 自我更正等结构性事件而切
   ——避免过度拆分。agent 在完成同一个用户意图过程中的内部转折，留在同一 atom。
3. 如果一段轨迹只完成一件事，输出 1 个 atom 即可。不要硬凑数量。

边界与坐标（重要）
==================
- 轨迹里每个 ``## User`` 回合的标题行都带 ``[line:<行号>]`` 标记，例如
  ``[line:42] ## User``。
- AtomTask 的边界**只能**落在这些带标记的 ``## User`` 行。
- 一个 atom = 从它的起始 ``## User`` 行，到下一个 atom 的起始 ``## User`` 行
  之前为止（**左闭右开**）。最后一个 atom 一直到轨迹末尾。
- 你**只报每个 atom 的起始行号 ``start_line``**，终点由下一个 atom 的起点
  自动推导。
- 多个 atom 的 ``start_line`` 必须严格递增。

增量重拆（续写场景）
====================
- user 消息里会给你"上次拆分到（续接点）resume_line"。续接点之前的内容**已经
  拆过 atom**，列在"本轨迹已经拆分的 Atom"里——**不要重复拆这部分**。
- 你只对续接点**之后的新增内容**切分。所有 submit_atom 的 start_line 必须
  ≥ 续接点。
- 需要旧上下文做衔接判断时，用 ``readfile(trajpath, offset, limit)`` 按
  "已拆 Atom"列出的行号范围去读，不必让我把旧正文整段塞进上下文。

可用工具
========
- ``readfile(path, offset, limit)`` —— 按行号读文件片段（offset=起始行号
  1-based，limit=读多少行；同 claude code 语义）。白名单：只能读本轨迹
  文件(trajpath)与 skill 目录,越界返错。
- ``grep(keyword, path)`` —— 在白名单文件里按关键字找行，返回命中行号+原文。
- ``submit_atom(start_line, intent, summary, tags, used_skills, ux_score)``
  —— 提交一个 atom。**提交即校验**：
    * start_line 须是带 ``[line:<行号>]`` 标记的 ``## User`` 行；
    * start_line 须 ≥ 续接点（resume_line）；
    * 多次提交的 start_line 须严格大于上一条。
  不合法会返回 error，请改正后重新提交。终点你不用报（由下一个 atom 的
  start_line 自动推导）。整段没有新意图时**可以一个都不提交**（0 个 atom
  合法，不报错）。

字段含义（submit_atom 各参数）
==============================
- start_line（整数，带 [line:] 标记的 ``## User`` 行号；本 atom 从这里开始）
- intent（≤40 字，目标）
- summary（≤200 字，复盘：根因、关键动作、产出、验证）
- tags（3-5 个，小写下划线）
- used_skills（这段对话里 agent 实际触发了哪些 skill 名；没有就传空列表）
- ux_score（1~10 整数；只评这段 atom 的用户体验，不评整条 traj）

ux_score 严格分档表
====================
**永远不要凭"做了多少事"或"步数"打分。质量驱动，参考表如下：**

  10 一次到位：用户提需求 → agent 一步给出正确产出 → 用户接受无澄清/无负面情绪。
   9 接近一次到位：仅一处细节澄清，后续顺畅。
   8 正确完成但绕了 1 个小弯（多读一个文件、命令小修一次），用户基本满意。
   7 正确完成，但用户做了 2-3 次澄清/修正；无明显不耐烦。
   6 完成度边界——用户对结果勉强可用，但已表达"这就行吧"之类不完全满意。
   5 部分完成：核心需求达成，但遗漏明显细节；用户不得不补一次说明。
   4 多次错误后才接近正确：用户已用否定词（"不对"/"错了"）2 次以上。
   3 任务勉强算完成但用户明显失望（"算了"/"我自己来"），或 agent 在关键
     步骤上误判方向但侥幸跑通。
   2 任务未完成 / agent 反复触发 blocker / 用户明显放弃。
   1 完全失败 / 引发副作用（删错文件、改坏代码、误推送等）。

判 ux_score 时同时看：
- 这段 atom 是否真正调用了某个 used_skill；若调了 skill 且导致绕弯/错误 → 直接降到
  ≤5；若调了 skill 且一步到位 → 至少 8 起步。
- 这段 atom 的产出在用户后续 turn 是否被否定 / 撤销 / 重做。

工作流程
========
1. 看"用户(新增)轨迹"里的 [line:] 标记，按用户意图切换决定每个 atom 的起点。
2. 续拆场景：只切续接点之后的新增内容；需要旧上下文用 readfile 读 trajpath。
3. 对每个新 atom 调一次 submit_atom（提交即校验，error 就改了重提）。完成后结束。
"""


# ── 用户消息模板（与 SYSTEM_PROMPT 放一起，便于整体审计）──────────────
# 变量在 _build_user_msg 里先备好再一次性注入,不在代码里把提示词切成碎片。
USER_MSG_TEMPLATE = """\
本轨迹元信息：
  trajid: {traj_id}
  trajpath: {traj_path}
  source_model: {source_model}
  上次拆分到（续接点）: 第 {resume_line} 行

本轨迹已经拆分的 Atom（仅作衔接参考，勿重复拆分；需要细节用 readfile 按行号读 trajpath）：
{prior_block}
用户提问历史（全轨迹 ``## User`` 行）：
{query_history}

用户(新增)轨迹如下（行号 [{window_start}:{window_end})；``## User`` 行带 [line:<行号>] 标记，按用户意图切换对每个 atom 调 submit_atom 报 start_line）：
---
{annotated}
---"""

PRIOR_ATOM_TEMPLATE = """\
  ----------------
  atomid: {atom_id}
  atom-path: {atom_path}
  atom-line: {offset_start}-{offset_end}
  atom-summary: {summary}
"""


def _inject(template: str, **vals: str) -> str:
    """把模板里的 ``{name}`` 占位一次性替成 vals[name]。

    单遍替换、不重扫注入值——等价于 f-string：注入的轨迹正文里若含 ``{}``
    或恰好含 ``{annotated}`` 这种字面量，都原样保留，不会被二次解析。
    """
    return re.sub(r"\{(\w+)\}", lambda m: vals[m.group(1)], template)


def _annotate_user_lines(
    chunk: str, first_line_no: int,
) -> tuple[str, list[int]]:
    """给 ``chunk`` 里每个 ``## User`` 标题行打 ``[line:<行号>]`` 标记。

    Args:
        chunk: 轨迹的一段（可能是从某行起的 delta 窗口）。
        first_line_no: ``chunk`` 第一行在**全文件**里的 1-based 行号。

    Returns:
        ``(annotated_chunk, user_line_numbers)``——后者是被标记的
        ``## User`` 行在全文件里的 1-based 行号,升序。
    """
    out: list[str] = []
    user_lines: list[int] = []
    line_no = first_line_no
    for line in chunk.splitlines(keepends=True):
        body = line.rstrip("\r\n").rstrip()
        if body == "## User" or body.startswith("## User "):
            user_lines.append(line_no)
            out.append(f"[line:{line_no}] {line}")
        else:
            out.append(line)
        line_no += 1
    return "".join(out), user_lines


def _extract_user_queries(lines: list[str]) -> list[tuple[int, str]]:
    """抽全文件每个 ``## User`` 回合的 (行号, 首条非空正文摘要)。

    给 user 消息的"用户提问历史"块用——让 agent 廉价地看到整条对话的弧线,
    不必把全文塞进上下文。摘要取该回合标题后第一条非空、非新标题的行,截 80 字。
    """
    out: list[tuple[int, str]] = []
    n = len(lines)
    for i, line in enumerate(lines):
        body = line.rstrip("\r\n").rstrip()
        if body == "## User" or body.startswith("## User "):
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


def _line_window(lines: list[str], start_idx: int,
                 max_chars: int) -> tuple[str, int]:
    """从 ``lines[start_idx:]`` 取尽量多的整行,总字符不超过 ``max_chars``。

    至少取 1 行(避免 0 行死循环——单行超预算也得整行送)。
    返回 ``(window_text, n_lines)``。
    """
    out: list[str] = []
    total = 0
    for ln in lines[start_idx:]:
        if out and total + len(ln) > max_chars:
            break
        out.append(ln)
        total += len(ln)
    return "".join(out), len(out)


@dataclass
class TaskAgent:
    """agentic AtomTask 拆分员。

    ``agno_agent_factory`` 契约同 cluster/edit agent：
    ``(*, instructions, tools) -> agent``，其 ``run(user_msg)`` 跑工具调用循环。
    """

    agno_agent_factory: Callable[..., Any]
    store: AtomTaskStore
    # readfile / grep 白名单根：默认取 store.root（轨迹文件所在目录）。
    traj_root: Path | None = None
    skill_dir: Path | None = None
    max_context_chars: int = 30000

    def __post_init__(self) -> None:
        if self.traj_root is None:
            self.traj_root = Path(self.store.root)
        else:
            self.traj_root = Path(self.traj_root)
        if self.skill_dir is not None:
            self.skill_dir = Path(self.skill_dir)

    # ── public API ────────────────────────────────────────────────

    def run(self, *, traj_id: str, traj_path: Path) -> list[AtomTask]:
        traj_path = Path(traj_path)
        text = traj_path.read_text(encoding="utf-8")
        lines = text.splitlines(keepends=True)
        total_lines = len(lines)
        source_model = _sidecar_model(traj_path)   # 轨迹的用户模型，继承给每个 atom

        # resume：start_line 是 1-based 行号。store 空时 last_offset 返回 0。
        start_line = self.store.last_offset(traj_id) or 1
        if start_line > total_lines:
            return []  # 没有新增行，省一次 LLM 调用

        prior_atoms = self.store.list_by_traj(traj_id)
        prior_atom = prior_atoms[-1] if prior_atoms else None

        window_text, n_win = _line_window(
            lines, start_line - 1, self.max_context_chars)
        window_end_line = start_line + n_win  # 半开:窗口末行的下一行
        annotated, user_lines = _annotate_user_lines(
            window_text, first_line_no=start_line)
        if not user_lines:
            # 窗口内没有 ## User —— 没有可切分的新 atom，省一次 LLM 调用
            return []

        # ── 跑 agentic 拆分：submit_atom 收集到 run-scoped 列表 ──
        submitted = self._run_agent(
            traj_id=traj_id, traj_path=traj_path, source_model=source_model,
            resume_line=start_line, prior_atoms=prior_atoms,
            all_lines=lines, annotated=annotated, user_lines=user_lines,
            window_start=start_line, window_end=window_end_line,
        )
        if not submitted:
            # 整段无新意图 → 0 个 atom 合法（设计第 4 点），不报错。
            return []

        parsed = self._derive_ranges(
            submitted, floor_line=start_line, window_end_line=window_end_line)

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

        # 与 store 末尾 atom 衔接
        if prior_atom is not None:
            new_atoms[0].pre_atom_id = prior_atom.atom_id
            prior_atom.post_atom_id = new_atoms[0].atom_id
            self.store.save(prior_atom)

        for a in new_atoms:
            self.store.save(a)
        return new_atoms

    # ── agentic 拆分循环 ──────────────────────────────────────────

    def _run_agent(self, *, traj_id, traj_path, source_model, resume_line,
                   prior_atoms, all_lines, annotated, user_lines,
                   window_start, window_end) -> list[dict]:
        """构造 agent + run-scoped 工具，跑一次工具调用循环。

        返回按提交顺序的 ``submitted`` 列表（每项含 start_line/intent/...）。
        ``submit_atom`` 把校验通过的 atom append 进闭包捕获的本地列表——
        每次 run 各自一份,线程安全（watcher 并发拆多条 traj 不会串）。
        """
        submitted: list[dict] = []
        valid = set(user_lines)
        ordered_valid = sorted(user_lines)

        def submit_atom(start_line: int, intent: str, summary: str,
                        tags: list | None = None,
                        used_skills: list | None = None,
                        ux_score: int | None = None) -> str:
            """提交一个新 AtomTask（提交即校验,不合法返 error 让你自改）。

            Args:
                start_line: 本 atom 起始行号,必须是带 [line:] 标记的 ## User 行。
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
                return (f"error: start_line {sl} 不是带 [line:] 标记的 ## User 行 "
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

        sysprompt = SYSTEM_PROMPT
        user_msg = self._build_user_msg(
            traj_id=traj_id, traj_path=traj_path, source_model=source_model,
            resume_line=resume_line, prior_atoms=prior_atoms,
            all_lines=all_lines, annotated=annotated,
            window_start=window_start, window_end=window_end,
        )
        agent = self.agno_agent_factory(
            instructions=[sysprompt],
            tools=[self.readfile, self.grep, submit_atom],
        )
        agent.run(user_msg)
        return submitted

    @staticmethod
    def _derive_ranges(submitted: list[dict], *,
                       floor_line: int, window_end_line: int) -> list[dict]:
        """把按序提交的 start_line 推成 [start, end) 行区间。

        首 atom 从 ``floor_line`` 起(含续接点到首 ## User 之间的衔接行);其余从
        各自 start_line 起。终点 = 下一 atom 起点 / 窗口末行的下一行。
        ``submit_atom`` 已保证严格递增 + 合法行,这里只做纯几何推导。
        """
        out: list[dict] = []
        for i, p in enumerate(submitted):
            os_line = floor_line if i == 0 else p["start_line"]
            if i + 1 < len(submitted):
                oe_line = submitted[i + 1]["start_line"]
            else:
                oe_line = window_end_line
            out.append({**p, "offset_start": os_line, "offset_end": oe_line})
        return out

    # ── prompt helpers ────────────────────────────────────────────

    def _context_prefix(self, text: str, lines: list[str],
                        start_line: int) -> str:
        """生成 atom 起始行之前内容的省略表示（给 prompt / ux 评分用）。

        - 起始行之前内容 ≤ 200 字：直接给原文。
        - 否则保留头 200 字（一般是轨迹元信息 / 首次 user query）+ 占位符。
        """
        char_off = sum(len(ln) for ln in lines[:start_line - 1])
        if char_off <= 200:
            return text[:char_off]
        return text[:200] + f"\n\n[省略 {char_off - 200} 字符]\n\n"

    def _build_user_msg(self, *, traj_id, traj_path, source_model, resume_line,
                        prior_atoms, all_lines, annotated,
                        window_start, window_end) -> str:
        """构造 user 消息（discover / update 共用一份模板）。

        prior_atoms 为空（首次拆分）时衔接块写"无"；非空（续写重拆）时列出
        每个已拆 atom 的行号范围 + 宿主机路径，供 agent 用 readfile 按需读取。
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

        queries = _extract_user_queries(all_lines)
        if queries:
            query_history = "\n".join(
                f"- Query {k}[line {ln}]: {snip}"
                for k, (ln, snip) in enumerate(queries, 1)
            )
        else:
            query_history = "  （无）"

        return _inject(
            USER_MSG_TEMPLATE,
            traj_id=str(traj_id),
            traj_path=str(traj_path),
            source_model=source_model or "(unknown)",
            resume_line=str(resume_line),
            prior_block=prior_block,
            query_history=query_history,
            window_start=str(window_start),
            window_end=str(window_end),
            annotated=annotated,
        )

    # ── agent tools (readfile / grep) ─────────────────────────────

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

    def readfile(self, path: str, offset: int = 1, limit: int = 200) -> str:
        """按行号读文件片段（白名单：本轨迹文件 trajpath + skill 目录）。

        Args:
            path: 目标文件路径。
            offset: 起始行号（1-based，同 claude code 语义）。
            limit: 最多读多少行。
        """
        p = Path(path)
        if not self._within_whitelist(p):
            return f"error: {path} 不在白名单（仅 trajpath + skill 目录可读）"
        if not p.is_file():
            return f"error: file not found: {path}"
        try:
            off = max(1, int(offset))
            lim = max(1, int(limit))
        except (TypeError, ValueError):
            return "error: offset/limit 必须是整数"
        flines = p.read_text(encoding="utf-8").splitlines(keepends=True)
        chunk = flines[off - 1: off - 1 + lim]
        return "".join(chunk) or "(empty range)"

    def grep(self, keyword: str, path: str) -> str:
        """在白名单文件里按关键字找行，返回命中行号+原文（最多 50 行）。

        Args:
            keyword: 子串关键字。
            path: 目标文件路径（白名单：trajpath + skill 目录）。
        """
        p = Path(path)
        if not self._within_whitelist(p):
            return f"error: {path} 不在白名单（仅 trajpath + skill 目录可读）"
        if not p.is_file():
            return f"error: file not found: {path}"
        hits: list[str] = []
        for i, line in enumerate(
                p.read_text(encoding="utf-8").splitlines(), 1):
            if keyword and keyword in line:
                hits.append(f"{i}: {line.strip()}")
                if len(hits) >= 50:
                    break
        return "\n".join(hits) or f"(no match for {keyword!r} in {p.name})"
