#!/usr/bin/env python3.11
"""ClusterAgent 跨轨迹批量消费 —— before/after 实测（模拟环境，无真实 LLM）。

对比同一份新代码在两个配置下的聚类开销：
  - batch_size=1  ≡ 旧行为（每个 atom 一次 ClusterAgent 调用）
  - batch_size=8  = 新默认（跨轨迹池化，每批 ≤8 个 atom 一次调用）

数据源：scripts/bench/real/annotations.json 里 10 条**真实轨迹**的人工标注
``atom_count``（真实轨迹的真实 atom 数，不含原文）。直接按该数 seed atom 到
store、置 indexed，再跑真实 watcher 聚类回路——split/embed 不是本次改动点，故
跳过，只量被改的 cluster 阶段。

度量两个**确定性、可信**的量（不假装测 wall-clock，因为没调真实 LLM）：
  1. ClusterAgent 会话数（= LLM 往返次数）——延迟主因
  2. 喂给模型的累计输入字符数（system catalog 每会话重复一次）——成本主因
每个 atom 的推理/输出工作量两种配置下不变，所以这两项才是真实提速杠杆。
"""
from __future__ import annotations

import json
import math
import sys
import tempfile
import time
from pathlib import Path

# 让 stub 能记录每次 cluster 调用的输入规模
_REC = {"sessions": 0, "input_chars": 0}


def _make_stub():
    import re

    class _Stub:
        def __init__(self, *, instructions, tools):
            self.instructions = instructions
            self.tools = {getattr(t, "__name__", ""): t for t in tools}

        def run(self, user_msg, **_kw):
            head = (self.instructions[0] if self.instructions else "")[:80]

            class _R:
                content = ""

            if "TaskClusterAgent" in head:
                _REC["sessions"] += 1
                # 真实输入 = system prompt（含 skill 路由表）+ user 消息
                _REC["input_chars"] += len(self.instructions[0]) + len(user_msg)
                atom_ids = re.findall(r"atom_id:\s*(\S+)", user_msg)
                for aid in atom_ids:
                    # round-robin 路由到 8 个 skill，形成一个真实（小）的 catalog
                    sk = f"skill-{abs(hash(aid)) % 8}"
                    if "new_skill_folder" in self.tools:
                        self.tools["new_skill_folder"](sk, "bench skill")
                    if "add_task_to_skill" in self.tools:
                        self.tools["add_task_to_skill"](sk, aid, 3)
                return _R()
            # split / edit / 其它：noop
            return _R()

    return _Stub


def _seed_trajectories(wd: Path, store, traj_atoms: dict[str, int]) -> None:
    from xskill.pipeline.atom import AtomTask

    for tid, n_atoms in traj_atoms.items():
        stem = tid[:-3] if tid.endswith(".md") else tid
        (wd / f"{stem}.md").write_text("placeholder\n", encoding="utf-8")
        for i in range(n_atoms):
            store.save(AtomTask(
                atom_id=f"atom_{stem}_{i:04d}", traj_id=stem,
                offset_start=1 + i * 10, offset_end=10 + i * 10,
                intent=f"intent {i}", summary=f"summary for atom {i} of {stem}",
                tags=["bench"], used_skills=[], ux_score=7,
            ))


def _mark_indexed(wd_id: int, traj_atoms: dict[str, int], db: Path) -> None:
    from xskill.pipeline.registry import update_traj_status

    for tid in traj_atoms:
        stem = tid[:-3] if tid.endswith(".md") else tid
        update_traj_status(wd_id, f"{stem}.md", "indexed", db_path=db)


def _wait_for_cluster(watcher, wd_id: int, db: Path) -> None:
    from xskill.pipeline.registry import get_trajs_by_status

    for _ in range(2000):
        watcher._scan_once()
        for _ in range(200):
            if not watcher._futures:
                break
            time.sleep(0.005)
            watcher._harvest()
        if not get_trajs_by_status(wd_id, "indexed", db_path=db):
            break


