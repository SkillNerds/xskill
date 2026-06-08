#!/usr/bin/env python3
"""诊断 TaskAgent 拆分(split)损失 —— 只读，不改任何状态。

排查弱模型(function-calling 不稳)导致的两类损失：

  ① 硬中止(hard_abort)：split 时 agent.run() 抛 Non-retryable(JSON 坏掉
     /工具名幻觉) → 状态被兜成 error/filtered，是终态不再自动重试。
     内容没丢(没存任何 atom，last_offset 没动)，可重置状态后续拆挽回。

  ② 静默漏拆(silent_gap)：split 算"成功"(split_done+)，但 last_offset
     覆盖原文比例偏低 / 大轨迹却 0 atom。续接点已推过头，普通续拆不回头
     补 → 内容被无声跳过。需清 atom 重拆或回退 last_offset 才能挽回。

用法：
    python3 scripts/diagnose_split_loss.py                 # 默认 ~/.xskill/registry.db
    python3 scripts/diagnose_split_loss.py --db /path/registry.db
    python3 scripts/diagnose_split_loss.py --cov-warn 0.8  # 覆盖率告警阈值
    python3 scripts/diagnose_split_loss.py --csv loss.csv  # 明细导出 CSV
"""
from __future__ import annotations

import argparse
import csv
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path

# split 已"完成"语义的状态：到了这些状态说明这条 traj 走过(或越过)了拆分。
DONE_STATES = {"split_done", "indexed", "meta_done", "clustering", "done"}
# 终态错误：硬中止落在这里。
ERROR_STATES = {"error", "filtered"}
# 这次弱模型 bug 在 error_msg 里的指纹(用于把 ① 里"确实是本 bug"挑出来)。
BUG_SIGNATURES = (
    "BadRequestError", "never closed", "decode function",
    "Function bash not found", "not found", "Expecting",
    "validation error", "valid list",
)


@dataclass
class TrajRow:
    wd_path: str
    filename: str
    status: str
    last_offset: int
    tasks_extracted: int
    error_msg: str
    total_lines: int        # 原文物理行数；-1 表示文件缺失
    coverage: float         # last_offset / total_lines，clamp 到 [0,1]
    category: str           # hard_abort / silent_gap / file_missing / healthy / pending

    @property
    def is_bug_signature(self) -> bool:
        msg = self.error_msg or ""
        return any(sig in msg for sig in BUG_SIGNATURES)


def _count_lines(path: Path) -> int:
    if not path.is_file():
        return -1
    with path.open("rb") as fh:
        return sum(1 for _ in fh)


def classify(row: sqlite3.Row, *, cov_warn: float) -> TrajRow:
    wd_path = row["wd_path"]
    filename = row["filename"]
    status = row["status"] or "discovered"
    last_offset = int(row["last_offset"] or 0)
    tasks = int(row["tasks_extracted"] or 0)
    err = row["error_msg"] or ""

    md = Path(wd_path) / filename
    total = _count_lines(md)
    if total <= 0:
        cov = 0.0
        cat = "file_missing" if total == -1 else "healthy"
    else:
        cov = min(last_offset, total) / total
        if status in ERROR_STATES:
            cat = "hard_abort"
        elif status in DONE_STATES:
            # 拆"完"了却覆盖不足 / 大文件 0 atom → 疑似静默漏拆
            big_enough = total >= 40   # 太短的轨迹本就可能 0 意图，不算
            if (cov < cov_warn) or (tasks == 0 and big_enough):
                cat = "silent_gap"
            else:
                cat = "healthy"
        else:
            cat = "pending"   # discovered/updated/splitting…还没定论
    # error 态但文件缺失也归 hard_abort（损失定性以状态为准）
    if status in ERROR_STATES and cat == "file_missing":
        cat = "hard_abort"
    return TrajRow(wd_path, filename, status, last_offset, tasks, err,
                   total, round(cov, 3), cat)


