"""TaskAgent —— 弃窗单趟 agentic 拆分（submit_atom/look/context_budget/my_atoms）

v3 起 TaskAgent 弃窗：抽全轨迹 User 地图喂一次,agent 一趟出全部 atom。
- 首 atom offset_start=1（并前言）、末 atom offset_end=total+1（盖到 EOF）。
- 有 User 轮却 0 提交 → raise（不静默产空）。
- 容噪过滤（F0）：纯机器签名的 ## User 块不进地图、不当边界。
- 增量续拆：resume_line=last_offset,只切 ≥ resume 的新意图。

测试用 stub agno 工厂注入假 submit 序列,确定性覆盖：单趟覆盖 / 前言并入 /
增量只加 / 0 提交 raise / 撤销不切（非法行被拒）/ 容噪过滤 / run_response error。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from xskill.pipeline.atom import AtomTaskStore
from xskill.agents.task_agent import (
    TaskAgent, TrajectoryNotFit, _extract_user_queries, _is_machine_noise_block,
)


def _tool_name(tool) -> str:
    return getattr(tool, "__name__", None) or getattr(tool, "name", "")


def _call_tool(tool, *args, **kwargs):
    entrypoint = tool if callable(tool) else getattr(tool, "entrypoint")
    return entrypoint(*args, **kwargs)


# ════════════════════════════════════════════════════════════════════
# 共享 stub：agno 工厂（watcher 等下游测试 import 复用）
# ════════════════════════════════════════════════════════════════════

class _RunResult:
    """模拟 agno agent.run() 的返回（.content + 可选 .status）。"""
    content = ""
    status = None


def autosplit_submit(user_msg: str, tools: dict) -> None:
    """扫 user_msg 里的 ``[line:N]`` 标记,每个 ## User 行调一次 submit_atom。

    弃窗后 user_msg 是"用户提问地图",每个真实回合带 ``[line:N]`` 标记。这个
    helper 对每个标记行 submit 一次——与轨迹具体行号解耦,任何含 ## User 的
    轨迹都能直接用。``tools`` 是 ``{name: callable}`` 映射。
    """
    submit = tools.get("submit_atom")
    if submit is None:
        return
    for ln in [int(n) for n in re.findall(r"\[line:(\d+)\]", user_msg)]:
        _call_tool(
            submit,
            start_line=ln,
            intent="stub intent",
            summary="stub summary",
            tags=["stub"],
            used_skills=[],
            ux_score=7,
        )


class _AutoSplitAgno:
    """split-agent stub：扫地图里的 [line:N] 标记逐个 submit_atom。

    构造签名与生产 agno 工厂一致 ``(*, instructions, tools)``。watcher /
    pipeline 等下游测试把它（或派生）当 ``agno_agent_factory`` 注入。
    """

    def __init__(self, *, instructions, tools):
        self.instructions = instructions
        self.tools = {_tool_name(t): t for t in tools}

    def run(self, user_msg, **_keyword_arguments):
        autosplit_submit(user_msg, self.tools)
        return _RunResult()


def _scripted_factory(submit_calls, *, status=None):
    """返回一个 stub 工厂：agent.run() 时按 submit_calls 逐条调 submit_atom,
    把每次返回值记到 ``factory.captured['results']``,sysprompt/user_msg/tool
    名也记到 captured。``status`` 注入 run_response.status（测 error 兜底）。
    """
    captured: dict = {"results": [], "instructions": None, "user_msg": None,
                      "tool_names": None}

    def factory(*, instructions, tools):
        toolmap = {_tool_name(t): t for t in tools}
        captured["instructions"] = instructions
        captured["tool_names"] = sorted(toolmap)

        class _A:
            def run(self, user_msg, **_keyword_arguments):
                captured["user_msg"] = user_msg
                for c in submit_calls:
                    captured["results"].append(
                        _call_tool(toolmap["submit_atom"], **c)
                    )
                r = _RunResult()
                r.status = status
                return r

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


class _AutoSplitLLM:
    """``.chat`` 形态的 LLM 占位（watcher 的 ``llm=`` 参数用）。

    弃窗后 split 走 agno 工厂,``llm`` 只用于 ``score_atom`` 闸门。watcher 等
    下游测试把它注入当 ``llm=`` 占位——保留 ``.chat`` 行为,返回一个合法的
    ux_score JSON,避免 score_atom 解析失败。
    """
    model = "stub-autosplit"

    def __init__(self):
        self.calls: list[str] = []
        self.systems: list[str] = []

    def chat(self, prompt: str, system: str = "") -> str:
        self.calls.append(prompt)
        self.systems.append(system)
        return '{"score": 7, "reasons": "stub"}'


def _seg(text: str, start_line: int, end_line: int) -> str:
    """text 中 [start_line, end_line) 行区间(1-based,半开)的原文。"""
    lines = text.splitlines(keepends=True)
    return "".join(lines[start_line - 1:end_line - 1])


# ────────────────────────────────────────────────────────────────────
# _extract_user_queries / 容噪过滤（F0）
# ────────────────────────────────────────────────────────────────────

class TestUserQueryMap:
    def test_extract_line_and_snippet(self):
        lines = _TRAJ_MD.splitlines(keepends=True)
        queries = _extract_user_queries(lines)
        assert [ln for ln, _ in queries] == [5, 17]
        assert queries[0][1] == "Deploy xquiz to 1717."
        assert queries[1][1] == "Now redesign the frontend."

    def test_content_hash_header_not_user_turn(self):
        """正文里的 markdown 二级标题不能被误判成 ## User。"""
        md = "## User\n\n看这个 ## Step 提到的步骤\n## Step 1 做点啥\n"
        queries = _extract_user_queries(md.splitlines(keepends=True))
        assert [ln for ln, _ in queries] == [1]

    def test_machine_noise_sha_block_filtered(self):
        """整块是 40-hex SHA 的 ## User 块不当作意图边界（容噪 F0）。"""
        block = ["a" * 40 + "\n"]
        assert _is_machine_noise_block(block) is True

    def test_machine_noise_log_block_filtered(self):
        block = ["12:30:01 [info] started\n", "12:30:02 [warn] retry\n"]
        assert _is_machine_noise_block(block) is True

    def test_machine_noise_json_block_filtered(self):
        block = ['{"event": "ping", "ts": 1717}\n']
        assert _is_machine_noise_block(block) is True

    def test_instruction_block_not_noise(self):
        """带用户指令特征的块即便混了 hash 也不算噪声。"""
        block = ["帮我把 " + "a" * 40 + " 这个 commit 回滚\n"]
        assert _is_machine_noise_block(block) is False

    def test_noise_user_turn_excluded_from_map(self):
        """纯机器签名的 ## User 回合不进地图,真实意图回合保留。"""
        md = ("## User\n\n" + "f" * 40 + "\n\n"
              "## Assistant\n\nok\n\n"
              "## User\n\n请帮我部署服务\n\n## Assistant\n\ndone\n")
        lines = md.splitlines(keepends=True)
        queries = _extract_user_queries(lines)
        # 第一个 ## User（纯 SHA）被过滤,只剩真实意图回合。
        snippets = [snip for _, snip in queries]
        assert any("部署" in s for s in snippets)
        assert not any(set(s) == {"f"} for s in snippets)


