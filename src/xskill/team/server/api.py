"""api.py — team server 的 /api/v1/team/* 路由（SP1）

team server 的 5 个端点。鉴权：除 register 外都校验
``X-Xskill-Token`` == join token 且 ``X-Xskill-Client`` 在注册表里。
client 完全信任 server；token 只挡组织外随机接入。

上下文（join_token / registry / skill_dir / traj_root / canary 参数）通过
``init_team_context`` 注入到模块级单例——沿用 agent 工具配置的单例风格
的既有模式，不引入 FastAPI Depends 体系。
"""
from __future__ import annotations

import asyncio
import base64
from concurrent.futures import ThreadPoolExecutor
import csv
from functools import partial
import hashlib
import io
import json
import logging
from pathlib import Path
import secrets
import shutil
import tempfile
import threading
import time
from typing import Callable
import zipfile

from dulwich.errors import NotGitRepository, ObjectMissing
from fastapi import APIRouter, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from starlette.concurrency import run_in_threadpool

from xskill import __version__ as XSKILL_VERSION
from xskill.canary import main_sha, staging_sha
from xskill.config import get_team_server_whl_dir
from xskill.team.server.client_registry import ClientRegistry
from xskill.team.shared.git_bundle import (
    fetch_branch_from_bundle, make_repo_archive, make_repo_bundle,
)
from xskill.team.server.skill_manifest import (
    _resolve_slot,
    build_manifest,
    get_recommend_engine,
    manifest_catalog_snapshot,
    repo_search_id,
)
from xskill.team.shared.protocol import (
    GenerateAccepted, GenerateRequest, PushEditResponse, RegisterRequest, RegisterResponse,
    UploadRejection, UploadRequest, UploadResponse,
)
from xskill.utils.sanitize import sanitize_trajectory_text

logger = logging.getLogger("xskill.team.server.api")
server_logger = logging.getLogger("xskill.server")
router = APIRouter(prefix="/api/v1/team")

_SKILL_ARCHIVE_MAX_FILES = 2048
_SKILL_ARCHIVE_MAX_FILE_BYTES = 50 * 1024 * 1024
_SKILL_ARCHIVE_MAX_TOTAL_BYTES = 100 * 1024 * 1024
_REPO_SEARCH_GRANT_TTL_SECONDS = 300.0
_REPO_SEARCH_GRANT_CAPACITY = 4096
_REPO_SEARCH_GRANTS: dict[tuple[str, str], tuple[float, str, str, str]] = {}
_REPO_SEARCH_GRANTS_LOCK = threading.Lock()


class _Ctx:
    """模块级上下文单例。init_team_context 填，端点读。

    这里**只放进程级资源/接线对象**（重启域）。纯配置开关（skill_slots /
    ranked_slots / canary.probability / allow_anonymous_user）**刻意不放这里**
    ——它们要热生效，由 ``live_manifest_tuning()`` / 端点内 ``config.*`` 每请求
    现取 live config，见 ``live_manifest_tuning`` 的 docstring。
    """
    join_token: str = ""
    client_registry: ClientRegistry | None = None
    skill_dir: Path | None = None
    traj_root: Path | None = None
    register_dir: Callable[[Path, str], None] | None = None
    configure_watch_dir: Callable[[Path, str, bool], None] | None = None
    skillhub = None
    profile_refresh_service = None


_ctx = _Ctx()


def team_context() -> _Ctx:
    """取 team server 上下文单例（跨模块公开入口）。

    dashboard 控制面等外部模块用它拿 skill_dir / client_registry 等**进程级**
    引用；调优数字不在这里，走 ``live_manifest_tuning()``（热生效）。
    """
    return _ctx


def live_manifest_tuning() -> tuple[int, int, float]:
    """现取 ``(total_slots, ranked_slots, probability)``——热生效的唯一来源。

    ``admin_config_reload`` 会**原地 mutate** ``app.\\_config`` dict（console.py
    的热加载实现），故每请求现取该 dict 即天然热生效，无需重启、无需回写
    ``_ctx``（照 app.py 的 /canary 状态端点同款读法）。曾把这三个值快照进
    ``_ctx``（仅 startup 填一次），导致面板改完静默不生效、必须重启 serve。

    在函数内 import：``app`` 模块 import 本模块，模块级 import 会循环。
    """
    from xskill.api import app as app_mod
    from xskill.canary import CanaryConfig
    from xskill.config import team_server_slots_config

    cfg = app_mod._config or {}  # pylint: disable=protected-access
    slots = team_server_slots_config(cfg)
    probability = CanaryConfig.from_dict(cfg.get("canary", {}) or {}).probability
    return slots["skill_slots"], slots["ranked_slots"], probability


_WHEEL_BUILD_LOCK = threading.Lock()
_SYNC_EXECUTOR_STATE = "xskill_team_sync_executor"
_TELEMETRY_EXECUTOR_STATE = "xskill_team_telemetry_executor"
_MANIFEST_CONTROL_CACHE_TTL = 5.0
_MANIFEST_CONTROL_CACHE: dict[str, tuple[float, dict]] = {}
_MANIFEST_CONTROL_CACHE_LOCK = threading.Lock()


class _BoundedExecutor:
    """拒绝超出上限的后台任务，避免慢 SQLite 写入无限堆积。"""

    def __init__(self, *, max_workers: int, max_pending: int) -> None:
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="xskill-team-telemetry",
        )
        self._slots = threading.BoundedSemaphore(max_pending)
        self._lock = threading.Lock()
        self._closed = False

    def submit(self, func: Callable[[], None]) -> bool:
        if not self._slots.acquire(blocking=False):
            return False
        with self._lock:
            if self._closed:
                self._slots.release()
                return False
            try:
                future = self._executor.submit(func)
            except RuntimeError:
                self._slots.release()
                return False
        future.add_done_callback(self._release_slot)
        return True

    def _release_slot(self, _future) -> None:
        """done callback：任务落地（成功/异常/取消）后归还并发槽位。"""
        self._slots.release()

    def shutdown(self) -> None:
        with self._lock:
            self._closed = True
        self._executor.shutdown(wait=True)


