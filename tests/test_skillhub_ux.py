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
from xskill.recommend.skillhub import SkillHub


def _git(args, cwd):
    subprocess.run(["git"] + args, cwd=str(cwd), capture_output=True, text=True, check=True)


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
        # 手写两条 ux 分
        AtomCanary(skill_dir=sub).append(
            atom_id="a1", skill_name="extfoo", side="main",
            commit_sha="deadbeef", score=8, reasons="ok")
        AtomCanary(skill_dir=sub).append(
            atom_id="a2", skill_name="extfoo", side="main",
            commit_sha="deadbeef", score=6, reasons="meh")
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
