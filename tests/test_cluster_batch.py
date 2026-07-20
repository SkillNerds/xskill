"""跨轨迹批量聚类（cluster batch）验收单测
================================================

ClusterAgent 由"每次消费 1 个 atom"改为"每次消费 cluster_batch_size 个 atom 的
位置（非内容）"。本文件三组验收对应需求三条：

1. **批量生效**：构造 N 条 indexed 轨迹（N > batch_size），观察 ClusterAgent
   调用次数 == ceil(总未归类 atom 数 / batch_size)，而**非** == 总 atom 数。
2. **已落地过滤**：构造已在某 skill ``.candidates.yml`` 的 atom，确认被
   ``_collect_cluster_batch`` 过滤、永不进入任何 batch、不送 LLM。
3. **断点续传**：消费中途"kill 进程"（丢弃 watcher + 线程池），重启（新 watcher
   同一 wd/db/skill_dir/store）后从断点继续，已落地 atom 不重复消费。

文件系统即队列：atom json = 待消费池，.candidates.yml = 已消费标记——所以去重与
断点续传都不需要额外 DB 表，靠 ``find_atom_entry_in_any_skill`` 即可。
"""
from __future__ import annotations

import math
import re
import time
from collections import Counter
from pathlib import Path

from xskill.pipeline.atom import AtomTask, AtomTaskStore
from xskill.pipeline.registry import (
    register_dir, discover_trajectories, update_traj_status,
    get_trajs_by_status,
)
from xskill.pipeline.runner import DirectoryWatcher
from xskill.skill import candidates as C

from tests.test_atom_task_store import _FakeEmbed
from tests.test_task_agent import _TRAJ_MD, _AutoSplitLLM


def _tool_name(tool) -> str:
    return getattr(tool, "__name__", None) or getattr(tool, "name", "")


def _call_tool(tool, *args):
    entrypoint = tool if callable(tool) else getattr(tool, "entrypoint")
    return entrypoint(*args)


# ─────────────────────────────────────────────────────────────────────
# stub agno factory：统计 ClusterAgent 调用次数 + 记录每批送进的 atom
# ─────────────────────────────────────────────────────────────────────

class _BatchCountingStub:
    """cluster 分支：每次 agent.run = 一次 ClusterAgent 调用 → cluster_calls += 1；
    解析 user_msg 里**所有** atom_id，一次 add_tasks_to_skill 写入 auto-skill。

    类级计数器跨实例/跨 watcher 共享（断点续传测试要统计两个 watcher 的总量）。
    每个测试 setup_method 复位。weightscore 取 3（< 晋升阈值 10）让 candidates
    不被 SkillEdit 清空——保证"已落地"标记稳定存在，dedup / done / resume 可判。
    """
    cluster_calls = 0
    sent_atoms: list[str] = []   # 所有进过 cluster batch 的 atom_id（含重复）
    target_skill = "auto-skill"

    def __init__(self, *, instructions, tools):
        self.instructions = instructions
        self.tools = {_tool_name(t): t for t in tools}

    def run(self, user_msg, **kw):
        head = (self.instructions[0] if self.instructions else "")[:80]
        if "AtomTask 拆分员" in head:
            from tests.test_task_agent import autosplit_submit
            autosplit_submit(user_msg, self.tools)
            return _R("split")
        if "TaskClusterAgent" in head:
            type(self).cluster_calls += 1
            atom_ids = re.findall(r"atom_id:\s*(\S+)", user_msg)
            if "new_skill_folder" in self.tools:
                _call_tool(self.tools["new_skill_folder"], type(self).target_skill, "stub desc")
            for aid in atom_ids:
                type(self).sent_atoms.append(aid)
            if "add_tasks_to_skill" in self.tools:
                _call_tool(
                    self.tools["add_tasks_to_skill"],
                    type(self).target_skill,
                    [
                        {"atom_id": atom_id, "weightscore": 3}
                        for atom_id in atom_ids
                    ],
                )
            return _R("clustered")
        # SkillEditAgent / 其它：不动手（不写 SKILL.md、不 commit → candidates 不清空）
        return _R("stub")


class _R:
    def __init__(self, content=""):
        self.content = content


# ─────────────────────────────────────────────────────────────────────
# helpers
# ─────────────────────────────────────────────────────────────────────

def _mk_atom(traj_id: str, i: int) -> AtomTask:
    return AtomTask(
        atom_id=f"atom_{traj_id}_{i:04d}", traj_id=traj_id,
        offset_start=1 + i * 10, offset_end=10 + i * 10,
        intent=f"intent {i}", summary=f"summary {i}",
        tags=["t"], used_skills=[], ux_score=7,
    )


