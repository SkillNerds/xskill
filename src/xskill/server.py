"""
server.py -- FastAPI application (SHORT operation endpoints)
=============================================================
Non-SSE REST endpoints for trajectory search, skill CRUD, and system operations.

Usage:
    from xskill.server import create_app
    app = create_app()
"""

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
from typing import Any, Optional

from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from xskill import __version__
from xskill.config import load_config, get_skill_dir, get_chat_archive_dir
from xskill.search import search as search_trajs, search_all as search_trajs_all
from xskill.skill_manager import (
    list_skills,
    show_skill,
    skill_log,
    skill_diff,
    rollback_skill,
    freeze_skill,
    unfreeze_skill,
    delete_skill,
    export_skill,
    import_skill,
)
from xskill.skill_tools import init_context, search_skills, rebuild_skill_index
from xskill.llm_client import create_llm_client, create_embed_client
from xskill.git_lock import ensure_repo, current_branch

logger = logging.getLogger("xskill.server")

# ---------------------------------------------------------------------------
# Module-level config -- lazy loaded
# ---------------------------------------------------------------------------
# 之前是 import 时就跑 ``_config = load_config()``，会让"导入 xskill.server"
# 这件本来无副作用的事情强依赖 ``~/.xskill/config.yaml``。CI runner 上没这个
# 文件 → 所有间接 import xskill.server 的测试 collection 都炸。
#
# 改成 lazy：占位 None；``_ensure_loaded()`` 在 ``create_app()`` 入口 + 每个
# server 启动路径首次调用时填充。endpoints 在 startup hook 之后才被 hit，
# 拿到的就是非 None；测试如果只 import ``_exec_tool`` / 常量，模块加载阶段
# 完全不读 config。
_config: dict | None = None
_skill_dir: Path | None = None
_chat_archive_dir: Path | None = None
_traj_dir: Path | None = None  # 兼容仍引用 _traj_dir 的代码路径
_watcher_ref: dict = {}  # {"instance": DirectoryWatcher} — set in create_app startup
_chat_agents: dict = {}   # session_id → Agno Agent instance

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
    global _config, _skill_dir, _chat_archive_dir, _traj_dir
    if _config is not None:
        return
    _config = load_config()
    _skill_dir = get_skill_dir()
    _chat_archive_dir = get_chat_archive_dir()
    _traj_dir = _chat_archive_dir

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
    traj_dir: str
    skill_count: int
    git_branch: str


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
        from xskill.registry import list_watch_dirs
        for d in list_watch_dirs():
            if str(resolved).startswith(str(Path(d["path"]).resolve())):
                allowed = True
                break
        if not allowed and str(resolved).startswith(str(_traj_dir.resolve())):
            allowed = True
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
        exported = export_skill(_skill_dir, name, tmp_dir)
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
    from xskill.candidates import load_candidates
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


# ---- Chat Tool-Use -------------------------------------------------------

CHAT_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_skills",
            "description": "Search the skill library for relevant skills by semantic query",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"}
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "search_trajectories",
            "description": "Search trajectories by semantic query",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                    "top_k": {"type": "integer", "description": "Number of results", "default": 3}
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read content of a file (trajectory or skill)",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path to read"}
                },
                "required": ["path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "execute_code",
            "description": "Execute a Python code snippet and return stdout/stderr",
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {"type": "string", "description": "Python code to execute"}
                },
                "required": ["code"]
            }
        }
    },
]

CHAT_TOOLS_INFO = [
    {"name": "search_skills", "icon": "\U0001f50d", "desc": "Search skill library"},
    {"name": "search_trajectories", "icon": "\U0001f4cb", "desc": "Search trajectories"},
    {"name": "read_file", "icon": "\U0001f4c4", "desc": "Read file content"},
    {"name": "execute_code", "icon": "\u25b6\ufe0f", "desc": "Run Python code"},
]


