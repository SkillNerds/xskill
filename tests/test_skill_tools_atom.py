"""agent_tools v2 atom-era 工具集单测"""
from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from xskill.pipeline.atom import AtomTask, AtomTaskStore
from xskill.agents import agent_tools


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
    # 一条 5 行的 traj.md，给 read_traj 测试用（read_traj 按行号取片段）
    (store_root / "x.md").write_text(
        "L1\nL2\nL3\nL4\nL5\n", encoding="utf-8")
    agent_tools.init_atom_task_tool_context(
        skill_dir=skill_dir, atom_store=store,
        default_traj_root=store_root,
        spill_root=tmp_path / "instance-spill",
    )
    return skill_dir, store


class TestAtomTaskRead:
    def test_returns_atom_json(self, tmp_path):
        _setup(tmp_path)
        out = agent_tools.atom_task_read.entrypoint("atom_x_0001")
        assert "atom_x_0001" in out
        assert "makemigrations" in out

    def test_not_found_returns_error(self, tmp_path):
        _setup(tmp_path)
        out = agent_tools.atom_task_read.entrypoint("atom_nonexistent")
        assert out.startswith("error")


class TestReadTraj:
    def test_returns_slice(self, tmp_path):
        """按行号半开区间取片段:[1,3) = 第 1、2 行。"""
        _setup(tmp_path)
        out = agent_tools.read_traj.entrypoint("x", offset_start=1, offset_end=3)
        assert out == "L1\nL2\n"

    def test_last_line_reachable(self, tmp_path):
        """末 atom 的 offset_end = 末行号+1,要能取到最后一行。"""
        _setup(tmp_path)  # x.md 共 5 行
        out = agent_tools.read_traj.entrypoint("x", offset_start=5, offset_end=6)
        assert out == "L5\n"

    def test_invalid_range_returns_error(self, tmp_path):
        _setup(tmp_path)
        out = agent_tools.read_traj.entrypoint("x", offset_start=10, offset_end=5)
        assert out.startswith("error")

    def test_zero_start_line_returns_error(self, tmp_path):
        """行号是 1-based,offset_start < 1 非法。"""
        _setup(tmp_path)
        out = agent_tools.read_traj.entrypoint("x", offset_start=0, offset_end=2)
        assert out.startswith("error")

    def test_out_of_bounds_returns_error(self, tmp_path):
        _setup(tmp_path)
        out = agent_tools.read_traj.entrypoint("x", offset_start=1, offset_end=999999)
        assert out.startswith("error")

    def test_nonexistent_traj_returns_error(self, tmp_path):
        _setup(tmp_path)
        out = agent_tools.read_traj.entrypoint("doesnt-exist", offset_start=1, offset_end=3)
        assert out.startswith("error")


class TestReadFile:
    def test_reads_tmp_spill_file_with_path_context(self, tmp_path):
        skill_dir, _ = _setup(tmp_path)
        agent_tools.init_skill_authoring_tool_context(
            skill_dir, skill_dir, {"skill_opt": {"enabled": False}},
            spill_root=tmp_path / "instance-spill",
        )
        spill = tmp_path / "instance-spill" / "tool-result.txt"
        spill.parent.mkdir(parents=True, exist_ok=True)
        spill.write_text("spilled raw tool result\n", encoding="utf-8")

        out = agent_tools.read_file.entrypoint(str(spill))

        assert "source_path:" in out
        assert "resolved_path:" in out
        assert str(spill) in out
        assert "spilled raw tool result" in out

    def test_reads_line_window_with_offset_and_limit(self, tmp_path):
        skill_dir, _ = _setup(tmp_path)
        agent_tools.init_skill_authoring_tool_context(
            skill_dir, skill_dir, {"skill_opt": {"enabled": False}},
            spill_root=tmp_path / "instance-spill",
        )
        spill = tmp_path / "instance-spill" / "window.txt"
        spill.parent.mkdir(parents=True, exist_ok=True)
        spill.write_text("L1\nL2\nL3\nL4\n", encoding="utf-8")

        out = agent_tools.read_file.entrypoint(str(spill), offset=2, limit=2)

        assert "source_path:" in out
        assert "resolved_path:" in out
        assert "line_range: [2, 4)" in out
        assert "line_offset:" not in out
        assert "line_limit:" not in out
        assert "total_lines:" not in out
        assert "L2\nL3\n" in out
        assert "L1\n" not in out
        assert "L4\n" not in out


