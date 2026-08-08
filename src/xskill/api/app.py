"""
api/app.py -- FastAPI application (SHORT operation endpoints)
=============================================================
Non-SSE REST endpoints for trajectory search, skill CRUD, and system operations.

Usage:
    from xskill.api.app import create_app
    app = create_app()
"""

from __future__ import annotations

# Upgrade sqlite3 to support RETURNING clause (needed by Agno session DB)
import sys as _sys
try:
    __import__("pysqlite3")
    _sys.modules["sqlite3"] = _sys.modules.pop("pysqlite3")
except ImportError:
    pass

import asyncio
import logging
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

from dulwich.errors import NotGitRepository
from fastapi import APIRouter, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from dulwich.errors import NotGitRepository

from xskill import __version__
from xskill.config import load_config, get_skill_dir
from xskill.utils.search import search as search_trajs, search_all as search_trajs_all
from xskill.skill.repo import (
    import_skill,
    list_skills,
    rebuild_skill_index,
    search_skill_index,
)
from xskill.skill.skill import (
    show_skill,
    skill_log,
    skill_diff,
    rollback_skill,
    freeze_skill,
    unfreeze_skill,
    delete_skill,
    export_skill,
)
from xskill.agents.agent_tools import (
    init_skill_authoring_tool_context,
)
from xskill.utils.llm import create_llm_client, create_embed_client
from xskill.skill.git import ensure_repo, current_branch

logger = logging.getLogger("xskill.server")

_CONTROL_PLANE_EXECUTOR_STATE = "xskill_control_plane_executor"


def _start_control_plane_executor(app: FastAPI) -> ThreadPoolExecutor:
    """为单个 app 创建控制面线程池。

    app factory 在测试和嵌入式部署中可能被调用多次，因此不能把
    executor 放在模块级共享。startup 每次都会创建新实例，shutdown 对称回收。
    """
    existing = getattr(app.state, _CONTROL_PLANE_EXECUTOR_STATE, None)
    if existing is not None:
        return existing
    executor = ThreadPoolExecutor(
        max_workers=4,
        thread_name_prefix="xskill-control-plane",
    )
    setattr(app.state, _CONTROL_PLANE_EXECUTOR_STATE, executor)
    return executor


def _stop_control_plane_executor(app: FastAPI) -> None:
    executor = getattr(app.state, _CONTROL_PLANE_EXECUTOR_STATE, None)
    if executor is None:
        return
    delattr(app.state, _CONTROL_PLANE_EXECUTOR_STATE)
    executor.shutdown(wait=True, cancel_futures=True)


async def _run_control_plane(func, app: FastAPI | None = None):
    """在不依赖 anyio 默认线程池的线程中执行同步读取。

    正常服务走 app 专用 executor。不执行 lifespan 的 ASGI 单测和直接调用
    走 asyncio 默认 executor，由事件循环负责回收，避免单测泄漏专用线程。
    """
    executor = (
        getattr(app.state, _CONTROL_PLANE_EXECUTOR_STATE, None)
        if app is not None else None
    )
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(executor, func)

# ---------------------------------------------------------------------------
# Module-level config -- lazy loaded
# ---------------------------------------------------------------------------
# 之前是 import 时就跑 ``_config = load_config()``，会让"导入 xskill.api.app"
# 这件本来无副作用的事情强依赖 ``~/.xskill/config.yaml``。CI runner 上没这个
# 文件 → 所有间接 import xskill.api.app 的测试 collection 都炸。
#
# 改成 lazy：占位 None；``_ensure_loaded()`` 在 ``create_app()`` 入口 + 每个
# server 启动路径首次调用时填充。endpoints 在 startup hook 之后才被 hit，
# 拿到的就是非 None；测试如果只 import ``_exec_tool`` / 常量，模块加载阶段
# 完全不读 config。
_config: dict | None = None
_skill_dir: Path | None = None
# 画像拆为短命子进程后 web 进程不再放常驻 service(恒无 "instance");dashboard 散点
# 端点仍读它:无 instance → 内联物化(画像子进程也会预物化进 scatter 缓存)。
_profile_refresh_ref: dict = {}
# 定时短命子进程调度器(画像 profile-refresh 等);startup 起、shutdown 停。
_schedulers: list = []

# debug 模式：把生态扫描的 home_root 指向用户自选目录，不扫真正的 $HOME。
# 用法：xskill serve --debug --home /tmp/test-home → 只扫
# /tmp/test-home/.claude/projects/*.jsonl，install 也走 /tmp/test-home/.claude/skills/。
_home_root_override: Path | None = None


def _home_root() -> Path:
    """生态层（ecosystems + ingester + install）应该用的 home root。

    debug 模式下指向 ``--home`` 自选目录；否则就是真实 ``$HOME``。
    """
    return _home_root_override if _home_root_override is not None else Path.home()


def _ensure_loaded() -> None:
    """幂等：第一次调用时载入配置 + 解析关键目录，之后是 no-op。

    server 内部的 endpoint / startup / chat 等代码路径都通过模块级
    ``_config`` / ``_skill_dir`` 等访问，这里只负责把 None 占位填上。
    """
    global _config, _skill_dir
    if _config is not None:
        return
    _config = load_config()
    _skill_dir = get_skill_dir()

# ---------------------------------------------------------------------------
# Pydantic request / response models
# ---------------------------------------------------------------------------


# -- Trajectories --

