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
    assert body["trajs"] == 1 and body["atoms"] == 6
    assert "skill_yield" not in body  # 死指标已下线（审计 P2-8）


def test_by_domain_endpoint(tmp_path):
    r = _client(tmp_path).get("/api/v1/dashboard/by-domain")
    assert r.status_code == 200
    body = r.json()
    assert body["by_ecosystem"][0]["ecosystem"] == "claude_code"
    assert body["by_model"][0]["model"] == "m"


def test_paused_user_backlog_is_hidden_until_resume(tmp_path):
    db = tmp_path / "paused.db"
    conn = get_connection(db)
    conn.execute(
        "INSERT INTO watch_dirs(id,path,label,ecosystem,auto_index)"
        " VALUES(1,'/active','local','claude_code',1)"
    )
    conn.execute(
        "INSERT INTO watch_dirs(id,path,label,ecosystem,auto_index)"
        " VALUES(2,'/paused','alice','team_client',0)"
    )
    rows = [
        (1, "active-pending.md", "discovered", 0, "active-model", "codex"),
        (2, "paused-pending.md", "discovered", 0, "backlog-model", "opencode"),
        (2, "paused-splitting.md", "splitting", 0, "backlog-model", "opencode"),
        (2, "paused-split.md", "split_done", 3, "split-model", "codex"),
        (2, "paused-done.md", "done", 5, "done-model", "claude_code"),
    ]
    conn.executemany(
        "INSERT INTO trajectories("
        "watch_dir_id,filename,status,tasks_extracted,source_model,source_harness"
        ") VALUES(?,?,?,?,?,?)",
        rows,
    )
    conn.commit()
    conn.close()

    app = FastAPI()
    app.include_router(build_dashboard_router(db_path=db))
    client = TestClient(app)

    assert client.get("/api/v1/dashboard/overview").json()["trajs"] == 3
    domains = client.get("/api/v1/dashboard/by-domain").json()
    assert sum(row["trajs"] for row in domains["by_model"]) == 3
    assert all(row["model"] != "backlog-model" for row in domains["by_model"])
    models = client.get("/api/v1/dashboard/models").json()
    assert sum(row["trajs"] for row in models["models"]) == 3
    assert all(row["model"] != "backlog-model" for row in models["models"])
    dirs = {
        row["label"]: row
        for row in client.get("/api/v1/dashboard/dirs").json()["dirs"]
    }
    assert dirs["alice"]["traj_count"] == 2
    pipeline = client.get("/api/v1/dashboard/pipeline").json()["stages"]
    assert pipeline == {
        "pending_split": 1,
        "splitting": 0,
        "clustering": 1,
        "done": 1,
        "error": 0,
    }
    users = client.get("/api/v1/dashboard/users").json()["users"]
    assert users[0]["client_id"] == "alice"
    assert users[0]["trajs"] == 2

    with get_connection(db) as conn:
        conn.execute("UPDATE watch_dirs SET auto_index=1 WHERE id=2")
        conn.commit()

    assert client.get("/api/v1/dashboard/overview").json()["trajs"] == 5
    resumed_pipeline = client.get(
        "/api/v1/dashboard/pipeline"
    ).json()["stages"]
    assert resumed_pipeline["pending_split"] == 2
    assert resumed_pipeline["splitting"] == 1


def test_serves_index_html(tmp_path):
    r = _client(tmp_path).get("/")
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]


def test_serves_local_i18n_script(tmp_path):
    r = _client(tmp_path).get("/i18n.js")
    assert r.status_code == 200
    assert "application/javascript" in r.headers["content-type"]
    assert "xskill.dashboard.language" in r.text


def test_skill_dir_for_respects_config_yaml(tmp_path):
    """独立只读实例按 registry 所在 home 读 skill_dir，不写死同级 skill/。"""
    from xskill.dashboard.router import _skill_dir_for

    db = tmp_path / "registry.db"
    db.write_bytes(b"")
    assert _skill_dir_for(db) == tmp_path / "skill"

    custom = tmp_path / "company_skills"
    custom.mkdir()
    (tmp_path / "config.yaml").write_text(f"skill_dir: {custom}\n", encoding="utf-8")
    assert _skill_dir_for(db) == custom
