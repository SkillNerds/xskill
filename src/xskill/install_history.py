"""
install_history.py -- daemon 自己装到 ~/.claude/skills/ 的 side 历史
=====================================================================

灰度链路里 CC 直接读磁盘上的 ``SKILL.md`` 一份文件——CC 既不知道也不关心
xskill 是不是在做 A/B。要做"半数 CC session 看到 main、半数看到 staging"，
daemon 唯一能动的就是周期性地翻磁盘文件，然后**记住自己什么时候装了哪边**。

这个文件是那份"记账"。append-only jsonl，每行一条 install 记录：

  {"t": 1700000000.123, "skill": "list-py-files", "side": "main", "sha": "abc1234"}

CC session 桥进 xskill 这边时：
  - 读 JSONL 第一条事件 → session_start_t
  - 用 lookup(session_start_t) 找出"那一刻盘上装的是哪 side"
  - 据此给桥过来的 traj 写 xskill header

整套思路在 daemon 单边自洽：不挂 FUSE、不动 CC 插件、不靠模型听话；CC 永远
对此无感，只是它每次 session 启动读盘那一刻**真**读到了 daemon 写下的内容
（无论 main 还是 staging），而 daemon 知道那一刻自己写了什么。
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Optional


class InstallHistory:
    """thread-safe append + linear scan reader for a single jsonl file.

    不上 SQLite——这是一份纯顺序写、纯查"≤t 的最后一条"的日志，几十到几百条
    规模，文件 IO 足够。多线程下用一把锁保护 append；reader 不锁，最坏只是
    读到稍旧的快照，对反查 side 无影响（lookup 给定的是 session_start_t，
    总有人后于它做完 append，下次 poll 就能查到）。
    """

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def record(
        self,
        *,
        skill: str,
        side: str,
        sha: str = "",
        t: Optional[float] = None,
    ) -> dict:
        """写一条 install 成功记录。返回写入的完整 record（含 t）。

        语义：``action`` 字段默认是 ``"install"``——本方法**只**写成功
        记录。失败请走 ``record_fail()``，那条记录形态不同（无
        ``side``、含 ``agent`` + ``reason``）。
        """
        if side not in ("main", "staging"):
            raise ValueError(f"side must be 'main' or 'staging', got {side!r}")
        record = {
            "t": t if t is not None else time.time(),
            "action": "install",
            "skill": skill,
            "side": side,
            "sha": sha,
        }
        with self._lock:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        return record

    def record_fail(
        self,
        *,
        skill: str,
        agent: str,
        reason: str,
        t: Optional[float] = None,
    ) -> dict:
        """写一条 install 失败记录。

        与 ``record()`` 的成功记录共享同一份 jsonl 文件；用 ``action`` 字段
        区分（``"install"`` vs ``"fail"``）。失败记录不带 side / sha——这两
        个字段只对成功 install 有意义；记 ``agent`` (``claude_code`` /
        ``codex`` / ``opencode``) + ``reason`` (异常摘要) 方便运维定位。

        ``lookup()`` / ``count_by_side()`` 内部按 ``action=="install"`` 过滤
        （成功记录默认无 action 字段或 action=="install"），失败记录不影响
        side 反查链路。
        """
        record = {
            "t": t if t is not None else time.time(),
            "action": "fail",
            "skill": skill,
            "agent": agent,
            "reason": reason,
        }
        with self._lock:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        return record

    def all_records(self) -> list[dict]:
        """读全量。文件不存在返回 []。坏行（解析失败）跳过——append 是写完
        整 jsonl 行，进程崩溃中断会留半行，恢复时静默丢弃比抛错好。"""
        if not self.path.is_file():
            return []
        out: list[dict] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out

    def lookup(self, t: float, *, skill: Optional[str] = None) -> Optional[dict]:
        """返回 ``t`` 时刻盘上装的是哪条**成功**记录（即 ``record.t ≤ t`` 中最晚的）。

        ``skill`` 指定时仅在该 skill 的记录里查；不指定时返回**全局**最后一
        条 ``record.t ≤ t``。在多 skill 同时灰度的场景应当传 skill。

        失败记录（``action == "fail"``）被过滤掉——本方法用于反查 side，
        失败记录无 side 字段，对反查无意义。

        没有合适记录（t 早于最早一条 install）返回 None。
        """
        recs = self.all_records()
        # 仅看成功 install 记录（无 action 字段的老记录默认视为 install）。
        recs = [r for r in recs if r.get("action", "install") == "install"]
        if skill is not None:
            recs = [r for r in recs if r.get("skill") == skill]
        candidate: Optional[dict] = None
        for r in recs:
            rt = r.get("t")
            if isinstance(rt, (int, float)) and rt <= t:
                if candidate is None or rt >= candidate["t"]:
                    candidate = r
        return candidate

    def count_by_side(self, *, skill: Optional[str] = None) -> dict[str, int]:
        """各 side 装了多少次（调试 / 测试看分布用）。

        失败记录不计入。
        """
        counts: dict[str, int] = {"main": 0, "staging": 0}
        for r in self.all_records():
            if r.get("action", "install") != "install":
                continue
            if skill is not None and r.get("skill") != skill:
                continue
            side = r.get("side")
            if side in counts:
                counts[side] += 1
        return counts

    def fail_records(self) -> list[dict]:
        """返回所有失败记录（``action == "fail"``）。运维 / 测试用。"""
        return [r for r in self.all_records() if r.get("action") == "fail"]
