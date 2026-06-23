"""test_canary_val_score.py -- 综合分（ux + val 正确率）晋升判定单测
=====================================================================
覆盖 canary.check_and_decide 的综合分改造：

  综合分 = (1 - val_weight) * ux_avg + val_weight * (val_acc * 10)

用例：
  1. 综合分按 0.5/0.5 算对（main/staging 各有 ux + .val_scores.json）。
  2. staging ux 更高但 val 更低 → 综合分被 val 拉到 main 之下 → discard（rejected）。
     同一组 ux 在 val_weight=0（纯 ux）下应当 promoted——证明是 val 真把它拉下马。
  3. 无 .val_scores.json（或缺该 sha 条目）→ 退回纯 ux，行为同未启用 val。
  4. .val_scores.json 进入 gitignore 模板。
"""
from __future__ import annotations

import json
from pathlib import Path

from xskill import canary
from xskill.canary import CanaryConfig
from xskill.skill.git import run_git


# ──────────────────────────────────────────────────────
# Helpers（与 test_canary.py 同风格）
# ──────────────────────────────────────────────────────

def _init_repo(path: Path) -> Path:
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


def _setup_staged(tmp_path) -> Path:
    """建一个 main + staging(领先一个 commit) 的 skill 仓。"""
    sd = _init_repo(tmp_path / "skill")
    initial = canary.main_sha(sd)
    _commit(sd, "v2", "update")
    canary.route_main_history_to_staging(sd, initial)
    return sd


def _fill_ux(sd: Path, *, main_score: float, staging_score: float, n: int):
    """两侧各灌 n 条单桶 ux 分（user_model 留空 → 单桶路径）。"""
    m_sha = canary.main_sha(sd)
    s_sha = canary.staging_sha(sd)
    for i in range(n):
        canary.append_ux_score(sd, traj_id=f"m{i}", skill_name="s", side="main",
                               commit_sha=m_sha, score=main_score, reasons="")
        canary.append_ux_score(sd, traj_id=f"s{i}", skill_name="s", side="staging",
                               commit_sha=s_sha, score=staging_score, reasons="")


def _write_val(sd: Path, *, main_acc: float, staging_acc: float, n: int = 10):
    """按 main/staging 各自的 commit sha 写 .val_scores.json。"""
    m_sha = canary.main_sha(sd)
    s_sha = canary.staging_sha(sd)
    p = sd / canary.VAL_SCORES_FILENAME
    p.write_text(json.dumps({
        m_sha: {"acc": main_acc, "n": n},
        s_sha: {"acc": staging_acc, "n": n},
    }), encoding="utf-8")


# ──────────────────────────────────────────────────────
# 1) 综合分算术正确
# ──────────────────────────────────────────────────────

class TestCompositeArithmetic:
    def test_composite_helper(self):
        # ux=6, val_acc=0.7 → val_norm=7 ; w=0.5 → 0.5*6 + 0.5*7 = 6.5
        comp, val_norm = canary._composite_score(6.0, 0.7, 0.5)
        assert val_norm == 7.0
        assert abs(comp - 6.5) < 1e-9

    def test_composite_missing_val_falls_back_to_ux(self):
        comp, val_norm = canary._composite_score(8.0, None, 0.5)
        assert val_norm is None
        assert comp == 8.0

    def test_composite_zero_weight_is_pure_ux(self):
        comp, val_norm = canary._composite_score(8.0, 0.1, 0.0)
        assert val_norm is None
        assert comp == 8.0

    def test_summary_reports_composite_and_breakdown(self, tmp_path):
        sd = _setup_staged(tmp_path)
        cfg = CanaryConfig(min_samples=3, val_weight=0.5)
        # main ux=5 val=0.6 → 0.5*5 + 0.5*6 = 5.5
        # staging ux=7 val=0.9 → 0.5*7 + 0.5*9 = 8.0  (staging wins → promoted)
        _fill_ux(sd, main_score=5, staging_score=7, n=3)
        _write_val(sd, main_acc=0.6, staging_acc=0.9)
        res = canary.check_and_decide(sd, cfg)
        assert res["action"] == "promoted"
        assert abs(res["main_avg"] - 5.5) < 1e-6
        assert abs(res["staging_avg"] - 8.0) < 1e-6
        assert res["main_ux"] == 5.0
        assert res["staging_ux"] == 7.0
        assert res["main_val_acc"] == 0.6
        assert res["staging_val_acc"] == 0.9
        assert res["val_weight"] == 0.5


# ──────────────────────────────────────────────────────
# 2) staging ux 高但 val 低 → 被综合分拉下马
# ──────────────────────────────────────────────────────

