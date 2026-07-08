"""test_skillhub.py — §6 SkillHub 三方 skill 扫描 + 检索池

TDD: 缺省关 no-op；启用扫描+向量化；启用但目录缺失抛错；三方进合并检索池；
三方不进质量位/staging。
"""
from __future__ import annotations

import pickle
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from xskill.recommend.client_interest import ClientInterest
from xskill.recommend.client_user import ClientUser
from xskill.recommend.engine import SkillRecommendEngine
from xskill.recommend.skillhub import SkillHub
from xskill.team.client.daemon import TeamClient
from xskill.team.client.state import ClientState
from xskill.team.server import api as server_api
from xskill.team.server.client_registry import ClientRegistry
from xskill.team.server.skill_manifest import build_manifest, set_recommend_engine
from xskill.team.shared.protocol import SkillSlot, SyncResponse


class FakeEmbed:
    def __init__(self, dim=4):
        self.dim = dim

    def encode(self, text):
        v = np.zeros(self.dim, dtype=float)
        for i, ch in enumerate(text):
            v[i % self.dim] += ord(ch) % 97
        return v

    def encode_batch(self, texts):
        return np.stack([self.encode(t) for t in texts])


def _git(args, cwd):
    subprocess.run(["git"] + args, cwd=str(cwd), capture_output=True, text=True, check=True)


def _make_main_skill(parent: Path, name: str, desc: str = "d"):
    d = parent / name
    d.mkdir(parents=True)
    _git(["init", "-q"], d); _git(["checkout", "-q", "-b", "main"], d)
    _git(["config", "user.email", "t@t"], d); _git(["config", "user.name", "t"], d)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {desc}\nmetadata:\n  version: 1\n---\n# {name}\n",
        encoding="utf-8")
    _git(["add", "."], d); _git(["commit", "-q", "-m", "v1"], d)
    return d


def _write_hub_skill(
    hub_dir: Path, rel_path: str, desc: str, *, name: str | None = None,
):
    d = hub_dir / rel_path
    d.mkdir(parents=True)
    frontmatter_name = name or d.name
    (d / "SKILL.md").write_text(
        f"---\nname: {frontmatter_name}\ndescription: {desc}\n---\n# {frontmatter_name}\n",
        encoding="utf-8")


def _write_index(skill_dir: Path, names: list[str], dim: int):
    embs = np.eye(len(names), dim, dtype=float)
    with open(skill_dir / ".skill_index.pkl", "wb") as f:
        pickle.dump({"skill_names": names, "embeddings": embs,
                     "atom_feats": np.zeros((len(names), dim)),
                     "atom_feat_present": [False] * len(names)}, f)


# ── SkillHub ─────────────────────────────────────────────────────

