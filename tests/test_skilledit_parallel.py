"""跨技能 SkillEdit 并行化回归测试。

修复对象：DirectoryWatcher._check_pending_skill_edits 原来用同步 for 循环
顺序对每个 skill 调 SkillEditAgent.maybe_run()（每个 ~LLM 一次写正文，串行
= N×耗时）。修复把每个 skill 的 maybe_run() 提交到 edit pool 并发跑，结果
收齐后回主线程串行做 _stats 自增 + install。

本测试验证：
  1. 并发跑多个 skill 的 SkillEdit，各自 per-skill git 仓不串扰——每个 skill
     的 SKILL.md 正文必须引用**自己**的 slug，且各自从 baby graduate 到 main。
  2. 最终毕业结果与串行版等价：所有满阈值的 baby skill 都被 graduate，
     candidates 都被清空，_stats["skills_edited"] 计数正确。
  3. 真正并发：用 barrier 让所有 maybe_run 的 LLM 调用同时在飞，证明不是
     串行退化（若串行，barrier 永远凑不齐 → 超时）。
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

from xskill.pipeline.atom import AtomTaskStore
from xskill.pipeline.registry import register_dir
from xskill.pipeline.runner import DirectoryWatcher
from xskill.skill import candidates as C
from tests.pool_helpers import pool_config
from xskill.skill.git import init_skill_repo_on_baby, current_branch
from tests.test_atom_task_store import _FakeEmbed
from tests.test_task_agent import _AutoSplitLLM


N_SKILLS = 5


def _tool_name(tool) -> str:
    return getattr(tool, "__name__", None) or getattr(tool, "name", "")


def _call_tool(tool, *args):
    entrypoint = tool if callable(tool) else getattr(tool, "entrypoint")
    return entrypoint(*args)


def _seed_baby_skill(skill_root, slug):
    """造一个 baby 分支 + 候选满阈值（weightscore=10≥10）的 skill。"""
    sd = skill_root / slug
    init_skill_repo_on_baby(str(sd), name=slug, description="stub")
    data = {"candidates": []}
    data, _ = C.add_atom_contribution(data, f"atom_{slug}_0001", 10)
    C.save_candidates(sd, data)
    return sd


def _make_barrier_agno(expected_workers, barrier):
    """造一个 agno stub 工厂：每个 SkillEditAgent 的 run() 先在 barrier 上
    等齐 expected_workers 个并发线程，再写自己的 SKILL.md + commit_baby_to_main。

    - barrier 凑齐 = 证明 expected_workers 个 maybe_run 真并发在飞（串行则死等超时）。
    - SKILL.md 正文写入自己的 slug = 串扰检测锚点：若 ctx 被别的线程改写，
      或目标解析错乱，正文里的 slug 会对不上目录名。
    """
    del expected_workers

    class _BarrierStub:
        def __init__(self, *, instructions, tools):
            self.instructions = instructions
            self.tools = {_tool_name(t): t for t in tools}

        def run(self, user_message, **unused_keyword_arguments):
            del unused_keyword_arguments
            import re
            # 从 scenario_block 抠出本 agent 负责的 skill 目录名（slug）
            directory_match = re.search(r"目标 skill 目录:\s*(\S+)", user_message)
            if directory_match:
                skill_slug = Path(directory_match.group(1).rstrip("/\\")).name
            else:
                skill_slug = "?"
            # 等齐——证明并发
            try:
                barrier.wait(timeout=20)
            except threading.BrokenBarrierError:
                pass
            skill_file_match = re.search(r"目标 SKILL\.md 路径:\s*(\S+)", user_message)
            if skill_file_match and "write_file" in self.tools:
                # 正文写入自己的 slug 作串扰锚点
                _call_tool(
                    self.tools["write_file"],
                    skill_file_match.group(1),
                    f"---\nname: {skill_slug}\ndescription: real body for {skill_slug}\n"
                    f"metadata:\n  version: 1\n---\n# {skill_slug}\nbody-of-{skill_slug}\n",
                )
            if "baby" in user_message and "commit_baby" in self.tools:
                _call_tool(
                    self.tools["commit_baby"],
                    skill_slug,
                    f"checkpoint {skill_slug}",
                )
            class _Response:
                pass
            response = _Response()
            response.content = ""
            return response

    def barrier_agent_factory(**factory_keyword_arguments):
        return _BarrierStub(**factory_keyword_arguments)

    return barrier_agent_factory


def _make_watcher(tmp_path, skill_root, factory, edit_workers):
    db = tmp_path / "test.db"
    wd = tmp_path / "wd"
    wd.mkdir(exist_ok=True)
    register_dir(wd, db_path=db)
    store = AtomTaskStore(root=wd)
    return DirectoryWatcher(
        llm=_AutoSplitLLM(),
        embed_client=_FakeEmbed(),
        config={"llm": {"base_url": "x", "model": "y", "api_key": "z"}},
        skill_dir=skill_root,
        poll_interval=0.0,
        pool_config=pool_config(workers=1, edit_workers=edit_workers),
        db_path=db,
        store=store,
        agno_agent_factory=factory,
        home_root=tmp_path,
    )


def _make_watcher_with_config(tmp_path, skill_root, factory, config):
    """Like _make_watcher but accepts a full config dict (for testing config wiring)."""
    db = tmp_path / "test.db"
    wd = tmp_path / "wd"
    wd.mkdir(exist_ok=True)
    register_dir(wd, db_path=db)
    store = AtomTaskStore(root=wd)
    return DirectoryWatcher(
        llm=_AutoSplitLLM(),
        embed_client=_FakeEmbed(),
        config=config,
        skill_dir=skill_root,
        poll_interval=0.0,
        pool_config=pool_config(workers=1),
        db_path=db,
        store=store,
        agno_agent_factory=factory,
        home_root=tmp_path,
    )


def test_runner_passes_jam_threshold(monkeypatch, tmp_path):
    """runner 构造 SkillEditAgent 时，把 config.canary jam 三闸参数注入。"""
    import xskill.agents.skill_edit_agent as SEA
    captured = {}
    real_init = SEA.SkillEditAgent.__init__
    def spy_init(self, *a, **kw):
        captured["jam_threshold"] = kw.get("jam_threshold")
        captured["min_jam_age_sec"] = kw.get("min_jam_age_sec")
        captured["jam_plateau_sec"] = kw.get("jam_plateau_sec")
        return real_init(self, *a, **kw)
    monkeypatch.setattr(SEA.SkillEditAgent, "__init__", spy_init)

    skill_root = tmp_path / "skill"
    skill_root.mkdir()
    # ws=15 > ATOM_PROMOTION_THRESHOLD(10) → 触发 _run_one
    _seed_baby_skill(skill_root, "s1")

    noop_barrier = threading.Barrier(1)
    factory = _make_barrier_agno(1, noop_barrier)
    config = {
        "llm": {"base_url": "x", "model": "y", "api_key": "z"},
        "canary": {
            "jam_threshold": 33,
            "min_jam_age_sec": 12,
            "jam_plateau_sec": 34,
        },
    }
    w = _make_watcher_with_config(tmp_path, skill_root, factory, config)
    w._check_pending_skill_edits()
    w._drain_futures(stage="skill_edit")

    assert captured.get("jam_threshold") == 33, (
        f"jam_threshold was not wired from config; captured={captured!r}"
    )
    assert captured.get("min_jam_age_sec") == 12
    assert captured.get("jam_plateau_sec") == 34


def test_runner_remembers_n1_retry_until_a_checkpoint_succeeds(tmp_path):
    skill_root = tmp_path / "skill"
    skill_root.mkdir()
    watcher = _make_watcher(
        tmp_path,
        skill_root,
        _make_barrier_agno(1, threading.Barrier(1)),
        edit_workers=1,
    )
    skill_dir = skill_root / "retry-target"

    watcher._on_skill_edit_done((skill_dir, False, 1))
    assert watcher._skill_edit_retry_batch_sizes[skill_dir] == 1

    watcher._on_skill_edit_done(
        (skill_dir, False, watcher.skill_edit_batch_size)
    )
    assert skill_dir not in watcher._skill_edit_retry_batch_sizes


class TestSkillEditParallel:
    def test_concurrent_no_cross_contamination_and_all_graduate(self, tmp_path):
        """N 个 baby skill 并发跑 SkillEdit：barrier 凑齐证明真并发，
        各自 per-skill 仓不串扰，全部 graduate baby→main。"""
        skill_root = tmp_path / "skill"
        skill_root.mkdir()
        slugs = [f"skill-{i}" for i in range(N_SKILLS)]
        skill_dirs = {s: _seed_baby_skill(skill_root, s) for s in slugs}

        # 全在 baby
        for s, sd in skill_dirs.items():
            assert current_branch(str(sd)) == "baby"

        # barrier 需要凑齐 N 个并发线程才放行 → edit workers 必须足够且真并发
        barrier = threading.Barrier(N_SKILLS)
        factory = _make_barrier_agno(N_SKILLS, barrier)
        watcher = _make_watcher(tmp_path, skill_root, factory,
                                edit_workers=N_SKILLS)

        t0 = time.time()
        watcher._check_pending_skill_edits()
        # SkillEdit 现在是像 split/cluster 一样的非阻塞 future 提交——
        # _check_pending_skill_edits() 立即返回,真正的完成要 drain。
        watcher._drain_futures(stage="skill_edit")
        elapsed = time.time() - t0
        # 若串行退化，barrier 永远凑不齐 → 每个 wait 撞 20s timeout → 远超阈值。
        # 真并发：一波凑齐后秒回。给宽松上限避免 CI 抖动误判。
        assert elapsed < 15, f"疑似串行退化：{elapsed:.1f}s"

        # 1) 全部 graduate baby→main
        for s, sd in skill_dirs.items():
            assert current_branch(str(sd)) == "main", f"{s} 未 graduate"
            # 2) candidates 清空
            assert C.load_candidates(sd)["candidates"] == [], f"{s} 候选未清"
            # 3) 串扰锚点：SKILL.md 正文必须引用自己的 slug，不是别人的
            body = (sd / "SKILL.md").read_text(encoding="utf-8")
            assert f"body-of-{s}" in body, f"{s} 正文串扰: {body[:120]!r}"
            for other in slugs:
                if other != s:
                    assert f"body-of-{other}" not in body, \
                        f"{s} 仓混入了 {other} 的正文"

        # 4) stats 计数 = N（主线程串行汇总，无并发自增丢失）
        assert watcher._stats["skills_edited"] == N_SKILLS

    def test_equivalent_to_serial(self, tmp_path):
        """并行版毕业结果与单 edit worker（无 barrier）等价：
        同一组 baby skill，两种执行模式下毕业集合 + 正文内容一致。"""
        def _run(max_conc, use_barrier):
            sub = tmp_path / f"root_{max_conc}_{int(use_barrier)}"
            sub.mkdir()
            skill_root = sub / "skill"
            skill_root.mkdir()
            slugs = [f"sk-{i}" for i in range(3)]
            dirs = {s: _seed_baby_skill(skill_root, s) for s in slugs}
            if use_barrier:
                barrier = threading.Barrier(len(slugs))
                factory = _make_barrier_agno(len(slugs), barrier)
            else:
                # 单 worker 版不用 barrier（否则会死锁）
                noop = threading.Barrier(1)
                factory = _make_barrier_agno(1, noop)
            w = _make_watcher(sub, skill_root, factory, edit_workers=max_conc)
            w._check_pending_skill_edits()
            w._drain_futures(stage="skill_edit")
            graduated = {s: current_branch(str(d)) for s, d in dirs.items()}
            bodies = {s: (d / "SKILL.md").read_text(encoding="utf-8")
                      for s, d in dirs.items()}
            return graduated, bodies, w._stats["skills_edited"]

        ser_grad, ser_bodies, ser_n = _run(1, use_barrier=False)
        par_grad, par_bodies, par_n = _run(8, use_barrier=True)

        assert ser_grad == par_grad == {s: "main" for s in ser_grad}
        assert ser_n == par_n == 3
        # 正文按 slug 一一对应，内容等价
        assert set(ser_bodies) == set(par_bodies)
        for s in ser_bodies:
            assert f"body-of-{s}" in ser_bodies[s]
            assert f"body-of-{s}" in par_bodies[s]


def test_imported_ready_skill_is_scheduled_before_distilled(tmp_path):
    """两边都已达 SkillEdit 触发条件时，import 进来的 skill 先占编辑席位。"""
    from xskill.skill.importer import mark_skill_imported

    skill_root = tmp_path / "skill"
    skill_root.mkdir()
    _seed_baby_skill(skill_root, "alpha")
    imported = _seed_baby_skill(skill_root, "zeta")
    mark_skill_imported(imported)
    assert (imported / ".xskill-origin").read_text(encoding="utf-8").strip() == "import"
    from xskill.skill.importer import is_imported_skill as _is_imp
    assert _is_imp(imported) is True
    assert _is_imp(skill_root / "alpha") is False

    hold = threading.Event()

    class _HoldStub:
        def __init__(self, *, instructions, tools):
            del instructions, tools

        def run(self, user_message, **unused):
            del user_message, unused
            hold.wait(5)
            class _Response:
                content = ""
            return _Response()

    def factory(**kwargs):
        return _HoldStub(**kwargs)

    watcher = _make_watcher(tmp_path, skill_root, factory, edit_workers=1)
    try:
        watcher._check_pending_skill_edits()
        deadline = time.time() + 2
        seat = None
        while time.time() < deadline:
            seats = watcher._pools["edit"].status["seats"]
            if seats and seats[0]:
                seat = seats[0]
                break
            time.sleep(0.02)
        assert seat is not None
        assert seat["task"]["skill_name"] == "zeta"
        inflight = [
            info["skill_dir"].name
            for info in watcher._futures.values()
            if info.get("stage") == "skill_edit"
        ]
        assert inflight[0] == "zeta"
    finally:
        hold.set()
        watcher.stop()

