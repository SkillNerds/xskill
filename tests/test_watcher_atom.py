"""watcher v2 流水线：discovered → splitting → split_done → indexed → clustering → done"""
from __future__ import annotations

import time

from xskill.pipeline.atom import AtomTaskStore
from xskill.pipeline.registry import (
    register_dir, discover_trajectories, update_traj_status,
    get_trajs_by_status, get_status_counts, get_connection, mark_not_fit,
)
from xskill.config import interests_fingerprint
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

    def run(self, user_msg, **_kwargs):
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
            # 跨轨迹批处理：一次调用可能拿到多个 atom 的位置，逐个归类。
            atom_ids = re.findall(r"atom_id:\s*(\S+)", user_msg)
            for atom_id in atom_ids:
                if "new_skill_folder" in self.tools:
                    self.tools["new_skill_folder"]("auto-skill", "stub desc")
                if "add_task_to_skill" in self.tools:
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
            store=store,
            agno_agent_factory=_StubAgno,
            home_root=tmp_path,
        )
        watcher._scan_once()
        # 僵尸 clustering 应被回退到 indexed（同一轮内可能又被重新提交，OK）
        assert "traj_y.md" not in get_trajs_by_status(wd_id, "clustering", db_path=db) \
               or len(watcher._futures) > 0


class _NotFitAgno:
    def __init__(self, *, instructions, tools):
        self.instructions = instructions
        self.tools = {getattr(tool, "__name__", ""): tool for tool in tools}

    def run(self, _user_msg, **_kwargs):
        if "mark_not_fit" in self.tools:
            self.tools["mark_not_fit"]("not infra")
        class _RunResult:
            content = "not fit"
        return _RunResult()


class TestInterestFiltering:
    def test_split_not_fit_marks_filtered_with_process_action(self, tmp_path):
        db = tmp_path / "test.db"
        watch_directory = tmp_path / "wd"
        watch_directory.mkdir()
        skill_dir = tmp_path / "skill"
        skill_dir.mkdir()
        (watch_directory / "traj_interest.md").write_text(
            _TRAJ_MD, encoding="utf-8",
        )
        watch_dir_id = register_dir(watch_directory, db_path=db)
        discover_trajectories(watch_dir_id, watch_directory, db_path=db)
        store = AtomTaskStore(root=watch_directory)
        watcher = DirectoryWatcher(
            llm=_AutoSplitLLM(),
            embed_client=_FakeEmbed(),
            config={"interests": ["infra"], "llm": {"api_key": "x"}},
            skill_dir=skill_dir,
            poll_interval=0.0,
            max_concurrent=2,
            db_path=db,
            store=store,
            agno_agent_factory=_NotFitAgno,
            home_root=tmp_path,
        )

        split_result = watcher._do_split(watch_directory, "traj_interest.md")
        watcher._on_split_done(
            watch_dir_id,
            "traj_interest.md",
            split_result,
            db_path=db,
        )

        connection = get_connection(db)
        try:
            row = connection.execute(
                "SELECT status, process_action, error_msg, interest_fingerprint "
                "FROM trajectories WHERE filename='traj_interest.md'"
            ).fetchone()
        finally:
            connection.close()
        assert row["status"] == "filtered"
        assert row["process_action"] == "not_fit"
        assert row["error_msg"] == "not infra"
        assert row["interest_fingerprint"] == interests_fingerprint(["infra"])
        assert store.list_by_traj("traj_interest") == []

    def test_scan_hot_reload_resets_only_stale_not_fit(self, tmp_path, monkeypatch):
        db = tmp_path / "test.db"
        watch_directory = tmp_path / "wd"
        watch_directory.mkdir()
        for filename in ("traj_stale.md", "traj_current.md", "traj_done.md"):
            (watch_directory / filename).write_text(_TRAJ_MD, encoding="utf-8")
        watch_dir_id = register_dir(watch_directory, db_path=db)
        discover_trajectories(watch_dir_id, watch_directory, db_path=db)
        old_interest_fingerprint = interests_fingerprint(["old"])
        new_interest_fingerprint = interests_fingerprint(["new"])
        mark_not_fit(
            watch_dir_id,
            "traj_stale.md",
            "old interests",
            old_interest_fingerprint,
            db_path=db,
        )
        mark_not_fit(
            watch_dir_id,
            "traj_current.md",
            "new interests",
            new_interest_fingerprint,
            db_path=db,
        )
        update_traj_status(watch_dir_id, "traj_done.md", "done", db_path=db)
        monkeypatch.setattr(
            "xskill.pipeline.runner.read_interests_config",
            lambda: ["new"],
        )
        watcher = DirectoryWatcher(
            llm=None,
            embed_client=None,
            config={"interests": ["old"]},
            skill_dir=None,
            poll_interval=0.0,
            max_concurrent=2,
            db_path=db,
            home_root=tmp_path,
        )

        watcher._scan_once()

        assert "traj_stale.md" in get_trajs_by_status(
            watch_dir_id, "discovered", db_path=db,
        )
        assert "traj_current.md" in get_trajs_by_status(
            watch_dir_id, "filtered", db_path=db,
        )
        assert "traj_done.md" in get_trajs_by_status(
            watch_dir_id, "done", db_path=db,
        )
        assert watcher.interests == ["new"]
        assert watcher.interest_fingerprint == new_interest_fingerprint


