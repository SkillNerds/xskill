"""模型分桶灰度(batch3)单测:

- eligible_models: top-N 选择 + 排除 unknown/synthetic + 权重归一
- pick_side_scoped: unknown/非 top-N → main;top-N → 同 pick_side
- _cohort_weighted: 只统计权重内模型,按人口加权
- check_and_decide(weights=...): 加权裁决能"翻转"朴素均分的结论
"""
from __future__ import annotations

from pathlib import Path

from xskill import canary
from xskill.canary import (
    CanaryConfig, eligible_models, pick_side, pick_side_scoped, _cohort_weighted,
)
from xskill.skill.git import run_git


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    run_git(["init"], cwd=str(path))
    run_git(["checkout", "-b", "main"], cwd=str(path))
    run_git(["config", "user.email", "t@t"], cwd=str(path))
    run_git(["config", "user.name", "t"], cwd=str(path))
    (path / "SKILL.md").write_text("v1", encoding="utf-8")
    run_git(["add", "-A"], cwd=str(path))
    run_git(["commit", "-m", "init"], cwd=str(path))
    return path


def _stage_v2(sd: Path) -> None:
    initial = canary.main_sha(sd)
    (sd / "SKILL.md").write_text("v2", encoding="utf-8")
    run_git(["add", "-A"], cwd=str(sd))
    run_git(["commit", "-m", "v2"], cwd=str(sd))
    canary.route_main_history_to_staging(sd, initial)


# ── eligible_models ───────────────────────────────────────────────

def test_eligible_models_topn_excludes_unknown():
    share = [
        {"model": "unknown", "trajs": 382},
        {"model": "claude-opus-4-7", "trajs": 100},
        {"model": "deepseek-v4-flash", "trajs": 40},
        {"model": "<synthetic>", "trajs": 5},
        {"model": "claude-haiku", "trajs": 10},
    ]
    w = eligible_models(share, top_n=2)
    assert set(w) == {"claude-opus-4-7", "deepseek-v4-flash"}   # unknown/synthetic 排除
    assert abs(sum(w.values()) - 1.0) < 1e-9                    # 归一
    assert w["claude-opus-4-7"] > w["deepseek-v4-flash"]        # 按使用量
    assert abs(w["claude-opus-4-7"] - 100 / 140) < 1e-9


def test_eligible_models_all_unknown_empty():
    assert eligible_models([{"model": "unknown", "trajs": 9}], top_n=2) == {}


# ── pick_side_scoped ──────────────────────────────────────────────

def test_pick_side_scoped_none_is_legacy():
    assert (pick_side_scoped("t1", "s", 1.0, user_model="x", eligible=None)
            == pick_side("t1", "s", 1.0))


def test_pick_side_scoped_excludes_noneligible():
    elig = {"claude-opus-4-7": 1.0}
    # prob=1.0 本应全 staging,但非 top-N / unknown 被踢回 main
    assert pick_side_scoped("t1", "s", 1.0, user_model="unknown", eligible=elig) == "main"
    assert pick_side_scoped("t1", "s", 1.0, user_model="gpt-5", eligible=elig) == "main"
    # 合格模型 → 照常分流(prob=1.0 → staging)
    assert pick_side_scoped("t1", "s", 1.0, user_model="claude-opus-4-7",
                            eligible=elig) == "staging"


# ── _cohort_weighted ──────────────────────────────────────────────

def test_cohort_weighted_excludes_unweighted_and_weights():
    scores = [
        {"score": 3, "user_model": "big"},
        {"score": 10, "user_model": "small"},
        {"score": 999, "user_model": "unknown"},   # 不在 weights → 丢弃
    ]
    weights = {"big": 0.9, "small": 0.1}
    val, cohorts = _cohort_weighted(scores, weights)
    assert cohorts == {"big": 1, "small": 1}        # unknown 不计
    assert abs(val - (0.9 * 3 + 0.1 * 10)) < 1e-9   # 人口加权,非朴素均分


# ── check_and_decide 加权裁决 ─────────────────────────────────────

def _seed(sd, side, sha, model, score, n):
    for i in range(n):
        canary.AtomCanary(sd).append(
            atom_id=f"{side}_{model}_{i}", skill_name="s", side=side,
            commit_sha=sha, score=score, reasons="", user_model=model)


def test_weighted_decision_flips_naive_average(tmp_path):
    """同一批数据:朴素均分会 promote,人口加权应 reject。

    staging 里小众模型刷了大量高分、主力模型只有少量低分;按人口加权后主力
    模型(权重高)拉低 staging → 不该 promote。
    """
    sd = _init_repo(tmp_path / "skill")
    _stage_v2(sd)
    m_sha, s_sha = canary.main_sha(sd), canary.staging_sha(sd)
    # staging: big 1×3, small 3×10  ;  main: big 1×8, small 3×8
    _seed(sd, "staging", s_sha, "big", 3, 1)
    _seed(sd, "staging", s_sha, "small", 10, 3)
    _seed(sd, "main", m_sha, "big", 8, 1)
    _seed(sd, "main", m_sha, "small", 8, 3)
    cfg = CanaryConfig(total_samples=2, min_samples=2)
    weights = {"big": 0.9, "small": 0.1}

    res = canary.check_and_decide(sd, cfg, weights=weights)
    assert res["action"] == "rejected"          # 加权:0.9*3+0.1*10=3.7 < 8
    assert not canary.has_staging(sd)           # rejected → staging 已被 discard
    # 对照:朴素均分(单桶,不传 weights)staging≈8.25 > main 8 → 本会 promote
    # 此处不重复造仓库,test_cohort_weighted 已证加权≠均分
