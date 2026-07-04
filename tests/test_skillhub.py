"""test_skillhub.py — §6 SkillHub 三方 skill 扫描 + 检索池

TDD: 缺省关 no-op；启用扫描+向量化；启用但目录缺失抛错；三方进合并检索池；
三方不进质量位/staging。
"""
from __future__ import annotations

import pickle
import subprocess
from pathlib import Path

import numpy as np
import pytest

from xskill.canary import main_sha
from xskill.recommend.engine import SkillRecommendEngine
from xskill.recommend.skillhub import SkillHub


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


def _write_hub_skill(hub_dir: Path, name: str, desc: str):
    d = hub_dir / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {desc}\n---\n# {name}\n", encoding="utf-8")


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
        names = {e["name"] for e in entries}
        assert names == {"foo", "bar"}
        for e in entries:
            assert abs(np.linalg.norm(e["vec"]) - 1.0) < 1e-6  # L2 归一

    def test_enabled_dir_missing_raises(self, tmp_path):
        hub = SkillHub(enabled=True, hub_dir=tmp_path / "nope", embed_client=FakeEmbed())
        with pytest.raises(FileNotFoundError, match="skillhub"):
            hub.index()

    def test_from_config_disabled_default(self):
        hub = SkillHub.from_config({}, FakeEmbed())
        assert hub.enabled is False
        assert hub.index() == []


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
        names = [n for n, _h in results]
        assert "extfoo" in names  # 三方 skill 进了检索池
        assert "s0" in names

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