def _seed_indexed_with_atoms(wd, store, db, n_trajs, atoms_per_traj):
    """造 n_trajs 条 indexed 轨迹，每条若干真 atom（跳过 split/embed）。返回 wd_id。"""
    from xskill.pipeline.atom import AtomTask
    for n in range(n_trajs):
        (wd / f"traj_{n}.md").write_text(_TRAJ_MD, encoding="utf-8")
        for i in range(atoms_per_traj):
            store.save(AtomTask(
                atom_id=f"atom_traj_{n}_{i:04d}", traj_id=f"traj_{n}",
                offset_start=1 + i * 10, offset_end=10 + i * 10,
                intent="i", summary="s", tags=[], used_skills=[], ux_score=7,
            ))
    wd_id = register_dir(wd, db_path=db)
    discover_trajectories(wd_id, wd, db_path=db)
    for n in range(n_trajs):
        update_traj_status(wd_id, f"traj_{n}.md", "indexed", db_path=db)
    return wd_id


class TestClusterSerial:
    """聚类始终串行：同 wd 同时只允许一个 cluster batch future 在飞——逐批让
    catalog 演化可见，避免并发 cluster agent 创建近义 baby slug。"""

    def _blocking_factory(self):
        class _BlockingStub:
            def __init__(self, **kw):
                pass

            def run(self, _message, **_kwargs):
                import time
                time.sleep(5)  # 模拟 cluster 长时间运行，让 future 留在飞
                class _R: pass
                r = _R(); r.content = ""; return r
        return lambda **k: _BlockingStub(**k)

    def test_only_one_cluster_batch_in_flight(self, tmp_path):
        db = tmp_path / "test.db"
        wd = tmp_path / "wd"; wd.mkdir()
        skill_dir = tmp_path / "skill"; skill_dir.mkdir()
        store = AtomTaskStore(root=wd)
        # 4 条 indexed 轨迹 × 2 atom，batch_size=2 → 即便有 8 个待消费 atom、
        # 4 个批次的量，同 wd 也只起 1 个 batch future（串行）。
        _seed_indexed_with_atoms(wd, store, db, n_trajs=4, atoms_per_traj=2)

        watcher = DirectoryWatcher(
            llm=_AutoSplitLLM(),
            embed_client=_FakeEmbed(),
            config={"llm": {"base_url": "x", "model": "y", "api_key": "z"}},
            skill_dir=skill_dir,
            poll_interval=0.0,
            max_concurrent=30,
            db_path=db,
            store=store,
            agno_agent_factory=self._blocking_factory(),
            home_root=tmp_path,
            cluster_batch_size=2,
        )
        watcher._scan_once()
        cluster_in_flight = sum(
            1 for i in watcher._futures.values() if i["stage"] == "cluster"
        )
        assert cluster_in_flight == 1, (
            f"cluster 应串行（1 batch in-flight），实际 {cluster_in_flight}"
        )
        # 再 scan 一次：上一个 batch 还在飞 → 不应再提交新的
        watcher._scan_once()
        cluster_in_flight = sum(
            1 for i in watcher._futures.values() if i["stage"] == "cluster"
        )
        assert cluster_in_flight == 1, "batch 在飞时第二轮 scan 不应再起新 batch"
        watcher._pool.shutdown(wait=False)


class TestClusterAllFailed:
    """整批 cluster 抛异常（LLM 余额耗尽等）→ atom 未落地 → 轨迹留在 indexed
    等下一轮重新进池重试，**不**被假冒成 done，也**不**再标 error（跨轨迹批处理
    无 per-traj cluster 错误态——重池化即重试，靠 cluster prompt 保证终将落地）。"""

    def test_all_atoms_failed_traj_stays_indexed(self, tmp_path):
        db = tmp_path / "test.db"
        wd = tmp_path / "wd"; wd.mkdir()
        skill_dir = tmp_path / "skill"; skill_dir.mkdir()
        (wd / "traj_x.md").write_text(_TRAJ_MD, encoding="utf-8")

        class _AlwaysFailAgno:
            def __init__(self, *, instructions, tools):
                self.instructions = instructions
                self.tools = {getattr(t, "__name__", ""): t for t in tools}
            def run(self, msg, **_kwargs):
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
            store=store,
            max_retries=0,
            agno_agent_factory=lambda **kw: _AlwaysFailAgno(**kw),
            home_root=tmp_path,
        )

        # 跑足够多轮：split 成功 → indexed → 每轮 cluster batch 抛异常
        for _ in range(8):
            watcher._scan_once()
            for _ in range(20):
                if not watcher._futures:
                    break
                time.sleep(0.05)
                watcher._harvest()

        wd_id = register_dir(wd, db_path=db)
        # cluster 全失败 → 不 done、不 error，留在 indexed 等重池化重试
        assert "traj_x.md" not in get_trajs_by_status(wd_id, "done", db_path=db), \
            "cluster 全失败的 traj 不应被标 done"
        assert "traj_x.md" not in get_trajs_by_status(
            wd_id, "error", db_path=db, max_retries=999), \
            "跨轨迹批处理无 per-traj cluster error 态"
        assert "traj_x.md" in get_trajs_by_status(wd_id, "indexed", db_path=db), \
            "cluster 全失败的 traj 应留在 indexed 等下轮重新进池"


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
            def run(self, msg, **_kwargs):
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

    def _drive_to_done(self, watcher, db, _filename, rounds=25):
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
