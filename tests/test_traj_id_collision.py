"""tests/test_traj_id_collision.py — issue #234：团队语料轨迹/原子 id 撞名。

四类确认过的覆盖口径（issue #234 讨论定稿）：

1. 中文用户名：成员标识可读部分为空时退化为 ``u_ + 8 位哈希``；
2. 同名用户跨设备：client_id 由用户名派生，成员标识跨设备一致，不产生
   第二个身份；
3. 新旧命名混批：8 位旧名文件与 16 位新名并存时读取侧都认，续写不产生
   重复文件；
4. 迁移一致性：``xskill tools migrate-traj-name`` 同步改 md / sidecar /
   原子目录 / 注册表 / candidates，且可回滚。

另附带两处修复的回归：内容一致的重复上传不改写文件（mtime 不动）、
多 store 同名原子内容一致按重复处理（内容不同才告警冲突）。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from xskill.ecosystems._shared import (
    legacy_short_sid,
    lookup_bridged_markdown,
    short_sid,
)
from xskill.team.server import api as server_api
from xskill.team.server.client_registry import (
    ClientRegistry,
    client_id_from_name,
    member_traj_tag,
)


# ─────────────────────────────────────────────────────────────────
# 成员标识
# ─────────────────────────────────────────────────────────────────

class TestMemberTrajTag:
    def test_readable_name_plus_hash8(self):
        cid = client_id_from_name("alice")
        assert member_traj_tag("alice", cid) == f"alice_{cid[:8]}"

    def test_chinese_only_name_falls_back_to_u(self):
        """覆盖口径 1：纯中文用户名可读部分为空 → ``u_`` + 8 位哈希。"""
        cid = client_id_from_name("小明")
        tag = member_traj_tag("小明", cid)
        assert tag == f"u_{cid[:8]}"

    def test_mixed_name_keeps_readable_part(self):
        cid = client_id_from_name("小明abc")
        assert member_traj_tag("小明abc", cid) == f"abc_{cid[:8]}"

    def test_anonymous_uses_u(self):
        assert member_traj_tag(None, "deadbeefcafebabe") == "u_deadbeef"

    def test_long_name_truncated_to_16(self):
        name = "a" * 40
        tag = member_traj_tag(name, "0" * 16)
        assert tag == "a" * 16 + "_" + "0" * 8

    def test_same_name_cross_device_same_tag(self):
        """覆盖口径 2：client_id 由用户名派生（sha256(name)[:16]），同名用户
        换设备注册得到同一 client_id → 同一成员标识，不会产生第二个身份。"""
        cid_a = client_id_from_name("bob")
        cid_b = client_id_from_name("  Bob ")  # 规范化：去空白、小写
        assert cid_a == cid_b
        assert member_traj_tag("bob", cid_a) == member_traj_tag("Bob", cid_b)

    def test_different_names_different_tags(self):
        t1 = member_traj_tag("alice", client_id_from_name("alice"))
        t2 = member_traj_tag("alicf", client_id_from_name("alicf"))
        assert t1 != t2


# ─────────────────────────────────────────────────────────────────
# 会话 id 尾段 16 位 + 新旧双键回退
# ─────────────────────────────────────────────────────────────────

class TestSidLength:
    def test_short_sid_keeps_16_chars(self):
        assert short_sid("ses_abc123def456ghi789") == "ses_abc123def456"

    def test_legacy_short_sid_is_old_8(self):
        assert legacy_short_sid("ses_abc123def456ghi789") == "ses_abc1"

    def test_prefixed_ids_now_distinguishable(self):
        """issue #234 核心：``ses_`` 前缀在 8 位下只剩 4 位有效字符。"""
        a = "ses_0f5d1111aaaaaaaa"
        b = "ses_0f5d2222bbbbbbbb"
        assert legacy_short_sid(a) == legacy_short_sid(b)  # 旧规则撞名
        assert short_sid(a) != short_sid(b)                # 新规则可区分

    def test_lookup_prefers_new_key(self, tmp_path):
        sid = "ses_abc123def456ghi789"
        new_md = tmp_path / f"traj_ng_p_{short_sid(sid)}.md"
        index = {short_sid(sid): new_md}
        assert lookup_bridged_markdown(index, sid) == new_md

    def test_lookup_falls_back_to_legacy_key(self, tmp_path):
        """覆盖口径 3：存量 8 位旧名文件仍被识别为「已桥接」，续写原地
        覆盖而不是再生成一份 16 位新名文件。"""
        sid = "ses_abc123def456ghi789"
        old_md = tmp_path / f"traj_ng_p_{legacy_short_sid(sid)}.md"
        index = {legacy_short_sid(sid): old_md}
        assert lookup_bridged_markdown(index, sid) == old_md

    def test_lookup_mixed_batch(self, tmp_path):
        """新旧命名混批：各自命中各自的文件，互不串。"""
        old_sid = "ses_old111122223333"
        new_sid = "ses_new111122223333"
        old_md = tmp_path / f"traj_ng_p_{legacy_short_sid(old_sid)}.md"
        new_md = tmp_path / f"traj_ng_p_{short_sid(new_sid)}.md"
        index = {
            legacy_short_sid(old_sid): old_md,
            short_sid(new_sid): new_md,
        }
        assert lookup_bridged_markdown(index, old_sid) == old_md
        assert lookup_bridged_markdown(index, new_sid) == new_md

    def test_lookup_miss_returns_none(self):
        assert lookup_bridged_markdown({}, "ses_whatever") is None


