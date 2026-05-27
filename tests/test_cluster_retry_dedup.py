"""cluster partial-fail 重试 + 去重 + per-atom 日志单测

覆盖三组语义：
1. partial-fail（一个 atom 失败）→ traj 标 error + retry_count=1，没耗尽
   预算前不会被假冒成 done
2. 连续重试 ≥ MAX_CLUSTER_RETRIES → 兜底标 done + WARNING（让 grep 看到）
3. 已落地 atom 不会被重投 cluster agent（节省 LLM 重试代价）
"""
from __future__ import annotations

import logging
import time
from pathlib import Path

import pytest

from xskill.pipeline.atom import AtomTaskStore
from xskill.pipeline.registry import (
    register_dir, discover_trajectories, update_traj_status,
    get_trajs_by_status, get_status_counts, get_traj_retry_count,
)
from xskill.pipeline.runner import DirectoryWatcher, MAX_CLUSTER_RETRIES
from xskill.skill import candidates as C

from tests.test_atom_task_store import _FakeEmbed
from tests.test_task_agent import _TRAJ_MD, _AutoSplitLLM


# ─────────────────────────────────────────────────────────────────────
# stub agent factories
# ─────────────────────────────────────────────────────────────────────

class _OneAtomFailsStub:
    """模拟 partial-fail：第一个 cluster 调用抛异常，后续正常 add_task_to_skill。

    通过 class 级计数器全局共享调用次数。每个测试 method setup 时复位。
    """
    call_count = 0
    fail_first_n_calls = 1  # 前 N 次 cluster 调用抛异常
    target_skill = "auto-skill"

    def __init__(self, *, instructions, tools):
        self.instructions = instructions
        self.tools = {getattr(t, "__name__", ""): t for t in tools}

    def run(self, user_msg, **kw):
        head = (self.instructions[0] if self.instructions else "")[:60]
        if "TaskClusterAgent" in head:
            type(self).call_count += 1
            if type(self).call_count <= type(self).fail_first_n_calls:
                raise RuntimeError(f"stub LLM 402 (call {type(self).call_count})")
            import re
            m = re.search(r"atom_id:\s*(\S+)", user_msg)
            atom_id = m.group(1) if m else None
            if "new_skill_folder" in self.tools:
                self.tools["new_skill_folder"](
                    type(self).target_skill, "stub desc",
                )
            if "add_task_to_skill" in self.tools and atom_id:
                self.tools["add_task_to_skill"](
                    type(self).target_skill, atom_id, 5,
                )
        elif "SkillEditAgent" in head:
            # 不主动 graduate；这条不是本测试关心的链路
            pass

        class _R:
            pass
        r = _R()
        r.content = "stub"
        return r


class _CountingClusterStub:
    """每次 cluster 调用 +1 计数；总是成功 add_task_to_skill 给 auto-skill。"""
    cluster_calls: list[str] = []
    target_skill = "auto-skill"

    def __init__(self, *, instructions, tools):
        self.instructions = instructions
        self.tools = {getattr(t, "__name__", ""): t for t in tools}

    def run(self, user_msg, **kw):
        head = (self.instructions[0] if self.instructions else "")[:60]
        if "TaskClusterAgent" in head:
            import re
            m = re.search(r"atom_id:\s*(\S+)", user_msg)
            atom_id = m.group(1) if m else "?"
            type(self).cluster_calls.append(atom_id)
            if "new_skill_folder" in self.tools:
                self.tools["new_skill_folder"](
                    type(self).target_skill, "stub desc",
                )
            if "add_task_to_skill" in self.tools:
                self.tools["add_task_to_skill"](
                    type(self).target_skill, atom_id, 5,
                )

        class _R:
            pass
        r = _R()
        r.content = "stub"
        return r


# ─────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────

def _drive_until_settled(watcher: DirectoryWatcher, max_rounds: int = 20):
    """跑 watcher._scan_once + harvest 推动到没有 in-flight。"""
    for _ in range(max_rounds):
        watcher._scan_once()
        for _ in range(30):
            if not watcher._futures:
                break
            time.sleep(0.05)
            watcher._harvest()


def _build_watcher(tmp_path: Path, db: Path, factory):
    wd = tmp_path / "wd"
    wd.mkdir(exist_ok=True)
    (wd / "traj_x.md").write_text(_TRAJ_MD, encoding="utf-8")
    skill_dir = tmp_path / "skill"
    skill_dir.mkdir(exist_ok=True)
    register_dir(wd, db_path=db)
    store = AtomTaskStore(root=wd)
    return DirectoryWatcher(
        llm=_AutoSplitLLM(),
        embed_client=_FakeEmbed(),
        config={"llm": {"base_url": "x", "model": "y", "api_key": "z"}},
        skill_dir=skill_dir,
        poll_interval=0.0,
        max_concurrent=2,
        db_path=db,
        # max_retries=0 让 watcher 自身不把 error 标回 discovered；
        # 真正的"再试 cluster"靠测试显式把 traj 拨回 indexed。
        max_retries=0,
        cold_start_threshold=999,
        store=store,
        agno_agent_factory=factory,
        home_root=tmp_path,
    ), wd, skill_dir


