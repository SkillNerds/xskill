"""test_recommend_engine.py — §5 SkillRecommendEngine

TDD: baby 排除、update_user_interest、80/20+回填、staging 优先达量、双向记录、
find_friend、find_tag_*。
"""
from __future__ import annotations

import json
import pickle
import subprocess
from pathlib import Path

import numpy as np

from xskill.canary import CanaryConfig, append_ux_score, main_sha, staging_sha
from xskill.recommend.client_interest import ClientInterest
from xskill.recommend.client_user import ClientUser
from xskill.recommend.engine import SkillRecommendEngine


def _git(args, cwd):
    subprocess.run(["git"] + args, cwd=str(cwd), capture_output=True, text=True, check=True)


def _make_main_skill(parent: Path, name: str, desc: str = "d") -> tuple[Path, str]:
    d = parent / name
    d.mkdir(parents=True)
    _git(["init", "-q"], d); _git(["checkout", "-q", "-b", "main"], d)
    _git(["config", "user.email", "t@t"], d); _git(["config", "user.name", "t"], d)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {desc}\nmetadata:\n  version: 1\n---\n# {name}\n",
        encoding="utf-8")
    _git(["add", "."], d); _git(["commit", "-q", "-m", "v1"], d)
    return d, main_sha(d)


def _make_baby_skill(parent: Path, name: str) -> Path:
    d = parent / name
    d.mkdir(parents=True)
    _git(["init", "-q"], d); _git(["checkout", "-q", "-b", "baby"], d)
    _git(["config", "user.email", "t@t"], d); _git(["config", "user.name", "t"], d)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: baby\n---\n# {name}\n", encoding="utf-8")
    _git(["add", "."], d); _git(["commit", "-q", "-m", "b"], d)
    return d


def _add_staging(d: Path) -> str:
    _git(["checkout", "-q", "-b", "staging"], d)
    (d / "SKILL.md").write_text(
        (d / "SKILL.md").read_text(encoding="utf-8") + "\nstagings\n", encoding="utf-8")
    _git(["add", "."], d); _git(["commit", "-q", "-m", "stg"], d)
    _git(["checkout", "-q", "main"], d)
    return staging_sha(d)


def _write_index(skill_dir: Path, names: list[str], dim: int):
    embs = np.eye(len(names), dim, dtype=float)  # one-hot
    with open(skill_dir / ".skill_index.pkl", "wb") as f:
        pickle.dump({
            "skill_names": names, "embeddings": embs,
            "atom_feats": np.zeros((len(names), dim)),
            "atom_feat_present": [False] * len(names),
        }, f)


def _write_atom(root: Path, traj_id: str, atom_id: str, *, summary: str,
                used_skills: list[str], tags: list[str] | None = None,
                ux_score: int | None = None):
    tasks = root / traj_id / "tasks"
    tasks.mkdir(parents=True, exist_ok=True)
    (tasks / f"{atom_id}.json").write_text(json.dumps({
        "atom_id": atom_id, "traj_id": traj_id, "offset_start": 1, "offset_end": 2,
        "intent": "i", "summary": summary, "used_skills": used_skills,
        "tags": tags or [], "ux_score": ux_score,
    }), encoding="utf-8")


class FakeEmbed:
    def __init__(self, dim=5):
        self.dim = dim

    def encode(self, text):
        v = np.zeros(self.dim, dtype=float)
        for i, ch in enumerate(text):
            v[i % self.dim] += ord(ch) % 97
        return v

    def encode_batch(self, texts):
        return np.stack([self.encode(t) for t in texts])


def _engine(tmp_path, skill_dir, traj_root, *, total_samples=3):
    return SkillRecommendEngine(
        config={"recommend": {"quality_ratio": 0.8, "staging_need": None},
                "canary": {"total_samples": total_samples}},
        skill_dir=skill_dir, traj_root=traj_root,
        embed_client=FakeEmbed(dim=5), profile_db=tmp_path / "profile.db",
    )


# ── baby 排除 ────────────────────────────────────────────────────

class TestPool:
    def test_baby_excluded(self, tmp_path):
        skill_dir = tmp_path / "skills"
        _make_main_skill(skill_dir, "s0")
        _make_baby_skill(skill_dir, "baby1")
        _write_index(skill_dir, ["s0", "baby1"], dim=5)
        eng = _engine(tmp_path, skill_dir, tmp_path / "traj")
        pool = eng._distributable_skills()
        assert {s.name for s in pool} == {"s0"}


# ── update_user_interest ─────────────────────────────────────────

