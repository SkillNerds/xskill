"""把看板挂到一个 FastAPI app:include_router + 访问中间件。仅在 enabled 时动。"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from xskill.config import XSKILL_HOME, dashboard_config
from xskill.dashboard.router import build_dashboard_router
from xskill.dashboard.security import DashboardAccessMiddleware


def _team_registry_provider():
    """登录时解引用 team ctx 的 ClientRegistry（app startup 后才存在）。

    非 team 模式 / ctx 未初始化 → None（普通用户登录不可用，仅 admin 口令）。
    """
    from xskill.team.server.api import team_context
    return getattr(team_context(), "client_registry", None)


def mount_dashboard(app, cfg: dict, *, db_path: Optional[Path] = None,
                    serve_builtin: bool = True) -> None:
    """``serve_builtin=False`` = 独立只读实例（D4）：只挂聚合 GET 端点；
    登录、写操作及内容级敏感路由均物理不挂载。"""
    dc = dashboard_config(cfg)
    if not dc["enabled"]:
        return
    app.include_router(build_dashboard_router(
        db_path=db_path,
        default_harness=dc["default_harness"],
        default_model=dc["default_model"],
        expose_sensitive=serve_builtin))
    if serve_builtin:
        # P2-2.2 登录与角色:仅 serve 内置形态挂载(D4)。
        from xskill.dashboard.auth import (
            build_auth_router, configure_auth, ensure_dashboard_secret,
        )
        configure_auth(
            secret=ensure_dashboard_secret(XSKILL_HOME / "dashboard_secret.json"),
            admins=dc["admins"],
            admin_password=dc["admin_password"],
            registry_provider=_team_registry_provider,
        )
        app.include_router(build_auth_router())
        # P2 控制面(我的/管理):同样只在 serve 内置形态
        from xskill.dashboard.console import build_console_router
        app.include_router(build_console_router(db_path=db_path))
    app.add_middleware(DashboardAccessMiddleware, public=dc["public"],
                       password=dc["password"])