class TrajectorySearchRequest(BaseModel):
    query: str
    dataset_dir: Optional[str] = None
    top_k: int = Field(default=5, ge=1, le=100)
    filter: str = Field(default="all", pattern="^(all|success|failure)$")


class TrajectorySearchResult(BaseModel):
    traj_id: str
    similarity: float
    meta: dict = {}
    md_path: str = ""


class TrajectorySearchResponse(BaseModel):
    results: list[TrajectorySearchResult]
    count: int


# -- Skills --

class SkillSummary(BaseModel):
    name: str
    version: int = 0
    eval_score: Optional[float] = None
    tags: list[str] = []
    frozen: bool = False


class SkillListResponse(BaseModel):
    skills: list[SkillSummary]
    count: int


class SkillDetailResponse(BaseModel):
    name: str
    description: str = ""
    metadata: dict = {}
    skill_md_body: str = ""    # body AFTER the frontmatter
    skill_md_raw: str = ""     # full raw SKILL.md including frontmatter
    files: list[str] = []


class SkillLogResponse(BaseModel):
    name: str
    log: str


class SkillDiffResponse(BaseModel):
    name: str
    diff: str


class RollbackRequest(BaseModel):
    version: Optional[str] = None


class ImportSkillRequest(BaseModel):
    source_path: str


class SkillSearchRequest(BaseModel):
    query: str
    top_k: int = Field(default=5, ge=1, le=100)


class SkillResolveRequest(BaseModel):
    query: str
    accept_staging: bool = True


# -- System --

class HealthResponse(BaseModel):
    status: str
    version: str


class StatusResponse(BaseModel):
    skill_dir: str
    skill_count: int
    git_branch: Optional[str] = None


class InitRequest(BaseModel):
    path: Optional[str] = None


class MessageResponse(BaseModel):
    message: str
    ok: bool = True


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------
router = APIRouter(prefix="/api/v1")


# ---- Trajectories --------------------------------------------------------

@router.post("/trajectories/search", response_model=TrajectorySearchResponse)
def api_search_trajectories(req: TrajectorySearchRequest):
    """Search for similar trajectories in the dataset index.

    同步 def（而非 async def）：内部经 embed_client 走同步 httpx（最长 60s），
    写成 async 会把整个事件循环冻住——embedding 后端一慢，所有端点集体
    "连不上"。def 路由 FastAPI 自动丢 anyio 线程池，事件循环保持响应。

    When ``dataset_dir`` is omitted, searches across **all registered
    directories** via :func:`search_all`.
    """
    try:
        if req.dataset_dir:
            dataset_dir = Path(req.dataset_dir)
            if not dataset_dir.is_dir():
                raise HTTPException(status_code=404, detail=f"Dataset directory not found: {dataset_dir}")
            results = search_trajs(
                dataset_dir=dataset_dir,
                query_text=req.query,
                top_k=req.top_k,
                success_filter=req.filter,
                config=_config,
            )
        else:
            results = search_trajs_all(
                query_text=req.query,
                top_k=req.top_k,
                success_filter=req.filter,
                config=_config,
            )
        items = [
            TrajectorySearchResult(
                traj_id=r["traj_id"],
                similarity=r["similarity"],
                meta=r.get("meta", {}),
                md_path=r.get("md_path", ""),
            )
            for r in results
        ]
        return TrajectorySearchResponse(results=items, count=len(items))
    except HTTPException:
        raise
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.exception("trajectory search failed")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/trajectories/content")
async def api_trajectory_content(path: str):
    """Read trajectory file content. Also returns .meta if available."""
    p = Path(path)
    if not p.is_file():
        raise HTTPException(status_code=404, detail=f"File not found: {path}")
    # Security: only allow reading within known traj dirs
    try:
        resolved = p.resolve()
        allowed = False
        from xskill.pipeline.registry import list_watch_dirs
        for d in list_watch_dirs():
            if str(resolved).startswith(str(Path(d["path"]).resolve())):
                allowed = True
                break
        if not allowed:
            raise HTTPException(status_code=403, detail="Access denied")
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=403, detail="Access denied")

    content = p.read_text(encoding="utf-8")[:20000]
    meta = None
    meta_path = p.parent / f"{p.name}.meta"
    if meta_path.is_file():
        import json as _json
        try:
            meta = _json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            meta = None
    return {"path": str(p), "content": content, "meta": meta}


# ---- Skills CRUD ---------------------------------------------------------

@router.get("/skills", response_model=SkillListResponse)
async def api_list_skills():
    """List all skills and their status."""
    try:
        skills = list_skills(_skill_dir)
        items = [SkillSummary(**s) for s in skills]
        return SkillListResponse(skills=items, count=len(items))
    except Exception as e:
        logger.exception("list skills failed")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/skills/{name}", response_model=SkillDetailResponse)
