"""
api/sse.py -- Background task management with SSE streaming
==========================================================
Provides SSE (Server-Sent Events) endpoints for long-running operations:
  - /api/v1/trajectories/index   -- build/update trajectory index
  - /api/v1/skills/process       -- process a single trajectory into skill
  - /api/v1/skills/batch         -- batch process trajectories

Each endpoint runs the heavy work in a ThreadPoolExecutor and streams
progress, log, and result events back to the client via SSE.
"""

from __future__ import annotations

import asyncio
import json
import logging
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

from fastapi import APIRouter
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from xskill.config import load_config, get_skill_dir, get_traj_dir
from xskill.utils.llm import create_llm_client, create_embed_client

logger = logging.getLogger("xskill.tasks")
SPLIT_ATOM_TASK_STEP = "拆分 AtomTask"

# ---------------------------------------------------------------------------
# Thread pool shared across all SSE endpoints
# ---------------------------------------------------------------------------
_executor = ThreadPoolExecutor(max_workers=4)

# ---------------------------------------------------------------------------
# SSE helpers
# ---------------------------------------------------------------------------

def sse_event(event: str, data: dict) -> str:
    """Format a raw SSE event string.

    Example output::

        event: progress
        data: {"step": "meta提取", "current": 42, "total": 300}

    """
    payload = json.dumps(data, ensure_ascii=False)
    return f"event: {event}\ndata: {payload}\n\n"


def make_sse_log(queue: asyncio.Queue):
    """Return a *log_fn(msg, tag)* that pushes SSE events onto *queue*.

    The returned callable is compatible with :class:`xskill.log.StreamLog`
    (same ``(msg, tag)`` signature) but instead of printing to stdout it
    enqueues ``("log", {"tag": ..., "msg": ...})`` tuples that the SSE
    generator will pick up.
    """

    def log_fn(msg: str, tag: str = "info"):
        try:
            queue.put_nowait(("log", {"tag": tag, "msg": msg}))
        except Exception:
            pass  # queue full / closed — drop silently

    return log_fn


def _push(queue: asyncio.Queue, event: str, data: dict):
    """Convenience: put an (event, data) tuple onto the queue."""
    try:
        queue.put_nowait((event, data))
    except Exception:
        pass


def _finish(queue: asyncio.Queue, data: dict):
    """Push a ``result`` event followed by the sentinel ``None``."""
    _push(queue, "result", data)
    queue.put_nowait(None)


def _fail(queue: asyncio.Queue, error: str):
    """Push an ``error`` event followed by the sentinel ``None``."""
    _push(queue, "error", {"error": error})
    queue.put_nowait(None)


async def _event_generator(queue: asyncio.Queue):
    """Async generator that drains *queue* and yields SSE dicts."""
    while True:
        item = await queue.get()
        if item is None:
            break
        event, data = item
        yield {"event": event, "data": json.dumps(data, ensure_ascii=False)}


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class IndexRequest(BaseModel):
    path: Optional[str] = None
    dataset: Optional[str] = None
    concurrency: int = 10
    no_llm: bool = False


class ProcessRequest(BaseModel):
    traj_path: str
    dry_run: bool = False


class BatchRequest(BaseModel):
    path: Optional[str] = None
    dataset: Optional[str] = None
    max: Optional[int] = None
    dry_run: bool = False


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------
sse_router = APIRouter(prefix="/api/v1")


def _start_background(queue: asyncio.Queue, target):
    loop = asyncio.get_event_loop()
    loop.run_in_executor(_executor, target)
    return EventSourceResponse(_event_generator(queue))


def _resolve_dataset_dir(path: Optional[str], dataset: Optional[str]) -> Path:
    base_traj_dir = get_traj_dir()
    if path:
        return Path(path)
    if dataset:
        return base_traj_dir / dataset
    return base_traj_dir


def _trajectory_markdown_files(dataset_dir: Path, limit: Optional[int] = None) -> list[Path]:
    md_files = sorted(dataset_dir.glob("traj_*.md"))
    md_files = [f for f in md_files if not f.name.endswith(".meta")]
    if limit and limit > 0:
        return md_files[:limit]
    return md_files


