"""冷启动 epoch 屏障回归测试（轨迹堰塞修复）。

验证：
  1. ColdStartController 纯逻辑：from_config 缺省关闭、字段解析、非法配置抛错、
     active/barrier_reached/consume_barrier 生命周期。
  2. 轨迹堰塞复现：候选 weightscore 低于默认晋升阈值（3<10）时，正常在线路径
     **不会**毕业任何技能（这就是堰塞——小数据下没有技能能毕业）。
  3. 冷启动 hold：启用冷启动、屏障未到 → _run_skill_edit_step 不写正文，
     baby 技能继续停在 baby，candidates 不清。
  4. 屏障 flush：算法落 sentinel → _run_skill_edit_step 用 flush_threshold 一次性
     把所有有候选的 baby 技能批量毕业到 main，candidates 清空，sentinel 被消费。
  5. flush 后 epoch 跑满 → 转在线：新的低分技能重回正常阈值，不再被强制毕业。
"""
from __future__ import annotations

import threading

import pytest

from xskill.pipeline.atom import AtomTaskStore
from xskill.pipeline.cold_start import ColdStartController
from xskill.pipeline.registry import register_dir
from xskill.pipeline.runner import DirectoryWatcher
from xskill.skill import candidates as C
from xskill.skill.git import init_skill_repo_on_baby, current_branch
from tests.test_atom_task_store import _FakeEmbed
from tests.test_task_agent import _AutoSplitLLM
from tests.test_skilledit_parallel import _make_barrier_agno


def _seed_baby(skill_root, slug, weightscore):
    """造一个 baby 分支 + 候选累计 weightscore 的 skill。"""
    sd = skill_root / slug
    init_skill_repo_on_baby(str(sd), name=slug, description="stub")
    data = {"candidates": []}
    data, _ = C.add_atom_contribution(data, f"atom_{slug}_0001", weightscore)
    C.save_candidates(sd, data)
    return sd


def _serial_factory():
    """非并发 agno stub：写 SKILL.md + commit_baby_to_main（barrier=1 即时放行）。"""
    return _make_barrier_agno(1, threading.Barrier(1))


def _make_watcher(tmp_path, skill_root, cold_start_cfg):
    db = tmp_path / "test.db"
    wd = tmp_path / "wd"
    wd.mkdir(exist_ok=True)
    register_dir(wd, db_path=db)
    store = AtomTaskStore(root=wd)
    cfg = {"llm": {"base_url": "x", "model": "y", "api_key": "z"}}
    if cold_start_cfg is not None:
        cfg["cold_start"] = cold_start_cfg
    return DirectoryWatcher(
        llm=_AutoSplitLLM(),
        embed_client=_FakeEmbed(),
        config=cfg,
        skill_dir=skill_root,
        poll_interval=0.0,
        max_concurrent=4,
        db_path=db,
        store=store,
        agno_agent_factory=_serial_factory(),
        home_root=tmp_path,
    )


# ───────────────────────── ColdStartController 纯逻辑 ─────────────────────────

class TestColdStartController:
    def test_disabled_by_default(self, tmp_path):
        cs = ColdStartController.from_config({}, tmp_path)
        assert cs.enabled is False
        assert cs.active is False
        assert cs.barrier_reached() is False  # 未启用永远 False

    def test_enabled_parsing_and_default_barrier(self, tmp_path):
        cs = ColdStartController.from_config(
            {"cold_start": {"enabled": True}}, tmp_path)
        assert cs.enabled and cs.active
        assert cs.flush_threshold == 1 and cs.epochs == 1
        assert cs.barrier_path == tmp_path / "EPOCH_FLUSH"

    def test_explicit_barrier_path(self, tmp_path):
        p = tmp_path / "sub" / "FLUSH"
        cs = ColdStartController.from_config(
            {"cold_start": {"enabled": True, "barrier_path": str(p)}}, tmp_path)
        assert cs.barrier_path == p

    @pytest.mark.parametrize("bad", [
        {"flush_threshold": 0}, {"flush_threshold": -1}, {"epochs": 0},
    ])
    def test_invalid_config_raises(self, tmp_path, bad):
        with pytest.raises(ValueError):
            ColdStartController.from_config(
                {"cold_start": {"enabled": True, **bad}}, tmp_path)

    def test_barrier_lifecycle(self, tmp_path):
        bp = tmp_path / "EPOCH_FLUSH"
        cs = ColdStartController.from_config(
            {"cold_start": {"enabled": True, "epochs": 1}}, tmp_path)
        assert cs.barrier_reached() is False           # sentinel 不存在
        bp.touch()
        assert cs.barrier_reached() is True            # sentinel 到达
        cs.consume_barrier()
        assert not bp.exists()                         # 被消费删除
        assert cs._epochs_done == 1
        assert cs.active is False                      # 跑满 1 epoch → 转在线
        bp.touch()
        assert cs.barrier_reached() is False           # 已非 active，屏障不再触发


