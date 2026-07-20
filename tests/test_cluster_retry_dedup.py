"""cluster 批处理：瞬时失败自愈（重池化）+ 去重 + per-atom 日志单测

跨轨迹批处理模型下：
1. 瞬时失败（一批 LLM 异常）→ atom 留在未落地池，下一轮 scan 重新进池重试，
   最终全部落地、轨迹 done。无 per-traj 重试计数（重池化即重试，靠 cluster
   prompt"每个 atom 必落地"保证永久失败不发生）。轨迹不会被标 error。
2. 已落地 atom（在某 skill 的 .candidates.yml）不会被重投 cluster agent。
3. cluster batch 完成发 per-atom 审计日志；silent drop 发 WARNING。
"""
from __future__ import annotations

import logging
import re
import time
from pathlib import Path

from xskill.pipeline.atom import AtomTaskStore
from xskill.pipeline.registry import (
    register_dir, update_traj_status, get_trajs_by_status, get_status_counts,
)
from xskill.pipeline.runner import DirectoryWatcher
from xskill.skill import candidates as C

from tests.test_atom_task_store import _FakeEmbed
from tests.test_task_agent import _TRAJ_MD, _AutoSplitLLM, autosplit_submit


def _tool_name(tool) -> str:
    return getattr(tool, "__name__", None) or getattr(tool, "name", "")


def _call_tool(tool, *args):
    entrypoint = tool if callable(tool) else getattr(tool, "entrypoint")
    return entrypoint(*args)


# ─────────────────────────────────────────────────────────────────────
# stub agent factories（cluster 分支解析**所有** atom_id —— 批量）
# ─────────────────────────────────────────────────────────────────────

class _R:
    def __init__(self, content=""):
        self.content = content


class _TransientFailStub:
    """前 N 次 cluster 调用整批抛异常，之后批量写入全部 atom。"""
    calls = 0
    fail_first_n = 1
    target = "auto-skill"

    def __init__(self, *, instructions, tools):
        self.instructions = instructions
        self.tools = {_tool_name(t): t for t in tools}

    def run(self, user_msg, **kw):
        head = (self.instructions[0] if self.instructions else "")[:80]
        if "AtomTask 拆分员" in head:
            autosplit_submit(user_msg, self.tools)
            return _R("split")
        if "TaskClusterAgent" in head:
            type(self).calls += 1
            if type(self).calls <= type(self).fail_first_n:
                raise RuntimeError(f"stub LLM 402 (call {type(self).calls})")
            atom_ids = re.findall(r"atom_id:\s*(\S+)", user_msg)
            if "new_skill_folder" in self.tools:
                _call_tool(
                    self.tools["new_skill_folder"],
                    type(self).target,
                    "stub desc",
                )
            if "add_tasks_to_skill" in self.tools:
                _call_tool(
                    self.tools["add_tasks_to_skill"],
                    type(self).target,
                    [
                        {"atom_id": atom_id, "weightscore": 3}
                        for atom_id in atom_ids
                    ],
                )
            return _R("clustered")
        return _R("stub")


class _CountingClusterStub:
    """每次 cluster 调用记录 atom_id，并批量写入 auto-skill。"""
    sent: list[str] = []
    target = "auto-skill"

    def __init__(self, *, instructions, tools):
        self.instructions = instructions
        self.tools = {_tool_name(t): t for t in tools}

    def run(self, user_msg, **kw):
        head = (self.instructions[0] if self.instructions else "")[:80]
        if "AtomTask 拆分员" in head:
            autosplit_submit(user_msg, self.tools)
            return _R("split")
        if "TaskClusterAgent" in head:
            atom_ids = re.findall(r"atom_id:\s*(\S+)", user_msg)
            type(self).sent.extend(atom_ids)
            if "new_skill_folder" in self.tools:
                _call_tool(
                    self.tools["new_skill_folder"],
                    type(self).target,
                    "stub desc",
                )
            if "add_tasks_to_skill" in self.tools:
                _call_tool(
                    self.tools["add_tasks_to_skill"],
                    type(self).target,
                    [
                        {"atom_id": atom_id, "weightscore": 3}
                        for atom_id in atom_ids
                    ],
                )
            return _R("clustered")
        return _R("stub")


# ─────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────

def _drive_until_settled(watcher: DirectoryWatcher, max_rounds: int = 20):
    for _ in range(max_rounds):
        watcher._scan_once()
        for _ in range(30):
            if not watcher._futures:
                break
            time.sleep(0.05)
            watcher._harvest()


def _build_watcher(tmp_path: Path, db: Path, factory, *, batch_size=8):
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
        max_retries=0,
        store=store,
        agno_agent_factory=factory,
        home_root=tmp_path,
        cluster_batch_size=batch_size,
    ), wd, skill_dir


# ─────────────────────────────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────────────────────────────

