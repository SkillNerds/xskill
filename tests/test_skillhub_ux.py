"""test_skillhub_ux.py — §7 三方 skillhub skill 的 ux 打分 + 查询

覆盖三个缺口：
1. ux 打分定位：runner 的 ``_score_atoms_for_traj`` / ``_score_atoms_for_traj_server``
   在自有 ``skill_dir`` 找不到 skill 时回退查 ``skillhub_dir``，对三方 skill 打分
   落盘到 ``skillhub_dir/<name>/.ux_scores.jsonl``（side 恒 main、sha = 内容哈希）。
2. ux 查询：``SkillHub.ux_avg`` / ``recent_ux_scores`` 读回正确。
3. canary 判定：三方 skill 无 git/staging → ``check_and_decide`` 返回 ``no_staging``，
   不抛异常、不进 staging 灰度。
"""
from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

from xskill.canary import AtomCanary, check_and_decide, load_ux_scores
from xskill.pipeline.atom import AtomTask, AtomTaskStore
from xskill.pipeline.runner import DirectoryWatcher
from xskill.recommend.skillhub import SkillHub


def _git(args, cwd):
    subprocess.run(["git"] + args, cwd=str(cwd), capture_output=True, text=True, check=True)


def _make_own_skill(skill_dir: Path, name: str) -> Path:
    """初始化一个有 git 的自有 skill（main 分支 + 一次 commit）。"""
    d = skill_dir / name
    d.mkdir(parents=True)
    _git(["init", "-q"], d); _git(["checkout", "-q", "-b", "main"], d)
    _git(["config", "user.email", "t@t"], d); _git(["config", "user.name", "t"], d)
    (d / "SKILL.md").write_text(f"# {name}", encoding="utf-8")
    _git(["add", "."], d); _git(["commit", "-q", "-m", "v1"], d)
    return d


def _write_hub_skill(hub_dir: Path, name: str, desc: str = "三方 skill") -> Path:
    """写一个无 git 的三方 skill（仅 SKILL.md）。"""
    d = hub_dir / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {desc}\n---\n# {name}\n",
        encoding="utf-8")
    return d


def _expected_content_sha(skill_md: Path) -> str:
    return hashlib.sha256(skill_md.read_bytes()).hexdigest()[:16]


# ── SkillHub 查询接口 ─────────────────────────────────────────────

class TestSkillHubUxQuery:
    def test_ux_avg_and_recent_scores_readback(self, tmp_path):
        hub_dir = tmp_path / "hub"
        sub = _write_hub_skill(hub_dir, "extfoo")
        hub = SkillHub(enabled=True, hub_dir=hub_dir, embed_client=None)
        # 手写两条 ux 分；commit_sha 必须匹配当前 SKILL.md 内容哈希，
        # 否则 ux_avg（按当前版本 content_sha 过滤）会返回 None。
        sha = _expected_content_sha(sub / "SKILL.md")
        AtomCanary(skill_dir=sub).append(
            atom_id="a1", skill_name="extfoo", side="main",
            commit_sha=sha, score=8, reasons="ok")
        AtomCanary(skill_dir=sub).append(
            atom_id="a2", skill_name="extfoo", side="main",
            commit_sha=sha, score=6, reasons="meh")
        rows = hub.recent_ux_scores("extfoo", days=0)
        assert len(rows) == 2
        assert {r["atom_id"] for r in rows} == {"a1", "a2"}
        assert hub.ux_avg("extfoo", days=0) == 7.0

    def test_ux_avg_none_when_no_scores(self, tmp_path):
        hub_dir = tmp_path / "hub"
        _write_hub_skill(hub_dir, "extfoo")
        hub = SkillHub(enabled=True, hub_dir=hub_dir, embed_client=None)
        assert hub.ux_avg("extfoo", days=0) is None
        assert hub.recent_ux_scores("extfoo", days=0) == []

    def test_ux_avg_none_when_only_old_version_scores(self, tmp_path):
        """旧版本（不匹配当前 content_sha）的分不应混进 ux_avg → None。"""
        hub_dir = tmp_path / "hub"
        sub = _write_hub_skill(hub_dir, "extfoo")
        hub = SkillHub(enabled=True, hub_dir=hub_dir, embed_client=None)
        AtomCanary(skill_dir=sub).append(
            atom_id="a1", skill_name="extfoo", side="main",
            commit_sha="old_version_sha", score=8, reasons="ok")
        assert hub.ux_avg("extfoo", days=0) is None

    def test_skill_path_none_when_disabled(self, tmp_path):
        hub_dir = tmp_path / "hub"
        _write_hub_skill(hub_dir, "extfoo")
        hub = SkillHub(enabled=False, hub_dir=hub_dir, embed_client=None)
        assert hub.skill_path("extfoo") is None
        assert hub.content_sha("extfoo") is None
        assert hub.recent_ux_scores("extfoo") == []
        assert hub.ux_avg("extfoo") is None

    def test_skill_path_none_when_missing(self, tmp_path):
        hub_dir = tmp_path / "hub"
        hub_dir.mkdir()
        hub = SkillHub(enabled=True, hub_dir=hub_dir, embed_client=None)
        assert hub.skill_path("nope") is None
        assert hub.content_sha("nope") is None

    def test_content_sha_matches_skimd(self, tmp_path):
        hub_dir = tmp_path / "hub"
        sub = _write_hub_skill(hub_dir, "extfoo")
        hub = SkillHub(enabled=True, hub_dir=hub_dir, embed_client=None)
        assert hub.content_sha("extfoo") == _expected_content_sha(sub / "SKILL.md")
        assert len(hub.content_sha("extfoo")) == 16