def _exec_tool(name: str, args: dict, skill_dir: Path, traj_dir: Path, config: dict) -> str:
    """Execute a tool call and return the result string."""
    if name == "search_skills":
        from xskill.skill_tools import search_skills as _search_skills
        return _search_skills(args.get("query", ""), top_k=3)
    elif name == "search_trajectories":
        from xskill.search import search_all
        results = search_all(args.get("query", ""), top_k=args.get("top_k", 3), config=config)
        import json
        return json.dumps([{"traj_id": r["traj_id"], "similarity": r["similarity"],
                           "intent": r.get("meta", {}).get("intent", "")} for r in results], ensure_ascii=False)
    elif name == "read_file":
        path = args.get("path", "")
        p = Path(path)
        # Security: only allow reading within skill_dir or traj_dir
        try:
            p = p.resolve()
            if not (str(p).startswith(str(skill_dir.resolve())) or str(p).startswith(str(traj_dir.resolve()))):
                return f"Error: access denied, path must be within {skill_dir} or {traj_dir}"
            return p.read_text(encoding="utf-8")[:5000]
        except Exception as e:
            return f"Error: {e}"
    elif name == "execute_code":
        import subprocess
        import sys as _sys
        try:
            # 用当前解释器自己跑用户代码：跨 py3.11 / py3.12 / 任意环境都成立，
            # 不会因为 runner 上没有 ``python3.11`` 二进制而直接报 ENOENT。
            result = subprocess.run(
                [_sys.executable, "-c", args.get("code", "")],
                capture_output=True, text=True, timeout=10,
                cwd=str(traj_dir),
            )
            out = result.stdout[:3000]
            if result.stderr:
                out += f"\n[stderr] {result.stderr[:1000]}"
            return out or "(no output)"
        except subprocess.TimeoutExpired:
            return "Error: execution timed out (10s limit)"
        except Exception as e:
            return f"Error: {e}"
    return f"Unknown tool: {name}"


@router.get("/chat/tools")
async def api_chat_tools():
    """Return available chat tools metadata for frontend display."""
    return {"tools": CHAT_TOOLS_INFO}


@router.post("/chat/tool")
async def api_exec_tool(req: dict):
    """Execute a single chat tool and return its result."""
    name = req.get("name", "")
    args = req.get("args", {})
    if not name:
        raise HTTPException(status_code=400, detail="tool name is required")
    result = _exec_tool(name, args, _skill_dir, _traj_dir, _config)
    return {"name": name, "result": result}


def _search_skill_fast(query: str) -> dict | None:
    """Fast skill search using text overlap — no embed API call.

    Reads frontmatter of each skill and scores by keyword overlap.
    Falls back to embed-based search only if no skills match textually.
    """
    import pickle
    try:
        if not _skill_dir.is_dir():
            return None
        query_lower = query.lower()
        query_words = set(query_lower.split())
        best, best_score = None, 0
        for sd in _skill_dir.iterdir():
            if not sd.is_dir() or sd.name.startswith("."):
                continue
            skill_md = sd / "SKILL.md"
            if not skill_md.is_file():
                continue
            # Read first 500 chars of SKILL.md for matching
            head = skill_md.read_text(encoding="utf-8")[:500].lower()
            # Score: count query words found in skill head
            score = sum(1 for w in query_words if len(w) > 2 and w in head)
            # Also check skill name
            name_words = set(sd.name.replace("-", " ").replace("_", " ").lower().split())
            score += len(query_words & name_words) * 2
            if score > best_score:
                best_score = score
                best = {"skill_name": sd.name, "similarity": min(score / max(len(query_words), 1), 1.0)}
        return best if best_score > 0 else None
    except Exception as e:
        logger.warning("fast skill search failed: %s", e)
        return None