class TestSkillHub:
    def test_disabled_noop(self, tmp_path):
        hub = SkillHub(enabled=False, hub_dir=tmp_path / "hub", embed_client=FakeEmbed())
        assert hub.index() == []

    def test_enabled_scans_and_vectorizes(self, tmp_path):
        hub_dir = tmp_path / "hub"
        _write_hub_skill(hub_dir, "foo", "django migration helper")
        _write_hub_skill(hub_dir, "bar", "react component gen")
        hub = SkillHub(enabled=True, hub_dir=hub_dir, embed_client=FakeEmbed(dim=4))
        entries = hub.index()
        names = {e["display_name"] for e in entries}
        assert names == {"foo", "bar"}
        for e in entries:
            assert abs(np.linalg.norm(e["vec"]) - 1.0) < 1e-6  # L2 归一
            assert e["name"] == e["skill_id"]
            assert e["source"] == "skillhub"
            assert e["source_path"] in {"foo", "bar"}
            assert e["content_sha"]

    def test_enabled_dir_missing_raises(self, tmp_path):
        hub = SkillHub(enabled=True, hub_dir=tmp_path / "nope", embed_client=FakeEmbed())
        with pytest.raises(FileNotFoundError, match="skillhub"):
            hub.index()

    def test_from_config_disabled_default(self):
        hub = SkillHub.from_config({}, FakeEmbed())
        assert hub.enabled is False
        assert hub.index() == []

    def test_recursively_scans_nested_skill_md_under_multiple_folders(self, tmp_path):
        hub_dir = tmp_path / "hub"
        _write_hub_skill(
            hub_dir, "hub-a/excel/format", "spreadsheet format helper",
            name="format-helper",
        )
        _write_hub_skill(
            hub_dir, "hub-b/ops/deploy", "deployment runbook helper",
            name="deploy-helper",
        )
        _write_hub_skill(
            hub_dir, "hub-b/flat", "flat folder helper",
            name="flat-helper",
        )
        (hub_dir / "hub-a" / "empty-folder").mkdir(parents=True)

        hub = SkillHub(enabled=True, hub_dir=hub_dir, embed_client=FakeEmbed(dim=4))

        entries = hub.index()

        assert {e["source_path"] for e in entries} == {
            "hub-a/excel/format",
            "hub-b/ops/deploy",
            "hub-b/flat",
        }
        assert {e["display_name"] for e in entries} == {
            "format-helper",
            "deploy-helper",
            "flat-helper",
        }

    def test_duplicate_display_names_are_distinguished_by_relative_path(self, tmp_path):
        hub_dir = tmp_path / "hub"
        _write_hub_skill(hub_dir, "hub-a/foo", "django helper", name="foo")
        _write_hub_skill(hub_dir, "hub-b/foo", "react helper", name="foo")

        hub = SkillHub(enabled=True, hub_dir=hub_dir, embed_client=FakeEmbed(dim=4))

        entries = sorted(hub.index(), key=lambda e: e["source_path"])

        assert [e["display_name"] for e in entries] == ["foo", "foo"]
        assert [e["source_path"] for e in entries] == ["hub-a/foo", "hub-b/foo"]
        assert entries[0]["skill_id"] != entries[1]["skill_id"]

    def test_same_name_and_content_are_still_distinguished_by_relative_path(
        self, tmp_path,
    ):
        hub_dir = tmp_path / "hub"
        _write_hub_skill(hub_dir, "hub-a/foo", "same helper", name="foo")
        _write_hub_skill(hub_dir, "hub-b/foo", "same helper", name="foo")

        hub = SkillHub(enabled=True, hub_dir=hub_dir, embed_client=FakeEmbed(dim=4))

        entries = sorted(hub.index(), key=lambda e: e["source_path"])

        assert entries[0]["content_sha"] == entries[1]["content_sha"]
        assert entries[0]["skill_id"] != entries[1]["skill_id"]


# ── 引擎检索池合并 ───────────────────────────────────────────────

