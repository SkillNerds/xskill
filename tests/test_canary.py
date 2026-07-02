"""
test_canary.py -- canary.py 灰度发布模块单元测试
=================================================
覆盖：
  - staging 分支创建 / 合入 / 丢弃
  - route_main_history_to_staging：commit 挪到 staging
  - pick_side：确定性 + 分布接近预期
  - ux_scores.jsonl：幂等追加 + 读取
  - controller check_and_decide：waiting / promoted / rejected / timeout
"""
from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

from xskill import canary
from xskill.skill.git import run_git


# ──────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────

def _init_repo(path: Path) -> Path:
    """初始化一个有 main 分支和 SKILL.md 的 git 仓库。"""
    path.mkdir(parents=True, exist_ok=True)
    run_git(["init"], cwd=str(path))
    run_git(["checkout", "-b", "main"], cwd=str(path))
    run_git(["config", "user.email", "test@t"], cwd=str(path))
    run_git(["config", "user.name", "test"], cwd=str(path))
    (path / "SKILL.md").write_text("v1", encoding="utf-8")
    run_git(["add", "-A"], cwd=str(path))
    run_git(["commit", "-m", "init"], cwd=str(path))
    return path


def _commit(path: Path, content: str, msg: str):
    (path / "SKILL.md").write_text(content, encoding="utf-8")
    run_git(["add", "-A"], cwd=str(path))
    run_git(["commit", "-m", msg], cwd=str(path))


# ──────────────────────────────────────────────────────
# staging 分支管理
# ──────────────────────────────────────────────────────

class TestStagingBranch:
    def test_no_staging_initially(self, tmp_path):
        sd = _init_repo(tmp_path / "skill")
        assert not canary.has_staging(sd)

    def test_route_creates_staging(self, tmp_path):
        sd = _init_repo(tmp_path / "skill")
        initial = canary.main_sha(sd)
        _commit(sd, "v2", "update")
        ok = canary.route_main_history_to_staging(sd, initial)
        assert ok
        assert canary.has_staging(sd)
        # main 应该回退到 initial
        assert canary.main_sha(sd) == initial
        # staging 上应该有 v2
        body = canary.read_skill_on_branch(sd, "staging")
        assert body.strip() == "v2"
        # main 上应该还是 v1
        body_main = canary.read_skill_on_branch(sd, "main")
        assert body_main.strip() == "v1"

    def test_merge_staging_to_main(self, tmp_path):
        sd = _init_repo(tmp_path / "skill")
        initial = canary.main_sha(sd)
        _commit(sd, "v2", "update")
        canary.route_main_history_to_staging(sd, initial)
        ok = canary.merge_staging_to_main(sd)
        assert ok
        assert not canary.has_staging(sd)
        body = canary.read_skill_on_branch(sd, "main")
        assert body.strip() == "v2"

    def test_discard_staging(self, tmp_path):
        sd = _init_repo(tmp_path / "skill")
        initial = canary.main_sha(sd)
        _commit(sd, "v2", "update")
        canary.route_main_history_to_staging(sd, initial)
        ok = canary.discard_staging(sd)
        assert ok
        assert not canary.has_staging(sd)
        body = canary.read_skill_on_branch(sd, "main")
        assert body.strip() == "v1"

    def test_skill_existed_on(self, tmp_path):
        # skill_existed_on expects: top-level skill_dir (parent) + skill_name (subdir)
        top = tmp_path / "skill_dir"
        sd = _init_repo(top / "my-skill")
        sha = canary.main_sha(sd)
        assert canary.skill_existed_on(top, sha, "my-skill")
        # non-existent skill
        assert not canary.skill_existed_on(top, sha, "no-such-skill")

    def test_staging_created_at_is_recent(self, tmp_path):
        sd = _init_repo(tmp_path / "skill")
        initial = canary.main_sha(sd)
        _commit(sd, "v2", "update")
        canary.route_main_history_to_staging(sd, initial)
        dt = canary.staging_created_at(sd)
        assert dt is not None
        diff = abs((datetime.now(timezone.utc) - dt.astimezone(timezone.utc)).total_seconds())
        assert diff < 60  # should be within last minute


# ──────────────────────────────────────────────────────
# pick_side：确定性 + 分布
# ──────────────────────────────────────────────────────