@router.post("/chat")
async def api_chat(req: dict):
    """最小聊天端点：搜 skill → canary 分流 → LLM 回复。

    Body: {"message": str, "traj_id": str?}
    Response: {"reply", "skill_name", "side", "commit_sha", "traj_id"}
    """
    from xskill import canary
    import json as _json
    message = req.get("message", "")
    traj_id = req.get("traj_id") or f"chat_{__import__('time').time_ns()}"
    if not message.strip():
        raise HTTPException(status_code=400, detail="message is required")

    # 1. 搜 skill (fast text match, no embed API call)
    try:
        hit = _search_skill_fast(message)
    except Exception:
        hit = None

    skill_name = None
    side = "none"
    commit_sha = ""
    skill_body = ""

    if hit and hit.get("skill_name"):
        skill_name = hit["skill_name"]
        sd = _skill_dir / skill_name
        if not sd.is_dir():
            hit = None
            skill_name = None
        else:
            skill_md_path = sd / "SKILL.md"
            if not skill_md_path.is_file():
                skill_md_path = sd / "skill.md"
            if skill_md_path.is_file():
                skill_body = skill_md_path.read_text(encoding="utf-8")
            canary_md = _skill_dir / ".canary" / skill_name / "SKILL.md"
            if canary_md.is_file():
                side = "staging"
                skill_body = canary_md.read_text(encoding="utf-8")
            else:
                side = "main"

    # 2. LLM 回复
    llm = create_llm_client(_config, role="skill")
    if not llm:
        raise HTTPException(
            status_code=503,
            detail="LLM is not configured. Please set llm.base_url and llm.model "
                   "in config.yaml, or provide LLM_API_KEY / OPENAI_API_KEY env var.",
        )

    system = "你是一个编程助手。" if not skill_body else (
        "你是一个编程助手。回答时严格遵守以下 SKILL 指令：\n\n" + skill_body
    )
    try:
        reply = llm.chat(message, system=system)
    except Exception as e:
        logger.exception("LLM chat call failed")
        raise HTTPException(
            status_code=502,
            detail=f"LLM call failed: {e}",
        )

    return {
        "reply": reply,
        "skill_name": skill_name,
        "side": side,
        "commit_sha": commit_sha[:8],
        "traj_id": traj_id,
        "similarity": hit.get("similarity") if hit else None,
    }


@router.post("/chat/archive")
async def api_chat_archive(req: dict):
    """Archive a chat conversation as a trajectory markdown file.

    Body: {"messages": [...], "skill_name": str?, "side": str?, "traj_id": str?}
    Each message: {"role": "user"|"assistant", "text": str, "meta": {...}?, "items": [...]?}

    Writes traj_<id>.md to trajectory/chat_app_demo/ with xskill header.
    Watcher will auto-process: meta → index → process → ux_score.
    """
    import time as _time

    messages = req.get("messages", [])
    if not messages:
        raise HTTPException(status_code=400, detail="No messages to archive")

    traj_id = req.get("traj_id") or f"chat_{_time.time_ns()}"
    skill_name = req.get("skill_name") or ""
    side = req.get("side") or ""
    sha = req.get("sha") or ""

    # Build trajectory markdown
    lines = []
    # xskill header for watcher to parse
    if skill_name:
        lines.append(f"<!-- xskill:skill={skill_name} side={side or 'main'} sha={sha} -->")
    lines.append(f"# Trajectory: {traj_id}")
    lines.append(f"<!-- source: chat_app_demo -->")
    lines.append("")

    for msg in messages:
        role = msg.get("role", "unknown")
        text = msg.get("text", "")
        items = msg.get("items", [])

        lines.append(f"## {role.capitalize()}")

        # Render items (tool calls + text interleaved)
        if items:
            for item in items:
                if item.get("type") == "tool":
                    tc = item.get("tc", {})
                    lines.append(f"**Tool: {tc.get('name', '?')}**")
                    args = tc.get("args", {})
                    if isinstance(args, dict):
                        for k, v in args.items():
                            lines.append(f"  - {k}: {v}")
                    if tc.get("result"):
                        lines.append(f"  - result: {str(tc['result'])[:200]}")
                    lines.append("")
                elif item.get("type") == "text" and item.get("content"):
                    lines.append(item["content"])
                    lines.append("")
        elif text:
            lines.append(text)
            lines.append("")

    # Write to chat archive directory (~/.xskill/chat_archive/)
    chat_traj_dir = _chat_archive_dir
    md_path = chat_traj_dir / f"traj_{traj_id}.md"
    md_path.write_text("\n".join(lines), encoding="utf-8")

    return {
        "ok": True,
        "traj_id": traj_id,
        "path": str(md_path),
        "skill_name": skill_name,
        "side": side,
        "message": f"Archived to {md_path.name}. Watcher will auto-process.",
    }


