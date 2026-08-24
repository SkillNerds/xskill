"""test_dashboard_console_p2.py —— P2 控制面（登录/角色/prefs/manifest 注入/生命周期/设置页）

覆盖 openspec dashboard-console-redesign P2 的 SHALL 条款：
- 2.2 登录:匿名 401 / 普通用户写 admin 端点 403 / admin 口令空=关闭
- 2.4 注入顺序:blocked 排除 → pinned 占位 → ranked → recommended 回填
- 2.4d 超量写入侧拒绝(409),含全局 pin 全员合计
- 2.4c retire 不分发不裁决;delete 需二次确认,prefs 清理
- 2.9 校验失败不落盘不生效
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from xskill.dashboard.auth import (
    build_auth_router, configure_auth, ensure_dashboard_secret,
)
from xskill.dashboard.console import build_console_router
from xskill.pipeline import registry as R
from xskill.team.server.api import init_team_context, team_context
from xskill.team.server.client_registry import ClientRegistry
from xskill.team.server.skill_manifest import build_manifest


def _git(args, cwd):
    subprocess.run(["git"] + args, cwd=str(cwd),
                   capture_output=True, text=True, check=True)


def _make_skill(root: Path, name: str) -> Path:
    d = root / name
    d.mkdir(parents=True)
    _git(["init", "-q"], d)
    _git(["checkout", "-q", "-b", "main"], d)
    _git(["config", "user.email", "t@t"], d)
    _git(["config", "user.name", "t"], d)
    (d / "SKILL.md").write_text(f"# {name}\n", encoding="utf-8")
    _git(["add", "."], d)
    _git(["commit", "-q", "-m", "v1"], d)
    return d


@pytest.fixture()
def console_env(tmp_path):
    """team ctx + auth + console app。alice=普通用户,boss=admin。"""
    skills = tmp_path / "skills"
    skills.mkdir()
    _git(["init", "-q"], skills)
    _git(["checkout", "-q", "-b", "main"], skills)
    _git(["config", "user.email", "t@t"], skills)
    _git(["config", "user.name", "t"], skills)
    for n in ("alpha", "beta", "gamma"):
        _make_skill(skills, n)
    db = tmp_path / "r.db"
    reg = ClientRegistry(tmp_path / "c.db")
    cid = reg.register(user_name="alice")
    token = reg.ensure_dashboard_token(cid)

    def configure_watch_dir(path: Path, label: str, auto_index: bool) -> None:
        R.register_dir(
            path,
            label=label,
            auto_index=auto_index,
            ecosystem="team_client",
            db_path=db,
        )

    init_team_context(
        join_token="jt", client_registry=reg, skill_dir=skills,
        traj_root=tmp_path / "traj",
        register_dir=lambda path, label: configure_watch_dir(path, label, True),
        configure_watch_dir=configure_watch_dir)
    # 槽位改由现取 live config(热生效),不再走 init_team_context 快照
    from xskill.api import app as app_mod
    app_mod._config = {"team": {"server": {"skill_slots": 3, "ranked_slots": 2}}}
    configure_auth(
        secret=ensure_dashboard_secret(tmp_path / "sec.json"),
        admins=["boss"], admin_password="pw",
        registry_provider=lambda: reg)
    app = FastAPI()
    app.include_router(build_auth_router())
    app.include_router(build_console_router(db_path=db))

    alice = TestClient(app)
    r = alice.post("/api/v1/dashboard/login",
                   json={"user_name": "alice", "secret": token})
    assert r.status_code == 200 and r.json()["role"] == "user"
    boss = TestClient(app)
    r = boss.post("/api/v1/dashboard/login",
                  json={"user_name": "boss", "secret": "pw"})
    assert r.status_code == 200 and r.json()["role"] == "admin"
    return {"app": app, "alice": alice, "boss": boss, "db": db,
            "skills": skills, "token": token, "registry": reg}


# ── 2.2 登录/角色 ─────────────────────────────────────────────────────

def test_anonymous_gets_401(console_env):
    anon = TestClient(console_env["app"])
    assert anon.get("/api/v1/dashboard/my/manifest").status_code == 401
    assert anon.get("/api/v1/dashboard/admin/skills").status_code == 401


def test_wrong_credentials_401(console_env):
    anon = TestClient(console_env["app"])
    assert anon.post("/api/v1/dashboard/login",
                     json={"user_name": "alice", "secret": "bad"}
                     ).status_code == 401
    # admin 名单里的人用错口令也 401(token 路径不给 admin 角色)
    assert anon.post("/api/v1/dashboard/login",
                     json={"user_name": "boss", "secret": "bad"}
                     ).status_code == 401


def test_user_hitting_admin_endpoint_403(console_env):
    alice = console_env["alice"]
    assert alice.get("/api/v1/dashboard/admin/skills").status_code == 403
    assert alice.get(
        "/api/v1/dashboard/admin/user/alice/recommendations"
    ).status_code == 403
    assert alice.post(
        "/api/v1/dashboard/admin/skill/alpha/retire").status_code == 403
    assert alice.get("/api/v1/dashboard/admin/config").status_code == 403


def test_logout_invalidates_session(console_env):
    boss = console_env["boss"]
    assert boss.get("/api/v1/dashboard/admin/skills").status_code == 200
    boss.post("/api/v1/dashboard/logout")
    assert boss.get("/api/v1/dashboard/admin/skills").status_code == 401


def test_dashboard_token_issued_and_idempotent(console_env):
    reg = console_env["registry"]
    cid = reg.find_by_user_name("alice")
    assert reg.ensure_dashboard_token(cid) == console_env["token"]
    assert reg.dashboard_token_for("alice") == console_env["token"]


# ── 2.4 manifest 注入顺序 ─────────────────────────────────────────────

def test_injection_order_blocked_pinned_ranked_recommended(console_env):
    db, skills = console_env["db"], console_env["skills"]
    R.set_skill_pref(user_key="alice", skill_name="gamma", pref="pinned",
                     set_by="alice", db_path=db)
    R.set_skill_pref(user_key="alice", skill_name="alpha", pref="blocked",
                     set_by="alice", db_path=db)
    prefs = R.effective_prefs("alice", db_path=db)
    resp = build_manifest(client_id="c", skill_dir=skills, probability=0.2,
                          ranked_slots=2, total_slots=3,
                          prefs=prefs, retired=set())
    got = [(s.skill_name, s.bucket) for s in resp.slots]
    assert got[0] == ("gamma", "pinned")
    assert all(n != "alpha" for n, _ in got)


def test_global_pin_ordered_before_user_pin(console_env):
    db = console_env["db"]
    R.set_skill_pref(user_key="alice", skill_name="beta", pref="pinned",
                     set_by="alice", db_path=db)
    R.set_skill_pref(user_key=R.GLOBAL_PREF_KEY, skill_name="gamma",
                     pref="pinned", set_by="boss", db_path=db)
    prefs = R.effective_prefs("alice", db_path=db)
    assert prefs["pinned"] == ["gamma", "beta"]
    assert prefs["pin_meta"]["gamma"]["scope"] == "global"


def test_retired_not_distributed_even_if_pinned(console_env):
    db, skills = console_env["db"], console_env["skills"]
    R.set_skill_pref(user_key="alice", skill_name="gamma", pref="pinned",
                     set_by="alice", db_path=db)
    resp = build_manifest(client_id="c", skill_dir=skills, probability=0.2,
                          ranked_slots=2, total_slots=3,
                          prefs=R.effective_prefs("alice", db_path=db),
                          retired={"gamma"})
    assert all(s.skill_name != "gamma" for s in resp.slots)


def test_annotate_library_skills_pin_and_push(console_env):
    from xskill.dashboard.console import annotate_library_skills

    db = console_env["db"]
    R.set_skill_pref(user_key="alice", skill_name="gamma", pref="pinned",
                     set_by="alice", db_path=db)
    R.set_skill_pref(user_key="alice", skill_name="alpha", pref="blocked",
                     set_by="alice", db_path=db)
    page = {"skills": [
        {"name": "alpha"}, {"name": "beta"}, {"name": "gamma"},
    ]}
    annotate_library_skills(page, user="alice", db_path=db)
    assert page["viewer"]["can_pin"] is True
    by_name = {row["name"]: row for row in page["skills"]}
    assert by_name["gamma"]["pinned"] is True
    assert by_name["gamma"]["in_push"] is True
    assert by_name["gamma"]["bucket"] == "pinned"
    assert by_name["gamma"]["user_removable"] is True
    assert by_name["alpha"]["pinned"] is False
    assert by_name["alpha"]["in_push"] is False
    assert by_name["beta"]["pinned"] is False
    assert by_name["beta"]["in_push"] is True
    assert by_name["beta"]["bucket"] in ("ranked", "recommended")


def test_annotate_library_skills_admin_pin_not_user_removable(console_env):
    from xskill.dashboard.console import annotate_library_skills

    db = console_env["db"]
    R.set_skill_pref(user_key=R.GLOBAL_PREF_KEY, skill_name="gamma",
                     pref="pinned", set_by="boss", db_path=db)
    page = {"skills": [{"name": "gamma"}]}
    annotate_library_skills(page, user="alice", db_path=db)
    row = page["skills"][0]
    assert row["pinned"] is True
    assert row["pin_scope"] == "global"
    assert row["user_removable"] is False


# ── 2.4d 写入侧超量拒绝 ───────────────────────────────────────────────

def test_pin_quota_rejected_at_write_side(console_env):
    alice, boss = console_env["alice"], console_env["boss"]
    assert alice.post("/api/v1/dashboard/my/prefs",
                      json={"skill_name": "alpha", "action": "pin"}
                      ).status_code == 200
    assert alice.post("/api/v1/dashboard/my/prefs",
                      json={"skill_name": "beta", "action": "pin"}
                      ).status_code == 200
    assert alice.post("/api/v1/dashboard/my/prefs",
                      json={"skill_name": "gamma", "action": "pin"}
                      ).status_code == 200
    # 第 4 个超 total_slots=3 → 409
    r = alice.post("/api/v1/dashboard/my/prefs",
                   json={"skill_name": "s4", "action": "pin"})
    assert r.status_code == 409
    # 全局 pin 会把 alice 顶爆 → 对全员合计校验后 409
    r = boss.post("/api/v1/dashboard/admin/prefs",
                  json={"user_key": R.GLOBAL_PREF_KEY,
                        "skill_name": "s5", "action": "pin"})
    assert r.status_code == 409


def test_admin_set_pref_immutable_by_user(console_env):
    alice, boss = console_env["alice"], console_env["boss"]
    assert boss.post("/api/v1/dashboard/admin/prefs",
                     json={"user_key": "alice", "skill_name": "beta",
                           "action": "pin"}).status_code == 200
    assert alice.post("/api/v1/dashboard/my/prefs",
                      json={"skill_name": "beta", "action": "clear"}
                      ).status_code == 403
    # 全局 pin 的条目不可屏蔽
    assert boss.post("/api/v1/dashboard/admin/prefs",
                     json={"user_key": R.GLOBAL_PREF_KEY,
                           "skill_name": "alpha", "action": "pin"}
                     ).status_code == 200
    assert alice.post("/api/v1/dashboard/my/prefs",
                      json={"skill_name": "alpha", "action": "block"}
                      ).status_code == 403


# ── 2.4c 生命周期 ────────────────────────────────────────────────────

def test_retire_stops_distribution_and_canary(console_env, tmp_path):
    alice, boss = console_env["alice"], console_env["boss"]
    assert boss.post(
        "/api/v1/dashboard/admin/skill/alpha/retire").status_code == 200
    m = alice.get("/api/v1/dashboard/my/manifest").json()
    assert all(s["skill_name"] != "alpha" for s in m["slots"])
    # canary 判定直接短路——check_and_decide 读默认注册库(生产=同一个库;
    # 本 fixture 的 console 用独立 tmp 库,故这里在默认库上单独验证)
    from xskill.canary import check_and_decide
    R.retire_skill(skill_name="alpha", set_by="t")
    try:
        assert check_and_decide(
            console_env["skills"] / "alpha")["action"] == "retired"
    finally:
        R.unretire_skill(skill_name="alpha")
    # 恢复在役
    assert boss.post(
        "/api/v1/dashboard/admin/skill/alpha/unretire").status_code == 200
    m = alice.get("/api/v1/dashboard/my/manifest").json()
    assert any(s["skill_name"] == "alpha" for s in m["slots"])


def test_delete_requires_confirm_and_purges_prefs(console_env):
    alice, boss, db = (console_env["alice"], console_env["boss"],
                       console_env["db"])
    assert alice.post("/api/v1/dashboard/my/prefs",
                      json={"skill_name": "gamma", "action": "pin"}
                      ).status_code == 200
    r = boss.request("DELETE", "/api/v1/dashboard/admin/skill/gamma",
                     json={"confirm_name": "not-gamma"})
    assert r.status_code == 400
    r = boss.request("DELETE", "/api/v1/dashboard/admin/skill/gamma",
                     json={"confirm_name": "gamma"})
    assert r.status_code == 200
    assert not (console_env["skills"] / "gamma").exists()
    assert all(p["skill_name"] != "gamma"
               for p in R.prefs_for("alice", db_path=db))


# ── 2.3/2.5/2.6 读视图 ──────────────────────────────────────────────

def test_my_views_empty_db_do_not_crash(console_env):
    alice = console_env["alice"]
    c = alice.get("/api/v1/dashboard/my/contributions")
    assert c.status_code == 200
    assert c.json()["steps"]["trajs"] == 0
    assert alice.get("/api/v1/dashboard/my/reco-trigger").json()["rows"] == []
    t = alice.get("/api/v1/dashboard/my/contributions/trajs")
    assert t.status_code == 200
    body = t.json()
    assert body["total"] == 0
    assert body["trajs"] == []
    m = alice.get("/api/v1/dashboard/my/manifest").json()
    assert "slots" in m
    assert "server_push" in m
    assert m["server_slots"] == 3
    for s in m["slots"]:
        assert "my_triggers" in s
        assert s["source"] in ("native", "upload", "skillhub")
    assert alice.get("/api/v1/dashboard/my/settings").status_code == 200
    assert alice.get("/api/v1/dashboard/my/uploads").json()["skills"] == []
    assert alice.get("/api/v1/dashboard/my/commits").json()["commits"] == []


def test_my_settings_take_n_truncates_manifest_slots(console_env):
    """看板改 take_n：slots=截取安装集，server_push 仍为完整服务器队列。"""
    alice = console_env["alice"]
    m0 = alice.get("/api/v1/dashboard/my/manifest").json()
    assert len(m0["server_push"]) == m0["server_pushed"] == 3
    assert len(m0["slots"]) == 3  # 默认 take_n = skill_slots
    r = alice.post("/api/v1/dashboard/my/settings", json={"take_n": 1})
    assert r.status_code == 200
    assert r.json()["take_n"] == 1
    m = alice.get("/api/v1/dashboard/my/manifest").json()
    assert m["settings"]["take_n"] == 1
    assert len(m["server_push"]) == 3
    assert len(m["slots"]) == 1
    assert m["slots"][0]["skill_name"] == m["server_push"][0]["skill_name"]
    assert m["installed"] == 1
    r0 = alice.post("/api/v1/dashboard/my/settings", json={"take_n": 0})
    assert r0.status_code == 200 and r0.json()["take_n"] == 0
    m0b = alice.get("/api/v1/dashboard/my/manifest").json()
    assert m0b["slots"] == []
    assert len(m0b["server_push"]) == 3
    bad = alice.post("/api/v1/dashboard/my/settings", json={"take_n": -1})
    assert bad.status_code == 400
    # 超过 skill_slots 夹取
    hi = alice.post("/api/v1/dashboard/my/settings", json={"push_count": 99})
    assert hi.status_code == 200 and hi.json()["take_n"] == 3


def test_my_commits_from_push_edit_events(console_env):
    alice, db = console_env["alice"], console_env["db"]
    from xskill.events import EventStore
    store = EventStore(db)
    store.emit_push_edit(actor="alice", skill="alpha",
                         branch="user-staging/c1", ref_sha="a" * 40)
    store.emit_canary(skill="alpha", action="promoted",
                      main_avg=7.0, staging_avg=8.0)
    store.emit_push_edit(actor="alice", skill="beta",
                         branch="user-staging/c1", ref_sha="b" * 40)
    body = alice.get("/api/v1/dashboard/my/commits").json()
    assert body["total"] >= 2
    by_skill = {c["skill"]: c for c in body["commits"]}
    assert by_skill["alpha"]["status"] == "absorbed"
    assert "被吸收到" in by_skill["alpha"]["status_label"]
    assert by_skill["beta"]["status"] in ("canary", "live")


def test_my_contribution_trajs_paginates_user_trajs(console_env):
    alice = console_env["alice"]
    db = console_env["db"]
    registry = console_env["registry"]
    client_id = registry.find_by_user_name("alice")
    sessions_dir = (
        team_context().traj_root
        / "clients"
        / registry.dir_name_for(client_id)
        / "sessions"
    )
    sessions_dir.mkdir(parents=True)
    R.register_dir(
        sessions_dir, label="alice", ecosystem="team_client", db_path=db,
    )
    with R.get_connection(db) as conn:
        wd = conn.execute(
            "SELECT id FROM watch_dirs WHERE path=?",
            (str(sessions_dir.resolve()),),
        ).fetchone()[0]
        for i, name in enumerate(("t-c.md", "t-b.md", "t-a.md")):
            conn.execute(
                "INSERT INTO trajectories("
                "watch_dir_id,filename,status,tasks_extracted,user_key,"
                "discovered_at) VALUES(?,?,?,?,?,?)",
                (wd, name, "done", 0, "alice",
                 f"2026-07-{10 + i:02d} 00:00:00"),
            )
        conn.commit()
    page = alice.get(
        "/api/v1/dashboard/my/contributions/trajs?offset=0&limit=2"
    ).json()
    assert page["total"] == 3
    assert len(page["trajs"]) == 2
    # discovered_at 倒序 → t-a, t-b
    assert [t["traj_id"] for t in page["trajs"]] == ["t-a", "t-b"]
    page2 = alice.get(
        "/api/v1/dashboard/my/contributions/trajs?offset=2&limit=2"
    ).json()
    assert [t["traj_id"] for t in page2["trajs"]] == ["t-c"]


def test_users_matrix_lists_clients_with_version(console_env):
    boss = console_env["boss"]
    reg = console_env["registry"]
    cid = reg.find_by_user_name("alice")
    reg.touch(cid, version="0.9.9")
    um = boss.get("/api/v1/dashboard/admin/users-matrix").json()
    row = next(u for u in um["users"] if u["user"] == "alice")
    assert row["client_version"] == "0.9.9"
    assert row["client_id"] == cid
    assert row["ingest_paused"] is False


def test_admin_recommendation_history_is_separate_and_paginated(console_env):
    db = console_env["db"]
    client_id = console_env["registry"].find_by_user_name("alice")
    with R.get_connection(db) as conn:
        conn.executemany(
            "INSERT INTO recommendation_log("
            "ts,client_id,skill,side,bucket,sha) VALUES(?,?,?,?,?,?)",
            [
                ("2026-07-01 00:00:00", client_id, "alpha", "main",
                 "recommended", "sha-alpha"),
                ("2026-07-02 00:00:00", client_id, "beta", "staging",
                 "ranked", "sha-beta"),
                ("2026-07-03 00:00:00", client_id, "gamma", "main",
                 "recommended", "sha-gamma"),
                ("2026-07-04 00:00:00", "another-client", "other", "main",
                 "recommended", "sha-other"),
            ],
        )
        conn.commit()

    boss = console_env["boss"]
    first = boss.get(
        "/api/v1/dashboard/admin/user/alice/recommendations",
        params={"offset": 0, "limit": 2},
    )
    assert first.status_code == 200
    payload = first.json()
    assert payload == {
        "user": "alice",
        "total": 3,
        "offset": 0,
        "limit": 2,
        "has_more": True,
        "exposures": [
            {"ts": "2026-07-03 00:00:00", "skill": "gamma",
             "side": "main", "bucket": "recommended", "sha": "sha-gamma"},
            {"ts": "2026-07-02 00:00:00", "skill": "beta",
             "side": "staging", "bucket": "ranked", "sha": "sha-beta"},
        ],
    }
    assert all("client_id" not in row for row in payload["exposures"])

    second = boss.get(
        "/api/v1/dashboard/admin/user/alice/recommendations",
        params={"offset": 2, "limit": 2},
    ).json()
    assert second["total"] == 3
    assert second["has_more"] is False
    assert [row["skill"] for row in second["exposures"]] == ["alpha"]
    assert boss.get(
        "/api/v1/dashboard/admin/user/alice/recommendations",
        params={"limit": 0},
    ).status_code == 422


def test_admin_ingest_control_is_authorized_idempotent_and_syncs_watch_dir(
    console_env,
):
    alice = console_env["alice"]
    boss = console_env["boss"]
    registry = console_env["registry"]
    db = console_env["db"]
    client_id = registry.find_by_user_name("alice")
    sessions_dir = (
        team_context().traj_root
        / "clients"
        / registry.dir_name_for(client_id)
        / "sessions"
    )
    sessions_dir.mkdir(parents=True)
    R.register_dir(
        sessions_dir,
        label="alice",
        ecosystem="team_client",
        db_path=db,
    )
    with R.get_connection(db) as conn:
        conn.execute(
            "INSERT INTO trajectories("
            "watch_dir_id,filename,status,tasks_extracted,user_key"
            ") VALUES("
            "(SELECT id FROM watch_dirs WHERE path=?),?,?,?,?"
            ")",
            (str(sessions_dir.resolve()), "done.md", "done", 3, "alice"),
        )
        conn.execute(
            "INSERT INTO trajectories("
            "watch_dir_id,filename,status,tasks_extracted,user_key"
            ") VALUES("
            "(SELECT id FROM watch_dirs WHERE path=?),?,?,?,?"
            ")",
            (
                str(sessions_dir.resolve()),
                "pending.md",
                "discovered",
                0,
                "alice",
            ),
        )
        conn.commit()
    endpoint = f"/api/v1/dashboard/admin/client/{client_id}/ingest"

    assert (
        alice.get("/api/v1/dashboard/my/contributions").json()["steps"]["trajs"]
        == 2
    )
    assert alice.put(
        endpoint, json={"paused": True, "reason": "quality review"},
    ).status_code == 403

    first = boss.put(
        endpoint, json={"paused": True, "reason": "quality review"},
    )
    assert first.status_code == 200
    body = first.json()
    assert body["ingest_paused"] is True
    assert body["ingest_paused_by"] == "boss"
    assert body["ingest_pause_reason"] == "quality review"
    assert body["auto_index"] is False
    paused_at = body["ingest_paused_at"]
    assert R.get_watch_dir(sessions_dir, db_path=db)["auto_index"] == 0
    from xskill.recommend.profile_dirty import list_dirty_profiles
    dirty = list_dirty_profiles(db_path=db)
    assert [(row["user_key"], row["generation"]) for row in dirty] == [
        ("alice", 1),
    ]
    paused_contributions = alice.get(
        "/api/v1/dashboard/my/contributions"
    ).json()["steps"]
    assert paused_contributions["trajs"] == 1
    assert paused_contributions["atoms"] == 3

    repeated = boss.put(
        endpoint, json={"paused": True, "reason": "different"},
    )
    assert repeated.status_code == 200
    assert repeated.json()["ingest_paused_at"] == paused_at
    assert repeated.json()["ingest_pause_reason"] == "quality review"
    assert list_dirty_profiles(db_path=db)[0]["generation"] == 1

    matrix = boss.get("/api/v1/dashboard/admin/users-matrix").json()
    user_row = next(row for row in matrix["users"] if row["client_id"] == client_id)
    assert user_row["ingest_paused"] is True
    assert user_row["ingest_pause_reason"] == "quality review"

    resumed = boss.put(endpoint, json={"paused": False})
    assert resumed.status_code == 200
    assert resumed.json()["ingest_paused"] is False
    assert resumed.json()["ingest_paused_at"] == ""
    assert resumed.json()["auto_index"] is True
    assert R.get_watch_dir(sessions_dir, db_path=db)["auto_index"] == 1
    assert list_dirty_profiles(db_path=db)[0]["generation"] == 2
    assert (
        alice.get("/api/v1/dashboard/my/contributions").json()["steps"]["trajs"]
        == 2
    )

    unknown = boss.put(
        "/api/v1/dashboard/admin/client/missing/ingest",
        json={"paused": True},
    )
    assert unknown.status_code == 404


# ── 2.9 设置页 ──────────────────────────────────────────────────────

def test_config_validate_and_reload(console_env, tmp_path, monkeypatch):
    import xskill.config as C
    from xskill.api import app as app_mod
    cfgp = tmp_path / "config.yaml"
    cfgp.write_text("skill_dir: /tmp/s\nllm:\n  base_url: http://x/v1\n",
                    encoding="utf-8")
    monkeypatch.setattr(C, "CONFIG_PATH", cfgp)
    monkeypatch.setattr(app_mod, "_config",
                        {"skill_dir": "/tmp/s",
                         "llm": {"base_url": "http://x/v1"}})
    boss = console_env["boss"]
    # 坏 YAML → 400,不落盘不生效
    before = cfgp.read_text()
    r = boss.post("/api/v1/dashboard/admin/config/reload",
                  json={"raw": "llm: [broken"})
    assert r.status_code == 400 and cfgp.read_text() == before
    # canary 段热生效,llm 段标注需重启
    new = ("skill_dir: /tmp/s\nllm:\n  base_url: http://y/v1\n"
           "canary:\n  probability: 0.5\n")
    r = boss.post("/api/v1/dashboard/admin/config/reload", json={"raw": new})
    body = r.json()
    assert r.status_code == 200
    assert "canary" in body["hot_reloaded"]
    assert "llm" in body["needs_restart"]
    assert app_mod._config["canary"]["probability"] == 0.5


def test_reload_slots_only_change_is_hot_not_restart(console_env, tmp_path, monkeypatch):
    """只改 team.server 的槽位子键 = 现取即生效 → 不该被标成"需重启"。

    回归:changed 是顶层 key 粒度,改 team.server.skill_slots 会算出
    changed=["team"] → needs_restart=["team"],把已经热的改动误标成要重启。
    """
    import xskill.config as C
    from xskill.api import app as app_mod
    cfgp = tmp_path / "config.yaml"
    base = ("skill_dir: /tmp/s\nteam:\n  server:\n"
            "    skill_slots: 100\n    ranked_slots: 80\n")
    cfgp.write_text(base, encoding="utf-8")
    monkeypatch.setattr(C, "CONFIG_PATH", cfgp)
    monkeypatch.setattr(app_mod, "_config", {
        "skill_dir": "/tmp/s",
        "team": {"server": {"skill_slots": 100, "ranked_slots": 80}},
    })
    boss = console_env["boss"]

    # 只动槽位 → hot,不需重启
    new = ("skill_dir: /tmp/s\nteam:\n  server:\n"
           "    skill_slots: 42\n    ranked_slots: 40\n")
    r = boss.post("/api/v1/dashboard/admin/config/reload", json={"raw": new})
    body = r.json()
    assert r.status_code == 200
    assert "team" not in body["needs_restart"], "槽位是热的,不该要求重启"
    assert "team" in body["hot_reloaded"]
    assert app_mod._config["team"]["server"]["skill_slots"] == 42


def test_reload_other_team_key_still_needs_restart(console_env, tmp_path, monkeypatch):
    """碰了 team 下的非热子键(进程级接线)→ 仍必须标注需重启。"""
    import xskill.config as C
    from xskill.api import app as app_mod
    cfgp = tmp_path / "config.yaml"
    cfgp.write_text("skill_dir: /tmp/s\nteam:\n  server:\n    skill_slots: 100\n",
                    encoding="utf-8")
    monkeypatch.setattr(C, "CONFIG_PATH", cfgp)
    monkeypatch.setattr(app_mod, "_config", {
        "skill_dir": "/tmp/s",
        "team": {"server": {"skill_slots": 100}},
    })
    boss = console_env["boss"]

    new = ("skill_dir: /tmp/s\nteam:\n  server:\n    skill_slots: 100\n"
           "    allow_anonymous_user: false\n")
    r = boss.post("/api/v1/dashboard/admin/config/reload", json={"raw": new})
    body = r.json()
    assert r.status_code == 200
    assert "team" in body["needs_restart"], "非热子键改动必须提示重启"


def test_reload_rejects_invalid_slots_without_persisting(console_env, tmp_path, monkeypatch):
    """槽位是热生效的,非法值必须落盘前就拒(不存在"部分生效")。"""
    import xskill.config as C
    from xskill.api import app as app_mod
    cfgp = tmp_path / "config.yaml"
    base = "skill_dir: /tmp/s\nteam:\n  server:\n    skill_slots: 100\n"
    cfgp.write_text(base, encoding="utf-8")
    monkeypatch.setattr(C, "CONFIG_PATH", cfgp)
    monkeypatch.setattr(app_mod, "_config", {
        "skill_dir": "/tmp/s", "team": {"server": {"skill_slots": 100}},
    })
    boss = console_env["boss"]

    bad = "skill_dir: /tmp/s\nteam:\n  server:\n    skill_slots: -5\n"
    r = boss.post("/api/v1/dashboard/admin/config/reload", json={"raw": bad})
    assert r.status_code == 400
    assert cfgp.read_text() == base                      # 没落盘
    assert app_mod._config["team"]["server"]["skill_slots"] == 100   # 没生效


def test_reload_bare_team_key_is_400_not_500(console_env, tmp_path, monkeypatch):
    """回归:光杆 `team:`(值为 None)曾让 team_server_slots_config 抛
    AttributeError,穿透 except ValueError → 500。必须是干净的 400 或被接受,
    绝不 500。"""
    import xskill.config as C
    from xskill.api import app as app_mod
    cfgp = tmp_path / "config.yaml"
    cfgp.write_text("skill_dir: /tmp/s\n", encoding="utf-8")
    monkeypatch.setattr(C, "CONFIG_PATH", cfgp)
    monkeypatch.setattr(app_mod, "_config", {"skill_dir": "/tmp/s"})
    boss = console_env["boss"]

    # 光杆 team: = 没配 → 走默认值,应被接受
    r = boss.post("/api/v1/dashboard/admin/config/reload",
                  json={"raw": "skill_dir: /tmp/s\nteam:\n"})
    assert r.status_code == 200, f"光杆 team: 不该报错,got {r.status_code}"

    # 畸形 team(非 mapping)→ 干净 400,不是 500
    r = boss.post("/api/v1/dashboard/admin/config/reload",
                  json={"raw": "skill_dir: /tmp/s\nteam: foo\n"})
    assert r.status_code == 400, f"畸形 team 应 400(带原因),got {r.status_code}"


# ── 控制面重算的短时缓存（10k skill 库下面板转圈的根因） ─────────────

def _write_ux_score(skill_path: Path, skill_name: str, atom_id: str,
                    scored_at: str, score: int = 8) -> None:
    import json
    with (skill_path / ".ux_scores.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({
            "skill_name": skill_name, "side": "main", "commit_sha": "sha1",
            "score": score, "scored_at": scored_at, "atom_id": atom_id}) + "\n")


def _seed_traj(db: Path, filename: str, user_key: str) -> None:
    with R.pooled_connection(db) as conn:
        conn.execute(
            "INSERT INTO watch_dirs(id,path,label,ecosystem)"
            " VALUES(1,'/tc','client-a','team_client')"
            " ON CONFLICT(id) DO NOTHING")
        conn.execute(
            "INSERT INTO trajectories(watch_dir_id,filename,status,user_key)"
            " VALUES(1,?,'done',?)", (filename, user_key))
        conn.commit()


def _clear_console_caches() -> None:
    import xskill.dashboard.console as console_module
    import xskill.dashboard.metrics as dashboard_metrics
    console_module._reco_trigger_cache.clear()
    dashboard_metrics._usage_records_cache.clear()
    dashboard_metrics._skills_catalog_cache.clear()


def test_reco_trigger_matrix_computed_once_across_requests(
        console_env, monkeypatch):
    """/my/reco-trigger(每用户一次) + /admin/users-matrix 共用一次全量矩阵。"""
    import xskill.dashboard.metrics as dashboard_metrics
    db = console_env["db"]
    cid = console_env["registry"].find_by_user_name("alice")
    R.record_recommendation(client_id=cid, skill="alpha", side="main",
                            bucket="ranked", sha="s1", db_path=db)
    _seed_traj(db, "traj0.md", "alice")
    _write_ux_score(console_env["skills"] / "alpha", "alpha",
                    "atom_traj0_0001", "2026-07-01T00:00:00")
    _clear_console_caches()
    # 断言「5 次请求只算一次」依赖 5 次请求都落在缓存 TTL（5 s）内。Windows
    # runner 比 Linux 慢约 6 倍，5 个 TestClient 请求 + 首次全量构建偶发跨过
    # 5 s，缓存到期后重建第二次（CI 见 assert 2 == 1）。测试关心的是「同一
    # 波次复用」，不是 TTL 长度——把 TTL 拉大，去掉对机器速度的隐含依赖。
    import xskill.dashboard.console as console_module
    monkeypatch.setattr(console_module._reco_trigger_cache, "ttl_seconds", 300.0)
    monkeypatch.setattr(dashboard_metrics._usage_records_cache, "ttl_seconds", 300.0)

    builds: list[int] = []
    real_load = dashboard_metrics.load_usage_records

    def counting_load(skill_dir):
        builds.append(1)
        return real_load(skill_dir)

    monkeypatch.setattr(dashboard_metrics, "load_usage_records", counting_load)

    alice, boss = console_env["alice"], console_env["boss"]
    rows = [alice.get("/api/v1/dashboard/my/reco-trigger").json() for _ in range(3)]
    matrices = [boss.get("/api/v1/dashboard/admin/users-matrix").json()
                for _ in range(2)]

    # 5 次请求只算了一次矩阵（旧实现:每次请求全量重算 + 全库读 .ux_scores.jsonl）
    assert len(builds) == 1
    # 口径不变：曝光 1 / 触发 1，/my 只拿自己那一行
    assert all(row == rows[0] for row in rows)
    assert rows[0]["user"] == "alice"
    assert rows[0]["rows"] == [{
        "skill": "alpha", "exposures": 1, "triggers": 1, "rate": 1.0,
        "last_trigger": "2026-07-01T00:00:00", "verdict": "正常"}]
    alice_row = next(u for u in matrices[0]["users"] if u["user"] == "alice")
    assert (alice_row["exposures"], alice_row["triggers"], alice_row["rate"]) == (1, 1, 1.0)


def test_reco_trigger_rows_are_independent_copies(console_env):
    """调用方改自己那份改不到缓存里的矩阵。"""
    from xskill.dashboard.console import reco_trigger_for_users
    db = console_env["db"]
    cid = console_env["registry"].find_by_user_name("alice")
    R.record_recommendation(client_id=cid, skill="alpha", side="main",
                            bucket="ranked", sha="s1", db_path=db)
    _clear_console_caches()

    kwargs = {"db_path": db, "skill_dir": console_env["skills"],
              "registry": console_env["registry"]}
    first = reco_trigger_for_users(**kwargs)
    first["alice"][0]["exposures"] = 999
    first["alice"].append({"skill": "injected"})
    first["intruder"] = []

    second = reco_trigger_for_users(**kwargs)
    assert set(second) == {"alice"}
    assert len(second["alice"]) == 1
    assert second["alice"][0]["exposures"] == 1


def test_users_matrix_reads_prefs_in_one_query(console_env, monkeypatch):
    """用户 × 配置矩阵不再逐用户查库(N+1)，且 pinned/blocked 口径不变：
    只算用户自己的行，全局 pin 单独出 global_pinned。"""
    import xskill.dashboard.console as console_module
    db = console_env["db"]
    R.set_skill_pref(user_key="alice", skill_name="alpha", pref="pinned",
                     set_by="alice", db_path=db)
    R.set_skill_pref(user_key="alice", skill_name="gamma", pref="blocked",
                     set_by="alice", db_path=db)
    R.set_skill_pref(user_key=R.GLOBAL_PREF_KEY, skill_name="beta",
                     pref="pinned", set_by="boss", db_path=db)
    _clear_console_caches()

    def forbidden_prefs_for(*args, **kwargs):
        raise AssertionError("users-matrix 不该逐用户查 prefs_for")

    monkeypatch.setattr(console_module, "prefs_for", forbidden_prefs_for)

    matrix = console_env["boss"].get(
        "/api/v1/dashboard/admin/users-matrix").json()
    alice_row = next(u for u in matrix["users"] if u["user"] == "alice")
    assert (alice_row["pinned"], alice_row["blocked"]) == (1, 1)  # 不含全局 pin
    assert matrix["global_pinned"] == ["beta"]
    assert alice_row["stale_advice"] == []
    assert alice_row["rate"] is None


def test_admin_skills_uses_cached_catalog_and_keeps_skillrepo_scope(
        console_env, monkeypatch):
    """技能生命周期表：状态取投影表（冷启动一次 backfill），
    但列出的 skill 集合仍与 SkillRepo 完全一致。"""
    import xskill.skill.catalog_store as catalog_store
    db = console_env["db"]
    skills = console_env["skills"]
    monkeypatch.setattr(
        "xskill.config.get_registry_db_path",
        lambda: db,
    )
    monkeypatch.setattr(
        "xskill.pipeline.registry.get_registry_db_path",
        lambda: db,
    )
    # beta 起灰度 → canary；gamma 下线 → retired；alpha 近 30 日有使用
    _git(["checkout", "-q", "-b", "staging"], skills / "beta")
    R.retire_skill(skill_name="gamma", set_by="boss", db_path=db)
    import datetime as dt
    recent = dt.datetime.now(dt.timezone.utc).isoformat()
    _write_ux_score(skills / "alpha", "alpha", "atom_traj0_0001", recent)
    _write_ux_score(skills / "alpha", "alpha", "atom_traj0_0002", recent)
    _write_ux_score(skills / "alpha", "alpha", "atom_traj0_0003",
                    "2020-01-01T00:00:00")           # 30 日外不计
    # SkillRepo 不认的目录：references / 没有 SKILL.md 的目录
    (skills / "references").mkdir()
    (skills / "references" / "SKILL.md").write_text("# ref\n", encoding="utf-8")
    (skills / "loose-dir").mkdir()
    (skills / "loose-dir" / "notes.md").write_text("x\n", encoding="utf-8")
    _clear_console_caches()

    scans: list[int] = []
    real_scan = catalog_store.scan_skills_catalog

    def counting_scan(*args, **kwargs):
        scans.append(1)
        return real_scan(*args, **kwargs)

    monkeypatch.setattr(catalog_store, "scan_skills_catalog", counting_scan)

    boss = console_env["boss"]
    first = boss.get("/api/v1/dashboard/admin/skills").json()
    second = boss.get("/api/v1/dashboard/admin/skills").json()

    assert len(scans) == 1                    # 两次请求共用一次 backfill 扫盘
    assert first == second
    assert first["skills"] == [
        {"name": "alpha", "state": "active", "usage_30d": 2},
        {"name": "beta", "state": "canary", "usage_30d": 0},
        {"name": "gamma", "state": "retired", "usage_30d": 0},
    ]


def test_user_cannot_patch_pipeline_pools(console_env):
    alice = console_env["alice"]
    assert alice.patch(
        "/api/v1/dashboard/admin/pipeline/pools",
        json={"pool": "edit", "workers": 2},
    ).status_code == 403


def test_admin_patch_pipeline_pools_writes_yaml(console_env, tmp_path, monkeypatch):
    import xskill.config as C
    from xskill.api import app as app_mod
    cfgp = tmp_path / "config.yaml"
    cfgp.write_text(
        "llm:\n  base_url: http://x/v1\n"
        "agent_worker:\n  pools:\n    edit:\n      workers: 4\n      llm_weight: 1\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(C, "CONFIG_PATH", cfgp)
    monkeypatch.setattr(app_mod, "_config", {
        "llm": {"base_url": "http://x/v1"},
        "agent_worker": {"pools": {"edit": {"workers": 4, "llm_weight": 1}}},
    })
    boss = console_env["boss"]
    r = boss.patch(
        "/api/v1/dashboard/admin/pipeline/pools",
        json={"pool": "edit", "workers": 8, "llm_weight": 2},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["needs_restart"] == []
    text = cfgp.read_text(encoding="utf-8")
    assert "workers: 8" in text
    assert "llm_weight: 2" in text
    assert app_mod._config["agent_worker"]["pools"]["edit"]["workers"] == 8


def test_admin_patch_pipeline_pools_rejects_zero(console_env, tmp_path, monkeypatch):
    import xskill.config as C
    cfgp = tmp_path / "config.yaml"
    cfgp.write_text(
        "llm:\n  base_url: http://x/v1\n"
        "agent_worker:\n  pools:\n    edit:\n      workers: 4\n      llm_weight: 1\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(C, "CONFIG_PATH", cfgp)
    boss = console_env["boss"]
    r = boss.patch(
        "/api/v1/dashboard/admin/pipeline/pools",
        json={"pool": "edit", "workers": 0},
    )
    assert r.status_code == 422
    assert "workers: 4" in cfgp.read_text(encoding="utf-8")


def test_reload_pool_seats_only_is_hot_not_restart(console_env, tmp_path, monkeypatch):
    import xskill.config as C
    from xskill.api import app as app_mod
    cfgp = tmp_path / "config.yaml"
    base = (
        "llm:\n  base_url: http://x/v1\n"
        "agent_worker:\n  pools:\n    edit:\n      workers: 4\n      llm_weight: 1\n"
    )
    cfgp.write_text(base, encoding="utf-8")
    monkeypatch.setattr(C, "CONFIG_PATH", cfgp)
    monkeypatch.setattr(app_mod, "_config", {
        "llm": {"base_url": "http://x/v1"},
        "agent_worker": {"pools": {"edit": {"workers": 4, "llm_weight": 1}}},
    })
    new = (
        "llm:\n  base_url: http://x/v1\n"
        "agent_worker:\n  pools:\n    edit:\n      workers: 2\n      llm_weight: 3\n"
    )
    r = console_env["boss"].post(
        "/api/v1/dashboard/admin/config/reload", json={"raw": new},
    )
    assert r.status_code == 200
    body = r.json()
    assert "agent_worker" not in body["needs_restart"]
    assert "agent_worker" in body["hot_reloaded"]