class TestUpdateUserInterest:
    def test_atom_updates_profile(self, tmp_path):
        skill_dir = tmp_path / "skills"
        _make_main_skill(skill_dir, "s0")
        _write_index(skill_dir, ["s0"], dim=5)
        traj_root = tmp_path / "traj"
        sessions = traj_root / "clients" / "u1" / "sessions"
        _write_atom(sessions, "traj_1", "atom_traj_1_0001",
                    summary="fix django migration", used_skills=["s0"], ux_score=8)
        eng = _engine(tmp_path, skill_dir, traj_root)
        ci = ClientInterest("u1")
        eng.update_user_interest(ci, task_atom=None)
        row = eng.profile_store.load("u1")
        assert row is not None
        assert row["feature_tensor"] is not None  # 有 atom → 有画像
        assert row["used_skills"][0]["name"] == "s0"
        assert row["used_skills"][0]["avg_score"] == 8.0

    def test_no_atoms_cold_start(self, tmp_path):
        skill_dir = tmp_path / "skills"
        _make_main_skill(skill_dir, "s0")
        _write_index(skill_dir, ["s0"], dim=5)
        eng = _engine(tmp_path, skill_dir, tmp_path / "traj")
        ci = ClientInterest("u1")
        eng.update_user_interest(ci)
        row = eng.profile_store.load("u1")
        assert row["feature_tensor"] is None


# ── get_skill_for_client 80/20 + 回填 ─────────────────────────────

class TestGetSkill:
    def _setup5(self, tmp_path):
        skill_dir = tmp_path / "skills"
        shas = {}
        for i in range(5):
            _, sh = _make_main_skill(skill_dir, f"s{i}", desc=f"desc {i}")
            shas[f"s{i}"] = sh
        _write_index(skill_dir, [f"s{i}" for i in range(5)], dim=5)
        return skill_dir, shas

    def test_standard_80_20(self, tmp_path):
        skill_dir, shas = self._setup5(tmp_path)
        # s0..s3 有 ux 分；s4 无
        for i in range(4):
            append_ux_score(skill_dir / f"s{i}", traj_id="t", skill_name=f"s{i}",
                            side="main", commit_sha=shas[f"s{i}"], score=5 + i, reasons="r")
        traj_root = tmp_path / "traj"
        eng = _engine(tmp_path, skill_dir, traj_root)
        # 用户 feature_tensor = [one-hot(s4)] → 相关性应选 s4
        ci = ClientInterest("u1")
        ci._feature_tensor = np.array([[0.0, 0.0, 0.0, 0.0, 1.0]])  # s4 one-hot
        ci._mean_tensor = np.array([0.0, 0.0, 0.0, 0.0, 1.0])
        user = ClientUser("u1", client_interest=ci)
        # 预置画像让 find 不报错
        eng.profile_store.upsert("u1", feature_tensor=ci._feature_tensor,
                                 mean_tensor=ci._mean_tensor, used_skills=[])
        skills = eng.get_skill_for_client(user, skill_num=5)
        names = [s.name for s in skills]
        # 质量 4 个（s0..s3）+ 相关性 1 个（s4）
        assert set(names) == {"s0", "s1", "s2", "s3", "s4"}

    def test_backfill_when_quality_small(self, tmp_path):
        skill_dir, shas = self._setup5(tmp_path)
        # 只有 s0 有 ux 分
        append_ux_score(skill_dir / "s0", traj_id="t", skill_name="s0",
                        side="main", commit_sha=shas["s0"], score=9, reasons="r")
        eng = _engine(tmp_path, skill_dir, tmp_path / "traj")
        user = ClientUser("u1")  # 冷启动无画像
        skills = eng.get_skill_for_client(user, skill_num=5)
        assert len(skills) == 5  # 质量 1 + 回填 4
        assert "s0" in [s.name for s in skills]


# ── resolve_side staging 优先达量 ─────────────────────────────────