# ─────────────────────────────────────────────────────────────────
# 服务器上传：成员前缀
# ─────────────────────────────────────────────────────────────────

UPLOAD = "/api/v1/team/upload"
REGISTER = "/api/v1/team/register"


@pytest.fixture
def team_app(tmp_path):
    reg = ClientRegistry(tmp_path / "clients.db")
    server_api.init_team_context(
        join_token="tok", client_registry=reg,
        skill_dir=tmp_path / "server_skill",
        traj_root=tmp_path / "team_traj",
        register_dir=lambda p, l: None,
    )
    app = FastAPI()
    app.include_router(server_api.router)
    return app, tmp_path / "team_traj"


def _register(http, name=None):
    body = {"token": "tok", "client_label": "t", "hostname": "h"}
    if name:
        body["user_name"] = name
    r = http.post(REGISTER, json=body)
    assert r.status_code == 200, r.text
    return r.json()["client_id"]


def _upload(http, cid, traj_id, content):
    return http.post(
        UPLOAD,
        json={"trajectories": [{
            "traj_id": traj_id, "content": content,
            "sha256": hashlib.sha256(content.encode()).hexdigest(),
        }]},
        headers={"X-Xskill-Token": "tok", "X-Xskill-Client": cid},
    )


class TestUploadMemberPrefix:
    def test_stored_name_prefixed_response_echoes_original(self, team_app):
        app, traj_root = team_app
        http = TestClient(app)
        cid = _register(http, "alice")
        r = _upload(http, cid, "traj_ng_agentfs_ses_0f5d", "# body-a")
        assert r.json()["accepted"] == ["traj_ng_agentfs_ses_0f5d"]
        sessions = traj_root / "clients" / "alice" / "sessions"
        stored = sessions / f"traj_alice_{cid[:8]}_ng_agentfs_ses_0f5d.md"
        assert stored.is_file()
        # 客户端原始名不落盘
        assert not (sessions / "traj_ng_agentfs_ses_0f5d.md").is_file()

    def test_two_members_same_traj_id_do_not_collide(self, team_app):
        """issue #234 的现场：两位成员同名项目、会话 id 前缀相同——服务器
        落盘名带各自成员标识，语料合并后仍全局唯一。"""
        app, traj_root = team_app
        http = TestClient(app)
        cid_a = _register(http, "alice")
        cid_b = _register(http, "bob")
        for cid in (cid_a, cid_b):
            r = _upload(http, cid, "traj_ng_agentfs_ses_0f5d", f"# by {cid}")
            assert r.json()["accepted"] == ["traj_ng_agentfs_ses_0f5d"]
        stored = sorted(
            p.name for p in (traj_root / "clients").rglob("traj_*.md")
        )
        assert stored == [
            f"traj_alice_{cid_a[:8]}_ng_agentfs_ses_0f5d.md",
            f"traj_bob_{cid_b[:8]}_ng_agentfs_ses_0f5d.md",
        ]

    def test_chinese_user_gets_u_prefix(self, team_app):
        """覆盖口径 1（端到端）：中文用户名上传，落盘用 ``u_`` 退化前缀。"""
        app, traj_root = team_app
        http = TestClient(app)
        cid = _register(http, "小明")
        r = _upload(http, cid, "traj_cc_p_s1", "# hi")
        assert r.json()["accepted"] == ["traj_cc_p_s1"]
        hits = list((traj_root / "clients").rglob(
            f"traj_u_{cid[:8]}_cc_p_s1.md"))
        assert len(hits) == 1

    def test_already_prefixed_id_not_double_prefixed(self, team_app):
        app, traj_root = team_app
        http = TestClient(app)
        cid = _register(http, "alice")
        pre = f"traj_alice_{cid[:8]}_cc_p_s1"
        _upload(http, cid, pre, "# hi")
        hits = [p.name for p in (traj_root / "clients").rglob("traj_*.md")]
        assert hits == [f"{pre}.md"]

    def test_identical_reupload_does_not_rewrite(self, team_app):
        """附带修复：断线重传（内容一致）直接确认、不重写——重写会刷新
        mtime，让 server 端 watcher 把旧轨迹当新文件重新入库。"""
        app, traj_root = team_app
        http = TestClient(app)
        cid = _register(http, "alice")
        _upload(http, cid, "traj_cc_p_s1", "# same")
        stored = next((traj_root / "clients").rglob("traj_*.md"))
        before = stored.stat().st_mtime_ns
        r = _upload(http, cid, "traj_cc_p_s1", "# same")
        assert r.json()["accepted"] == ["traj_cc_p_s1"]
        assert stored.stat().st_mtime_ns == before

    def test_changed_reupload_still_rewrites(self, team_app):
        app, traj_root = team_app
        http = TestClient(app)
        cid = _register(http, "alice")
        _upload(http, cid, "traj_cc_p_s1", "# v1")
        _upload(http, cid, "traj_cc_p_s1", "# v2")
        stored = next((traj_root / "clients").rglob("traj_*.md"))
        assert stored.read_text(encoding="utf-8").rstrip() == "# v2"

    def test_legacy_unprefixed_identical_file_recognized(self, team_app):
        """覆盖口径 3（服务器侧）：前缀上线前落盘的旧名文件内容一致时视为
        已存储，不再写一份新名副本（新旧共存，改名交给迁移命令）。"""
        app, traj_root = team_app
        http = TestClient(app)
        cid = _register(http, "alice")
        sessions = traj_root / "clients" / "alice" / "sessions"
        sessions.mkdir(parents=True)
        (sessions / "traj_cc_p_s1.md").write_text("# old", encoding="utf-8")
        r = _upload(http, cid, "traj_cc_p_s1", "# old")
        assert r.json()["accepted"] == ["traj_cc_p_s1"]
        assert [p.name for p in sessions.glob("traj_*.md")] == [
            "traj_cc_p_s1.md",
        ]