class TestPickSide:
    def test_deterministic(self):
        s1 = canary.pick_side("traj_001", "skill-a", 0.5)
        s2 = canary.pick_side("traj_001", "skill-a", 0.5)
        assert s1 == s2

    def test_different_traj_may_differ(self):
        sides = {canary.pick_side(f"traj_{i:04d}", "skill-a", 0.5) for i in range(100)}
        assert sides == {"main", "staging"}

    def test_probability_zero_always_main(self):
        for i in range(50):
            assert canary.pick_side(f"t{i}", "s", 0.0) == "main"

    def test_probability_one_always_staging(self):
        for i in range(50):
            assert canary.pick_side(f"t{i}", "s", 1.0) == "staging"

    def test_distribution_approximate(self):
        n = 2000
        staging_count = sum(
            1 for i in range(n)
            if canary.pick_side(f"t_{i}", "s", 0.2) == "staging"
        )
        ratio = staging_count / n
        assert 0.12 < ratio < 0.28, f"staging ratio {ratio} out of expected range"


# ──────────────────────────────────────────────────────
# ux_scores.jsonl I/O
# ──────────────────────────────────────────────────────

class TestUxScores:
    def test_append_and_load(self, tmp_path):
        sd = tmp_path / "skill"
        sd.mkdir()
        written = canary.append_ux_score(
            sd, traj_id="traj_001", skill_name="s", side="main",
            commit_sha="abc123", score=8.0, reasons="good",
        )
        assert written
        records = canary.load_ux_scores(sd)
        assert len(records) == 1
        assert records[0]["score"] == 8.0
        assert records[0]["side"] == "main"

    def test_idempotent(self, tmp_path):
        sd = tmp_path / "skill"
        sd.mkdir()
        canary.append_ux_score(
            sd, traj_id="traj_001", skill_name="s", side="main",
            commit_sha="abc", score=7, reasons="ok",
        )
        written2 = canary.append_ux_score(
            sd, traj_id="traj_001", skill_name="s", side="main",
            commit_sha="abc", score=9, reasons="dup",
        )
        assert not written2
        assert len(canary.load_ux_scores(sd)) == 1

    def test_recent_scores_filters_by_sha(self, tmp_path):
        sd = tmp_path / "skill"
        sd.mkdir()
        for i in range(3):
            canary.append_ux_score(
                sd, traj_id=f"t{i}", skill_name="s", side="main",
                commit_sha="sha_old", score=5.0, reasons="old",
            )
        for i in range(3):
            canary.append_ux_score(
                sd, traj_id=f"tn{i}", skill_name="s", side="main",
                commit_sha="sha_new", score=9.0, reasons="new",
            )
        recent = canary.recent_scores(sd, side="main", commit_sha="sha_new", n=5)
        assert len(recent) == 3
        assert all(r["commit_sha"] == "sha_new" for r in recent)


# ──────────────────────────────────────────────────────
# controller check_and_decide
# ──────────────────────────────────────────────────────

class TestController:
    def _setup_staged(self, tmp_path):
        sd = _init_repo(tmp_path / "skill")
        initial = canary.main_sha(sd)
        _commit(sd, "v2", "update")
        canary.route_main_history_to_staging(sd, initial)
        return sd

    def test_no_staging(self, tmp_path):
        sd = _init_repo(tmp_path / "skill")
        res = canary.check_and_decide(sd)
        assert res["action"] == "no_staging"

    def test_waiting_when_not_enough(self, tmp_path):
        sd = self._setup_staged(tmp_path)
        cfg = canary.CanaryConfig(min_samples=5)
        # 只加 2 条 main / 2 条 staging
        m_sha = canary.main_sha(sd)
        s_sha = canary.staging_sha(sd)
        for i in range(2):
            canary.append_ux_score(sd, traj_id=f"m{i}", skill_name="s", side="main",
                                   commit_sha=m_sha, score=5, reasons="")
            canary.append_ux_score(sd, traj_id=f"s{i}", skill_name="s", side="staging",
                                   commit_sha=s_sha, score=9, reasons="")
        res = canary.check_and_decide(sd, cfg)
        assert res["action"] == "waiting"

    def test_promoted_when_staging_wins(self, tmp_path):
        sd = self._setup_staged(tmp_path)
        cfg = canary.CanaryConfig(min_samples=3)
        m_sha = canary.main_sha(sd)
        s_sha = canary.staging_sha(sd)
        for i in range(3):
            canary.append_ux_score(sd, traj_id=f"m{i}", skill_name="s", side="main",
                                   commit_sha=m_sha, score=3, reasons="bad")
            canary.append_ux_score(sd, traj_id=f"s{i}", skill_name="s", side="staging",
                                   commit_sha=s_sha, score=9, reasons="good")
        res = canary.check_and_decide(sd, cfg)
        assert res["action"] == "promoted"
        assert not canary.has_staging(sd)
        body = canary.read_skill_on_branch(sd, "main")
        assert body.strip() == "v2"

    def test_rejected_when_main_wins(self, tmp_path):
        sd = self._setup_staged(tmp_path)
        cfg = canary.CanaryConfig(min_samples=3)
        m_sha = canary.main_sha(sd)
        s_sha = canary.staging_sha(sd)
        for i in range(3):
            canary.append_ux_score(sd, traj_id=f"m{i}", skill_name="s", side="main",
                                   commit_sha=m_sha, score=9, reasons="main good")
            canary.append_ux_score(sd, traj_id=f"s{i}", skill_name="s", side="staging",
                                   commit_sha=s_sha, score=2, reasons="staging bad")
        res = canary.check_and_decide(sd, cfg)
        assert res["action"] == "rejected"
        assert not canary.has_staging(sd)
        body = canary.read_skill_on_branch(sd, "main")
        assert body.strip() == "v1"  # main unchanged

    def test_timeout_discards(self, tmp_path):
        sd = self._setup_staged(tmp_path)
        cfg = canary.CanaryConfig(min_samples=5, max_days_hold=0)  # 0 天 = 立刻超时
        res = canary.check_and_decide(sd, cfg)
        assert res["action"] == "timeout_discarded"
        assert not canary.has_staging(sd)


