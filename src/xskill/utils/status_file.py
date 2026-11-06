"""跨进程状态文件:常驻 agent-worker 定期写心跳，短命 profile-refresh 写一轮结果，
常驻 api 进程的 /agent-worker/status、/watcher/status、/stats 端点读取状态。

agent-worker / 画像拆成独立子进程后,api 进程不再持有它们的内存对象(原来
``_watcher_ref["instance"].stats`` / ``_profile_refresh_ref["instance"].metrics``),
故改经磁盘 JSON 通信。写为原子(临时文件 + os.replace),避免读到半截 JSON。
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger("xskill.utils.status_file")

WATCHER_STATUS_FILE = "watcher_status.json"
AGENT_WORKER_STATUS_FILE = "agent_worker_status.json"
PROFILE_STATUS_FILE = "profile_refresh_status.json"


def write_status_file(path: Path, stats: dict, *, ok: bool,
                      error: Optional[str] = None, active: bool | None = None) -> None:
    """原子写心跳或一轮任务结果。状态是观测数据,写失败只落 warning、不抛——
    绝不因为写不了状态文件而让子进程的核心任务失败(与后台刷新 best-effort 一致)。"""
    payload = {
        "ok": ok,
        "error": error,
        "ended_at": time.time(),
        "stats": stats or {},
        "pid": os.getpid(),
    }
    if active is not None:
        payload["active"] = active
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        logger.warning("写状态文件失败 %s", path, exc_info=True)


def read_status_file(path: Path) -> Optional[dict]:
    """读最近状态;文件不存在返回 None(子进程尚未启动)。"""
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning("读状态文件失败 %s", path, exc_info=True)
        return None