# ────────────────────────────────────────────────────────────────────
# 首轮单趟：两个 atom + 行号坐标 + EOF 覆盖
# ────────────────────────────────────────────────────────────────────

class TestFirstRunSinglePass:
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
        # 首 atom 从 floor(1) 起（并前言）；终点 = 下一 atom 起点（17）。
        assert atoms[0].offset_start == 1
        assert atoms[0].offset_end == 17
        # 末 atom 终点 = 末行号+1（23+1=24）—— EOF 硬覆盖。
        assert atoms[1].offset_start == 17
        assert atoms[1].offset_end == 24
        # 半开衔接无重叠无缝隙
        assert atoms[0].offset_end == atoms[1].offset_start
        # 链表
        assert atoms[0].pre_atom_id is None
        assert atoms[0].post_atom_id == atoms[1].atom_id
        assert atoms[1].pre_atom_id == atoms[0].atom_id
        assert atoms[1].ux_score == 7
        # raw_segment 按行区间切回
        assert atoms[0].raw_segment == _seg(_TRAJ_MD, 1, 17)
        assert atoms[1].raw_segment == _seg(_TRAJ_MD, 17, 24)
        # 落盘
        assert (traj_dir / "traj_x" / "tasks" /
                f"{atoms[0].atom_id}.json").is_file()

    def test_preamble_folded_into_first_atom(self, tmp_path):
        """首个 ## User 前有大段前言时,前言并入首 atom（offset_start=1）。"""
        traj_dir = tmp_path / "cc-sessions"
        traj_dir.mkdir()
        traj_path = traj_dir / "traj_pre.md"
        preamble = "## System\n\n" + "\n".join(f"note {i}" for i in range(50))
        full = preamble + "\n\n## User\n\n请部署服务\n\n## Assistant\n\ndone\n"
        traj_path.write_text(full, encoding="utf-8")
        total = len(full.splitlines(keepends=True))
        store = AtomTaskStore(root=traj_dir)
        atoms = TaskAgent(
            agno_agent_factory=_AutoSplitAgno, store=store,
        ).run(traj_id="traj_pre", traj_path=traj_path)
        assert len(atoms) == 1
        assert atoms[0].offset_start == 1            # 前言并入
        assert atoms[0].offset_end == total + 1      # 盖到 EOF

    def test_user_msg_is_query_map_without_assistant_body(self, tmp_path):
        """context-0 只放 User 地图,不含 assistant 正文。"""
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
        user_msg = factory.captured["user_msg"]
        # 地图带行号标记 + 元信息
        assert "[line:5] ## User" in user_msg
        assert "[line:17] ## User" in user_msg
        assert "trajid: traj_p" in user_msg
        assert "续接点）: 第 1 行" in user_msg
        assert "total_lines: 23" in user_msg
        # assistant 正文不进 context-0
        assert "Cloning..." not in user_msg
        assert "Editing CSS" not in user_msg
        # 首次拆分无 prior 衔接块
        assert "首次拆分" in user_msg
        assert all(r.startswith("ok:") for r in factory.captured["results"])

    def test_four_agentic_tools_exposed(self, tmp_path):
        """agent 拿到 4 个工具：submit_atom / look / context_budget / my_atoms。"""
        traj_dir = tmp_path / "cc-sessions"
        traj_dir.mkdir()
        traj_path = traj_dir / "traj_t.md"
        traj_path.write_text(_TRAJ_MD, encoding="utf-8")
        store = AtomTaskStore(root=traj_dir)
        factory = _scripted_factory([
            dict(start_line=5, intent="i", summary="s"),
        ])
        TaskAgent(agno_agent_factory=factory, store=store).run(
            traj_id="traj_t", traj_path=traj_path)
        assert factory.captured["tool_names"] == [
            "context_budget", "look", "my_atoms", "submit_atom"]