# ─────────────────────────────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────────────────────────────

class TestPartialFailRetry:
    def setup_method(self):
        _OneAtomFailsStub.call_count = 0
        _OneAtomFailsStub.fail_first_n_calls = 1
        _CountingClusterStub.cluster_calls = []

    def test_partial_fail_marks_traj_error_with_retry_count_1(self, tmp_path):
        """_TRAJ_MD 拆 2 个 atom：第 1 个 cluster 抛异常、第 2 个成功
        → traj 应当标 error 且 retry_count=1，第 4 次才放过去标 done。
        """
        db = tmp_path / "test.db"
        watcher, wd, skill_dir = _build_watcher(
            tmp_path, db, lambda **kw: _OneAtomFailsStub(**kw),
        )

        _drive_until_settled(watcher)

        wd_id = register_dir(wd, db_path=db)
        # traj 必须留在 error（n_errors=1 > 0 且 retry_count 还没耗尽）
        assert "traj_x.md" in get_trajs_by_status(wd_id, "error", db_path=db), \
            "partial-fail 应标 error 等下轮重试"
        assert "traj_x.md" not in get_trajs_by_status(wd_id, "done", db_path=db), \
            "partial-fail 不应被假冒成 done"
        # retry_count 应被 +1
        rc = get_traj_retry_count(wd_id, "traj_x.md", db_path=db)
        assert rc == 1, f"retry_count 应为 1，实际 {rc}"

    def test_exhausted_retries_marks_done_with_warning(self, tmp_path, caplog):
        """连续 MAX_CLUSTER_RETRIES 次 partial-fail → 第 N+1 次兜底标 done + WARNING。"""
        db = tmp_path / "test.db"
        # 每次 cluster 调用都对第一个 atom 抛错（fail_first_n_calls=999 实质永远抛）
        _OneAtomFailsStub.fail_first_n_calls = 999

        watcher, wd, skill_dir = _build_watcher(
            tmp_path, db, lambda **kw: _OneAtomFailsStub(**kw),
        )

        # 推进到 traj 第一次进入 error（retry_count=1）
        _drive_until_settled(watcher)
        wd_id = register_dir(wd, db_path=db)
        assert get_traj_retry_count(wd_id, "traj_x.md", db_path=db) == 1

        # 手动模拟 daemon 重试：把 traj 拨回 indexed，再跑 cluster
        # （生产中 _scan_dir 走 max_retries 自动 retry；这里 max_retries=0
        # 所以手动拨）。注意：get_trajs_by_status(..., 'error') 默认会过滤
        # retry_count < max_retries 的行，传 max_retries=999 关闭这层过滤
        # 才能断言"traj 真留在 error 状态"。
        for expected_rc in (2, 3):
            update_traj_status(wd_id, "traj_x.md", "indexed", db_path=db)
            _drive_until_settled(watcher)
            err_list = get_trajs_by_status(
                wd_id, "error", db_path=db, max_retries=999,
            )
            assert "traj_x.md" in err_list, (
                f"retry {expected_rc} 应保持 error 状态; 实际: {err_list}"
            )
            assert get_traj_retry_count(wd_id, "traj_x.md", db_path=db) == expected_rc

        # 第 MAX_CLUSTER_RETRIES+1 次 → 兜底标 done + WARN
        caplog.set_level(logging.WARNING, logger="xskill.watcher")
        update_traj_status(wd_id, "traj_x.md", "indexed", db_path=db)
        _drive_until_settled(watcher)
        # traj 兜底落到 done
        assert "traj_x.md" in get_trajs_by_status(wd_id, "done", db_path=db), \
            "重试预算耗尽后 traj 应兜底 done"
        # WARN 日志 grep 得到
        assert any(
            "gave up after" in rec.getMessage()
            and "retries" in rec.getMessage()
            for rec in caplog.records
        ), "兜底 done 必须留下 WARN 日志"

    def test_full_success_no_retry(self, tmp_path):
        """完全成功（0 errors）→ 直接 done，retry_count 不增。"""
        db = tmp_path / "test.db"
        _OneAtomFailsStub.fail_first_n_calls = 0  # 不失败
        watcher, wd, skill_dir = _build_watcher(
            tmp_path, db, lambda **kw: _OneAtomFailsStub(**kw),
        )

        _drive_until_settled(watcher)

        wd_id = register_dir(wd, db_path=db)
        assert "traj_x.md" in get_trajs_by_status(wd_id, "done", db_path=db)
        assert get_traj_retry_count(wd_id, "traj_x.md", db_path=db) == 0


