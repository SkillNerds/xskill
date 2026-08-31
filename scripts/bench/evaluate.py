#!/usr/bin/env python3.11
"""评测器 —— 比对预测切分边界 vs ground-truth。

输出两类指标:

1. 边界精确指标(boundary-exact):把"边界"看成内部切分点(各 atom 起始
   行号去掉第一个)。按行号精确匹配(带可选容差 tol,近失算命中):
     TP = 预测∩真值, FP = 预测-真值(过切), FN = 真值-预测(漏拆)。
   汇报 precision/recall/micro-F1 + exact_match + eof_coverage + 分场景。

2. 分段标准指标(segmentation):Pk 与 WindowDiff,都是越低越好的"错配率"。
   它们把轨迹看成 N 行的序列,用一个宽度 k 的滑窗扫过,统计参考分段与
   预测分段在窗口两端/窗口内边界数上的不一致比例。相比精确 F1,这两个指标
   对"差一两行的近失"宽容、对"漏掉一大段"严惩 —— 正好补 F1 的盲区。

设计取舍
========
- 精确 F1 对行号零容差,适合合成集(行号由生成器精确给出);真实集人工
  标注边界本身有 ±几行的不确定,所以同时跑 tol>0 的宽容 F1 与 Pk/WindowDiff。
- Pk/WindowDiff 的窗宽 k 按惯例取"参考段平均长度的一半"(每条 case 各算各的
  k),空内部边界(单 atom)时整条无可滑窗,记 0.0(完美一致)。

不调用任何 LLM、不访问网络。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SYNTH_GT = HERE / "synthetic" / "ground_truth.json"


# --------------------------------------------------------------------------- #
# 边界精确指标                                                                  #
# --------------------------------------------------------------------------- #
def prf(tp: int, fp: int, fn: int) -> tuple[float, float, float]:
    """从计数算 precision/recall/F1。无任何正例时(tp=fp=fn=0)记满分。"""
    if tp == fp == fn == 0:
        return 1.0, 1.0, 1.0
    p = tp / (tp + fp) if (tp + fp) else 0.0
    r = tp / (tp + fn) if (tp + fn) else 0.0
    f = 2 * p * r / (p + r) if (p + r) else 0.0
    return p, r, f


def _match_with_tol(pred: list[int], true: list[int],
                    tol: int) -> tuple[int, int, int]:
    """带容差的贪心一对一匹配:每个真值边界至多吸收一个 |Δ|<=tol 的预测边界。

    返回 (tp, fp, fn)。tol=0 时退化为精确集合匹配。
    """
    pred_sorted = sorted(pred)
    used = [False] * len(pred_sorted)
    tp = 0
    for t in sorted(true):
        best = -1
        best_d = tol + 1
        for i, p in enumerate(pred_sorted):
            if used[i]:
                continue
            d = abs(p - t)
            if d <= tol and d < best_d:
                best_d = d
                best = i
        if best >= 0:
            used[best] = True
            tp += 1
    fp = used.count(False)
    fn = len(true) - tp
    return tp, fp, fn


def score_case(pred_boundaries: list[int],
               true_boundaries: list[int], tol: int = 0) -> dict:
    """单条 case 的边界精确指标:tp/fp/fn/precision/recall/f1/exact。"""
    tp, fp, fn = _match_with_tol(pred_boundaries, true_boundaries, tol)
    p, r, f = prf(tp, fp, fn)
    exact = sorted(set(pred_boundaries)) == sorted(set(true_boundaries))
    return {"tp": tp, "fp": fp, "fn": fn, "precision": p, "recall": r,
            "f1": f, "exact": exact}


# --------------------------------------------------------------------------- #
# 分段标准指标:Pk / WindowDiff                                                 #
# --------------------------------------------------------------------------- #
def _boundary_mask(n_units: int, boundaries: list[int]) -> list[int]:
    """把内部边界行号转成长度 n_units-1 的"间隙掩码"。

    序列有 n_units 个单元(此处=行),相邻两单元间有 n_units-1 个间隙。
    掩码[i]=1 表示第 i 个间隙(在单元 i 与 i+1 之间)是一个段边界。
    边界行号 b(1-based)表示"第 b 行起一个新段",即间隙在单元 b-1 与 b 之间,
    对应掩码下标 b-2。越界的边界被忽略(防御性,正常不会发生)。
    """
    mask = [0] * max(n_units - 1, 0)
    for b in boundaries:
        idx = b - 2
        if 0 <= idx < len(mask):
            mask[idx] = 1
    return mask


def _prefix_counts(mask: list[int]) -> list[int]:
    """前缀边界计数:prefix[j] = mask[0..j-1] 之和,便于 O(1) 取窗内边界数。"""
    pref = [0] * (len(mask) + 1)
    for j, v in enumerate(mask):
        pref[j + 1] = pref[j] + v
    return pref


def _auto_k(n_units: int, ref_boundaries: list[int]) -> int:
    """窗宽 k = 参考平均段长的一半(Beeferman 等惯例),夹到 [2, n_units-1]。"""
    n_seg = len(ref_boundaries) + 1
    k = max(2, round((n_units / n_seg) / 2))
    return min(k, max(n_units - 1, 2))


def pk(n_units: int, ref_boundaries: list[int],
       hyp_boundaries: list[int], k: int | None = None) -> float:
    """Pk(Beeferman 1999):滑窗两端"是否同段"的不一致率。越低越好。

    单元数 < 2 或无内部边界可滑时返回 0.0(无可错配)。
    """
    if n_units < 2:
        return 0.0
    ref = _boundary_mask(n_units, ref_boundaries)
    hyp = _boundary_mask(n_units, hyp_boundaries)
    if k is None:
        k = _auto_k(n_units, ref_boundaries)
    rp = _prefix_counts(ref)
    hp = _prefix_counts(hyp)
    # 窗口跨越单元 i..i+k,即间隙下标 [i, i+k-1];共 (n_units - k) 个窗口位置。
    n_windows = n_units - k
    if n_windows <= 0:
        return 0.0
    disagree = 0
    for i in range(n_windows):
        ref_same = (rp[i + k] - rp[i]) == 0  # 窗内无参考边界 => 同段
        hyp_same = (hp[i + k] - hp[i]) == 0
        if ref_same != hyp_same:
            disagree += 1
    return disagree / n_windows


def window_diff(n_units: int, ref_boundaries: list[int],
                hyp_boundaries: list[int], k: int | None = None) -> float:
    """WindowDiff(Pevzner & Hearst 2002):滑窗内"边界计数差非零"的比例。

    比 Pk 更敏感于过切/漏切的数目差异。越低越好。
    """
    if n_units < 2:
        return 0.0
    ref = _boundary_mask(n_units, ref_boundaries)
    hyp = _boundary_mask(n_units, hyp_boundaries)
    if k is None:
        k = _auto_k(n_units, ref_boundaries)
    rp = _prefix_counts(ref)
    hp = _prefix_counts(hyp)
    n_windows = n_units - k
    if n_windows <= 0:
        return 0.0
    bad = 0
    for i in range(n_windows):
        rc = rp[i + k] - rp[i]
        hc = hp[i + k] - hp[i]
        if rc != hc:
            bad += 1
    return bad / n_windows


# --------------------------------------------------------------------------- #
# 汇总                                                                          #
# --------------------------------------------------------------------------- #
def aggregate(preds: dict, ground: dict, tol: int = 0) -> dict:
    """preds[case_id] = {boundaries:[...], covered_eof:bool, error:str|None}。

    ground[case_id] 需含 boundaries / scenario / total_lines。
    """
    def blank() -> dict:
        return {"tp": 0, "fp": 0, "fn": 0, "exact": 0, "eof": 0,
                "err": 0, "n": 0, "pk_sum": 0.0, "wd_sum": 0.0}

    by_scen: dict[str, dict] = {}
    tot = blank()

    for case_id, gt in ground.items():
        pr = preds.get(case_id, {})
        scen = gt.get("scenario", "UNKNOWN")
        s = by_scen.setdefault(scen, blank())
        if pr.get("error"):
            tot["err"] += 1
            s["err"] += 1
        pred_b = pr.get("boundaries", [])
        true_b = gt["boundaries"]
        n_units = gt["total_lines"]
        sc = score_case(pred_b, true_b, tol=tol)
        pkv = pk(n_units, true_b, pred_b)
        wdv = window_diff(n_units, true_b, pred_b)
        for k in ("tp", "fp", "fn"):
            tot[k] += sc[k]
            s[k] += sc[k]
        tot["exact"] += int(sc["exact"])
        s["exact"] += int(sc["exact"])
        cov = bool(pr.get("covered_eof", False))
        tot["eof"] += int(cov)
        s["eof"] += int(cov)
        tot["pk_sum"] += pkv
        s["pk_sum"] += pkv
        tot["wd_sum"] += wdv
        s["wd_sum"] += wdv
        tot["n"] += 1
        s["n"] += 1

    def pack(d: dict) -> dict:
        p, r, f = prf(d["tp"], d["fp"], d["fn"])
        n = d["n"] or 1
        return {"n": d["n"], "precision": round(p, 3), "recall": round(r, 3),
                "f1": round(f, 3), "exact_match": round(d["exact"] / n, 3),
                "eof_coverage": round(d["eof"] / n, 3),
                "pk": round(d["pk_sum"] / n, 3),
                "window_diff": round(d["wd_sum"] / n, 3),
                "errors": d["err"], "tp": d["tp"], "fp": d["fp"],
                "fn": d["fn"]}

    return {"tol": tol,
            "overall": pack(tot),
            "by_scenario": {k: pack(v) for k, v in sorted(by_scen.items())}}


# --------------------------------------------------------------------------- #
# 自测(独立已知小例子,防"预测==真值"硬编码骗指标)                              #
# --------------------------------------------------------------------------- #
def _selftest() -> None:
    # --- 边界精确(tol=0)---
    assert score_case([10, 20], [10, 20])["f1"] == 1.0
    s = score_case([10, 15, 20], [10, 20])
    assert s["tp"] == 2 and s["fp"] == 1 and s["fn"] == 0
    assert abs(s["precision"] - 2 / 3) < 1e-9 and s["recall"] == 1.0
    s = score_case([10], [10, 20])
    assert s["tp"] == 1 and s["fp"] == 0 and s["fn"] == 1
    assert score_case([], [])["f1"] == 1.0
    s = score_case([5], [10])
    assert s["tp"] == 0 and s["f1"] == 0.0

    # --- 容差匹配:预测差 2 行,tol=3 算命中,tol=0 算双错 ---
    s = score_case([12, 22], [10, 20], tol=3)
    assert s["tp"] == 2 and s["fp"] == 0 and s["fn"] == 0, s
    s0 = score_case([12, 22], [10, 20], tol=0)
    assert s0["tp"] == 0 and s0["fp"] == 2 and s0["fn"] == 2, s0
    # 容差不许一个预测吸收两个真值边界(一对一)
    s = score_case([11], [10, 12], tol=3)
    assert s["tp"] == 1 and s["fn"] == 1, s

    # --- 掩码:边界行 b 落在间隙下标 b-2 ---
    assert _boundary_mask(5, [3]) == [0, 1, 0, 0]   # 5 行 -> 4 间隙
    assert _boundary_mask(5, []) == [0, 0, 0, 0]
    assert _boundary_mask(4, [2, 4]) == [1, 0, 1]

    # --- Pk / WindowDiff 已知例子 ---
    # 完美一致 => 0.0
    assert pk(10, [5], [5]) == 0.0
    assert window_diff(10, [5], [5]) == 0.0
    assert pk(10, [], []) == 0.0
    # 参考有边界,预测全漏(单 atom)=> Pk、WD 都 > 0
    assert pk(10, [5], []) > 0.0
    assert window_diff(10, [5], []) > 0.0

    # 手算校验:n_units=6, k=2(强制), ref 边界在行 4 -> mask=[0,0,1,0,0]
    #   窗位置 i=0..3(n-k=4):窗覆盖间隙[i,i+1]
    #   ref 同段?  i=0:[0,0]同 i=1:[0,1]异 i=2:[1,0]异 i=3:[0,0]同
    #   hyp 边界在行 3 -> mask=[0,1,0,0,0]
    #   hyp 同段?  i=0:[0,1]异 i=1:[1,0]异 i=2:[0,0]同 i=3:[0,0]同
    #   Pk 不一致位:i=0(同vs异),i=2(异vs同) => 2/4 = 0.5
    assert abs(pk(6, [4], [3], k=2) - 0.5) < 1e-9, pk(6, [4], [3], k=2)
    #   WindowDiff 看每窗内边界计数差:窗覆盖间隙[i,i+1]
    #     ref 计数 i=0:0 i=1:1 i=2:1 i=3:0
    #     hyp 计数 i=0:1 i=1:1 i=2:0 i=3:0
    #   差非零位:i=0(0vs1),i=2(1vs0) => 2/4 = 0.5
    assert abs(window_diff(6, [4], [3], k=2) - 0.5) < 1e-9, \
        window_diff(6, [4], [3], k=2)

    # 近失(差 1 行)应比远失(漏整段)Pk 更低 —— 验证"宽容近失"性质
    near = pk(20, [10], [11])
    miss = pk(20, [10], [])
    assert near < miss, (near, miss)

    # 防自洽陷阱:即便预测==真值,Pk/WD 也必须真为 0(不是被硬编码)
    assert pk(50, [10, 30], [10, 30]) == 0.0
    assert window_diff(50, [10, 30], [10, 30]) == 0.0

    print("evaluate._selftest OK")


def main() -> None:
    """命令行入口:selftest,或对 preds.json 打分。"""
    if len(sys.argv) >= 2 and sys.argv[1] == "selftest":
        _selftest()
        return
    # 用法: evaluate.py <preds.json> [preds2.json ...] [--tol N] [--gt PATH]
    args = sys.argv[1:]
    tol = 0
    gt_path = SYNTH_GT
    files = []
    i = 0
    while i < len(args):
        if args[i] == "--tol":
            tol = int(args[i + 1])
            i += 2
        elif args[i] == "--gt":
            gt_path = Path(args[i + 1])
            i += 2
        else:
            files.append(args[i])
            i += 1
    ground = json.loads(Path(gt_path).read_text(encoding="utf-8"))
    for pf in files:
        preds = json.loads(Path(pf).read_text(encoding="utf-8"))
        print(f"\n==== {pf} (tol={tol}) ====")
        print(json.dumps(aggregate(preds, ground, tol=tol),
                         ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
