"""whole 模式（整轨成 1 atom）测试 —— 由 TaskAgent + WHOLE_SYSTEM_PROMPT 实现
================================================================================

whole 模式（``atom.split_mode: whole`` / 环境变量 ``XSKILL_SPLIT_MODE=whole``）
**仍走 TaskAgent（LLM）**，只是选 ``WHOLE_SYSTEM_PROMPT`` 而非默认 ``SYSTEM_PROMPT``：
- TaskAgent 按 ``split_mode`` 选 prompt（whole → "整轨 1 atom" 指示）。
- whole prompt 内容含"恰好 1 个 AtomTask"类整轨指示。
- whole 模式下 submit_atom 被调多次时，第 2 次起返回 error（代码兜死，确保 1 atom）。
- used_skills / ux_score 等字段照常由 LLM（stub）产出 → CS 归因不空转。
- runner._do_split：whole 与 agentic 都构造 TaskAgent，split_mode 正确透传。
- config.split_mode_config 的 env 覆盖 / 非法值抛错。
"""
from __future__ import annotations

import re

import pytest

from xskill.agents.task_agent import (
    TaskAgent,
    SYSTEM_PROMPT,
    WHOLE_SYSTEM_PROMPT,
)
from xskill.pipeline.atom import AtomTaskStore
from xskill.pipeline.runner import DirectoryWatcher
from xskill.config import split_mode_config, SPLIT_MODE_ENV


# 多轮轨迹：## User 在第 5、17 行，共 23 行（与 test_task_agent 同形态）。
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


def _write_traj(tmp_path, traj_id="traj_demo", text=_TRAJ_MD, model="deepseek-v4"):
    md = tmp_path / f"{traj_id}.md"
    md.write_text(text, encoding="utf-8")
    (tmp_path / f"{traj_id}.json").write_text(
        '{"model": "%s"}' % model, encoding="utf-8")
    return md


class _RunResult:
    """stub agno run_response（无 status 字段视为正常）。"""


def _whole_factory():
    """stub agno 工厂，模拟 whole 模式下的 LLM：**只调一次** submit_atom，
    start_line 取地图里第一个 [line:N]（= 续接点对应的首个 User 行），
    并照常填 used_skills / ux_score。把 instructions / tool 名记进 captured。
    """
    captured: dict = {"instructions": None, "tool_names": None,
                      "results": [], "user_msg": None}

    def factory(*, instructions, tools):
        toolmap = {getattr(t, "__name__", ""): t for t in tools}
        captured["instructions"] = instructions
        captured["tool_names"] = sorted(toolmap)

        class _A:
            def run(self, user_msg, **kw):
                captured["user_msg"] = user_msg
                lines = [int(n) for n in re.findall(r"\[line:(\d+)\]", user_msg)]
                # whole：只对第一个合法 User 行调一次 submit_atom。
                first = lines[0] if lines else 1
                captured["results"].append(toolmap["submit_atom"](
                    start_line=first, intent="whole traj intent",
                    summary="whole traj summary", tags=["deploy", "frontend"],
                    used_skills=["fullstack-web-deployment"], ux_score=8))
                return _RunResult()

        return _A()

    factory.captured = captured
    return factory


def _greedy_two_submit_factory():
    """stub：whole 模式下故意调两次 submit_atom，验证代码兜死只收 1 个。"""
    captured: dict = {"results": []}

    def factory(*, instructions, tools):
        toolmap = {getattr(t, "__name__", ""): t for t in tools}

        class _A:
            def run(self, user_msg, **kw):
                lines = sorted(set(
                    int(n) for n in re.findall(r"\[line:(\d+)\]", user_msg)))
                for ln in lines:
                    captured["results"].append(toolmap["submit_atom"](
                        start_line=ln, intent="i", summary="s",
                        tags=["t"], used_skills=[], ux_score=7))
                return _RunResult()

        return _A()

    factory.captured = captured
    return factory