def start_team_sync_executor(
    app,
    *,
    max_workers: int = 32,
) -> ThreadPoolExecutor:
    """为单个 team app 创建独立的 ``/sync`` 线程池。"""
    existing = getattr(app.state, _SYNC_EXECUTOR_STATE, None)
    if existing is not None:
        return existing
    executor = ThreadPoolExecutor(
        max_workers=max_workers,
        thread_name_prefix="xskill-team-sync",
    )
    setattr(app.state, _SYNC_EXECUTOR_STATE, executor)
    setattr(
        app.state,
        _TELEMETRY_EXECUTOR_STATE,
        _BoundedExecutor(max_workers=1, max_pending=1024),
    )
    return executor


def stop_team_sync_executor(app) -> None:
    """停止接收新 sync，并取消尚未开始的排队任务。"""
    executor = getattr(app.state, _SYNC_EXECUTOR_STATE, None)
    if executor is None:
        return
    delattr(app.state, _SYNC_EXECUTOR_STATE)
    executor.shutdown(wait=True, cancel_futures=True)
    telemetry_executor = getattr(app.state, _TELEMETRY_EXECUTOR_STATE, None)
    if telemetry_executor is not None:
        delattr(app.state, _TELEMETRY_EXECUTOR_STATE)
        telemetry_executor.shutdown()


async def _run_team_sync(app, func):
    """在 team 专用 executor 中执行同步 manifest 计算。"""
    executor = getattr(app.state, _SYNC_EXECUTOR_STATE, None)
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(executor, func)


def _submit_team_telemetry(app, func: Callable[[], None]) -> bool:
    executor = getattr(app.state, _TELEMETRY_EXECUTOR_STATE, None)
    if executor is None:
        return False
    return bool(executor.submit(func))


def _manifest_controls(user_key: str) -> tuple[dict, set]:
    """短时复用控制面只读快照，避免每个 sync 打开两次 registry DB。"""
    from xskill.config import get_registry_db_path
    from xskill.pipeline.registry import (
        effective_prefs_from_snapshot,
        manifest_control_plane_snapshot,
    )

    key = str(get_registry_db_path().expanduser().resolve())
    now = time.monotonic()
    cached = _MANIFEST_CONTROL_CACHE.get(key)
    if cached is None or cached[0] <= now:
        with _MANIFEST_CONTROL_CACHE_LOCK:
            cached = _MANIFEST_CONTROL_CACHE.get(key)
            if cached is None or cached[0] <= now:
                snapshot = manifest_control_plane_snapshot()
                cached = (now + _MANIFEST_CONTROL_CACHE_TTL, snapshot)
                _MANIFEST_CONTROL_CACHE[key] = cached
    snapshot = cached[1]
    return effective_prefs_from_snapshot(snapshot, user_key), snapshot["retired"]


def init_team_context(
    *,
    join_token: str,
    client_registry: ClientRegistry,
    skill_dir: Path,
    traj_root: Path,
    register_dir: Callable[[Path, str], None],
    configure_watch_dir: Callable[[Path, str, bool], None] | None = None,
    skillhub=None,
    profile_refresh_service=None,
) -> None:
    """create_app(team_server=True) 在 startup 时调用一次。"""
    # create_app/TestClient 可在同一进程内反复初始化。新上下文接管前
    # 先有界停止旧服务，避免留下持有旧 engine 的 daemon 线程。
    previous = _ctx.profile_refresh_service
    previous_registry = _ctx.client_registry
    if previous is not None and previous is not profile_refresh_service:
        try:
            previous.stop(timeout=5.0)
        except Exception:  # pylint: disable=broad-exception-caught
            logger.warning("failed to stop previous profile refresh service",
                           exc_info=True)
    if previous_registry is not None and previous_registry is not client_registry:
        try:
            previous_registry.close()
        except Exception:  # pylint: disable=broad-exception-caught
            logger.warning("failed to close previous client registry", exc_info=True)
    _ctx.join_token = join_token
    _ctx.client_registry = client_registry
    _ctx.skill_dir = Path(skill_dir)
    _ctx.traj_root = Path(traj_root)
    _ctx.register_dir = register_dir
    _ctx.configure_watch_dir = configure_watch_dir
    _ctx.skillhub = skillhub
    _ctx.profile_refresh_service = profile_refresh_service
    with _REPO_SEARCH_GRANTS_LOCK:
        _REPO_SEARCH_GRANTS.clear()


def clear_team_context(*, profile_refresh_shutdown_timeout: float = 5.0) -> bool:
    """有界停止画像服务并清空模块上下文。

    先调用 ``stop`` 让新的 ``/sync`` 刷新请求立即被拒绝，再清空
    registry/路径等引用。返回画像 worker 是否在时限内全部退出。
    """
    service = _ctx.profile_refresh_service
    registry = _ctx.client_registry
    stopped = True
    if service is not None:
        try:
            stopped = bool(service.stop(timeout=profile_refresh_shutdown_timeout))
        except Exception:  # pylint: disable=broad-exception-caught
            stopped = False
            logger.warning("failed to stop profile refresh service", exc_info=True)
    if registry is not None:
        try:
            stopped = bool(registry.close()) and stopped
        except Exception:  # pylint: disable=broad-exception-caught
            stopped = False
            logger.warning("failed to close client registry", exc_info=True)
    _ctx.join_token = ""
    _ctx.client_registry = None
    _ctx.skill_dir = None
    _ctx.traj_root = None
    _ctx.register_dir = None
    _ctx.configure_watch_dir = None
    _ctx.skillhub = None
    _ctx.profile_refresh_service = None
    with _REPO_SEARCH_GRANTS_LOCK:
        _REPO_SEARCH_GRANTS.clear()
    return stopped


def _auth(token: str | None, client_id: str | None,
          version: str | None = None) -> str:
    """校验 token + client_id，返回 client_id。失败抛 HTTPException。

    ``version``（P2-2.10）= 请求的 ``X-Xskill-Version`` header，非空时随
    touch 一并 upsert 进 clients.client_version。"""
    if _ctx.client_registry is None:
        raise HTTPException(status_code=503, detail="team context not initialized")
    if not token or token != _ctx.join_token:
        raise HTTPException(status_code=401, detail="invalid join token")
    if not client_id or not _ctx.client_registry.authenticate_and_touch(
        client_id, version,
    ):
        raise HTTPException(status_code=403, detail="unknown client_id")
    return client_id


