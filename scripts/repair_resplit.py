#!/usr/bin/env python3
"""repair_resplit.py —— 触发"覆盖不全"轨迹的全量重拆，挽回历史静默漏拆。

背景：旧版 TaskAgent.run() 一次只拆一个窗，大轨迹标 split_done 后剩余窗
永不调度 → 后段内容静默漏拆。修复后 run() 会从 last_offset 续接点逐窗
拆到 EOF。本脚本把**续接点没到文件末尾**的历史轨迹状态翻回 ``updated``，
让服务重新派 split，新代码即从断点续拆补齐——已存 atom 不动，只补尾部。

⚠ 执行顺序（很重要）：
    1) 先部署修好的代码（task_agent.py 多窗循环 + 无 User 窗并入）；
    2) 重启 xskill 服务，让它加载新代码；
    3) 再跑本脚本 --apply。
   顺序错了（旧代码还在跑）只会又拆一个窗再标 done，白忙。

只读判定 + 单次 UPDATE，不删任何 atom、不碰轨迹原文。默认 dry-run。

用法：
    python3.11 scripts/repair_resplit.py                 # dry-run：列出待修轨迹
    python3.11 scripts/repair_resplit.py --apply         # 翻状态触发重拆
    python3.11 scripts/repair_resplit.py --db /path/registry.db --apply
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

# 这些状态属于"已走过(或越过)拆分"，覆盖不全就是漏拆受害者，需要重拆。
# discovered/updated 本就会被派 split，不必动；splitting 在途，别打断。
REPAIRABLE_STATES = {"split_done", "indexed", "meta_done", "clustering",
                     "done", "error", "filtered"}


def _count_lines(path: Path) -> int:
    if not path.is_file():
        return -1
    with path.open("rb") as fh:
        return sum(1 for _ in fh)


def find_targets(conn: sqlite3.Connection) -> list[dict]:
    """挑出续接点未到 EOF、且处于可修状态的轨迹。"""
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT t.id, w.path AS wd_path, t.filename, t.status,"
        "       t.last_offset, t.tasks_extracted"
        "  FROM trajectories t JOIN watch_dirs w ON t.watch_dir_id = w.id"
    ).fetchall()
    targets = []
    for r in rows:
        if (r["status"] or "") not in REPAIRABLE_STATES:
            continue
        total = _count_lines(Path(r["wd_path"]) / r["filename"])
        if total <= 0:
            continue  # 原文缺失/空 → 无从重拆，跳过
        last_off = int(r["last_offset"] or 0)
        # last_offset 是半开续接点；覆盖到 EOF 时 == total+1。<= total 即漏拆。
        if last_off <= total:
            targets.append({
                "id": r["id"], "filename": r["filename"], "status": r["status"],
                "last_offset": last_off, "total_lines": total,
                "tasks_extracted": int(r["tasks_extracted"] or 0),
                # 向下取整：避免 99.x% 被显示成 100% 误导（它确实还差几行没拆）
                "coverage": int(100 * min(last_off, total) / total),
            })
    return targets


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="触发覆盖不全轨迹的全量重拆(默认 dry-run)")
    ap.add_argument("--db", default=str(Path.home() / ".xskill" / "registry.db"),
                    help="registry.db 路径（默认 ~/.xskill/registry.db）")
    ap.add_argument("--apply", action="store_true", help="真正翻状态触发重拆")
    args = ap.parse_args(argv)

    db_path = Path(args.db)
    if not db_path.is_file():
        raise FileNotFoundError(f"registry.db 不存在: {db_path}")

    conn = sqlite3.connect(str(db_path), timeout=10)
    try:
        targets = find_targets(conn)
        print("=" * 64)
        print(f"  待重拆轨迹（续接点未到 EOF）= {len(targets)}")
        print("=" * 64)
        for t in sorted(targets, key=lambda x: x["coverage"]):
            print(f"  - {t['filename']:42s} status={t['status']:10s}"
                  f" cov={t['coverage']}%"
                  f" off={t['last_offset']}/{t['total_lines']}"
                  f" atoms={t['tasks_extracted']}")
        print("=" * 64)

        if not args.apply:
            print("（dry-run。确认 1)已部署修复代码 2)已重启服务 后，加 --apply 执行）")
            return 0

        ids = [t["id"] for t in targets]
        for tid in ids:
            conn.execute(
                "UPDATE trajectories SET status='updated', retry_count=0,"
                " error_msg=NULL, updated_at=datetime('now') WHERE id=?",
                (tid,),
            )
        conn.commit()
        print(f">> 已把 {len(ids)} 条轨迹翻回 updated，等服务下一轮 scan 重拆。")
        print(">> 跟踪进度：再跑 scripts/diagnose_split_loss.py 看 silent_gap 是否归零。")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
