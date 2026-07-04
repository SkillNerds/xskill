"""test_skill_feature.py — §3 SkillFeature (vec=desc, atom_feat 独立) + Skill 属性 + rebuild

TDD: 主特征为 description 向量、不融合；atom_feat 独立读索引；skill_meta 视图；
rebuild_skill_index 产出 desc-only embeddings + 独立 atom_feats。
"""
from __future__ import annotations

import pickle
import subprocess
from pathlib import Path

import numpy as np
import pytest

from xskill.recommend.skill_feature import SkillFeature
from xskill.skill.repo import rebuild_skill_index
from xskill.skill.skill import Skill


def _git(args, cwd):
    subprocess.run(["git"] + args, cwd=str(cwd), capture_output=True, text=True, check=True)


def _make_skill_md(name: str, description: str) -> str:
    return (
        f"---\nname: {name}\ndescription: {description}\n"
        f"metadata:\n  version: 1\n---\n# {name}\n"
    )


class FakeEmbed:
    """记录调用文本的假 embed client；encode/encode_batch 返回确定性向量。"""

    def __init__(self, dim: int = 4):
        self.dim = dim
        self.batch_texts: list[list[str]] = []

    def encode(self, text: str) -> np.ndarray:
        return self._vec(text)

    def encode_batch(self, texts: list[str]) -> np.ndarray:
        self.batch_texts.append(list(texts))
        return np.stack([self._vec(t) for t in texts])

    def _vec(self, text: str) -> np.ndarray:
        v = np.zeros(self.dim, dtype=float)
        for i, ch in enumerate(text):
            v[i % self.dim] += ord(ch) % 97
        return v


# ── SkillFeature.vec ─────────────────────────────────────────────

class TestVec:
    def test_read_from_index(self, tmp_path):
        skill_dir = tmp_path / "skills"
        (skill_dir / "foo").mkdir(parents=True)
        (skill_dir / "foo" / "SKILL.md").write_text(_make_skill_md("foo", "desc foo"), encoding="utf-8")
        onehot = np.zeros((1, 4)); onehot[0] = [1, 0, 0, 0]
        with open(skill_dir / ".skill_index.pkl", "wb") as f:
            pickle.dump({"skill_names": ["foo"], "embeddings": onehot,
                         "atom_feats": np.zeros((1, 4)), "atom_feat_present": [False]}, f)
        s = Skill(skill_dir / "foo")
        assert np.allclose(s.vec, [1, 0, 0, 0])

    def test_compute_from_description_when_no_index(self, tmp_path, monkeypatch):
        skill_dir = tmp_path / "skills"
        (skill_dir / "foo").mkdir(parents=True)
        (skill_dir / "foo" / "SKILL.md").write_text(_make_skill_md("foo", "hello"), encoding="utf-8")
        fake = FakeEmbed()
        s = Skill(skill_dir / "foo")
        s._embed_client = fake
        v = s.vec
        expected = FakeEmbed._vec(fake, "hello")
        expected = expected / np.linalg.norm(expected)
        assert np.allclose(v, expected)

    def test_vec_cached_compute_once(self, tmp_path):
        skill_dir = tmp_path / "skills"
        (skill_dir / "foo").mkdir(parents=True)
        (skill_dir / "foo" / "SKILL.md").write_text(_make_skill_md("foo", "hello"), encoding="utf-8")
        calls = {"n": 0}

        class CountingEmbed(FakeEmbed):
            def encode(self, text):
                calls["n"] += 1
                return self._vec(text)

        s = Skill(skill_dir / "foo")
        s._embed_client = CountingEmbed()
        _ = s.vec
        _ = s.vec
        assert calls["n"] == 1

    def test_vec_does_not_fuse_tags_or_summary(self, tmp_path):
        """主特征只来自 description：即便 frontmatter 有 tags/summary 也不并入。"""
        skill_dir = tmp_path / "skills"
        (skill_dir / "foo").mkdir(parents=True)
        md = ("---\nname: foo\ndescription: mydesc\nmetadata:\n"
              "  tags: [a, b]\n  summary: mysum\n  version: 1\n---\n# foo\n")
        (skill_dir / "foo" / "SKILL.md").write_text(md, encoding="utf-8")
        fake = FakeEmbed()
        s = Skill(skill_dir / "foo")
        s._embed_client = fake
        _ = s.vec
        # 没有索引 → 走现算；FakeEmbed.encode 被调用的文本应只是 description
        # （vec 现算只 embed description，不 embed tags/summary）
        # 通过 _vec("mydesc") 归一化比对
        expected = FakeEmbed._vec(fake, "mydesc")
        expected = expected / np.linalg.norm(expected)
        assert np.allclose(s.vec, expected)


# ── SkillFeature.atom_feat ───────────────────────────────────────

