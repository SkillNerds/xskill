"""test_client_identity.py — §2 --name 稳定身份 + allow_anonymous 闸门

TDD: 确定性 client_id、跨设备同名、--name 优先于指纹、匿名回退、allow_anonymous 闸门。
"""
from __future__ import annotations

import hashlib

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from xskill.team.server import api as server_api
from xskill.team.server.client_registry import (
    ClientRegistry,
    client_id_from_name,
)


# ── client_id_from_name / ClientRegistry ─────────────────────────

class TestClientIdFromName:
    def test_deterministic(self):
        assert client_id_from_name("alice") == client_id_from_name("alice")

    def test_different_names_different_ids(self):
        assert client_id_from_name("alice") != client_id_from_name("bob")

    def test_normalizes_case_and_whitespace(self):
        assert client_id_from_name("Alice") == client_id_from_name("alice")
        assert client_id_from_name("  alice  ") == client_id_from_name("alice")
        assert client_id_from_name("Alice") == client_id_from_name("  ALICE  ")

    def test_matches_spec_formula(self):
        norm = "alice"
        expected = hashlib.sha256(("name:" + norm).encode("utf-8")).hexdigest()[:16]
        assert client_id_from_name("Alice") == expected

    def test_empty_or_none_raises(self):
        with pytest.raises(ValueError):
            client_id_from_name("")
        with pytest.raises(ValueError):
            client_id_from_name("   ")
        with pytest.raises((ValueError, TypeError)):
            client_id_from_name(None)  # type: ignore[arg-type]


class TestRegistryUserName:
    def test_same_name_returns_same_id(self, tmp_path):
        reg = ClientRegistry(tmp_path / "c.db")
        a = reg.register(user_name="alice", hostname="h1")
        b = reg.register(user_name="alice", hostname="h2")
        assert a == b
        assert reg.exists(a)

    def test_name_precedence_over_claimed(self, tmp_path):
        reg = ClientRegistry(tmp_path / "c.db")
        # 先注册一个匿名 uuid 身份
        anon = reg.register(label="x", hostname="h")
        # 带 --name + 一个不相关的 claimed_client_id → 应走 name 派生，忽略 claimed
        named = reg.register(user_name="alice", hostname="h", claimed_client_id=anon)
        assert named != anon
        assert named == client_id_from_name("alice")

    def test_anonymous_no_name_uses_existing_logic(self, tmp_path):
        reg = ClientRegistry(tmp_path / "c.db")
        a = reg.register(label="x", hostname="h1")
        b = reg.register(label="y", hostname="h2")
        # 两个不同指纹的匿名 → 两个不同 uuid
        assert a != b

    def test_name_touches_last_seen_on_revisit(self, tmp_path):
        reg = ClientRegistry(tmp_path / "c.db")
        cid = reg.register(user_name="alice", hostname="h1")
        before = reg.get(cid)["last_seen"]
        cid2 = reg.register(user_name="alice", hostname="h2")
        assert cid2 == cid
        assert reg.get(cid)["last_seen"] >= before


# ── /register 端点 + allow_anonymous 闸门 ─────────────────────────

def _make_client(tmp_path, *, allow_anonymous: bool):
    reg = ClientRegistry(tmp_path / "clients.db")
    server_api.init_team_context(
        join_token="secret-token",
        client_registry=reg,
        skill_dir=tmp_path / "skill",
        traj_root=tmp_path / "traj",
        probability=0.2, ranked_slots=80, total_slots=100,
        register_dir=lambda path, label: None,
        allow_anonymous_user=allow_anonymous,
    )
    app = FastAPI()
    app.include_router(server_api.router)
    return TestClient(app)


class TestRegisterEndpoint:
    def test_name_returns_deterministic_id(self, tmp_path):
        c = _make_client(tmp_path, allow_anonymous=True)
        r1 = c.post("/api/v1/team/register",
                    json={"token": "secret-token", "user_name": "alice"})
        r2 = c.post("/api/v1/team/register",
                    json={"token": "secret-token", "user_name": "alice",
                          "hostname": "other-device"})
        assert r1.status_code == 200 and r2.status_code == 200
        assert r1.json()["client_id"] == r2.json()["client_id"]
        assert r1.json()["client_id"] == client_id_from_name("alice")

    def test_anonymous_allowed_by_default(self, tmp_path):
        c = _make_client(tmp_path, allow_anonymous=True)
        r = c.post("/api/v1/team/register",
                   json={"token": "secret-token", "client_label": "x", "hostname": "h"})
        assert r.status_code == 200
        assert "client_id" in r.json()

    def test_anonymous_rejected_when_disabled(self, tmp_path):
        c = _make_client(tmp_path, allow_anonymous=False)
        r = c.post("/api/v1/team/register",
                   json={"token": "secret-token", "client_label": "x", "hostname": "h"})
        assert r.status_code == 403
        assert "anonymous" in r.json()["detail"].lower()

    def test_named_allowed_when_anonymous_disabled(self, tmp_path):
        c = _make_client(tmp_path, allow_anonymous=False)
        r = c.post("/api/v1/team/register",
                   json={"token": "secret-token", "user_name": "alice"})
        assert r.status_code == 200
        assert r.json()["client_id"] == client_id_from_name("alice")

    def test_wrong_token_rejected(self, tmp_path):
        c = _make_client(tmp_path, allow_anonymous=True)
        r = c.post("/api/v1/team/register",
                   json={"token": "wrong", "user_name": "alice"})
        assert r.status_code == 401
