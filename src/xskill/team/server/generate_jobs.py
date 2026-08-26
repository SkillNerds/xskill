"""team server 上的 generate 任务：入队到 agent-worker 的 SkillEdit 池，流式给客户端。"""
from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Iterator

logger = logging.getLogger("xskill.team.generate")

_JOBS: dict[str, dict[str, Any]] = {}
_JOBS_LOCK = threading.Lock()
_ACTIVE_STATUSES = ("queued", "running")


def _job_dir(logs_dir: Path, user_id: str) -> Path:
    path = logs_dir / "agents" / "generate_agents" / user_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def prepare_generate_wiki(logs_dir: Path, user_id: str, job_id: str) -> Path:
    """按 job 建 wiki。已有页不覆盖，同一 job_id 重跑可恢复。"""
    from xskill.agents.llm_wiki import seed_generate_wiki

    root = (
        Path(logs_dir)
        / "agents"
        / "generate_agents"
        / user_id
        / "wiki"
        / job_id
    )
    return seed_generate_wiki(root)


def jobs_root(logs_dir: Path) -> Path:
    """web 进程与 agent-worker 共用的入队目录（与 logs 同级）。"""
    return Path(logs_dir).expanduser().resolve().parent / "generate_jobs"


def _pending_dir(logs_dir: Path) -> Path:
    path = jobs_root(logs_dir) / "pending"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _claimed_dir(logs_dir: Path) -> Path:
    path = jobs_root(logs_dir) / "claimed"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _status_path(log_path: Path) -> Path:
    return log_path.with_suffix(".status.json")


def _instruction_preview(text: str) -> str:
    one = " ".join((text or "").split())
    return one[:80]


def create_job(
    *,
    client_id: str,
    user_id: str,
    instruction: str,
    preferred_names: list[str],
    logs_dir: Path,
) -> dict[str, Any]:
    job_id = uuid.uuid4().hex
    log_path = _job_dir(logs_dir, user_id) / f"{job_id}.log"
    log_path.write_text(
        f"generate queued job_id={job_id} user={user_id}\n"
        "waiting for SkillEdit pool seat\n",
        encoding="utf-8",
    )
    job = {
        "job_id": job_id,
        "client_id": client_id,
        "user_id": user_id,
        "instruction": instruction,
        "preferred_names": list(preferred_names),
        "status": "queued",
        "log_path": str(log_path),
        "skill_names": [],
        "pinned": [],
        "error": "",
        "created_at": time.time(),
    }
    with _JOBS_LOCK:
        _JOBS[job_id] = job
    _write_status(job)
    return dict(job)


def enqueue_generate_job(job: dict[str, Any], *, logs_dir: Path) -> None:
    """把任务写进 pending，供 agent-worker 的 edit 池领取。"""
    payload = {
        "job_id": job["job_id"],
        "client_id": job["client_id"],
        "user_id": job["user_id"],
        "instruction": job["instruction"],
        "preferred_names": list(job.get("preferred_names") or []),
        "log_path": job["log_path"],
        "created_at": job.get("created_at") or time.time(),
        "status": "queued",
    }
    path = _pending_dir(logs_dir) / f"{job['job_id']}.json"
    _atomic_write_json(path, payload)


def list_pending_paths(logs_dir: Path) -> list[Path]:
    pending = jobs_root(logs_dir) / "pending"
    if not pending.is_dir():
        return []
    return sorted(
        (p for p in pending.glob("*.json") if p.is_file()),
        key=lambda p: p.stat().st_mtime,
    )