# ──────────────────────────────────────────────────────
# gitignore 模板
# ──────────────────────────────────────────────────────

class TestGitignore:
    def test_ensure_creates_if_missing(self, tmp_path):
        sd = tmp_path / "skill"
        sd.mkdir()
        canary.ensure_gitignore(sd)
        gi = (sd / ".gitignore").read_text()
        assert ".ux_scores.jsonl" in gi
        assert ".lock" in gi

    def test_ensure_appends_if_partial(self, tmp_path):
        sd = tmp_path / "skill"
        sd.mkdir()
        (sd / ".gitignore").write_text("*.pyc\n", encoding="utf-8")
        canary.ensure_gitignore(sd)
        gi = (sd / ".gitignore").read_text()
        assert "*.pyc" in gi
        assert ".ux_scores.jsonl" in gi

    def test_ensure_noop_if_present(self, tmp_path):
        sd = tmp_path / "skill"
        sd.mkdir()
        (sd / ".gitignore").write_text(".ux_scores.jsonl\n.lock\n", encoding="utf-8")
        canary.ensure_gitignore(sd)
        gi = (sd / ".gitignore").read_text()
        assert gi.count(".ux_scores.jsonl") == 1


# ──────────────────────────────────────────────────────
# staging 物化与清理
# ──────────────────────────────────────────────────────

class TestMaterialize:
    def test_materialize_creates_canary_dir(self, tmp_path):
        sd = _init_repo(tmp_path / "skill")
        initial = canary.main_sha(sd)
        _commit(sd, "staging content", "add staging")
        canary.route_main_history_to_staging(sd, initial)
        assert canary.has_staging(sd)

        canary_root = tmp_path / "canary"
        out = canary.materialize_staging(sd, canary_root)
        assert out is not None
        assert (out / "SKILL.md").is_file()
        assert (out / "SKILL.md").read_text().strip() == "staging content"

    def test_materialize_returns_none_without_staging(self, tmp_path):
        sd = _init_repo(tmp_path / "skill")
        canary_root = tmp_path / "canary"
        # no staging branch → read_skill_on_branch returns None
        out = canary.materialize_staging(sd, canary_root)
        assert out is None

    # 老测试针对 ``canary.cleanup_canary``，那个函数在 2140de5 "死代码清理"
    # 提交里跟着一批未被任何路径调用的 helper 一起删了。当前 canary 模块的
    # 物化/丢弃语义由 ``materialize_staging`` + ``discard_staging`` 承担，
    # 在 TestMaterialize 上面已经覆盖。这里不再保留 orphan 测试。


def test_jam_threshold_default_is_50():
    assert canary.CanaryConfig.from_dict({}).jam_threshold == 50


def test_jam_threshold_read_from_dict():
    assert canary.CanaryConfig.from_dict({"jam_threshold": 30}).jam_threshold == 30
