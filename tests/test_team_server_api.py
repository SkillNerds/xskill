from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from xskill.team.server import api as server_api
from xskill.team.server.client_registry import ClientRegistry


def _git(args, cwd):
    subprocess.run(["git"] + args, cwd=str(cwd), capture_output=True, text=True, check=True)


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _make_skill(skill_dir: Path, name: str):
    d = skill_dir / name
    d.mkdir(parents=True)
    _git(["init", "-q"], d); _git(["checkout", "-q", "-b", "main"], d)
    _git(["config", "user.email", "t@t"], d); _git(["config", "user.name", "t"], d)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: d\nmetadata:\n  version: 1\n---\n# {name}\n",
        encoding="utf-8")
    _git(["add", "."], d); _git(["commit", "-q", "-m", "v1"], d)
    return d


@pytest.fixture
def client(tmp_path):
    skill_dir = tmp_path / "skill"
    skill_dir.mkdir()
    _make_skill(skill_dir, "fix-foo")
    traj_root = tmp_path / "team_traj"
    reg = ClientRegistry(tmp_path / "clients.db")
    server_api.init_team_context(
        join_token="secret-token",
        client_registry=reg,
        skill_dir=skill_dir,
        traj_root=traj_root,
        probability=0.2, ranked_slots=80, total_slots=100,
        register_dir=lambda path, label: None,   # 测试不碰真 registry.db
    )
    app = FastAPI()
    app.include_router(server_api.router)
    return TestClient(app)


def test_register_then_use_endpoints(client):
    # 错 token 被拒
    r = client.post("/api/v1/team/register", json={"token": "wrong"})
    assert r.status_code == 401
    # 正确 token → 拿 client_id
    r = client.post("/api/v1/team/register",
                    json={"token": "secret-token", "client_label": "alice", "hostname": "a"})
    assert r.status_code == 200
    cid = r.json()["client_id"]

    hdr = {"X-Xskill-Token": "secret-token", "X-Xskill-Client": cid}
    # 上传一条轨迹（sha256 必须真实——server 端校验完整性）
    body = "# hello"
    r = client.post("/api/v1/team/upload", headers=hdr, json={
        "trajectories": [{"traj_id": "traj_cc_x_001", "content": body,
                          "sha256": _sha(body)}]})
    assert r.status_code == 200
    assert r.json()["accepted"] == ["traj_cc_x_001"]

    # sha256 不匹配 → 拒收
    r = client.post("/api/v1/team/upload", headers=hdr, json={
        "trajectories": [{"traj_id": "traj_cc_x_002", "content": "# x",
                          "sha256": "deadbeef"}]})
    assert r.status_code == 200
    assert r.json()["accepted"] == []
    assert r.json()["rejected"][0]["traj_id"] == "traj_cc_x_002"

    # sync 拿 manifest
    r = client.get("/api/v1/team/sync", headers=hdr)
    assert r.status_code == 200
    names = [s["skill_name"] for s in r.json()["slots"]]
    assert "fix-foo" in names

    # 拉 skill bundle
    r = client.get("/api/v1/team/skill/fix-foo/bundle", headers=hdr)
    assert r.status_code == 200
    assert len(r.content) > 0


def test_unknown_client_rejected(client):
    hdr = {"X-Xskill-Token": "secret-token", "X-Xskill-Client": "ghost"}
    r = client.get("/api/v1/team/sync", headers=hdr)
    assert r.status_code == 403


def test_version_reports_matching_server_wheel(client, tmp_path, monkeypatch):
    whl_dir = tmp_path / "whls"
    whl_dir.mkdir()
    wheel = whl_dir / "xskill-1.2.3-py3-none-any.whl"
    wheel.write_bytes(b"wheel-bytes")
    (whl_dir / "xskill-1.2.2-py3-none-any.whl").write_bytes(b"old")
    (whl_dir / "other-1.2.3-py3-none-any.whl").write_bytes(b"other")
    monkeypatch.setattr(server_api, "XSKILL_VERSION", "1.2.3")
    monkeypatch.setattr(server_api, "get_team_server_whl_dir", lambda: whl_dir)

    r = client.post("/api/v1/team/register", json={"token": "secret-token"})
    cid = r.json()["client_id"]
    hdr = {"X-Xskill-Token": "secret-token", "X-Xskill-Client": cid}

    r = client.get("/api/v1/team/version", headers=hdr)
    assert r.status_code == 200
    assert r.json() == {
        "package": "xskill",
        "version": "1.2.3",
        "wheel_available": True,
        "wheel_filename": "xskill-1.2.3-py3-none-any.whl",
    }

    r = client.get("/api/v1/team/wheel", headers=hdr)
    assert r.status_code == 200
    assert r.content == b"wheel-bytes"


