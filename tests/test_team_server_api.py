from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from xskill.pipeline import registry as R
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


def test_paused_upload_is_accepted_and_backlog_is_discovered_after_resume(
    tmp_path,
):
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
    clients.set_ingest_paused(client_id, True, actor="boss", reason="review")

    content = "# paused trajectory"
    response = http.post(
        "/api/v1/team/upload",
        headers={
            "X-Xskill-Token": "secret-token",
            "X-Xskill-Client": client_id,
        },
        json={
            "trajectories": [{
                "traj_id": "traj_cc_paused_001",
                "content": content,
                "sha256": _sha(content),
            }],
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "accepted": ["traj_cc_paused_001"],
        "rejected": [],
    }
    sessions_dir = traj_root / "clients" / "alice" / "sessions"
    stored_name = f"traj_alice_{client_id[:8]}_cc_paused_001.md"
    assert (sessions_dir / stored_name).read_text() == content
    watch_dir = R.get_watch_dir(sessions_dir, db_path=registry_db)
    assert watch_dir["auto_index"] == 0
    with R.pooled_connection(registry_db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM trajectories").fetchone()[0] == 0

    # 模拟 watcher 的真实入口：暂停目录不会被调度；恢复只翻执行开关，
    # backlog 文件保持原位并在下一轮可被处理。
    clients.set_ingest_paused(client_id, False, actor="boss")
    state = server_api.reconcile_client_ingest_watch_dir(client_id)
    assert state["ingest_paused"] is False
    assert R.get_watch_dir(sessions_dir, db_path=registry_db)["auto_index"] == 1
    assert R.discover_trajectories(
        watch_dir["id"], sessions_dir, db_path=registry_db,
    ) == [stored_name]


def test_sync_auth_uses_current_token_and_delete_revokes_immediately(client):
    registered = client.post(
        "/api/v1/team/register",
        json={"token": "secret-token", "client_label": "alice", "hostname": "a"},
    )
    cid = registered.json()["client_id"]
    registry = server_api._ctx.client_registry
    assert registry is not None

    wrong = client.get(
        "/api/v1/team/sync",
        headers={"X-Xskill-Token": "wrong", "X-Xskill-Client": cid},
    )
    assert wrong.status_code == 401

    # 配置重载后旧 token 必须立刻失效；同一个 registry 不应被关闭。
    server_api.init_team_context(
        join_token="rotated-token",
        client_registry=registry,
        skill_dir=server_api._ctx.skill_dir,
        traj_root=server_api._ctx.traj_root,
        register_dir=server_api._ctx.register_dir,
        skillhub=server_api._ctx.skillhub,
        profile_refresh_service=server_api._ctx.profile_refresh_service,
    )
    old_token = client.get(
        "/api/v1/team/sync",
        headers={"X-Xskill-Token": "secret-token", "X-Xskill-Client": cid},
    )
    assert old_token.status_code == 401
    current = client.get(
        "/api/v1/team/sync",
        headers={"X-Xskill-Token": "rotated-token", "X-Xskill-Client": cid},
    )
    assert current.status_code == 200

    assert registry.delete(cid) is True
    revoked = client.get(
        "/api/v1/team/sync",
        headers={"X-Xskill-Token": "rotated-token", "X-Xskill-Client": cid},
    )
    assert revoked.status_code == 403


def test_reinitializing_context_flushes_and_closes_previous_registry(
    client, tmp_path, monkeypatch,
):
    previous = server_api._ctx.client_registry
    assert previous is not None
    cid = previous.register(label="pending", hostname="old", client_version="1.0")
    monkeypatch.setattr(
        previous, "_schedule_touch_flush_locked", lambda _delay: None,
    )
    assert previous.authenticate_and_touch(cid, "2.0") is True

    replacement = ClientRegistry(tmp_path / "replacement.db")
    server_api.init_team_context(
        join_token="replacement-token",
        client_registry=replacement,
        skill_dir=server_api._ctx.skill_dir,
        traj_root=server_api._ctx.traj_root,
        register_dir=server_api._ctx.register_dir,
        skillhub=server_api._ctx.skillhub,
        profile_refresh_service=server_api._ctx.profile_refresh_service,
    )

    assert previous.authenticate_and_touch(cid) is False
    assert previous._pending_touches == {}
    assert previous.get(cid)["client_version"] == "2.0"
    # 同一个 registry 的配置刷新只轮换 token，不误关当前实例。
    server_api.init_team_context(
        join_token="replacement-token-2",
        client_registry=replacement,
        skill_dir=server_api._ctx.skill_dir,
        traj_root=server_api._ctx.traj_root,
        register_dir=server_api._ctx.register_dir,
        skillhub=server_api._ctx.skillhub,
        profile_refresh_service=server_api._ctx.profile_refresh_service,
    )
    replacement_id = replacement.register(label="still-open")
    assert replacement.authenticate_and_touch(replacement_id) is True


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
    # 落盘名带成员标识前缀（issue #234；匿名客户端为 u_ + client_id 前 8 位）
    expected = (tmp_path / "team_traj" / "clients" / cid / "sessions"
                / f"traj_u_{cid[:8]}_cc_x_001.md")
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
    sidecar = sess / f"traj_u_{cid[:8]}_cc_x_001.json"
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
    assert (sess / f"traj_u_{cid[:8]}_cc_x_001.md").is_file()
    # 行为不回归
    assert not (sess / f"traj_u_{cid[:8]}_cc_x_001.json").exists()


# ── 推荐调优热生效(回归:曾被 _ctx 启动快照冻死,面板改完必须重启) ────

def testlive_manifest_tuning_reads_config_without_reinit():
    """三个调优值必须每次现取 live config,而不是吃 init_team_context 快照。

    回归:probability 源段 canary 在 HOT_RELOAD_SECTIONS(面板改完不提示重启),
    但值曾被快照进 _ctx → 静默不生效,必须重启 serve。
    """
    from xskill.api import app as app_mod

    app_mod._config = {
        "team": {"server": {"skill_slots": 7, "ranked_slots": 3}},
        "canary": {"probability": 0.9},
    }
    assert server_api.live_manifest_tuning() == (7, 3, 0.9)

    # 原地改(= admin_config_reload 的热加载做法),不重调 init_team_context
    app_mod._config["team"]["server"]["skill_slots"] = 11
    app_mod._config["canary"]["probability"] = 0.1
    assert server_api.live_manifest_tuning() == (11, 3, 0.1)


def testlive_manifest_tuning_falls_back_to_defaults_when_unset():
    from xskill.api import app as app_mod
    app_mod._config = None
    assert server_api.live_manifest_tuning() == (100, 80, 0.2)


def test_sync_slot_count_follows_config_without_restart(client):
    """端到端:改 team.server.skill_slots 后,下一次 /sync 立刻按新值分发,
    无需重启 serve、无需重调 init_team_context。"""
    from xskill.api import app as app_mod

    r = client.post("/api/v1/team/register", json={"token": "secret-token"})
    cid = r.json()["client_id"]
    hdr = {"X-Xskill-Token": "secret-token", "X-Xskill-Client": cid}

    app_mod._config = {"team": {"server": {"skill_slots": 100, "ranked_slots": 80}}}
    assert "fix-foo" in [s["skill_name"]
                         for s in client.get("/api/v1/team/sync", headers=hdr).json()["slots"]]

    # 面板把分发关掉 → 下一次 sync 即刻为空(没有任何重启/重init)
    app_mod._config["team"]["server"]["skill_slots"] = 0
    assert client.get("/api/v1/team/sync", headers=hdr).json()["slots"] == []

    # 再打开 → 又立刻恢复
    app_mod._config["team"]["server"]["skill_slots"] = 100
    assert "fix-foo" in [s["skill_name"]
                         for s in client.get("/api/v1/team/sync", headers=hdr).json()["slots"]]


def test_allow_anonymous_user_is_hot_without_restart(client):
    """回归:allow_anonymous_user 曾被 init_team_context 快照进 _ctx(只在 serve
    启动时填一次),面板改完必须重启 serve 才生效。现在 /register 每请求现取
    live config(admin_config_reload 原地 mutate 的那个 dict)。

    全程不重新 init_team_context——重启即视为不通过。
    """
    from xskill.api import app as app_mod

    app_mod._config = {"team": {"server": {"allow_anonymous_user": True}}}
    allowed = client.post(
        "/api/v1/team/register",
        json={"token": "secret-token", "client_label": "x", "hostname": "h"})
    assert allowed.status_code == 200

    # 面板关掉匿名(原地改同一个 dict) → 下一次注册立刻被拒
    app_mod._config["team"]["server"]["allow_anonymous_user"] = False
    rejected = client.post(
        "/api/v1/team/register",
        json={"token": "secret-token", "client_label": "x", "hostname": "h"})
    assert rejected.status_code == 403          # 旧代码:仍是 200
    assert "anonymous" in rejected.json()["detail"].lower()

    # 具名注册不受影响;再打开 → 匿名又放行
    named = client.post(
        "/api/v1/team/register", json={"token": "secret-token", "user_name": "alice"})
    assert named.status_code == 200
    app_mod._config["team"]["server"]["allow_anonymous_user"] = True
    assert client.post(
        "/api/v1/team/register",
        json={"token": "secret-token", "client_label": "x", "hostname": "h"},
    ).status_code == 200


def test_register_rejects_malformed_team_section_loudly(client):
    """畸形 team 段不得把 /register 变成 500 之外的静默放行:
    config parser 抛 ValueError(fail-loud),不静默取默认值。"""
    import pytest
    from xskill.api import app as app_mod

    app_mod._config = {"team": {"server": {"allow_anonymous_user": "no"}}}
    with pytest.raises(ValueError, match="allow_anonymous_user"):
        client.post("/api/v1/team/register",
                    json={"token": "secret-token", "client_label": "x"})