class TestAtomFeat:
    def _index(self, names, present, dim=4):
        feats = np.zeros((len(names), dim), dtype=float)
        for i, p in enumerate(present):
            if p:
                feats[i] = [1, 1, 1, 1]
        return {"skill_names": names, "embeddings": np.zeros((len(names), dim)),
                "atom_feats": feats, "atom_feat_present": present}

    def test_present_from_index(self, tmp_path):
        skill_dir = tmp_path / "skills"
        (skill_dir / "foo").mkdir(parents=True)
        (skill_dir / "foo" / "SKILL.md").write_text(_make_skill_md("foo", "d"), encoding="utf-8")
        idx = self._index(["foo"], [True])
        sf = SkillFeature(Skill(skill_dir / "foo"), skill_index=idx)
        assert np.allclose(sf.atom_feat, [1, 1, 1, 1])

    def test_none_when_not_present(self, tmp_path):
        skill_dir = tmp_path / "skills"
        (skill_dir / "foo").mkdir(parents=True)
        (skill_dir / "foo" / "SKILL.md").write_text(_make_skill_md("foo", "d"), encoding="utf-8")
        idx = self._index(["foo"], [False])
        sf = SkillFeature(Skill(skill_dir / "foo"), skill_index=idx)
        assert sf.atom_feat is None

    def test_raises_when_index_lacks_atom_feats(self, tmp_path):
        skill_dir = tmp_path / "skills"
        (skill_dir / "foo").mkdir(parents=True)
        (skill_dir / "foo" / "SKILL.md").write_text(_make_skill_md("foo", "d"), encoding="utf-8")
        idx = {"skill_names": ["foo"], "embeddings": np.zeros((1, 4))}  # 无 atom_feats
        sf = SkillFeature(Skill(skill_dir / "foo"), skill_index=idx)
        with pytest.raises(RuntimeError, match="atom_feats"):
            _ = sf.atom_feat


# ── Skill.skill_meta ─────────────────────────────────────────────

class TestSkillMeta:
    def _git_skill(self, path: Path, name: str):
        path.mkdir(parents=True)
        _git(["init", "-q"], path); _git(["checkout", "-q", "-b", "main"], path)
        _git(["config", "user.email", "t@t"], path); _git(["config", "user.name", "t"], path)
        (path / "SKILL.md").write_text(_make_skill_md(name, "d"), encoding="utf-8")
        _git(["add", "."], path); _git(["commit", "-q", "-m", "v1"], path)

    def test_main_only_no_staging(self, tmp_path):
        d = tmp_path / "foo"
        self._git_skill(d, "foo")
        meta = Skill(d).skill_meta
        assert meta["main"]["git_hash"]
        assert meta["staging"] is None

    def test_staging_present(self, tmp_path):
        d = tmp_path / "foo"
        self._git_skill(d, "foo")
        _git(["branch", "staging"], d)
        meta = Skill(d).skill_meta
        assert meta["staging"] is not None
        assert meta["staging"]["git_hash"]


# ── rebuild_skill_index ──────────────────────────────────────────

class TestRebuildIndex:
    def test_desc_only_embeddings_plus_atom_feats(self, tmp_path):
        skill_dir = tmp_path / "skills"
        for name, desc in [("foo", "desc foo"), ("bar", "desc bar")]:
            d = skill_dir / name
            d.mkdir(parents=True)
            (d / "SKILL.md").write_text(_make_skill_md(name, desc), encoding="utf-8")
        # atom store：foo 有一个用过它的 atom，bar 没有
        atom_root = tmp_path / "atoms"
        tasks = atom_root / "traj_001" / "tasks"
        tasks.mkdir(parents=True)
        import json
        (tasks / "atom_traj_001_0001.json").write_text(json.dumps({
            "atom_id": "atom_traj_001_0001", "traj_id": "traj_001",
            "offset_start": 1, "offset_end": 2, "intent": "i", "summary": "foo summary",
            "used_skills": ["foo"],
        }), encoding="utf-8")

        fake = FakeEmbed()
        rebuild_skill_index(skill_dir=skill_dir, embed_client=fake,
                            atom_store_roots=[atom_root], last_n_atoms=5)

        with open(skill_dir / ".skill_index.pkl", "rb") as f:
            idx = pickle.load(f)
        # embeddings 来自 description-only（batch 调用的文本就是 descriptions）
        assert idx["skill_names"] == ["bar", "foo"]  # sorted
        descs_sorted = ["desc bar", "desc foo"]
        assert fake.batch_texts[0] == descs_sorted
        # atom_feats：foo present，bar not
        names = idx["skill_names"]
        fi = names.index("foo"); bi = names.index("bar")
        assert idx["atom_feat_present"][fi] is True
        assert idx["atom_feat_present"][bi] is False
        assert np.allclose(idx["atom_feats"][bi], 0.0)
        # foo 的 atom_feat 来自 "foo summary" 均值归一
        exp = FakeEmbed._vec(fake, "foo summary")
        exp = exp / np.linalg.norm(exp)
        assert np.allclose(idx["atom_feats"][fi], exp)

    def test_no_atom_roots_all_absent(self, tmp_path):
        skill_dir = tmp_path / "skills"
        d = skill_dir / "foo"; d.mkdir(parents=True)
        (d / "SKILL.md").write_text(_make_skill_md("foo", "desc"), encoding="utf-8")
        rebuild_skill_index(skill_dir=skill_dir, embed_client=FakeEmbed(),
                            atom_store_roots=None)
        with open(skill_dir / ".skill_index.pkl", "rb") as f:
            idx = pickle.load(f)
        assert idx["atom_feat_present"] == [False]