def _seed_indexed_trajs(wd: Path, store: AtomTaskStore, db: Path,
                        n_trajs: int, atoms_per_traj: int) -> int:
    """造 n_trajs 条轨迹，每条 atoms_per_traj 个 atom，直接置 indexed（跳过
    split/embed，让全量池一开始就齐——批大小才确定，ceil 公式才严格成立）。
    返回 wd_id。"""
    for n in range(n_trajs):
        fname = f"traj_{n}.md"
        (wd / fname).write_text(_TRAJ_MD, encoding="utf-8")
        tid = f"traj_{n}"
        for i in range(atoms_per_traj):
            store.save(_mk_atom(tid, i))
    wd_id = register_dir(wd, db_path=db)
    discover_trajectories(wd_id, wd, db_path=db)
    for n in range(n_trajs):
        update_traj_status(wd_id, f"traj_{n}.md", "indexed", db_path=db)
    return wd_id


def _build_watcher(wd: Path, skill_dir: Path, db: Path, store: AtomTaskStore,
                   batch_size: int) -> DirectoryWatcher:
    return DirectoryWatcher(
        llm=_AutoSplitLLM(),
        embed_client=_FakeEmbed(),
        config={"llm": {"base_url": "x", "model": "y", "api_key": "z"}},
        skill_dir=skill_dir,
        poll_interval=0.0,
        max_concurrent=4,
        db_path=db,
        store=store,
        agno_agent_factory=_BatchCountingStub,
        home_root=wd.parent,
        cluster_batch_size=batch_size,
    )


def _drive(watcher: DirectoryWatcher, wd_id: int, db: Path, max_rounds: int = 30):
    """跑 scan + harvest 直到没有 indexed 轨迹剩（全 done）或轮次耗尽。"""
    for _ in range(max_rounds):
        watcher._scan_once()
        for _ in range(40):
            if not watcher._futures:
                break
            time.sleep(0.02)
            watcher._harvest()
        if not get_trajs_by_status(wd_id, "indexed", db_path=db):
            return


# ─────────────────────────────────────────────────────────────────────
# 验收 1：批量生效 —— 调用次数 == ceil(总 atom / batch_size) 而非 == 总 atom
# ─────────────────────────────────────────────────────────────────────

class TestBatchCallCount:
    def setup_method(self):
        _BatchCountingStub.cluster_calls = 0
        _BatchCountingStub.sent_atoms = []

    def test_calls_equal_ceil_total_over_batch(
        self, tmp_path, monkeypatch,
    ):
        from unittest.mock import Mock

        from xskill.pipeline import registry as registry_module

        db = tmp_path / "test.db"
        global_db = tmp_path / "global" / "registry.db"
        monkeypatch.setattr(
            registry_module,
            "get_registry_db_path",
            Mock(return_value=global_db),
        )
        wd = tmp_path / "wd"; wd.mkdir()
        skill_dir = tmp_path / "skill"; skill_dir.mkdir()
        store = AtomTaskStore(root=wd)

        n_trajs, k = 5, 2          # N=5 > batch_size=4；总 atom=10
        batch_size = 4
        total = n_trajs * k
        wd_id = _seed_indexed_trajs(wd, store, db, n_trajs, k)

        watcher = _build_watcher(wd, skill_dir, db, store, batch_size)
        _drive(watcher, wd_id, db)

        expected = math.ceil(total / batch_size)   # ceil(10/4) = 3
        assert _BatchCountingStub.cluster_calls == expected, (
            f"ClusterAgent 调用次数应为 ceil({total}/{batch_size})={expected}，"
            f"实际 {_BatchCountingStub.cluster_calls}"
        )
        # 关键对比：远小于"每 atom 一次"的旧行为
        assert _BatchCountingStub.cluster_calls < total, (
            "批量后调用次数必须明显少于总 atom 数"
        )
        # 所有 atom 都被消费一次（无遗漏、无重复）
        sent = Counter(_BatchCountingStub.sent_atoms)
        assert len(sent) == total and all(v == 1 for v in sent.values())
        # 全部轨迹 done
        assert len(get_trajs_by_status(wd_id, "done", db_path=db)) == n_trajs
        with registry_module.pooled_connection(db) as connection:
            adoption_count = connection.execute(
                "SELECT COUNT(*) FROM atom_adoption"
            ).fetchone()[0]
        assert adoption_count == total
        assert not global_db.exists()

    def test_pool_smaller_than_batch_takes_all_in_one_call(self, tmp_path):
        """待消费 < batch_size 时全取，一次调用清空。"""
        db = tmp_path / "test.db"
        wd = tmp_path / "wd"; wd.mkdir()
        skill_dir = tmp_path / "skill"; skill_dir.mkdir()
        store = AtomTaskStore(root=wd)
        wd_id = _seed_indexed_trajs(wd, store, db, n_trajs=2, atoms_per_traj=1)

        watcher = _build_watcher(wd, skill_dir, db, store, batch_size=8)
        _drive(watcher, wd_id, db)

        assert _BatchCountingStub.cluster_calls == 1  # 2 atoms < 8 → 一批
        assert len(get_trajs_by_status(wd_id, "done", db_path=db)) == 2


