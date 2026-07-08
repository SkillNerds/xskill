"""api.py — team server 的 /api/v1/team/* 路由（SP1）

team server 的 5 个端点。鉴权：除 register 外都校验
``X-Xskill-Token`` == join token 且 ``X-Xskill-Client`` 在注册表里。
client 完全信任 server；token 只挡组织外随机接入。

上下文（join_token / registry / skill_dir / traj_root / canary 参数）通过
``init_team_context`` 注入到模块级单例——沿用 agent 工具配置的单例风格
的既有模式，不引入 FastAPI Depends 体系。
"""
from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import logging
from pathlib import Path
import tempfile
import threading
from typing import Callable
import zipfile

from fastapi import APIRouter, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, Response

from xskill import __version__ as XSKILL_VERSION
from xskill.config import get_team_server_whl_dir
from xskill.team.server.client_registry import ClientRegistry
from xskill.team.shared.git_bundle import fetch_branch_from_bundle, make_repo_bundle
from xskill.team.server.skill_manifest import build_manifest
from xskill.team.shared.protocol import (
    PushEditResponse, RegisterRequest, RegisterResponse,
    UploadRejection, UploadRequest, UploadResponse,
)
from xskill.utils.sanitize import sanitize_trajectory_text

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
    allow_anonymous_user: bool = True
    register_dir: Callable[[Path, str], None] | None = None
    skillhub = None


_ctx = _Ctx()
_WHEEL_BUILD_LOCK = threading.Lock()


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
    allow_anonymous_user: bool = True,
    skillhub=None,
) -> None:
    """create_app(team_server=True) 在 startup 时调用一次。"""
    _ctx.join_token = join_token
    _ctx.client_registry = client_registry
    _ctx.skill_dir = Path(skill_dir)
    _ctx.traj_root = Path(traj_root)
    _ctx.probability = probability
    _ctx.ranked_slots = ranked_slots
    _ctx.total_slots = total_slots
    _ctx.allow_anonymous_user = allow_anonymous_user
    _ctx.register_dir = register_dir
    _ctx.skillhub = skillhub


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


def _find_server_wheel(package: str = "xskill", version: str | None = None) -> Path | None:
    """从 ~/.xskill/whls 中选择与 server 当前版本严格匹配的 wheel。"""
    from packaging.utils import canonicalize_name, parse_wheel_filename
    from packaging.version import Version

    want_name = canonicalize_name(package)
    version = version or XSKILL_VERSION
    try:
        want_version = Version(version)
    except Exception:
        logger.debug("invalid xskill version for wheel lookup: %s", version, exc_info=True)
        return None

    matches: list[Path] = []
    for path in sorted(get_team_server_whl_dir().glob("*.whl")):
        try:
            name, wheel_version, _build, _tags = parse_wheel_filename(path.name)
        except Exception:
            logger.debug("skip invalid wheel filename: %s", path, exc_info=True)
            continue
        if canonicalize_name(str(name)) == want_name and wheel_version == want_version:
            matches.append(path)
    return matches[-1] if matches else None


def _ensure_server_wheel(package: str = "xskill", version: str | None = None) -> Path | None:
    """返回 server wheel；缓存缺失时从当前已安装 distribution 懒生成。"""
    version = version or XSKILL_VERSION
    wheel = _find_server_wheel(package=package, version=version)
    if wheel is not None:
        return wheel
    with _WHEEL_BUILD_LOCK:
        wheel = _find_server_wheel(package=package, version=version)
        if wheel is not None:
            return wheel
        try:
            return _build_installed_distribution_wheel(package, version)
        except Exception:
            logger.warning("failed to build server wheel for %s==%s",
                           package, version, exc_info=True)
            return None


