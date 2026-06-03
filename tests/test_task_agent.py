"""TaskAgent —— agentic 行号坐标切分（submit_atom 工具 + 增量续拆）

v2.3 起 TaskAgent 改为 agentic：不再解析 XML，而是给 agno agent 三个工具
``readfile`` / ``grep`` / ``submit_atom``。``submit_atom`` 提交即校验（带
[line:] 标记的 ## User 行、≥ 续接点、严格递增），不合法返 error 让 agent 自改；
整段无新意图时可一个都不提交（0 个 atom 合法）。

这里用 stub agno 工厂驱动：测试控制 agent 在 run() 里调用哪些工具、传什么参数,
从而确定性地覆盖首轮 / 增量 / 校验 / 提示词内容。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from xskill.pipeline.atom import AtomTask, AtomTaskStore
from xskill.agents.task_agent import (
    TaskAgent, _annotate_user_lines, _extract_user_queries,
)


# ════════════════════════════════════════════════════════════════════
# 共享 stub：agno 工厂 + autosplit 助手（watcher 等下游测试 import 复用）
# ════════════════════════════════════════════════════════════════════

class _RunResult:
    """模拟 agno agent.run() 的返回（只需 .content 字段）。"""
    content = ""


def autosplit_submit(user_msg: str, tools: dict) -> None:
    """扫 user_msg 里的 ``[line:N]`` 标记，每个 ## User 行调一次 submit_atom。

    供 split-agent 的 stub 工厂复用——与轨迹具体行号解耦，任何含 ``## User``
    的轨迹都能直接用，不必手算行号。``tools`` 是 ``{name: callable}`` 映射。
    """
    submit = tools.get("submit_atom")
    if submit is None:
        return
    for ln in [int(n) for n in re.findall(r"\[line:(\d+)\]", user_msg)]:
        submit(start_line=ln, intent="stub intent", summary="stub summary",
               tags=["stub"], used_skills=[], ux_score=7)


class _AutoSplitAgno:
    """split-agent stub：扫 [line:N] 标记逐个 submit_atom。

    构造签名与生产 agno 工厂一致 ``(*, instructions, tools)``。watcher /
    pipeline 等下游测试把它（或派生）当 ``agno_agent_factory`` 注入。
    """

    def __init__(self, *, instructions, tools):
        self.instructions = instructions
        self.tools = {getattr(t, "__name__", ""): t for t in tools}

    def run(self, user_msg, **kw):
        autosplit_submit(user_msg, self.tools)
        return _RunResult()


def _scripted_factory(submit_calls):
    """返回一个 stub 工厂：agent.run() 时按 submit_calls 逐条调 submit_atom，
    把每次返回值记到 ``factory.results``，sysprompt / user_msg 记到 captured。
    """
    captured: dict = {"results": [], "instructions": None, "user_msg": None}

    def factory(*, instructions, tools):
        toolmap = {getattr(t, "__name__", ""): t for t in tools}
        captured["instructions"] = instructions

        class _A:
            def run(self, user_msg, **kw):
                captured["user_msg"] = user_msg
                for c in submit_calls:
                    captured["results"].append(toolmap["submit_atom"](**c))
                return _RunResult()

        return _A()

    factory.captured = captured
    return factory


# 真实形态的小轨迹:`## User` 在第 5、17 行,共 23 行。
_TRAJ_MD = """## System

You are Claude Code.

## User

Deploy xquiz to 1717.

## Assistant

Cloning...

## Tool Call: Bash

git clone https://example.com/xquiz

## User

Now redesign the frontend.

## Assistant