# ── 单机路径：_score_atoms_for_traj 回退 skillhub ─────────────────

class TestSingleMachineScoresSkillhub:
    def test_scores_third_party_skill(self, tmp_path, monkeypatch):
        skill_dir = tmp_path / "skill"; skill_dir.mkdir()
        hub_dir = tmp_path / "hub"
        sub = _write_hub_skill(hub_dir, "extfoo")

        wd = tmp_path / "wd"; wd.mkdir()
        traj_text = (
            "<!-- xskill:skill=extfoo side=staging sha=shouldbeignored -->\n"
            "# body\n"
        )
        (wd / "traj_t.md").write_text(traj_text, encoding="utf-8")
        store = AtomTaskStore(root=wd)
        store.save(AtomTask(
            atom_id="atom_traj_t_0001", traj_id="traj_t",
            offset_start=0, offset_end=2, intent="i", summary="s",
            tags=[], used_skills=["extfoo"], ux_score=None,
            pre_atom_id=None, post_atom_id=None,
            context_prefix="", raw_segment="# body",
        ))

        scored = []

        def _fake_score_atom(*, llm, atom, side):
            scored.append((atom.atom_id, side))
            return {"score": 9, "reasons": "ok"}

        monkeypatch.setattr("xskill.pipeline.atom.score_atom", _fake_score_atom)

        w = DirectoryWatcher(
            llm=object(), skill_dir=skill_dir, store=store,
            config={"skillhub": {"enabled": True, "dir": str(hub_dir)}},
            home_root=tmp_path,
        )
        monkeypatch.setattr(
            "xskill.pipeline.runner.list_watch_dirs",
            lambda **kw: [{"id": 1, "path": str(wd)}])
        w._score_atoms_for_traj(1, "traj_t.md")

        # side 被强制成 main（三方无 staging），sha = 内容哈希
        assert scored == [("atom_traj_t_0001", "main")]
        rows = load_ux_scores(sub)
        assert len(rows) == 1
        assert rows[0]["side"] == "main"
        assert rows[0]["commit_sha"] == _expected_content_sha(sub / "SKILL.md")
        assert rows[0]["score"] == 9.0

    def test_own_skill_takes_priority_over_skillhub(self, tmp_path, monkeypatch):
        """同名 skill 在 skill_dir 与 skillhub_dir 都存在 → 用自有（有 git）那个。"""
        skill_dir = tmp_path / "skill"; skill_dir.mkdir()
        own = _make_own_skill(skill_dir, "dup")
        hub_dir = tmp_path / "hub"
        _write_hub_skill(hub_dir, "dup")

        wd = tmp_path / "wd"; wd.mkdir()
        (wd / "traj_t.md").write_text(
            "<!-- xskill:skill=dup side=main sha=ownsha -->\n# body\n",
            encoding="utf-8")
        store = AtomTaskStore(root=wd)
        store.save(AtomTask(
            atom_id="atom_traj_t_0001", traj_id="traj_t",
            offset_start=0, offset_end=2, intent="i", summary="s",
            tags=[], used_skills=["dup"], ux_score=None,
            pre_atom_id=None, post_atom_id=None,
            context_prefix="", raw_segment="# body",
        ))

        monkeypatch.setattr(
            "xskill.pipeline.atom.score_atom",
            lambda *, llm, atom, side: {"score": 7, "reasons": "ok"})

        w = DirectoryWatcher(
            llm=object(), skill_dir=skill_dir, store=store,
            config={"skillhub": {"enabled": True, "dir": str(hub_dir)}},
            home_root=tmp_path,
        )
        monkeypatch.setattr(
            "xskill.pipeline.runner.list_watch_dirs",
            lambda **kw: [{"id": 1, "path": str(wd)}])
        w._score_atoms_for_traj(1, "traj_t.md")

        # 自有 skill 目录有记录、三方目录无记录
        assert len(load_ux_scores(own)) == 1
        assert not (hub_dir / "dup" / ".ux_scores.jsonl").is_file()

    def test_neither_found_skips_silently(self, tmp_path, monkeypatch):
        skill_dir = tmp_path / "skill"; skill_dir.mkdir()
        hub_dir = tmp_path / "hub"; hub_dir.mkdir()
        wd = tmp_path / "wd"; wd.mkdir()
        (wd / "traj_t.md").write_text(
            "<!-- xskill:skill=ghost side=main sha=x -->\n# body\n",
            encoding="utf-8")
        store = AtomTaskStore(root=wd)
        store.save(AtomTask(
            atom_id="atom_traj_t_0001", traj_id="traj_t",
            offset_start=0, offset_end=2, intent="i", summary="s",
            tags=[], used_skills=["ghost"], ux_score=None,
            pre_atom_id=None, post_atom_id=None,
            context_prefix="", raw_segment="# body",
        ))
        monkeypatch.setattr(
            "xskill.pipeline.atom.score_atom",
            lambda *, llm, atom, side: {"score": 7, "reasons": "ok"})

        w = DirectoryWatcher(
            llm=object(), skill_dir=skill_dir, store=store,
            config={"skillhub": {"enabled": True, "dir": str(hub_dir)}},
            home_root=tmp_path,
        )
        monkeypatch.setattr(
            "xskill.pipeline.runner.list_watch_dirs",
            lambda **kw: [{"id": 1, "path": str(wd)}])
        # 既不在 skill_dir 也不在 skillhub → 不抛、不写
        w._score_atoms_for_traj(1, "traj_t.md")
        assert not (skill_dir / "ghost").exists()


