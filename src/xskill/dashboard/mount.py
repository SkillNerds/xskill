"""把看板挂到一个 FastAPI app:include_router + 访问中间件。仅在 enabled 时动。"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from xskill.config import dashboard_config
from xskill.dashboard.router import build_dashboard_router
from xskill.dashboard.security import DashboardAccessMiddleware


def mount_dashboard(app, cfg: dict, *, db_path: Optional[Path] = None) -> None:
    dc = dashboard_config(cfg)
    if not dc["enabled"]:
        return
    app.include_router(build_dashboard_router(
        db_path=db_path,
        default_harness=dc["default_harness"],
        default_model=dc["default_model"]))
    app.add_middleware(DashboardAccessMiddleware, public=dc["public"],
                       password=dc["password"])