def reconcile_client_ingest_watch_dir(
    client_id: str,
    *,
    ensure_directory: bool = False,
) -> dict:
    """把 clients 中的权威暂停状态同步到该用户 watch_dir。"""
    if _ctx.client_registry is None or _ctx.traj_root is None:
        raise RuntimeError("team context not initialized")
    from xskill.team.server.client_registry import safe_dir_name

    row = _ctx.client_registry.get(client_id)
    if row is None:
        raise ValueError(f"unknown client_id: {client_id}")
    dir_name = safe_dir_name(row.get("user_name") or None, client_id)
    sessions_dir = _ctx.traj_root / "clients" / dir_name / "sessions"
    if ensure_directory:
        sessions_dir.mkdir(parents=True, exist_ok=True)
    paused = _ctx.client_registry.is_ingest_paused(client_id)
    configured = sessions_dir.is_dir()
    if configured:
        if _ctx.configure_watch_dir is not None:
            _ctx.configure_watch_dir(sessions_dir, dir_name, not paused)
        elif paused:
            raise RuntimeError(
                "paused client requires configure_watch_dir callback"
            )
        elif not paused and _ctx.register_dir is not None:
            # 兼容只提供旧式 register_dir callback 的嵌入/测试调用方。
            _ctx.register_dir(sessions_dir, dir_name)
    return {
        "client_id": client_id,
        "dir_name": dir_name,
        "sessions_dir": sessions_dir,
        "ingest_paused": paused,
        "watch_dir_configured": configured,
    }


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
            # errors="strict"：base64 输出恒在 ASCII 字母表内，解不开=编码器坏了，
            # 属于必须炸出来的程序 bug，不是需要容错的外部 GBK 输入。
            digest = base64.urlsafe_b64encode(
                hashlib.sha256(data).digest(),
            ).rstrip(b"=").decode("ascii", errors="strict")
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
    client_id = _auth(x_xskill_token, x_xskill_client)
    wheel = _ensure_server_wheel()
    # 带 client_id 留痕——排查"某用户的更新请求到过没"时，access log
    # 只有 IP 没有身份，这行是唯一能按用户查的服务端痕迹
    logger.info("updater check: client=%s server_version=%s wheel=%s",
                client_id, XSKILL_VERSION, wheel.name if wheel else None)
    return {
        "package": "xskill",
        "version": XSKILL_VERSION,
        "wheel_available": wheel is not None,
        "wheel_filename": wheel.name if wheel else None,
    }


@router.post("/dashboard_link")
async def team_dashboard_link(
    x_xskill_token: str | None = Header(default=None),
    x_xskill_client: str | None = Header(default=None),
) -> dict:
    """给已命名 client 签发一次性看板登录链接（``xskill dashboard`` 用）。"""
    client_id = _auth(x_xskill_token, x_xskill_client)
    client_row = (
        _ctx.client_registry.get(client_id)
        if _ctx.client_registry is not None else None
    )
    user_name = str((client_row or {}).get("user_name") or "").strip()
    if not user_name:
        raise HTTPException(
            status_code=400,
            detail="匿名 client 无面板身份：先 `xskill connect <host:port> "
                   "--token <t> --name <你的名字>` 注册命名身份",
        )
    from xskill.dashboard.auth import issue_login_link_token
    link_token = issue_login_link_token(user_name)
    if link_token is None:
        raise HTTPException(status_code=503, detail="server 未启用 dashboard 登录")
    logger.info("dashboard link issued: client=%s user=%s", client_id, user_name)
    return {
        "user": user_name,
        "path": f"/api/v1/dashboard/login/link?t={link_token}",
    }


@router.get("/wheel")
async def team_wheel(
    x_xskill_token: str | None = Header(default=None),
    x_xskill_client: str | None = Header(default=None),
) -> FileResponse:
    """下载 server 当前版本对应的 xskill wheel。"""
    client_id = _auth(x_xskill_token, x_xskill_client)
    wheel = _ensure_server_wheel()
    if wheel is None:
        logger.warning("updater wheel miss: client=%s 请求 wheel 但 server 无货",
                       client_id)
        raise HTTPException(status_code=404, detail="xskill wheel not found")
    logger.info("updater wheel pull: client=%s -> %s", client_id, wheel.name)
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
    # 每请求现取：面板改 allow_anonymous_user 后下一次 connect 即生效，无需重启
    # serve（同 live_manifest_tuning 的理由——admin_config_reload 原地 mutate
    # app._config）。函数内 import：app 模块 import 本模块，模块级会循环。
    from xskill.api import app as app_mod
    from xskill.config import allow_anonymous_user
    if not user_name and not allow_anonymous_user(app_mod._config or {}):  # pylint: disable=protected-access
        raise HTTPException(
            status_code=403, detail="anonymous users not allowed"
        )
    client_id = _ctx.client_registry.register(
        label=req.client_label,
        hostname=req.hostname,
        claimed_client_id=req.claimed_client_id,
        user_name=user_name,
        client_version=req.client_version,
    )
    logger.info("team client registered: %s (label=%s, name=%s)",
                client_id, req.client_label, user_name or "<anonymous>")
    # P2-2.2(Q2a):命名用户发放 dashboard 登录 token(幂等,已有则原样返回)。
    # 匿名用户无 user_name 身份键,dashboard 登录不适用 → None。
    dashboard_token = (
        _ctx.client_registry.ensure_dashboard_token(client_id)
        if user_name else None
    )
    return RegisterResponse(client_id=client_id, dashboard_token=dashboard_token)


