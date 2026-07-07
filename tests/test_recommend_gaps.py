"""test_recommend_gaps.py — review 第二轮 Top2 修复验证

A: /reindex 后引擎缓存失效（invalidate_cache）。
B: _pick_recommended engine 分支索引缺失守卫（防 /sync 500）。
"""
from __future__ import annotations

import pickle
import subprocess
from pathlib import Path

import numpy as np
from fastapi import FastAPI
from fastapi.testclient import TestClient

from xskill.recommend.engine import SkillRecommendEngine
from xskill.team.server import api as server_api
from xskill.team.server.client_registry import ClientRegistry
from xskill.team.server.skill_manifest import (
    _pick_recommended,
    set_recommend_engine,
)


def _git(args, cwd):
    subprocess.run(["git"] + args, cwd=str(cwd), capture_output=True, text=True, check=True)


def _make_main_skill(parent: Path, name: str):
    d = parent / name
    d.mkdir(parents=True)
    _git(["init", "-q"], d); _git(["checkout", "-q", "-b", "main"], d)
    _git(["config", "user.email", "t@t"], d); _git(["config", "user.name", "t"], d)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {name} desc\nmetadata:\n  version: 1\n---\n# {name}\n",
        encoding="utf-8")
    _git(["add", "."], d); _git(["commit", "-q", "-m", "v1"], d)


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


def _write_index(skill_dir: Path, names: list[str], dim: int = 4):
    with open(skill_dir / ".skill_index.pkl", "wb") as f:
        pickle.dump({"skill_names": names, "embeddings": np.eye(len(names), dim),
                     "atom_feats": np.zeros((len(names), dim)),
                     "atom_feat_present": [False] * len(names),
                     "schema_version": 2}, f)


# ── A: invalidate_cache ──────────────────────────────────────────

class TestInvalidateCache:
    def test_invalidate_clears_and_reloads(self, tmp_path):
        skill_dir = tmp_path / "skills"
        _make_main_skill(skill_dir, "s0")
        _write_index(skill_dir, ["s0"])
        eng = SkillRecommendEngine(
            config={"recommend": {}}, skill_dir=skill_dir, traj_root=tmp_path / "traj",
            embed_client=FakeEmbed(), profile_db=tmp_path / "p.db",
        )
        idx1 = eng._skill_index()
        assert idx1["skill_names"] == ["s0"]
        assert eng._skill_index_cache is not None
        # 磁盘上换索引内容
        _make_main_skill(skill_dir, "s1")
        _write_index(skill_dir, ["s0", "s1"])
        # 未失效前仍是旧缓存
        assert eng._skill_index()["skill_names"] == ["s0"]
        # 失效后重读
        eng.invalidate_cache()
        assert eng._skill_index_cache is None
        assert eng._skill_index()["skill_names"] == ["s0", "s1"]

    def test_invalidate_clears_skillhub_cache(self, tmp_path):
        skill_dir = tmp_path / "skills"
        _make_main_skill(skill_dir, "s0")
        _write_index(skill_dir, ["s0"])
        eng = SkillRecommendEngine(
            config={"recommend": {}}, skill_dir=skill_dir, traj_root=tmp_path / "traj",
            embed_client=FakeEmbed(), profile_db=tmp_path / "p.db",
        )
        eng._skillhub_cache = [{"name": "stale"}]
        eng.invalidate_cache()
        assert eng._skillhub_cache is None


# ── B: _pick_recommended 索引缺失守卫 ─────────────────────────────

class TestPickRecommendedIndexGuard:
    def test_no_index_falls_back_to_ux_tail(self, tmp_path, monkeypatch):
        skill_dir = tmp_path / "skills"
        for i in range(3):
            _make_main_skill(skill_dir, f"s{i}")
        # 不写 .skill_index.pkl
        eng = SkillRecommendEngine(
            config={"recommend": {}}, skill_dir=skill_dir, traj_root=tmp_path / "traj",
            embed_client=FakeEmbed(), profile_db=tmp_path / "p.db",
        )
        set_recommend_engine(eng)
        try:
            from xskill.skill.repo import SkillRepo
            repo = SkillRepo(skill_dir)
            ux_ordered = list(repo)
            ranked = ux_ordered[:2]
            ranked_names = {s.name for s in ranked}
            # engine 已注入但无索引 → 应退回 ux_tail，不 raise
            picked = _pick_recommended(
                client_id="u1", skill_dir=skill_dir, ranked=ranked,
                ranked_names=ranked_names, ux_ordered=ux_ordered,
                reco_slots=1, traj_root=tmp_path / "traj",
            )
            assert len(picked) == 1
            assert picked[0].name not in ranked_names
        finally:
            set_recommend_engine(None)

    def test_sync_does_not_500_when_index_missing(self, tmp_path):
        """大仓 rebuild 窗口：索引缺失 + 引擎注入 → /sync 不应 500。"""
        skill_dir = tmp_path / "skill"
        for i in range(3):
            _make_main_skill(skill_dir, f"s{i}")
        traj_root = tmp_path / "traj"
        reg = ClientRegistry(tmp_path / "clients.db")
        eng = SkillRecommendEngine(
            config={"recommend": {"staging_need": 3},
                    "canary": {"total_samples": 3, "min_samples": 3}},
            skill_dir=skill_dir, traj_root=traj_root,
            embed_client=FakeEmbed(), profile_db=tmp_path / "p.db",
        )
        set_recommend_engine(eng)
        server_api.init_team_context(
            join_token="tok", client_registry=reg, skill_dir=skill_dir, traj_root=traj_root,
            probability=0.2, ranked_slots=2, total_slots=3,
            register_dir=lambda p, l: None, allow_anonymous_user=True,
        )
        app = FastAPI()
        app.include_router(server_api.router)
        tc = TestClient(app)
        rr = tc.post("/api/v1/team/register", json={"token": "tok"})
        cid = rr.json()["client_id"]
        try:
            r = tc.get("/api/v1/team/sync",
                       headers={"X-Xskill-Token": "tok", "X-Xskill-Client": cid})
            assert r.status_code == 200  # 不 500
        finally:
            set_recommend_engine(None)
