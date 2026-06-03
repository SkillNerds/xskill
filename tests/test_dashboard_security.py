"""test_dashboard_security.py —— 看板访问控制中间件"""
from __future__ import annotations

import base64

from fastapi import FastAPI
from fastapi.responses import PlainTextResponse
from fastapi.testclient import TestClient

from xskill.dashboard.security import DashboardAccessMiddleware


def _app(public, password):
    app = FastAPI()
    app.add_middleware(DashboardAccessMiddleware, public=public, password=password,
                       guarded_prefixes=("/", "/api/v1/dashboard"))

    @app.get("/")
    def root():
        return PlainTextResponse("panel")

    @app.get("/api/v1/team/x")
    def team():
        return PlainTextResponse("team")

    return app


def test_loopback_allowed_when_not_public():
    # TestClient 客户端 host = "testclient"(非 loopback)
    c = TestClient(_app(False, ""))
    assert c.get("/").status_code == 403                  # 默认仅本机 → 非 loopback 被挡
    assert c.get("/api/v1/team/x").status_code == 200      # 非看板路由不受限


def test_public_allows_any_source():
    c = TestClient(_app(True, ""))
    assert c.get("/").status_code == 200


def test_password_requires_basic_auth():
    c = TestClient(_app(True, "s3cret"))
    assert c.get("/").status_code == 401
    tok = base64.b64encode(b"admin:s3cret").decode()
    assert c.get("/", headers={"Authorization": f"Basic {tok}"}).status_code == 200
    bad = base64.b64encode(b"admin:wrong").decode()
    assert c.get("/", headers={"Authorization": f"Basic {bad}"}).status_code == 401