class TestEngineSkillhubPool:
    def _engine(self, tmp_path, *, skillhub_enabled, hub_dir=None):
        skill_dir = tmp_path / "skills"
        _make_main_skill(skill_dir, "s0", "own skill")
        _write_index(skill_dir, ["s0"], dim=4)
        cfg = {"recommend": {"quality_ratio": 0.8}}
        if skillhub_enabled:
            cfg["skillhub"] = {"enabled": True, "dir": str(hub_dir)}
        return SkillRecommendEngine(
            config=cfg, skill_dir=skill_dir, traj_root=tmp_path / "traj",
            embed_client=FakeEmbed(dim=4), profile_db=tmp_path / "p.db",
        )

    def test_skillhub_in_combined_search(self, tmp_path):
        hub_dir = tmp_path / "hub"
        _write_hub_skill(hub_dir, "extfoo", "django migration helper")
        eng = self._engine(tmp_path, skillhub_enabled=True, hub_dir=hub_dir)
        # 用与 extfoo description 同向的 query 向量检索
        q = FakeEmbed(dim=4).encode("django migration helper")
        q = q / np.linalg.norm(q)
        results = eng.relevance_search(q, top_k=5)
        all_names = [n for n, _is_hub in results]
        hub_names = [n for n, is_hub in results if is_hub]
        assert any(n.startswith("extfoo@") for n in hub_names)  # 三方 skill 进了检索池
        assert "s0" in all_names

    def test_skillhub_not_in_quality_bucket(self, tmp_path):
        hub_dir = tmp_path / "hub"
        _write_hub_skill(hub_dir, "extfoo", "django migration helper")
        eng = self._engine(tmp_path, skillhub_enabled=True, hub_dir=hub_dir)
        pool = eng._distributable_skills()
        # 三方 skill 无 git/main → 不在可分发池 → 不进质量位
        assert all(s.name != "extfoo" for s in pool)

    def test_skillhub_disabled_not_in_pool(self, tmp_path):
        eng = self._engine(tmp_path, skillhub_enabled=False)
        q = np.array([1.0, 0.0, 0.0, 0.0])
        results = eng.relevance_search(q, top_k=5)
        names = [n for n, _h in results]
        assert "extfoo" not in names

    def test_recommends_existing_skillhub_entries_and_skips_deleted_cache(
        self, tmp_path,
    ):
        skill_dir = tmp_path / "skills"
        skill_dir.mkdir()
        _write_index(skill_dir, [], dim=4)
        hub_dir = tmp_path / "hub"
        first = hub_dir / "hub-a" / "foo"
        _write_hub_skill(hub_dir, "hub-a/foo", "django migration helper", name="foo")
        _write_hub_skill(hub_dir, "hub-b/foo", "react component helper", name="foo")
        eng = SkillRecommendEngine(
            config={
                "recommend": {"quality_ratio": 0.0},
                "skillhub": {"enabled": True, "dir": str(hub_dir)},
            },
            skill_dir=skill_dir,
            traj_root=tmp_path / "traj",
            embed_client=FakeEmbed(dim=4),
            profile_db=tmp_path / "p.db",
        )
        entries = sorted(eng._skillhub_entries(), key=lambda e: e["source_path"])
        stale_id = entries[0]["skill_id"]
        shutil.rmtree(first)

        q = FakeEmbed(dim=4).encode("django migration helper")
        q = q / np.linalg.norm(q)
        user = ClientUser(
            "u",
            client_interest=ClientInterest("u", feature_tensor=np.asarray([q])),
        )

        picked = eng.get_skill_for_client(user, 2)

        assert stale_id not in {p["skill_id"] for p in picked}
        assert all(p["source"] == "skillhub" for p in picked)

    def test_manifest_can_include_skillhub_slot_with_identity_and_version(
        self, tmp_path,
    ):
        skill_dir = tmp_path / "skills"
        skill_dir.mkdir()
        _write_index(skill_dir, [], dim=4)
        hub_dir = tmp_path / "hub"
        _write_hub_skill(hub_dir, "hub-a/foo", "django migration helper", name="foo")
        eng = SkillRecommendEngine(
            config={
                "recommend": {"quality_ratio": 0.0},
                "skillhub": {"enabled": True, "dir": str(hub_dir)},
            },
            skill_dir=skill_dir,
            traj_root=tmp_path / "traj",
            embed_client=FakeEmbed(dim=4),
            profile_db=tmp_path / "p.db",
        )
        q = FakeEmbed(dim=4).encode("django migration helper")
        q = q / np.linalg.norm(q)
        eng.profile_store.upsert(
            "client-one",
            feature_tensor=np.asarray([q]),
            mean_tensor=q,
            used_skills=[],
        )
        set_recommend_engine(eng)
        try:
            resp = build_manifest(
                client_id="client-one",
                skill_dir=skill_dir,
                probability=0.0,
                ranked_slots=0,
                total_slots=1,
                traj_root=tmp_path / "traj",
            )
        finally:
            set_recommend_engine(None)

        assert len(resp.slots) == 1
        slot = resp.slots[0]
        entry = eng.skillhub.index()[0]
        assert slot.source == "skillhub"
        assert slot.skill_name == entry["skill_id"]
        assert slot.display_name == "foo"
        assert slot.source_path == "hub-a/foo"
        assert slot.sha == entry["content_sha"]

    def test_adding_skillhub_after_empty_scan_refreshes_recommendations(
        self, tmp_path,
    ):
        skill_dir = tmp_path / "skills"
        skill_dir.mkdir()
        _write_index(skill_dir, [], dim=4)
        hub_dir = tmp_path / "hub"
        hub_dir.mkdir()
        eng = SkillRecommendEngine(
            config={
                "recommend": {"quality_ratio": 0.0},
                "skillhub": {"enabled": True, "dir": str(hub_dir)},
            },
            skill_dir=skill_dir,
            traj_root=tmp_path / "traj",
            embed_client=FakeEmbed(dim=4),
            profile_db=tmp_path / "p.db",
        )
        q = FakeEmbed(dim=4).encode("django migration helper")
        q = q / np.linalg.norm(q)
        eng.profile_store.upsert(
            "client-one",
            feature_tensor=np.asarray([q]),
            mean_tensor=q,
            used_skills=[],
        )
        set_recommend_engine(eng)
        try:
            first = build_manifest(
                client_id="client-one",
                skill_dir=skill_dir,
                probability=0.0,
                ranked_slots=0,
                total_slots=1,
                traj_root=tmp_path / "traj",
            )
            assert first.slots == []

            _write_hub_skill(
                hub_dir, "hub-a/foo", "django migration helper", name="foo",
            )
            second = build_manifest(
                client_id="client-one",
                skill_dir=skill_dir,
                probability=0.0,
                ranked_slots=0,
                total_slots=1,
                traj_root=tmp_path / "traj",
            )
        finally:
            set_recommend_engine(None)

        assert len(second.slots) == 1
        assert second.slots[0].source == "skillhub"