def _progress(queue: asyncio.Queue, step: str, current: int, total: int,
              detail: Optional[str] = None):
    data = {"step": step, "current": current, "total": total}
    if detail is not None:
        data["detail"] = detail
    _push(queue, "progress", data)


def _run_index_task(req: IndexRequest, queue: asyncio.Queue):
    try:
        from xskill.pipeline.atom import AtomTaskStore
        from xskill.agents.task_agent import TaskAgent
        from xskill.agents.agno_factory import make_default_factory

        config = load_config()
        log_fn = make_sse_log(queue)
        dataset_dir = _resolve_dataset_dir(req.path, req.dataset)
        if not dataset_dir.is_dir():
            _fail(queue, f"directory not found: {dataset_dir}")
            return

        llm = None if req.no_llm else create_llm_client(config)
        embed_client = create_embed_client(config)
        store = AtomTaskStore(root=dataset_dir)
        md_files = _trajectory_markdown_files(dataset_dir)
        total = len(md_files)
        _progress(
            queue, SPLIT_ATOM_TASK_STEP, 0, total,
            f"{dataset_dir.name}, concurrency={req.concurrency}",
        )

        if llm is None:
            _fail(queue, "AtomTask 拆分需要 LLM；不要传 no_llm=true")
            return

        agent = TaskAgent(
            agno_agent_factory=make_default_factory(config),
            store=store, traj_root=dataset_dir, skill_dir=get_skill_dir(),
        )
        _split_index_trajectories(md_files, agent, log_fn, queue)
        _progress(queue, "重建向量索引", 0, 1)
        store.rebuild_vector_index(embed_client)
        _progress(queue, "重建向量索引", 1, 1)
        return _early_finish_index(queue, dataset_dir, total)
    except Exception as exc:
        logger.error("index task failed: %s", exc, exc_info=True)
        _fail(queue, f"{type(exc).__name__}: {exc}")


def _split_index_trajectories(md_files: list[Path], agent, log_fn, queue: asyncio.Queue):
    from xskill.pipeline.trajectory import validate_trajectory_source

    total = len(md_files)
    for idx, md in enumerate(md_files, 1):
        validation = validate_trajectory_source(md)
        if not validation.valid:
            log_fn(f"[{idx}/{total}] {md.name} filtered: {validation.reason}", "step")
            _progress(queue, SPLIT_ATOM_TASK_STEP, idx, total)
            continue
        try:
            atoms = agent.run(traj_id=md.stem, traj_path=md)
            log_fn(f"[{idx}/{total}] {md.name} -> {len(atoms)} atoms", "step")
        except Exception as e:
            log_fn(f"[{idx}/{total}] {md.name} 拆分失败: {e}", "error")
        _progress(queue, SPLIT_ATOM_TASK_STEP, idx, total)


# ===================================================================
# POST /api/v1/trajectories/index
# ===================================================================

@sse_router.post("/trajectories/index")
async def api_index(req: IndexRequest):
    """v2: 对一个目录跑 TaskAgent 拆 AtomTask + 整批重建向量索引。

    旧 v1 走 index_dataset（meta + index）已删除。新流水线下"索引"=
    AtomTask 索引；不再有独立的"meta 提取"阶段。
    """
    queue: asyncio.Queue = asyncio.Queue()
    return _start_background(queue, lambda: _run_index_task(req, queue))


def _early_finish_index(queue, dataset_dir, total):
    _finish(queue, {
        "status": "done",
        "dataset": str(dataset_dir),
        "trajectories": total,
    })


