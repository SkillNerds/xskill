"""看板「流水线」页的实时监看数据：读 agent-worker 状态文件 + agent 日志尾巴。

常驻 agent-worker 子进程每 ``status_interval`` 秒把
``DirectoryWatcher.agent_worker_status`` 原子落盘到
``<home>/agent_worker_status.json``（见 ``utils/status_file.py``）；本模块只做
只读整形，不碰 watcher 进程内存，因此 serve 内置看板与独立只读实例都能用。

原则（与概念稿一致）：**禁止 fallback 糊弄**——状态文件缺失 / 日志不存在
一律显式空态，由前端如实展示，绝不编造数据。
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from xskill.utils.status_file import AGENT_WORKER_STATUS_FILE, read_status_file

logger = logging.getLogger(__name__)

# 监看展示的三池（embed 不进概念稿：读库事实，不占席位色块）。
# generate 不另开线程池：与 SkillEdit 共用 edit 席位，整形时拆成第四栏。
MONITORED_POOLS = ("split", "cluster", "edit")

# 日志尾巴默认/上限行数。
DEFAULT_LOG_TAIL = 300
MAX_LOG_TAIL = 2000
# 读日志尾巴时最多回退的字节数（避免整文件载入内存）。
_LOG_READBACK_BYTES = 512 * 1024

_KIND_TO_LOG_SUBPATH = {
    # SkillEditAgent._trace_path()
    "skill": ("agents", "skill_edit_agents", "skills"),
    # TaskAgent 拆分轨迹的逐轮 trace
    "traj": ("agents", "task_agents"),
    # GenerateAgent：实际文件在 generate_agents/<user_id>/<job_id>.log
    "generate": ("agents", "generate_agents"),
}


def status_root_for(db_path: Optional[Path]) -> Path:
    """状态文件所在 home：与 ``_skill_dir_for`` 同一套旁推——registry.db /
    状态文件 / skill 库同在 XSKILL_HOME 下；独立实例（显式 db_path）与
    serve 内置（db_path=None 走 config 默认）都能解析。"""
    if db_path is not None:
        return Path(db_path).parent
    from xskill.config import XSKILL_HOME
    return XSKILL_HOME


def pipeline_live(db_path: Optional[Path]) -> dict:
    """整形 agent-worker 状态文件为「流水线」页响应。

    状态文件缺失 / 内容为空 / watcher 已停 → ``running: False`` + 明示原因；
    绝不返回半真半假的占位数据。
    """
    root = status_root_for(db_path)
    payload = read_status_file(root / AGENT_WORKER_STATUS_FILE)
    if payload is None:
        return {
            "running": False,
            "message": "agent-worker 尚未启动（无状态文件）",
        }
    stats = payload.get("stats") or {}
    if not stats:
        return {
            "running": False,
            "ok": bool(payload.get("ok")),
            "error": payload.get("error"),
            "message": "agent-worker 未上报状态（状态文件为空）",
        }

    pool_config = stats.get("pool_config") or {}
    raw_pools = stats.get("pools") or {}
    pools: dict[str, dict] = {}
    for name in MONITORED_POOLS:
        status = raw_pools.get(name) or {}
        cfg = pool_config.get(name) or {}
        workers = int(status.get("workers") or cfg.get("workers") or 0)
        seats = status.get("seats")
        if not isinstance(seats, list):
            seats = [None] * workers
        elif len(seats) < workers:
            seats = list(seats) + [None] * (workers - len(seats))
        queue = status.get("queue")
        if not isinstance(queue, list):
            queue = []
        pools[name] = {
            "workers": workers,
            "llm_weight": cfg.get("llm_weight"),
            "batch_size": cfg.get("batch_size"),
            "seats": seats,
            "queue": queue,
            "queued": int(status.get("queued") or 0),
            "completed": int(status.get("completed") or 0),
            "failed": int(status.get("failed") or 0),
        }

    watcher = stats.get("watcher") or {}
    cluster = stats.get("cluster") or {}
    generate_stats = stats.get("generate") or {}
    edit_pool = pools.get("edit")
    if edit_pool is not None:
        pools["generate"] = _project_generate_pool(edit_pool, generate_stats)
        pools["edit"] = _hide_generate_tasks(edit_pool)
    return {
        "running": bool(watcher.get("running")),
        "ok": bool(payload.get("ok")),
        "error": payload.get("error"),
        "pid": stats.get("pid"),
        "started_at": stats.get("started_at"),
        "heartbeat_at": stats.get("heartbeat_at"),
        "llm": stats.get("llm") or {},
        "pending_atoms": int(cluster.get("pending_atoms") or 0),
        "pools": pools,
    }


def _is_generate_task(task: object) -> bool:
    return isinstance(task, dict) and task.get("kind") == "generate"


def _project_generate_pool(edit: dict, generate_stats: dict) -> dict:
    """从 edit 池席位里抽出 generate 任务，做成流水线第四栏。

    席位下标与 edit 对齐：同一 worker 只在一栏里显示为占用。
    """
    workers = int(edit.get("workers") or 0)
    seats = []
    for seat in edit.get("seats") or []:
        if isinstance(seat, dict) and _is_generate_task(seat.get("task")):
            seats.append(seat)
        else:
            seats.append(None)
    if len(seats) < workers:
        seats.extend([None] * (workers - len(seats)))
    elif len(seats) > workers:
        seats = seats[:workers]
    queue = [
        task for task in (edit.get("queue") or [])
        if _is_generate_task(task)
    ]
    return {
        "workers": workers,
        "llm_priority": True,
        "batch_size": None,
        "seats": seats,
        "queue": queue,
        "queued": len(queue),
        "completed": int(generate_stats.get("completed") or 0),
        "failed": int(generate_stats.get("failed") or 0),
        "shared_pool": "edit",
    }


def _hide_generate_tasks(edit: dict) -> dict:
    """SkillEdit 栏不重复展示 generate 占用的席位。"""
    seats = []
    for seat in edit.get("seats") or []:
        if isinstance(seat, dict) and _is_generate_task(seat.get("task")):
            seats.append(None)
        else:
            seats.append(seat)
    queue = [
        task for task in (edit.get("queue") or [])
        if not _is_generate_task(task)
    ]
    out = dict(edit)
    out["seats"] = seats
    out["queue"] = queue
    return out


def _safe_log_name(name: str) -> str:
    """日志文件名防路径穿越：只允许单段文件名。"""
    if not name or name in (".", ".."):
        raise ValueError("name 不能为空")
    if "/" in name or "\\" in name or ".." in name:
        raise ValueError(f"非法日志名: {name!r}")
    return name


def tail_task_log(
    db_path: Optional[Path],
    *,
    kind: str,
    name: str,
    tail: int = DEFAULT_LOG_TAIL,
) -> dict:
    """读单个任务的 agent trace 尾巴。

    ``kind`` 认 ``skill`` / ``traj`` / ``generate``；cluster 批没有独立日志
    文件（概念稿的 Cluster 详情展示本批 atom id 列表，不走本端点）。文件不
    存在是正常情况（任务刚起跑 / logs_dir 未配），返回显式空态而非 404。
    """
    if kind not in _KIND_TO_LOG_SUBPATH:
        raise ValueError(f"kind 必须是 {sorted(_KIND_TO_LOG_SUBPATH)} 之一")
    name = _safe_log_name(name)
    tail = max(1, min(int(tail), MAX_LOG_TAIL))
    root = status_root_for(db_path) / "logs"
    if kind == "generate":
        matches = list(root.joinpath(*_KIND_TO_LOG_SUBPATH[kind]).glob(f"*/{name}.log"))
        path = matches[0] if matches else root.joinpath(*_KIND_TO_LOG_SUBPATH[kind], name + ".log")
    else:
        path = root.joinpath(*_KIND_TO_LOG_SUBPATH[kind]) / f"{name}.log"
    if not path.is_file():
        return {
            "kind": kind,
            "name": name,
            "exists": False,
            "lines": [],
            "message": "该任务暂无日志文件",
        }
    try:
        with path.open("rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            handle.seek(max(0, size - _LOG_READBACK_BYTES))
            raw = handle.read()
    except OSError as exc:
        logger.warning("读任务日志失败 %s: %s", path, exc)
        return {
            "kind": kind,
            "name": name,
            "exists": False,
            "lines": [],
            "message": f"日志读取失败: {exc}",
        }
    text = raw.decode("utf-8", errors="replace")
    lines = text.splitlines()[-tail:]
    return {
        "kind": kind,
        "name": name,
        "exists": True,
        "lines": lines,
        "truncated": size > _LOG_READBACK_BYTES,
    }
