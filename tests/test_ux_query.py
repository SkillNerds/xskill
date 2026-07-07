"""test_ux_query.py — ux 分版本聚合 + atom 关联 + HTTP 端点

覆盖四个缺口：
1. ``Skill.ux_scores_by_version`` 多版本场景：同一 skill 名，写入两个不同
   ``commit_sha`` 的 ux 分（各 2-3 条）→ 返回两组，各自 count/avg 正确，按
   ``last_scored_at`` 降序。
2. ``Skill.ux_avg`` 按当前版本 sha 过滤：当前版本 sha = 新版本 → ``ux_avg``
   只算新版本的分，不混旧版本。
3. ``Skill.ux_scores_with_atoms(traj_root=...)``：atom 文件存在 → ``atom``
   字段含 summary/intent；atom 文件不存在 → ``atom=None``。
4. 三方 skill（``SkillHub``）同上。
5. HTTP 端点：FastAPI TestClient 打 ``GET /api/v1/dashboard/skill/{name}/ux``
   断言响应结构（versions / current_version）；``/skillhub/{name}/ux`` 同测。
"""
from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from xskill.dashboard.router import build_dashboard_router
from xskill.recommend.skillhub import SkillHub
from xskill.skill.skill import Skill


# ──────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────

def _git(args, cwd):
    subprocess.run(["git"] + args, cwd=str(cwd), capture_output=True,
                   text=True, check=True)


def _make_own_skill(path: Path, name: str) -> Path:
    """初始化一个有 git 的自有 skill（main 分支 + 一次 commit）。返回目录。"""
    path.mkdir(parents=True)
    _git(["init", "-q"], path)
    _git(["checkout", "-q", "-b", "main"], path)
    _git(["config", "user.email", "t@t"], path)
    _git(["config", "user.name", "t"], path)
    (path / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: d\n---\n# {name}\n",
        encoding="utf-8")
    _git(["add", "."], path)
    _git(["commit", "-q", "-m", "v1"], path)
    return path


def _add_staging(path: Path) -> None:
    """在 path 上加一个 staging 分支，指向 main 的初始 commit（与 main 不同 sha）。

    main 已有 v1 commit；本 helper 先在 main 上加 v2 commit（main 前进到 v2），
    再把 staging 指回 v1 → main_sha != staging_sha。
    """
    initial = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True).stdout.strip()
    (path / "SKILL.md").write_text("v2 staging\n", encoding="utf-8")
    _git(["add", "."], path)
    _git(["commit", "-q", "-m", "v2"], path)
    _git(["branch", "staging", initial], path)


def _write_ux_score(skill_dir: Path, *, atom_id: str, side: str,
                    commit_sha: str, score: float, reasons: str = "",
                    user_model: str = "", scored_at: str) -> None:
    """直接追加一条 ux 分到 .ux_scores.jsonl，scored_at 可显式控制。"""
    rec = {
        "atom_id": atom_id,
        "skill_name": skill_dir.name,
        "side": side,
        "commit_sha": commit_sha,
        "score": float(score),
        "reasons": reasons,
        "user_model": user_model,
        "scored_at": scored_at,
    }
    p = skill_dir / ".ux_scores.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _write_hub_skill(hub_dir: Path, name: str, desc: str = "三方 skill") -> Path:
    d = hub_dir / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {desc}\n---\n# {name}\n",
        encoding="utf-8")
    return d


def _content_sha(skill_md: Path) -> str:
    return hashlib.sha256(skill_md.read_bytes()).hexdigest()[:16]


def _write_atom(traj_root: Path, client_id: str, traj_id: str,
                atom_id: str, *, summary: str, intent: str,
                tags: list[str] | None = None,
                used_skills: list[str] | None = None) -> Path:
    """按 team server 落盘结构写一个 atom JSON 文件。"""
    tasks = traj_root / "clients" / client_id / "sessions" / traj_id / "tasks"
    tasks.mkdir(parents=True, exist_ok=True)
    p = tasks / f"{atom_id}.json"
    data = {
        "atom_id": atom_id,
        "traj_id": traj_id,
        "offset_start": 0,
        "offset_end": 2,
        "intent": intent,
        "summary": summary,
        "tags": tags or [],
        "used_skills": used_skills or [],
        "ux_score": None,
        "pre_atom_id": None,
        "post_atom_id": None,
        "context_prefix": "",
        "raw_segment": "",
        "source_model": "",
        "clustered": False,
    }
    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return p