# ─────────────────────────────────────────────────────────────────
# 迁移命令
# ─────────────────────────────────────────────────────────────────

def _make_bridged_traj(bridge: Path, sid: str, project="proj") -> str:
    """按旧 8 位规则造一条已桥接轨迹（md + sidecar + 原子目录）。"""
    traj_id = f"traj_ng_{project}_{legacy_short_sid(sid)}"
    bridge.mkdir(parents=True, exist_ok=True)
    (bridge / f"{traj_id}.md").write_text("# t\nline2\n", encoding="utf-8")
    (bridge / f"{traj_id}.json").write_text(
        json.dumps({"session_id": sid}), encoding="utf-8")
    tasks = bridge / traj_id / "tasks"
    tasks.mkdir(parents=True)
    atom_id = f"atom_{traj_id}_0001"
    (tasks / f"{atom_id}.json").write_text(json.dumps({
        "atom_id": atom_id, "traj_id": traj_id,
        "offset_start": 1, "offset_end": 3,
        "intent": "i", "summary": "s",
        "pre_atom_id": None, "post_atom_id": f"atom_{traj_id}_0002",
    }), encoding="utf-8")
    return traj_id


class TestMigrateTrajName:
    def test_migrates_files_atoms_db_and_candidates(self, tmp_path):
        """覆盖口径 4：md / sidecar / 原子目录与内容 / 注册表 / candidates
        全部同步改名，且留有备份。"""
        from xskill.tools import migrate_traj_names

        home = tmp_path / "xskill_home"
        bridge = home / "ng_sessions"
        sid = "ses_abc123def456ghi789"
        old_id = _make_bridged_traj(bridge, sid)
        new_id = f"traj_ng_proj_{short_sid(sid)}"
        # 注册表
        db = home / "registry.db"
        home.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(db) as conn:
            conn.execute(
                "CREATE TABLE watch_dirs (id INTEGER PRIMARY KEY, path TEXT)")
            conn.execute(
                "CREATE TABLE trajectories (id INTEGER PRIMARY KEY, "
                "watch_dir_id INTEGER, filename TEXT)")
            conn.execute("INSERT INTO watch_dirs (id, path) VALUES (1, ?)",
                         (str(bridge),))
            conn.execute(
                "INSERT INTO trajectories (watch_dir_id, filename) "
                "VALUES (1, ?)", (f"{old_id}.md",))
        # candidates 引用
        skill_dir = tmp_path / "skill"
        (skill_dir / "fix-foo").mkdir(parents=True)
        (skill_dir / "fix-foo" / ".candidates.yml").write_text(
            f"candidates:\n- atom_id: atom_{old_id}_0001\n",
            encoding="utf-8")

        report = migrate_traj_names(
            xskill_home=home, registry_db=db, skill_dir=skill_dir)
        assert report.renamed == 1
        assert (bridge / f"{new_id}.md").is_file()
        assert (bridge / f"{new_id}.json").is_file()
        assert not (bridge / f"{old_id}.md").exists()
        new_atom = bridge / new_id / "tasks" / f"atom_{new_id}_0001.json"
        assert new_atom.is_file()
        payload = json.loads(new_atom.read_text(encoding="utf-8"))
        assert payload["atom_id"] == f"atom_{new_id}_0001"
        assert payload["traj_id"] == new_id
        assert payload["post_atom_id"] == f"atom_{new_id}_0002"
        with sqlite3.connect(db) as conn:
            rows = conn.execute(
                "SELECT filename FROM trajectories").fetchall()
        assert rows == [(f"{new_id}.md",)]
        cand = (skill_dir / "fix-foo" / ".candidates.yml").read_text(
            encoding="utf-8")
        assert f"atom_{new_id}_0001" in cand
        # 注意旧 id 是新 id 的前缀（尾段 8 → 16 只是变长），只能断言完整
        # 旧原子 id 不再出现
        assert f"atom_{old_id}_0001" not in cand
        assert report.backup_dir is not None
        assert (report.backup_dir / "manifest.json").is_file()

    def test_dry_run_touches_nothing(self, tmp_path):
        from xskill.tools import migrate_traj_names

        home = tmp_path / "xskill_home"
        bridge = home / "ng_sessions"
        old_id = _make_bridged_traj(bridge, "ses_abc123def456ghi789")
        report = migrate_traj_names(xskill_home=home, dry_run=True)
        assert len(report.planned) == 1
        assert report.renamed == 0
        assert (bridge / f"{old_id}.md").is_file()

    def test_new_named_file_not_replanned(self, tmp_path):
        """幂等：已迁移（或新代码生成）的 16 位名不再进计划。"""
        from xskill.tools import migrate_traj_names

        home = tmp_path / "xskill_home"
        bridge = home / "ng_sessions"
        _make_bridged_traj(bridge, "ses_abc123def456ghi789")
        migrate_traj_names(xskill_home=home)
        report2 = migrate_traj_names(xskill_home=home, dry_run=True)
        assert report2.planned == []

    def test_target_exists_skips_without_overwrite(self, tmp_path):
        from xskill.tools import migrate_traj_names

        home = tmp_path / "xskill_home"
        bridge = home / "ng_sessions"
        sid = "ses_abc123def456ghi789"
        _make_bridged_traj(bridge, sid)
        blocker = bridge / f"traj_ng_proj_{short_sid(sid)}.md"
        blocker.write_text("# occupied", encoding="utf-8")
        report = migrate_traj_names(xskill_home=home)
        assert report.renamed == 0
        assert blocker.read_text(encoding="utf-8") == "# occupied"

    def test_rollback_restores_everything(self, tmp_path):
        """覆盖口径 4（回滚半程）：迁移后按备份恢复原名与原内容。"""
        from xskill.tools import migrate_traj_names, rollback_traj_names

        home = tmp_path / "xskill_home"
        bridge = home / "ng_sessions"
        sid = "ses_abc123def456ghi789"
        old_id = _make_bridged_traj(bridge, sid)
        migrate_traj_names(xskill_home=home)
        restored = rollback_traj_names(xskill_home=home)
        assert restored == 1
        assert (bridge / f"{old_id}.md").is_file()
        atom = (bridge / old_id / "tasks" / f"atom_{old_id}_0001.json")
        assert atom.is_file()
        payload = json.loads(atom.read_text(encoding="utf-8"))
        assert payload["atom_id"] == f"atom_{old_id}_0001"
        assert not (bridge / f"traj_ng_proj_{short_sid(sid)}.md").exists()

    def test_server_side_member_prefix_migration(self, tmp_path):
        """服务器侧：按 client 注册表反查目录归属，补成员前缀。"""
        from xskill.tools import migrate_traj_names

        home = tmp_path / "xskill_home"
        home.mkdir()
        reg_db = tmp_path / "clients.db"
        reg = ClientRegistry(reg_db)
        cid = reg.register(label="t", hostname="h", user_name="alice")
        traj_root = tmp_path / "team_traj"
        sessions = traj_root / "clients" / "alice" / "sessions"
        sessions.mkdir(parents=True)
        (sessions / "traj_ng_agentfs_ses_0f5d.md").write_text(
            "# t", encoding="utf-8")
        report = migrate_traj_names(
            xskill_home=home, traj_root=traj_root,
            clients_registry_db=reg_db)
        assert report.renamed == 1
        assert (sessions
                / f"traj_alice_{cid[:8]}_ng_agentfs_ses_0f5d.md").is_file()

    def test_unknown_client_dir_skipped_with_note(self, tmp_path):
        from xskill.tools import migrate_traj_names

        home = tmp_path / "xskill_home"
        home.mkdir()
        reg_db = tmp_path / "clients.db"
        ClientRegistry(reg_db)  # 空注册表
        traj_root = tmp_path / "team_traj"
        sessions = traj_root / "clients" / "stray" / "sessions"
        sessions.mkdir(parents=True)
        (sessions / "traj_x_y_z.md").write_text("# t", encoding="utf-8")
        report = migrate_traj_names(
            xskill_home=home, traj_root=traj_root,
            clients_registry_db=reg_db)
        assert report.renamed == 0
        assert any("stray" in s for s in report.skipped)
        assert (sessions / "traj_x_y_z.md").is_file()