# ────────────────────────────────────────────────────────────────────
# 单趟覆盖多回合（曾经窗机制只拆第一窗 → 漏拆）
# ────────────────────────────────────────────────────────────────────

class TestSinglePassCoversAll:
    def test_many_turns_all_split_in_one_run(self, tmp_path):
        """6 个 user turn 一趟全拆 + 覆盖到 EOF（弃窗后无字符上限,不漏拆）。"""
        traj_dir = tmp_path / "cc-sessions"
        traj_dir.mkdir()
        traj_path = traj_dir / "traj_big.md"
        segs = [f"## User\n\nTask {i}\n\n## Assistant\n\ndone\n" for i in range(6)]
        full = "".join(segs)
        traj_path.write_text(full, encoding="utf-8")
        total_lines = len(full.splitlines(keepends=True))
        store = AtomTaskStore(root=traj_dir)

        atoms = TaskAgent(
            agno_agent_factory=_AutoSplitAgno, store=store,
        ).run(traj_id="traj_big", traj_path=traj_path)

        assert len(atoms) == 6
        assert len(store.list_by_traj("traj_big")) == 6
        # 续接点覆盖到文件末尾（半开：末行号+1）
        assert store.last_offset("traj_big") == total_lines + 1
        ordered = sorted(store.list_by_traj("traj_big"),
                         key=lambda a: a.offset_start)
        for prev, cur in zip(ordered, ordered[1:]):
            assert prev.offset_end == cur.offset_start
            assert cur.pre_atom_id == prev.atom_id
            assert prev.post_atom_id == cur.atom_id


# ────────────────────────────────────────────────────────────────────
# 增量续拆：append 新 user turn → 只切新增
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
        # 增量 atom 起点 = resume 行（上一轮末 atom 的 offset_end = 24）,
        # 终点 = 末行号+1
        assert new_atoms[0].offset_start == 24
        assert new_atoms[0].offset_end == total_lines + 1
        assert new_atoms[0].pre_atom_id is not None
        assert new_atoms[0].pre_atom_id.startswith("atom_traj_y_")
        prior = store.load(new_atoms[0].pre_atom_id)
        assert prior.post_atom_id == new_atoms[0].atom_id
        assert len(store.list_by_traj("traj_y")) == 3
        # 增量 user_msg：续接点=24 + 列出已拆 atom 衔接块
        user_msg = factory.captured["user_msg"]
        assert f"[line:{new_user_line}] ## User" in user_msg
        assert "续接点）: 第 24 行" in user_msg
        assert "atom-line: 1-17" in user_msg or "atom-line: 17-24" in user_msg
        assert str(traj_path) in user_msg

    def test_no_new_content_returns_empty_no_agent_call(self, tmp_path):
        traj_dir = tmp_path / "cc-sessions"
        traj_dir.mkdir()
        traj_path = traj_dir / "traj_z.md"
        traj_path.write_text(_TRAJ_MD, encoding="utf-8")
        store = AtomTaskStore(root=traj_dir)
        TaskAgent(agno_agent_factory=_AutoSplitAgno, store=store).run(
            traj_id="traj_z", traj_path=traj_path)

        calls = {"n": 0}

        def _spy_factory(*, instructions, tools):
            calls["n"] += 1
            return _AutoSplitAgno(instructions=instructions, tools=tools)

        result = TaskAgent(agno_agent_factory=_spy_factory, store=store).run(
            traj_id="traj_z", traj_path=traj_path)
        assert result == []
        assert calls["n"] == 0

    def test_no_user_header_returns_empty_no_agent_call(self, tmp_path):
        """全文无 ## User → 无可切边界,直接返回,不构造 agent,不 raise。"""
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
# 0 提交抛错（有 User 轮却 0 提交 → 不静默产空）
# ────────────────────────────────────────────────────────────────────