# ──────────────────────────────────────────────────────
# Skill.ux_scores_by_version — 多版本聚合
# ──────────────────────────────────────────────────────

class TestSkillUxByVersion:
    def test_two_versions_grouped_correctly(self, tmp_path):
        d = _make_own_skill(tmp_path / "foo", "foo")
        old_sha = "old111" + "0" * 34
        new_sha = "new222" + "0" * 34
        # 旧版本 2 条 (8, 6) → avg 7.0
        _write_ux_score(d, atom_id="a1", side="main", commit_sha=old_sha,
                        score=8, scored_at="2026-01-01T00:00:00+00:00")
        _write_ux_score(d, atom_id="a2", side="main", commit_sha=old_sha,
                        score=6, scored_at="2026-01-02T00:00:00+00:00")
        # 新版本 3 条 (9, 7, 8) → avg 8.0
        _write_ux_score(d, atom_id="a3", side="main", commit_sha=new_sha,
                        score=9, scored_at="2026-02-01T00:00:00+00:00")
        _write_ux_score(d, atom_id="a4", side="main", commit_sha=new_sha,
                        score=7, scored_at="2026-02-02T00:00:00+00:00")
        _write_ux_score(d, atom_id="a5", side="main", commit_sha=new_sha,
                        score=8, scored_at="2026-02-03T00:00:00+00:00")

        sk = Skill(d)
        versions = sk.ux_scores_by_version(side="main", days=0)
        assert len(versions) == 2
        # 按 last_scored_at 降序 → 新版本在前
        assert versions[0]["commit_sha"] == new_sha
        assert versions[1]["commit_sha"] == old_sha
        # 新版本组
        assert versions[0]["count"] == 3
        assert versions[0]["avg"] == 8.0
        assert versions[0]["side"] == "main"
        assert versions[0]["last_scored_at"] == "2026-02-03T00:00:00+00:00"
        assert versions[0]["first_scored_at"] == "2026-02-01T00:00:00+00:00"
        # 旧版本组
        assert versions[1]["count"] == 2
        assert versions[1]["avg"] == 7.0

    def test_side_none_merges_sides_labels_mixed(self, tmp_path):
        d = _make_own_skill(tmp_path / "foo", "foo")
        sha = "shared_sha_abcdef"
        # 同一 sha 上 main + staging 各一条 → 合并成一组，side 标 "mixed"
        _write_ux_score(d, atom_id="a1", side="main", commit_sha=sha,
                        score=8, scored_at="2026-02-01T00:00:00+00:00")
        _write_ux_score(d, atom_id="a2", side="staging", commit_sha=sha,
                        score=6, scored_at="2026-02-02T00:00:00+00:00")
        versions = Skill(d).ux_scores_by_version(side=None, days=0)
        assert len(versions) == 1
        assert versions[0]["commit_sha"] == sha
        assert versions[0]["count"] == 2
        assert versions[0]["avg"] == 7.0
        assert versions[0]["side"] == "mixed"

    def test_empty_returns_empty_list(self, tmp_path):
        d = _make_own_skill(tmp_path / "foo", "foo")
        assert Skill(d).ux_scores_by_version(days=0) == []


# ──────────────────────────────────────────────────────
# Skill.ux_avg — 按当前版本 sha 过滤
# ──────────────────────────────────────────────────────

