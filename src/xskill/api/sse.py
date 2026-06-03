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

logger = logging.getLogger("tasks")

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

    def run():
        try:
            from xskill.pipeline.atom import AtomTaskStore
            from xskill.agents.task_agent import TaskAgent
            from xskill.agents.agno_factory import make_default_factory
            from xskill.pipeline.trajectory import validate_trajectory_source

            config = load_config()
            log_fn = make_sse_log(queue)

            base_traj_dir = get_traj_dir()
            if req.path:
                dataset_dir = Path(req.path)
            elif req.dataset:
                dataset_dir = base_traj_dir / req.dataset
            else:
                dataset_dir = base_traj_dir

            if not dataset_dir.is_dir():
                _fail(queue, f"directory not found: {dataset_dir}")
                return

            llm = None if req.no_llm else create_llm_client(config)
            embed_client = create_embed_client(config)
            store = AtomTaskStore(root=dataset_dir)

            md_files = sorted(dataset_dir.glob("traj_*.md"))
            md_files = [f for f in md_files if not f.name.endswith(".meta")]
            total = len(md_files)
            _push(queue, "progress", {
                "step": "拆分 AtomTask",
                "current": 0,
                "total": total,
                "detail": f"{dataset_dir.name}, concurrency={req.concurrency}",
            })

            if llm is None:
                _fail(queue, "AtomTask 拆分需要 LLM；不要传 no_llm=true")
                return

            agent = TaskAgent(
                agno_agent_factory=make_default_factory(config),
                store=store, traj_root=dataset_dir, skill_dir=get_skill_dir(),
            )
            for idx, md in enumerate(md_files, 1):
                validation = validate_trajectory_source(md)
                if not validation.valid:
                    log_fn(
                        f"[{idx}/{total}] {md.name} filtered: {validation.reason}",
                        "step",
                    )
                    _push(queue, "progress", {
                        "step": "拆分 AtomTask",
                        "current": idx,
                        "total": total,
                    })
                    continue
                try:
                    atoms = agent.run(traj_id=md.stem, traj_path=md)
                    log_fn(f"[{idx}/{total}] {md.name} -> {len(atoms)} atoms", "step")
                except Exception as e:
                    log_fn(f"[{idx}/{total}] {md.name} 拆分失败: {e}", "error")
                _push(queue, "progress", {
                    "step": "拆分 AtomTask",
                    "current": idx,
                    "total": total,
                })

            _push(queue, "progress", {"step": "重建向量索引", "current": 0, "total": 1})
            store.rebuild_vector_index(embed_client)
            _push(queue, "progress", {"step": "重建向量索引", "current": 1, "total": 1})

            return _early_finish_index(queue, dataset_dir, total)
        except Exception as exc:
            logger.error("index task failed: %s", exc, exc_info=True)
            _fail(queue, f"{type(exc).__name__}: {exc}")

    loop = asyncio.get_event_loop()
    loop.run_in_executor(_executor, run)

    return EventSourceResponse(_event_generator(queue))


def _early_finish_index(queue, dataset_dir, total):
    _finish(queue, {
        "status": "done",
        "dataset": str(dataset_dir),
        "trajectories": total,
    })


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

    def run():
        try:
            from xskill.pipeline.atom import AtomTaskStore
            from xskill.agents.task_agent import TaskAgent
            from xskill.pipeline.runner import process_atom_task
            from xskill.pipeline.trajectory import validate_trajectory_source
            from xskill.agents.agno_factory import make_default_factory
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

            _push(queue, "progress", {
                "step": "init", "current": 0, "total": 1,
                "detail": f"processing {traj_path.name}",
            })

            ensure_repo(str(skill_dir))
            embed = create_embed_client(config)
            store = AtomTaskStore(root=traj_path.parent)

            _push(queue, "progress", {"step": "拆分 AtomTask", "current": 0, "total": 1})
            atoms = TaskAgent(
                agno_agent_factory=make_default_factory(config),
                store=store, traj_root=traj_path.parent, skill_dir=skill_dir,
            ).run(traj_id=traj_path.stem, traj_path=traj_path)
            log_fn(f"拆出 {len(atoms)} 个 atom", "step")
            _push(queue, "progress", {"step": "拆分 AtomTask", "current": 1, "total": 1})

            store.rebuild_vector_index(embed)
            log_fn("AtomTask 向量索引已重建", "step")

            if req.dry_run:
                _finish(queue, {"status": "dry_run", "traj": traj_path.name,
                                "n_atoms": len(atoms)})
                return

            factory = make_default_factory(config)
            atom_results = []
            edited_total: set[str] = set()
            for i, atom in enumerate(store.list_by_traj(traj_path.stem), 1):
                _push(queue, "progress", {
                    "step": "cluster + edit",
                    "current": i, "total": len(atoms) or 1,
                    "detail": atom.atom_id,
                })
                res = process_atom_task(
                    atom_id=atom.atom_id,
                    config=config,
                    skill_dir=skill_dir,
                    store=store,
                    embed_client=embed,
                    agno_agent_factory=factory,
                )
                atom_results.append(res)
                for s in res.get("edited_skills") or []:
                    edited_total.add(s)
                log_fn(f"  {atom.atom_id} -> edited={res.get('edited_skills') or '-'}",
                       "decision")

            _finish(queue, {
                "status": "done", "traj": traj_path.name,
                "n_atoms": len(atoms),
                "edited_skills": sorted(edited_total),
                "atom_results": atom_results,
            })
        except Exception as exc:
            logger.error("process task failed: %s", exc, exc_info=True)
            _fail(queue, f"{type(exc).__name__}: {exc}")

    loop = asyncio.get_event_loop()
    loop.run_in_executor(_executor, run)

    return EventSourceResponse(_event_generator(queue))