class TestZeroSubmitRaises:
    def test_user_turns_but_zero_submit_raises(self, tmp_path):
        traj_dir = tmp_path / "cc-sessions"
        traj_dir.mkdir()
        traj_path = traj_dir / "traj_e.md"
        traj_path.write_text(_TRAJ_MD, encoding="utf-8")
        store = AtomTaskStore(root=traj_dir)
        factory = _scripted_factory([])  # 不调 submit_atom

        with pytest.raises(RuntimeError, match="0 提交"):
            TaskAgent(agno_agent_factory=factory, store=store).run(
                traj_id="traj_e", traj_path=traj_path)
        # 抛错后不落盘
        assert store.list_by_traj("traj_e") == []

    def test_run_status_error_raises(self, tmp_path):
        """agno run_response.status=ERROR → raise（不把静默失败当成功）。"""
        traj_dir = tmp_path / "cc-sessions"
        traj_dir.mkdir()
        traj_path = traj_dir / "traj_s.md"
        traj_path.write_text(_TRAJ_MD, encoding="utf-8")
        store = AtomTaskStore(root=traj_dir)

        class _Status:
            value = "ERROR"

        factory = _scripted_factory([
            dict(start_line=5, intent="i", summary="s"),
        ], status=_Status())
        with pytest.raises(RuntimeError, match="ERROR"):
            TaskAgent(agno_agent_factory=factory, store=store).run(
                traj_id="traj_s", traj_path=traj_path)


# ────────────────────────────────────────────────────────────────────
# submit_atom 提交即校验：非法 → error 返回 + 不落盘（含撤销不切）
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
        # 第 9 行是 ## Assistant,不是 ## User 回合
        factory = _scripted_factory([
            dict(start_line=9, intent="i", summary="s"),
            dict(start_line=5, intent="i2", summary="s2"),  # 补一个合法的
        ])
        atoms = TaskAgent(agno_agent_factory=factory, store=store).run(
            traj_id="traj_e", traj_path=traj_path)
        assert factory.captured["results"][0].startswith("error:")
        assert "## User" in factory.captured["results"][0]
        # 合法那条仍落盘 → 单 atom 覆盖到 EOF
        assert len(atoms) == 1
        assert atoms[0].offset_start == 1

    def test_undo_revert_backward_line_rejected(self, tmp_path):
        """撤销/反悔不切：倒退的 start_line（回到上一意图）被拒,不另起 atom。"""
        traj_path, store = self._setup(tmp_path)
        factory = _scripted_factory([
            dict(start_line=17, intent="i1", summary="s1"),
            dict(start_line=5, intent="撤销回上一个", summary="s2"),  # 倒退 → reject
        ])
        atoms = TaskAgent(agno_agent_factory=factory, store=store).run(
            traj_id="traj_e", traj_path=traj_path)
        assert len(atoms) == 1
        # 唯一 atom 从 floor(1) 起,盖到 EOF
        assert atoms[0].offset_start == 1
        assert atoms[0].offset_end == 24
        assert factory.captured["results"][0].startswith("ok:")
        assert factory.captured["results"][1].startswith("error:")
        assert "严格大于" in factory.captured["results"][1]

    def test_missing_required_field_then_valid(self, tmp_path):
        traj_path, store = self._setup(tmp_path)
        factory = _scripted_factory([
            dict(start_line=5, intent="i", summary=""),   # 缺 summary → reject
            dict(start_line=5, intent="i", summary="s"),  # 改对重提
        ])
        atoms = TaskAgent(agno_agent_factory=factory, store=store).run(
            traj_id="traj_e", traj_path=traj_path)
        assert factory.captured["results"][0].startswith("error:")
        assert "必填" in factory.captured["results"][0]
        assert len(atoms) == 1