class TestClusterDedup:
    def setup_method(self):
        _CountingClusterStub.cluster_calls = []

    def test_already_clustered_atom_not_resent_to_llm(self, tmp_path):
        """已落地 atom 在 _do_cluster 再次执行时不会被投 LLM。

        预先用 cluster stub 跑一次让两个 atom 都落到 auto-skill；然后**手动
        把 traj 拨回 indexed** 模拟"重试"，再跑一轮——cluster_calls 应保持
        不变（去重生效）。
        """
        db = tmp_path / "test.db"
        watcher, wd, skill_dir = _build_watcher(
            tmp_path, db, lambda **kw: _CountingClusterStub(**kw),
        )

        # 第一轮：cluster 跑 2 次（_TRAJ_MD 拆 2 atom）
        _drive_until_settled(watcher)
        wd_id = register_dir(wd, db_path=db)
        # 检验确实进了 done + candidates 落了 2 个 atom
        assert "traj_x.md" in get_trajs_by_status(wd_id, "done", db_path=db)
        data = C.load_candidates(skill_dir / "auto-skill")
        assert len(data["candidates"]) == 2
        first_round_calls = len(_CountingClusterStub.cluster_calls)
        assert first_round_calls == 2

        # 手动模拟"重新走 cluster"：拨回 indexed → 再跑一轮
        update_traj_status(wd_id, "traj_x.md", "indexed", db_path=db)
        _drive_until_settled(watcher)

        # cluster_calls 不应增加（两个 atom 都被 find_atom_in_any_skill 跳过）
        second_round_calls = len(_CountingClusterStub.cluster_calls)
        assert second_round_calls == first_round_calls, (
            f"已落地 atom 不应再投 LLM；第一轮 {first_round_calls} 次, "
            f"第二轮总计 {second_round_calls} 次"
        )
        # candidates 仍是 2 个（没被重复 add）
        data2 = C.load_candidates(skill_dir / "auto-skill")
        assert len(data2["candidates"]) == 2


class TestPerAtomLog:
    def setup_method(self):
        _CountingClusterStub.cluster_calls = []

    def test_per_atom_info_lines_emitted(self, tmp_path, caplog):
        """正常 cluster 完成应 emit per-atom info 行（含 atom_id → skill_name @ ws=...）。"""
        db = tmp_path / "test.db"
        watcher, wd, skill_dir = _build_watcher(
            tmp_path, db, lambda **kw: _CountingClusterStub(**kw),
        )

        caplog.set_level(logging.INFO, logger="xskill.watcher")
        _drive_until_settled(watcher)

        # 总结行：3 atoms total + 0 dropped + 0 errors
        summary = [
            rec.getMessage() for rec in caplog.records
            if "clustered" in rec.getMessage() and "total" in rec.getMessage()
        ]
        assert summary, "应当 emit 总结行（clustered (N total, ...)）"
        # 应该有 in skills 段
        assert any("in skills" in m for m in summary)

        # per-atom info 行：包含 "→ auto-skill @ ws=5"
        atom_lines = [
            rec.getMessage() for rec in caplog.records
            if "→ auto-skill @ ws=" in rec.getMessage()
        ]
        # _TRAJ_MD 拆 2 atom
        assert len(atom_lines) == 2

    def test_silent_drop_emits_warning(self, tmp_path, caplog):
        """cluster agent 没调 add_task_to_skill（silent drop）必须发 WARNING。"""
        class _DropAllStub:
            """完全不调 add_task_to_skill——模拟 prompt 违规。"""
            def __init__(self, *, instructions, tools):
                self.instructions = instructions

            def run(self, msg, **kw):
                class _R:
                    pass
                r = _R()
                r.content = "I refuse"
                return r

        db = tmp_path / "test.db"
        watcher, wd, skill_dir = _build_watcher(
            tmp_path, db, lambda **kw: _DropAllStub(**kw),
        )

        caplog.set_level(logging.WARNING, logger="xskill.watcher")
        _drive_until_settled(watcher)

        # 必须有 DROPPED 的 WARN 行
        dropped_warns = [
            rec.getMessage() for rec in caplog.records
            if "DROPPED" in rec.getMessage()
        ]
        assert dropped_warns, (
            "silent drop 必须发 WARNING 让人 grep 得到；"
            f"实际 records: {[r.getMessage() for r in caplog.records]}"
        )