Editing CSS...
"""


# ── 向后兼容：旧 XML 时代的 _AutoSplitLLM 仍被若干 watcher 测试当作 ``llm=``
# 占位（watcher 的 split 现在走 agno 工厂，``llm`` 只用于 score_atom 闸门）。
# 保留它的 .chat 行为不变,避免下游 import 断裂。
class _AutoSplitLLM:
    """旧 XML split stub。现仅作 watcher 的 ``llm=`` 占位（不再驱动 split）。"""
    model = "stub-autosplit"

    def __init__(self):
        self.calls: list[str] = []
        self.systems: list[str] = []

    def chat(self, prompt: str, system: str = "") -> str:
        self.calls.append(prompt)
        self.systems.append(system)
        marks = [int(n) for n in re.findall(r"\[line:(\d+)\]", prompt)]
        if not marks:
            raise RuntimeError("_AutoSplitLLM: prompt 里没有 [line:] 标记")
        atoms = "\n".join(
            f"<atom><start_line>{ln}</start_line>"
            f"<intent>stub intent</intent>"
            f"<summary>stub summary</summary>"
            f"<tags><tag>stub</tag></tags>"
            f"<used_skills></used_skills>"
            f"<ux_score>7</ux_score></atom>"
            for ln in marks
        )
        return f"<atoms>\n{atoms}\n</atoms>"


def _seg(text: str, start_line: int, end_line: int) -> str:
    """text 中 [start_line, end_line) 行区间(1-based,半开)的原文。"""
    lines = text.splitlines(keepends=True)
    return "".join(lines[start_line - 1:end_line - 1])


# ────────────────────────────────────────────────────────────────────
# _annotate_user_lines / _extract_user_queries 单元测试
# ────────────────────────────────────────────────────────────────────

class TestAnnotateUserLines:
    def test_tags_only_user_headers_with_full_file_line_numbers(self):
        annotated, user_lines = _annotate_user_lines(_TRAJ_MD, first_line_no=1)
        assert "[line:5] ## User" in annotated
        assert "[line:17] ## User" in annotated
        assert "] ## Assistant" not in annotated
        assert "] ## Tool Call" not in annotated
        assert "] ## System" not in annotated
        assert user_lines == [5, 17]

    def test_line_numbers_shift_with_first_line_no(self):
        """delta 不是从文件头开始时,行号按 first_line_no 平移。"""
        lines = _TRAJ_MD.splitlines(keepends=True)
        delta = "".join(lines[8:])  # 从第 9 行(## Assistant)起
        annotated, user_lines = _annotate_user_lines(delta, first_line_no=9)
        assert user_lines == [17]
        assert "[line:17] ## User" in annotated

    def test_content_hash_headers_not_mistaken_for_user(self):
        """用户正文里的 markdown 二级标题不能被误判成 ## User。"""
        md = "## User\n\n看这个 ## User 提到的步骤\n## Step 1 做点啥\n"
        annotated, user_lines = _annotate_user_lines(md, first_line_no=1)
        assert user_lines == [1]
        assert annotated.count("[line:") == 1

    def test_extract_user_queries_line_and_snippet(self):
        lines = _TRAJ_MD.splitlines(keepends=True)
        queries = _extract_user_queries(lines)
        assert [ln for ln, _ in queries] == [5, 17]
        assert queries[0][1] == "Deploy xquiz to 1717."
        assert queries[1][1] == "Now redesign the frontend."


# ────────────────────────────────────────────────────────────────────
# 首轮:两个 atom + 行号坐标
# ────────────────────────────────────────────────────────────────────