@router.post("/upload", response_model=UploadResponse)
async def team_upload(
    req: UploadRequest,
    x_xskill_token: str | None = Header(default=None),
    x_xskill_client: str | None = Header(default=None),
) -> UploadResponse:
    client_id = _auth(x_xskill_token, x_xskill_client)
    watch_state = reconcile_client_ingest_watch_dir(
        client_id, ensure_directory=True,
    )
    sessions_dir = watch_state["sessions_dir"]
    # 落盘文件名带成员标识前缀（issue #234）：不同成员在各自机器上生成的
    # traj_id 只含「项目名 + 会话 id 截断」，同名项目 + 短会话前缀会撞名；
    # 服务器把多人轨迹合并训练，必须由服务器保证全局唯一。注意只改**存储
    # 文件名**，响应里 accepted 仍回显客户端原始 traj_id——客户端以它对账
    # 本地状态，改了会导致重复上传。
    from xskill.team.server.client_registry import member_traj_tag
    _row = _ctx.client_registry.get(client_id) or {}
    member_tag = member_traj_tag(_row.get("user_name") or None, client_id)

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
        # 已带本成员前缀的 id（历史上服务器改名后客户端回传等场景）不再叠加，
        # 避免 traj_alice_xxx_alice_xxx_ 式的双重前缀。
        rest = t.traj_id[len("traj_"):]
        if rest.startswith(member_tag + "_"):
            stored_id = t.traj_id
        else:
            stored_id = f"traj_{member_tag}_{rest}"
        # sha256 完整性校验已过（上面），落盘前再做一遍内容清洗：客户端桥接常把
        # 终端 ANSI 码 / 控制字符灌进 .md，会让 splitlines 行号错位、污染模型输入。
        clean = sanitize_trajectory_text(t.content)
        md_path = sessions_dir / f"{stored_id}.md"
        # 内容未变的重复上传直接确认、不重写：重写会刷新 mtime，让 watcher
        # 把旧轨迹当新文件重新入库；客户端断线重传是常态，不能因此翻倍语料。
        # 同理，成员前缀上线前落盘的旧名文件若内容一致也视为已存储（新旧
        # 命名共存，改名交给 ``xskill tools migrate-traj-name`` 手动迁移）。
        existing = None
        for cand in (md_path, sessions_dir / f"{t.traj_id}.md"):
            try:
                if cand.is_file() and cand.read_text(encoding="utf-8") == clean:
                    existing = cand
                    break
            except OSError:
                continue
        if existing is not None:
            accepted.append(t.traj_id)
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
            (sessions_dir / f"{stored_id}.json").write_text(
                json.dumps(sidecar, ensure_ascii=False), encoding="utf-8")
        md_path.write_text(clean, encoding="utf-8")
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
    watch_state = reconcile_client_ingest_watch_dir(
        client_id, ensure_directory=True,
    )
    sessions_dir = watch_state["sessions_dir"]
    dir_name = watch_state["dir_name"]

    # 落盘：uploads/<eco>/<client_id>/<安全文件名>
    safe_name = Path(file.filename or "upload.db").name
    dest_dir = get_uploads_dir() / eco / client_id
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / safe_name
    dest.write_bytes(await file.read())

    # 桥接到该 client 的 sessions 桶，label=dir_name（与 team_upload 一致）
    try:
        # SQLite 解析 + 批量落盘是阻塞调用，卸到线程池，别占事件循环
        summary = await run_in_threadpool(
            read_db_files,
            dest, eco=eco, target_dir=sessions_dir, register=False,
            register_label=dir_name,
        )
    except (FileNotFoundError, ValueError) as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    logger.info("team ingest-db from %s: %s → bridged %d traj",
                client_id, safe_name, summary["bridged"])
    return {"client_id": client_id, "saved": str(dest),
            "bridged": summary["bridged"]}


def team_sync(
    x_xskill_token: str | None = Header(default=None),
    x_xskill_client: str | None = Header(default=None),
    x_xskill_version: str | None = Header(default=None),
    telemetry_submit: Callable[[Callable[[], None]], bool] | None = None,
):
    """只读已落库画像构建 manifest，再提交后台刷新。

    路由保持 ``def``，因为 manifest 路径仍包含同步 SQLite/Git 读取；
    慢 embedding 只在独立的 ProfileRefreshService worker 中执行。
    """
    client_id = _auth(x_xskill_token, x_xskill_client, version=x_xskill_version)
    # 每请求现取：面板改推荐个数/灰度概率后下一轮 sync 即生效，无需重启 serve。
    total_slots, ranked_slots, probability = live_manifest_tuning()
    if total_slots <= 0:
        # 明确禁用分发时无需读取 client 行、偏好和 retired 集合。300 并发
        # 冷启动会放大这些无效 SQLite 打开；画像刷新仍按下方路径提交。
        resp = build_manifest(
            client_id=client_id,
            skill_dir=_ctx.skill_dir,
            probability=probability,
            ranked_slots=ranked_slots,
            total_slots=0,
            traj_root=_ctx.traj_root,
            telemetry_submit=telemetry_submit,
        )
        resp.server_slots = 0
        resp.take_n = 0
    else:
        # P2-2.4 控制面注入:blocked 排除→pinned 占位→ranked→recommended。
        # best-effort 读取(D8:超量在写入侧拒绝,这里读挂了退回无 prefs 分发,
        # 后台链路绝不因控制面阻塞)。user_key=user_name(D5),匿名 client 只吃全局。
        prefs = None
        retired = None
        user_key = ""
        try:
            user_key = _ctx.client_registry.user_name_for(client_id) or ""
            prefs, retired = _manifest_controls(user_key)
        except Exception:  # pylint: disable=broad-exception-caught
            logger.warning("skill prefs lookup failed, serving without control-plane",
                           exc_info=True)
        resp = build_manifest(
            client_id=client_id,
            skill_dir=_ctx.skill_dir,
            probability=probability,
            ranked_slots=ranked_slots,
            total_slots=total_slots,
            traj_root=_ctx.traj_root,
            prefs=prefs,
            retired=retired,
            telemetry_submit=telemetry_submit,
            user_key=user_key,
        )
        resp.server_slots = total_slots
        # client 截取安装数：默认=服务器 skill_slots；看板可改 user_client_settings
        try:
            from xskill.pipeline.registry import get_client_take_n
            resp.take_n = get_client_take_n(
                user_key, default=total_slots,
            ) if user_key else total_slots
        except Exception:  # pylint: disable=broad-exception-caught
            resp.take_n = total_slots
            logger.debug("take_n lookup failed", exc_info=True)
    # 本次响应必须使用 request() 之前的已落库画像。request 只操作
    # 有界内存队列；服务缺失、正在停止、队列满或自身异常都不改变
    # /sync 的成功响应。
    service = _ctx.profile_refresh_service
    if service is not None:
        try:
            service.request(client_id)
        except Exception:  # pylint: disable=broad-exception-caught
            logger.warning("profile refresh request failed for %s", client_id,
                           exc_info=True)
    return resp.model_dump()


@router.get("/sync")
async def team_sync_endpoint(
    request: Request,
    x_xskill_token: str | None = Header(default=None),
    x_xskill_client: str | None = Header(default=None),
    x_xskill_version: str | None = Header(default=None),
):
    """所有 manifest 计算都在 team 专用线程池执行。"""
    return await _run_team_sync(
        request.app,
        partial(
            team_sync,
            x_xskill_token=x_xskill_token,
            x_xskill_client=x_xskill_client,
            x_xskill_version=x_xskill_version,
            telemetry_submit=partial(_submit_team_telemetry, request.app),
        ),
    )