class TestValPullsStagingDown:
    def test_high_ux_low_val_staging_rejected(self, tmp_path):
        sd = _setup_staged(tmp_path)
        cfg = CanaryConfig(min_samples=3, val_weight=0.5)
        # staging ux 明显更高(9 vs 6)，纯 ux 必 promote；
        # 但 staging val 很差(0.2 → val_norm=2)，main val 不错(0.8 → val_norm=8)。
        #   main 综合分     = 0.5*6 + 0.5*8 = 7.0
        #   staging 综合分 = 0.5*9 + 0.5*2 = 5.5  < 7.0 → rejected
        _fill_ux(sd, main_score=6, staging_score=9, n=3)
        _write_val(sd, main_acc=0.8, staging_acc=0.2)
        res = canary.check_and_decide(sd, cfg)
        assert res["action"] == "rejected", res
        assert abs(res["main_avg"] - 7.0) < 1e-6
        assert abs(res["staging_avg"] - 5.5) < 1e-6
        # staging 真的被丢弃，main 保持 v1
        assert not canary.has_staging(sd)
        assert canary.read_skill_on_branch(sd, "main").strip() == "v1"

    def test_same_ux_pure_ux_would_promote(self, tmp_path):
        """同一组 ux（staging 9 > main 6），val_weight=0（纯 ux）下应 promoted——
        证明上一个用例的 reject 是 val 拉的，不是 ux 本身的问题。"""
        sd = _setup_staged(tmp_path)
        cfg = CanaryConfig(min_samples=3, val_weight=0.0)
        _fill_ux(sd, main_score=6, staging_score=9, n=3)
        _write_val(sd, main_acc=0.8, staging_acc=0.2)  # 写了但 weight=0 应被忽略
        res = canary.check_and_decide(sd, cfg)
        assert res["action"] == "promoted", res
        assert res["main_avg"] == 6.0
        assert res["staging_avg"] == 9.0


# ──────────────────────────────────────────────────────
# 3) 无 .val_scores.json → 退回纯 ux，行为不变
# ──────────────────────────────────────────────────────

class TestFallbackNoValFile:
    # 这些用例验证"缺 val 退回纯 ux"的旧行为；决策触发式 val（val_block 默认
    # True）会把缺 val 改成挂起等 val，故这里显式 val_block=False 走旧回退路径。
    def test_no_val_file_promotes_on_ux(self, tmp_path):
        sd = _setup_staged(tmp_path)
        cfg = CanaryConfig(min_samples=3, val_weight=0.5, val_block=False)
        _fill_ux(sd, main_score=3, staging_score=9, n=3)
        # 不写 .val_scores.json
        res = canary.check_and_decide(sd, cfg)
        assert res["action"] == "promoted"
        # 综合分 == 纯 ux（无 val 退回）
        assert res["main_avg"] == 3.0
        assert res["staging_avg"] == 9.0
        assert res["main_val_acc"] is None
        assert res["staging_val_acc"] is None

    def test_no_val_file_rejects_on_ux(self, tmp_path):
        sd = _setup_staged(tmp_path)
        cfg = CanaryConfig(min_samples=3, val_weight=0.5, val_block=False)
        _fill_ux(sd, main_score=9, staging_score=2, n=3)
        res = canary.check_and_decide(sd, cfg)
        assert res["action"] == "rejected"
        assert res["main_avg"] == 9.0
        assert res["staging_avg"] == 2.0

    def test_missing_sha_entry_side_falls_back(self, tmp_path):
        """.val_scores.json 存在但只含 main 的 sha（staging sha 换了读不到）→
        staging 侧退回纯 ux，main 侧用 val。"""
        sd = _setup_staged(tmp_path)
        cfg = CanaryConfig(min_samples=3, val_weight=0.5, val_block=False)
        _fill_ux(sd, main_score=6, staging_score=9, n=3)
        m_sha = canary.main_sha(sd)
        # 只写 main 的 val，staging sha 缺失
        (sd / canary.VAL_SCORES_FILENAME).write_text(
            json.dumps({m_sha: {"acc": 1.0, "n": 10}}), encoding="utf-8")
        res = canary.check_and_decide(sd, cfg)
        # main 综合 = 0.5*6 + 0.5*10 = 8.0 ; staging 退回纯 ux = 9.0 → staging 胜
        assert res["action"] == "promoted", res
        assert abs(res["main_avg"] - 8.0) < 1e-6
        assert res["staging_avg"] == 9.0
        assert res["main_val_acc"] == 1.0
        assert res["staging_val_acc"] is None

    def test_corrupt_val_file_falls_back(self, tmp_path):
        sd = _setup_staged(tmp_path)
        cfg = CanaryConfig(min_samples=3, val_weight=0.5, val_block=False)
        _fill_ux(sd, main_score=3, staging_score=9, n=3)
        (sd / canary.VAL_SCORES_FILENAME).write_text("{not json", encoding="utf-8")
        res = canary.check_and_decide(sd, cfg)
        assert res["action"] == "promoted"
        assert res["main_avg"] == 3.0
        assert res["staging_avg"] == 9.0


# ──────────────────────────────────────────────────────
# 4) val_scores.json 进 gitignore
# ──────────────────────────────────────────────────────

class TestGitignoreVal:
    def test_template_includes_val(self, tmp_path):
        sd = tmp_path / "skill"
        sd.mkdir()
        canary.ensure_gitignore(sd)
        gi = (sd / ".gitignore").read_text()
        assert ".val_scores.json" in gi
        assert ".ux_scores.jsonl" in gi

    def test_appends_val_when_only_ux_present(self, tmp_path):
        sd = tmp_path / "skill"
        sd.mkdir()
        (sd / ".gitignore").write_text(".ux_scores.jsonl\n.lock\n", encoding="utf-8")
        canary.ensure_gitignore(sd)
        gi = (sd / ".gitignore").read_text()
        assert ".val_scores.json" in gi