class TestFirstRun:
    def test_persists_two_atoms_with_line_offsets(self, tmp_path):
        traj_dir = tmp_path / "cc-sessions"
        traj_dir.mkdir()
        traj_path = traj_dir / "traj_x.md"
        traj_path.write_text(_TRAJ_MD, encoding="utf-8")
        store = AtomTaskStore(root=traj_dir)
        atoms = TaskAgent(
            agno_agent_factory=_AutoSplitAgno, store=store,
        ).run(traj_id="traj_x", traj_path=traj_path)

        assert len(atoms) == 2
        # offset 是 1-based 行号。首 atom 从 floor 行(1)起,终点 = 下一 atom
        # 起点(第 17 行);末 atom 终点 = 末行号+1(23+1=24)。
        assert atoms[0].offset_start == 1
        assert atoms[0].offset_end == 17
        assert atoms[1].offset_start == 17
        assert atoms[1].offset_end == 24
        # 半开衔接:无重叠无缝隙
        assert atoms[0].offset_end == atoms[1].offset_start
        # 链表
        assert atoms[0].pre_atom_id is None
        assert atoms[0].post_atom_id == atoms[1].atom_id
        assert atoms[1].pre_atom_id == atoms[0].atom_id
        # ux_score 富化字段
        assert atoms[1].ux_score == 7
        # raw_segment 按行区间切回
        assert atoms[0].raw_segment == _seg(_TRAJ_MD, 1, 17)
        assert atoms[1].raw_segment == _seg(_TRAJ_MD, 17, 24)
        # 落盘
        assert (traj_dir / "traj_x" / "tasks" /
                f"{atoms[0].atom_id}.json").is_file()

    def test_user_msg_and_sysprompt_carry_markers(self, tmp_path):
        traj_dir = tmp_path / "cc-sessions"
        traj_dir.mkdir()
        traj_path = traj_dir / "traj_p.md"
        traj_path.write_text(_TRAJ_MD, encoding="utf-8")
        store = AtomTaskStore(root=traj_dir)
        factory = _scripted_factory([
            dict(start_line=5, intent="部署", summary="克隆并部署 xquiz",
                 tags=["deploy"], used_skills=[], ux_score=7),
            dict(start_line=17, intent="前端", summary="编辑 CSS",
                 tags=["frontend"], used_skills=["frontend-design"], ux_score=8),
        ])
        atoms = TaskAgent(agno_agent_factory=factory, store=store).run(
            traj_id="traj_p", traj_path=traj_path)
        assert len(atoms) == 2
        assert atoms[1].used_skills == ["frontend-design"]
        # 喂给 agent 的 user_msg 带行号标记 + 元信息
        user_msg = factory.captured["user_msg"]
        assert "[line:5] ## User" in user_msg
        assert "[line:17] ## User" in user_msg
        assert "trajid: traj_p" in user_msg
        assert "续接点）: 第 1 行" in user_msg
        # 首次拆分无 prior 衔接块
        assert "首次拆分" in user_msg
        # 提交都成功
        assert all(r.startswith("ok:") for r in factory.captured["results"])


# ────────────────────────────────────────────────────────────────────
# 增量轮:append 新 user turn → 续切
# ────────────────────────────────────────────────────────────────────