# ════════════════════════════════════════════════════════════════════
# WHOLE_SYSTEM_PROMPT 内容 + TaskAgent 按 split_mode 选 prompt
# ════════════════════════════════════════════════════════════════════

class TestWholePromptSelection:
    def test_whole_prompt_says_one_atom(self):
        """whole prompt 含"整轨成恰好 1 个 atom / 只调一次 submit_atom"类指示。"""
        assert "恰好" in WHOLE_SYSTEM_PROMPT and "1 个" in WHOLE_SYSTEM_PROMPT
        assert "只调一次" in WHOLE_SYSTEM_PROMPT
        assert "整轨" in WHOLE_SYSTEM_PROMPT
        # 整条评 used_skills / ux_score 的指示在场。
        assert "used_skills" in WHOLE_SYSTEM_PROMPT
        assert "ux_score" in WHOLE_SYSTEM_PROMPT

    def test_agentic_prompt_unchanged_split_into_n(self):
        """默认 agentic prompt 仍是"切成 0~N 个 atom"。"""
        assert "0~N 个 AtomTask" in SYSTEM_PROMPT
        assert SYSTEM_PROMPT is not WHOLE_SYSTEM_PROMPT

    def test_taskagent_default_split_mode_agentic(self, tmp_path):
        ta = TaskAgent(agno_agent_factory=_whole_factory(),
                       store=AtomTaskStore(root=tmp_path))
        assert ta.split_mode == "agentic"
        assert ta._system_prompt is SYSTEM_PROMPT

    def test_taskagent_whole_selects_whole_prompt(self, tmp_path):
        ta = TaskAgent(agno_agent_factory=_whole_factory(),
                       store=AtomTaskStore(root=tmp_path), split_mode="whole")
        assert ta._system_prompt is WHOLE_SYSTEM_PROMPT

    def test_taskagent_invalid_split_mode_raises(self, tmp_path):
        with pytest.raises(ValueError):
            TaskAgent(agno_agent_factory=_whole_factory(),
                      store=AtomTaskStore(root=tmp_path), split_mode="bogus")


# ════════════════════════════════════════════════════════════════════
# whole 模式跑 TaskAgent：选 WHOLE_SYSTEM_PROMPT + 整轨 1 atom + 字段照常
# ════════════════════════════════════════════════════════════════════

class TestWholeModeRun:
    def test_whole_mode_passes_whole_prompt_to_factory(self, tmp_path):
        """whole 模式下传给 agno 工厂的 instructions = WHOLE_SYSTEM_PROMPT。"""
        store = AtomTaskStore(root=tmp_path)
        md = _write_traj(tmp_path)
        factory = _whole_factory()
        TaskAgent(agno_agent_factory=factory, store=store,
                  split_mode="whole").run(traj_id="traj_demo", traj_path=md)
        assert factory.captured["instructions"] == [WHOLE_SYSTEM_PROMPT]

    def test_whole_mode_one_atom_with_used_skills_and_ux(self, tmp_path):
        """whole 模式整轨成 1 atom，used_skills / ux_score 照常落盘（CS 归因不空转）。"""
        store = AtomTaskStore(root=tmp_path)
        md = _write_traj(tmp_path)
        atoms = TaskAgent(agno_agent_factory=_whole_factory(), store=store,
                          split_mode="whole").run(
            traj_id="traj_demo", traj_path=md)
        assert len(atoms) == 1
        a = atoms[0]
        total = len(_TRAJ_MD.splitlines(keepends=True))
        assert a.offset_start == 1
        assert a.offset_end == total + 1
        assert a.raw_segment == _TRAJ_MD
        # 关键：whole 模式也由 LLM 报 used_skills / ux_score（非空）。
        assert a.used_skills == ["fullstack-web-deployment"]
        assert a.ux_score == 8
        assert a.atom_id == "atom_traj_demo_0001"
        assert a.source_model == "deepseek-v4"

    def test_whole_mode_rejects_second_submit(self, tmp_path):
        """whole 模式 submit_atom 调第 2 次返回 error，最终仍只落 1 个 atom。"""
        store = AtomTaskStore(root=tmp_path)
        md = _write_traj(tmp_path)
        factory = _greedy_two_submit_factory()
        atoms = TaskAgent(agno_agent_factory=factory, store=store,
                          split_mode="whole").run(
            traj_id="traj_demo", traj_path=md)
        assert len(atoms) == 1
        results = factory.captured["results"]
        assert results[0].startswith("ok")
        # 第 2 次提交被代码兜死。
        assert any("error" in r and "whole" in r for r in results[1:])

    def test_agentic_mode_passes_default_prompt(self, tmp_path):
        """agentic 模式（默认）传给工厂的 instructions = SYSTEM_PROMPT（零改变）。"""
        store = AtomTaskStore(root=tmp_path)
        md = _write_traj(tmp_path)
        factory = _whole_factory()
        TaskAgent(agno_agent_factory=factory, store=store).run(
            traj_id="traj_demo", traj_path=md)
        assert factory.captured["instructions"] == [SYSTEM_PROMPT]


