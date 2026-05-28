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

from xskill.team.client.redact import redact_text

logger = logging.getLogger("xskill.team.client.collector")


@dataclass
class PendingTrajectory:
    traj_id: str
    content: str       # 已脱敏
    sha256: str        # 脱敏后 content 的 sha256


class TeamCollector:
    """采集本机生态轨迹 → 标准 bridge 目录；吐 pending 给 TeamClient 上传。"""

    def __init__(
        self,
        *,
        cursor_path: Path,
        quiet_seconds: int = 180,
        home_root: Path | None = None,
        poll_interval: float = 10.0,
    ):
        self.cursor_path = Path(cursor_path)
        self.quiet_seconds = quiet_seconds
        self.home_root = Path(home_root) if home_root else Path.home()
        self.poll_interval = poll_interval
        # 标准 bridge 目录都落在 <home_root>/.xskill/ 下（cc_sessions /
        # codex_sessions / opencode_sessions）——与 detect_known_ecosystems
        # 返回的 bridge 路径一致。
        self._bridge_root = self.home_root / ".xskill"
        self._ingesters: list = []
        self._cursor: dict[str, str] = self._load_cursor()

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
        """记录某 traj 的某版本已上传。"""
        self._cursor[traj_id] = sha256
        self._save_cursor()

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
        """扫 ``~/.xskill/*_sessions/`` 所有 traj_*.md，吐出静默够久 +
        未上传过/内容已变的。不依赖 start_ingesters 是否已跑——直接扫盘。
        """
        now = time.time()
        out: list[PendingTrajectory] = []
        for md in sorted(self._bridge_root.glob("*_sessions/traj_*.md")):
            if not md.is_file():
                continue
            # 静默窗口：太新的文件可能还在写，等它静默
            if (now - md.stat().st_mtime) < self.quiet_seconds:
                continue
            raw = md.read_text(encoding="utf-8")
            content = redact_text(raw)
            sha = hashlib.sha256(content.encode("utf-8")).hexdigest()
            traj_id = md.stem
            if self._cursor.get(traj_id) == sha:
                continue   # 这个版本已上传过
            out.append(PendingTrajectory(traj_id=traj_id, content=content, sha256=sha))
        return out
