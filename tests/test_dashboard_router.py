"""test_dashboard_router.py —— 看板聚合端点 + 静态壳"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from xskill.pipeline.registry import get_connection
from xskill.dashboard.router import build_dashboard_router


def _client(tmp_path):
    db = tmp_path / "r.db"
    conn = get_connection(db)
    conn.execute("INSERT INTO watch_dirs(path,label,ecosystem) VALUES('/cc','cc','claude_code')")
    conn.execute("INSERT INTO trajectories(watch_dir_id,filename,status,tasks_extracted,"
                 "skill_generated,ux_score,source_model) VALUES(1,'f','done',6,'s',8.0,'m')")
    conn.commit()
    conn.close()
    app = FastAPI()
    app.include_router(build_dashboard_router(db_path=db))
    return TestClient(app)


def test_overview_endpoint(tmp_path):
    r = _client(tmp_path).get("/api/v1/dashboard/overview")
    assert r.status_code == 200
    body = r.json()
    assert body["trajs"] == 1 and body["atoms"] == 6 and body["skill_yield"] == 100.0


def test_by_domain_endpoint(tmp_path):
    r = _client(tmp_path).get("/api/v1/dashboard/by-domain")
    assert r.status_code == 200
    body = r.json()
    assert body["by_ecosystem"][0]["ecosystem"] == "claude_code"
    assert body["by_model"][0]["model"] == "m"


def test_serves_index_html(tmp_path):
    r = _client(tmp_path).get("/")
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]