@router.get("/skill/{name}/bundle")
async def team_skill_bundle(
    name: str,
    x_xskill_token: str | None = Header(default=None),
    x_xskill_client: str | None = Header(default=None),
) -> Response:
    client_id = _auth(x_xskill_token, x_xskill_client)
    repo_dir = _ctx.skill_dir / name
    exact_repo = (repo_dir / ".git").is_dir()
    if _is_repo_search_id(name):
        download = await run_in_threadpool(
            _repo_search_archive, name, client_id, exact_repo,
        )
        if download is not None:
            archive, content_sha, side = download
            return Response(
                content=archive,
                media_type="application/zip",
                headers={
                    "X-XSkill-Content-Sha": content_sha,
                    "X-XSkill-Side": side,
                },
            )

    if exact_repo:
        bundle = make_repo_bundle(repo_dir)
        return Response(content=bundle, media_type="application/octet-stream")

    hub = _ctx.skillhub
    hub_entry = hub.entry(name) if hub is not None else None
    if hub_entry is None:
        raise HTTPException(status_code=404, detail=f"skill not found: {name}")
    hub_dir = Path(hub_entry["path"])
    if not hub_dir.is_dir() or not (hub_dir / "SKILL.md").is_file():
        raise HTTPException(status_code=404, detail=f"skill not found: {name}")
    archive = _make_skillhub_archive(hub_dir)
    return Response(
        content=archive,
        media_type="application/zip",
        headers={"X-XSkill-Content-Sha": hub_entry["content_sha"]},
    )


def _repo_search_archive(
    search_id: str, client_id: str, exact_repo: bool,
) -> tuple[bytes, str, str] | None:
    """在线程内强制刷新 refs、校验 search grant，并导出固定 commit。"""
    grant = _repo_search_grant(client_id, search_id)
    if grant is None:
        catalog = manifest_catalog_snapshot(_ctx.skill_dir, max_age_seconds=0)
        skill = catalog.search_by_id.get(search_id)
        if skill is None:
            if exact_repo:
                return None
            raise HTTPException(status_code=404, detail=f"skill not found: {search_id}")
        if exact_repo:
            raise HTTPException(status_code=409, detail="ambiguous repo search id")
        _total_slots, _ranked_slots, probability = live_manifest_tuning()
        slot = _resolve_slot(
            skill, client_id, probability, "ranked", refs=catalog.refs,
        )
        if slot is None:
            raise HTTPException(status_code=404, detail=f"skill not found: {search_id}")
        repo_name = skill.name
        content_sha, side = _pin_repo_search_grant(
            client_id, search_id, repo_name, slot.sha, slot.side,
            catalog.refs[repo_name],
        )
    else:
        repo_name, content_sha, side = grant
    if exact_repo:
        raise HTTPException(status_code=409, detail="ambiguous repo search id")
    if repo_search_id(repo_name) != search_id:
        raise HTTPException(status_code=409, detail="invalid search grant")
    repo_dir = _ctx.skill_dir / repo_name
    if content_sha not in (main_sha(repo_dir), staging_sha(repo_dir)):
        raise HTTPException(
            status_code=409, detail="skill version changed; search again",
        )
    try:
        archive = make_repo_archive(repo_dir, content_sha)
    except (KeyError, NotGitRepository, ObjectMissing, OSError, ValueError) as error:
        logger.warning(
            "repo search archive changed during download: %s (%s)",
            repo_name, type(error).__name__,
        )
        raise HTTPException(
            status_code=409, detail="skill version changed; search again",
        ) from error
    return archive, content_sha, side


@router.get("/skill_hub/entry/{skill_id}")
async def team_skill_hub_entry(
    skill_id: str,
    x_xskill_token: str | None = Header(default=None),
    x_xskill_client: str | None = Header(default=None),
) -> dict:
    """按 search 返回的稳定 ID 读取当前下载元信息，不传输 skill 内容。"""
    client_id = _auth(x_xskill_token, x_xskill_client)
    return await run_in_threadpool(
        _team_skill_entry, skill_id, client_id,
    )


def _team_skill_entry(skill_id: str, client_id: str) -> dict:
    _total_slots, _ranked_slots, probability = live_manifest_tuning()
    if _is_repo_search_id(skill_id):
        catalog = manifest_catalog_snapshot(
            _ctx.skill_dir, max_age_seconds=0,
        )
        skill = catalog.search_by_id.get(skill_id)
        if skill is None:
            raise HTTPException(
                status_code=404, detail=f"skill not found: {skill_id}",
            )
        hit = {
            "source": "repo",
            "skill_id": skill_id,
            "repo_name": skill.name,
            "display_name": str(
                skill.frontmatter.get("name") or skill.name
            ),
            "description": skill.description,
            "source_path": skill.name,
            "content_sha": catalog.refs[skill.name][0],
            "ux_avg": None,
            "bm25_rank": None,
            "semantic_rank": None,
        }
        results = _format_team_search_results(
            [hit], catalog, client_id, probability,
        )["results"]
    else:
        hub = _ctx.skillhub
        entry = hub.entry(skill_id) if hub is not None else None
        if entry is None:
            raise HTTPException(
                status_code=404, detail=f"skill not found: {skill_id}",
            )
        hit = dict(entry)
        hit["bm25_rank"] = None
        hit["semantic_rank"] = None
        hit["ux_avg"] = hub.ux_avg(skill_id)
        results = _format_team_search_results(
            [hit], None, client_id, probability,
        )["results"]
    if not results:
        raise HTTPException(
            status_code=404, detail=f"skill not found: {skill_id}",
        )
    return {"result": results[0]}


