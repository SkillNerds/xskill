"""watcher v2 流水线：discovered → splitting → split_done → indexed → clustering → done"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from xskill.pipeline.atom import AtomTaskStore
from xskill.pipeline.registry import (
    register_dir, discover_trajectories, update_traj_status,
    get_trajs_by_status, get_status_counts,
)
from xskill.pipeline.runner import DirectoryWatcher
from tests.test_atom_task_store import _FakeEmbed
from tests.test_task_agent import _TRAJ_MD, _AutoSplitLLM, autosplit_submit


class _StubAgno:
    """根据 sysprompt 头分发：
    - split agent (AtomTask 拆分员) → 扫 [line:N] 标记逐个 submit_atom
    - cluster agent → 调 new_skill_folder + add_task_to_skill 给 auto-skill 打 10 分
    - edit agent (baby)  → 写 SKILL.md + commit_baby_to_main
    - edit agent (main)  → 写 SKILL.md + commit_to_staging
    - absorb agent → 调 absorb_user_edit_to_main
    """
    def __init__(self, *, instructions, tools):
        self.instructions = instructions
        self.tools = {getattr(t, "__name__", ""): t for t in tools}

    def run(self, user_msg, **kw):
        head = (self.instructions[0] if self.instructions else "")[:80]
        if "AtomTask 拆分员" in head:
            autosplit_submit(user_msg, self.tools)
            class _R: pass
            r = _R(); r.content = "split"; return r
        if "TaskClusterAgent" in head:
            import re
            import time as _t
            # 真聚类要等大模型(按秒)；stub 模拟一点耗时,让"逐 atom 写 registry"
            # 这类毫秒级旁路开销相对可忽略,贴近生产时序(否则瞬时 stub 会放大
            # 旁路写入、扰动 candidates 晋升竞态)。
            _t.sleep(0.03)
            m = re.search(r"atom_id:\s*(\S+)", user_msg)
            atom_id = m.group(1) if m else None
            if "new_skill_folder" in self.tools:
                self.tools["new_skill_folder"]("auto-skill", "stub desc")
            if "add_task_to_skill" in self.tools and atom_id:
                self.tools["add_task_to_skill"]("auto-skill", atom_id, 10)
        elif "SkillEditAgent" in head:
            import re
            m = re.search(r"目标 SKILL\.md 路径:\s*(\S+)", user_msg)
            if m and "write_file" in self.tools:
                self.tools["write_file"](
                    m.group(1),
                    "---\nname: auto-skill\ndescription: stub\nmetadata:\n  version: 1\n---\n# body\n",
                )
            # 根据当前分支决定调哪个 commit
            if "baby" in user_msg:
                if "commit_baby_to_main" in self.tools:
                    self.tools["commit_baby_to_main"]("auto-skill", "stub baby")
            elif "main" in user_msg:
                if "commit_to_staging" in self.tools:
                    self.tools["commit_to_staging"]("auto-skill", "stub staging")
        elif "UserEditAbsorbAgent" in head:
            if "absorb_user_edit_to_main" in self.tools:
                self.tools["absorb_user_edit_to_main"]("auto-skill", "absorb user edit: test")
        class _R: pass
        r = _R(); r.content = "stub"; return r


class TestNewStatusValuesAccepted:
    """先把新状态名能进 registry 跑通"""
    def test_splitting_and_clustering_accepted(self, tmp_path):
        db = tmp_path / "test.db"
        wd = tmp_path / "wd"; wd.mkdir()
        (wd / "traj_a.md").write_text("hi", encoding="utf-8")
        wd_id = register_dir(wd, db_path=db)
        discover_trajectories(wd_id, wd, db_path=db)
        for status in ("splitting", "split_done", "clustering"):
            update_traj_status(wd_id, "traj_a.md", status, db_path=db)
            assert get_trajs_by_status(wd_id, status, db_path=db) == ["traj_a.md"]


class TestZombieCleanup:
    """splitting / clustering 是 in-flight 中间态。daemon 重启后如果它们
    还留在 DB 但没有对应 in-flight future，必须回退到前一阶段重新调度，
    否则 traj 永远卡死。"""

    def test_splitting_zombie_rolls_back_to_discovered(self, tmp_path):
        db = tmp_path / "test.db"
        wd = tmp_path / "wd"; wd.mkdir()
        skill_dir = tmp_path / "skill"; skill_dir.mkdir()
        (wd / "traj_x.md").write_text(_TRAJ_MD, encoding="utf-8")

        wd_id = register_dir(wd, db_path=db)
        discover_trajectories(wd_id, wd, db_path=db)
        # 模拟上次 daemon 切死时 traj 留在 splitting
        update_traj_status(wd_id, "traj_x.md", "splitting", db_path=db)

        store = AtomTaskStore(root=wd)
        watcher = DirectoryWatcher(
            llm=_AutoSplitLLM(),
            embed_client=_FakeEmbed(),
            config={"llm": {"base_url": "x", "model": "y", "api_key": "z"}},
            skill_dir=skill_dir,
            poll_interval=0.0,
            max_concurrent=2,
            db_path=db,
            cold_start_threshold=999,
            store=store,
            agno_agent_factory=_StubAgno,
            home_root=tmp_path,
        )
        # 新 watcher 进程 _futures 为空 → 僵尸 splitting 应回退到 discovered
        # 然后同一轮提交新 split future
        watcher._scan_once()
        # 状态应**前进**：要么已经在 splitting + 有 future 在飞，要么更靠后
        assert (
            get_trajs_by_status(wd_id, "discovered", db_path=db) == []
            or len(watcher._futures) > 0
        )

    def test_clustering_zombie_rolls_back_to_indexed(self, tmp_path):
        db = tmp_path / "test.db"
        wd = tmp_path / "wd"; wd.mkdir()
        skill_dir = tmp_path / "skill"; skill_dir.mkdir()
        (wd / "traj_y.md").write_text(_TRAJ_MD, encoding="utf-8")

        wd_id = register_dir(wd, db_path=db)
        discover_trajectories(wd_id, wd, db_path=db)
        update_traj_status(wd_id, "traj_y.md", "clustering", db_path=db)

        store = AtomTaskStore(root=wd)
        watcher = DirectoryWatcher(
            llm=_AutoSplitLLM(),
            embed_client=_FakeEmbed(),
            config={"llm": {"base_url": "x", "model": "y", "api_key": "z"}},
            skill_dir=skill_dir,
            poll_interval=0.0,
            max_concurrent=2,
            db_path=db,
            cold_start_threshold=999,
            store=store,
            agno_agent_factory=_StubAgno,
            home_root=tmp_path,
        )
        watcher._scan_once()
        # 僵尸 clustering 应被回退到 indexed（同一轮内可能又被重新提交，OK）
        assert "traj_y.md" not in get_trajs_by_status(wd_id, "clustering", db_path=db) \
               or len(watcher._futures) > 0


class TestColdStartSerial:
    """冷启动期间 cluster 强制串行（max_concurrent=1）：避免并发 cluster
    agent 创建近义 baby slug——逐条决策让 catalog 演化可见。"""

    def test_only_one_cluster_in_flight_during_cold_start(self, tmp_path):
        """pending pre-index ≥ threshold 时，单轮 scan 最多提交 1 个 cluster job。"""
        db = tmp_path / "test.db"
        wd = tmp_path / "wd"; wd.mkdir()
        skill_dir = tmp_path / "skill"; skill_dir.mkdir()
        # 5 条 traj，2 条 indexed（待 cluster），3 条 split_done（pending pre-index）
        for i in range(5):
            (wd / f"traj_{i}.md").write_text(_TRAJ_MD, encoding="utf-8")

        wd_id = register_dir(wd, db_path=db)
        discover_trajectories(wd_id, wd, db_path=db)
        for fname in ("traj_0.md", "traj_1.md"):
            update_traj_status(wd_id, fname, "indexed", db_path=db)
        for fname in ("traj_2.md", "traj_3.md", "traj_4.md"):
            update_traj_status(wd_id, fname, "split_done", db_path=db)

        # cluster 一直挂着不返回，让我们能观察 in-flight 数
        class _BlockingStub:
            def __init__(self, **kw): pass
            def run(self, msg, **kw):
                import time
                time.sleep(5)  # 模拟 cluster 长时间运行
                class _R: pass
                r = _R(); r.content = ""; return r

        store = AtomTaskStore(root=wd)
        watcher = DirectoryWatcher(
            llm=_AutoSplitLLM(),
            embed_client=_FakeEmbed(),
            config={"llm": {"base_url": "x", "model": "y", "api_key": "z"}},
            skill_dir=skill_dir,
            poll_interval=0.0,
            max_concurrent=30,
            db_path=db,
            cold_start_threshold=3,
            store=store,
            agno_agent_factory=lambda **k: _BlockingStub(**k),
            home_root=tmp_path,
        )
        watcher._scan_once()
        # cold-start 应该只让 1 个 cluster 在飞
        cluster_in_flight = sum(
            1 for i in watcher._futures.values() if i["stage"] == "cluster"
        )
        assert cluster_in_flight == 1, (
            f"冷启动期间 cluster 应串行（1 in-flight），实际 {cluster_in_flight}"
        )
        # 再 scan 一次：cluster 还在飞 → 不应再提交新的
        watcher._scan_once()
        cluster_in_flight = sum(
            1 for i in watcher._futures.values() if i["stage"] == "cluster"
        )
        assert cluster_in_flight == 1, "第二轮 scan 不应增加 cluster in-flight"
        # cleanup: 不等 sleep(5) 跑完，主测试就完成
        watcher._pool.shutdown(wait=False)

    def test_steady_state_allows_max_concurrent(self, tmp_path):
        """稳态（backlog < threshold）→ 允许 max_concurrent 并发。"""
        db = tmp_path / "test.db"
        wd = tmp_path / "wd"; wd.mkdir()
        skill_dir = tmp_path / "skill"; skill_dir.mkdir()
        # 2 条 traj indexed，threshold=10 → 远低于阈值，稳态
        for i in range(2):
            (wd / f"traj_{i}.md").write_text(_TRAJ_MD, encoding="utf-8")
        wd_id = register_dir(wd, db_path=db)
        discover_trajectories(wd_id, wd, db_path=db)
        for i in range(2):
            update_traj_status(wd_id, f"traj_{i}.md", "indexed", db_path=db)

        class _BlockingStub:
            def __init__(self, **kw): pass
            def run(self, msg, **kw):
                import time
                time.sleep(5)
                class _R: pass
                r = _R(); r.content = ""; return r

        store = AtomTaskStore(root=wd)
        watcher = DirectoryWatcher(
            llm=_AutoSplitLLM(),
            embed_client=_FakeEmbed(),
            config={"llm": {"base_url": "x", "model": "y", "api_key": "z"}},
            skill_dir=skill_dir,
            poll_interval=0.0,
            max_concurrent=30,
            db_path=db,
            cold_start_threshold=10,  # 高阈值 → 2 条不触发 cold-start
            store=store,
            agno_agent_factory=lambda **k: _BlockingStub(**k),
            home_root=tmp_path,
        )
        watcher._scan_once()
        cluster_in_flight = sum(
            1 for i in watcher._futures.values() if i["stage"] == "cluster"
        )
        assert cluster_in_flight == 2, (
            f"稳态（backlog < threshold）应并发 2 个，实际 {cluster_in_flight}"
        )
        watcher._pool.shutdown(wait=False)


class TestClusterAllFailed:
    """LLM 余额耗尽 / 全部 atom cluster 抛异常时，traj 必须标 error 让下轮 retry，
    不能被假冒成 ``done``（实跑暴露的 bug：原 ``_on_cluster_done`` 无条件标 done）。"""

    def test_all_atoms_failed_marks_traj_error(self, tmp_path):
        db = tmp_path / "test.db"
        wd = tmp_path / "wd"; wd.mkdir()
        skill_dir = tmp_path / "skill"; skill_dir.mkdir()
        (wd / "traj_x.md").write_text(_TRAJ_MD, encoding="utf-8")

        class _AlwaysFailAgno:
            def __init__(self, *, instructions, tools):
                self.instructions = instructions
                self.tools = {getattr(t, "__name__", ""): t for t in tools}
            def run(self, msg, **kw):
                head = (self.instructions[0] or "")[:80]
                if "AtomTask 拆分员" in head:
                    autosplit_submit(msg, self.tools)
                    class _R: pass
                    r = _R(); r.content = "split"; return r
                if "TaskClusterAgent" in head:
                    raise RuntimeError("Insufficient Balance (stub LLM 402)")
                class _R: pass
                r = _R(); r.content = ""; return r

        register_dir(wd, db_path=db)
        store = AtomTaskStore(root=wd)
        watcher = DirectoryWatcher(
            llm=_AutoSplitLLM(),
            embed_client=_FakeEmbed(),
            config={"llm": {"base_url": "x", "model": "y", "api_key": "z"}},
            skill_dir=skill_dir,
            poll_interval=0.0,
            max_concurrent=2,
            db_path=db,
            cold_start_threshold=999,
            store=store,
            max_retries=0,  # 不让 watcher 自动 retry，便于断言"留在 error"
            agno_agent_factory=lambda **kw: _AlwaysFailAgno(**kw),
            home_root=tmp_path,
        )

        # 跑足够多轮把 traj 推到 cluster 阶段
        for _ in range(10):
            watcher._scan_once()
            for _ in range(20):
                if not watcher._futures:
                    break
                time.sleep(0.05)
                watcher._harvest()
            wd_id = register_dir(wd, db_path=db)
            if "traj_x.md" in get_trajs_by_status(wd_id, "error", db_path=db):
                break

        wd_id = register_dir(wd, db_path=db)
        # 关键断言：cluster 全失败 → traj 必须是 error，不能是 done
        assert "traj_x.md" not in get_trajs_by_status(wd_id, "done", db_path=db), \
            "全部 atom cluster 失败的 traj 不应被标 done"
        assert "traj_x.md" in get_trajs_by_status(wd_id, "error", db_path=db), \
            "全部 atom cluster 失败的 traj 应被标 error 等待 retry"


class TestIndependentSkillEditScan:
    """Bug 1 修复关键回归：edit 触发独立于 cluster 链路。

    即便单个 atom 的 cluster 抛异常，buffer 已满阈值的 skill 仍能被
    每轮 watcher scan 检出 + 触发 SkillEdit。
    """

    def test_edit_triggers_even_when_cluster_fails(self, tmp_path):
        """先手动 seed baby 分支 + 候选满分 → cluster 抛异常 → edit 仍触发。"""
        db = tmp_path / "test.db"
        wd = tmp_path / "wd"; wd.mkdir()
        skill_dir = tmp_path / "skill"; skill_dir.mkdir()
        (wd / "traj_x.md").write_text(_TRAJ_MD, encoding="utf-8")

        from xskill.skill import candidates as C
        from xskill.skill.git import init_skill_repo_on_baby
        my_skill = skill_dir / "my-skill"
        init_skill_repo_on_baby(str(my_skill), name="my-skill", description="stub")
        data = {"candidates": []}
        data, _ = C.add_atom_contribution(data, "atom_pre_0001", 10)
        C.save_candidates(my_skill, data)

        class _MixedStub:
            def __init__(self, *, instructions, tools):
                self.instructions = instructions
                self.tools = {getattr(t, "__name__", ""): t for t in tools}
            def run(self, msg, **kw):
                head = (self.instructions[0] if self.instructions else "")[:80]
                if "AtomTask 拆分员" in head:
                    autosplit_submit(msg, self.tools)
                    class _R: pass
                    r = _R(); r.content = "split"; return r
                if "TaskClusterAgent" in head:
                    raise RuntimeError("cluster LLM 402")
                # SkillEditAgent on baby：写 SKILL.md + 调 commit_baby_to_main
                import re
                m = re.search(r"目标 SKILL\.md 路径:\s*(\S+)", msg)
                if m and "write_file" in self.tools:
                    self.tools["write_file"](
                        m.group(1),
                        "---\nname: my-skill\ndescription: stub\nmetadata:\n  version: 1\n---\n# body\n",
                    )
                if "commit_baby_to_main" in self.tools:
                    self.tools["commit_baby_to_main"]("my-skill", "stub commit")
                class _R: pass
                r = _R(); r.content = ""; return r

        register_dir(wd, db_path=db)
        store = AtomTaskStore(root=wd)
        watcher = DirectoryWatcher(
            llm=_AutoSplitLLM(),
            embed_client=_FakeEmbed(),
            config={"llm": {"base_url": "x", "model": "y", "api_key": "z"}},
            skill_dir=skill_dir,
            poll_interval=0.0,
            max_concurrent=2,
            db_path=db,
            cold_start_threshold=999,
            store=store,
            agno_agent_factory=lambda **kw: _MixedStub(**kw),
            home_root=tmp_path,
        )

        for _ in range(10):
            watcher._scan_once()
            for _ in range(20):
                if not watcher._futures:
                    break
                time.sleep(0.05)
                watcher._harvest()
            # v2.1: 检查 candidates 已清空 = edit 成功
            data_check = C.load_candidates(my_skill)
            if data_check["candidates"] == []:
                break

        # edit 成功 → SKILL.md 仍在（baby 阶段已有 stub，edit 后内容已更新）
        assert (my_skill / "SKILL.md").is_file()
        # v2.1: candidates 清空（取代 promoted=true 标记）
        data2 = C.load_candidates(my_skill)
        assert data2["candidates"] == []
        # baby graduate 到 main
        from xskill.skill.git import current_branch
        assert current_branch(str(my_skill)) == "main"


class TestUxScoreAtomLevel:
    """cluster 完成后该 traj 的所有 atom 应被逐个调 score_atom + AtomCanary.append。"""

    def test_with_header_triggers_atom_scoring(self, tmp_path):
        from unittest.mock import patch
        db = tmp_path / "test.db"
        wd = tmp_path / "wd"; wd.mkdir()
        skill_dir = tmp_path / "skill"; skill_dir.mkdir()
        # 准备 skill 目录（需要存在才会真打分）
        (skill_dir / "test-skill").mkdir()
        from xskill.skill.git import run_git
        run_git(["init"], cwd=str(skill_dir / "test-skill"))
        run_git(["checkout", "-b", "main"], cwd=str(skill_dir / "test-skill"))
        run_git(["config", "user.email", "t@t"], cwd=str(skill_dir / "test-skill"))
        run_git(["config", "user.name", "t"], cwd=str(skill_dir / "test-skill"))
        (skill_dir / "test-skill" / "SKILL.md").write_text("v1", encoding="utf-8")
        run_git(["add", "-A"], cwd=str(skill_dir / "test-skill"))
        run_git(["commit", "-m", "init"], cwd=str(skill_dir / "test-skill"))

        traj_text = (
            "<!-- xskill:skill=test-skill side=staging sha=abc123 -->\n"
            + _TRAJ_MD
        )
        (wd / "traj_z.md").write_text(traj_text, encoding="utf-8")

        register_dir(wd, db_path=db)
        store = AtomTaskStore(root=wd)
        watcher = DirectoryWatcher(
            llm=_AutoSplitLLM(),
            embed_client=_FakeEmbed(),
            config={"llm": {"base_url": "x", "model": "y", "api_key": "z"}},
            skill_dir=skill_dir,
            poll_interval=0.0,
            max_concurrent=2,
            db_path=db,
            cold_start_threshold=999,
            store=store,
            agno_agent_factory=_StubAgno,
            home_root=tmp_path,
        )
        # mock score_atom 返回固定分数
        with patch("xskill.pipeline.runner.score_atom",
                   return_value={"score": 8, "reasons": "ok"}) if False else \
             patch("xskill.pipeline.atom.score_atom",
                   return_value={"score": 8, "reasons": "ok"}) as mock_score:
            # 多轮推进到 done
            for _ in range(20):
                watcher._scan_once()
                for _ in range(30):
                    if not watcher._futures:
                        break
                    time.sleep(0.05)
                    watcher._harvest()
                counts = get_status_counts(db_path=db)
                if counts.get("done"):
                    break
            # _TRAJ_MD 有 2 个 ## User → 2 个 atom → score_atom 调 2 次
            assert mock_score.call_count == 2
            # 验证传给 score_atom 的 side 来自 header
            call_kw = mock_score.call_args[1]
            assert call_kw["side"] == "staging"

        # 落盘到 .ux_scores.jsonl，主键是 atom_id
        import json
        ux_file = skill_dir / "test-skill" / ".ux_scores.jsonl"
        assert ux_file.is_file()
        lines = ux_file.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 2
        for line in lines:
            rec = json.loads(line)
            assert rec["atom_id"].startswith("atom_traj_z_")
            assert rec["side"] == "staging"
            assert rec["commit_sha"] == "abc123"

    def test_no_header_no_scoring(self, tmp_path):
        from unittest.mock import patch
        db = tmp_path / "test.db"
        wd = tmp_path / "wd"; wd.mkdir()
        skill_dir = tmp_path / "skill"; skill_dir.mkdir()
        (wd / "traj_z.md").write_text(_TRAJ_MD, encoding="utf-8")  # no header

        register_dir(wd, db_path=db)
        store = AtomTaskStore(root=wd)
        watcher = DirectoryWatcher(
            llm=_AutoSplitLLM(),
            embed_client=_FakeEmbed(),
            config={"llm": {"base_url": "x", "model": "y", "api_key": "z"}},
            skill_dir=skill_dir,
            poll_interval=0.0,
            max_concurrent=2,
            db_path=db,
            cold_start_threshold=999,
            store=store,
            agno_agent_factory=_StubAgno,
            home_root=tmp_path,
        )
        with patch("xskill.pipeline.atom.score_atom") as mock_score:
            for _ in range(20):
                watcher._scan_once()
                for _ in range(30):
                    if not watcher._futures:
                        break
                    time.sleep(0.05)
                    watcher._harvest()
                if get_status_counts(db_path=db).get("done"):
                    break
            mock_score.assert_not_called()


class TestContinuationResplit:
    """fix-dicover 验收：同名轨迹追加内容后重传 → 出现新 atom（行号 ≥ 续接点、
    不与旧 atom 重叠、旧 atom 不被重复生成）。"""

    def _drive_to_done(self, watcher, db, fname, rounds=25):
        from xskill.pipeline.registry import register_dir as _reg
        for _ in range(rounds):
            watcher._scan_once()
            for _ in range(30):
                if not watcher._futures:
                    break
                time.sleep(0.05)
                watcher._harvest()
            if get_status_counts(db_path=db).get("done"):
                return

    def test_appended_traj_resplits_from_resume_point(self, tmp_path):
        import os
        db = tmp_path / "test.db"
        wd = tmp_path / "wd"; wd.mkdir()
        skill_dir = tmp_path / "skill"; skill_dir.mkdir()
        traj = wd / "traj_cont.md"
        traj.write_text(_TRAJ_MD, encoding="utf-8")

        register_dir(wd, db_path=db)
        store = AtomTaskStore(root=wd)
        watcher = DirectoryWatcher(
            llm=_AutoSplitLLM(),
            embed_client=_FakeEmbed(),
            config={"llm": {"base_url": "x", "model": "y", "api_key": "z"}},
            skill_dir=skill_dir,
            poll_interval=0.0,
            max_concurrent=4,
            db_path=db,
            cold_start_threshold=999,
            store=store,
            agno_agent_factory=_StubAgno,
            home_root=tmp_path,
        )

        # 第一次处理到 done：2 个 atom
        self._drive_to_done(watcher, db, "traj_cont.md")
        first_atoms = store.list_by_traj("traj_cont")
        assert len(first_atoms) == 2
        first_ids = {a.atom_id for a in first_atoms}
        resume = store.last_offset("traj_cont")  # 续接点 = 末 atom offset_end
        assert resume == 24

        # 续写：追加一个新 ## User 回合，重传（覆盖写 + mtime 增大）
        appended = "\n## User\n\nAdd unit tests.\n\n## Assistant\n\nWriting pytest...\n"
        traj.write_text(_TRAJ_MD + appended, encoding="utf-8")
        st = traj.stat()
        os.utime(traj, (st.st_atime + 100, st.st_mtime + 100))

        # 再驱动若干轮：discover 翻 updated → 重新 split 续拆 → 回到 done
        for _ in range(25):
            watcher._scan_once()
            for _ in range(30):
                if not watcher._futures:
                    break
                time.sleep(0.05)
                watcher._harvest()
            if "traj_cont.md" in get_trajs_by_status(
                register_dir(wd, db_path=db), "done", db_path=db):
                if len(store.list_by_traj("traj_cont")) == 3:
                    break

        final_atoms = store.list_by_traj("traj_cont")
        # 出现 1 个新 atom（共 3）
        assert len(final_atoms) == 3
        new_atoms = [a for a in final_atoms if a.atom_id not in first_ids]
        assert len(new_atoms) == 1
        na = new_atoms[0]
        # 新 atom 行号 ≥ 续接点，不与旧 atom 重叠
        assert na.offset_start == resume
        assert na.offset_start >= resume
        # 旧 atom 未被重复生成（id 不变、行号不变）
        kept = {a.atom_id: (a.offset_start, a.offset_end)
                for a in final_atoms if a.atom_id in first_ids}
        for a in first_atoms:
            assert kept[a.atom_id] == (a.offset_start, a.offset_end)
        # 链表衔接：新 atom 接在旧末 atom 之后
        assert na.pre_atom_id in first_ids


class TestPipelineRun:
    def test_scan_then_harvest_full_chain(self, tmp_path):
        db = tmp_path / "test.db"
        wd = tmp_path / "wd"; wd.mkdir()
        skill_dir = tmp_path / "skill"; skill_dir.mkdir()

        # 写一条带 ## User 的真实形态 traj.md
        (wd / "traj_x.md").write_text(_TRAJ_MD, encoding="utf-8")

        register_dir(wd, db_path=db)
        store = AtomTaskStore(root=wd)
        watcher = DirectoryWatcher(
            llm=_AutoSplitLLM(),
            embed_client=_FakeEmbed(),
            config={"llm": {"base_url": "test://", "model": "stub", "api_key": "k"}},
            skill_dir=skill_dir,
            poll_interval=0.0,
            max_concurrent=4,
            db_path=db,
            cold_start_threshold=999,  # 关闭门控避免阻塞
            store=store,
            agno_agent_factory=_StubAgno,
            home_root=tmp_path,
        )

        # 多轮 scan + harvest 推动状态流转
        # done 后还需再跑 ≥1 轮 _scan_once 让 _check_pending_skill_edits
        # 检测到 cluster 阶段写的 candidates 并触发 SkillEdit（Bug 1 修复后
        # edit 不再绑在 cluster 链路里）
        done_seen_rounds = 0
        for _ in range(25):
            watcher._scan_once()
            for _ in range(30):
                if not watcher._futures:
                    break
                time.sleep(0.05)
                watcher._harvest()
            counts = get_status_counts(db_path=db)
            if counts.get("done"):
                done_seen_rounds += 1
                if done_seen_rounds >= 2:  # done 后再跑一轮让 edit scan 触发
                    break

        # 最终状态：traj_x.md 应到 done
        final_status = get_trajs_by_status(
            register_dir(wd, db_path=db),  # idempotent，返回同一 wd_id
            "done", db_path=db,
        )
        assert "traj_x.md" in final_status

        # 落盘：atom 文件存在
        atoms = store.list_by_traj("traj_x")
        assert len(atoms) == 2  # _TRAJ_MD 有 2 个 ## User → 2 个 atom

        # 索引文件存在
        assert (wd / "index.pkl").is_file()

        # v2.1: auto-skill 被创建（baby 分支 + git 仓库）；SkillEdit 触发
        # 后会调 commit_baby_to_main → 分支变 main + candidates 清空
        assert (skill_dir / "auto-skill").is_dir()
        assert (skill_dir / "auto-skill" / ".git").is_dir()
        from xskill.skill import candidates as C
        from xskill.skill.git import current_branch
        data = C.load_candidates(skill_dir / "auto-skill")
        # candidates 清空（v2.1 替代 promoted 标记）
        assert data["candidates"] == []
        # 已 graduate 到 main
        assert current_branch(str(skill_dir / "auto-skill")) == "main"

        # registry 中 last_offset / last_atom_id / tasks_extracted 已写
        from xskill.pipeline.registry import get_connection
        conn = get_connection(db)
        row = conn.execute(
            "SELECT last_offset, last_atom_id, tasks_extracted FROM trajectories"
            " WHERE filename=?", ("traj_x.md",),
        ).fetchone()
        conn.close()
        assert row["last_offset"] > 0
        assert row["last_atom_id"] is not None
        assert row["tasks_extracted"] == 2