# ─────────────────────────────────────────────────────────────────
# 附带修复：多 store 同名原子——内容一致是重复，内容不同才是冲突
# ─────────────────────────────────────────────────────────────────

class TestMultiStoreDuplicateVsConflict:
    def _store(self, root: Path, traj_id: str, body: dict):
        from xskill.pipeline.atom import AtomTaskStore
        tasks = root / traj_id / "tasks"
        tasks.mkdir(parents=True)
        (tasks / f"{body['atom_id']}.json").write_text(
            json.dumps(body), encoding="utf-8")
        return AtomTaskStore(root)

    def _atom_body(self, traj_id: str, summary="s") -> dict:
        return {
            "atom_id": f"atom_{traj_id}_0001", "traj_id": traj_id,
            "offset_start": 1, "offset_end": 2,
            "intent": "i", "summary": summary,
        }

    def test_identical_content_logged_debug_not_warning(self, tmp_path, caplog):
        from xskill.pipeline.atom import MultiAtomTaskStore
        traj = "traj_ng_agentfs_ses_0f5d"
        body = self._atom_body(traj)
        s1 = self._store(tmp_path / "a", traj, body)
        s2 = self._store(tmp_path / "b", traj, body)
        multi = MultiAtomTaskStore([s1, s2])
        with caplog.at_level("DEBUG", logger="xskill.pipeline.atom"):
            atom = multi.load(f"atom_{traj}_0001")
        assert atom.summary == "s"
        warnings = [r for r in caplog.records if r.levelname == "WARNING"]
        assert warnings == []

    def test_different_content_warns_conflict(self, tmp_path, caplog):
        from xskill.pipeline.atom import MultiAtomTaskStore
        traj = "traj_ng_agentfs_ses_0f5d"
        s1 = self._store(tmp_path / "a", traj, self._atom_body(traj, "one"))
        s2 = self._store(tmp_path / "b", traj, self._atom_body(traj, "two"))
        multi = MultiAtomTaskStore([s1, s2])
        with caplog.at_level("WARNING", logger="xskill.pipeline.atom"):
            multi.load(f"atom_{traj}_0001")
        assert any("conflict" in r.getMessage() for r in caplog.records)