# ───────────────────────── 端到端：堰塞复现 + 屏障修复 ─────────────────────────

class TestColdStartEpochFlush:
    def test_jam_reproduced_without_cold_start(self, tmp_path):
        """对照组：候选 3<10，关闭冷启动 → 正常路径不毕业任何技能（堰塞）。"""
        skill_root = tmp_path / "skill"; skill_root.mkdir()
        sd = _seed_baby(skill_root, "sk-jam", weightscore=3)
        w = _make_watcher(tmp_path, skill_root, cold_start_cfg=None)
        w._run_skill_edit_step()
        assert current_branch(str(sd)) == "baby", "低分技能不该在在线路径毕业"
        assert C.load_candidates(sd)["candidates"], "候选不该被清"

    def test_hold_then_flush(self, tmp_path):
        """冷启动启用：屏障未到 hold；落 sentinel 后批量 flush 把低分技能毕业。"""
        skill_root = tmp_path / "skill"; skill_root.mkdir()
        slugs = ["sk-a", "sk-b", "sk-c"]
        dirs = {s: _seed_baby(skill_root, s, weightscore=3) for s in slugs}
        w = _make_watcher(tmp_path, skill_root,
                          cold_start_cfg={"enabled": True, "flush_threshold": 1})

        # 屏障未到：hold —— 全部停在 baby，候选不清
        w._run_skill_edit_step()
        for s, sd in dirs.items():
            assert current_branch(str(sd)) == "baby", f"{s} 不该在 hold 阶段毕业"
            assert C.load_candidates(sd)["candidates"], f"{s} hold 阶段候选不该清"

        # 算法落 epoch sentinel → 批量 flush
        (tmp_path / "EPOCH_FLUSH").touch()
        w._run_skill_edit_step()

        for s, sd in dirs.items():
            assert current_branch(str(sd)) == "main", f"{s} 屏障后应毕业到 main"
            assert C.load_candidates(sd)["candidates"] == [], f"{s} 毕业后候选应清空"
            body = (sd / "SKILL.md").read_text(encoding="utf-8")
            assert f"body-of-{s}" in body
        assert w._stats["skills_edited"] == len(slugs)
        # sentinel 被消费
        assert not (tmp_path / "EPOCH_FLUSH").exists()

    def test_online_after_epoch_exhausted(self, tmp_path):
        """flush 跑满 epochs 后转在线：新低分技能重回正常阈值，不被强制毕业。"""
        skill_root = tmp_path / "skill"; skill_root.mkdir()
        first = _seed_baby(skill_root, "sk-first", weightscore=3)
        w = _make_watcher(tmp_path, skill_root,
                          cold_start_cfg={"enabled": True, "epochs": 1})
        # 冷启动 epoch：落屏障 flush
        (tmp_path / "EPOCH_FLUSH").touch()
        w._run_skill_edit_step()
        assert current_branch(str(first)) == "main"
        assert w._cold_start.active is False  # 跑满 → 在线

        # 在线阶段：新低分技能 + 再落屏障，也不应被强制毕业（屏障对非 active 无效）
        second = _seed_baby(skill_root, "sk-second", weightscore=3)
        (tmp_path / "EPOCH_FLUSH").touch()
        w._run_skill_edit_step()
        assert current_branch(str(second)) == "baby", "在线阶段低分技能不该毕业"