class TestSkillUxAvgFiltersByCurrentSha:
    def test_only_current_version_scores_counted(self, tmp_path):
        d = _make_own_skill(tmp_path / "foo", "foo")
        cur_sha = Skill(d).canary_ops.main_sha()
        old_sha = "old000" + "0" * 34
        # 旧版本 2 条 (10, 10) → 若混算会拉高均分
        _write_ux_score(d, atom_id="a1", side="main", commit_sha=old_sha,
                        score=10, scored_at="2026-01-01T00:00:00+00:00")
        _write_ux_score(d, atom_id="a2", side="main", commit_sha=old_sha,
                        score=10, scored_at="2026-01-02T00:00:00+00:00")
        # 当前版本 2 条 (6, 8) → avg 7.0
        _write_ux_score(d, atom_id="a3", side="main", commit_sha=cur_sha,
                        score=6, scored_at="2026-02-01T00:00:00+00:00")
        _write_ux_score(d, atom_id="a4", side="main", commit_sha=cur_sha,
                        score=8, scored_at="2026-02-02T00:00:00+00:00")
        assert Skill(d).ux_avg(side="main", days=0) == 7.0

    def test_no_current_version_scores_returns_none(self, tmp_path):
        d = _make_own_skill(tmp_path / "foo", "foo")
        old_sha = "old000" + "0" * 34
        _write_ux_score(d, atom_id="a1", side="main", commit_sha=old_sha,
                        score=10, scored_at="2026-01-01T00:00:00+00:00")
        # 当前 main_sha 上无分 → None（不混旧版本）
        assert Skill(d).ux_avg(side="main", days=0) is None

    def test_staging_side_uses_staging_sha(self, tmp_path):
        d = _make_own_skill(tmp_path / "foo", "foo")
        _add_staging(d)
        sk = Skill(d)
        m_sha = sk.canary_ops.main_sha()
        s_sha = sk.canary_ops.staging_sha()
        assert s_sha is not None and s_sha != m_sha
        _write_ux_score(d, atom_id="a1", side="main", commit_sha=m_sha,
                        score=3, scored_at="2026-02-01T00:00:00+00:00")
        _write_ux_score(d, atom_id="a2", side="staging", commit_sha=s_sha,
                        score=9, scored_at="2026-02-02T00:00:00+00:00")
        _write_ux_score(d, atom_id="a3", side="staging", commit_sha=s_sha,
                        score=7, scored_at="2026-02-03T00:00:00+00:00")
        assert sk.ux_avg(side="staging", days=0) == 8.0
        assert sk.ux_avg(side="main", days=0) == 3.0

    def test_staging_side_no_staging_returns_none(self, tmp_path):
        d = _make_own_skill(tmp_path / "foo", "foo")
        assert Skill(d).ux_avg(side="staging", days=0) is None

    def test_side_none_merges_current_main_and_staging(self, tmp_path):
        d = _make_own_skill(tmp_path / "foo", "foo")
        _add_staging(d)
        sk = Skill(d)
        m_sha = sk.canary_ops.main_sha()
        s_sha = sk.canary_ops.staging_sha()
        _write_ux_score(d, atom_id="a1", side="main", commit_sha=m_sha,
                        score=6, scored_at="2026-02-01T00:00:00+00:00")
        _write_ux_score(d, atom_id="a2", side="staging", commit_sha=s_sha,
                        score=8, scored_at="2026-02-02T00:00:00+00:00")
        # 两侧合并：当前版本分一起算 → (6+8)/2 = 7.0
        assert sk.ux_avg(side=None, days=0) == 7.0

    def test_unknown_side_raises(self, tmp_path):
        d = _make_own_skill(tmp_path / "foo", "foo")
        with pytest.raises(ValueError):
            Skill(d).ux_avg(side="weird", days=0)


# ──────────────────────────────────────────────────────
# Skill.ux_scores_with_atoms — atom 关联
# ──────────────────────────────────────────────────────

