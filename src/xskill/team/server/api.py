"""api.py — team server 的 /api/v1/team/* 路由（SP1）

team server 的 5 个端点。鉴权：除 register 外都校验
``X-Xskill-Token`` == join token 且 ``X-Xskill-Client`` 在注册表里。
client 完全信任 server；token 只挡组织外随机接入。

上下文（join_token / registry / skill_dir / traj_root / canary 参数）通过
``init_team_context`` 注入到模块级单例——沿用 ``skill_tools.init_context``
的既有模式，不引入 FastAPI Depends 体系。
"""
from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Callable

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import Response

from xskill.team.server.client_registry import ClientRegistry
from xskill.team.shared.git_bundle import fetch_branch_from_bundle, make_repo_bundle
from xskill.team.server.skill_manifest import build_manifest
from xskill.team.shared.protocol import (
    PushEditResponse, RegisterRequest, RegisterResponse,
    UploadRejection, UploadRequest, UploadResponse,
)

logger = logging.getLogger("xskill.team.server.api")
router = APIRouter(prefix="/api/v1/team")


class _Ctx:
    """模块级上下文单例。init_team_context 填，端点读。"""
    join_token: str = ""
    client_registry: ClientRegistry | None = None
    skill_dir: Path | None = None
    traj_root: Path | None = None
    probability: float = 0.2
    ranked_slots: int = 80
    total_slots: int = 100
    register_dir: Callable[[Path, str], None] | None = None


_ctx = _Ctx()


def init_team_context(
    *,
    join_token: str,
    client_registry: ClientRegistry,
    skill_dir: Path,
    traj_root: Path,
    probability: float,
    ranked_slots: int,
    total_slots: int,
    register_dir: Callable[[Path, str], None],
) -> None:
    """create_app(team_server=True) 在 startup 时调用一次。"""
    _ctx.join_token = join_token
    _ctx.client_registry = client_registry
    _ctx.skill_dir = Path(skill_dir)
    _ctx.traj_root = Path(traj_root)
    _ctx.probability = probability
    _ctx.ranked_slots = ranked_slots
    _ctx.total_slots = total_slots
    _ctx.register_dir = register_dir


def _auth(token: str | None, client_id: str | None) -> str:
    """校验 token + client_id，返回 client_id。失败抛 HTTPException。"""
    if _ctx.client_registry is None:
        raise HTTPException(status_code=503, detail="team context not initialized")
    if not token or token != _ctx.join_token:
        raise HTTPException(status_code=401, detail="invalid join token")
    if not client_id or not _ctx.client_registry.exists(client_id):
        raise HTTPException(status_code=403, detail="unknown client_id")
    _ctx.client_registry.touch(client_id)
    return client_id


@router.post("/register", response_model=RegisterResponse)
async def team_register(req: RegisterRequest) -> RegisterResponse:
    if _ctx.client_registry is None:
        raise HTTPException(status_code=503, detail="team context not initialized")
    if req.token != _ctx.join_token:
        raise HTTPException(status_code=401, detail="invalid join token")
    client_id = _ctx.client_registry.register(
        label=req.client_label,
        hostname=req.hostname,
        claimed_client_id=req.claimed_client_id,
    )
    logger.info("team client registered: %s (label=%s)", client_id, req.client_label)
    return RegisterResponse(client_id=client_id)