async def api_show_skill(name: str):
    """Show skill details: description, metadata, and raw SKILL.md body."""
    try:
        result = show_skill(_skill_dir, name)
        if "error" in result:
            raise HTTPException(status_code=404, detail=result["error"])
        return SkillDetailResponse(**result)
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("show skill failed")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/skills/{name}/log", response_model=SkillLogResponse)
async def api_skill_log(name: str):
    """Return the git log for a skill."""
    try:
        log_text = skill_log(_skill_dir, name)
        if log_text.startswith("skill not found"):
            raise HTTPException(status_code=404, detail=log_text)
        return SkillLogResponse(name=name, log=log_text)
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("skill log failed")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/skills/{name}/diff", response_model=SkillDiffResponse)
async def api_skill_diff(name: str, v1: Optional[str] = None, v2: Optional[str] = None):
    """Return the diff for a skill between two versions."""
    try:
        diff_text = skill_diff(_skill_dir, name, v1=v1, v2=v2)
        if diff_text.startswith("skill not found"):
            raise HTTPException(status_code=404, detail=diff_text)
        return SkillDiffResponse(name=name, diff=diff_text)
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("skill diff failed")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/skills/{name}/rollback", response_model=MessageResponse)
async def api_rollback_skill(name: str, req: RollbackRequest):
    """Rollback a skill to a specific version or to the previous version."""
    try:
        ok = rollback_skill(_skill_dir, name, version=req.version)
        if not ok:
            raise HTTPException(status_code=400, detail=f"Rollback failed for skill: {name}")
        target = req.version or "previous version"
        return MessageResponse(message=f"Rolled back {name} to {target}", ok=True)
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("rollback failed")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/skills/{name}/freeze", response_model=MessageResponse)
async def api_freeze_skill(name: str):
    """Freeze a skill so it is not auto-updated by batch runs."""
    try:
        ok = freeze_skill(_skill_dir, name)
        if not ok:
            raise HTTPException(status_code=400, detail=f"Freeze failed for skill: {name}")
        return MessageResponse(message=f"Frozen: {name}", ok=True)
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("freeze failed")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/skills/{name}/unfreeze", response_model=MessageResponse)
async def api_unfreeze_skill(name: str):
    """Unfreeze a skill to allow auto-updates."""
    try:
        ok = unfreeze_skill(_skill_dir, name)
        if not ok:
            raise HTTPException(status_code=400, detail=f"Unfreeze failed for skill: {name}")
        return MessageResponse(message=f"Unfrozen: {name}", ok=True)
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("unfreeze failed")
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/skills/{name}", response_model=MessageResponse)
async def api_delete_skill(name: str):
    """Delete a skill and commit the change."""
    try:
        ok = delete_skill(_skill_dir, name)
        if not ok:
            raise HTTPException(status_code=404, detail=f"Skill not found or delete failed: {name}")
        return MessageResponse(message=f"Deleted: {name}", ok=True)
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("delete failed")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/skills/{name}/export")
async def api_export_skill(name: str):
    """Export a skill directory as a downloadable archive."""
    try:
        tmp_dir = Path(tempfile.mkdtemp())
        export_skill(_skill_dir, name, tmp_dir)
        # Tar it up for download
        import shutil
        archive_path = Path(tempfile.mkdtemp()) / f"{name}.tar.gz"
        shutil.make_archive(
            str(archive_path).replace(".tar.gz", ""),
            "gztar",
            root_dir=str(tmp_dir),
            base_dir=name,
        )
        return FileResponse(
            path=str(archive_path),
            filename=f"{name}.tar.gz",
            media_type="application/gzip",
        )
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.exception("export failed")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/skills/import", response_model=MessageResponse)
async def api_import_skill(req: ImportSkillRequest):
    """Import a skill from a source directory path."""
    try:
        source = Path(req.source_path)
        if not source.is_dir():
            raise HTTPException(status_code=404, detail=f"Source path not found: {req.source_path}")
        name = import_skill(_skill_dir, source)
        return MessageResponse(message=f"Imported: {name}", ok=True)
    except HTTPException:
        raise
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.exception("import failed")
        raise HTTPException(status_code=500, detail=str(e))


# ---- Skill Search --------------------------------------------------------

@router.post("/skills/search")
def api_search_skills(req: SkillSearchRequest):
    """Search existing skills by semantic similarity.

    同步 def：embed 是同步网络调用，见 api_search_trajectories 的说明。
    """
    try:
        if not (_skill_dir / ".skill_index.pkl").exists():
            return []
        embedding_client = create_embed_client(_config)
        return search_skill_index(
            skill_dir=_skill_dir,
            query=req.query,
            embed_client=embedding_client,
            top_k=req.top_k,
        )
    except Exception as e:
        logger.exception("skill search failed")
        raise HTTPException(status_code=500, detail=str(e))


# ---- Skill Resolve (canary-aware) ----------------------------------------

@router.post("/skills/resolve")
def api_resolve_skill(req: SkillResolveRequest):
    """搜索 skill + canary 分流，返回 agent 应该读取的路径。

    ``accept_staging=True`` 时，若 skill 有活跃 staging 分支，按 80/20 概率
    决定返回 main 路径还是 ``.canary/`` 物化路径。

    同步 def：embed 是同步网络调用，见 api_search_trajectories 的说明。
    """
    from xskill import canary
    import time

    try:
        embedding_client = create_embed_client(_config)
        hits = search_skill_index(
            skill_dir=_skill_dir,
            query=req.query,
            embed_client=embedding_client,
            top_k=1,
        )
        if not hits:
            return {"skill_name": None, "path": None, "side": "none", "sha": ""}

        skill_name = hits[0].get("skill_name") or hits[0].get("name", "")
        if not skill_name:
            return {"skill_name": None, "path": None, "side": "none", "sha": ""}

        sd = _skill_dir / skill_name
        if not sd.is_dir():
            return {"skill_name": skill_name, "path": None, "side": "none", "sha": ""}

        canary_cfg_raw = _config.get("canary", {})
        cfg = canary.CanaryConfig.from_dict(canary_cfg_raw)

        side = "main"
        if req.accept_staging and canary.has_staging(sd):
            traj_id = f"resolve_{time.time_ns()}"
            side = canary.pick_side(traj_id, skill_name, cfg.probability)

        canary_root = _skill_dir / ".canary"

        if side == "staging":
            # 确保物化目录存在
            canary_path = canary_root / skill_name
            if not (canary_path / "SKILL.md").is_file():
                canary.materialize_staging(sd, canary_root)
            path = str(canary_root / skill_name)
        else:
            path = str(sd)

        sha = canary.staging_sha(sd) if side == "staging" else canary.main_sha(sd)

        return {
            "skill_name": skill_name,
            "path": path,
            "side": side,
            "sha": (sha or "")[:8],
        }
    except Exception as e:
        logger.exception("resolve skill failed")
        raise HTTPException(status_code=500, detail=str(e))


