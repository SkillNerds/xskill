"""skill_tools v2 atom-era 工具集单测"""
from __future__ import annotations

from pathlib import Path

import pytest

from xskill.pipeline.atom import AtomTask, AtomTaskStore
from xskill.agents import skill_tools as ST
from tests.test_atom_task_store import _FakeEmbed


def _setup(tmp_path: Path) -> tuple[Path, AtomTaskStore]:
    skill_dir = tmp_path / "skill"
    skill_dir.mkdir()
    store_root = tmp_path / "cc-sessions"
    store_root.mkdir()
    store = AtomTaskStore(root=store_root)
    atom = AtomTask(
        atom_id="atom_x_0001", traj_id="x",
        offset_start=1, offset_end=3,        # 1-based 行号,半开
        intent="修 django migration", summary="跑了 makemigrations 找冲突",
        tags=["django"], used_skills=[], ux_score=7,
        pre_atom_id=None, post_atom_id=None,
        context_prefix="", raw_segment="MIGRATIONS!",
    )
    store.save(atom)
    store.rebuild_vector_index(_FakeEmbed())
    # 一条 5 行的 traj.md，给 read_traj 测试用（read_traj 按行号取片段）
    (store_root / "x.md").write_text(
        "L1\nL2\nL3\nL4\nL5\n", encoding="utf-8")
    ST.init_context_v2(
        skill_dir=skill_dir, store=store,
        embed_client=_FakeEmbed(), traj_root=store_root,
    )
    return skill_dir, store


class TestAtomTaskRead:
    def test_returns_atom_json(self, tmp_path):
        _setup(tmp_path)
        out = ST.atom_task_read("atom_x_0001")
        assert "atom_x_0001" in out
        assert "makemigrations" in out

    def test_not_found_returns_error(self, tmp_path):
        _setup(tmp_path)
        out = ST.atom_task_read("atom_nonexistent")
        assert out.startswith("error")


class TestReadAtomMeta:
    def test_returns_meta_fields(self, tmp_path):
        """元信息含 traj_id / offset / n_lines / intent / summary / tags 等。"""
        import json
        _setup(tmp_path)
        out = ST.read_atom_meta("atom_x_0001")
        meta = json.loads(out)
        assert meta["atom_id"] == "atom_x_0001"
        assert meta["traj_id"] == "x"
        assert meta["offset_start"] == 1
        assert meta["offset_end"] == 3
        assert meta["n_lines"] == 2  # offset_end - offset_start
        assert meta["intent"] == "修 django migration"
        assert meta["summary"] == "跑了 makemigrations 找冲突"
        assert meta["tags"] == ["django"]
        assert "used_skills" in meta
        assert "source_model" in meta

    def test_excludes_raw_segment_and_context_prefix(self, tmp_path):
        """关键：元信息**不含** raw_segment / context_prefix 大字段（防上下文爆炸）。"""
        import json
        _setup(tmp_path)
        out = ST.read_atom_meta("atom_x_0001")
        meta = json.loads(out)
        assert "raw_segment" not in meta
        assert "context_prefix" not in meta
        # raw_segment 的内容 "MIGRATIONS!" 不应出现在返回里
        assert "MIGRATIONS!" not in out

    def test_not_found_returns_error(self, tmp_path):
        _setup(tmp_path)
        out = ST.read_atom_meta("atom_nonexistent")
        assert out.startswith("error")


class TestAtomTaskSearch:
    def test_returns_hits_in_json(self, tmp_path):
        _setup(tmp_path)
        out = ST.atom_task_search("makemigrations")
        assert "atom_x_0001" in out


class TestReadTraj:
    def test_returns_slice(self, tmp_path):
        """按行号半开区间取片段:[1,3) = 第 1、2 行。"""
        _setup(tmp_path)
        out = ST.read_traj("x", offset_start=1, offset_end=3)
        assert out == "L1\nL2\n"

    def test_last_line_reachable(self, tmp_path):
        """末 atom 的 offset_end = 末行号+1,要能取到最后一行。"""
        _setup(tmp_path)  # x.md 共 5 行
        out = ST.read_traj("x", offset_start=5, offset_end=6)
        assert out == "L5\n"

    def test_invalid_range_returns_error(self, tmp_path):
        _setup(tmp_path)
        out = ST.read_traj("x", offset_start=10, offset_end=5)
        assert out.startswith("error")

    def test_zero_start_line_returns_error(self, tmp_path):
        """行号是 1-based,offset_start < 1 非法。"""
        _setup(tmp_path)
        out = ST.read_traj("x", offset_start=0, offset_end=2)
        assert out.startswith("error")

    def test_out_of_bounds_returns_error(self, tmp_path):
        _setup(tmp_path)
        out = ST.read_traj("x", offset_start=1, offset_end=999999)
        assert out.startswith("error")

    def test_nonexistent_traj_returns_error(self, tmp_path):
        _setup(tmp_path)
        out = ST.read_traj("doesnt-exist", offset_start=1, offset_end=3)
        assert out.startswith("error")


class TestNewSkillFolder:
    def test_creates_directory_with_skeleton(self, tmp_path):
        skill_dir, _ = _setup(tmp_path)
        msg = ST.new_skill_folder("my-new-skill", "stub desc")
        assert "created" in msg
        assert (skill_dir / "my-new-skill" / "scripts").is_dir()
        assert (skill_dir / "my-new-skill" / "references").is_dir()

    def test_repeated_call_returns_already_exists(self, tmp_path):
        _setup(tmp_path)
        ST.new_skill_folder("dup-skill", "stub desc")
        msg = ST.new_skill_folder("dup-skill", "stub desc")
        assert "already exists" in msg

    def test_description_required(self, tmp_path):
        """无 description / 空白 desc 应被拒——避免 cluster agent 偷懒。"""
        _setup(tmp_path)
        for bad in ["", "   ", "\n\t"]:
            msg = ST.new_skill_folder("no-desc-skill", bad)
            assert msg.startswith("error"), f"空 desc ({bad!r}) 应被拒"

    def test_description_written_to_stub_skill_md(self, tmp_path):
        """v2: desc 落到 stub SKILL.md frontmatter；同时 baby 分支被初始化。"""
        skill_dir, _ = _setup(tmp_path)
        ST.new_skill_folder(
            "django-migration-fix", "修复 Django manage.py migrate 冲突",
        )
        skill_md = skill_dir / "django-migration-fix" / "SKILL.md"
        assert skill_md.is_file(), "stub SKILL.md 必须由 new_skill_folder 写出"
        from xskill.skill.frontmatter import parse as fm_parse
        fm, body = fm_parse(skill_md.read_text(encoding="utf-8"))
        assert fm["name"] == "django-migration-fix"
        assert fm["description"] == "修复 Django manage.py migrate 冲突"
        assert fm["metadata"]["state"] == "baby"
        # git 仓库 + baby 分支
        from xskill.skill.git import current_branch
        assert (skill_dir / "django-migration-fix" / ".git").is_dir()
        assert current_branch(str(skill_dir / "django-migration-fix")) == "baby"
        # .gitignore 含 .candidates.yml
        gi = (skill_dir / "django-migration-fix" / ".gitignore").read_text(encoding="utf-8")
        assert ".candidates.yml" in gi
        assert ".ux_scores.jsonl" in gi
        assert ".canary/" in gi


class TestAddTaskToSkill:
    def test_first_add_creates_entry(self, tmp_path):
        skill_dir, _ = _setup(tmp_path)
        ST.new_skill_folder("auto-skill", "stub desc")
        msg = ST.add_task_to_skill("auto-skill", "atom_x_0001", 6)
        assert "buffer_total=6" in msg

    def test_repeated_add_overwrites(self, tmp_path):
        """v2.1: 同 atom 重复 add → 覆盖 weightscore（不累加）。"""
        _setup(tmp_path)
        ST.new_skill_folder("auto-skill", "stub desc")
        ST.add_task_to_skill("auto-skill", "atom_x_0001", 4)
        msg = ST.add_task_to_skill("auto-skill", "atom_x_0001", 5)
        assert "weightscore=5" in msg
        # buffer 仍是单条 atom，total 是覆盖后的 5（不是 4+5=9）
        assert "buffer_total=5" in msg
        assert "overwrite" in msg

    def test_nonexistent_skill_returns_error(self, tmp_path):
        _setup(tmp_path)
        msg = ST.add_task_to_skill("nonexistent", "atom_x_0001", 5)
        assert msg.startswith("error")

    def test_weightscore_out_of_range_returns_error(self, tmp_path):
        _setup(tmp_path)
        ST.new_skill_folder("auto-skill", "stub desc")
        for bad in [0, 11, -1, 100]:
            msg = ST.add_task_to_skill("auto-skill", "atom_x_0001", bad)
            assert msg.startswith("error")


class TestScoreTask:
    def test_score_task_updates_atom(self, tmp_path):
        _, store = _setup(tmp_path)
        msg = ST.score_task("atom_x_0001", 9)
        assert "9" in msg
        assert store.load("atom_x_0001").ux_score == 9

    def test_invalid_score_returns_error(self, tmp_path):
        _setup(tmp_path)
        for bad in [0, 11, -3]:
            msg = ST.score_task("atom_x_0001", bad)
            assert msg.startswith("error")


class TestSkillRead:
    def test_empty_skill_returns_placeholder(self, tmp_path):
        _setup(tmp_path)
        ST.new_skill_folder("brand-new", "stub desc")
        msg = ST.skill_read("brand-new")
        assert "no SKILL.md" in msg or "placeholder" in msg

    def test_existing_skill_returns_content(self, tmp_path):
        skill_dir, _ = _setup(tmp_path)
        ST.new_skill_folder("has-content", "has content")
        (skill_dir / "has-content" / "SKILL.md").write_text(
            "---\nname: has-content\n---\n# body here\n", encoding="utf-8",
        )
        msg = ST.skill_read("has-content")
        assert "body here" in msg