@router.post("/upload", response_model=UploadResponse)
async def team_upload(
    req: UploadRequest,
    x_xskill_token: str | None = Header(default=None),
    x_xskill_client: str | None = Header(default=None),
) -> UploadResponse:
    client_id = _auth(x_xskill_token, x_xskill_client)
    sessions_dir = _ctx.traj_root / "clients" / client_id / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    # 该 client 桶首次出现 → 注册成 watch_dir，label=client_id 让 watcher
    # 在 CS 归因时能反查 client。register_dir 幂等。
    if _ctx.register_dir is not None:
        _ctx.register_dir(sessions_dir, client_id)

    accepted: list[str] = []
    rejected: list[UploadRejection] = []
    for t in req.trajectories:
        if not t.traj_id.startswith("traj_"):
            rejected.append(UploadRejection(traj_id=t.traj_id,
                                            reason="traj_id must start with 'traj_'"))
            continue
        actual = hashlib.sha256(t.content.encode("utf-8")).hexdigest()
        # sha256 不匹配 → 传输损坏，拒收（CLAUDE.md：遇问题 throw，不静默接受）
        if t.sha256 and actual != t.sha256:
            rejected.append(UploadRejection(traj_id=t.traj_id, reason="sha256 mismatch"))
            continue
        # model / harness 非空时先落 .json sidecar，再落 .md：watcher 只 glob
        # traj_*.md，必须保证它发现新 .md 时同名 sidecar 已就位，否则 discover 会
        # INSERT source_model/source_harness=NULL 且永不回读（已存在的行只更 mtime）。
        sidecar = {}
        if t.model:
            sidecar["model"] = t.model
        if t.harness:
            sidecar["harness"] = t.harness
        if sidecar:
            (sessions_dir / f"{t.traj_id}.json").write_text(
                json.dumps(sidecar, ensure_ascii=False), encoding="utf-8")
        (sessions_dir / f"{t.traj_id}.md").write_text(t.content, encoding="utf-8")
        accepted.append(t.traj_id)
    logger.info("team upload from %s: %d accepted, %d rejected",
                client_id, len(accepted), len(rejected))
    return UploadResponse(accepted=accepted, rejected=rejected)


@router.get("/sync")
async def team_sync(
    x_xskill_token: str | None = Header(default=None),
    x_xskill_client: str | None = Header(default=None),
):
    client_id = _auth(x_xskill_token, x_xskill_client)
    resp = build_manifest(
        client_id=client_id,
        skill_dir=_ctx.skill_dir,
        probability=_ctx.probability,
        ranked_slots=_ctx.ranked_slots,
        total_slots=_ctx.total_slots,
        traj_root=_ctx.traj_root,
    )
    return resp.model_dump()


@router.get("/skill/{name}/bundle")
async def team_skill_bundle(
    name: str,
    x_xskill_token: str | None = Header(default=None),
    x_xskill_client: str | None = Header(default=None),
) -> Response:
    _auth(x_xskill_token, x_xskill_client)
    repo_dir = _ctx.skill_dir / name
    if not (repo_dir / ".git").is_dir():
        raise HTTPException(status_code=404, detail=f"skill not found: {name}")
    bundle = make_repo_bundle(repo_dir)
    return Response(content=bundle, media_type="application/octet-stream")


@router.post("/push-edit", response_model=PushEditResponse)
async def team_push_edit(
    request: Request,
    x_xskill_token: str | None = Header(default=None),
    x_xskill_client: str | None = Header(default=None),
    x_xskill_skill: str | None = Header(default=None),
) -> PushEditResponse:
    client_id = _auth(x_xskill_token, x_xskill_client)
    if not x_xskill_skill:
        raise HTTPException(status_code=400, detail="X-Xskill-Skill header required")
    repo_dir = _ctx.skill_dir / x_xskill_skill
    if not (repo_dir / ".git").is_dir():
        raise HTTPException(status_code=404, detail=f"skill not found: {x_xskill_skill}")
    bundle = await request.body()
    if not bundle:
        raise HTTPException(status_code=400, detail="empty bundle")
    dest_ref = f"refs/heads/user-staging/{client_id}"
    sha = fetch_branch_from_bundle(bundle, repo_dir, "_useredit", dest_ref)
    logger.info("team push-edit: %s -> %s (%s)", x_xskill_skill, dest_ref, sha[:8])
    return PushEditResponse(branch=f"user-staging/{client_id}", ref_sha=sha)