# ---- Candidates + Canary -------------------------------------------------

@router.get("/skills/{name}/candidates")
async def api_skill_candidates(name: str):
    """返回 .candidates.yml 内容。"""
    from xskill.skill.candidates import load_candidates
    sd = _skill_dir / name
    if not sd.is_dir():
        raise HTTPException(status_code=404, detail=f"skill not found: {name}")
    data = load_candidates(sd)
    candidates = data.get("candidates", [])
    return {"skill_name": name, "candidates": candidates, "count": len(candidates)}


@router.get("/skills/{name}/canary")
async def api_skill_canary(name: str):
    """返回某 skill 的灰度状态：staging 有无、ux_scores 汇总、判定结果预览。"""
    from xskill import canary
    sd = _skill_dir / name
    if not sd.is_dir():
        raise HTTPException(status_code=404, detail=f"skill not found: {name}")

    has_stg = canary.has_staging(sd)
    m_sha = canary.main_sha(sd)
    s_sha = canary.staging_sha(sd) if has_stg else None
    created = None
    if has_stg:
        dt = canary.staging_created_at(sd)
        if dt:
            created = dt.isoformat()

    scores = canary.load_ux_scores(sd)
    main_scores = [s for s in scores if s.get("side") == "main"]
    staging_scores = [s for s in scores if s.get("side") == "staging"]

    canary_cfg_raw = _config.get("canary", {})
    cfg = canary.CanaryConfig.from_dict(canary_cfg_raw)

    main_body = canary.read_skill_on_branch(sd, "main")
    staging_body = canary.read_skill_on_branch(sd, "staging") if has_stg else None

    return {
        "skill_name": name,
        "has_staging": has_stg,
        "main_sha": m_sha,
        "staging_sha": s_sha,
        "staging_created_at": created,
        "main_body_preview": (main_body or "")[:500],
        "staging_body_preview": (staging_body or "")[:500],
        "ux_scores": {
            "main": main_scores,
            "staging": staging_scores,
        },
        "config": {
            "probability": cfg.probability,
            "min_samples": cfg.min_samples,
            "max_days_hold": cfg.max_days_hold,
        },
    }


@router.get("/canary/overview")
async def api_canary_overview():
    """返回所有 skill 的灰度状态一览。"""
    from xskill import canary
    items = []
    if not _skill_dir.is_dir():
        return {"skills": [], "count": 0}
    for d in sorted(_skill_dir.iterdir()):
        if not d.is_dir() or d.name.startswith("."):
            continue
        has_stg = canary.has_staging(d)
        scores = canary.load_ux_scores(d)
        main_scores = [s["score"] for s in scores if s.get("side") == "main"]
        stg_scores = [s["score"] for s in scores if s.get("side") == "staging"]
        items.append({
            "skill_name": d.name,
            "has_staging": has_stg,
            "main_avg": round(sum(main_scores) / len(main_scores), 2) if main_scores else None,
            "staging_avg": round(sum(stg_scores) / len(stg_scores), 2) if stg_scores else None,
            "main_n": len(main_scores),
            "staging_n": len(stg_scores),
        })
    return {"skills": items, "count": len(items)}


# ---- Registry + Watcher --------------------------------------------------

@router.get("/registry/dirs")
async def api_list_registry_dirs():
    """List all registered watch directories with trajectory counts."""
    from xskill.pipeline.registry import list_watch_dirs
    dirs = list_watch_dirs()
    return {"dirs": dirs, "count": len(dirs)}


@router.post("/registry/dirs")
async def api_register_dir(req: dict):
    """Register a directory for watching."""
    from xskill.pipeline.registry import register_dir
    path = req.get("path", "")
    label = req.get("label", "")
    if not path:
        raise HTTPException(status_code=400, detail="path is required")
    p = Path(path)
    if not p.is_dir():
        raise HTTPException(status_code=404, detail=f"Not a directory: {path}")
    wid = register_dir(p, label=label)
    return {"id": wid, "path": str(p.resolve()), "ok": True}


@router.delete("/registry/dirs")
async def api_unregister_dir(req: dict):
    """Unregister a directory."""
    from xskill.pipeline.registry import unregister_dir
    path = req.get("path", "")
    if not path:
        raise HTTPException(status_code=400, detail="path is required")
    ok = unregister_dir(path)
    if not ok:
        raise HTTPException(status_code=404, detail="Directory not found in registry")
    return {"ok": True}