class TestListFiles:
    def test_returns_paths_read_file_can_use(self, tmp_path):
        skill_dir, _ = _setup(tmp_path)
        agent_tools.init_skill_authoring_tool_context(
            skill_dir, skill_dir, {"skill_opt": {"enabled": False}},
            spill_root=tmp_path / "instance-spill",
        )
        note = skill_dir / "notes.md"
        note.write_text("hello from listed file\n", encoding="utf-8")

        listing = agent_tools.list_files.entrypoint(str(skill_dir))

        assert str(note) in listing
        out = agent_tools.read_file.entrypoint(str(note), offset=1, limit=20)
        assert "hello from listed file" in out


class TestNewSkillFolder:
    def test_creates_directory_with_skeleton(self, tmp_path):
        skill_dir, _ = _setup(tmp_path)
        msg = agent_tools.new_skill_folder.entrypoint("my-new-skill", "stub desc")
        assert "created" in msg
        assert (skill_dir / "my-new-skill" / "scripts").is_dir()
        assert (skill_dir / "my-new-skill" / "references").is_dir()

    def test_repeated_call_returns_already_exists(self, tmp_path):
        _setup(tmp_path)
        agent_tools.new_skill_folder.entrypoint("dup-skill", "stub desc")
        msg = agent_tools.new_skill_folder.entrypoint("dup-skill", "stub desc")
        assert "already exists" in msg

    def test_description_required(self, tmp_path):
        """无 description / 空白 desc 应被拒——避免 cluster agent 偷懒。"""
        _setup(tmp_path)
        for bad in ["", "   ", "\n\t"]:
            msg = agent_tools.new_skill_folder.entrypoint("no-desc-skill", bad)
            assert msg.startswith("error"), f"空 desc ({bad!r}) 应被拒"

    def test_description_written_to_stub_skill_md(self, tmp_path):
        """v2: desc 落到 stub SKILL.md frontmatter；同时 baby 分支被初始化。"""
        skill_dir, _ = _setup(tmp_path)
        agent_tools.new_skill_folder.entrypoint(
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
        agent_tools.new_skill_folder.entrypoint("auto-skill", "stub desc")
        with agent_tools.use_cluster_batch(["atom_x_0001"]):
            msg = agent_tools.add_task_to_skill.entrypoint(
                "auto-skill",
                "atom_x_0001",
                6,
            )
        assert "buffer_total=6" in msg

    def test_repeated_add_overwrites(self, tmp_path):
        """v2.1: 同 atom 重复 add → 覆盖 weightscore（不累加）。"""
        _setup(tmp_path)
        agent_tools.new_skill_folder.entrypoint("auto-skill", "stub desc")
        with agent_tools.use_cluster_batch(["atom_x_0001"]):
            agent_tools.add_task_to_skill.entrypoint(
                "auto-skill",
                "atom_x_0001",
                4,
            )
            msg = agent_tools.add_task_to_skill.entrypoint(
                "auto-skill",
                "atom_x_0001",
                5,
            )
        assert "weightscore=5" in msg
        # buffer 仍是单条 atom，total 是覆盖后的 5（不是 4+5=9）
        assert "buffer_total=5" in msg
        assert "overwrite" in msg

    def test_nonexistent_skill_returns_error(self, tmp_path):
        _setup(tmp_path)
        msg = agent_tools.add_task_to_skill.entrypoint("nonexistent", "atom_x_0001", 5)
        assert msg.startswith("error")

    def test_weightscore_out_of_range_returns_error(self, tmp_path):
        _setup(tmp_path)
        agent_tools.new_skill_folder.entrypoint("auto-skill", "stub desc")
        with agent_tools.use_cluster_batch(["atom_x_0001"]):
            for bad in [0, 11, -1, 100]:
                msg = agent_tools.add_task_to_skill.entrypoint(
                    "auto-skill",
                    "atom_x_0001",
                    bad,
                )
                assert msg.startswith("error")

    @pytest.mark.parametrize("invalid_score", [True, "8", 8.0])
    def test_weightscore_requires_strict_integer(self, tmp_path, invalid_score):
        _setup(tmp_path)
        agent_tools.new_skill_folder.entrypoint("auto-skill", "stub desc")

        with agent_tools.use_cluster_batch(["atom_x_0001"]):
            with pytest.raises(ValidationError):
                agent_tools.add_task_to_skill.entrypoint(
                    "auto-skill",
                    "atom_x_0001",
                    invalid_score,
                )

    def test_rejects_write_without_current_cluster_batch(self, tmp_path):
        skill_dir, _ = _setup(tmp_path)
        agent_tools.new_skill_folder.entrypoint("auto-skill", "stub desc")

        with agent_tools.use_cluster_batch([]):
            message = agent_tools.add_task_to_skill.entrypoint(
                "auto-skill",
                "atom_x_0001",
                7,
            )

        assert message.startswith("error:")
        assert "current cluster batch" in message
        from xskill.skill import candidates as C
        assert C.load_candidates(
            skill_dir / "auto-skill",
        )["candidates"] == []

    def test_rejects_atom_outside_current_cluster_batch(self, tmp_path):
        skill_dir, _ = _setup(tmp_path)
        agent_tools.new_skill_folder.entrypoint("auto-skill", "stub desc")

        with agent_tools.use_cluster_batch(["atom_x_0001"]):
            message = agent_tools.add_task_to_skill.entrypoint(
                "auto-skill",
                "atom_outside_batch",
                7,
            )

        assert message.startswith("error:")
        assert "current cluster batch" in message
        from xskill.skill import candidates as C
        assert C.load_candidates(
            skill_dir / "auto-skill",
        )["candidates"] == []

    def test_allows_independent_cross_skill_associations(self, tmp_path):
        skill_dir, _ = _setup(tmp_path)
        agent_tools.new_skill_folder.entrypoint("skill-a", "first target")
        agent_tools.new_skill_folder.entrypoint("skill-b", "second target")

        with agent_tools.use_cluster_batch(["atom_x_0001"]):
            first = agent_tools.add_task_to_skill.entrypoint(
                "skill-a",
                "atom_x_0001",
                6,
            )
            second = agent_tools.add_task_to_skill.entrypoint(
                "skill-b",
                "atom_x_0001",
                7,
            )

        assert first.startswith("new:")
        assert second.startswith("new:")
        from xskill.skill import candidates as C
        assert C.load_candidates(skill_dir / "skill-a")["candidates"] == [{
            "atom_id": "atom_x_0001",
            "weightscore": 6,
        }]
        assert C.load_candidates(skill_dir / "skill-b")["candidates"] == [{
            "atom_id": "atom_x_0001",
            "weightscore": 7,
        }]

    def test_batch_add_writes_every_atom(self, tmp_path):
        skill_dir, _ = _setup(tmp_path)
        agent_tools.new_skill_folder.entrypoint("auto-skill", "stub desc")
        with agent_tools.use_cluster_batch(["atom-a", "atom-b"]):
            message = agent_tools.add_tasks_to_skill.entrypoint(
                "auto-skill",
                [
                    {"atom_id": "atom-a", "weightscore": 3},
                    {
                        "atom_id": "atom-b",
                        "weightscore": 7,
                        "note": "strong",
                    },
                ],
            )

        assert "atoms=2" in message
        assert "new=2" in message
        assert "buffer_total=10" in message
        from xskill.skill import candidates as C
        data = C.load_candidates(skill_dir / "auto-skill")
        assert {
            candidate["atom_id"]
            for candidate in data["candidates"]
        } == {"atom-a", "atom-b"}

    def test_batch_add_rejects_duplicate_without_writing(self, tmp_path):
        skill_dir, _ = _setup(tmp_path)
        agent_tools.new_skill_folder.entrypoint("auto-skill", "stub desc")
        with agent_tools.use_cluster_batch(["atom-a"]):
            message = agent_tools.add_tasks_to_skill.entrypoint(
                "auto-skill",
                [
                    {"atom_id": "atom-a", "weightscore": 3},
                    {"atom_id": "atom-a", "weightscore": 7},
                ],
            )

        assert message.startswith("error")
        from xskill.skill import candidates as C
        assert C.load_candidates(
            skill_dir / "auto-skill",
        )["candidates"] == []

    @pytest.mark.parametrize("invalid_score", [True, "8", 8.0])
    def test_batch_weightscore_requires_strict_integer(
        self,
        tmp_path,
        invalid_score,
    ):
        _setup(tmp_path)
        agent_tools.new_skill_folder.entrypoint("auto-skill", "stub desc")

        with agent_tools.use_cluster_batch(["atom_x_0001"]):
            with pytest.raises(ValidationError):
                agent_tools.add_tasks_to_skill.entrypoint(
                    "auto-skill",
                    [{
                        "atom_id": "atom_x_0001",
                        "weightscore": invalid_score,
                    }],
                )

    def test_batch_rejects_write_without_current_cluster_batch(self, tmp_path):
        skill_dir, _ = _setup(tmp_path)
        agent_tools.new_skill_folder.entrypoint("auto-skill", "stub desc")

        with agent_tools.use_cluster_batch([]):
            message = agent_tools.add_tasks_to_skill.entrypoint(
                "auto-skill",
                [{"atom_id": "atom_x_0001", "weightscore": 7}],
            )

        assert message.startswith("error:")
        assert "current cluster batch" in message
        from xskill.skill import candidates as C
        assert C.load_candidates(
            skill_dir / "auto-skill",
        )["candidates"] == []

    def test_batch_preserves_existing_cross_skill_association(self, tmp_path):
        skill_dir, _ = _setup(tmp_path)
        agent_tools.new_skill_folder.entrypoint("skill-a", "first target")
        agent_tools.new_skill_folder.entrypoint("skill-b", "second target")

        with agent_tools.use_cluster_batch(["atom_x_0001", "atom_x_0002"]):
            agent_tools.add_task_to_skill.entrypoint(
                "skill-a",
                "atom_x_0001",
                6,
            )
            message = agent_tools.add_tasks_to_skill.entrypoint(
                "skill-b",
                [
                    {"atom_id": "atom_x_0002", "weightscore": 5},
                    {"atom_id": "atom_x_0001", "weightscore": 7},
                ],
            )

        assert message.startswith("batched:")
        from xskill.skill import candidates as C
        assert C.load_candidates(skill_dir / "skill-a")["candidates"] == [{
            "atom_id": "atom_x_0001",
            "weightscore": 6,
        }]
        assert {
            candidate["atom_id"]: candidate["weightscore"]
            for candidate in C.load_candidates(skill_dir / "skill-b")[
                "candidates"
            ]
        } == {
            "atom_x_0001": 7,
            "atom_x_0002": 5,
        }

    def test_batch_membership_validation_is_all_or_nothing(self, tmp_path):
        skill_dir, _ = _setup(tmp_path)
        agent_tools.new_skill_folder.entrypoint("auto-skill", "stub desc")

        with agent_tools.use_cluster_batch(["atom_x_0001"]):
            message = agent_tools.add_tasks_to_skill.entrypoint(
                "auto-skill",
                [
                    {"atom_id": "atom_x_0001", "weightscore": 5},
                    {"atom_id": "atom_outside_batch", "weightscore": 7},
                ],
            )

        assert message.startswith("error:")
        assert "current cluster batch" in message
        from xskill.skill import candidates as C
        assert C.load_candidates(
            skill_dir / "auto-skill",
        )["candidates"] == []

    def test_batch_tool_schema_exposes_item_fields(self):
        parameters = agent_tools.add_tasks_to_skill.parameters
        task_items = parameters["properties"]["tasks"]["items"]

        assert task_items["type"] == "object"
        assert task_items["additionalProperties"] is False
        assert set(task_items["properties"]) == {
            "atom_id",
            "weightscore",
            "note",
        }
        assert set(task_items["required"]) == {
            "atom_id",
            "weightscore",
        }
        assert task_items["properties"]["weightscore"]["minimum"] == 1
        assert task_items["properties"]["weightscore"]["maximum"] == 10


class TestScoreTask:
    def test_score_task_updates_atom(self, tmp_path):
        _, store = _setup(tmp_path)
        msg = agent_tools.score_task.entrypoint("atom_x_0001", 9)
        assert "9" in msg
        assert store.load("atom_x_0001").ux_score == 9

    def test_invalid_score_returns_error(self, tmp_path):
        _setup(tmp_path)
        for bad in [0, 11, -3]:
            msg = agent_tools.score_task.entrypoint("atom_x_0001", bad)
            assert msg.startswith("error")


class TestSkillRead:
    def test_empty_skill_returns_placeholder(self, tmp_path):
        _setup(tmp_path)
        agent_tools.new_skill_folder.entrypoint("brand-new", "stub desc")
        msg = agent_tools.skill_read.entrypoint("brand-new")
        assert "no SKILL.md" in msg or "placeholder" in msg

    def test_existing_skill_returns_content(self, tmp_path):
        skill_dir, _ = _setup(tmp_path)
        agent_tools.new_skill_folder.entrypoint("has-content", "has content")
        (skill_dir / "has-content" / "SKILL.md").write_text(
            "---\nname: has-content\n---\n# body here\n", encoding="utf-8",
        )
        msg = agent_tools.skill_read.entrypoint("has-content")
        assert "body here" in msg


class TestRenameSkill:
    def test_rename_baby_succeeds(self, tmp_path):
        skill_dir, _ = _setup(tmp_path)
        agent_tools.new_skill_folder.entrypoint("old-name", "stub")
        result = agent_tools.rename_skill.entrypoint("old-name", "new-name")
        assert "renamed" in result
        assert (skill_dir / "new-name").is_dir()
        assert not (skill_dir / "old-name").exists()
        # SKILL.md frontmatter.name 已更新
        from xskill.skill.frontmatter import parse as fm_parse
        fm, _ = fm_parse((skill_dir / "new-name" / "SKILL.md").read_text(encoding="utf-8"))
        assert fm["name"] == "new-name"

    def test_rename_to_existing_target_fails(self, tmp_path):
        _setup(tmp_path)
        agent_tools.new_skill_folder.entrypoint("source", "stub")
        agent_tools.new_skill_folder.entrypoint("target", "stub")
        result = agent_tools.rename_skill.entrypoint("source", "target")
        assert result.startswith("error")
        assert "已存在" in result

    def test_rename_main_state_fails(self, tmp_path):
        skill_dir, _ = _setup(tmp_path)
        agent_tools.new_skill_folder.entrypoint("graduated", "stub")
        # graduate 到 main
        from xskill.skill.git import run_git
        run_git(["branch", "-m", "baby", "main"], cwd=str(skill_dir / "graduated"))
        result = agent_tools.rename_skill.entrypoint("graduated", "new-name")
        assert result.startswith("error")
        assert "baby" in result

    def test_rename_nonexistent_skill_fails(self, tmp_path):
        _setup(tmp_path)
        result = agent_tools.rename_skill.entrypoint("nonexistent", "new")
        assert result.startswith("error")

    def test_rename_to_same_name_noop(self, tmp_path):
        _setup(tmp_path)
        agent_tools.new_skill_folder.entrypoint("same", "stub")
        result = agent_tools.rename_skill.entrypoint("same", "same")
        assert "noop" in result


class TestReadSkillTasks:
    def test_reads_candidates_with_weightscore(self, tmp_path):
        _setup(tmp_path)
        agent_tools.new_skill_folder.entrypoint("buf", "stub")
        with agent_tools.use_cluster_batch(["atom_a", "atom_b"]):
            agent_tools.add_task_to_skill.entrypoint("buf", "atom_a", 6)
            agent_tools.add_task_to_skill.entrypoint("buf", "atom_b", 8)
        result = agent_tools.read_skill_tasks.entrypoint("buf")
        assert "atom_a" in result
        assert "atom_b" in result
        assert "weightscore=6" in result
        assert "weightscore=8" in result
        assert "total=14" in result

    def test_empty_buffer_returns_zero_message(self, tmp_path):
        _setup(tmp_path)
        agent_tools.new_skill_folder.entrypoint("empty-buf", "stub")
        result = agent_tools.read_skill_tasks.entrypoint("empty-buf")
        assert "0 candidates" in result

    def test_nonexistent_skill_returns_error(self, tmp_path):
        _setup(tmp_path)
        result = agent_tools.read_skill_tasks.entrypoint("nonexistent")
        assert result.startswith("error")


class TestMoveTaskTo:
    def test_move_task_between_skills(self, tmp_path):
        skill_dir, _ = _setup(tmp_path)
        agent_tools.new_skill_folder.entrypoint("from-skill", "stub")
        agent_tools.new_skill_folder.entrypoint("to-skill", "stub")
        with agent_tools.use_cluster_batch(["atom_x"]):
            agent_tools.add_task_to_skill.entrypoint(
                "from-skill",
                "atom_x",
                8,
            )
        with agent_tools.use_cluster_batch(["atom_current"]):
            result = agent_tools.move_task_to.entrypoint(
                "from-skill",
                "to-skill",
                "atom_x",
            )
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
        agent_tools.new_skill_folder.entrypoint("from", "stub")
        agent_tools.new_skill_folder.entrypoint("to", "stub")
        with agent_tools.use_cluster_batch(["atom_dup"]):
            agent_tools.add_task_to_skill.entrypoint(
                "from",
                "atom_dup",
                3,
            )
            agent_tools.add_task_to_skill.entrypoint(
                "to",
                "atom_dup",
                9,
            )
        agent_tools.move_task_to.entrypoint("from", "to", "atom_dup")
        # target 的 atom_dup 被覆盖为 from 的 weightscore=3
        from xskill.skill import candidates as C
        to_data = C.load_candidates(skill_dir / "to")
        assert to_data["candidates"][0]["weightscore"] == 3

    def test_move_same_skill_noop(self, tmp_path):
        _setup(tmp_path)
        agent_tools.new_skill_folder.entrypoint("solo", "stub")
        with agent_tools.use_cluster_batch(["atom_a"]):
            agent_tools.add_task_to_skill.entrypoint("solo", "atom_a", 5)
        result = agent_tools.move_task_to.entrypoint("solo", "solo", "atom_a")
        assert "noop" in result

    def test_move_atom_not_in_source_fails(self, tmp_path):
        _setup(tmp_path)
        agent_tools.new_skill_folder.entrypoint("a", "stub")
        agent_tools.new_skill_folder.entrypoint("b", "stub")
        result = agent_tools.move_task_to.entrypoint("a", "b", "atom_missing")
        assert result.startswith("error")
        assert "不在" in result


class TestAddTask:
    def test_writes_synthetic_atom(self, tmp_path):
        _, store = _setup(tmp_path)
        msg = agent_tools.add_task.entrypoint(
            atom_id="atom_manual_0001", traj_id="manual",
            offset_start=0, offset_end=5,
            intent="manual intent", summary="manual summary",
            tags=["t1"], used_skills=[], ux_score=8,
        )
        assert "added" in msg
        a = store.load("atom_manual_0001")
        assert a.intent == "manual intent"
        assert a.ux_score == 8