class TestResolveSide:
    def _skill_with_staging(self, tmp_path, name="sx"):
        skill_dir = tmp_path / "skills"
        d, msh = _make_main_skill(skill_dir, name)
        ssh = _add_staging(d)
        _write_index(skill_dir, [name], dim=5)
        return skill_dir, d, msh, ssh

    def test_staging_under_quota_pushes_staging(self, tmp_path):
        skill_dir, d, msh, ssh = self._skill_with_staging(tmp_path)
        eng = _engine(tmp_path, skill_dir, tmp_path / "traj", total_samples=3)
        # 无任何 ux 分 → staging 未达量 → staging
        user = ClientUser("u1")
        s = eng._distributable_skills()[0]
        assert eng.resolve_side(s, user) == "staging"

    def test_staging_full_main_under_pushes_main(self, tmp_path):
        skill_dir, d, msh, ssh = self._skill_with_staging(tmp_path)
        # staging 3 分（达量），main 0 分
        for i in range(3):
            append_ux_score(d, traj_id=f"t{i}", skill_name="sx", side="staging",
                            commit_sha=ssh, score=7, reasons="r")
        eng = _engine(tmp_path, skill_dir, tmp_path / "traj", total_samples=3)
        user = ClientUser("u1")
        s = eng._distributable_skills()[0]
        assert eng.resolve_side(s, user) == "main"

    def test_both_full_defers_to_router(self, tmp_path):
        skill_dir, d, msh, ssh = self._skill_with_staging(tmp_path)
        for i in range(3):
            append_ux_score(d, traj_id=f"tm{i}", skill_name="sx", side="main",
                            commit_sha=msh, score=7, reasons="r")
            append_ux_score(d, traj_id=f"ts{i}", skill_name="sx", side="staging",
                            commit_sha=ssh, score=7, reasons="r")
        eng = _engine(tmp_path, skill_dir, tmp_path / "traj", total_samples=3)
        user = ClientUser("u1")
        s = eng._distributable_skills()[0]
        side = eng.resolve_side(s, user)
        assert side in ("main", "staging")  # router 决定，但确定性钉死


# ── 双向记录 ─────────────────────────────────────────────────────

class TestBidirectionalRecord:
    def test_recorded_both_ways(self, tmp_path):
        skill_dir = tmp_path / "skills"
        _, sh = _make_main_skill(skill_dir, "s0")
        _write_index(skill_dir, ["s0"], dim=5)
        eng = _engine(tmp_path, skill_dir, tmp_path / "traj")
        user = ClientUser("u1")
        eng.get_skill_for_client(user, skill_num=1)
        assert len(user.recommended_skills) == 1
        rec = user.recommended_skills[0]
        assert rec["skill"] == "s0"
        # 反查：s0 main 被推给了 u1
        assert "u1" in eng.reco_store.users_for_skill("s0", rec["branch"])


# ── find_friend / find_tag ───────────────────────────────────────

class TestFindFriendAndTag:
    def test_find_friend_returns_similar(self, tmp_path):
        skill_dir = tmp_path / "skills"
        _make_main_skill(skill_dir, "s0")
        _write_index(skill_dir, ["s0"], dim=5)
        eng = _engine(tmp_path, skill_dir, tmp_path / "traj")
        # 两个用户画像，u1 与 u2 mean 相同、与 u3 不同
        m = np.array([1.0, 0.0, 0.0, 0.0, 0.0])
        eng.profile_store.upsert("u2", feature_tensor=np.array([[1.0, 0, 0, 0, 0]]),
                                 mean_tensor=m, used_skills=[])
        eng.profile_store.upsert("u3", feature_tensor=np.array([[0.0, 1.0, 0, 0, 0]]),
                                 mean_tensor=np.array([0.0, 1.0, 0, 0, 0]), used_skills=[])
        ci = ClientInterest("u1")
        ci._feature_tensor = np.array([[1.0, 0, 0, 0, 0]])
        ci._mean_tensor = m
        user = ClientUser("u1", client_interest=ci)
        friends = eng.find_friend(user)
        assert "u2" in friends
        assert friends[0] == "u2"  # 最相似

    def test_find_tag_for_skill(self, tmp_path):
        skill_dir = tmp_path / "skills"
        _make_main_skill(skill_dir, "s0")
        _write_index(skill_dir, ["s0"], dim=5)
        traj_root = tmp_path / "traj"
        sessions = traj_root / "clients" / "u1" / "sessions"
        _write_atom(sessions, "traj_1", "atom_1", summary="x",
                    used_skills=["s0"], tags=["django", "migration"])
        eng = _engine(tmp_path, skill_dir, traj_root)
        s = eng._distributable_skills()[0]
        tags = eng.find_tag_for_skill(s)
        assert set(tags) >= {"django", "migration"}

    def test_find_tag_for_user(self, tmp_path):
        skill_dir = tmp_path / "skills"
        _make_main_skill(skill_dir, "s0")
        _write_index(skill_dir, ["s0"], dim=5)
        traj_root = tmp_path / "traj"
        sessions = traj_root / "clients" / "u1" / "sessions"
        _write_atom(sessions, "traj_1", "atom_1", summary="django migration",
                    used_skills=["s0"], tags=["django", "migration"])
        eng = _engine(tmp_path, skill_dir, traj_root)
        ci = ClientInterest("u1")
        eng.update_user_interest(ci)  # 建画像
        user = ClientUser("u1", client_interest=ci)
        tags = eng.find_tag_for_user(user)
        assert isinstance(tags, list)