class TestSkillUxScoresWithAtoms:
    def test_atom_present_returns_fields(self, tmp_path):
        d = _make_own_skill(tmp_path / "foo", "foo")
        sha = Skill(d).canary_ops.main_sha()
        _write_ux_score(d, atom_id="atom_t_0001", side="main", commit_sha=sha,
                        score=8, reasons="ok", user_model="m1",
                        scored_at="2026-02-01T00:00:00+00:00")
        traj_root = tmp_path / "traj"
        _write_atom(traj_root, "cid-1", "traj_t", "atom_t_0001",
                    summary="做 X", intent="想 X", tags=["t1"],
                    used_skills=["foo"])
        rows = Skill(d).ux_scores_with_atoms(
            side="main", days=0, traj_root=traj_root)
        assert len(rows) == 1
        r = rows[0]
        assert r["atom_id"] == "atom_t_0001"
        assert r["score"] == 8.0
        assert r["reasons"] == "ok"
        assert r["user_model"] == "m1"
        assert r["atom"] is not None
        assert r["atom"]["summary"] == "做 X"
        assert r["atom"]["intent"] == "想 X"
        assert r["atom"]["tags"] == ["t1"]
        assert r["atom"]["used_skills"] == ["foo"]
        assert r["atom"]["traj_id"] == "traj_t"

    def test_atom_missing_returns_none(self, tmp_path):
        d = _make_own_skill(tmp_path / "foo", "foo")
        sha = Skill(d).canary_ops.main_sha()
        _write_ux_score(d, atom_id="atom_ghost", side="main", commit_sha=sha,
                        score=8, scored_at="2026-02-01T00:00:00+00:00")
        traj_root = tmp_path / "traj"
        traj_root.mkdir()
        # atom 文件不存在 → atom=None
        rows = Skill(d).ux_scores_with_atoms(
            side="main", days=0, traj_root=traj_root)
        assert len(rows) == 1
        assert rows[0]["atom"] is None

    def test_traj_root_none_atom_always_none(self, tmp_path):
        d = _make_own_skill(tmp_path / "foo", "foo")
        sha = Skill(d).canary_ops.main_sha()
        _write_ux_score(d, atom_id="a1", side="main", commit_sha=sha,
                        score=8, scored_at="2026-02-01T00:00:00+00:00")
        rows = Skill(d).ux_scores_with_atoms(side="main", days=0)
        assert rows[0]["atom"] is None

    def test_commit_sha_filter(self, tmp_path):
        d = _make_own_skill(tmp_path / "foo", "foo")
        sha_a = "aaaa1111"
        sha_b = "bbbb2222"
        _write_ux_score(d, atom_id="a1", side="main", commit_sha=sha_a,
                        score=8, scored_at="2026-02-01T00:00:00+00:00")
        _write_ux_score(d, atom_id="a2", side="main", commit_sha=sha_b,
                        score=6, scored_at="2026-02-02T00:00:00+00:00")
        rows = Skill(d).ux_scores_with_atoms(
            side="main", commit_sha=sha_a, days=0)
        assert len(rows) == 1
        assert rows[0]["commit_sha"] == sha_a


# ──────────────────────────────────────────────────────
# SkillHub — 版本聚合 + ux_avg 过滤 + atom 关联
# ──────────────────────────────────────────────────────

class TestSkillHubUxQuery:
    def test_by_version_groups_by_content_sha(self, tmp_path):
        hub_dir = tmp_path / "hub"
        sub = _write_hub_skill(hub_dir, "ext")
        cur_sha = _content_sha(sub / "SKILL.md")
        old_sha = "old1234567890abc"
        _write_ux_score(sub, atom_id="a1", side="main", commit_sha=old_sha,
                        score=10, scored_at="2026-01-01T00:00:00+00:00")
        _write_ux_score(sub, atom_id="a2", side="main", commit_sha=cur_sha,
                        score=6, scored_at="2026-02-01T00:00:00+00:00")
        _write_ux_score(sub, atom_id="a3", side="main", commit_sha=cur_sha,
                        score=8, scored_at="2026-02-02T00:00:00+00:00")
        hub = SkillHub(enabled=True, hub_dir=hub_dir, embed_client=None)
        versions = hub.ux_scores_by_version("ext", days=0)
        assert len(versions) == 2
        # 当前版本在前（last_scored_at 更晚）
        assert versions[0]["commit_sha"] == cur_sha
        assert versions[0]["count"] == 2
        assert versions[0]["avg"] == 7.0
        assert versions[1]["commit_sha"] == old_sha

    def test_ux_avg_filters_by_current_content_sha(self, tmp_path):
        hub_dir = tmp_path / "hub"
        sub = _write_hub_skill(hub_dir, "ext")
        cur_sha = _content_sha(sub / "SKILL.md")
        old_sha = "old1234567890abc"
        _write_ux_score(sub, atom_id="a1", side="main", commit_sha=old_sha,
                        score=10, scored_at="2026-01-01T00:00:00+00:00")
        _write_ux_score(sub, atom_id="a2", side="main", commit_sha=cur_sha,
                        score=6, scored_at="2026-02-01T00:00:00+00:00")
        hub = SkillHub(enabled=True, hub_dir=hub_dir, embed_client=None)
        # 只算当前版本 → 6.0，不混旧版本的 10
        assert hub.ux_avg("ext", days=0) == 6.0

    def test_with_atoms_present_and_missing(self, tmp_path):
        hub_dir = tmp_path / "hub"
        sub = _write_hub_skill(hub_dir, "ext")
        sha = _content_sha(sub / "SKILL.md")
        _write_ux_score(sub, atom_id="atom_t_0001", side="main", commit_sha=sha,
                        score=8, scored_at="2026-02-01T00:00:00+00:00")
        _write_ux_score(sub, atom_id="atom_ghost", side="main", commit_sha=sha,
                        score=6, scored_at="2026-02-02T00:00:00+00:00")
        traj_root = tmp_path / "traj"
        _write_atom(traj_root, "cid-1", "traj_t", "atom_t_0001",
                    summary="做 X", intent="想 X")
        hub = SkillHub(enabled=True, hub_dir=hub_dir, embed_client=None)
        rows = hub.ux_scores_with_atoms("ext", days=0, traj_root=traj_root)
        by_atom = {r["atom_id"]: r for r in rows}
        assert by_atom["atom_t_0001"]["atom"] is not None
        assert by_atom["atom_t_0001"]["atom"]["summary"] == "做 X"
        assert by_atom["atom_ghost"]["atom"] is None

    def test_skill_not_found_returns_empty(self, tmp_path):
        hub_dir = tmp_path / "hub"
        hub_dir.mkdir()
        hub = SkillHub(enabled=True, hub_dir=hub_dir, embed_client=None)
        assert hub.ux_scores_by_version("nope", days=0) == []
        assert hub.ux_scores_with_atoms("nope", days=0) == []