@router.get("/skill_hub/search")
async def team_skill_hub_search(
    query: str,
    limit: int = 5,
    x_xskill_token: str | None = Header(default=None),
    x_xskill_client: str | None = Header(default=None),
) -> dict:
    """BM25 + 语义 RRF 统一检索自产 main skill + SkillHub（无画像）。"""
    client_id = _auth(x_xskill_token, x_xskill_client)
    hub = _ctx.skillhub
    if not query.strip():
        raise HTTPException(status_code=400, detail="empty query")
    bounded_limit = max(1, min(int(limit), 10))
    try:
        _total_slots, _ranked_slots, probability = live_manifest_tuning()
        engine = get_recommend_engine()
        if engine is None and (hub is None or not getattr(hub, "enabled", False)):
            raise HTTPException(
                status_code=503, detail="skillhub not enabled on server",
            )
        if engine is not None:
            return await run_in_threadpool(
                _search_and_format_team_skills,
                engine, query, bounded_limit, client_id, probability,
            )

        packed = hub.cached_search_with_meta(query, bounded_limit)
        hot_cache_hit = packed is not None
        if hot_cache_hit:
            await asyncio.sleep(0)
            matches, search_meta = packed
        else:
            matches, search_meta = await run_in_threadpool(
                hub.search_with_meta, query, bounded_limit,
            )
        payload = _format_team_search_results(
            matches, None, client_id, probability, meta=search_meta,
        )
        if hot_cache_hit:
            await asyncio.sleep(0)
        return payload
    except HTTPException:
        raise
    except FileNotFoundError:
        request_id = f"search-{secrets.token_hex(8)}"
        server_logger.exception(
            "SkillHub search source unavailable request_id=%s client_id=%s "
            "limit=%d query_length=%d",
            request_id, client_id, bounded_limit, len(query),
        )
        return JSONResponse(
            status_code=503,
            content={
                "code": "SKILL_HUB_SOURCE_UNAVAILABLE",
                "message": "SkillHub 数据源暂时不可用",
                "request_id": request_id,
                "retryable": True,
            },
            headers={"X-Request-ID": request_id},
        )
    except Exception:  # pylint: disable=broad-exception-caught
        request_id = f"search-{secrets.token_hex(8)}"
        server_logger.exception(
            "SkillHub search failed request_id=%s client_id=%s limit=%d "
            "query_length=%d",
            request_id, client_id, bounded_limit, len(query),
        )
        return JSONResponse(
            status_code=500,
            content={
                "code": "SKILL_HUB_SEARCH_FAILED",
                "message": "服务器执行 SkillHub 搜索时发生异常",
                "request_id": request_id,
                "retryable": False,
            },
            headers={"X-Request-ID": request_id},
        )


def _search_and_format_team_skills(
    engine,
    query: str,
    limit: int,
    client_id: str,
    probability: float,
) -> dict:
    """冷路径：刷新 catalog、统一 hybrid search，并在线程内补齐灰度元数据。"""
    catalog = manifest_catalog_snapshot(_ctx.skill_dir)
    if (
        not catalog.search_by_id
        and not getattr(engine.skillhub, "enabled", False)
    ):
        raise HTTPException(status_code=503, detail="no searchable skill source")
    matches, search_meta = engine.search_team_skills_with_meta(
        query, limit, catalog,
    )
    return _format_team_search_results(
        matches, catalog, client_id, probability, meta=search_meta,
    )


def _format_team_search_results(
    matches: list[dict], catalog, client_id: str, probability: float,
    *,
    meta: dict | None = None,
) -> dict:
    """把统一排名命中投影为既有 search/install 响应契约。"""
    results: list[dict] = []
    for hit in matches:
        source = hit.get("source")
        result = {
            "skill_id": hit["skill_id"],
            "display_name": hit["display_name"],
            "description": hit["description"],
            "content_sha": hit["content_sha"],
            "source_path": hit["source_path"],
            "source": (
                "repo" if source == "repo"
                else _skillhub_result_source(hit["source_path"])
            ),
            "ux_avg": hit.get("ux_avg"),
            "match": {
                "bm25_rank": hit.get("bm25_rank"),
                "semantic_rank": hit.get("semantic_rank"),
            },
        }
        if source == "repo":
            search_id = repo_search_id(hit["repo_name"])
            skill = catalog.search_by_id.get(search_id)
            if skill is None:
                continue
            slot = _resolve_slot(
                skill, client_id, probability, "ranked", refs=catalog.refs,
            )
            if slot is None:
                continue
            grant = _pin_repo_search_grant(
                client_id, search_id, skill.name, slot.sha, slot.side,
                catalog.refs[skill.name],
            )
            content_sha, side = grant
            result.update({
                "skill_id": search_id,
                "content_sha": content_sha,
                "side": side,
                "staging_available": catalog.refs[skill.name][1] is not None,
                "staging_assigned": side == "staging",
            })
        results.append(result)
    payload = {"results": results}
    if meta is not None:
        payload["meta"] = {
            "corpus_empty": bool(meta.get("corpus_empty")),
            "degraded_to_bm25": bool(meta.get("degraded_to_bm25")),
        }
    return payload


def _pin_repo_search_grant(
    client_id: str,
    search_id: str,
    repo_name: str,
    content_sha: str,
    side: str,
    current_refs: tuple[str, str | None],
) -> tuple[str, str]:
    """同一 client/skill 的有效租约不被并发搜索覆盖。"""
    key = (client_id, search_id)
    now = time.monotonic()
    with _REPO_SEARCH_GRANTS_LOCK:
        existing = _REPO_SEARCH_GRANTS.get(key)
        if existing is not None and now < existing[0]:
            if existing[2] in current_refs:
                return existing[2], existing[3]
        _REPO_SEARCH_GRANTS.pop(key, None)
        _REPO_SEARCH_GRANTS[key] = (
            now + _REPO_SEARCH_GRANT_TTL_SECONDS,
            repo_name,
            content_sha,
            side,
        )
        while len(_REPO_SEARCH_GRANTS) > _REPO_SEARCH_GRANT_CAPACITY:
            _REPO_SEARCH_GRANTS.pop(next(iter(_REPO_SEARCH_GRANTS)))
        return content_sha, side


def _repo_search_grant(
    client_id: str, search_id: str,
) -> tuple[str, str, str] | None:
    key = (client_id, search_id)
    with _REPO_SEARCH_GRANTS_LOCK:
        grant = _REPO_SEARCH_GRANTS.get(key)
        if grant is None:
            return None
        expires_at, repo_name, content_sha, side = grant
        if time.monotonic() >= expires_at:
            del _REPO_SEARCH_GRANTS[key]
            return None
        return repo_name, content_sha, side


def _is_repo_search_id(name: str) -> bool:
    return (
        name.startswith("repo@")
        and len(name) == 69
        and all(char in "0123456789abcdef" for char in name[5:])
    )


def _skillhub_result_source(source_path: str) -> str:
    """``user_skill_hub/<owner>/...`` 上传件标为 ``上传者:<owner>``，其余为 ``skillhub``。"""
    parts = source_path.split("/")
    if len(parts) >= 2 and parts[0] == "user_skill_hub":
        return f"上传者:{parts[1]}"
    return "skillhub"