@router.post("/chat/stream")
async def api_chat_stream(req: dict):
    """SSE streaming chat endpoint.

    Body: {"message": str, "traj_id": str?}
    Events:
      - event: meta   {skill_name, side, commit_sha, similarity, traj_id}
      - event: token   {text: "chunk"}
      - event: done    {reply: "full text"}
      - event: error   {error: "message"}
    """
    import asyncio
    import json as _json
    from concurrent.futures import ThreadPoolExecutor
    from sse_starlette.sse import EventSourceResponse
    from xskill import canary

    # Pause watcher to yield API bandwidth for this chat request
    watcher = _watcher_ref.get("instance")
    if watcher:
        watcher.pause()

    message = req.get("message", "")
    session_id = req.get("session_id") or "default"
    traj_id = req.get("traj_id") or f"chat_{__import__('time').time_ns()}"
    if not message.strip():
        raise HTTPException(status_code=400, detail="message is required")

    queue: asyncio.Queue = asyncio.Queue()

    # ── Agno tool definitions ──
    # Tools emit SSE events via the queue so frontend can show tool bubbles
    # inline with text. Since tool functions are sync but queue is async,
    # we use loop.call_soon_threadsafe.
    from agno.tools import tool as _agno_tool
    _loop = asyncio.get_event_loop()

    def _tool_emit(event, data):
        _loop.call_soon_threadsafe(queue.put_nowait, (event, data))

    @_agno_tool(name="search_skills", description="Search the skill library for relevant coding skills.\nArgs:\n    query: natural language search query")
    def _tool_search_skills(query: str) -> str:
        import json as _j
        _tool_emit("tool_call", {"name": "search_skills", "args": {"query": query}, "live": True})
        results = []
        for sd in sorted(_skill_dir.iterdir()):
            if not sd.is_dir() or sd.name.startswith("."):
                continue
            skill_md = sd / "SKILL.md"
            if not skill_md.is_file():
                continue
            head = skill_md.read_text(encoding="utf-8")[:500]
            canary_path = _skill_dir / ".canary" / sd.name / "SKILL.md"
            side = "staging" if canary_path.is_file() else "main"
            results.append({"skill_name": sd.name, "side": side,
                            "path": str(sd / "SKILL.md"), "description": head[:200]})
        result = _j.dumps(results, ensure_ascii=False) if results else "No skills found."
        # Emit tool result + canary info
        if results:
            _tool_emit("meta", {"skill_name": results[0]["skill_name"],
                                "side": results[0]["side"], "commit_sha": "",
                                "similarity": None, "traj_id": traj_id})
        _tool_emit("tool_result", {"name": "search_skills", "result": result[:500],
                                    "side": results[0]["side"] if results else "none"})
        return result

    @_agno_tool(name="read_skill", description="Read full content of a skill (SKILL.md). Use after search_skills.\nArgs:\n    skill_name: skill directory name from search results")
    def _tool_read_skill(skill_name: str) -> str:
        import json as _j
        _tool_emit("tool_call", {"name": "read_skill", "args": {"skill_name": skill_name}, "live": True})
        sd = _skill_dir / skill_name
        canary_md = _skill_dir / ".canary" / skill_name / "SKILL.md"
        if canary_md.is_file():
            body = canary_md.read_text(encoding="utf-8")
            result = _j.dumps({"skill_name": skill_name, "side": "staging", "body": body}, ensure_ascii=False)
            _tool_emit("meta", {"skill_name": skill_name, "side": "staging",
                                "commit_sha": "", "similarity": None, "traj_id": traj_id})
            _tool_emit("tool_result", {"name": "read_skill", "result": result[:500], "side": "staging"})
            return result
        elif (sd / "SKILL.md").is_file():
            body = (sd / "SKILL.md").read_text(encoding="utf-8")
            result = _j.dumps({"skill_name": skill_name, "side": "main", "body": body}, ensure_ascii=False)
            _tool_emit("meta", {"skill_name": skill_name, "side": "main",
                                "commit_sha": "", "similarity": None, "traj_id": traj_id})
            _tool_emit("tool_result", {"name": "read_skill", "result": result[:500], "side": "main"})
            return result
        _tool_emit("tool_result", {"name": "read_skill", "result": f"not found: {skill_name}"})
        return f"Error: skill not found: {skill_name}"

    @_agno_tool(name="read_file", description="Read a file from the project directories.\nArgs:\n    path: file path to read")
    def _tool_read_file(path: str) -> str:
        _tool_emit("tool_call", {"name": "read_file", "args": {"path": path}, "live": True})
        p = Path(path)
        if not p.is_file():
            _tool_emit("tool_result", {"name": "read_file", "result": f"not found: {path}"})
            return f"Error: file not found: {path}"
        try:
            content = p.read_text(encoding="utf-8")[:5000]
            _tool_emit("tool_result", {"name": "read_file", "result": content[:200]})
            return content
        except Exception as e:
            _tool_emit("tool_result", {"name": "read_file", "result": str(e)})
            return f"Error: {e}"

    @_agno_tool(name="execute_code", description="Execute a Python code snippet.\nArgs:\n    code: Python code to execute")
    def _tool_execute_code(code: str) -> str:
        import subprocess, sys as _sys
        _tool_emit("tool_call", {"name": "execute_code", "args": {"code": code[:80]}, "live": True})
        try:
            r = subprocess.run([_sys.executable, "-c", code],
                               capture_output=True, text=True, timeout=10)
            out = r.stdout[:3000]
            if r.stderr:
                out += f"\n[stderr] {r.stderr[:1000]}"
            result = out or "(no output)"
            _tool_emit("tool_result", {"name": "execute_code", "result": result[:200]})
            return result
        except Exception as e:
            _tool_emit("tool_result", {"name": "execute_code", "result": str(e)})
            return f"Error: {e}"

    # ── Get or create Agno Agent for this session ──
    from agno.agent import Agent as _Agent
    from agno.models.openai.like import OpenAILike as _OpenAILike

    llm_cfg = _config.get("llm", {})
    if not llm_cfg.get("base_url") or not llm_cfg.get("model"):
        raise HTTPException(status_code=503, detail="LLM not configured")

    if session_id in _chat_agents:
        chat_agent = _chat_agents[session_id]
    else:
        from agno.db.sqlite import SqliteDb as _SqliteDb
        from xskill.config import get_chat_db_path
        _db_path = str(get_chat_db_path())
        _chat_db = _SqliteDb(db_file=_db_path)

        model = _OpenAILike(
            id=llm_cfg["model"],
            api_key=llm_cfg.get("api_key") or os.environ.get("LLM_API_KEY", ""),
            base_url=llm_cfg["base_url"],
        )
        chat_agent = _Agent(
            model=model,
            tools=[_tool_search_skills, _tool_read_skill, _tool_read_file, _tool_execute_code],
            tool_call_limit=8,
            session_id=session_id,
            db=_chat_db,
            add_history_to_context=True,
            num_history_runs=10,
            instructions=[
                "你是一个编程助手。\n"
                "1. 收到用户问题后，先用 search_skills 搜索是否有相关 skill\n"
                "2. 如果搜到，用 read_skill 加载详细内容，结合 skill 回答\n"
                "3. skill 有 main（正式版）和 staging（灰度版），注意区分\n"
                "4. 可以用 read_file 读取文件，execute_code 执行代码验证\n"
                f"Skill 目录: {_skill_dir}\n轨迹目录: {_traj_dir}\n"
            ],
            markdown=True,
        )
        _chat_agents[session_id] = chat_agent

    # ── Emit initial meta ──
    await queue.put(("meta", {
        "skill_name": None, "side": "none", "commit_sha": "",
        "similarity": None, "traj_id": traj_id,
    }))

    # ── Run Agno agent — tools emit their own SSE events via _tool_emit ──
    async def _run_agent():
        full_reply = []
        try:
            async for chunk in chat_agent.arun(message, stream=True):
                content = getattr(chunk, "content", None)
                if content and isinstance(content, str):
                    full_reply.append(content)
                    await queue.put(("token", {"text": content}))
                    await asyncio.sleep(0)  # force flush to SSE

            await queue.put(("done", {"reply": "".join(full_reply)}))
        except Exception as exc:
            logger.exception("Agno agent failed")
            await queue.put(("error", {"error": f"{type(exc).__name__}: {exc}"}))
        finally:
            await queue.put(None)
            _w = _watcher_ref.get("instance")
            if _w:
                _w.resume()

    asyncio.create_task(_run_agent())

    async def event_generator():
        while True:
            item = await queue.get()
            if item is None:
                break
            event, data = item
            yield {"event": event, "data": _json.dumps(data, ensure_ascii=False)}

    return EventSourceResponse(event_generator())


