"""test_dashboard_mount.py —— create_app 按 config 挂载看板"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from xskill.pipeline.registry import get_connection
from xskill.dashboard.mount import mount_dashboard


def test_mounts_when_enabled(tmp_path):
    db = tmp_path / "r.db"
    get_connection(db).close()
    app = FastAPI()
    mount_dashboard(app, {"dashboard": {"enabled": True, "public": True}}, db_path=db)
    assert TestClient(app).get("/api/v1/dashboard/overview").status_code == 200


def test_not_mounted_when_disabled(tmp_path):
    app = FastAPI()
    mount_dashboard(app, {"dashboard": {"enabled": False}}, db_path=tmp_path / "r.db")
    assert TestClient(app).get("/api/v1/dashboard/overview").status_code == 404