@router.post("/skills/import")
async def team_skills_import(
    file: UploadFile = File(...),
    name: str = Form(...),
    x_xskill_token: str | None = Header(default=None),
    x_xskill_client: str | None = Header(default=None),
) -> dict:
    """把技能目录纳入 server 自有仓。不要和 skill_hub/upload 混用。"""
    client_id = _auth(x_xskill_token, x_xskill_client)
    if _ctx.skill_dir is None:
        raise HTTPException(status_code=503, detail="skill_dir not configured")
    from xskill.recommend.skillhub import safe_id_part
    skill_name = safe_id_part(name)
    if not skill_name:
        raise HTTPException(status_code=400, detail="invalid skill name")
    payload = await file.read()
    if len(payload) > 50 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="import archive exceeds 50MB")
    result = await run_in_threadpool(_import_skill_archive, payload, skill_name)
    result["pinned"] = _pin_imported_skill(client_id, result["name"])
    logger.info("native import from %s: %s sha=%s existed=%s pinned=%s",
                client_id, result["name"], result.get("sha", "")[:8],
                result.get("existed"), result["pinned"])
    return result


_IMPORT_ARCHIVE_MAX_FILES = 20000


def _pin_imported_skill(client_id: str, skill_name: str) -> list[str]:
    """纳入后钉到发起人，和 generate 一样只钉 user_name，不钉全员。"""
    row = (_ctx.client_registry.get(client_id) or {}) if _ctx.client_registry else {}
    user_id = (row.get("user_name") or "").strip()
    if not user_id:
        return []
    from xskill.api import app as app_mod
    from xskill.config import get_registry_db_path, team_server_slots_config
    from xskill.team.server.generate_jobs import pin_generated_skills

    max_pinned = None
    try:
        max_pinned = team_server_slots_config(app_mod._config or {})["skill_slots"]
    except Exception:  # pylint: disable=broad-exception-caught
        logger.debug("skill_slots unavailable for import pin quota", exc_info=True)
    pinned = pin_generated_skills(
        user_id=user_id,
        skill_names=[skill_name],
        db_path=get_registry_db_path(),
        max_pinned=max_pinned,
        origin_source="import",
    )
    try:
        from xskill.dashboard.console import _bump_routing_epoch
        _bump_routing_epoch()
    except Exception:  # pylint: disable=broad-exception-caught
        logger.debug("routing epoch bump after import pin skipped", exc_info=True)
    return pinned


def _import_skill_archive(payload: bytes, skill_name: str) -> dict:
    from xskill.skill.importer import extract_import_zip, import_one_skill
    from xskill.team.server.skill_manifest import invalidate_manifest_cache

    tmp_dir = Path(tempfile.mkdtemp(prefix="xskill-import."))
    try:
        source = tmp_dir / skill_name
        extract_import_zip(payload, source, max_files=_IMPORT_ARCHIVE_MAX_FILES)
        imported = import_one_skill(_ctx.skill_dir, source)
        invalidate_manifest_cache(_ctx.skill_dir)
        return {
            "name": imported.name,
            "sha": imported.sha,
            "existed": imported.existed,
            "baby_overwritten": imported.baby_overwritten,
            "staging_kept": imported.staging_kept,
            "main_round_scores_cleared": imported.main_round_scores_cleared,
        }
    except HTTPException:
        raise
    except FileNotFoundError as missing:
        raise HTTPException(status_code=400, detail=str(missing)) from missing
    except ValueError as bad:
        raise HTTPException(status_code=400, detail=str(bad)) from bad
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


@router.post("/skill_hub/upload")
async def team_skill_hub_upload(
    file: UploadFile = File(...),
    x_xskill_token: str | None = Header(default=None),
    x_xskill_client: str | None = Header(default=None),
) -> dict:
    """收 client 打包的 skill 文件夹 zip，落到 user_skill_hub/<用户目录>/ 下。

    落盘位置在 skillhub 目录树内，所以上传件天然进入 skillhub 扫描范围：
    可被 `/skill_hub/search` 搜到、可经 `/skill/{id}/bundle` 分发。
    """
    client_id = _auth(x_xskill_token, x_xskill_client)
    hub = _ctx.skillhub
    if hub is None or not getattr(hub, "enabled", False):
        raise HTTPException(status_code=503, detail="skillhub not enabled on server")
    payload = await file.read()
    if len(payload) > 20 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="skill archive exceeds 20MB")
    from xskill.team.server.client_registry import safe_dir_name
    registry_row = _ctx.client_registry.get(client_id)
    owner_dir = safe_dir_name((registry_row or {}).get("user_name") or None, client_id)
    stored = await run_in_threadpool(_store_user_skill, hub, owner_dir, payload)
    logger.info("skill_hub upload from %s: %s -> %s",
                client_id, stored["display_name"], stored["stored_path"])
    return stored


