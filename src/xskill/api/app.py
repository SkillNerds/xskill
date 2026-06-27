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

import logging
import os
import tempfile
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from xskill import __version__
from xskill.config import load_config, get_skill_dir
from xskill.utils.search import search as search_trajs, search_all as search_trajs_all
from xskill.skill.repo import list_skills, import_skill
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
from xskill.agents.skill_tools import init_context, search_skills, rebuild_skill_index
from xskill.utils.llm import create_llm_client, create_embed_client
from xskill.skill.git import ensure_repo, current_branch

logger = logging.getLogger("xskill.server")

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
_watcher_ref: dict = {}  # {"instance": DirectoryWatcher} — set in create_app startup

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
    git_branch: str


class InitRequest(BaseModel):
    path: Optional[str] = None


class MessageResponse(BaseModel):
    message: str
    ok: bool = True


class _SubmitRequest(BaseModel):
    content: str
    format: str = "markdown"
    metadata: dict | None = None
    traj_id: str | None = None


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------
router = APIRouter(prefix="/api/v1")
STARTUP_ALL_SKILLS = "<startup_all>"


# ---- Trajectories --------------------------------------------------------

@router.post("/trajectories/search", response_model=TrajectorySearchResponse)
async def api_search_trajectories(req: TrajectorySearchRequest):
    """Search for similar trajectories in the dataset index.

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
async def api_search_skills(req: SkillSearchRequest):
    """Search existing skills by semantic similarity."""
    try:
        results_json = search_skills(req.query, top_k=req.top_k)
        import json
        results = json.loads(results_json)
        return results
    except Exception as e:
        logger.exception("skill search failed")
        raise HTTPException(status_code=500, detail=str(e))


# ---- Skill Resolve (canary-aware) ----------------------------------------

@router.post("/skills/resolve")
async def api_resolve_skill(req: SkillResolveRequest):
    """搜索 skill + canary 分流，返回 agent 应该读取的路径。

    ``accept_staging=True`` 时，若 skill 有活跃 staging 分支，按 80/20 概率
    决定返回 main 路径还是 ``.canary/`` 物化路径。
    """
    from xskill import canary
    import json as _json
    import time

    try:
        raw = search_skills(req.query, top_k=1)
        hits = _json.loads(raw)
        if isinstance(hits, dict):
            hits = hits.get("results", [])
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
    from xskill.pipeline.registry import list_watch_dirs, get_connection
    import json as _json

    dirs = list_watch_dirs()
    for d in dirs:
        if dir and d["path"] != dir:
            continue
        conn = get_connection()
        try:
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
        finally:
            conn.close()
    return {"logs": [], "filename": filename}


@router.get("/trajectories/list")
async def api_list_trajectories():
    """List all trajectories across registered directories with full status."""
    from xskill.pipeline.registry import list_watch_dirs, get_connection, get_status_counts
    dirs = list_watch_dirs()
    all_trajs = []
    for d in dirs:
        conn = get_connection()
        try:
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
        finally:
            conn.close()
    status_counts = get_status_counts()
    return {"trajectories": all_trajs, "count": len(all_trajs), "status_counts": status_counts}


# ---- System --------------------------------------------------------------

@router.get("/health", response_model=HealthResponse)
async def api_health():
    """Health check endpoint."""
    return HealthResponse(status="ok", version=__version__)


@router.get("/status", response_model=StatusResponse)
async def api_status():
    """Return system status: skill dir, skill count, git branch."""
    try:
        skills = list_skills(_skill_dir)
        branch = current_branch(str(_skill_dir))
        return StatusResponse(
            skill_dir=str(_skill_dir),
            skill_count=len(skills),
            git_branch=branch,
        )
    except Exception as e:
        logger.exception("status check failed")
        raise HTTPException(status_code=500, detail=str(e))


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
async def api_reindex():
    """Rebuild the skill vector index."""
    try:
        rebuild_skill_index()
        return MessageResponse(message="Skill index rebuilt", ok=True)
    except Exception as e:
        logger.exception("reindex failed")
        raise HTTPException(status_code=500, detail=str(e))


def _include_team_router(app: FastAPI, team_server: bool):
    if not team_server:
        return
    from xskill.team.server.api import router as team_router
    app.include_router(team_router)


def _include_sse_router(app: FastAPI):
    from xskill.api.sse import sse_router
    app.include_router(sse_router)


def _mount_trajectory_submit_route(app: FastAPI):
    from xskill.ecosystems import submit_trajectory

    @app.post("/api/v1/trajectories/submit")
    async def api_submit_trajectory(req: _SubmitRequest):
        try:
            result = submit_trajectory(
                content=req.content,
                format=req.format,
                metadata=req.metadata or {},
                traj_id=req.traj_id,
            )
            return result
        except Exception as e:
            raise HTTPException(status_code=400, detail=str(e))


def _mount_status_routes(app: FastAPI):
    @app.get("/api/v1/watcher/status")
    async def api_watcher_status():
        if _watcher_ref.get("instance"):
            return _watcher_ref["instance"].stats
        return {"running": False, "message": "watcher not started"}

    @app.get("/api/v1/stats")
    async def api_stats():
        from xskill.pipeline.registry import model_share, usage_summary
        watcher = (_watcher_ref["instance"].stats
                   if _watcher_ref.get("instance") else None)
        return {
            "role": "server" if _config.get("team", {}).get("server") else "client",
            "cost": usage_summary(),
            "models": model_share(),
            "pipeline": watcher,
        }


def _install_startup_skills(installer, history, agent: str, info_msg: str,
                            record_main: bool = False):
    try:
        installed = installer(_skill_dir, target_root=_home_root())
        if record_main:
            for dest in installed:
                history.record(skill=dest.parent.name, side="main", sha="")
        logger.info(info_msg, len(installed))
    except Exception as e:
        logger.warning("startup install_all_to_%s failed", agent, exc_info=True)
        history.record_fail(
            skill=STARTUP_ALL_SKILLS, agent=agent, reason=str(e)[:200],
        )


def _start_ingester(eco: str, ingester):
    ingester.start()
    _watcher_ref[f"ingester_{eco}"] = ingester


def _ecosystem_context() -> dict:
    from xskill.ecosystems import (
        detect_known_ecosystems,
        CCSessionIngester, JsonlIngester, SqliteIngester,
        CODEX_SPEC, OPENCODE_SPEC, NGAGENT_SPEC,
        OPENCLAW_SPEC, CURSOR_SPEC,
        TraeIngester,
        install_all_to_claude_code,
        install_all_to_codex,
        install_all_to_opencode,
        install_all_to_ngagent,
        install_all_to_openclaw,
        install_all_to_cursor,
        install_all_to_trae,
        make_openclaw_canary_flip_hook,
    )
    from xskill.canary import CanaryConfig
    from xskill.config import XSKILL_HOME
    from xskill.ecosystems._history import InstallHistory
    from xskill.pipeline.registry import register_dir

    install_history_path = XSKILL_HOME / "install_history.jsonl"
    return {
        "detect_known_ecosystems": detect_known_ecosystems,
        "CCSessionIngester": CCSessionIngester,
        "JsonlIngester": JsonlIngester,
        "SqliteIngester": SqliteIngester,
        "TraeIngester": TraeIngester,
        "CODEX_SPEC": CODEX_SPEC,
        "OPENCODE_SPEC": OPENCODE_SPEC,
        "NGAGENT_SPEC": NGAGENT_SPEC,
        "OPENCLAW_SPEC": OPENCLAW_SPEC,
        "CURSOR_SPEC": CURSOR_SPEC,
        "install_all_to_claude_code": install_all_to_claude_code,
        "install_all_to_codex": install_all_to_codex,
        "install_all_to_opencode": install_all_to_opencode,
        "install_all_to_ngagent": install_all_to_ngagent,
        "install_all_to_openclaw": install_all_to_openclaw,
        "install_all_to_cursor": install_all_to_cursor,
        "install_all_to_trae": install_all_to_trae,
        "make_openclaw_canary_flip_hook": make_openclaw_canary_flip_hook,
        "CanaryConfig": CanaryConfig,
        "install_history_path": install_history_path,
        "install_history": InstallHistory(install_history_path),
        "register_dir": register_dir,
    }


def _start_claude_code_ingester(det: dict, bridge: Path, poll_interval: float,
                                ctx: dict):
    _install_startup_skills(
        ctx["install_all_to_claude_code"], ctx["install_history"],
        "claude_code",
        "startup install_all_to_claude_code: %d skills installed (side=main)",
        record_main=True,
    )
    ingester = ctx["CCSessionIngester"](
        target_traj_dir=bridge,
        home_root=_home_root(),
        poll_interval=poll_interval,
        skill_dir=_skill_dir,
        target_root=_home_root(),
        history_path=ctx["install_history_path"],
        assignments_path=_skill_dir / "session_assignments.jsonl",
    )
    _start_ingester(det["ecosystem"], ingester)


def _start_jsonl_ingester(det: dict, bridge: Path, poll_interval: float,
                          ctx: dict, spec_key: str):
    ingester = ctx["JsonlIngester"](
        ctx[spec_key],
        target_traj_dir=bridge,
        home_root=_home_root(),
        poll_interval=poll_interval,
    )
    _start_ingester(det["ecosystem"], ingester)


def _start_sqlite_ingester(det: dict, bridge: Path, poll_interval: float,
                           ctx: dict, spec_key: str):
    ingester = ctx["SqliteIngester"](
        target_traj_dir=bridge,
        home_root=_home_root(),
        spec=ctx[spec_key],
        poll_interval=poll_interval,
    )
    _start_ingester(det["ecosystem"], ingester)


def _start_codex_ingester(det: dict, bridge: Path, poll_interval: float,
                          ctx: dict):
    _install_startup_skills(
        ctx["install_all_to_codex"], ctx["install_history"], "codex",
        "startup install_all_to_codex: %d skills installed to ~/.agents/skills/",
    )
    _start_jsonl_ingester(det, bridge, poll_interval, ctx, "CODEX_SPEC")


def _start_opencode_ingester(det: dict, bridge: Path, poll_interval: float,
                             ctx: dict):
    _install_startup_skills(
        ctx["install_all_to_opencode"], ctx["install_history"], "opencode",
        "startup install_all_to_opencode: %d skills installed to ~/.agents/skills/",
    )
    _start_sqlite_ingester(det, bridge, poll_interval, ctx, "OPENCODE_SPEC")


def _start_ngagent_ingester(det: dict, bridge: Path, poll_interval: float,
                            ctx: dict):
    _install_startup_skills(
        ctx["install_all_to_ngagent"], ctx["install_history"], "ngagent",
        "startup install_all_to_ngagent: %d skills installed to ~/.config/opencode/skills/",
    )
    _start_sqlite_ingester(det, bridge, poll_interval, ctx, "NGAGENT_SPEC")


def _start_openclaw_ingester(det: dict, bridge: Path, poll_interval: float,
                             ctx: dict):
    _install_startup_skills(
        ctx["install_all_to_openclaw"], ctx["install_history"], "openclaw",
        "startup install_all_to_openclaw: %d skills installed (copy mode) to ~/.agents/skills/",
        record_main=True,
    )
    canary_cfg = ctx["CanaryConfig"].from_dict(_config.get("canary", {}))
    flip_hook = ctx["make_openclaw_canary_flip_hook"](
        skill_dir=_skill_dir,
        target_root=_home_root(),
        history=ctx["install_history"],
        probability=canary_cfg.probability,
    )
    ingester = ctx["JsonlIngester"](
        ctx["OPENCLAW_SPEC"],
        target_traj_dir=bridge,
        home_root=_home_root(),
        poll_interval=poll_interval,
        on_new_sessions=flip_hook,
    )
    _start_ingester(det["ecosystem"], ingester)


def _start_cursor_ingester(det: dict, bridge: Path, poll_interval: float,
                           ctx: dict):
    _install_startup_skills(
        ctx["install_all_to_cursor"], ctx["install_history"], "cursor",
        "startup install_all_to_cursor: %d skills installed to ~/.cursor/skills/",
        record_main=True,
    )
    _start_jsonl_ingester(det, bridge, poll_interval, ctx, "CURSOR_SPEC")


def _start_trae_ingester(det: dict, bridge: Path, poll_interval: float,
                         ctx: dict):
    _install_startup_skills(
        ctx["install_all_to_trae"], ctx["install_history"], "trae",
        "startup install_all_to_trae: %d skills installed",
        record_main=True,
    )
    ingester = ctx["TraeIngester"](
        target_traj_dir=bridge,
        home_root=_home_root(),
        poll_interval=poll_interval,
    )
    _start_ingester(det["ecosystem"], ingester)


def _ecosystem_handlers() -> dict:
    return {
        "claude_code": _start_claude_code_ingester,
        "codex": _start_codex_ingester,
        "opencode": _start_opencode_ingester,
        "ngagent": _start_ngagent_ingester,
        "openclaw": _start_openclaw_ingester,
        "cursor": _start_cursor_ingester,
        "trae": _start_trae_ingester,
    }


def _ensure_single_ecosystem_ingester(det: dict, poll_interval: float, ctx: dict):
    eco = det["ecosystem"]
    ingester_key = f"ingester_{eco}"
    if ingester_key in _watcher_ref:
        return
    bridge: Path = det["bridge"]
    bridge.mkdir(parents=True, exist_ok=True)
    ctx["register_dir"](bridge, label=f"{eco} sessions", ecosystem=eco)
    handler = _ecosystem_handlers().get(eco)
    if handler is not None:
        handler(det, bridge, poll_interval, ctx)
    logger.info("ecosystem %s detected: source=%s bridge=%s",
                eco, det["source"], bridge)


def _ensure_ingesters_for_detected_ecosystems(team_server: bool):
    if team_server:
        return
    try:
        ctx = _ecosystem_context()
        detections = ctx["detect_known_ecosystems"](home_root=_home_root())
        poll_interval = float(_config.get("watcher", {}).get("poll_interval", 10))
        for det in detections:
            _ensure_single_ecosystem_ingester(det, poll_interval, ctx)
    except Exception:
        logger.warning("ecosystem auto-detect failed", exc_info=True)


def _init_team_server_context():
    try:
        from xskill.team.server.client_registry import ClientRegistry
        from xskill.team.server.api import init_team_context
        from xskill.team.server.state import ensure_join_token
        from xskill.config import (
            get_team_clients_db_path, get_team_server_state_path,
            get_team_trajectories_dir,
        )
        from xskill.pipeline.registry import register_dir as _register_dir
        from xskill.canary import CanaryConfig

        join_token = ensure_join_token(get_team_server_state_path())
        client_registry = ClientRegistry(get_team_clients_db_path())
        traj_root = get_team_trajectories_dir()
        team_cfg = _config.get("team", {}).get("server", {})
        canary_cfg = CanaryConfig.from_dict(_config.get("canary", {}))

        def _team_register_dir(path, label):
            _register_dir(path, label=label, ecosystem="team_client")

        init_team_context(
            join_token=join_token,
            client_registry=client_registry,
            skill_dir=_skill_dir,
            traj_root=traj_root,
            probability=canary_cfg.probability,
            ranked_slots=int(team_cfg.get("ranked_slots", 80)),
            total_slots=int(team_cfg.get("skill_slots", 100)),
            register_dir=_team_register_dir,
        )
        logger.info("team server context ready (traj_root=%s)", traj_root)
    except Exception:
        logger.warning("team server context init failed", exc_info=True)


def _start_directory_watcher(llm, embed, team_server: bool):
    try:
        from xskill.pipeline.runner import DirectoryWatcher
        watcher_cfg = _config.get("watcher", {})
        watcher = DirectoryWatcher(
            llm=llm, embed_client=embed, config=_config,
            skill_dir=_skill_dir,
            poll_interval=float(watcher_cfg.get("poll_interval", 30)),
            max_concurrent=int(watcher_cfg.get("max_concurrent", 30)),
            cluster_batch_size=int(watcher_cfg.get("cluster_batch_size", 8)),
            server_mode=team_server,
            on_poll_hook=lambda: _ensure_ingesters_for_detected_ecosystems(team_server),
        )
        watcher.start()
        _watcher_ref["instance"] = watcher
        logger.info("watcher started (team_server=%s)", team_server)
    except Exception:
        logger.warning("watcher startup failed", exc_info=True)


async def _startup_app(team_server: bool):
    llm = create_llm_client(_config)
    if llm is None:
        raise RuntimeError(
            "LLM client could not be created — check ~/.xskill/config.yaml: "
            "llm.base_url / llm.model / llm.api_key must all be valid"
        )
    embed = create_embed_client(_config)
    init_context(
        skill_dir=_skill_dir,
        data_dir=_skill_dir,
        llm_client=llm,
        embed_client=embed,
        config=_config,
    )
    logger.info("xskill server ready  skill_dir=%s  llm=ok  embed=ok", _skill_dir)
    _ensure_ingesters_for_detected_ecosystems(team_server)
    if team_server:
        _init_team_server_context()
    _start_directory_watcher(llm, embed, team_server)


async def _shutdown_app():
    watcher = _watcher_ref.get("instance")
    if watcher:
        watcher.stop()
    for k, v in list(_watcher_ref.items()):
        if k.startswith("ingester_"):
            try:
                v.stop()
            except Exception:
                logger.warning("failed to stop %s", k, exc_info=True)


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

def create_app(home_root: Path | str | None = None,
               *, team_server: bool = False) -> FastAPI:
    """Build and configure the FastAPI application."""
    global _home_root_override
    if home_root is not None:
        _home_root_override = Path(home_root).expanduser().resolve()
    _ensure_loaded()

    app = FastAPI(
        title="xskill",
        description="Trajectory-to-Skill distillation API",
        version=__version__,
    )
    app.include_router(router)
    _include_team_router(app, team_server)
    _include_sse_router(app)
    _mount_trajectory_submit_route(app)
    _mount_status_routes(app)

    @app.on_event("startup")
    async def _startup():
        await _startup_app(team_server)

    @app.on_event("shutdown")
    async def _shutdown():
        await _shutdown_app()

    from xskill.dashboard.mount import mount_dashboard
    mount_dashboard(app, _config)
    return app