def _run_process_task(req: ProcessRequest, queue: asyncio.Queue):
    try:
        from xskill.pipeline.atom import AtomTaskStore
        from xskill.pipeline.trajectory import validate_trajectory_source
        from xskill.skill.git import ensure_repo

        config = load_config()
        log_fn = make_sse_log(queue)
        skill_dir = get_skill_dir()
        traj_path = Path(req.traj_path)
        if not traj_path.is_file():
            _fail(queue, f"traj file not found: {traj_path}")
            return
        validation = validate_trajectory_source(traj_path)
        if not validation.valid:
            _finish(queue, {
                "status": "filtered",
                "traj": traj_path.name,
                "reason": validation.reason,
            })
            return

        _progress(queue, "init", 0, 1, f"processing {traj_path.name}")
        ensure_repo(str(skill_dir))
        embed = create_embed_client(config)
        store = AtomTaskStore(root=traj_path.parent)
        atoms = _split_single_trajectory(config, skill_dir, store, traj_path, log_fn, queue)
        if req.dry_run:
            _finish(queue, {"status": "dry_run", "traj": traj_path.name,
                            "n_atoms": len(atoms)})
            return
        result = _process_single_atoms(config, skill_dir, store, embed, traj_path, atoms, queue, log_fn)
        _finish(queue, result)
    except Exception as exc:
        logger.error("process task failed: %s", exc, exc_info=True)
        _fail(queue, f"{type(exc).__name__}: {exc}")


def _split_single_trajectory(config: dict, skill_dir: Path, store, traj_path: Path,
                             log_fn, queue: asyncio.Queue) -> list:
    from xskill.agents.agno_factory import make_default_factory
    from xskill.agents.task_agent import TaskAgent

    _progress(queue, SPLIT_ATOM_TASK_STEP, 0, 1)
    atoms = TaskAgent(
        agno_agent_factory=make_default_factory(config),
        store=store, traj_root=traj_path.parent, skill_dir=skill_dir,
    ).run(traj_id=traj_path.stem, traj_path=traj_path)
    log_fn(f"拆出 {len(atoms)} 个 atom", "step")
    _progress(queue, SPLIT_ATOM_TASK_STEP, 1, 1)
    return atoms


def _process_single_atoms(config: dict, skill_dir: Path, store, embed, traj_path: Path,
                          atoms: list, queue: asyncio.Queue, log_fn) -> dict:
    from xskill.agents.agno_factory import make_default_factory
    from xskill.pipeline.runner import process_atom_task

    store.rebuild_vector_index(embed)
    log_fn("AtomTask 向量索引已重建", "step")
    factory = make_default_factory(config)
    atom_results = []
    edited_total: set[str] = set()
    for i, atom in enumerate(store.list_by_traj(traj_path.stem), 1):
        _progress(queue, "cluster + edit", i, len(atoms) or 1, atom.atom_id)
        res = process_atom_task(
            atom_id=atom.atom_id,
            config=config,
            skill_dir=skill_dir,
            store=store,
            embed_client=embed,
            agno_agent_factory=factory,
        )
        atom_results.append(res)
        edited_total.update(res.get("edited_skills") or [])
        log_fn(f"  {atom.atom_id} -> edited={res.get('edited_skills') or '-'}",
               "decision")
    return {
        "status": "done", "traj": traj_path.name,
        "n_atoms": len(atoms),
        "edited_skills": sorted(edited_total),
        "atom_results": atom_results,
    }


def _run_batch_task(req: BatchRequest, queue: asyncio.Queue):
    try:
        from xskill.pipeline.atom import AtomTaskStore
        from xskill.agents.task_agent import TaskAgent
        from xskill.agents.agno_factory import make_default_factory
        from xskill.skill.git import ensure_repo

        config = load_config()
        log_fn = make_sse_log(queue)
        skill_dir = get_skill_dir()
        dataset_dir = _resolve_dataset_dir(req.path, req.dataset)
        if not dataset_dir.is_dir():
            _fail(queue, f"directory not found: {dataset_dir}")
            return

        md_files = _trajectory_markdown_files(dataset_dir, req.max)
        total = len(md_files)
        if total == 0:
            _finish(queue, {"status": "done", "processed": 0,
                            "detail": "no trajectories found"})
            return

        _progress(queue, "batch", 0, total)
        ensure_repo(str(skill_dir))
        embed = create_embed_client(config)
        store = AtomTaskStore(root=dataset_dir)
        split_agent = TaskAgent(
            agno_agent_factory=make_default_factory(config),
            store=store, traj_root=dataset_dir, skill_dir=skill_dir,
        )
        _split_batch_trajectories(md_files, split_agent, log_fn)
        store.rebuild_vector_index(embed)
        log_fn("AtomTask 向量索引已重建", "step")

        if req.dry_run:
            _finish(queue, {"status": "dry_run",
                            "trajectories": total,
                            "atoms": sum(1 for _ in store.all_atoms())})
            return

        summary = _process_batch_atoms(config, skill_dir, store, embed, queue, log_fn)
        _finish(queue, {"status": "done", **summary, "trajectories": total})
    except Exception as exc:
        logger.error("batch task failed: %s", exc, exc_info=True)
        _fail(queue, f"{type(exc).__name__}: {exc}")


