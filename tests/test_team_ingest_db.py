"""tests/test_team_ingest_db.py — team server `/ingest-db` 上传 db 端点（子项目 B-2）"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from xskill.pipeline import registry as R
from xskill.team.server import api as server_api
from xskill.team.server.client_registry import ClientRegistry

FIXTURE_DB = Path(__file__).parent / "fixtures" / "opencode" / "sample.db"


@pytest.fixture
def client_and_root(tmp_path, monkeypatch):
    # uploads 目录隔离到 tmp（避免落到真 ~/.xskill/uploads）
    monkeypatch.setattr("xskill.config.get_uploads_dir",
                        lambda: tmp_path / "uploads")
    skill_dir = tmp_path / "skill"
    skill_dir.mkdir()
    traj_root = tmp_path / "team_traj"
    reg = ClientRegistry(tmp_path / "clients.db")
    server_api.init_team_context(
        join_token="secret-token",
        client_registry=reg,
        skill_dir=skill_dir,
        traj_root=traj_root,
        register_dir=lambda path, label: None,
    )
    app = FastAPI()
    app.include_router(server_api.router)
    return TestClient(app), traj_root


def _register(client) -> str:
    r = client.post("/api/v1/team/register",
                    json={"token": "secret-token", "client_label": "alice",
                          "hostname": "a"})
    assert r.status_code == 200
    return r.json()["client_id"]


def test_ingest_db_requires_auth(client_and_root):
    client, _ = client_and_root
    with FIXTURE_DB.open("rb") as fh:
        r = client.post("/api/v1/team/ingest-db",
                        files={"file": ("ngagent.db", fh, "application/octet-stream")})
    assert r.status_code in (401, 403)


def test_ingest_db_bridges_into_client_bucket(client_and_root):
    client, traj_root = client_and_root
    cid = _register(client)
    hdr = {"X-Xskill-Token": "secret-token", "X-Xskill-Client": cid}

    with FIXTURE_DB.open("rb") as fh:
        r = client.post(
            "/api/v1/team/ingest-db",
            headers=hdr,
            data={"eco": "ngagent"},
            files={"file": ("ngagent.db", fh, "application/octet-stream")},
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["client_id"] == cid
    assert body["bridged"] >= 1

    # bridged 轨迹落在该 client 的 sessions 桶里
    sessions = traj_root / "clients" / cid / "sessions"
    assert list(sessions.glob("traj_ng_*.md")), "未在 client 桶里看到 bridged traj"


def test_ingest_db_preserves_paused_watch_dir_and_resume_reenables_it(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(
        "xskill.config.get_uploads_dir", lambda: tmp_path / "uploads",
    )
    skill_dir = tmp_path / "skill"
    skill_dir.mkdir()
    traj_root = tmp_path / "team_traj"
    registry_db = tmp_path / "registry.db"
    clients = ClientRegistry(tmp_path / "clients.db")

    def configure_watch_dir(path: Path, label: str, auto_index: bool) -> None:
        R.register_dir(
            path,
            label=label,
            auto_index=auto_index,
            ecosystem="team_client",
            db_path=registry_db,
        )

    server_api.init_team_context(
        join_token="secret-token",
        client_registry=clients,
        skill_dir=skill_dir,
        traj_root=traj_root,
        register_dir=lambda path, label: configure_watch_dir(path, label, True),
        configure_watch_dir=configure_watch_dir,
    )
    app = FastAPI()
    app.include_router(server_api.router)
    http = TestClient(app)
    registered = http.post(
        "/api/v1/team/register",
        json={"token": "secret-token", "user_name": "alice"},
    )
    client_id = registered.json()["client_id"]
    clients.set_ingest_paused(client_id, True, actor="boss")
    headers = {
        "X-Xskill-Token": "secret-token",
        "X-Xskill-Client": client_id,
    }

    with FIXTURE_DB.open("rb") as fh:
        response = http.post(
            "/api/v1/team/ingest-db",
            headers=headers,
            data={"eco": "ngagent"},
            files={"file": ("ngagent.db", fh, "application/octet-stream")},
        )

    assert response.status_code == 200, response.text
    assert response.json()["bridged"] >= 1
    sessions_dir = traj_root / "clients" / "alice" / "sessions"
    assert list(sessions_dir.glob("traj_ng_*.md"))
    assert R.get_watch_dir(sessions_dir, db_path=registry_db)["auto_index"] == 0

    clients.set_ingest_paused(client_id, False, actor="boss")
    server_api.reconcile_client_ingest_watch_dir(client_id)
    assert R.get_watch_dir(sessions_dir, db_path=registry_db)["auto_index"] == 1