class TestIncrementalRun:
    def test_second_run_splits_appended_turn(self, tmp_path):
        traj_dir = tmp_path / "cc-sessions"
        traj_dir.mkdir()
        traj_path = traj_dir / "traj_y.md"
        traj_path.write_text(_TRAJ_MD, encoding="utf-8")
        store = AtomTaskStore(root=traj_dir)
        TaskAgent(agno_agent_factory=_AutoSplitAgno, store=store).run(
            traj_id="traj_y", traj_path=traj_path)

        # append 第三个 user turn
        appended = "\n## User\n\nAdd tests.\n\n## Assistant\n\nWriting pytest...\n"
        full = _TRAJ_MD + appended
        traj_path.write_text(full, encoding="utf-8")
        new_user_line = full[:full.rindex("## User")].count("\n") + 1
        total_lines = len(full.splitlines(keepends=True))

        factory = _scripted_factory([
            dict(start_line=new_user_line, intent="补单元测试",
                 summary="用户要求加测试；agent 写 pytest",
                 tags=["testing"], used_skills=[], ux_score=7),
        ])
        new_atoms = TaskAgent(agno_agent_factory=factory, store=store).run(
            traj_id="traj_y", traj_path=traj_path)

        assert len(new_atoms) == 1
        # 增量 atom 起点 = resume 行(上一轮窗口末行的下一行 = 24),
        # 终点 = 末行号+1
        assert new_atoms[0].offset_start == 24
        assert new_atoms[0].offset_end == total_lines + 1
        # 与上一轮末 atom 链接
        assert new_atoms[0].pre_atom_id is not None
        assert new_atoms[0].pre_atom_id.startswith("atom_traj_y_")
        prior = store.load(new_atoms[0].pre_atom_id)
        assert prior.post_atom_id == new_atoms[0].atom_id
        assert len(store.list_by_traj("traj_y")) == 3
        # 增量 user_msg：续接点=24 + 列出已拆 atom 衔接块（行号范围 + 路径）
        user_msg = factory.captured["user_msg"]
        assert f"[line:{new_user_line}] ## User" in user_msg
        assert "续接点）: 第 24 行" in user_msg
        assert "atom-line: 1-17" in user_msg or "atom-line: 17-24" in user_msg
        assert str(traj_path) in user_msg  # atom-path 是宿主机路径

    def test_no_new_content_returns_empty_no_agent_call(self, tmp_path):
        traj_dir = tmp_path / "cc-sessions"
        traj_dir.mkdir()
        traj_path = traj_dir / "traj_z.md"
        traj_path.write_text(_TRAJ_MD, encoding="utf-8")
        store = AtomTaskStore(root=traj_dir)
        TaskAgent(agno_agent_factory=_AutoSplitAgno, store=store).run(
            traj_id="traj_z", traj_path=traj_path)

        # 文件没变,再跑一次 → 空,且不构造/调 agent
        calls = {"n": 0}

        def _spy_factory(*, instructions, tools):
            calls["n"] += 1
            return _AutoSplitAgno(instructions=instructions, tools=tools)

        result = TaskAgent(agno_agent_factory=_spy_factory, store=store).run(
            traj_id="traj_z", traj_path=traj_path)
        assert result == []
        assert calls["n"] == 0

    def test_window_without_user_header_returns_empty_no_agent_call(self, tmp_path):
        """窗口内没有 ## User → 没有可切分的新 atom,直接返回,不构造 agent。"""
        traj_dir = tmp_path / "cc-sessions"
        traj_dir.mkdir()
        traj_path = traj_dir / "traj_n.md"
        traj_path.write_text("## System\n\njust a system note, no user turn\n",
                             encoding="utf-8")
        store = AtomTaskStore(root=traj_dir)
        calls = {"n": 0}

        def _spy_factory(*, instructions, tools):
            calls["n"] += 1
            return _AutoSplitAgno(instructions=instructions, tools=tools)

        result = TaskAgent(agno_agent_factory=_spy_factory, store=store).run(
            traj_id="traj_n", traj_path=traj_path)
        assert result == []
        assert calls["n"] == 0


# ────────────────────────────────────────────────────────────────────
# submit_atom 提交即校验：非法 → error 返回 + 不落盘
# ────────────────────────────────────────────────────────────────────