# ── Team push path ───────────────────────────────────────────────

class TestSkillhubTeamPush:
    def _app(self, tmp_path, hub: SkillHub, reg: ClientRegistry):
        skill_dir = tmp_path / "skills"
        skill_dir.mkdir(exist_ok=True)
        server_api.init_team_context(
            join_token="tok",
            client_registry=reg,
            skill_dir=skill_dir,
            traj_root=tmp_path / "traj",
            probability=0.0,
            ranked_slots=0,
            total_slots=1,
            register_dir=lambda _p, _l: None,
            skillhub=hub,
        )
        app = FastAPI()
        app.include_router(server_api.router)
        return TestClient(app)

    def test_server_serves_skillhub_archive_and_404_after_delete(self, tmp_path):
        hub_dir = tmp_path / "hub"
        skill_path = hub_dir / "hub-a" / "foo"
        _write_hub_skill(hub_dir, "hub-a/foo", "django migration helper", name="foo")
        hub = SkillHub(enabled=True, hub_dir=hub_dir, embed_client=FakeEmbed(dim=4))
        entry = hub.index()[0]
        reg = ClientRegistry(tmp_path / "clients.db")
        client_id = reg.register(label="alice", hostname="host")
        http = self._app(tmp_path, hub, reg)
        headers = {"X-Xskill-Token": "tok", "X-Xskill-Client": client_id}

        ok = http.get(f"/api/v1/team/skill/{entry['skill_id']}/bundle", headers=headers)
        assert ok.status_code == 200
        assert (ok.content[:2] == b"PK")  # zip archive

        shutil.rmtree(skill_path)
        missing = http.get(
            f"/api/v1/team/skill/{entry['skill_id']}/bundle", headers=headers,
        )
        assert missing.status_code == 404

    def test_client_materializes_skillhub_slot_without_git(self, tmp_path):
        hub_dir = tmp_path / "hub"
        _write_hub_skill(hub_dir, "hub-a/foo", "django migration helper", name="foo")
        hub = SkillHub(enabled=True, hub_dir=hub_dir, embed_client=FakeEmbed(dim=4))
        entry = hub.index()[0]
        reg = ClientRegistry(tmp_path / "clients.db")
        client_id = reg.register(label="alice", hostname="host")
        http = self._app(tmp_path, hub, reg)
        state = ClientState(
            server_url="http://testserver",
            client_id=client_id,
            join_token="tok",
        )
        client = TeamClient(
            state=state,
            http=http,
            skill_dir=tmp_path / "client_home" / ".xskill" / "skill",
            cursor_path=tmp_path / "cursor.json",
            history_path=tmp_path / "history.jsonl",
            home_root=tmp_path / "client_home",
            min_change_interval=0,
        )
        manifest = SyncResponse(
            server_time=1.0,
            slots=[
                SkillSlot(
                    skill_name=entry["skill_id"],
                    side="main",
                    sha=entry["content_sha"],
                    bucket="recommended",
                    source="skillhub",
                    display_name=entry["display_name"],
                    source_path=entry["source_path"],
                )
            ],
        )

        client.reconcile_skill_sides(manifest)

        installed = client.skill_dir / entry["skill_id"]
        assert (installed / "SKILL.md").is_file()
        assert not (installed / ".git").exists()
        assert (installed / ".xskill_skillhub.json").is_file()