# ────────────────────────────────────────────────────────────────────
# look 工具：读某行附近原文（含向前看）
# ────────────────────────────────────────────────────────────────────

class TestLookTool:
    def test_look_returns_numbered_neighborhood(self, tmp_path):
        traj_dir = tmp_path / "cc-sessions"
        traj_dir.mkdir()
        traj_path = traj_dir / "traj_l.md"
        traj_path.write_text(_TRAJ_MD, encoding="utf-8")
        store = AtomTaskStore(root=traj_dir)
        captured_look: dict = {}

        def factory(*, instructions, tools):
            captured_look["instructions"] = instructions
            toolmap = {_tool_name(t): t for t in tools}

            class _A:
                def run(self, _user_msg, **_keyword_arguments):
                    # 用 look 读第 5 行（## User）附近,验证向前看包含 assistant
                    captured_look["out"] = _call_tool(
                        toolmap["look"], line=11, before=3, after=2)
                    _call_tool(
                        toolmap["submit_atom"],
                        start_line=5,
                        intent="i",
                        summary="s",
                    )
                    return _RunResult()

            return _A()

        TaskAgent(agno_agent_factory=factory, store=store).run(
            traj_id="traj_l", traj_path=traj_path)
        out = captured_look["out"]
        # 含行号前缀 + 读到 assistant 正文（弃窗后唯一进上下文的途径）
        assert "11: Cloning..." in out
        assert re.search(r"^\d+: ", out, re.MULTILINE)

    def test_my_atoms_reports_submitted_spans(self, tmp_path):
        traj_dir = tmp_path / "cc-sessions"
        traj_dir.mkdir()
        traj_path = traj_dir / "traj_m.md"
        traj_path.write_text(_TRAJ_MD, encoding="utf-8")
        store = AtomTaskStore(root=traj_dir)
        captured_my: dict = {}

        def factory(*, instructions, tools):
            captured_my["instructions"] = instructions
            toolmap = {_tool_name(t): t for t in tools}

            class _A:
                def run(self, _user_msg, **_keyword_arguments):
                    _call_tool(
                        toolmap["submit_atom"],
                        start_line=5,
                        intent="i",
                        summary="s",
                    )
                    _call_tool(
                        toolmap["submit_atom"],
                        start_line=17,
                        intent="i2",
                        summary="s2",
                    )
                    captured_my["out"] = _call_tool(toolmap["my_atoms"])
                    captured_my["budget"] = _call_tool(toolmap["context_budget"])
                    return _RunResult()

            return _A()

        TaskAgent(agno_agent_factory=factory, store=store).run(
            traj_id="traj_m", traj_path=traj_path)
        # 两个区间：[5,17) [17,24)
        assert "[5,17)" in captured_my["out"]
        assert "[17,24)" in captured_my["out"]
        # context_budget 返回 JSON 含 used/max/remaining
        assert '"max_tokens"' in captured_my["budget"]
        assert '"used_tokens"' in captured_my["budget"]


# ────────────────────────────────────────────────────────────────────
# SYSTEM_PROMPT 共享一份：含严格分档表 + 4 工具协议 + 弃窗/撤销原则
# ────────────────────────────────────────────────────────────────────