@router.get("/trajectories/logs")
async def api_trajectory_logs(filename: str, dir: str = ""):
    """Return stored process logs for a trajectory."""
    from xskill.pipeline.registry import list_watch_dirs, pooled_connection
    import json as _json

    dirs = list_watch_dirs()
    for d in dirs:
        if dir and d["path"] != dir:
            continue
        with pooled_connection() as conn:
            row = conn.execute(
                "SELECT process_log FROM trajectories"
                " WHERE watch_dir_id=? AND filename=?",
                (d["id"], filename),
            ).fetchone()
            if row and row["process_log"]:
                try:
                    entries = _json.loads(row["process_log"])
                except Exception:
                    entries = [{"t": "", "tag": "raw", "msg": row["process_log"]}]
                return {"logs": entries, "filename": filename}
    return {"logs": [], "filename": filename}


@router.get("/trajectories/list")
async def api_list_trajectories():
    """List all trajectories across registered directories with full status."""
    from xskill.pipeline.registry import list_watch_dirs, pooled_connection, get_status_counts
    dirs = list_watch_dirs()
    all_trajs = []
    for d in dirs:
        with pooled_connection() as conn:
            rows = conn.execute(
                "SELECT filename, has_meta, has_embedding, status, process_action,"
                " skill_generated, skill_used, canary_side, ux_score, error_msg,"
                " retry_count, discovered_at, indexed_at, updated_at"
                " FROM trajectories WHERE watch_dir_id=? ORDER BY discovered_at DESC",
                (d["id"],),
            ).fetchall()
            for r in rows:
                all_trajs.append({
                    "filename": r["filename"],
                    "dir": d["path"],
                    "dir_label": d["label"],
                    "status": r["status"] or "discovered",
                    "process_action": r["process_action"],
                    "skill_generated": r["skill_generated"],
                    "has_meta": bool(r["has_meta"]),
                    "has_embedding": bool(r["has_embedding"]),
                    "skill_used": r["skill_used"],
                    "canary_side": r["canary_side"],
                    "ux_score": r["ux_score"],
                    "error_msg": r["error_msg"],
                    "retry_count": r["retry_count"] or 0,
                    "discovered_at": r["discovered_at"],
                    "indexed_at": r["indexed_at"],
                    "updated_at": r["updated_at"],
                })
    status_counts = get_status_counts()
    return {"trajectories": all_trajs, "count": len(all_trajs), "status_counts": status_counts}


# ---- System --------------------------------------------------------------

@router.get("/health", response_model=HealthResponse)
async def api_health():
    """Health check endpoint."""
    return HealthResponse(status="ok", version=__version__)


@router.get("/status", response_model=StatusResponse)
async def api_status(request: Request = None):
    """Return system status: skill dir, skill count, git branch."""
    def _read_status():
        skills = list_skills(_skill_dir)
        try:
            branch = current_branch(str(_skill_dir))
        except NotGitRepository:
            branch = None
        return StatusResponse(
            skill_dir=str(_skill_dir),
            skill_count=len(skills),
            git_branch=branch,
        )

    return await _run_control_plane(
        _read_status,
        request.app if request is not None else None,
    )


@router.post("/init", response_model=MessageResponse)
async def api_init(req: InitRequest):
    """Initialize the skill git repository."""
    try:
        target = req.path or str(_skill_dir)
        ensure_repo(target)
        return MessageResponse(message=f"Initialized skill repo at: {target}", ok=True)
    except Exception as e:
        logger.exception("init failed")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/reindex", response_model=MessageResponse)