class TestTransientFailSelfHeals:
    def setup_method(self):
        _TransientFailStub.calls = 0
        _TransientFailStub.fail_first_n = 1

    def test_failed_batch_repools_and_eventually_done(self, tmp_path):
        """第一批 cluster 抛异常 → atom 未落地 → 轨迹留在 indexed（非 error）→
        下一轮重新进池 → 第二批成功 → 全部落地 → done。"""
        db = tmp_path / "test.db"
        watcher, wd, skill_dir = _build_watcher(
            tmp_path, db, lambda **kw: _TransientFailStub(**kw),
        )
        _drive_until_settled(watcher)

        wd_id = register_dir(wd, db_path=db)
        # 最终落到 done（瞬时失败被重池化吸收）
        assert "traj_x.md" in get_trajs_by_status(wd_id, "done", db_path=db)
        # 不会被标 error（新模型无 per-traj cluster 错误态）
        assert "traj_x.md" not in get_trajs_by_status(
            wd_id, "error", db_path=db, max_retries=999)
        # 两个 atom 都落进 auto-skill
        data = C.load_candidates(skill_dir / "auto-skill")
        assert len(data["candidates"]) == 2
        # cluster 至少被调用 2 次（首次失败 + 后续成功）
        assert _TransientFailStub.calls >= 2

    def test_full_success_single_batch(self, tmp_path):
        """不失败：一批搞定（batch_size≥2），轨迹直接 done。"""
        db = tmp_path / "test.db"
        _TransientFailStub.fail_first_n = 0
        watcher, wd, skill_dir = _build_watcher(
            tmp_path, db, lambda **kw: _TransientFailStub(**kw),
        )
        _drive_until_settled(watcher)

        wd_id = register_dir(wd, db_path=db)
        assert "traj_x.md" in get_trajs_by_status(wd_id, "done", db_path=db)
        assert _TransientFailStub.calls == 1  # _TRAJ_MD 2 atoms 一批消费


class TestClusterDedup:
    def setup_method(self):
        _CountingClusterStub.sent = []

    def test_already_clustered_atom_not_resent_to_llm(self, tmp_path):
        """已落地 atom 在轨迹被拨回 indexed 后不会被重投 cluster agent。"""
        db = tmp_path / "test.db"
        watcher, wd, skill_dir = _build_watcher(
            tmp_path, db, lambda **kw: _CountingClusterStub(**kw),
        )

        # 第一轮：2 个 atom 一批落地 → done
        _drive_until_settled(watcher)
        wd_id = register_dir(wd, db_path=db)
        assert "traj_x.md" in get_trajs_by_status(wd_id, "done", db_path=db)
        data = C.load_candidates(skill_dir / "auto-skill")
        assert len(data["candidates"]) == 2
        first_round = len(_CountingClusterStub.sent)
        assert first_round == 2

        # 手动拨回 indexed 模拟"重新进入 cluster 候选" → 再跑
        update_traj_status(wd_id, "traj_x.md", "indexed", db_path=db)
        _drive_until_settled(watcher)

        # 已落地 atom 被 _collect_cluster_batch 过滤 → 不再送 LLM
        assert len(_CountingClusterStub.sent) == first_round, (
            f"已落地 atom 不应再投 LLM；第一轮 {first_round}，"
            f"第二轮总计 {len(_CountingClusterStub.sent)}"
        )
        # candidates 仍是 2 个（没被重复 add）+ 轨迹仍 done
        assert len(C.load_candidates(skill_dir / "auto-skill")["candidates"]) == 2
        assert "traj_x.md" in get_trajs_by_status(wd_id, "done", db_path=db)


class TestPerAtomLog:
    def setup_method(self):
        _CountingClusterStub.sent = []

    def test_per_atom_info_lines_emitted(self, tmp_path, caplog):
        """cluster batch 完成应发总结行 + per-atom info 行。"""
        db = tmp_path / "test.db"
        watcher, wd, skill_dir = _build_watcher(
            tmp_path, db, lambda **kw: _CountingClusterStub(**kw),
        )
        caplog.set_level(logging.INFO, logger="xskill.watcher")
        _drive_until_settled(watcher)

        summary = [
            r.getMessage() for r in caplog.records
            if "cluster batch" in r.getMessage() and "in skills" in r.getMessage()
        ]
        assert summary, "应发批次总结行（cluster batch → ... in skills）"
        atom_lines = [
            r.getMessage() for r in caplog.records
            if "→ auto-skill @ ws=" in r.getMessage()
        ]
        assert len(atom_lines) == 2  # _TRAJ_MD 2 atoms

    def test_silent_drop_emits_warning(self, tmp_path, caplog):
        """cluster agent 不调候选添加工具（silent drop）→ WARNING。"""
        class _DropAllStub:
            def __init__(self, *, instructions, tools):
                self.instructions = instructions
                self.tools = {_tool_name(t): t for t in tools}

            def run(self, msg, **kw):
                head = (self.instructions[0] if self.instructions else "")[:80]
                if "AtomTask 拆分员" in head:
                    autosplit_submit(msg, self.tools)
                return _R("I refuse")

        db = tmp_path / "test.db"
        watcher, wd, skill_dir = _build_watcher(
            tmp_path, db, lambda **kw: _DropAllStub(**kw),
        )
        caplog.set_level(logging.WARNING, logger="xskill.watcher")
        _drive_until_settled(watcher)

        dropped = [r.getMessage() for r in caplog.records
                   if "DROPPED" in r.getMessage()]
        assert dropped, (
            "silent drop 必须发 WARNING；"
            f"records: {[r.getMessage() for r in caplog.records]}"
        )