def load_rows(db_path: Path, *, cov_warn: float) -> list[TrajRow]:
    if not db_path.is_file():
        raise FileNotFoundError(f"registry.db 不存在: {db_path}")
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.execute(
            "SELECT w.path AS wd_path, t.filename, t.status, t.last_offset,"
            "       t.tasks_extracted, t.error_msg"
            "  FROM trajectories t JOIN watch_dirs w ON t.watch_dir_id = w.id"
        )
        return [classify(r, cov_warn=cov_warn) for r in cur.fetchall()]
    finally:
        conn.close()


def report(rows: list[TrajRow], *, cov_warn: float) -> None:
    total = len(rows)
    if total == 0:
        print("registry 里没有任何 trajectory 记录。")
        return

    buckets: dict[str, list[TrajRow]] = {}
    for r in rows:
        buckets.setdefault(r.category, []).append(r)

    hard = buckets.get("hard_abort", [])
    gap = buckets.get("silent_gap", [])
    bug_hard = [r for r in hard if r.is_bug_signature]
    impacted = len(hard) + len(gap)

    def pct(n: int) -> str:
        return f"{100 * n / total:5.1f}%"

    print("=" * 64)
    print(f"  TaskAgent 拆分损失诊断   总轨迹数 = {total}")
    print(f"  覆盖率告警阈值 cov_warn = {cov_warn:.0%}")
    print("=" * 64)
    print(f"  ① 硬中止 hard_abort  : {len(hard):4d}  ({pct(len(hard))})"
          f"   其中命中本 bug 指纹 {len(bug_hard)}")
    print(f"  ② 静默漏拆 silent_gap: {len(gap):4d}  ({pct(len(gap))})")
    print("  ─────────────────────────────────────")
    print(f"  受影响合计 impacted  : {impacted:4d}  ({pct(impacted)})")
    print(f"  健康 healthy         : {len(buckets.get('healthy', [])):4d}"
          f"  ({pct(len(buckets.get('healthy', [])))})")
    print(f"  未决 pending         : {len(buckets.get('pending', [])):4d}"
          f"  ({pct(len(buckets.get('pending', [])))})")
    miss = buckets.get("file_missing", [])
    if miss:
        print(f"  原文缺失 file_missing: {len(miss):4d}  ({pct(len(miss))})")
    print("=" * 64)

    def dump(title: str, items: list[TrajRow], limit: int = 20) -> None:
        if not items:
            return
        print(f"\n[{title}]  共 {len(items)} 条"
              + ("（截前 %d）" % limit if len(items) > limit else ""))
        for r in items[:limit]:
            tail = f"  err={r.error_msg[:60]!r}" if r.error_msg else ""
            print(f"  - {r.filename:40s} status={r.status:10s}"
                  f" cov={r.coverage:.0%} atoms={r.tasks_extracted}"
                  f" lines={r.total_lines}{tail}")

    dump("① 硬中止（可重置状态续拆挽回）", hard)
    dump("② 静默漏拆（需清 atom 重拆 / 回退 last_offset）", gap)


def write_csv(rows: list[TrajRow], out: Path) -> None:
    with out.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["category", "wd_path", "filename", "status",
                    "last_offset", "total_lines", "coverage",
                    "tasks_extracted", "is_bug_signature", "error_msg"])
        for r in rows:
            w.writerow([r.category, r.wd_path, r.filename, r.status,
                        r.last_offset, r.total_lines, r.coverage,
                        r.tasks_extracted, int(r.is_bug_signature),
                        (r.error_msg or "")[:300]])
    print(f"\n明细已写出: {out}")


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="TaskAgent 拆分损失诊断(只读)")
    ap.add_argument("--db", default=str(Path.home() / ".xskill" / "registry.db"),
                    help="registry.db 路径（默认 ~/.xskill/registry.db）")
    ap.add_argument("--cov-warn", type=float, default=0.8,
                    help="覆盖率低于此值的 split_done 轨迹判为静默漏拆（默认 0.8）")
    ap.add_argument("--csv", default=None, help="把全部明细导出到 CSV")
    args = ap.parse_args(argv)

    rows = load_rows(Path(args.db), cov_warn=args.cov_warn)
    report(rows, cov_warn=args.cov_warn)
    if args.csv:
        write_csv(rows, Path(args.csv))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