# ===================================================================
# POST /api/v1/skills/batch
# ===================================================================

@sse_router.post("/skills/batch")
async def api_batch(req: BatchRequest):
    queue: asyncio.Queue = asyncio.Queue()

    def run():
        try:
            from xskill.pipeline.atom import AtomTaskStore
            from xskill.agents.task_agent import TaskAgent
            from xskill.pipeline.runner import process_atom_task
            from xskill.pipeline.trajectory import validate_trajectory_source
            from xskill.agents.agno_factory import make_default_factory
            from xskill.skill.git import ensure_repo

            config = load_config()
            log_fn = make_sse_log(queue)
            skill_dir = get_skill_dir()
            base_traj_dir = get_traj_dir()

            if req.path:
                dataset_dir = Path(req.path)
            elif req.dataset:
                dataset_dir = base_traj_dir / req.dataset
            else:
                dataset_dir = base_traj_dir

            if not dataset_dir.is_dir():
                _fail(queue, f"directory not found: {dataset_dir}")
                return

            md_files = sorted(dataset_dir.glob("traj_*.md"))
            md_files = [f for f in md_files if not f.name.endswith(".meta")]
            if req.max and req.max > 0:
                md_files = md_files[: req.max]
            total = len(md_files)
            if total == 0:
                _finish(queue, {"status": "done", "processed": 0,
                                "detail": "no trajectories found"})
                return

            _push(queue, "progress", {"step": "batch", "current": 0, "total": total})
            ensure_repo(str(skill_dir))
            embed = create_embed_client(config)
            store = AtomTaskStore(root=dataset_dir)
            split_agent = TaskAgent(
                agno_agent_factory=make_default_factory(config),
                store=store, traj_root=dataset_dir, skill_dir=skill_dir,
            )

            # Phase 1: 整批拆 atom + 重建索引
            for idx, md in enumerate(md_files, 1):
                validation = validate_trajectory_source(md)
                if not validation.valid:
                    log_fn(
                        f"[{idx}/{total}] filtered: {md.name}: {validation.reason}",
                        "step",
                    )
                    continue
                try:
                    atoms = split_agent.run(traj_id=md.stem, traj_path=md)
                    log_fn(f"[{idx}/{total}] split: {md.name} -> {len(atoms)} atoms",
                           "step")
                except Exception as e:
                    log_fn(f"[{idx}/{total}] split failed: {md.name}: {e}", "error")
            store.rebuild_vector_index(embed)
            log_fn("AtomTask 向量索引已重建", "step")

            if req.dry_run:
                _finish(queue, {"status": "dry_run",
                                "trajectories": total,
                                "atoms": sum(1 for _ in store.all_atoms())})
                return

            # Phase 2: 对每个 atom 跑 cluster + edit
            factory = make_default_factory(config)
            summary = {"clustered_atoms": 0, "edited_skills": set(), "errors": 0,
                       "details": []}
            atoms_all = list(store.all_atoms())
            for j, atom in enumerate(atoms_all, 1):
                _push(queue, "progress", {
                    "step": "cluster", "current": j,
                    "total": len(atoms_all), "detail": atom.atom_id,
                })
                try:
                    res = process_atom_task(
                        atom_id=atom.atom_id, config=config,
                        skill_dir=skill_dir, store=store,
                        embed_client=embed, agno_agent_factory=factory,
                    )
                    summary["clustered_atoms"] += 1
                    for s in res.get("edited_skills") or []:
                        summary["edited_skills"].add(s)
                    summary["details"].append({
                        "atom_id": atom.atom_id,
                        "edited_skills": res.get("edited_skills") or [],
                    })
                except Exception as e:
                    summary["errors"] += 1
                    summary["details"].append({
                        "atom_id": atom.atom_id, "error": str(e),
                    })
                    log_fn(f"  cluster failed: {atom.atom_id}: {e}", "error")

            summary["edited_skills"] = sorted(summary["edited_skills"])
            _finish(queue, {"status": "done", **summary,
                            "trajectories": total})
        except Exception as exc:
            logger.error("batch task failed: %s", exc, exc_info=True)
            _fail(queue, f"{type(exc).__name__}: {exc}")

    loop = asyncio.get_event_loop()
    loop.run_in_executor(_executor, run)

    return EventSourceResponse(_event_generator(queue))