# ── CS 路径：_score_atoms_for_traj_server 回退 skillhub ───────────

class TestServerScoresSkillhub:
    def test_scores_third_party_skill(self, tmp_path, monkeypatch):
        skill_dir = tmp_path / "skill"; skill_dir.mkdir()
        hub_dir = tmp_path / "hub"
        sub = _write_hub_skill(hub_dir, "extfoo")

        sessions = tmp_path / "clients" / "cid-1" / "sessions"
        sessions.mkdir(parents=True)
        (sessions / "traj_cc_x_001.md").write_text("# body", encoding="utf-8")
        store = AtomTaskStore(root=sessions)
        store.save(AtomTask(
            atom_id="atom_traj_cc_x_001_0001", traj_id="traj_cc_x_001",
            offset_start=0, offset_end=6, intent="i", summary="s",
            tags=[], used_skills=["extfoo"], ux_score=None,
            pre_atom_id=None, post_atom_id=None,
            context_prefix="", raw_segment="# body",
        ))

        scored = []

        def _fake_score_atom(*, llm, atom, side):
            scored.append((atom.atom_id, side))
            return {"score": 8, "reasons": "ok"}

        monkeypatch.setattr("xskill.pipeline.atom.score_atom", _fake_score_atom)

        w = DirectoryWatcher(
            llm=object(), skill_dir=skill_dir, store=store,
            config={"canary": {"probability": 0.2},
                    "skillhub": {"enabled": True, "dir": str(hub_dir)}},
            server_mode=True,
        )
        monkeypatch.setattr(
            "xskill.pipeline.runner.list_watch_dirs",
            lambda **kw: [{"id": 1, "path": str(sessions), "label": "cid-1"}])
        monkeypatch.setattr(
            "xskill.pipeline.registry.model_share", lambda **kw: [])
        w._score_atoms_for_traj_server(1, "traj_cc_x_001.md")

        assert scored == [("atom_traj_cc_x_001_0001", "main")]
        rows = load_ux_scores(sub)
        assert len(rows) == 1
        assert rows[0]["side"] == "main"
        assert rows[0]["commit_sha"] == _expected_content_sha(sub / "SKILL.md")
        assert rows[0]["score"] == 8.0

    def test_own_skill_still_uses_git_routing(self, tmp_path, monkeypatch):
        """CS 路径下自有 skill 仍走 has_staging / pick_side_scoped 路由。"""
        skill_dir = tmp_path / "skill"; skill_dir.mkdir()
        own = _make_own_skill(skill_dir, "fix-foo")
        hub_dir = tmp_path / "hub"; hub_dir.mkdir()

        sessions = tmp_path / "clients" / "cid-1" / "sessions"
        sessions.mkdir(parents=True)
        (sessions / "traj_cc.md").write_text("# body", encoding="utf-8")
        store = AtomTaskStore(root=sessions)
        store.save(AtomTask(
            atom_id="atom_traj_cc_0001", traj_id="traj_cc",
            offset_start=0, offset_end=6, intent="i", summary="s",
            tags=[], used_skills=["fix-foo"], ux_score=None,
            pre_atom_id=None, post_atom_id=None,
            context_prefix="", raw_segment="# body",
        ))

        monkeypatch.setattr(
            "xskill.pipeline.atom.score_atom",
            lambda *, llm, atom, side: {"score": 8, "reasons": "ok"})

        w = DirectoryWatcher(
            llm=object(), skill_dir=skill_dir, store=store,
            config={"canary": {"probability": 0.2},
                    "skillhub": {"enabled": True, "dir": str(hub_dir)}},
            server_mode=True,
        )
        monkeypatch.setattr(
            "xskill.pipeline.runner.list_watch_dirs",
            lambda **kw: [{"id": 1, "path": str(sessions), "label": "cid-1"}])
        monkeypatch.setattr(
            "xskill.pipeline.registry.model_share", lambda **kw: [])
        w._score_atoms_for_traj_server(1, "traj_cc.md")

        # 自有 skill 目录有记录、三方目录无 extfoo（hub 空）
        rows = load_ux_scores(own)
        assert len(rows) == 1
        assert rows[0]["side"] == "main"  # 无 staging → main


# ── canary 判定：三方 skill 不进 staging 灰度 ─────────────────────

class TestSkillhubNoCanary:
    def test_check_and_decide_no_staging_on_gitless(self, tmp_path):
        hub_dir = tmp_path / "hub"
        sub = _write_hub_skill(hub_dir, "extfoo")
        # 写几条 ux 分（模拟已打分）
        AtomCanary(skill_dir=sub).append(
            atom_id="a1", skill_name="extfoo", side="main",
            commit_sha="x", score=9, reasons="ok")
        # check_and_decide 不抛、返回 no_staging
        result = check_and_decide(sub)
        assert result["action"] == "no_staging"
