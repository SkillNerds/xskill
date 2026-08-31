"""tests/test_bench_evaluate.py — 轨迹拆分 benchmark 评测器单测。

固化 scripts/bench/evaluate.py 的 Pk / WindowDiff / 边界 P-R-F1 计算正确性,
让 ``make test`` 每次都验证评测器本身没退化(评测器是度量回路的传感器,
传感器坏了整条回路不可信)。

Pk / WindowDiff 用**独立手算的小例子**钉死,并设防"预测==真值硬编码骗分"
的闸门 —— 即便预测与真值相同,也必须真算出 0 而非被写死。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

_BENCH = Path(__file__).resolve().parent.parent / "scripts" / "bench"
_spec = importlib.util.spec_from_file_location(
    "bench_evaluate", _BENCH / "evaluate.py")
ev = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ev)


# --------------------------------------------------------------------------- #
# 边界精确 P/R/F1                                                              #
# --------------------------------------------------------------------------- #
def test_boundary_perfect():
    s = ev.score_case([10, 20], [10, 20])
    assert s["f1"] == 1.0 and s["exact"] is True


def test_boundary_oversplit():
    s = ev.score_case([10, 15, 20], [10, 20])
    assert (s["tp"], s["fp"], s["fn"]) == (2, 1, 0)
    assert abs(s["precision"] - 2 / 3) < 1e-9 and s["recall"] == 1.0


def test_boundary_undersplit():
    s = ev.score_case([10], [10, 20])
    assert (s["tp"], s["fp"], s["fn"]) == (1, 0, 1)
    assert s["precision"] == 1.0 and abs(s["recall"] - 0.5) < 1e-9


def test_boundary_empty_both_is_perfect():
    assert ev.score_case([], [])["f1"] == 1.0


def test_tolerance_matches_near_miss():
    # 预测各差 2 行:tol=3 命中,tol=0 双错
    s = ev.score_case([12, 22], [10, 20], tol=3)
    assert (s["tp"], s["fp"], s["fn"]) == (2, 0, 0)
    s0 = ev.score_case([12, 22], [10, 20], tol=0)
    assert (s0["tp"], s0["fp"], s0["fn"]) == (0, 2, 2)


def test_tolerance_is_one_to_one():
    # 一个预测不许吸收两个真值边界
    s = ev.score_case([11], [10, 12], tol=3)
    assert (s["tp"], s["fn"]) == (1, 1)


# --------------------------------------------------------------------------- #
# 边界行 -> 间隙掩码                                                            #
# --------------------------------------------------------------------------- #
def test_boundary_mask_indexing():
    assert ev._boundary_mask(5, [3]) == [0, 1, 0, 0]
    assert ev._boundary_mask(5, []) == [0, 0, 0, 0]
    assert ev._boundary_mask(4, [2, 4]) == [1, 0, 1]


# --------------------------------------------------------------------------- #
# Pk / WindowDiff(独立手算例子)                                               #
# --------------------------------------------------------------------------- #
def test_pk_wd_perfect_is_zero():
    assert ev.pk(10, [5], [5]) == 0.0
    assert ev.window_diff(10, [5], [5]) == 0.0
    assert ev.pk(10, [], []) == 0.0
    assert ev.window_diff(10, [], []) == 0.0


def test_pk_handworked_example():
    # n=6, k=2, ref 边界@4 -> mask[0,0,1,0,0]; hyp 边界@3 -> mask[0,1,0,0,0]
    # 4 个窗:ref 同段? 同/异/异/同 ; hyp 同段? 异/异/同/同
    # 不一致位 i=0,i=2 => 2/4 = 0.5
    assert abs(ev.pk(6, [4], [3], k=2) - 0.5) < 1e-9


def test_window_diff_handworked_example():
    # 同上;每窗边界计数 ref[0,1,1,0] hyp[1,1,0,0] -> 差非零位 i=0,i=2 => 0.5
    assert abs(ev.window_diff(6, [4], [3], k=2) - 0.5) < 1e-9


def test_pk_misses_are_penalized():
    # 参考有边界、预测全漏(退化成单 atom)=> Pk、WD 都 > 0
    assert ev.pk(10, [5], []) > 0.0
    assert ev.window_diff(10, [5], []) > 0.0


def test_near_miss_beats_full_miss():
    # 差 1 行的近失,应比漏整段的 Pk 更低(宽容近失、严惩漏段)
    assert ev.pk(20, [10], [11]) < ev.pk(20, [10], [])


def test_no_hardcoded_self_match():
    # 防"预测==真值硬编码骗分":相同输入也必须真算出 0
    assert ev.pk(50, [10, 30], [10, 30]) == 0.0
    assert ev.window_diff(50, [10, 30], [10, 30]) == 0.0
    # 而错配必须 > 0
    assert ev.pk(50, [10, 30], [10, 40]) > 0.0


# --------------------------------------------------------------------------- #
# 汇总:Pk/WindowDiff 进入 overall                                             #
# --------------------------------------------------------------------------- #
def test_aggregate_emits_segmentation_metrics():
    ground = {"a": {"boundaries": [10], "scenario": "X", "total_lines": 20}}
    preds = {"a": {"boundaries": [10], "covered_eof": True, "error": None}}
    out = ev.aggregate(preds, ground, tol=0)
    o = out["overall"]
    assert o["f1"] == 1.0 and o["pk"] == 0.0 and o["window_diff"] == 0.0
    assert o["eof_coverage"] == 1.0


def test_aggregate_scenarios_do_not_inherit_previous_totals():
    ground = {
        "a": {"boundaries": [10], "scenario": "FIRST", "total_lines": 20},
        "b": {"boundaries": [8], "scenario": "SECOND", "total_lines": 20},
    }
    preds = {
        "a": {"boundaries": [10], "covered_eof": True, "error": None},
        "b": {"boundaries": [], "covered_eof": False, "error": "failed"},
    }

    scenarios = ev.aggregate(preds, ground, tol=0)["by_scenario"]

    assert scenarios["FIRST"] == {
        "n": 1,
        "precision": 1.0,
        "recall": 1.0,
        "f1": 1.0,
        "exact_match": 1.0,
        "eof_coverage": 1.0,
        "pk": 0.0,
        "window_diff": 0.0,
        "errors": 0,
        "tp": 1,
        "fp": 0,
        "fn": 0,
    }
    assert scenarios["SECOND"]["n"] == 1
    assert scenarios["SECOND"]["tp"] == 0
    assert scenarios["SECOND"]["fn"] == 1
    assert scenarios["SECOND"]["errors"] == 1
