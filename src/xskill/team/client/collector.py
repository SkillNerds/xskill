"""collector.py — client 端本地轨迹采集（SP1）

两件事：
1. start_ingesters() —— 复用既有 JsonlIngester(CC_SPEC/CODEX_SPEC) +
   SqliteIngester(OPENCODE_SPEC) 把本机 code-agent session 镜像成
   ``traj_*.md`` 落进**标准 bridge 目录** ``~/.xskill/<eco>_sessions/``
   （即 ``detect_known_ecosystems`` 返回的 ``bridge`` 路径——不另造一份
   平行 outbox）。这些 ingester 是纯镜像——不做 canary/header 注入。
2. pending() —— 扫 ``~/.xskill/*_sessions/``，吐出"静默 ≥quiet_seconds 且
   未上传过/内容已变"的 traj，content 已过脱敏 hook。游标落 cursor.json：
   traj_id -> sha256。

静默窗口 = 设计里约定的上传时机点（与 xskill 既有的"用户手改静默 3min
才吸收"同源），也天然是脱敏 hook 的插入位。
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from xskill.team.client.redact import redact_text

logger = logging.getLogger("xskill.team.client.collector")


@dataclass
class PendingTrajectory:
    traj_id: str
    content: str       # 已脱敏
    sha256: str        # 脱敏后 content 的 sha256
    model: str = ""    # 用户 agent 模型，取自同名 .json sidecar 的 "model"
    harness: str = ""  # 用户 coding agent，按所在 bridge 目录推断（cc_sessions→claude_code）


# bridge 目录名 → 规范 harness(=ecosystem) 名。collector 把各生态镜像到
# <home>/.xskill/<bridge>/，目录名即生态来源,据此还原用户用的是哪个 coding agent。
_HARNESS_BY_BRIDGE = {
    "cc_sessions": "claude_code",
    "codex_sessions": "codex",
    "opencode_sessions": "opencode",
    "ngagent_sessions": "ngagent",
    "trae_sessions": "trae",
    "cursor_sessions": "cursor",
}


def _harness_for(md_path: Path) -> str:
    """从 traj_*.md 所在 bridge 目录名推断 harness（coding agent）。"""
    bridge = md_path.parent.name
    return _HARNESS_BY_BRIDGE.get(bridge, bridge.replace("_sessions", ""))


def _sidecar_model(md_path: Path) -> str:
    """读 ``<traj>.md`` 同目录同名 ``.json`` sidecar 里的 ``model``；
    无 sidecar / 无该键 / 解析失败 → 空串（保持 unknown，不抛错不影响上传）。"""
    jp = md_path.with_suffix(".json")
    if not jp.is_file():
        return ""
    try:
        return str(json.loads(jp.read_text(encoding="utf-8")).get("model") or "")
    except (OSError, json.JSONDecodeError):
        return ""


class TeamCollector:
    """采集本机生态轨迹 → 标准 bridge 目录；吐 pending 给 TeamClient 上传。"""

    def __init__(
        self,
        *,
        cursor_path: Path,
        quiet_seconds: int = 180,
        min_change_interval: int = 600,
        home_root: Path | None = None,
        poll_interval: float = 10.0,
        time_fn: Callable[[], float] = time.time,
    ):
        self.cursor_path = Path(cursor_path)
        self.quiet_seconds = quiet_seconds
        # 上传频率拦截（limit_rate）：同一条 traj 的内容（hash）距上次变更必须
        # 静默 ≥ min_change_interval 秒才允许上传。用户代理工具调用会让轨迹文件
        # 每 ~30s 追加一次，若每次增量都上传，server 每次跑全量流水线，原子会被
        # 切碎、不成体系。这里按 hash-变更去抖（debounce）：内容只要还在变，计时
        # 就一直重置，直到稳定满 10 分钟（默认）才放行——保证上传的是一段相对
        # 完整、可被连贯拆分的轨迹。
        self.min_change_interval = min_change_interval
        self.home_root = Path(home_root) if home_root else Path.home()
        self.poll_interval = poll_interval
        self._now = time_fn
        # 标准 bridge 目录都落在 <home_root>/.xskill/ 下（cc_sessions /
        # codex_sessions / opencode_sessions）——与 detect_known_ecosystems
        # 返回的 bridge 路径一致。
        self._bridge_root = self.home_root / ".xskill"
        self._ingesters: list = []
        self._cursor: dict[str, str] = self._load_cursor()
        # 去抖状态：traj_id -> {"sha": <当前未上传版本的 hash>, "since": <该 hash
        # 首次出现的时间戳>}。落盘到 cursor 旁的 sidecar,重启后不丢去抖计时。
        self._debounce_path = self.cursor_path.with_suffix(".debounce.json")
        self._change_state: dict[str, dict] = self._load_change_state()

    # ── 游标 ─────────────────────────────────────────────────────
    def _load_cursor(self) -> dict[str, str]:
        if not self.cursor_path.is_file():
            return {}
        try:
            return json.loads(self.cursor_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}

    def _save_cursor(self) -> None:
        self.cursor_path.parent.mkdir(parents=True, exist_ok=True)
        self.cursor_path.write_text(json.dumps(self._cursor), encoding="utf-8")

    def mark_uploaded(self, traj_id: str, sha256: str) -> None:
        """记录某 traj 的某版本已上传。同时清掉它的去抖状态（该版本已落地）。"""
        self._cursor[traj_id] = sha256
        self._save_cursor()
        if self._change_state.pop(traj_id, None) is not None:
            self._save_change_state()

    # ── hash-变更去抖状态 ────────────────────────────────────────
    def _load_change_state(self) -> dict[str, dict]:
        if not self._debounce_path.is_file():
            return {}
        try:
            return json.loads(self._debounce_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}

    def _save_change_state(self) -> None:
        self._debounce_path.parent.mkdir(parents=True, exist_ok=True)
        self._debounce_path.write_text(
            json.dumps(self._change_state), encoding="utf-8")

    # ── ingester 生命周期 ────────────────────────────────────────
    def start_ingesters(self) -> None:
        """探测本机生态，对每个起一个纯镜像 ingester 写进标准 bridge 目录。"""
        from xskill.ecosystems import (
            detect_known_ecosystems, JsonlIngester, SqliteIngester,
            TraeIngester,
            CC_SPEC, CODEX_SPEC, OPENCODE_SPEC, NGAGENT_SPEC,
        )
        for det in detect_known_ecosystems(home_root=self.home_root):
            eco = det["ecosystem"]
            bridge = det["bridge"]   # 标准路径 ~/.xskill/<eco>_sessions
            bridge.mkdir(parents=True, exist_ok=True)
            if eco == "claude_code":
                ing = JsonlIngester(CC_SPEC, target_traj_dir=bridge,
                                    home_root=self.home_root,
                                    poll_interval=self.poll_interval)
            elif eco == "codex":
                ing = JsonlIngester(CODEX_SPEC, target_traj_dir=bridge,
                                    home_root=self.home_root,
                                    poll_interval=self.poll_interval)
            elif eco == "opencode":
                ing = SqliteIngester(target_traj_dir=bridge,
                                     home_root=self.home_root,
                                     spec=OPENCODE_SPEC,
                                     poll_interval=self.poll_interval)
            elif eco == "ngagent":
                # ngagent = opencode 企业分支，复用 SqliteIngester，只换 spec
                ing = SqliteIngester(target_traj_dir=bridge,
                                     home_root=self.home_root,
                                     spec=NGAGENT_SPEC,
                                     poll_interval=self.poll_interval)
            elif eco == "trae":
                ing = TraeIngester(target_traj_dir=bridge,
                                   home_root=self.home_root,
                                   poll_interval=self.poll_interval)
            else:
                continue
            ing.start()
            self._ingesters.append(ing)
            logger.info("collector ingester started: %s -> %s", eco, bridge)

    def stop_ingesters(self) -> None:
        for ing in self._ingesters:
            try:
                ing.stop()
            except Exception:
                logger.warning("failed to stop ingester", exc_info=True)
        self._ingesters.clear()

    # ── pending ─────────────────────────────────────────────────
    def pending(self) -> list[PendingTrajectory]:
        """扫 ``~/.xskill/*_sessions/`` 所有 traj_*.md，吐出满足放行条件的轨迹。

        放行条件（两道闸，都过才上传）：
        1. **mtime 静默** ≥ quiet_seconds：避免读到正在写一半的文件。
        2. **hash-变更去抖** ≥ min_change_interval（默认 10 分钟）：内容自上次变更
           起必须稳定够久。内容每变一次就把计时重置——agent 频繁工具调用导致的
           连续增量会被一直拦住，直到轨迹稳定下来，才作为一段连贯轨迹上传。

        不依赖 start_ingesters 是否已跑——直接扫盘。每次调用会就地推进 / 重置
        去抖计时并落盘,所以 daemon 的周期性 poll 就是计时驱动。
        """
        now = self._now()
        out: list[PendingTrajectory] = []
        seen_ids: set[str] = set()
        changed = False
        for md in sorted(self._bridge_root.glob("*_sessions/traj_*.md")):
            if not md.is_file():
                continue
            traj_id = md.stem
            seen_ids.add(traj_id)
            # 闸 1：mtime 静默窗口
            if (now - md.stat().st_mtime) < self.quiet_seconds:
                continue
            raw = md.read_text(encoding="utf-8")
            content = redact_text(raw)
            sha = hashlib.sha256(content.encode("utf-8")).hexdigest()
            if self._cursor.get(traj_id) == sha:
                # 这个版本已上传过——清掉残留去抖状态
                if self._change_state.pop(traj_id, None) is not None:
                    changed = True
                continue
            # 闸 2：hash-变更去抖。内容（hash）每变一次就把 since 重置成此刻,
            # 必须自上次变更起稳定满 min_change_interval 秒才放行。
            st = self._change_state.get(traj_id)
            if st is None or st.get("sha") != sha:
                st = {"sha": sha, "since": now}
                self._change_state[traj_id] = st
                changed = True
            if (now - st["since"]) < self.min_change_interval:
                continue  # 还没稳定满窗口,继续拦（min_change_interval<=0 时恒放行）
            out.append(PendingTrajectory(traj_id=traj_id, content=content,
                                         sha256=sha, model=_sidecar_model(md),
                                         harness=_harness_for(md)))
        # 清理已消失的 traj 的去抖状态,避免无限增长
        for gone in [k for k in self._change_state if k not in seen_ids]:
            del self._change_state[gone]
            changed = True
        if changed:
            self._save_change_state()
        return out
