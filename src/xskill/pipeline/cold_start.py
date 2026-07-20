"""冷启动一次性批量 flush 信号。

冷启动不是可配置的线上状态机，也没有多轮概念。``xskill rebuild`` 把本批被
重置的轨迹 id 快照写进当前 XSkill 实例的 ``COLD_START``，watcher 只等这批轨迹全部
到达终态（离开 pending 状态）就按既有 ``ATOM_PROMOTION_THRESHOLD`` 做一次
SkillEdit 扫描并删除该文件——rebuild 之后新进的轨迹不延长等待。
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path


COLD_START_FILENAME = "COLD_START"

# 快照内个别轨迹卡死时的安全网：超过该时长强制 flush，不再 hold。
COLD_START_MAX_HOLD_SECONDS = 24 * 3600


@dataclass(frozen=True)
class ColdStartSignal:
    """管理一次 cold-start flush 的文件信号。"""

    xskill_home: Path

    @property
    def file_path(self) -> Path:
        return self.xskill_home / COLD_START_FILENAME

    @property
    def exists(self) -> bool:
        return self.file_path.exists()

    def create(self, trajectory_ids: list[int]) -> dict:
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "trajectory_ids": list(trajectory_ids),
            "created_at": time.time(),
        }
        # 原子写：CLI 与 watcher 补录跨进程双写，不能让对方读到半截 JSON。
        temp_path = self.file_path.with_suffix(".tmp")
        temp_path.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(temp_path, self.file_path)
        return payload

    def snapshot(self) -> dict | None:
        """读快照。≤0.6.11 的空 touch 文件/坏 JSON 返回 None，调用方补录。"""
        try:
            payload = json.loads(self.file_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(payload, dict):
            return None
        if not isinstance(payload.get("trajectory_ids"), list):
            return None
        return payload

    def consume(self) -> None:
        if self.exists:
            self.file_path.unlink()