def _run(traj_atoms: dict[str, int], batch_size: int):
    from xskill.pipeline.atom import AtomTaskStore
    from xskill.pipeline.registry import (
        register_dir, discover_trajectories,
        get_trajs_by_status,
    )
    from xskill.pipeline.runner import DirectoryWatcher

    _REC["sessions"] = 0
    _REC["input_chars"] = 0

    tmp = Path(tempfile.mkdtemp(prefix="bench_cluster_"))
    wd = tmp / "wd"; wd.mkdir()
    skill_dir = tmp / "skill"; skill_dir.mkdir()
    db = tmp / "bench.db"
    store = AtomTaskStore(root=wd)

    _seed_trajectories(wd, store, traj_atoms)
    wd_id = register_dir(wd, db_path=db)
    discover_trajectories(wd_id, wd, db_path=db)
    _mark_indexed(wd_id, traj_atoms, db)

    watcher = DirectoryWatcher(
        llm=None, embed_client=None,
        config={"llm": {"base_url": "x", "model": "y", "api_key": "z"}},
        skill_dir=skill_dir,
        poll_interval=0.0,
        max_concurrent=4,
        db_path=db,
        store=store,
        agno_agent_factory=_make_stub(),
        home_root=tmp,
        cluster_batch_size=batch_size,
    )

    t0 = time.monotonic()
    _wait_for_cluster(watcher, wd_id, db)
    wall = time.monotonic() - t0
    done = len(get_trajs_by_status(wd_id, "done", db_path=db))
    watcher._pool.shutdown(wait=False)
    return {"sessions": _REC["sessions"], "input_chars": _REC["input_chars"],
            "wall": wall, "done": done, "n_trajs": len(traj_atoms)}


def main():
    ann = json.load(open(Path(__file__).parent / "real" / "annotations.json"))
    # 取 atom 数最多的 10 条真实轨迹（= 有实质多意图的工作 session；1-atom 的琐碎
    # session 不进表）。同分按 traj_id 排序保证确定性。
    ranked = sorted(ann.items(),
                    key=lambda kv: (-int(kv[1].get("atom_count", 0)), kv[0]))
    picked = {tid: int(v["atom_count"]) for tid, v in ranked
              if v.get("atom_count")}
    picked = dict(list(picked.items())[:10])

    total_atoms = sum(picked.values())
    BATCH = 8

    before = _run(picked, batch_size=1)
    after = _run(picked, batch_size=BATCH)

    print("\n=== 10 条真实轨迹（来自 annotations.json 人工标注 atom_count）===")
    print(f"{'traj':44s} {'atoms':>6s} {'sess.before':>12s} {'sess.after(全局)':>16s}")
    for tid, n in picked.items():
        # before: 每 atom 一次；after 是跨轨迹全局 ceil，单条不可分摊，留空标注
        print(f"{tid:44s} {n:6d} {n:12d} {'—':>16s}")
    print(f"{'TOTAL':44s} {total_atoms:6d} {before['sessions']:12d} "
          f"{after['sessions']:16d}")

    def fmt(d):
        return (f"sessions={d['sessions']}  input_chars={d['input_chars']:,}  "
                f"done={d['done']}/{d['n_trajs']}  wall={d['wall']:.2f}s(mock)")

    print("\n=== 汇总 ===")
    print(f"总 atom 数:           {total_atoms}")
    print(f"BEFORE (batch=1):    {fmt(before)}")
    print(f"AFTER  (batch={BATCH}):    {fmt(after)}")
    print(f"理论 after 会话数:    ceil({total_atoms}/{BATCH}) = "
          f"{math.ceil(total_atoms / BATCH)}")
    sr = before['sessions'] / after['sessions'] if after['sessions'] else 0
    ir = before['input_chars'] / after['input_chars'] if after['input_chars'] else 0
    print(f"\n往返次数(round-trips)降幅:   {before['sessions']} → "
          f"{after['sessions']}  =  {sr:.2f}x 更少")
    print(f"输入字符(prefill 体量)降幅:  {before['input_chars']:,} → "
          f"{after['input_chars']:,}  =  {ir:.2f}x 更少")


if __name__ == "__main__":
    sys.exit(main())
