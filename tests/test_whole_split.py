"""WholeTrajSplitter + runner whole-mode 消融拆分测试
================================================================================

覆盖 ``atom.split_mode: whole``（环境变量 ``XSKILL_SPLIT_MODE=whole``）消融路径：
- 整条多轮轨迹 → 恰好 1 个 AtomTask，raw_segment 覆盖整段、offset 正确、
  intent 非空、atom_id 合法。
- agentic 默认（split_mode 未设）行为零改变。
- 续写场景：updated 轨迹只把新增段成 1 atom（不重复整条）。
- config.split_mode_config 的 env 覆盖 / 非法值抛错。
"""
from __future__ import annotations

import os

import pytest

from xskill.agents.whole_splitter import WholeTrajSplitter
from xskill.pipeline.atom import AtomTask, AtomTaskStore
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


# ════════════════════════════════════════════════════════════════════
# WholeTrajSplitter：整轨成 1 atom
# ════════════════════════════════════════════════════════════════════

class TestWholeSplitterFirstPass:
    def test_one_atom_covers_whole_traj(self, tmp_path):
        store = AtomTaskStore(root=tmp_path)
        md = _write_traj(tmp_path)
        atoms = WholeTrajSplitter(store=store, traj_root=tmp_path).run(
            traj_id="traj_demo", traj_path=md)

        assert len(atoms) == 1
        a = atoms[0]
        total = len(_TRAJ_MD.splitlines(keepends=True))
        # 整条轨迹（首行到 EOF）成一段。
        assert a.offset_start == 1
        assert a.offset_end == total + 1
        # raw_segment 覆盖整条轨迹原文。
        assert a.raw_segment == _TRAJ_MD
        # intent 启发式取首条 User 请求首句，非空。
        assert a.intent.strip()
        assert a.intent == "Deploy xquiz to 1717."
        assert a.summary.strip()
        # atom_id 同构、合法、首条序号 0001。
        assert a.atom_id == "atom_traj_demo_0001"
        # source_model 继承 sidecar。
        assert a.source_model == "deepseek-v4"
        # 已落盘且可被 store 读回（下游 cluster/edit 据此引用）。
        assert store.load(a.atom_id).atom_id == a.atom_id

    def test_no_user_turn_still_one_atom(self, tmp_path):
        """无 ## User 回合的轨迹（纯日志）仍整段成 1 atom，intent 退到首条非空行。"""
        store = AtomTaskStore(root=tmp_path)
        text = "## System\n\nbootstrapping the agent\n\nrunning task\n"
        md = _write_traj(tmp_path, text=text)
        atoms = WholeTrajSplitter(store=store, traj_root=tmp_path).run(
            traj_id="traj_demo", traj_path=md)
        assert len(atoms) == 1
        assert atoms[0].intent.strip()
        assert atoms[0].raw_segment == text

    def test_empty_traj_zero_atoms(self, tmp_path):
        store = AtomTaskStore(root=tmp_path)
        md = _write_traj(tmp_path, text="\n\n  \n")
        atoms = WholeTrajSplitter(store=store, traj_root=tmp_path).run(
            traj_id="traj_demo", traj_path=md)
        assert atoms == []


class TestWholeSplitterContinuation:
    def test_continuation_only_new_segment(self, tmp_path):
        """续写：已有 1 atom 覆盖 [1, N)，新增段只成 1 个新 atom（不重复整条）。"""
        store = AtomTaskStore(root=tmp_path)
        md = _write_traj(tmp_path)
        first = WholeTrajSplitter(store=store, traj_root=tmp_path).run(
            traj_id="traj_demo", traj_path=md)
        assert len(first) == 1
        old_end = first[0].offset_end  # = total + 1

        # 轨迹增长：追加一个新 User 回合。
        appended = _TRAJ_MD + "\n## User\n\nAdd dark mode toggle.\n\n## Assistant\n\nDone.\n"
        md.write_text(appended, encoding="utf-8")

        second = WholeTrajSplitter(store=store, traj_root=tmp_path).run(
            traj_id="traj_demo", traj_path=md)
        assert len(second) == 1
        new = second[0]
        # 新 atom 从旧 last_offset（= old_end）起，不重叠已拆段。
        assert new.offset_start == old_end
        total2 = len(appended.splitlines(keepends=True))
        assert new.offset_end == total2 + 1
        # raw_segment 只含新增段，不含已拆的整条。
        assert "Deploy xquiz to 1717." not in new.raw_segment
        assert "Add dark mode toggle." in new.raw_segment
        # 序号递增 + 链表衔接。
        assert new.atom_id == "atom_traj_demo_0002"
        assert new.pre_atom_id == first[0].atom_id
        # store 共 2 个 atom，无缝铺满。
        all_atoms = store.list_by_traj("traj_demo")
        assert len(all_atoms) == 2

    def test_updated_but_no_new_lines_zero_atoms(self, tmp_path):
        """updated 但无新增行（last_offset 已到 EOF）→ 产 0 atom。"""
        store = AtomTaskStore(root=tmp_path)
        md = _write_traj(tmp_path)
        WholeTrajSplitter(store=store, traj_root=tmp_path).run(
            traj_id="traj_demo", traj_path=md)
        again = WholeTrajSplitter(store=store, traj_root=tmp_path).run(
            traj_id="traj_demo", traj_path=md)
        assert again == []


# ════════════════════════════════════════════════════════════════════
# runner._do_split：whole 分支 vs agentic 默认零改变
# ════════════════════════════════════════════════════════════════════

class TestRunnerDoSplitWholeMode:
    def test_whole_mode_one_atom_no_llm(self, tmp_path):
        """whole 模式 _do_split：不传 agno 工厂（不调 LLM）也能产 1 atom。"""
        store = AtomTaskStore(root=tmp_path)
        md = _write_traj(tmp_path)
        w = DirectoryWatcher(
            store=store, skill_dir=None,
            agno_agent_factory=None,  # 关键：whole 不触发工厂
            split_mode="whole",
        )
        fname, num, last_off, last_id, err = w._do_split(tmp_path, md.name)
        assert err is None
        assert num == 1
        assert last_id == "atom_traj_demo_0001"
        atoms = store.list_by_traj("traj_demo")
        assert len(atoms) == 1
        assert atoms[0].raw_segment == _TRAJ_MD

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
        # config 说 agentic，env 说 whole → env 赢。
        assert split_mode_config({"atom": {"split_mode": "agentic"}}) == "whole"

    def test_env_invalid_raises(self, monkeypatch):
        monkeypatch.setenv(SPLIT_MODE_ENV, "garbage")
        with pytest.raises(ValueError):
            split_mode_config({})

    def test_config_invalid_raises(self):
        with pytest.raises(ValueError):
            split_mode_config({"atom": {"split_mode": "halfway"}})