# ════════════════════════════════════════════════════════════════════
# runner._do_split：whole / agentic 都走 TaskAgent，split_mode 透传
# ════════════════════════════════════════════════════════════════════

class TestRunnerDoSplitWholeMode:
    def test_whole_mode_routes_through_task_agent(self, tmp_path):
        """whole 模式 _do_split 构造 TaskAgent 并透传 split_mode=whole。"""
        store = AtomTaskStore(root=tmp_path)
        md = _write_traj(tmp_path)
        factory = _whole_factory()
        w = DirectoryWatcher(
            store=store, skill_dir=None,
            agno_agent_factory=factory,
            split_mode="whole",
        )
        fname, num, last_off, last_id, err = w._do_split(tmp_path, md.name)
        assert err is None
        assert num == 1
        assert last_id == "atom_traj_demo_0001"
        # 走的是 whole prompt（证明经 TaskAgent，而非旧零-LLM splitter）。
        assert factory.captured["instructions"] == [WHOLE_SYSTEM_PROMPT]
        atoms = store.list_by_traj("traj_demo")
        assert len(atoms) == 1
        assert atoms[0].used_skills == ["fullstack-web-deployment"]
        assert atoms[0].ux_score == 8

    def test_default_mode_is_agentic(self):
        """split_mode 未设 → 默认 agentic（开关零行为改变）。"""
        w = DirectoryWatcher(store=None)
        assert w.split_mode == "agentic"

    def test_invalid_split_mode_raises(self):
        with pytest.raises(ValueError):
            DirectoryWatcher(store=None, split_mode="bogus")


# ════════════════════════════════════════════════════════════════════
# config.split_mode_config：env 覆盖 + 非法值
# ════════════════════════════════════════════════════════════════════

class TestSplitModeConfig:
    def test_config_field_default_agentic(self):
        assert split_mode_config({}) == "agentic"
        assert split_mode_config({"atom": {"split_mode": "agentic"}}) == "agentic"

    def test_config_field_whole(self):
        assert split_mode_config({"atom": {"split_mode": "whole"}}) == "whole"

    def test_env_overrides_config(self, monkeypatch):
        monkeypatch.setenv(SPLIT_MODE_ENV, "whole")
        assert split_mode_config({"atom": {"split_mode": "agentic"}}) == "whole"

    def test_env_invalid_raises(self, monkeypatch):
        monkeypatch.setenv(SPLIT_MODE_ENV, "garbage")
        with pytest.raises(ValueError):
            split_mode_config({})

    def test_config_invalid_raises(self):
        with pytest.raises(ValueError):
            split_mode_config({"atom": {"split_mode": "halfway"}})