class TestSystemPrompt:
    def test_prompt_contains_rubric_tools_and_principles(self, tmp_path):
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
        # 4 工具协议
        assert "submit_atom" in system_msg
        assert "look" in system_msg
        assert "context_budget" in system_msg
        assert "my_atoms" in system_msg
        assert "start_line" in system_msg
        # 弃窗 + 撤销原则
        assert "弃窗单趟" in system_msg
        assert "撤销" in system_msg

    def test_literal_braces_in_query_snippet_are_safe(self, tmp_path):
        """User 提问含 ``{}`` 字面量时,模板注入不二次解析。"""
        traj_dir = tmp_path / "cc-sessions"
        traj_dir.mkdir()
        traj_path = traj_dir / "traj_b.md"
        traj_path.write_text(
            "## User\n\n聊到 {start_line} 这个占位\n\n## Assistant\n\nok\n",
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
        assert "{start_line}" in user_msg


class TestInterestFilter:
    def test_empty_interests_keep_existing_four_tools(self, tmp_path):
        traj_dir = tmp_path / "cc-sessions"
        traj_dir.mkdir()
        traj_path = traj_dir / "traj_interest_empty.md"
        traj_path.write_text(_TRAJ_MD, encoding="utf-8")
        store = AtomTaskStore(root=traj_dir)
        factory = _scripted_factory([
            dict(start_line=5, intent="deploy", summary="done"),
            dict(start_line=17, intent="frontend", summary="done"),
        ])

        TaskAgent(
            agno_agent_factory=factory, store=store, interests=[],
        ).run(traj_id="traj_interest_empty", traj_path=traj_path)

        assert factory.captured["tool_names"] == [
            "context_budget", "look", "my_atoms", "submit_atom",
        ]

    def test_non_empty_interests_add_tool_and_prompt(self, tmp_path):
        traj_dir = tmp_path / "cc-sessions"
        traj_dir.mkdir()
        traj_path = traj_dir / "traj_interest_prompt.md"
        traj_path.write_text(_TRAJ_MD, encoding="utf-8")
        store = AtomTaskStore(root=traj_dir)
        factory = _scripted_factory([
            dict(start_line=5, intent="deploy", summary="done"),
            dict(start_line=17, intent="frontend", summary="done"),
        ])

        TaskAgent(
            agno_agent_factory=factory,
            store=store,
            interests=[" 基础设施 ", "docker"],
        ).run(traj_id="traj_interest_prompt", traj_path=traj_path)

        assert "mark_not_fit" in factory.captured["tool_names"]
        system_prompt = factory.captured["instructions"][0]
        assert "兴趣过滤" in system_prompt
        assert "- 基础设施" in system_prompt
        assert "- docker" in system_prompt

    def test_mark_not_fit_raises_without_writing_atoms(self, tmp_path):
        traj_dir = tmp_path / "cc-sessions"
        traj_dir.mkdir()
        traj_path = traj_dir / "traj_not_fit.md"
        traj_path.write_text(_TRAJ_MD, encoding="utf-8")
        store = AtomTaskStore(root=traj_dir)

        def factory(*, instructions, tools):
            assert instructions
            toolmap = {_tool_name(tool): tool for tool in tools}

            class _Agent:
                def run(self, _user_msg, **_keyword_arguments):
                    _call_tool(toolmap["mark_not_fit"], reason="not infra")
                    return _RunResult()

            return _Agent()

        with pytest.raises(TrajectoryNotFit, match="not infra"):
            TaskAgent(
                agno_agent_factory=factory,
                store=store,
                interests=["infra"],
            ).run(traj_id="traj_not_fit", traj_path=traj_path)

        assert store.list_by_traj("traj_not_fit") == []
        assert not (traj_dir / "traj_not_fit" / "tasks").exists()

    def test_submit_atom_after_mark_not_fit_returns_error(self, tmp_path):
        traj_dir = tmp_path / "cc-sessions"
        traj_dir.mkdir()
        traj_path = traj_dir / "traj_submit_after_not_fit.md"
        traj_path.write_text(_TRAJ_MD, encoding="utf-8")
        store = AtomTaskStore(root=traj_dir)
        captured: dict = {"results": []}

        def factory(*, instructions, tools):
            assert instructions
            toolmap = {_tool_name(tool): tool for tool in tools}

            class _Agent:
                def run(self, _user_msg, **_keyword_arguments):
                    captured["results"].append(
                        _call_tool(toolmap["mark_not_fit"], reason="not infra")
                    )
                    captured["results"].append(
                        _call_tool(
                            toolmap["submit_atom"],
                            start_line=5,
                            intent="deploy",
                            summary="done",
                        )
                    )
                    return _RunResult()

            return _Agent()

        with pytest.raises(TrajectoryNotFit):
            TaskAgent(
                agno_agent_factory=factory,
                store=store,
                interests=["infra"],
            ).run(traj_id="traj_submit_after_not_fit", traj_path=traj_path)

        assert captured["results"][0].startswith("ok:")
        assert captured["results"][1].startswith("error:")
        assert store.list_by_traj("traj_submit_after_not_fit") == []

    def test_mark_not_fit_after_submit_atom_returns_error_and_keeps_atom(self, tmp_path):
        traj_dir = tmp_path / "cc-sessions"
        traj_dir.mkdir()
        traj_path = traj_dir / "traj_mark_after_submit.md"
        traj_path.write_text(_TRAJ_MD, encoding="utf-8")
        store = AtomTaskStore(root=traj_dir)
        captured: dict = {"results": []}

        def factory(*, instructions, tools):
            assert instructions
            toolmap = {_tool_name(tool): tool for tool in tools}

            class _Agent:
                def run(self, _user_msg, **_keyword_arguments):
                    captured["results"].append(
                        _call_tool(
                            toolmap["submit_atom"],
                            start_line=5,
                            intent="deploy",
                            summary="done",
                        )
                    )
                    captured["results"].append(
                        _call_tool(toolmap["mark_not_fit"], reason="not infra")
                    )
                    return _RunResult()

            return _Agent()

        atoms = TaskAgent(
            agno_agent_factory=factory,
            store=store,
            interests=["infra"],
        ).run(traj_id="traj_mark_after_submit", traj_path=traj_path)

        assert len(atoms) == 1
        assert captured["results"][0].startswith("ok:")
        assert captured["results"][1].startswith("error:")


# ────────────────────────────────────────────────────────────────────
# 上下文自管理（context_budget 模块）：剪裁 + 超长兜底 + max_context 解析
# ────────────────────────────────────────────────────────────────────

class TestContextManager:
    def test_resolve_max_context_config_first(self):
        from xskill.agents.context_budget import resolve_max_context
        assert resolve_max_context({"max_context": 128000}) == 128000

    def test_resolve_max_context_default_with_warning(self, caplog):
        from xskill.agents.context_budget import (
            resolve_max_context, DEFAULT_MAX_CONTEXT)
        import logging
        with caplog.at_level(logging.WARNING):
            assert resolve_max_context({}) == DEFAULT_MAX_CONTEXT
        assert any("max_context" in r.message for r in caplog.records)

    def test_proactive_trim_of_old_look_results(self):
        """到 85% 上限时,旧 look 工具返回被纯截断（不调模型）。"""
        from xskill.agents.context_budget import ContextManager, _TRIM_MARK

        class _Msg:
            def __init__(self, role, content, tool_name=None):
                self.role = role
                self.content = content
                self.tool_name = tool_name

        # max_context 小,凑一条超大的 look 返回触发剪裁。
        cm = ContextManager(max_context=1000)
        big = "x" * 8000  # ~2000 token,远超 85%*1000=850
        messages = [
            _Msg("user", "map"),
            _Msg("tool", big, tool_name="look"),
        ]
        sent = {}

        def fake_invoke(msgs, **_keyword_arguments):
            sent["look_content"] = msgs[1].content
            return {"usage": {"prompt_tokens": 123}}

        managed = cm.wrap(fake_invoke)
        managed(messages)
        # 旧 look 返回被剪成标记
        assert sent["look_content"] == _TRIM_MARK

    def test_trim_spills_atom_task_read_result_with_metadata(self, tmp_path):
        """SkillEditAgent 的旧 atom_task_read 结果应落临时文件,上下文只留摘要+路径。"""
        import json
        import re
        from xskill.agents.context_budget import ContextManager

        class _Msg:
            def __init__(self, role, content, tool_name=None):
                self.role = role
                self.content = content
                self.tool_name = tool_name

        atom_payload = {
            "atom_id": "atom_traj_demo_0001",
            "traj_id": "traj_demo",
            "offset_start": 10,
            "offset_end": 30,
            "intent": "修复 SkillEditAgent 上下文超限",
            "summary": "读 atom 后 raw_segment 过大导致下一轮请求超窗",
            "raw_segment": "RAW_SEGMENT_SHOULD_NOT_STAY_IN_CONTEXT\n" * 500,
        }
        original_tool_content = json.dumps(atom_payload, ensure_ascii=False)
        messages = [
            _Msg("user", "scenario"),
            _Msg("tool", original_tool_content, "atom_task_read"),
        ]
        sent = {}

        def fake_invoke(msgs, **_keyword_arguments):
            sent["tool_content"] = msgs[1].content
            return {"usage": {"prompt_tokens": 123}}

        cm = ContextManager(max_context=1000, spill_root=tmp_path / "spill")
        cm.wrap(fake_invoke)(messages)

        content = sent["tool_content"]
        assert "trimmed_tool_result" in content
        assert "tool_name: atom_task_read" in content
        assert "atom_id: atom_traj_demo_0001" in content
        assert "intent: 修复 SkillEditAgent 上下文超限" in content
        assert "summary: 读 atom 后 raw_segment 过大导致下一轮请求超窗" in content
        assert "RAW_SEGMENT_SHOULD_NOT_STAY_IN_CONTEXT" not in content
        spill_path = re.search(r"spill_path: (.+)", content).group(1).strip()
        assert Path(spill_path).read_text(encoding="utf-8") == original_tool_content

    def test_compact_runs_after_spill_when_history_still_exceeds_limit(self, tmp_path):
        """spill 后仍超 compact_token_limit 时,应调用 compact_fn 并收敛历史。"""
        from xskill.agents.context_budget import ContextManager

        class _Msg:
            def __init__(self, role, content, tool_name=None):
                self.role = role
                self.content = content
                self.tool_name = tool_name

        compact_calls: list[str] = []

        def compact_fn(prompt: str) -> str:
            compact_calls.append(prompt)
            return "COMPACTED: keep atom evidence and pending edits"

        messages = [
            _Msg("system", "SkillEditAgent system prompt"),
            _Msg("user", "turn0 scenario with target skill"),
            _Msg("assistant", "I will inspect atoms"),
            _Msg("tool", "OLD_ATOM_RESULT\n" + ("x" * 8000), "atom_task_read"),
            _Msg("assistant", "recent reasoning"),
            _Msg("user", "recent continuation"),
        ]
        seen = {}

        def fake_invoke(msgs, **_keyword_arguments):
            seen["messages"] = msgs
            return {"usage": {"prompt_tokens": 100}}

        cm = ContextManager(
            max_context=1000,
            spill_root=tmp_path / "spill",
            compact_token_limit=20,
            compact_keep_recent_messages=2,
            compact_fn=compact_fn,
        )
        cm.wrap(fake_invoke)(messages)

        assert len(compact_calls) == 1
        assert "SkillEditAgent working memory" in compact_calls[0]
        assert "spill_path:" in compact_calls[0]
        compacted = seen["messages"]
        assert [m.role for m in compacted] == [
            "system", "user", "assistant", "assistant", "user",
        ]
        assert compacted[0].content == "SkillEditAgent system prompt"
        assert compacted[1].content == "turn0 scenario with target skill"
        assert "COMPACTED" in compacted[2].content
        assert compacted[-2].content == "recent reasoning"
        assert compacted[-1].content == "recent continuation"

    def test_compact_recent_tail_does_not_keep_orphan_tool_message(self, tmp_path):
        """compact 后不能留下没有对应 assistant tool_calls 的 tool 消息。"""
        from xskill.agents.context_budget import ContextManager

        class _Msg:
            def __init__(self, role, content, tool_name=None, tool_call_id=None):
                self.role = role
                self.content = content
                self.tool_name = tool_name
                self.tool_call_id = tool_call_id

        messages = [
            _Msg("system", "SkillEditAgent system prompt"),
            _Msg("user", "turn0 scenario"),
            _Msg("assistant", "old reasoning"),
            _Msg("tool", "OLD_ATOM_RESULT\n" + ("x" * 8000), "atom_task_read"),
            _Msg("tool", "RECENT_TOOL_WITHOUT_ASSISTANT", "read_file", "call_recent"),
            _Msg("user", "continue after tool"),
        ]
        seen = {}

        def fake_invoke(msgs, **_keyword_arguments):
            seen["messages"] = msgs
            return {"usage": {"prompt_tokens": 100}}

        cm = ContextManager(
            max_context=1000,
            spill_root=tmp_path / "spill",
            compact_token_limit=20,
            compact_keep_recent_messages=2,
            compact_fn=lambda _prompt: "summary includes recent tool evidence",
        )
        cm.wrap(fake_invoke)(messages)

        compacted = seen["messages"]
        assert [m.role for m in compacted] == [
            "system", "user", "assistant", "assistant", "user",
        ]
        assert all(m.content != "RECENT_TOOL_WITHOUT_ASSISTANT" for m in compacted)
        assert compacted[-1].content == "continue after tool"

    def test_overlong_error_triggers_retrim_and_resend(self):
        from xskill.agents.context_budget import ContextManager, _TRIM_MARK

        class _Msg:
            def __init__(self, role, content, tool_name=None):
                self.role = role
                self.content = content
                self.tool_name = tool_name

        cm = ContextManager(max_context=100000)
        messages = [
            _Msg("user", "map"),
            _Msg("tool", "y" * 4000, tool_name="look"),
        ]
        calls = {"n": 0}

        def flaky_invoke(_messages, **_keyword_arguments):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("maximum context length exceeded")
            return {"usage": {"prompt_tokens": 50}}

        managed = cm.wrap(flaky_invoke)
        resp = managed(messages)
        assert calls["n"] == 2                 # 兜底重发了一次
        assert messages[1].content == _TRIM_MARK
        assert resp["usage"]["prompt_tokens"] == 50

    def test_non_overlong_error_propagates(self):
        from xskill.agents.context_budget import ContextManager
        cm = ContextManager(max_context=100000)

        def boom(_messages, **_keyword_arguments):
            raise ValueError("totally unrelated failure")

        with pytest.raises(ValueError, match="unrelated"):
            cm.wrap(boom)([])

    def test_records_real_prompt_tokens(self):
        from xskill.agents.context_budget import (
            ContextManager, get_used_tokens)
        cm = ContextManager(max_context=100000)

        def ok_invoke(_messages, **_keyword_arguments):
            return {"usage": {"prompt_tokens": 777}}

        cm.wrap(ok_invoke)([])
        assert get_used_tokens() == 777