# ---- Registry + Watcher --------------------------------------------------

@router.get("/registry/dirs")
async def api_list_registry_dirs():
    """List all registered watch directories with trajectory counts."""
    from xskill.registry import list_watch_dirs
    dirs = list_watch_dirs()
    return {"dirs": dirs, "count": len(dirs)}


@router.post("/registry/dirs")
async def api_register_dir(req: dict):
    """Register a directory for watching."""
    from xskill.registry import register_dir
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
    from xskill.registry import unregister_dir
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
    from xskill.registry import list_watch_dirs, get_connection
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
    from xskill.registry import list_watch_dirs, get_connection, get_status_counts
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
            traj_dir=str(_traj_dir),
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


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

def create_app(home_root: Path | str | None = None) -> FastAPI:
    """Build the FastAPI app. Calls ``_ensure_loaded`` first so all module-level
    config globals (``_config``/``_skill_dir``/...) are populated before any
    endpoint or startup hook reads them.

    Args:
        home_root: 可选，覆盖生态扫描的 home root。debug 模式下设成自选目录
                   （只扫描该目录下的 ``.claude/``），生产环境留 None 用真
                   实 ``$HOME``。
    """
    global _home_root_override
    if home_root is not None:
        _home_root_override = Path(home_root).expanduser().resolve()
    _ensure_loaded()
    """Create and configure the FastAPI application."""
    app = FastAPI(
        title="xskill",
        description="Trajectory-to-Skill distillation API",
        version=__version__,
    )
    app.include_router(router)

    # SSE 长耗时接口
    from xskill.tasks import sse_router
    app.include_router(sse_router)

    # 轨迹提交接口
    from xskill.adapters import submit_trajectory
    from pydantic import BaseModel as _BaseModel

    class _SubmitRequest(_BaseModel):
        content: str
        format: str = "markdown"
        metadata: dict | None = None
        traj_id: str | None = None

    @app.post("/api/v1/trajectories/submit")
    async def api_submit_trajectory(req: _SubmitRequest):
        try:
            result = submit_trajectory(
                content=req.content,
                format=req.format,
                metadata=req.metadata or {},
                traj_id=req.traj_id,
                traj_dir=_traj_dir,
            )
            return result
        except Exception as e:
            raise HTTPException(status_code=400, detail=str(e))

    # -- Watcher status endpoint (must be before SPA mount) --
    @app.get("/api/v1/watcher/status")
    async def api_watcher_status():
        if _watcher_ref.get("instance"):
            return _watcher_ref["instance"].stats
        return {"running": False, "message": "watcher not started"}

    # ------------------------------------------------------------------
    # Static UI mount (SPA) -- must be registered AFTER all /api/* routes
    # ------------------------------------------------------------------
    _dist_dir = Path(__file__).parent / "web" / "dist"
    _ui_disabled = os.environ.get("T2S_NO_UI", "").lower() in ("1", "true", "yes")

    if _dist_dir.is_dir() and not _ui_disabled:
        _index_file = _dist_dir / "index.html"
        _dist_root = _dist_dir.resolve()

        from starlette.exceptions import HTTPException as StarletteHTTPException

        class _SPAStaticFiles(StaticFiles):
            async def get_response(self, path: str, scope):
                request_path = scope.get("path", "") or ""
                if request_path.startswith("/api/") or request_path == "/api":
                    raise StarletteHTTPException(status_code=404)
                try:
                    response = await super().get_response(path, scope)
                except StarletteHTTPException as exc:
                    if exc.status_code == 404 and _index_file.is_file():
                        return FileResponse(str(_index_file))
                    raise
                if response.status_code == 404 and _index_file.is_file():
                    return FileResponse(str(_index_file))
                return response

        app.mount(
            "/",
            _SPAStaticFiles(directory=str(_dist_dir), html=True),
            name="ui",
        )

    @app.on_event("startup")
    async def _startup():
        """Initialize skill_tools context so search_skills / rebuild_skill_index work."""
        llm = None
        embed = None
        try:
            llm = create_llm_client(_config)
        except Exception:
            logger.warning("LLM client not configured", exc_info=True)
        try:
            embed = create_embed_client(_config)
        except Exception:
            logger.warning("Embed client not configured — indexing will be skipped", exc_info=True)
        try:
            init_context(
                skill_dir=_skill_dir,
                data_dir=_traj_dir,
                llm_client=llm,
                embed_client=embed,
                config=_config,
            )
            logger.info(
                "xskill server ready  skill_dir=%s  traj_dir=%s  llm=%s  embed=%s",
                _skill_dir, _traj_dir,
                "ok" if llm else "NONE",
                "ok" if embed else "NONE",
            )
        except Exception:
            logger.warning("init_context failed", exc_info=True)

        # Auto-register chat archive directory
        try:
            from xskill.registry import register_dir
            register_dir(_chat_archive_dir, label="chat_archive", ecosystem="manual")
            logger.info("chat archive dir registered: %s", _chat_archive_dir)
        except Exception:
            logger.warning("failed to register chat archive dir", exc_info=True)

        # Auto-detect known agent ecosystems on this host and bridge them in.
        # For each detected source (e.g. ~/.claude/projects), we register a
        # paired bridge dir (~/.xskill/cc_sessions) and start an ingester
        # thread that periodically mirrors native sessions into it as
        # traj_*.md. After that the regular watcher takes over.
        try:
            from xskill.ecosystems import (
                detect_known_ecosystems, CCSessionIngester,
                JsonlIngester, SqliteIngester,
                CODEX_SPEC, OPENCODE_SPEC,
                install_all_to_claude_code,
                install_all_to_codex,
                install_all_to_opencode,
            )
            from xskill.config import XSKILL_HOME
            from xskill.install_history import InstallHistory
            from xskill.registry import register_dir

            # install_history.jsonl 是 daemon 全局状态（与 registry.db 同级），
            # 不属于任一 skill；落到 ~/.xskill/ 根而不是 skill_dir。否则 ls
            # ~/.xskill/skill/ 会把它误识成 skill 名（实跑遇到的 bug）。
            install_history_path = XSKILL_HOME / "install_history.jsonl"
            install_history = InstallHistory(install_history_path)

            detections = detect_known_ecosystems(home_root=_home_root())
            poll_interval = float(_config.get("watcher", {}).get("poll_interval", 10))

            for det in detections:
                bridge: Path = det["bridge"]
                bridge.mkdir(parents=True, exist_ok=True)
                register_dir(
                    bridge,
                    label=f"{det['ecosystem']} sessions",
                    ecosystem=det["ecosystem"],
                )

                if det["ecosystem"] == "claude_code":
                    # 启动时先把现有 skill 全部装 main 到 ~/.claude/skills/，
                    # 同时往 install_history append 起始记录。后续 ingester
                    # 见到新 session 才有依据查"那一刻装的是哪 side"。
                    try:
                        installed = install_all_to_claude_code(
                            _skill_dir, target_root=_home_root(),
                        )
                        for dest in installed:
                            install_history.record(
                                skill=dest.parent.name, side="main",
                                sha="",
                            )
                        logger.info(
                            "startup install_all_to_claude_code: %d skills installed",
                            len(installed),
                        )
                    except Exception:
                        logger.warning(
                            "startup install_all_to_claude_code failed",
                            exc_info=True,
                        )
                    ingester = CCSessionIngester(
                        target_traj_dir=bridge,
                        home_root=_home_root(),
                        poll_interval=poll_interval,
                        skill_dir=_skill_dir,
                        target_root=_home_root(),
                        history_path=install_history_path,
                        assignments_path=_skill_dir / "session_assignments.jsonl",
                    )
                    ingester.start()
                    _watcher_ref[f"ingester_{det['ecosystem']}"] = ingester

                elif det["ecosystem"] == "codex":
                    # Codex CLI 写 ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl
                    # 形态与 CC JSONL 一致——复用 JsonlIngester(CODEX_SPEC)。
                    # Skill install 走 ~/.agents/skills/ (Codex/OpenCode 共享)。
                    try:
                        installed = install_all_to_codex(
                            _skill_dir, target_root=_home_root(),
                        )
                        logger.info(
                            "startup install_all_to_codex: %d skills installed to ~/.agents/skills/",
                            len(installed),
                        )
                    except Exception:
                        logger.warning(
                            "startup install_all_to_codex failed", exc_info=True,
                        )
                    # JsonlIngester 当前没有内置 daemon 线程；走一次性 scan_and_bridge
                    # 由后续每轮 watcher poll 通过 register_dir + bridge_subpath 自动
                    # 走 split → embed → cluster 链路。
                    try:
                        bridged = JsonlIngester(CODEX_SPEC).scan_and_bridge(
                            target_traj_dir=bridge,
                            home_root=_home_root(),
                        )
                        logger.info(
                            "startup codex ingester: bridged %d sessions to %s",
                            len(bridged), bridge,
                        )
                    except Exception:
                        logger.warning("startup codex ingester failed", exc_info=True)

                elif det["ecosystem"] == "opencode":
                    # OpenCode 走 SQLite + WAL（~/.local/share/opencode/opencode.db）
                    # —— SqliteIngester 用 ?mode=ro&immutable=1 避免 WAL 锁。
                    # Skill install 走 ~/.agents/skills/ (Codex/OpenCode 共享；
                    # 与 codex install_all 重复 install 同一目录是 idempotent)。
                    try:
                        installed = install_all_to_opencode(
                            _skill_dir, target_root=_home_root(),
                        )
                        logger.info(
                            "startup install_all_to_opencode: %d skills installed to ~/.agents/skills/",
                            len(installed),
                        )
                    except Exception:
                        logger.warning(
                            "startup install_all_to_opencode failed", exc_info=True,
                        )
                    try:
                        bridged = SqliteIngester(
                            target_traj_dir=bridge,
                            home_root=_home_root(),
                            spec=OPENCODE_SPEC,
                        ).run_once()
                        logger.info(
                            "startup opencode ingester: bridged %d sessions to %s",
                            len(bridged), bridge,
                        )
                    except Exception:
                        logger.warning(
                            "startup opencode ingester failed", exc_info=True,
                        )

                logger.info(
                    "ecosystem %s detected: source=%s bridge=%s",
                    det["ecosystem"], det["source"], bridge,
                )
        except Exception:
            logger.warning("ecosystem auto-detect failed", exc_info=True)

        # Start watcher if any dirs are registered
        try:
            from xskill.registry import list_watch_dirs
            from xskill.watcher import DirectoryWatcher
            dirs = list_watch_dirs()
            if dirs:
                watcher_cfg = _config.get("watcher", {})
                watcher = DirectoryWatcher(
                    llm=llm, embed_client=embed, config=_config,
                    skill_dir=_skill_dir,
                    poll_interval=float(watcher_cfg.get("poll_interval", 30)),
                    max_concurrent=int(watcher_cfg.get("max_concurrent", 30)),
                    cold_start_threshold=int(watcher_cfg.get("cold_start_threshold", 3)),
                )
                watcher.start()
                _watcher_ref["instance"] = watcher
                logger.info("watcher started, monitoring %d directories", len(dirs))
        except Exception:
            logger.warning("watcher startup failed", exc_info=True)

    @app.on_event("shutdown")
    async def _shutdown():
        watcher = _watcher_ref.get("instance")
        if watcher:
            watcher.stop()
        # Stop any ecosystem ingesters started in startup.
        for k, v in list(_watcher_ref.items()):
            if k.startswith("ingester_"):
                try:
                    v.stop()
                except Exception:
                    logger.warning("failed to stop %s", k, exc_info=True)

    return app