def _build_installed_distribution_wheel(package: str, version: str) -> Path | None:
    """把当前环境中已安装的 package 重组为 wheel，并缓存到 ~/.xskill/whls。

    这里不依赖源码 checkout，也不运行 ``python -m build``；只读取已安装
    distribution 的 package 文件和 dist-info 元数据，重写 wheel 的 RECORD。
    """
    from importlib.metadata import PackageNotFoundError, distribution
    from packaging.utils import canonicalize_name
    from packaging.version import Version

    try:
        dist = distribution(package)
    except PackageNotFoundError:
        logger.warning("cannot build server wheel: distribution not found: %s", package)
        return None

    try:
        if Version(dist.version) != Version(version):
            logger.warning("cannot build server wheel: installed %s==%s, server version=%s",
                           package, dist.version, version)
            return None
    except Exception:
        logger.warning("cannot build server wheel: invalid version (%s, %s)",
                       dist.version, version, exc_info=True)
        return None

    files = list(dist.files or [])
    if not files:
        logger.warning("cannot build server wheel: distribution file list unavailable")
        return None

    dist_info_dir = _distribution_dist_info_dir(files)
    if not dist_info_dir:
        logger.warning("cannot build server wheel: dist-info directory not found")
        return None
    if _distribution_is_editable(dist, files, dist_info_dir):
        logger.warning("cannot build server wheel from editable install: %s", package)
        return None

    package_root = canonicalize_name(package).replace("-", "_")
    entries = _distribution_wheel_entries(dist, files, dist_info_dir, package_root)
    names = {name for name, _path in entries}
    required = {f"{dist_info_dir}/METADATA", f"{dist_info_dir}/WHEEL"}
    missing = sorted(required - names)
    if missing:
        logger.warning("cannot build server wheel: missing metadata files: %s", missing)
        return None
    if not any(name.startswith(f"{package_root}/") for name in names):
        logger.warning("cannot build server wheel: package files not found: %s",
                       package_root)
        return None

    tags = _distribution_wheel_tags(dist, dist_info_dir)
    wheel_name = _wheel_filename(package, version, tags)
    wheel_dir = get_team_server_whl_dir()
    wheel_dir.mkdir(parents=True, exist_ok=True)
    dest = wheel_dir / wheel_name
    tmp = tempfile.NamedTemporaryFile(
        prefix=f".{wheel_name}.", suffix=".tmp", dir=wheel_dir, delete=False,
    )
    tmp_path = Path(tmp.name)
    tmp.close()
    try:
        _write_wheel_zip(entries, dist_info_dir, tmp_path)
        tmp_path.replace(dest)
        logger.info("generated server wheel: %s", dest)
        return dest
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def _distribution_dist_info_dir(files) -> str | None:
    for file in files:
        rel = _dist_file_rel(file)
        first = rel.split("/", 1)[0]
        if first.endswith(".dist-info"):
            return first
    return None


def _distribution_is_editable(dist, files, dist_info_dir: str) -> bool:
    direct_url = f"{dist_info_dir}/direct_url.json"
    for file in files:
        if _dist_file_rel(file) != direct_url:
            continue
        path = Path(dist.locate_file(file))
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return False
        return bool(data.get("dir_info", {}).get("editable"))
    return False


def _distribution_wheel_entries(
    dist,
    files,
    dist_info_dir: str,
    package_root: str,
) -> list[tuple[str, Path]]:
    skip_dist_info = {"RECORD", "INSTALLER", "REQUESTED", "direct_url.json"}
    entries: dict[str, Path] = {}
    for file in files:
        rel = _dist_file_rel(file)
        if not rel or rel.startswith("../") or rel.startswith("/"):
            continue
        parts = rel.split("/")
        if "__pycache__" in parts or rel.endswith(".pyc"):
            continue
        if parts[0] == package_root:
            pass
        elif parts[0] == dist_info_dir:
            if parts[-1] in skip_dist_info:
                continue
        else:
            continue
        path = Path(dist.locate_file(file))
        if path.is_file():
            entries[rel] = path
    return sorted(entries.items())


def _distribution_wheel_tags(dist, dist_info_dir: str) -> str:
    wheel_file = Path(dist.locate_file(f"{dist_info_dir}/WHEEL"))
    try:
        text = wheel_file.read_text(encoding="utf-8")
    except Exception:
        return "py3-none-any"
    tags = [
        line.split(":", 1)[1].strip()
        for line in text.splitlines()
        if line.lower().startswith("tag:")
    ]
    return ".".join(tags) if tags else "py3-none-any"