def _store_user_skill(hub, owner_dir: str, payload: bytes) -> dict:
    """校验并解压上传的 skill zip 到 <skillhub>/user_skill_hub/<owner>/<name>/。"""
    from xskill.skill.frontmatter import FrontmatterError, parse_strict

    try:
        archive = zipfile.ZipFile(io.BytesIO(payload))
    except zipfile.BadZipFile as bad_zip:
        raise HTTPException(status_code=400, detail=f"invalid zip: {bad_zip}") from bad_zip
    with archive:
        members = archive.infolist()
        files = [member for member in members if not member.is_dir()]
        root_skill_files = [member for member in files if member.filename == "SKILL.md"]
        if not root_skill_files:
            raise HTTPException(status_code=400,
                                detail="SKILL.md missing at archive root")
        if len(root_skill_files) != 1:
            raise HTTPException(status_code=400,
                                detail="archive contains duplicate SKILL.md entries")
        if len(files) > _SKILL_ARCHIVE_MAX_FILES:
            raise HTTPException(
                status_code=413,
                detail=f"skill archive contains more than {_SKILL_ARCHIVE_MAX_FILES} files",
            )
        if any(member.file_size > _SKILL_ARCHIVE_MAX_FILE_BYTES for member in files):
            raise HTTPException(status_code=413, detail="skill archive contains an oversized file")
        if sum(member.file_size for member in files) > _SKILL_ARCHIVE_MAX_TOTAL_BYTES:
            raise HTTPException(status_code=413,
                                detail="skill archive expands beyond 100MB")
        try:
            # errors="strict" 是**故意**的：非 utf-8 的 SKILL.md 由下面的 except
            # 转成 400 退回上传方(fail-loud 校验)，而不是 errors="replace" 把乱码
            # 静默收进 hub 分发给所有人。这里的 GBK 来源是可拒绝的上传请求，
            # 不是 collector 那种"拒了就停摆"的本地轮询。
            frontmatter, _body = parse_strict(
                archive.read("SKILL.md").decode("utf-8", errors="strict"))
        except (FrontmatterError, UnicodeDecodeError) as bad_skill:
            raise HTTPException(status_code=400,
                                detail=f"invalid SKILL.md: {bad_skill}") from bad_skill
        display_name = str(frontmatter["name"]).strip()
        from xskill.recommend.skillhub import safe_id_part
        dest_dir = (Path(hub.dir) / "user_skill_hub" / owner_dir
                    / safe_id_part(display_name))
        dest_dir.parent.mkdir(parents=True, exist_ok=True)
        tmp_dir = Path(tempfile.mkdtemp(
            prefix=f".{dest_dir.name}.tmp.", dir=dest_dir.parent,
        ))
        extracted_root = tmp_dir.resolve()
        extracted_bytes = 0
        try:
            for info in members:
                target = (tmp_dir / info.filename).resolve()
                try:
                    target.relative_to(extracted_root)
                except ValueError:
                    raise HTTPException(
                        status_code=400, detail=f"unsafe archive path: {info.filename}",
                    )
                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                file_bytes = 0
                with archive.open(info) as src, open(target, "wb") as dst:
                    while True:
                        chunk = src.read(1024 * 1024)
                        if not chunk:
                            break
                        file_bytes += len(chunk)
                        extracted_bytes += len(chunk)
                        if (file_bytes > _SKILL_ARCHIVE_MAX_FILE_BYTES
                                or extracted_bytes > _SKILL_ARCHIVE_MAX_TOTAL_BYTES):
                            raise HTTPException(
                                status_code=413, detail="skill archive expands beyond limit",
                            )
                        dst.write(chunk)
        except HTTPException:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            raise
        except (OSError, RuntimeError, zipfile.BadZipFile, EOFError) as extract_error:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            raise HTTPException(
                status_code=400, detail=f"invalid zip content: {extract_error}",
            ) from extract_error
    shutil.rmtree(dest_dir, ignore_errors=True)
    tmp_dir.replace(dest_dir)
    source_path = dest_dir.relative_to(Path(hub.dir)).as_posix()
    entry = hub.entry(source_path, force_refresh=True)
    if entry is None:
        raise HTTPException(status_code=500,
                            detail="stored skill not visible in skillhub scan")
    _backfill_skill_embed(hub, entry["description"])
    return {
        "skill_id": entry["skill_id"],
        "display_name": entry["display_name"],
        "description": entry["description"],
        "content_sha": entry["content_sha"],
        "source_path": entry["source_path"],
        "stored_path": str(dest_dir),
    }


def _backfill_skill_embed(hub, description: str) -> None:
    """上传成功后有界、异步补这一条 corpus 向量；忙时保留 BM25 可用性。"""
    if not hub.backfill_description_embedding(description):
        logger.debug("skill_hub upload embed backfill skipped or semantic search disabled")


def _make_skillhub_archive(skill_dir: Path) -> bytes:
    """Pack a deterministic, content-addressable skill archive for thin clients."""
    skill_dir = Path(skill_dir)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for p in sorted(skill_dir.rglob("*")):
            relative = p.relative_to(skill_dir)
            if (p.is_symlink() or not p.is_file()
                    or any(part in (".git", "__pycache__") for part in relative.parts)
                    or p.name == ".ux_scores.jsonl"
                    or p.name.startswith(".xskill_")):
                continue
            info = zipfile.ZipInfo(relative.as_posix(), date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = (p.stat().st_mode & 0o777) << 16
            zf.writestr(info, p.read_bytes())
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
    # git 子进程是阻塞调用，卸到线程池，别占事件循环
    sha = await run_in_threadpool(
        fetch_branch_from_bundle, bundle, repo_dir, "_useredit", dest_ref)
    logger.info("team push-edit: %s -> %s (%s)", x_xskill_skill, dest_ref, sha[:8])
    # P3-3.1 埋点:手改分支即修改意见——通知该 skill 贡献者(旁路,失败不阻断)
    try:
        from xskill.events import EventStore
        row = _ctx.client_registry.get(client_id) or {}
        EventStore().emit_push_edit(
            actor=row.get("user_name") or client_id,
            skill=x_xskill_skill,
            branch=f"user-staging/{client_id}", ref_sha=sha)
    except Exception:  # pylint: disable=broad-exception-caught
        logger.debug("push-edit event emit skipped", exc_info=True)
    return PushEditResponse(branch=f"user-staging/{client_id}", ref_sha=sha)


@router.post("/generate", response_model=GenerateAccepted)
def team_generate(
    req: GenerateRequest,
    x_xskill_token: str | None = Header(default=None),
    x_xskill_client: str | None = Header(default=None),
    x_xskill_version: str | None = Header(default=None),
) -> GenerateAccepted:
    client_id = _auth(x_xskill_token, x_xskill_client, x_xskill_version)
    instruction = (req.instruction or "").strip()
    if not instruction:
        raise HTTPException(status_code=400, detail="instruction 不能为空")
    row = (_ctx.client_registry.get(client_id) or {}) if _ctx.client_registry else {}
    user_id = (row.get("user_name") or "").strip() or client_id
    names = [n.strip() for n in (req.names or []) if str(n).strip()]
    from xskill.config import get_logs_dir
    from xskill.team.server.generate_jobs import (
        create_job, enqueue_generate_job,
    )

    logs_dir = get_logs_dir()
    job = create_job(
        client_id=client_id,
        user_id=user_id,
        instruction=instruction,
        preferred_names=names,
        logs_dir=logs_dir,
    )
    enqueue_generate_job(job, logs_dir=logs_dir)
    return GenerateAccepted(job_id=job["job_id"])


@router.get("/generate/{job_id}/events")
def team_generate_events(
    job_id: str,
    x_xskill_token: str | None = Header(default=None),
    x_xskill_client: str | None = Header(default=None),
    x_xskill_version: str | None = Header(default=None),
):
    client_id = _auth(x_xskill_token, x_xskill_client, x_xskill_version)
    from xskill.team.server.generate_jobs import get_job, iter_job_events

    job = get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="unknown generate job")
    if job.get("client_id") != client_id:
        raise HTTPException(status_code=403, detail="job belongs to another client")

    def event_stream():
        for event in iter_job_events(job_id):
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )
