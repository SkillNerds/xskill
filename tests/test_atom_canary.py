"""AtomCanary：用 atom_id 替代 traj_id 作为 ux_scores 主键"""
from __future__ import annotations

from pathlib import Path

import pytest

from xskill import canary
from xskill.canary import AtomCanary
from xskill.skill.git import run_git


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    run_git(["init"], cwd=str(path))
    run_git(["checkout", "-b", "main"], cwd=str(path))
    run_git(["config", "user.email", "t@t"], cwd=str(path))
    run_git(["config", "user.name", "t"], cwd=str(path))
    (path / "SKILL.md").write_text("v1", encoding="utf-8")
    run_git(["add", "-A"], cwd=str(path))
    run_git(["commit", "-m", "init"], cwd=str(path))
    return path


def _commit(path: Path, content: str, msg: str):
    (path / "SKILL.md").write_text(content, encoding="utf-8")
    run_git(["add", "-A"], cwd=str(path))
    run_git(["commit", "-m", msg], cwd=str(path))


class TestAppendIdempotent:
    def test_first_append_writes(self, tmp_path):
        skill_dir = _init_repo(tmp_path / "skill_a")
        ac = AtomCanary(skill_dir=skill_dir)
        wrote = ac.append(
            atom_id="atom_x_0001", skill_name="skill_a",
            side="main", commit_sha="deadbeef", score=8, reasons="ok",
        )
        assert wrote is True

    def test_duplicate_returns_false(self, tmp_path):
        skill_dir = _init_repo(tmp_path / "skill_b")
        ac = AtomCanary(skill_dir=skill_dir)
        ac.append(atom_id="a1", skill_name="skill_b",
                  side="main", commit_sha="x", score=7, reasons="")
        dup = ac.append(atom_id="a1", skill_name="skill_b",
                        side="main", commit_sha="x", score=7, reasons="")
        assert dup is False

    def test_record_contains_atom_id_not_traj_id(self, tmp_path):
        skill_dir = _init_repo(tmp_path / "skill_c")
        ac = AtomCanary(skill_dir=skill_dir)
        ac.append(atom_id="atom_unique_42", skill_name="skill_c",
                  side="main", commit_sha="abc", score=6, reasons="")
        # 直接读 jsonl 文件验证 key
        import json
        p = skill_dir / canary.UX_SCORES_FILENAME
        line = p.read_text(encoding="utf-8").strip().splitlines()[0]
        rec = json.loads(line)
        assert rec["atom_id"] == "atom_unique_42"
        assert "traj_id" not in rec


class TestRecent:
    def test_filters_by_side_and_commit_sha(self, tmp_path):
        skill_dir = _init_repo(tmp_path / "skill_r")
        ac = AtomCanary(skill_dir=skill_dir)
        # 写多条不同 side / sha
        for i in range(3):
            ac.append(atom_id=f"m{i}", skill_name="skill_r",
                      side="main", commit_sha="X", score=7, reasons="")
        for i in range(2):
            ac.append(atom_id=f"s{i}", skill_name="skill_r",
                      side="staging", commit_sha="Y", score=9, reasons="")
        ac.append(atom_id="m_old", skill_name="skill_r",
                  side="main", commit_sha="OLD", score=4, reasons="")

        main_recent = ac.recent(side="main", commit_sha="X", n=5)
        assert len(main_recent) == 3
        assert all(r["side"] == "main" and r["commit_sha"] == "X"
                   for r in main_recent)
        staging_recent = ac.recent(side="staging", commit_sha="Y", n=5)
        assert len(staging_recent) == 2


class TestCheckAndDecide:
    def test_no_staging_returns_no_staging(self, tmp_path):
        skill_dir = _init_repo(tmp_path / "skill_n")
        ac = AtomCanary(skill_dir=skill_dir)
        result = ac.check_and_decide()
        assert result["action"] == "no_staging"

    def test_promoted_when_staging_average_higher(self, tmp_path):
        skill_dir = _init_repo(tmp_path / "skill_p")
        # 建 staging 分支领先 main
        run_git(["checkout", "-b", "staging"], cwd=str(skill_dir))
        _commit(skill_dir, "v2-staging", "staging v2")
        # 切回 main 不动
        run_git(["checkout", "main"], cwd=str(skill_dir))

        main_sha = canary.main_sha(skill_dir)
        staging_sha = canary.staging_sha(skill_dir)

        ac = AtomCanary(skill_dir=skill_dir)
        # 5 条 main + 5 条 staging，staging 均分更高
        for i in range(5):
            ac.append(atom_id=f"m{i}", skill_name="skill_p",
                      side="main", commit_sha=main_sha, score=6, reasons="")
            ac.append(atom_id=f"s{i}", skill_name="skill_p",
                      side="staging", commit_sha=staging_sha, score=9, reasons="")
        # 纯 ux 翻牌机制测试：无 val 分，val_block=False 走旧回退路径
        result = ac.check_and_decide(config=canary.CanaryConfig(val_block=False))
        assert result["action"] == "promoted"