# ─────────────────────────────────────────────────────────────────────
# 验收 2：已落地 atom 被过滤，永不送 LLM
# ─────────────────────────────────────────────────────────────────────

class TestAlreadyLandedFiltered:
    def setup_method(self):
        _BatchCountingStub.cluster_calls = 0
        _BatchCountingStub.sent_atoms = []

    def test_preseeded_candidate_never_sent_to_llm(self, tmp_path):
        db = tmp_path / "test.db"
        wd = tmp_path / "wd"; wd.mkdir()
        skill_dir = tmp_path / "skill"; skill_dir.mkdir()
        store = AtomTaskStore(root=wd)
        wd_id = _seed_indexed_trajs(wd, store, db, n_trajs=3, atoms_per_traj=2)

        # 预先把 traj_0 的第一个 atom 塞进 pre-skill 的 .candidates.yml（ws<10
        # 不触发 SkillEdit），模拟"上一轮已落地"。
        landed_id = "atom_traj_0_0000"
        pre = skill_dir / "pre-skill"; pre.mkdir()
        data = {"candidates": []}
        data, _ = C.add_atom_contribution(data, landed_id, 3)
        C.save_candidates(pre, data)

        watcher = _build_watcher(wd, skill_dir, db, store, batch_size=4)
        _drive(watcher, wd_id, db)

        # 预落地 atom 从未进入任何 cluster batch
        assert landed_id not in _BatchCountingStub.sent_atoms, (
            "已在 .candidates.yml 的 atom 必须被过滤、不送 LLM"
        )
        # 其余 5 个 atom 各消费一次
        sent = Counter(_BatchCountingStub.sent_atoms)
        assert len(sent) == 5 and all(v == 1 for v in sent.values())
        # 全部轨迹仍能 done（traj_0 的已落地 atom 计入"已消费"）
        assert len(get_trajs_by_status(wd_id, "done", db_path=db)) == 3


# ─────────────────────────────────────────────────────────────────────
# 验收 3：断点续传 —— kill 后重启从断点继续，已落地 atom 不重复消费
# ─────────────────────────────────────────────────────────────────────

class TestResumeAfterKill:
    def setup_method(self):
        _BatchCountingStub.cluster_calls = 0
        _BatchCountingStub.sent_atoms = []

    def test_resume_does_not_reconsume_landed(self, tmp_path):
        db = tmp_path / "test.db"
        wd = tmp_path / "wd"; wd.mkdir()
        skill_dir = tmp_path / "skill"; skill_dir.mkdir()
        store = AtomTaskStore(root=wd)
        n_trajs, k = 5, 2
        total = n_trajs * k
        wd_id = _seed_indexed_trajs(wd, store, db, n_trajs, k)

        # ── 进程 1：只跑到落地一批（4 个）就"被 kill" ──
        w1 = _build_watcher(wd, skill_dir, db, store, batch_size=4)
        for _ in range(30):
            w1._scan_once()
            for _ in range(40):
                if not w1._futures:
                    break
                time.sleep(0.02)
                w1._harvest()
            if _BatchCountingStub.cluster_calls >= 1 and not w1._futures:
                break
        landed_after_kill = list(_BatchCountingStub.sent_atoms)
        assert len(landed_after_kill) == 4, "进程 1 应恰好落地一批 4 个"
        # 模拟 kill：丢弃线程池，不再用 w1
        w1._pool.shutdown(wait=False)

        # ── 进程 2：新 watcher 同一 wd/db/skill_dir/store，从断点继续 ──
        w2 = _build_watcher(wd, skill_dir, db, store, batch_size=4)
        _drive(w2, wd_id, db)

        # 每个 atom 全程只被消费一次（已落地的 4 个没被重投）
        sent = Counter(_BatchCountingStub.sent_atoms)
        assert len(sent) == total, f"应消费全部 {total} 个 atom，实际 {len(sent)}"
        assert all(v == 1 for v in sent.values()), (
            f"已落地 atom 被重复消费：{[a for a, v in sent.items() if v > 1]}"
        )
        # 断点续传：进程 2 只处理了剩余的 6 个
        resumed = [a for a in _BatchCountingStub.sent_atoms
                   if a not in landed_after_kill]
        assert len(resumed) == total - 4
        # 全部 done
        assert len(get_trajs_by_status(wd_id, "done", db_path=db)) == n_trajs