def _wheel_filename(package: str, version: str, tags: str) -> str:
    from packaging.utils import canonicalize_name

    name = canonicalize_name(package).replace("-", "_")
    safe_version = str(version).replace("-", "_")
    return f"{name}-{safe_version}-{tags}.whl"


def _dist_file_rel(file) -> str:
    return str(file).replace("\\", "/")


def _write_wheel_zip(
    entries: list[tuple[str, Path]],
    dist_info_dir: str,
    dest: Path,
) -> None:
    records: list[tuple[str, str, str]] = []
    with zipfile.ZipFile(dest, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for rel, path in entries:
            data = path.read_bytes()
            zf.writestr(rel, data)
            digest = base64.urlsafe_b64encode(
                hashlib.sha256(data).digest(),
            ).rstrip(b"=").decode("ascii")
            records.append((rel, f"sha256={digest}", str(len(data))))

        record_rel = f"{dist_info_dir}/RECORD"
        buf = io.StringIO()
        writer = csv.writer(buf, lineterminator="\n")
        for row in records:
            writer.writerow(row)
        writer.writerow((record_rel, "", ""))
        zf.writestr(record_rel, buf.getvalue().encode("utf-8"))


@router.get("/version")
async def team_version(
    x_xskill_token: str | None = Header(default=None),
    x_xskill_client: str | None = Header(default=None),
) -> dict:
    """返回 server 当前 xskill 版本，以及同版本 wheel 是否可下载。"""
    _auth(x_xskill_token, x_xskill_client)
    wheel = _ensure_server_wheel()
    return {
        "package": "xskill",
        "version": XSKILL_VERSION,
        "wheel_available": wheel is not None,
        "wheel_filename": wheel.name if wheel else None,
    }


@router.get("/wheel")
async def team_wheel(
    x_xskill_token: str | None = Header(default=None),
    x_xskill_client: str | None = Header(default=None),
) -> FileResponse:
    """下载 server 当前版本对应的 xskill wheel。"""
    _auth(x_xskill_token, x_xskill_client)
    wheel = _ensure_server_wheel()
    if wheel is None:
        raise HTTPException(status_code=404, detail="xskill wheel not found")
    return FileResponse(
        wheel,
        media_type="application/octet-stream",
        filename=wheel.name,
    )


@router.post("/register", response_model=RegisterResponse)
async def team_register(req: RegisterRequest) -> RegisterResponse:
    if _ctx.client_registry is None:
        raise HTTPException(status_code=503, detail="team context not initialized")
    if req.token != _ctx.join_token:
        raise HTTPException(status_code=401, detail="invalid join token")
    user_name = (req.user_name or "").strip() or None
    if not user_name and not _ctx.allow_anonymous_user:
        raise HTTPException(
            status_code=403, detail="anonymous users not allowed"
        )
    client_id = _ctx.client_registry.register(
        label=req.client_label,
        hostname=req.hostname,
        claimed_client_id=req.claimed_client_id,
        user_name=user_name,
    )
    logger.info("team client registered: %s (label=%s, name=%s)",
                client_id, req.client_label, user_name or "<anonymous>")
    return RegisterResponse(client_id=client_id)


@router.post("/upload", response_model=UploadResponse)
async def team_upload(
    req: UploadRequest,
    x_xskill_token: str | None = Header(default=None),
    x_xskill_client: str | None = Header(default=None),
) -> UploadResponse:
    client_id = _auth(x_xskill_token, x_xskill_client)
    # 目录名优先用 user_name 明文（可读），匿名用 client_id；canary/git 分支仍用 client_id
    from xskill.team.server.client_registry import safe_dir_name
    _row = _ctx.client_registry.get(client_id)
    _dir_name = safe_dir_name((_row or {}).get("user_name") or None, client_id)
    sessions_dir = _ctx.traj_root / "clients" / _dir_name / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    # 该 client 桶首次出现 → 注册成 watch_dir，label=dir_name 让 watcher
    # 在 CS 归因时能反查 client（dir_name = user_name 明文或 client_id）。
    if _ctx.register_dir is not None:
        _ctx.register_dir(sessions_dir, _dir_name)

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
        # sha256 完整性校验已过（上面），落盘前再做一遍内容清洗：客户端桥接常把
        # 终端 ANSI 码 / 控制字符灌进 .md，会让 splitlines 行号错位、污染模型输入。
        clean = sanitize_trajectory_text(t.content)
        (sessions_dir / f"{t.traj_id}.md").write_text(clean, encoding="utf-8")
        accepted.append(t.traj_id)
    logger.info("team upload from %s: %d accepted, %d rejected",
                client_id, len(accepted), len(rejected))
    return UploadResponse(accepted=accepted, rejected=rejected)


@router.post("/ingest-db")
async def team_ingest_db(
    file: UploadFile = File(...),
    eco: str = Form("ngagent"),
    x_xskill_token: str | None = Header(default=None),
    x_xskill_client: str | None = Header(default=None),
) -> dict:
    """收一个原始 db 文件（ngagent/opencode SQLite），落盘后桥接入库。

    给没装 sshpass / 不愿手敲密码的 Windows 用户用：``upload_ngagent_db.ps1``
    直接 POST db 文件到这里，免 scp。落盘到 ``uploads/<eco>/<client_id>/``，
    再 ``read_db_files`` 桥成 traj 落到该 client 的 sessions 桶（label=client_id
    让 watcher 做 CS 归因），watcher 后续按常规流水线出 skill。
    """
    client_id = _auth(x_xskill_token, x_xskill_client)

    from xskill.config import get_uploads_dir
    from xskill.pipeline.db_ingest import read_db_files
    from xskill.team.server.client_registry import safe_dir_name

    _row = _ctx.client_registry.get(client_id)
    _dir_name = safe_dir_name((_row or {}).get("user_name") or None, client_id)

    # 落盘：uploads/<eco>/<client_id>/<安全文件名>
    safe_name = Path(file.filename or "upload.db").name
    dest_dir = get_uploads_dir() / eco / client_id
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / safe_name
    dest.write_bytes(await file.read())

    # 桥接到该 client 的 sessions 桶，label=dir_name（与 team_upload 一致）
    sessions_dir = _ctx.traj_root / "clients" / _dir_name / "sessions"
    try:
        summary = read_db_files(
            dest, eco=eco, target_dir=sessions_dir, register_label=_dir_name,
        )
    except (FileNotFoundError, ValueError) as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    logger.info("team ingest-db from %s: %s → bridged %d traj",
                client_id, safe_name, summary["bridged"])
    return {"client_id": client_id, "saved": str(dest),
            "bridged": summary["bridged"]}


@router.get("/sync")
async def team_sync(
    x_xskill_token: str | None = Header(default=None),
    x_xskill_client: str | None = Header(default=None),
):
    client_id = _auth(x_xskill_token, x_xskill_client)
    # §5 sync 前刷新该 client 的用户画像（atom 集变化时重算，未变则指纹命中跳过）。
    # 画像由 build_manifest → _pick_recommended → engine.get_skill_for_client 消费。
    eng = None
    try:
        from xskill.team.server.skill_manifest import get_recommend_engine
        eng = get_recommend_engine()
    except Exception:  # pylint: disable=broad-exception-caught
        eng = None
    if eng is not None:
        try:
            from xskill.recommend.client_interest import ClientInterest
            eng.update_user_interest(ClientInterest(client_id))
        except Exception:  # pylint: disable=broad-exception-caught
            logger.debug("profile refresh for %s skipped", client_id, exc_info=True)
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
        hub = _ctx.skillhub
        if hub is None:
            raise HTTPException(status_code=404, detail=f"skill not found: {name}")
        hub_dir = hub.skill_path(name)
        if hub_dir is None:
            raise HTTPException(status_code=404, detail=f"skill not found: {name}")
        archive = _make_skillhub_archive(hub_dir)
        return Response(content=archive, media_type="application/zip")
    bundle = make_repo_bundle(repo_dir)
    return Response(content=bundle, media_type="application/octet-stream")


def _make_skillhub_archive(skill_dir: Path) -> bytes:
    """Pack a non-git skillhub directory as a zip archive for thin clients."""
    skill_dir = Path(skill_dir)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for p in sorted(skill_dir.rglob("*")):
            if p.is_file():
                zf.write(p, p.relative_to(skill_dir).as_posix())
    return buf.getvalue()


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