def test_wheel_endpoint_404_when_matching_wheel_missing(client, tmp_path, monkeypatch):
    whl_dir = tmp_path / "whls"
    whl_dir.mkdir()
    (whl_dir / "xskill-1.2.2-py3-none-any.whl").write_bytes(b"old")
    monkeypatch.setattr(server_api, "XSKILL_VERSION", "1.2.3")
    monkeypatch.setattr(server_api, "get_team_server_whl_dir", lambda: whl_dir)
    monkeypatch.setattr(
        server_api,
        "_build_installed_distribution_wheel",
        lambda package, version: None,
    )

    r = client.post("/api/v1/team/register", json={"token": "secret-token"})
    cid = r.json()["client_id"]
    hdr = {"X-Xskill-Token": "secret-token", "X-Xskill-Client": cid}

    r = client.get("/api/v1/team/version", headers=hdr)
    assert r.status_code == 200
    assert r.json()["wheel_available"] is False
    assert r.json()["wheel_filename"] is None

    r = client.get("/api/v1/team/wheel", headers=hdr)
    assert r.status_code == 404


def test_version_lazily_generates_server_wheel(client, tmp_path, monkeypatch):
    whl_dir = tmp_path / "whls"
    whl_dir.mkdir()
    generated = whl_dir / "xskill-1.2.3-py3-none-any.whl"
    monkeypatch.setattr(server_api, "XSKILL_VERSION", "1.2.3")
    monkeypatch.setattr(server_api, "get_team_server_whl_dir", lambda: whl_dir)

    def fake_build(package: str, version: str) -> Path:
        assert package == "xskill"
        assert version == "1.2.3"
        generated.write_bytes(b"generated-wheel")
        return generated

    monkeypatch.setattr(
        server_api,
        "_build_installed_distribution_wheel",
        fake_build,
    )

    r = client.post("/api/v1/team/register", json={"token": "secret-token"})
    cid = r.json()["client_id"]
    hdr = {"X-Xskill-Token": "secret-token", "X-Xskill-Client": cid}

    r = client.get("/api/v1/team/version", headers=hdr)
    assert r.status_code == 200
    assert r.json()["wheel_available"] is True
    assert r.json()["wheel_filename"] == "xskill-1.2.3-py3-none-any.whl"
    assert generated.read_bytes() == b"generated-wheel"

    r = client.get("/api/v1/team/wheel", headers=hdr)
    assert r.status_code == 200
    assert r.content == b"generated-wheel"


def test_upload_writes_traj_md_under_client_bucket(client, tmp_path):
    r = client.post("/api/v1/team/register", json={"token": "secret-token"})
    cid = r.json()["client_id"]
    hdr = {"X-Xskill-Token": "secret-token", "X-Xskill-Client": cid}
    body = "# body"
    client.post("/api/v1/team/upload", headers=hdr, json={
        "trajectories": [{"traj_id": "traj_cc_x_001", "content": body,
                          "sha256": _sha(body)}]})
    expected = (tmp_path / "team_traj" / "clients" / cid / "sessions"
                / "traj_cc_x_001.md")
    assert expected.is_file()
    assert expected.read_text(encoding="utf-8") == body


def test_upload_with_model_writes_json_sidecar(client, tmp_path):
    import json as _json
    r = client.post("/api/v1/team/register", json={"token": "secret-token"})
    cid = r.json()["client_id"]
    hdr = {"X-Xskill-Token": "secret-token", "X-Xskill-Client": cid}
    body = "# body"
    client.post("/api/v1/team/upload", headers=hdr, json={
        "trajectories": [{"traj_id": "traj_cc_x_001", "content": body,
                          "sha256": _sha(body), "model": "claude-opus-4-7"}]})
    sess = tmp_path / "team_traj" / "clients" / cid / "sessions"
    sidecar = sess / "traj_cc_x_001.json"
    assert sidecar.is_file()
    assert _json.loads(sidecar.read_text(encoding="utf-8"))["model"] == "claude-opus-4-7"


def test_upload_without_model_no_json_sidecar(client, tmp_path):
    r = client.post("/api/v1/team/register", json={"token": "secret-token"})
    cid = r.json()["client_id"]
    hdr = {"X-Xskill-Token": "secret-token", "X-Xskill-Client": cid}
    body = "# body"
    client.post("/api/v1/team/upload", headers=hdr, json={
        "trajectories": [{"traj_id": "traj_cc_x_001", "content": body,
                          "sha256": _sha(body)}]})   # 不带 model
    sess = tmp_path / "team_traj" / "clients" / cid / "sessions"
    assert (sess / "traj_cc_x_001.md").is_file()
    assert not (sess / "traj_cc_x_001.json").exists()   # 行为不回归