def api_reindex(
    scope: str = Query(
        default="search",
        description=(
            "search=仅 description→embeddings（默认，检索止血，不扫 atom）；"
            "full=description+atom_feats（扫全部 client atom，代价高）"
        ),
    ),
):
    """Rebuild the skill vector index.

    同步 def：embed 调用可能持续较久；放事件循环上会让服务假死，见
    api_search_trajectories 的说明。默认 ``scope=search`` 只重建检索用
    description 向量；``scope=full`` 才扫 atom 算 atom_feats。
    """
    try:
        embedding_client = create_embed_client(_config)
        atom_roots = _team_atom_roots() if scope == "full" else None
        rebuild_skill_index(
            skill_dir=_skill_dir, embed_client=embedding_client,
            atom_store_roots=atom_roots, scope=scope,
        )
        # 失效推荐引擎的 skill 索引 / skillhub 缓存，否则引擎仍服务旧 embedding
        try:
            from xskill.team.server.skill_manifest import get_recommend_engine
            eng = get_recommend_engine()
            if eng is not None:
                eng.invalidate_cache()
        except Exception:  # pylint: disable=broad-exception-caught
            logger.debug("engine cache invalidation skipped", exc_info=True)
        return MessageResponse(
            message=f"Skill index rebuilt (scope={scope})", ok=True,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as e:
        logger.exception("reindex failed")
        raise HTTPException(status_code=500, detail=str(e)) from e


def _team_atom_roots() -> list[Path] | None:
    """收集 team server 各 client 的 atom store 根（traj_root/clients/<c>/sessions）。

    非 team / 无 client 时返回 None（atom_feats 不计算，standalone 场景）。
    """
    try:
        from xskill.config import get_team_trajectories_dir
        traj_root = get_team_trajectories_dir()
    except Exception:  # pylint: disable=broad-exception-caught
        return None
    clients = traj_root / "clients"
    if not clients.is_dir():
        return None
    roots = [c / "sessions" for c in clients.iterdir() if (c / "sessions").is_dir()]
    return roots or None


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

def create_app(home_root: Path | str | None = None,
               *, team_server: bool = False) -> FastAPI:
    """Build the FastAPI app. Calls ``_ensure_loaded`` first so all module-level
    config globals (``_config``/``_skill_dir``/...) are populated before any
    endpoint or startup hook reads them.

    Args:
        home_root: 可选，覆盖生态扫描的 home root。debug 模式下设成自选目录
                   （只扫描该目录下的 ``.claude/``），生产环境留 None 用真
                   实 ``$HOME``。
        team_server: True = team server 模式。挂 /api/v1/team/* 路由、跳过
                   本机生态自动探测（纯 server 不采集自己的轨迹）、watcher
                   开 server_mode。
    """
    global _home_root_override
    _home_root_override = (
        Path(home_root).expanduser().resolve()
        if home_root is not None
        else None
    )
    ecosystem_home_root = _home_root().resolve()
    _ensure_loaded()
    """Create and configure the FastAPI application."""
    app = FastAPI(
        title="xskill",
        description="Trajectory-to-Skill distillation API",
        version=__version__,
    )
    app.include_router(router)

    # team server 模式：挂 /api/v1/team/* 路由
    if team_server:
        from xskill.team.server.api import router as team_router
        app.include_router(team_router)

    # SSE 长耗时接口
    from xskill.api.sse import sse_router
    app.include_router(sse_router)

    # 轨迹提交接口
    from xskill.ecosystems import submit_trajectory
    from pydantic import BaseModel as _BaseModel

    class _SubmitRequest(_BaseModel):
        content: str
        format: str = "markdown"
        metadata: dict | None = None
        traj_id: str | None = None

    @app.post("/api/v1/trajectories/submit")
    async def api_submit_trajectory(req: _SubmitRequest):
        try:
            # traj_dir 不传 → submit_trajectory 落到 get_traj_dir()
            # （第一个已注册的 watch dir）；没注册目录会抛错。
            result = submit_trajectory(
                content=req.content,
                format=req.format,
                metadata=req.metadata or {},
                traj_id=req.traj_id,
            )
            return result
        except Exception as e:
            raise HTTPException(status_code=400, detail=str(e))

    # -- Watcher status endpoint --
    @app.get("/api/v1/watcher/status")
    async def api_watcher_status():
        # watcher 位于常驻子进程，web 进程不持有其内存对象；
        # 读子进程定期落盘的心跳和统计。
        from xskill.config import XSKILL_HOME
        from xskill.utils.status_file import WATCHER_STATUS_FILE, read_status_file
        status = read_status_file(XSKILL_HOME / WATCHER_STATUS_FILE)
        if status is None:
            return {"running": False, "message": "watcher has not started yet"}
        return status

    @app.get("/api/v1/agent-worker/status")
    async def api_agent_worker_status():
        from xskill.config import XSKILL_HOME
        from xskill.utils.status_file import (
            AGENT_WORKER_STATUS_FILE,
            read_status_file,
        )

        status = read_status_file(XSKILL_HOME / AGENT_WORKER_STATUS_FILE)
        if status is None:
            return {
                "running": False,
                "message": "agent-worker has not started yet",
            }
        return status

    # -- Usage / cost stats (Issue #43) --
    @app.get("/api/v1/stats")
    async def api_stats():
        # watcher 读常驻子进程心跳；画像仍读短命子进程的最近一轮状态。
        from xskill.config import XSKILL_HOME
        from xskill.utils.status_file import (
            AGENT_WORKER_STATUS_FILE,
            PROFILE_STATUS_FILE,
            WATCHER_STATUS_FILE,
            read_status_file,
        )
        watcher = read_status_file(XSKILL_HOME / WATCHER_STATUS_FILE)
        agent_worker = read_status_file(
            XSKILL_HOME / AGENT_WORKER_STATUS_FILE,
        )
        profile_refresh = read_status_file(XSKILL_HOME / PROFILE_STATUS_FILE)

        def _read_registry_stats():
            from xskill.pipeline.registry import model_share, usage_summary
            return usage_summary(), model_share()

        cost, models = await _run_control_plane(_read_registry_stats, app)
        return {
            "role": "server" if _config.get("team", {}).get("server") else "client",
            "cost": cost,
            "models": models,
            "pipeline": agent_worker,
            "watcher": watcher,
            "profile_refresh": profile_refresh,
        }

    # ------------------------------------------------------------------
    @app.on_event("startup")
    async def _startup():
        """Initialize agent tool config and watcher runtime dependencies.

        无 fallback：LLM/embed 客户端构造失败一律 raise，daemon 启动失败而不是
        带 None client 带病跑（CLAUDE.md 第 1 条）。create_llm_client 内部仍可能
        返回 None（其它调用方依赖此语义），所以在 daemon startup 处显式断言。
        """
        # 所有 def 路由仍共享 anyio 线程池，保留可配容量以兼容
        # 现有部署。画像 embedding 已转到独立固定 worker，不使用此池。
        # limiter 必须在 startup 的事件循环内设置。
        import anyio.to_thread
        anyio.to_thread.current_default_thread_limiter().total_tokens = int(
            _config.get("server", {}).get("thread_pool_tokens", 80))

        # 配置错误必须让 team server 启动失败，不能在下方 best-effort
        # 上下文初始化中被吞掉。
        profile_refresh_cfg = None
        team_sync_cfg = None
        if team_server:
            from xskill.config import profile_refresh_config, team_sync_config
            profile_refresh_cfg = profile_refresh_config(_config)
            team_sync_cfg = team_sync_config(_config)

        llm = create_llm_client(_config)
        if llm is None:
            raise RuntimeError(
                "LLM client could not be created — check ~/.xskill/config.yaml: "
                "llm.base_url / llm.model / llm.api_key must all be valid"
            )
        # 构造即校验(fail-loud):api 进程本身不 embed,实际 embed 在 watcher/profile
        # 子进程各自构造;这里仅确认配置可用,配错早报错。
        create_embed_client(_config)
        # data_dir 在 server 端点路径上不被消费（trajectory 搜索走 Registry），
        # 传 _skill_dir 占位即可——同 core.py 的 tool context 初始化。
        from xskill.config import XSKILL_HOME as current_xskill_home
        init_skill_authoring_tool_context(
            skill_dir=_skill_dir,
            data_dir=_skill_dir,
            config=_config,
            spill_root=current_xskill_home / "tmp" / "spill",
        )
        logger.info(
            "xskill server ready  skill_dir=%s  llm=ok  embed=ok",
            _skill_dir,
        )

        # 生态自动检测 + 一次性入库已迁到常驻 watcher 子进程
        # (pipeline.watcher_factory.ingest_detected_ecosystems_once),web 进程不再起
        # 常驻 ingester 线程。

        # team server：初始化 team 上下文 + 注册 traj_root 为 watch_dir 基。
        if team_server:
            client_registry = None
            profile_scheduler = None
            recommend_engine_attached = False
            team_sync_executor_cleanup_required = False
            try:
                from xskill.team.server.client_registry import ClientRegistry
                from xskill.team.server.api import (
                    init_team_context,
                    reconcile_client_ingest_watch_dir,
                    start_team_sync_executor,
                )
                from xskill.pipeline.scheduler import IntervalSubprocessScheduler
                from xskill.team.server.state import ensure_join_token
                from xskill.config import (
                    get_team_clients_db_path, get_team_server_state_path,
                    get_team_trajectories_dir,
                )
                from xskill.pipeline.registry import register_dir as _register_dir

                join_token = ensure_join_token(get_team_server_state_path())
                client_registry = ClientRegistry(get_team_clients_db_path())
                traj_root = get_team_trajectories_dir()
                # team.server 的槽位/allow_anonymous_user 与 canary 都不再在此
                # 读取快照：它们是 HOT_RELOAD 段，读方每次现取 _config。

                def _team_register_dir(path, label):
                    # team_client 生态标签：watcher 的 CS 归因靠 wd.label 反查 client
                    _register_dir(path, label=label, ecosystem="team_client")

                def _team_configure_watch_dir(path, label, auto_index):
                    # clients.ingest_paused 是权威状态；每次上传/导入都据此重放，
                    # 修复 team_clients.db 与 registry.db 跨库写入可能留下的漂移。
                    _register_dir(
                        path,
                        label=label,
                        auto_index=auto_index,
                        ecosystem="team_client",
                    )

                # §5 构造 SkillRecommendEngine 并注入 manifest（staging 优先达量 + 画像推荐）
                from xskill.config import XSKILL_HOME as _xhome
                from xskill.recommend.engine import SkillRecommendEngine
                from xskill.team.server.skill_manifest import set_recommend_engine
                _team_embed = create_embed_client(_config)
                _engine = SkillRecommendEngine(
                    config=_config, skill_dir=_skill_dir, traj_root=traj_root,
                    embed_client=_team_embed,
                    profile_db=_xhome / "team_profile.db",
                    client_registry=client_registry,
                )
                set_recommend_engine(_engine)
                recommend_engine_attached = True

                init_team_context(
                    join_token=join_token,
                    client_registry=client_registry,
                    skill_dir=_skill_dir,
                    traj_root=traj_root,
                    register_dir=_team_register_dir,
                    configure_watch_dir=_team_configure_watch_dir,
                    skillhub=_engine.skillhub,
                    profile_refresh_service=None,
                )
                for client_row in client_registry.list():
                    reconcile_client_ingest_watch_dir(client_row["client_id"])
                team_sync_executor_cleanup_required = True
                start_team_sync_executor(
                    app,
                    max_workers=team_sync_cfg["workers"],
                )
                # 重活合并：画像 + Milvus 对账 + 脏用户推荐预计算，全在独立子进程。
                # Web /sync 只读已落库画像与 client_recommend_slots
                # (profile_refresh_service=None 时 api.team_sync 入队守卫跳过)。
                profile_scheduler = IntervalSubprocessScheduler(
                    "recommend-heavy",
                    [_sys.executable, "-m", "xskill._workers", "recommend-heavy"],
                    interval=profile_refresh_cfg["interval"],
                    timeout=profile_refresh_cfg["timeout"],
                )
                profile_scheduler.start()
                _schedulers.append(profile_scheduler)
                logger.info(
                    "team server context ready (traj_root=%s, "
                    "recommend-heavy every %.0fs via subprocess)",
                    traj_root, profile_refresh_cfg["interval"],
                )
            except Exception:
                if team_sync_executor_cleanup_required:
                    try:
                        from xskill.team.server.api import stop_team_sync_executor
                        stop_team_sync_executor(app)
                    except Exception:  # pylint: disable=broad-exception-caught
                        logger.warning("failed to stop partial team sync executor",
                                       exc_info=True)
                if profile_scheduler is not None and profile_scheduler not in _schedulers:
                    try:
                        profile_scheduler.stop()
                    except Exception:  # pylint: disable=broad-exception-caught
                        logger.warning("failed to stop partial profile scheduler",
                                       exc_info=True)
                for scheduler in _schedulers:
                    try:
                        scheduler.stop()
                    except Exception:  # pylint: disable=broad-exception-caught
                        logger.warning("failed to stop partial team scheduler",
                                       exc_info=True)
                _schedulers.clear()
                registry_owned_by_context = False
                try:
                    from xskill.team.server.api import clear_team_context, team_context
                    registry_owned_by_context = (
                        client_registry is not None
                        and team_context().client_registry is client_registry
                    )
                    clear_team_context(profile_refresh_shutdown_timeout=0)
                except Exception:  # pylint: disable=broad-exception-caught
                    logger.warning("failed to clear partial team context",
                                   exc_info=True)
                if client_registry is not None and not registry_owned_by_context:
                    try:
                        client_registry.close()
                    except Exception:  # pylint: disable=broad-exception-caught
                        logger.warning("failed to close unattached client registry",
                                       exc_info=True)
                if recommend_engine_attached:
                    try:
                        from xskill.team.server.skill_manifest import set_recommend_engine
                        set_recommend_engine(None)
                    except Exception:  # pylint: disable=broad-exception-caught
                        logger.warning("failed to detach recommend engine after "
                                       "partial team init", exc_info=True)
                logger.exception("team server context init failed")
                raise

        # agent-worker 在常驻子进程中持续运行。
        # 进程只留一个轻量守护线程，负责启动、监测和异常退出后重启子进程；重计算
        # 仍与 web 事件循环保持 GIL 隔离。DirectoryWatcher 自己按 poll_interval
        # 持续扫描，Future 跨轮保留，不再等待一批全部结束后才启动下一轮。
        from xskill.pipeline.scheduler import IntervalSubprocessScheduler as _WorkerSched
        poll_interval = float(_config.get("watcher", {}).get("poll_interval", 5))
        agent_worker_command = [
            _sys.executable, "-m", "xskill._workers", "agent-worker",
        ]
        if team_server:
            agent_worker_command.append("--server")
        else:
            agent_worker_command.extend(["--home", str(ecosystem_home_root)])
        agent_worker_scheduler = _WorkerSched(
            "agent-worker", agent_worker_command,
            interval=poll_interval,
            timeout=5.0,
            persistent=True,
        )
        agent_worker_scheduler.start()
        _schedulers.append(agent_worker_scheduler)
        logger.info(
            "persistent agent worker started (team_server=%s, poll every %.0fs)",
            team_server,
            poll_interval,
        )
        from xskill.config import ux_scores_sync_config
        ux_sync_cfg = ux_scores_sync_config(_config)
        ux_sync_scheduler = _WorkerSched(
            "ux-scores-sync",
            [_sys.executable, "-m", "xskill._workers", "ux-scores-sync"],
            interval=ux_sync_cfg["interval"],
            timeout=ux_sync_cfg["timeout"],
        )
        ux_sync_scheduler.start()
        _schedulers.append(ux_sync_scheduler)
        logger.info(
            "ux_scores disk→db sync every %.0fs via subprocess",
            ux_sync_cfg["interval"],
        )
        if not team_server:
            ingest_interval = min(poll_interval, 1.0)
            ingest_scheduler = _WorkerSched(
                "ecosystem-ingest",
                [
                    _sys.executable,
                    "-m",
                    "xskill._workers",
                    "ecosystem-ingest",
                    "--home",
                    str(ecosystem_home_root),
                    "--loop",
                    "--interval",
                    "0.5",
                ],
                interval=max(ingest_interval, 1.0),
                timeout=5.0,
                persistent=True,
            )
            ingest_scheduler.start()
            _schedulers.append(ingest_scheduler)
            logger.info(
                "ecosystem ingest scheduler started "
                "(persistent subprocess, poll every 0.5s)",
            )

        # 依赖初始化全部成功后才创建线程池；如果 startup 在此之前
        # 失败，不会留下需要额外回收的非 daemon 线程。
        _start_control_plane_executor(app)

    @app.on_event("shutdown")
    async def _shutdown():
        # 先竖旗：所有 worker 线程里的 LLM 重试循环见旗即弃，退避睡眠立即
        # 中断。不竖旗的话 join 最坏拖 11 分钟，supervisor 10s 后 SIGKILL。
        from xskill.utils.shutdown import request_shutdown
        from xskill.api.sse import shutdown_sse_executor
        request_shutdown()
        shutdown_sse_executor()
        # 先停止控制面新任务并等待已接收的短 Git/SQLite 读取退出。
        # 即使后续 team/watcher 清理抛异常，也不会泄漏这个 app 的线程池。
        _stop_control_plane_executor(app)
        # 先停定时子进程调度器(画像 profile-refresh 等),再清理 team 上下文；
        # watcher 在其后停止,避免慢 embedding 占用 watcher/anyio 资源。
        for scheduler in _schedulers:
            scheduler.stop()
        _schedulers.clear()
        if team_server:
            try:
                from xskill.team.server.api import (
                    clear_team_context,
                    stop_team_sync_executor,
                )
                stop_team_sync_executor(app)
                # 画像已无常驻 service(profile_refresh_service=None),clear 对它是 no-op。
                clear_team_context(profile_refresh_shutdown_timeout=0)
            finally:
                from xskill.team.server.skill_manifest import set_recommend_engine
                set_recommend_engine(None)
        # watcher / ingester 位于常驻子进程，web 进程无内存实例可停；上面 stop
        # 全部调度器时会向子进程发 TERM 并等待其收敛。

    # 看板:仅当 config.dashboard.enabled 时挂载(默认不挂)
    from xskill.dashboard.mount import mount_dashboard
    mount_dashboard(app, _config)

    return app