class TestRenameSkill:
    def test_rename_baby_succeeds(self, tmp_path):
        skill_dir, _ = _setup(tmp_path)
        ST.new_skill_folder("old-name", "stub")
        result = ST.rename_skill("old-name", "new-name")
        assert "renamed" in result
        assert (skill_dir / "new-name").is_dir()
        assert not (skill_dir / "old-name").exists()
        # SKILL.md frontmatter.name 已更新
        from xskill.skill.frontmatter import parse as fm_parse
        fm, _ = fm_parse((skill_dir / "new-name" / "SKILL.md").read_text(encoding="utf-8"))
        assert fm["name"] == "new-name"

    def test_rename_to_existing_target_fails(self, tmp_path):
        _setup(tmp_path)
        ST.new_skill_folder("source", "stub")
        ST.new_skill_folder("target", "stub")
        result = ST.rename_skill("source", "target")
        assert result.startswith("error")
        assert "已存在" in result

    def test_rename_main_state_fails(self, tmp_path):
        skill_dir, _ = _setup(tmp_path)
        ST.new_skill_folder("graduated", "stub")
        # graduate 到 main
        from xskill.skill.git import run_git
        run_git(["branch", "-m", "baby", "main"], cwd=str(skill_dir / "graduated"))
        result = ST.rename_skill("graduated", "new-name")
        assert result.startswith("error")
        assert "baby" in result

    def test_rename_nonexistent_skill_fails(self, tmp_path):
        _setup(tmp_path)
        result = ST.rename_skill("nonexistent", "new")
        assert result.startswith("error")

    def test_rename_to_same_name_noop(self, tmp_path):
        _setup(tmp_path)
        ST.new_skill_folder("same", "stub")
        result = ST.rename_skill("same", "same")
        assert "noop" in result


class TestReadSkillTasks:
    def test_reads_candidates_with_weightscore(self, tmp_path):
        _setup(tmp_path)
        ST.new_skill_folder("buf", "stub")
        ST.add_task_to_skill("buf", "atom_a", 6)
        ST.add_task_to_skill("buf", "atom_b", 8)
        result = ST.read_skill_tasks("buf")
        assert "atom_a" in result
        assert "atom_b" in result
        assert "weightscore=6" in result
        assert "weightscore=8" in result
        assert "total=14" in result

    def test_empty_buffer_returns_zero_message(self, tmp_path):
        _setup(tmp_path)
        ST.new_skill_folder("empty-buf", "stub")
        result = ST.read_skill_tasks("empty-buf")
        assert "0 candidates" in result

    def test_nonexistent_skill_returns_error(self, tmp_path):
        _setup(tmp_path)
        result = ST.read_skill_tasks("nonexistent")
        assert result.startswith("error")


class TestMoveTaskTo:
    def test_move_task_between_skills(self, tmp_path):
        skill_dir, _ = _setup(tmp_path)
        ST.new_skill_folder("from-skill", "stub")
        ST.new_skill_folder("to-skill", "stub")
        ST.add_task_to_skill("from-skill", "atom_x", 8)
        result = ST.move_task_to("from-skill", "to-skill", "atom_x")
        assert "moved" in result
        # source 空
        from xskill.skill import candidates as C
        from_data = C.load_candidates(skill_dir / "from-skill")
        assert from_data["candidates"] == []
        # target 含 atom_x，weightscore 保留为 8
        to_data = C.load_candidates(skill_dir / "to-skill")
        assert len(to_data["candidates"]) == 1
        assert to_data["candidates"][0]["atom_id"] == "atom_x"
        assert to_data["candidates"][0]["weightscore"] == 8

    def test_move_overwrites_existing_target_atom(self, tmp_path):
        skill_dir, _ = _setup(tmp_path)
        ST.new_skill_folder("from", "stub")
        ST.new_skill_folder("to", "stub")
        ST.add_task_to_skill("from", "atom_dup", 3)
        ST.add_task_to_skill("to", "atom_dup", 9)
        ST.move_task_to("from", "to", "atom_dup")
        # target 的 atom_dup 被覆盖为 from 的 weightscore=3
        from xskill.skill import candidates as C
        to_data = C.load_candidates(skill_dir / "to")
        assert to_data["candidates"][0]["weightscore"] == 3

    def test_move_same_skill_noop(self, tmp_path):
        _setup(tmp_path)
        ST.new_skill_folder("solo", "stub")
        ST.add_task_to_skill("solo", "atom_a", 5)
        result = ST.move_task_to("solo", "solo", "atom_a")
        assert "noop" in result

    def test_move_atom_not_in_source_fails(self, tmp_path):
        _setup(tmp_path)
        ST.new_skill_folder("a", "stub")
        ST.new_skill_folder("b", "stub")
        result = ST.move_task_to("a", "b", "atom_missing")
        assert result.startswith("error")
        assert "不在" in result


class TestAddTask:
    def test_writes_synthetic_atom(self, tmp_path):
        _, store = _setup(tmp_path)
        msg = ST.add_task(
            atom_id="atom_manual_0001", traj_id="manual",
            offset_start=0, offset_end=5,
            intent="manual intent", summary="manual summary",
            tags=["t1"], used_skills=[], ux_score=8,
        )
        assert "added" in msg
        a = store.load("atom_manual_0001")
        assert a.intent == "manual intent"
        assert a.ux_score == 8