def _split_batch_trajectories(md_files: list[Path], split_agent, log_fn):
    from xskill.pipeline.trajectory import validate_trajectory_source

    total = len(md_files)
    for idx, md in enumerate(md_files, 1):
        validation = validate_trajectory_source(md)
        if not validation.valid:
            log_fn(f"[{idx}/{total}] filtered: {md.name}: {validation.reason}", "step")
            continue
        try:
            atoms = split_agent.run(traj_id=md.stem, traj_path=md)
            log_fn(f"[{idx}/{total}] split: {md.name} -> {len(atoms)} atoms", "step")
        except Exception as e:
            log_fn(f"[{idx}/{total}] split failed: {md.name}: {e}", "error")


def _process_batch_atoms(config: dict, skill_dir: Path, store, embed,
                         queue: asyncio.Queue, log_fn) -> dict:
    from xskill.agents.agno_factory import make_default_factory

    factory = make_default_factory(config)
    summary = {"clustered_atoms": 0, "edited_skills": set(), "errors": 0,
               "details": []}
    atoms_all = list(store.all_atoms())
    for j, atom in enumerate(atoms_all, 1):
        _progress(queue, "cluster", j, len(atoms_all), atom.atom_id)
        _process_batch_atom(
            atom, config, skill_dir, store, embed, factory, summary, log_fn,
        )
    summary["edited_skills"] = sorted(summary["edited_skills"])
    return summary


def _process_batch_atom(atom, config: dict, skill_dir: Path, store, embed,
                        factory, summary: dict, log_fn):
    from xskill.pipeline.runner import process_atom_task

    try:
        res = process_atom_task(
            atom_id=atom.atom_id, config=config,
            skill_dir=skill_dir, store=store,
            embed_client=embed, agno_agent_factory=factory,
        )
        summary["clustered_atoms"] += 1
        summary["edited_skills"].update(res.get("edited_skills") or [])
        summary["details"].append({
            "atom_id": atom.atom_id,
            "edited_skills": res.get("edited_skills") or [],
        })
    except Exception as e:
        summary["errors"] += 1
        summary["details"].append({"atom_id": atom.atom_id, "error": str(e)})
        log_fn(f"  cluster failed: {atom.atom_id}: {e}", "error")


# ===================================================================
# POST /api/v1/skills/process
# ===================================================================

@sse_router.post("/skills/process")
async def api_process(req: ProcessRequest):
    """v2: 单条 traj 的同步流水线 = 拆 atom + 重建索引 + 对每个 atom 跑 cluster + edit。

    旧 v1 的 process_traj（整篇喂 LLM → SkillAgent → eval）已删除。
    返回字段：
      {status, traj, n_atoms, edited_skills, atom_results}
    """
    queue: asyncio.Queue = asyncio.Queue()
    return _start_background(queue, lambda: _run_process_task(req, queue))


# ===================================================================
# POST /api/v1/skills/batch
# ===================================================================

@sse_router.post("/skills/batch")
async def api_batch(req: BatchRequest):
    queue: asyncio.Queue = asyncio.Queue()
    return _start_background(queue, lambda: _run_batch_task(req, queue))