class TestSubmitValidation:
    def _setup(self, tmp_path: Path):
        traj_dir = tmp_path / "cc-sessions"
        traj_dir.mkdir()
        traj_path = traj_dir / "traj_e.md"
        traj_path.write_text(_TRAJ_MD, encoding="utf-8")
        store = AtomTaskStore(root=traj_dir)
        return traj_path, store

    def test_start_line_not_a_user_header_rejected(self, tmp_path):
        traj_path, store = self._setup(tmp_path)
        # 第 9 行是 ## Assistant,不是被标记的 ## User 行
        factory = _scripted_factory([
            dict(start_line=9, intent="i", summary="s"),
        ])
        atoms = TaskAgent(agno_agent_factory=factory, store=store).run(
            traj_id="traj_e", traj_path=traj_path)
        assert atoms == []  # 非法提交不落盘
        assert factory.captured["results"][0].startswith("error:")
        assert "## User" in factory.captured["results"][0]

    def test_non_monotonic_start_line_rejected(self, tmp_path):
        traj_path, store = self._setup(tmp_path)
        factory = _scripted_factory([
            dict(start_line=17, intent="i1", summary="s1"),
            dict(start_line=5, intent="i2", summary="s2"),  # 倒退 → reject
        ])
        atoms = TaskAgent(agno_agent_factory=factory, store=store).run(
            traj_id="traj_e", traj_path=traj_path)
        # 第一条合法 → 落 1 个 atom；第二条被拒
        assert len(atoms) == 1
        assert atoms[0].offset_start == 1  # 首 atom 从 floor 起
        assert factory.captured["results"][0].startswith("ok:")
        assert factory.captured["results"][1].startswith("error:")
        assert "严格大于" in factory.captured["results"][1]

    def test_missing_required_field_rejected(self, tmp_path):
        traj_path, store = self._setup(tmp_path)
        factory = _scripted_factory([
            dict(start_line=5, intent="i", summary=""),  # 缺 summary
        ])
        atoms = TaskAgent(agno_agent_factory=factory, store=store).run(
            traj_id="traj_e", traj_path=traj_path)
        assert atoms == []
        assert factory.captured["results"][0].startswith("error:")
        assert "必填" in factory.captured["results"][0]

    def test_zero_atoms_is_legal_no_raise(self, tmp_path):
        """整段无新意图 → agent 一个都不提交 → 返回 []，不报错（设计第 4 点）。"""
        traj_path, store = self._setup(tmp_path)
        factory = _scripted_factory([])  # 不调 submit_atom
        atoms = TaskAgent(agno_agent_factory=factory, store=store).run(
            traj_id="traj_e", traj_path=traj_path)
        assert atoms == []
        assert store.list_by_traj("traj_e") == []


# ────────────────────────────────────────────────────────────────────
# SYSTEM_PROMPT 共享一份：含严格分档表 + submit_atom 协议
# ────────────────────────────────────────────────────────────────────

class TestSystemPrompt:
    def test_prompt_contains_strict_rubric_and_submit_protocol(self, tmp_path):
        traj_dir = tmp_path / "cc-sessions"
        traj_dir.mkdir()
        traj_path = traj_dir / "traj_p.md"
        traj_path.write_text(_TRAJ_MD, encoding="utf-8")
        store = AtomTaskStore(root=traj_dir)
        factory = _scripted_factory([
            dict(start_line=5, intent="i", summary="s"),
            dict(start_line=17, intent="i2", summary="s2"),
        ])
        TaskAgent(agno_agent_factory=factory, store=store).run(
            traj_id="traj_p", traj_path=traj_path)
        system_msg = factory.captured["instructions"][0]
        assert "10 一次到位" in system_msg
        assert "用户意图切换" in system_msg
        assert "ux_score" in system_msg
        # 新协议关键词：submit_atom + 行号标记 + readfile
        assert "submit_atom" in system_msg
        assert "start_line" in system_msg
        assert "[line:" in system_msg
        assert "readfile" in system_msg

    def test_literal_braces_in_content_are_safe(self, tmp_path):
        """轨迹正文含 ``{annotated}`` / ``{start_line}`` 字面量时,注入不二次解析。"""
        traj_dir = tmp_path / "cc-sessions"
        traj_dir.mkdir()
        traj_path = traj_dir / "traj_b.md"
        traj_path.write_text(
            "## User\n\n聊到 {annotated} 和 {start_line} 这两个占位\n",
            encoding="utf-8",
        )
        store = AtomTaskStore(root=traj_dir)
        factory = _scripted_factory([
            dict(start_line=1, intent="i", summary="s"),
        ])
        atoms = TaskAgent(agno_agent_factory=factory, store=store).run(
            traj_id="traj_b", traj_path=traj_path)
        assert len(atoms) == 1
        user_msg = factory.captured["user_msg"]
        # 占位字面量原样保留在 user_msg 里,没被当模板变量替掉
        assert "{annotated}" in user_msg
        assert "{start_line}" in user_msg
