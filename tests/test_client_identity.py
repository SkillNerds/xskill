"""test_client_identity.py — §2 --name 稳定身份 + allow_anonymous 闸门

TDD: 确定性 client_id、跨设备同名、--name 优先于指纹、匿名回退、allow_anonymous 闸门。
"""
from __future__ import annotations

import hashlib
import sqlite3

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


# ── user_name 明文持久化 + find_by_user_name + 跨重启迁移 ──────────

class TestUserNameColumn:
    def test_user_name_persisted_in_get_and_list(self, tmp_path):
        reg = ClientRegistry(tmp_path / "c.db")
        cid = reg.register(user_name="alice", hostname="h")
        row = reg.get(cid)
        assert row is not None
        assert row["user_name"] == "alice"
        listed = reg.list()
        assert len(listed) == 1
        assert listed[0]["user_name"] == "alice"

    def test_user_name_normalized_on_persist(self, tmp_path):
        reg = ClientRegistry(tmp_path / "c.db")
        cid = reg.register(user_name="  Alice  ", hostname="h")
        assert reg.get(cid)["user_name"] == "alice"

    def test_anonymous_register_has_empty_user_name(self, tmp_path):
        reg = ClientRegistry(tmp_path / "c.db")
        cid = reg.register(label="x", hostname="h")
        assert reg.get(cid)["user_name"] == ""

    def test_find_by_user_name_returns_client_id(self, tmp_path):
        reg = ClientRegistry(tmp_path / "c.db")
        cid = reg.register(user_name="alice", hostname="h")
        assert reg.find_by_user_name("alice") == cid
        # 规范化匹配：大小写/空白不影响反查
        assert reg.find_by_user_name("  Alice  ") == cid

    def test_find_by_user_name_none_when_absent(self, tmp_path):
        reg = ClientRegistry(tmp_path / "c.db")
        reg.register(user_name="alice", hostname="h")
        assert reg.find_by_user_name("bob") is None

    def test_find_by_user_name_rejects_empty(self, tmp_path):
        reg = ClientRegistry(tmp_path / "c.db")
        with pytest.raises(ValueError):
            reg.find_by_user_name("")

    def test_revisit_updates_user_name_column(self, tmp_path):
        reg = ClientRegistry(tmp_path / "c.db")
        cid = reg.register(user_name="alice", hostname="h1")
        # 同名重连（不同 hostname）→ user_name 列仍持明文
        reg.register(user_name="alice", hostname="h2")
        assert reg.get(cid)["user_name"] == "alice"
        assert reg.get(cid)["hostname"] == "h2"

    def test_migration_adds_user_name_column_on_reopen(self, tmp_path):
        """老 db（无 user_name 列）重开 ClientRegistry → 幂等 ALTER 补列。"""
        db = tmp_path / "c.db"
        conn = sqlite3.connect(str(db))
        conn.execute(
            "CREATE TABLE clients ("
            " client_id TEXT PRIMARY KEY, label TEXT DEFAULT '',"
            " hostname TEXT DEFAULT '', joined_at TEXT NOT NULL,"
            " last_seen TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO clients (client_id, label, hostname, joined_at, last_seen)"
            " VALUES ('oldhash', 'h', 'host',"
            " '2024-01-01T00:00:00+00:00', '2024-01-01T00:00:00+00:00')"
        )
        conn.commit()
        conn.close()

        # 重开 → 迁移补 user_name 列，老行默认 ''
        reg = ClientRegistry(db)
        old_row = reg.get("oldhash")
        assert old_row is not None
        assert old_row["user_name"] == ""

        # 迁移后再注册带 name 的 client 正常写入明文
        cid = reg.register(user_name="alice", hostname="h")
        assert reg.get(cid)["user_name"] == "alice"
        assert reg.find_by_user_name("Alice") == cid

        # 二次重开仍幂等（列已存在，ALTER 不再执行）
        reg2 = ClientRegistry(db)
        assert reg2.get(cid)["user_name"] == "alice"


# ── safe_dir_name / dir_name_for ────────────────────────────────

class TestSafeDirName:
    def test_alphanumeric_passes_through(self):
        from xskill.team.server.client_registry import safe_dir_name
        assert safe_dir_name("m00947023", "abc") == "m00947023"
        assert safe_dir_name("02020222", "abc") == "02020222"
        assert safe_dir_name("alice", "abc") == "alice"

    def test_special_chars_escaped(self):
        from xskill.team.server.client_registry import safe_dir_name
        assert safe_dir_name("alice/bob", "abc") == "alice_bob"
        assert safe_dir_name("a b", "abc") == "a_b"

    def test_anonymous_uses_client_id(self):
        from xskill.team.server.client_registry import safe_dir_name
        assert safe_dir_name(None, "7e8e2d7833a2eb0f") == "7e8e2d7833a2eb0f"
        assert safe_dir_name("", "abc") == "abc"

    def test_unsafe_rejected(self):
        from xskill.team.server.client_registry import safe_dir_name
        with pytest.raises(ValueError):
            safe_dir_name("..", "abc")
        with pytest.raises(ValueError):
            safe_dir_name("   ", "abc")


class TestDirNameFor:
    def test_named_user_gets_user_name_dir(self, tmp_path):
        from xskill.team.server.client_registry import ClientRegistry
        r = ClientRegistry(tmp_path / "c.db")
        cid = r.register(user_name="m00947023")
        assert r.dir_name_for(cid) == "m00947023"

    def test_anonymous_gets_client_id_dir(self, tmp_path):
        from xskill.team.server.client_registry import ClientRegistry
        r = ClientRegistry(tmp_path / "c.db")
        cid = r.register(label="x", hostname="h")
        assert r.dir_name_for(cid) == cid

    def test_unknown_client_raises(self, tmp_path):
        from xskill.team.server.client_registry import ClientRegistry
        r = ClientRegistry(tmp_path / "c.db")
        with pytest.raises(ValueError):
            r.dir_name_for("nonexistent")