def try_claim(logs_dir: Path, pending_path: Path) -> dict[str, Any] | None:
    claimed = _claimed_dir(logs_dir) / pending_path.name
    try:
        pending_path.replace(claimed)
    except FileNotFoundError:
        return None
    except OSError:
        logger.warning("generate claim failed for %s", pending_path, exc_info=True)
        return None
    try:
        return json.loads(claimed.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning("generate claimed file unreadable %s", claimed, exc_info=True)
        return None


def release_claim(logs_dir: Path, job_id: str) -> None:
    claimed = _claimed_dir(logs_dir) / f"{job_id}.json"
    if not claimed.is_file():
        return
    pending = _pending_dir(logs_dir) / claimed.name
    try:
        claimed.replace(pending)
    except OSError:
        logger.warning("generate release claim failed for %s", job_id, exc_info=True)


def finish_claim(logs_dir: Path, job_id: str) -> None:
    claimed = _claimed_dir(logs_dir) / f"{job_id}.json"
    try:
        claimed.unlink(missing_ok=True)
    except TypeError:
        # Python 3.9: Path.unlink 无 missing_ok
        try:
            claimed.unlink()
        except FileNotFoundError:
            pass
    except OSError:
        logger.warning("generate finish claim failed for %s", job_id, exc_info=True)


def reclaim_orphans(logs_dir: Path, inflight_ids: set[str]) -> None:
    """进程重启后：已结束的 claimed 丢掉，未结束的退回 pending。"""
    claimed_dir = jobs_root(logs_dir) / "claimed"
    if not claimed_dir.is_dir():
        return
    for path in list(claimed_dir.glob("*.json")):
        job_id = path.stem
        if job_id in inflight_ids:
            continue
        try:
            job = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        status = ""
        log_path = job.get("log_path")
        if log_path:
            disk = _read_status_file(Path(log_path))
            status = str((disk or {}).get("status") or "")
        if status in ("succeeded", "failed"):
            try:
                path.unlink()
            except OSError:
                pass
            continue
        try:
            path.replace(_pending_dir(logs_dir) / path.name)
        except OSError:
            logger.warning("generate reclaim failed for %s", job_id, exc_info=True)


def monitor_task(job: dict[str, Any]) -> dict[str, Any]:
    """流水线席位元数据（给 BoundedExecutor 的 task=）。"""
    return {
        "kind": "generate",
        "job_id": job["job_id"],
        "user_id": job.get("user_id") or "",
        "instruction": _instruction_preview(str(job.get("instruction") or "")),
    }


def get_job(job_id: str) -> dict[str, Any] | None:
    with _JOBS_LOCK:
        cached = _JOBS.get(job_id)
        job = dict(cached) if cached is not None else None
    if job is None:
        job = _find_job_on_disk(job_id)
    if job is None:
        return None
    return _refresh_from_status(job)


def _find_job_on_disk(job_id: str) -> dict[str, Any] | None:
    from xskill.config import get_logs_dir

    logs_dir = get_logs_dir()
    root = logs_dir / "agents" / "generate_agents"
    if root.is_dir():
        matches = list(root.glob(f"*/{job_id}.status.json"))
        if matches:
            try:
                payload = json.loads(matches[0].read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                payload = {}
            payload.setdefault("job_id", job_id)
            payload["log_path"] = str(matches[0].with_name(f"{job_id}.log"))
            return payload
    for sub in ("pending", "claimed"):
        path = jobs_root(logs_dir) / sub / f"{job_id}.json"
        if not path.is_file():
            continue
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
    return None


def _read_status_file(log_path: Path) -> dict[str, Any] | None:
    status_path = _status_path(Path(log_path))
    try:
        return json.loads(status_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _refresh_from_status(job: dict[str, Any]) -> dict[str, Any]:
    log_path = job.get("log_path")
    if not log_path:
        return job
    disk = _read_status_file(Path(log_path))
    if not disk:
        return job
    for key in ("status", "skill_names", "pinned", "error", "client_id", "user_id"):
        if key in disk:
            job[key] = disk[key]
    with _JOBS_LOCK:
        cached = _JOBS.get(job["job_id"])
        if cached is not None:
            for key in ("status", "skill_names", "pinned", "error"):
                if key in disk:
                    cached[key] = disk[key]
    return job


def _update_job(job_id: str, **fields: Any) -> dict[str, Any] | None:
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
        if job is None:
            return None
        job.update(fields)
        snapshot = dict(job)
    _write_status(snapshot)
    return snapshot


def _append_generate_log(log_path: str | None, text: str) -> None:
    if not log_path:
        return
    try:
        with Path(log_path).open("a", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
    except OSError:
        logger.debug("failed to append generate log %s", log_path, exc_info=True)


def _write_status(job: dict[str, Any]) -> None:
    log_path = Path(job["log_path"])
    payload = {
        key: job[key]
        for key in (
            "job_id", "client_id", "user_id", "status",
            "skill_names", "pinned", "error", "created_at",
        )
        if key in job
    }
    try:
        _atomic_write_json(_status_path(log_path), payload)
    except OSError:
        logger.warning("failed to write generate status %s", log_path, exc_info=True)


def collect_read_roots(
    skill_dir: Path,
    traj_root: Path | None,
    db_path: Path | None = None,
) -> list[Path]:
    roots: list[Path] = [Path(skill_dir)]
    if traj_root is not None:
        roots.append(Path(traj_root))
        clients = Path(traj_root) / "clients"
        if clients.is_dir():
            roots.append(clients)
    try:
        from xskill.pipeline.registry import list_watch_dirs
        kw = {"db_path": db_path} if db_path is not None else {}
        for row in list_watch_dirs(**kw):
            path = Path(row["path"])
            if path.is_dir():
                roots.append(path)
    except Exception:
        logger.debug("list_watch_dirs unavailable for generate roots", exc_info=True)
    unique: list[Path] = []
    seen: set[str] = set()
    for path in roots:
        key = str(path.resolve()) if path.exists() else str(path)
        if key in seen:
            continue
        seen.add(key)
        unique.append(path)
    return unique


def exclude_blocked_read_roots(
    roots: list[Path],
    blocked: tuple[Path, ...] | list[Path],
) -> list[Path]:
    """Drop roots that are an on-hold client tree or sit inside one."""
    if not blocked:
        return list(roots)
    blocked_resolved: list[Path] = []
    for raw in blocked:
        path = Path(raw)
        try:
            blocked_resolved.append(path.resolve())
        except OSError:
            blocked_resolved.append(path)

    def _is_blocked(path: Path) -> bool:
        try:
            resolved = path.resolve() if path.exists() else path
        except OSError:
            resolved = path
        for root in blocked_resolved:
            if resolved == root:
                return True
            try:
                resolved.relative_to(root)
                return True
            except ValueError:
                continue
        return False

    return [path for path in roots if not _is_blocked(path)]


def collect_blocked_traj_roots(
    traj_root: Path | None,
    clients_db_path: Path | None = None,
) -> tuple[Path, ...]:
    from xskill.config import get_team_clients_db_path
    from xskill.team.server.client_registry import paused_trajectory_roots

    if traj_root is None:
        return ()
    db_path = clients_db_path
    if db_path is None:
        db_path = get_team_clients_db_path()
    return paused_trajectory_roots(traj_root, db_path)


def pin_generated_skills(
    *,
    user_id: str,
    skill_names: list[str],
    db_path: Path | None,
    max_pinned: int | None,
    origin_source: str = "generate",
) -> list[str]:
    from xskill.pipeline.registry import (
        PinQuotaExceeded, record_skill_origin, set_skill_pref,
    )

    pinned: list[str] = []
    for name in skill_names:
        try:
            set_skill_pref(
                user_key=user_id,
                skill_name=name,
                pref="pinned",
                set_by=user_id,
                max_pinned=max_pinned,
                db_path=db_path,
            )
            pinned.append(name)
        except PinQuotaExceeded as error:
            logger.warning("generate pin quota exceeded for %s: %s", name, error)
        except Exception:
            logger.exception("generate pin failed for %s", name)
        try:
            record_skill_origin(
                skill_name=name,
                user_key=user_id,
                source=origin_source,
                db_path=db_path,
            )
        except Exception:
            logger.exception("skill origin record failed for %s", name)
    try:
        from xskill.team.server import api as server_api
        with server_api._MANIFEST_CONTROL_CACHE_LOCK:
            server_api._MANIFEST_CONTROL_CACHE.clear()
    except Exception:
        logger.debug("could not invalidate manifest cache after generate pin", exc_info=True)
    return pinned


def run_generate_job(job_id: str, *, ctx: Any, config: dict | None) -> None:
    """Run one generate job in the current thread. Tests may call this directly."""
    job = get_job(job_id)
    if job is None:
        return
    try:
        traj_root = Path(ctx.traj_root) if getattr(ctx, "traj_root", None) is not None else None
        from xskill.utils.rate_limit import request_source
        with request_source("generate"):
            _run_generate_job_body(
                job,
                skill_dir=Path(ctx.skill_dir),
                traj_root=traj_root,
                config=config or {},
            )
    except Exception as error:  # noqa: BLE001 — job must end in failed, not crash thread
        logger.exception("generate job %s failed", job_id)
        _update_job(job_id, status="failed", error=str(error))


def run_claimed_generate_job(
    job: dict[str, Any],
    *,
    skill_dir: Path,
    config: dict | None,
    db_path: Path | None = None,
    logs_dir: Path | None = None,
    traj_root: Path | None = None,
) -> None:
    """agent-worker edit 池线程入口：认领后的 payload 跑完并写 status 文件。"""
    job_id = job["job_id"]
    with _JOBS_LOCK:
        stored = dict(job)
        stored.setdefault("status", "running")
        stored.setdefault("skill_names", [])
        stored.setdefault("pinned", [])
        stored.setdefault("error", "")
        _JOBS[job_id] = stored
    _update_job(job_id, status="running")
    _append_generate_log(
        stored.get("log_path") or job.get("log_path"),
        "generate running, starting agent\n",
    )
    try:
        from xskill.utils.rate_limit import request_source
        with request_source("generate"):
            _run_generate_job_body(
                get_job(job_id) or stored,
                skill_dir=Path(skill_dir),
                traj_root=Path(traj_root) if traj_root is not None else None,
                config=config or {},
                db_path=db_path,
                logs_dir=logs_dir,
            )
    except Exception as error:  # noqa: BLE001
        logger.exception("generate job %s failed", job_id)
        _update_job(job_id, status="failed", error=str(error))


def _run_generate_job_body(
    job: dict[str, Any],
    *,
    skill_dir: Path,
    traj_root: Path | None,
    config: dict,
    db_path: Path | None = None,
    logs_dir: Path | None = None,
    clients_db_path: Path | None = None,
) -> None:
    from xskill.agents import agent_tools
    from xskill.agents.agno_factory import make_default_factory
    from xskill.agents.generate_agent import GenerateAgent
    from xskill.config import get_logs_dir, get_registry_db_path

    skill_dir = Path(skill_dir)
    extra_roots = collect_read_roots(skill_dir, traj_root, db_path=db_path)
    blocked_roots = collect_blocked_traj_roots(
        traj_root, clients_db_path=clients_db_path,
    )
    extra_roots = exclude_blocked_read_roots(extra_roots, blocked_roots)
    logs_dir = Path(logs_dir) if logs_dir is not None else get_logs_dir()
    wiki_root = prepare_generate_wiki(logs_dir, job["user_id"], job["job_id"])
    extra_roots = list(extra_roots)
    extra_roots.append(wiki_root)
    spill_root = (
        logs_dir / "agents" / "generate_agents" / job["user_id"] / "spill" / job["job_id"]
    )
    spill_root.mkdir(parents=True, exist_ok=True)
    resolved_db = Path(db_path) if db_path is not None else get_registry_db_path()
    agent_tools.reset_generate_session()
    tool_context = agent_tools.create_agent_tool_context(
        skill_dir=skill_dir,
        data_dir=skill_dir,
        config=config,
        atom_skill_dir=skill_dir,
        default_traj_root=traj_root,
        spill_root=spill_root,
        extra_read_roots=tuple(extra_roots),
        generate_user_id=job["user_id"],
        wiki_root=wiki_root,
        registry_db_path=resolved_db,
        blocked_read_roots=blocked_roots,
    )
    llm_cfg = {**(config.get("llm") or {}), **(config.get("llm_skill") or {})}
    factory = make_default_factory(
        config, spill_root=spill_root,
    )
    agent = GenerateAgent(
        skill_dir=skill_dir,
        agno_agent_factory=factory,
        llm_cfg=llm_cfg,
        logs_dir=logs_dir,
        extra_read_roots=tuple(extra_roots),
    )
    token = agent_tools.bind_agent_tool_context(tool_context)
    try:
        agent.run(
            instruction=job["instruction"],
            user_id=job["user_id"],
            job_id=job["job_id"],
            preferred_names=job.get("preferred_names") or [],
        )
        skill_names = agent_tools.generate_committed_skills()
    finally:
        agent_tools.reset_agent_tool_context(token)

    if not skill_names:
        _update_job(
            job["job_id"],
            status="failed",
            error="generate 结束但没有 commit_generate_main 提交任何 skill",
        )
        return
    max_pinned = None
    try:
        from xskill.config import team_server_slots_config
        max_pinned = team_server_slots_config(config)["skill_slots"]
    except Exception:
        logger.debug("skill_slots unavailable for generate pin quota", exc_info=True)
    pinned = pin_generated_skills(
        user_id=job["user_id"],
        skill_names=skill_names,
        db_path=resolved_db,
        max_pinned=max_pinned,
    )
    _update_job(
        job["job_id"],
        status="succeeded",
        skill_names=skill_names,
        pinned=pinned,
        error="",
    )


def iter_job_events(
    job_id: str,
    *,
    poll_seconds: float = 0.2,
    ping_every: float = 15.0,
) -> Iterator[dict[str, Any]]:
    """Yield log chunks then a terminal event. Blocking generator.

    Stays open until the job leaves ``queued`` / ``running``. Quiet periods
    emit ping events so proxies and the CLI do not treat a long model call
    as death. Status is re-read from the status file so the web process can
    see the terminal state written by agent-worker.
    """
    job = get_job(job_id)
    if job is None:
        yield {"type": "done", "ok": False, "error": "unknown job_id"}
        return
    log_path = Path(job["log_path"])
    offset = 0
    last_emit = time.time()
    while True:
        try:
            data = log_path.read_bytes()
        except OSError:
            data = b""
        if len(data) > offset:
            chunk = data[offset:].decode("utf-8", errors="replace")
            offset = len(data)
            yield {"type": "log", "chunk": chunk}
            last_emit = time.time()
        current = get_job(job_id) or job
        if current.get("status") not in _ACTIVE_STATUSES:
            try:
                data = log_path.read_bytes()
            except OSError:
                data = b""
            if len(data) > offset:
                chunk = data[offset:].decode("utf-8", errors="replace")
                yield {"type": "log", "chunk": chunk}
            yield {
                "type": "done",
                "ok": current.get("status") == "succeeded",
                "skill_names": current.get("skill_names") or [],
                "pinned": current.get("pinned") or [],
                "error": current.get("error") or "",
            }
            return
        if time.time() - last_emit >= ping_every:
            yield {
                "type": "ping",
                "status": current.get("status") or "queued",
            }
            last_emit = time.time()
        time.sleep(poll_seconds)