# ──────────────────────────────────────────────────────
# HTTP 端点 — GET /api/v1/dashboard/skill/{name}/ux + skillhub
# ──────────────────────────────────────────────────────

class TestHttpUxEndpoints:
    def _seed_skill_with_ux(self, tmp_path):
        """建一个有 git 的 skill + 两条 ux 分（新旧版本各一），返回 (db, sha)。"""
        skill_dir = tmp_path / "skill"
        d = _make_own_skill(skill_dir / "foo", "foo")
        cur_sha = Skill(d).canary_ops.main_sha()
        old_sha = "old000" + "0" * 34
        _write_ux_score(d, atom_id="a1", side="main", commit_sha=old_sha,
                        score=10, scored_at="2026-01-01T00:00:00+00:00")
        _write_ux_score(d, atom_id="a2", side="main", commit_sha=cur_sha,
                        score=8, scored_at="2026-02-01T00:00:00+00:00")
        return cur_sha, old_sha

    def test_skill_ux_endpoint_structure(self, tmp_path):
        cur_sha, _old_sha = self._seed_skill_with_ux(tmp_path)
        # 指向 tmp_path/skill 让 _skill_dir_for(db_path) 解析到正确目录
        db = tmp_path / "registry.db"
        app = FastAPI()
        app.include_router(build_dashboard_router(db_path=db))
        client = TestClient(app)
        r = client.get("/api/v1/dashboard/skill/foo/ux", params={"days": 0})
        assert r.status_code == 200
        body = r.json()
        assert body["skill"] == "foo"
        assert "versions" in body
        assert "current_version" in body
        assert body["current_version"]["main"] == cur_sha
        assert body["current_version"]["staging"] is None
        # 两个版本组（旧 + 当前）
        shas = {v["commit_sha"] for v in body["versions"]}
        assert cur_sha in shas

    def test_skill_ux_atoms_endpoint_unavailable_when_no_traj_root(self, tmp_path):
        self._seed_skill_with_ux(tmp_path)
        db = tmp_path / "registry.db"
        app = FastAPI()
        app.include_router(build_dashboard_router(db_path=db))
        client = TestClient(app)
        # 没有 team server 的 traj_root → atom_lookup=unavailable, atom=None
        r = client.get("/api/v1/dashboard/skill/foo/ux/atoms", params={"days": 0})
        assert r.status_code == 200
        body = r.json()
        assert body["skill"] == "foo"
        assert body["atom_lookup"] == "unavailable"
        assert all(s["atom"] is None for s in body["scores"])

    def test_skill_ux_atoms_endpoint_with_traj_root(self, tmp_path, monkeypatch):
        cur_sha, _ = self._seed_skill_with_ux(tmp_path)
        # 给 skill 写一条带 atom_id 的 ux 分
        d = tmp_path / "skill" / "foo"
        _write_ux_score(d, atom_id="atom_t_0001", side="main", commit_sha=cur_sha,
                        score=9, scored_at="2026-02-03T00:00:00+00:00")
        # 伪造 team server traj_root 结构（clients/ 子目录）
        traj_root = tmp_path / "team_traj"
        _write_atom(traj_root, "cid-1", "traj_t", "atom_t_0001",
                    summary="做 X", intent="想 X")
        # monkeypatch _resolve_traj_root 返回该 traj_root
        import xskill.dashboard.router as router_mod
        monkeypatch.setattr(router_mod, "_resolve_traj_root", lambda: traj_root)
        db = tmp_path / "registry.db"
        app = FastAPI()
        app.include_router(build_dashboard_router(db_path=db))
        client = TestClient(app)
        r = client.get("/api/v1/dashboard/skill/foo/ux/atoms", params={"days": 0})
        assert r.status_code == 200
        body = r.json()
        assert body["atom_lookup"] == "ok"
        scored = {s["atom_id"]: s for s in body["scores"]}
        assert scored["atom_t_0001"]["atom"] is not None
        assert scored["atom_t_0001"]["atom"]["summary"] == "做 X"

    def test_skill_ux_nonexistent_skill_400(self, tmp_path):
        db = tmp_path / "registry.db"
        app = FastAPI()
        app.include_router(build_dashboard_router(db_path=db))
        client = TestClient(app)
        # skill 目录不存在 → _skill_path 校验抛 400
        r = client.get("/api/v1/dashboard/skill/nonexistent/ux")
        assert r.status_code == 400

    def test_skillhub_ux_endpoint(self, tmp_path, monkeypatch):
        # 配置 skillhub.enabled + dir，让 _build_skillhub 能读到
        hub_dir = tmp_path / "hub"
        sub = _write_hub_skill(hub_dir, "ext")
        cur_sha = _content_sha(sub / "SKILL.md")
        old_sha = "old1234567890abc"
        _write_ux_score(sub, atom_id="a1", side="main", commit_sha=old_sha,
                        score=10, scored_at="2026-01-01T00:00:00+00:00")
        _write_ux_score(sub, atom_id="a2", side="main", commit_sha=cur_sha,
                        score=8, scored_at="2026-02-01T00:00:00+00:00")
        monkeypatch.setattr(
            "xskill.config.get_config",
            lambda: {"skillhub": {"enabled": True, "dir": str(hub_dir)}})
        db = tmp_path / "registry.db"
        app = FastAPI()
        app.include_router(build_dashboard_router(db_path=db))
        client = TestClient(app)
        r = client.get("/api/v1/dashboard/skillhub/ext/ux", params={"days": 0})
        assert r.status_code == 200
        body = r.json()
        assert body["skill"] == "ext"
        assert body["current_version"]["content_sha"] == cur_sha
        shas = {v["commit_sha"] for v in body["versions"]}
        assert cur_sha in shas

    def test_skillhub_ux_endpoint_disabled_404(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "xskill.config.get_config",
            lambda: {"skillhub": {"enabled": False}})
        db = tmp_path / "registry.db"
        app = FastAPI()
        app.include_router(build_dashboard_router(db_path=db))
        client = TestClient(app)
        r = client.get("/api/v1/dashboard/skillhub/ext/ux")
        assert r.status_code == 404

    def test_skillhub_ux_atoms_endpoint(self, tmp_path, monkeypatch):
        hub_dir = tmp_path / "hub"
        sub = _write_hub_skill(hub_dir, "ext")
        sha = _content_sha(sub / "SKILL.md")
        _write_ux_score(sub, atom_id="atom_t_0001", side="main", commit_sha=sha,
                        score=8, scored_at="2026-02-01T00:00:00+00:00")
        traj_root = tmp_path / "team_traj"
        _write_atom(traj_root, "cid-1", "traj_t", "atom_t_0001",
                    summary="做 X", intent="想 X")
        monkeypatch.setattr(
            "xskill.config.get_config",
            lambda: {"skillhub": {"enabled": True, "dir": str(hub_dir)}})
        import xskill.dashboard.router as router_mod
        monkeypatch.setattr(router_mod, "_resolve_traj_root", lambda: traj_root)
        db = tmp_path / "registry.db"
        app = FastAPI()
        app.include_router(build_dashboard_router(db_path=db))
        client = TestClient(app)
        r = client.get("/api/v1/dashboard/skillhub/ext/ux/atoms",
                       params={"days": 0})
        assert r.status_code == 200
        body = r.json()
        assert body["atom_lookup"] == "ok"
        assert body["scores"][0]["atom"] is not None
        assert body["scores"][0]["atom"]["summary"] == "做 X"
