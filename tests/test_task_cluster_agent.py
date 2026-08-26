"""TaskClusterAgent + build_skill_catalog_block 单测"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from xskill.agents import task_cluster_agent as task_cluster_module
from xskill.pipeline.atom import AtomTask, AtomTaskStore
from xskill.agents.task_cluster_agent import (
    TaskClusterAgent,
    _clear_catalog_block_cache,
    _format_skill_catalog_block,
    build_skill_catalog_block,
)


@pytest.fixture(autouse=True)
def _clear_cluster_catalog_cache():
    _clear_catalog_block_cache()
    yield
    _clear_catalog_block_cache()


def _make_skill_on_main(skill_dir: Path, name: str, desc: str):
    """模拟一个 main 状态的 skill：git init + main 分支 + SKILL.md。"""
    from xskill.skill.git import run_git
    sd = skill_dir / name
    sd.mkdir(parents=True)
    run_git(["init"], cwd=str(sd))
    run_git(["checkout", "-b", "main"], cwd=str(sd))
    run_git(["config", "user.email", "t@t"], cwd=str(sd))
    run_git(["config", "user.name", "t"], cwd=str(sd))
    (sd / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {desc}\nmetadata:\n  version: 1\n---\n# {name}\n",
        encoding="utf-8",
    )
    run_git(["add", "."], cwd=str(sd))
    run_git(["commit", "-m", "init"], cwd=str(sd))


def _make_skill_on_baby(skill_dir: Path, name: str, desc: str):
    """模拟一个 baby 状态的 skill（cluster 刚创建，还没 SkillEdit graduate）。"""
    from xskill.skill.git import init_skill_repo_on_baby
    sd = skill_dir / name
    init_skill_repo_on_baby(str(sd), name=name, description=desc)


def _make_skill_on_staging(skill_dir: Path, name: str, main_desc: str, staging_desc: str):
    """模拟 main + staging 双态。"""
    from xskill.skill.git import run_git
    _make_skill_on_main(skill_dir, name, main_desc)
    sd = skill_dir / name
    run_git(["checkout", "-b", "staging"], cwd=str(sd))
    (sd / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {staging_desc}\nmetadata:\n  version: 2\n---\n# {name} (staging)\n",
        encoding="utf-8",
    )
    run_git(["add", "."], cwd=str(sd))
    run_git(["commit", "-m", "staging variant"], cwd=str(sd))
    run_git(["checkout", "main"], cwd=str(sd))


# 兼容旧测试名（保留 main 创建 helper）
_make_skill = _make_skill_on_main


class TestSkillCatalogBudget:
    def test_short_list_keeps_full_descriptions(self, tmp_path):
        skill_dir = tmp_path / "skill"
        for n, d in [("a-skill", "deals with A"), ("b-skill", "deals with B")]:
            _make_skill(skill_dir, n, d)
        block = build_skill_catalog_block(skill_dir, max_chars=20000)
        # 新格式带 [state] 标识；两个 skill 都有 SKILL.md → main
        assert "a-skill[main]: deals with A" in block
        assert "b-skill[main]: deals with B" in block

    def test_overflow_truncates_descriptions(self):
        # 100 skills, desc 100 chars 各，约 10k chars desc；预算 4000 强制截断
        entries = [(f"s-{i:03d}", "main", "x" * 100) for i in range(100)]
        block = _format_skill_catalog_block(entries, max_chars=4000)
        for i in range(100):
            assert f"s-{i:03d}" in block
        # 不允许完整 100 字 desc
        assert "x" * 100 not in block

    def test_extreme_overflow_drops_all_descriptions(self):
        # 500 skills, 200 字 desc → 100k 字 desc，预算 5000 → per_desc 远 < 75
        entries = [(f"big-{i:04d}", "main", "y" * 200) for i in range(500)]
        block = _format_skill_catalog_block(entries, max_chars=5000)
        for i in range(500):
            assert f"big-{i:04d}" in block
        # 该模式下不留 desc
        assert "yyyy" not in block

    def test_baby_skill_shown_with_buffer_count(self, tmp_path):
        """baby 分支的 skill 也要进路由表，标 [baby] + N candidates 计数。"""
        skill_dir = tmp_path / "skill"
        _make_skill_on_baby(skill_dir, "baby-skill", "处理 baby case")
        (skill_dir / "baby-skill" / ".candidates.yml").write_text(
            "candidates:\n"
            "- atom_id: atom_a\n  weightscore: 5\n"
            "- atom_id: atom_b\n  weightscore: 4\n",
            encoding="utf-8",
        )
        block = build_skill_catalog_block(skill_dir, max_chars=20000)
        assert "baby-skill[baby]" in block
        assert "处理 baby case" in block  # desc 从 stub SKILL.md 取
        assert "2 cand" in block

    def test_baby_skill_shows_stub_skill_md_description(self, tmp_path):
        """baby 分支已有 stub SKILL.md，desc 从 frontmatter 取，配合 buffer 计数展示。

        这是避免近义 slug 泛滥的关键：cluster agent 看 desc 判断 baby 是
        否承接当前 atom。
        """
        skill_dir = tmp_path / "skill"
        _make_skill_on_baby(skill_dir, "django-fix",
                            "修复 Django manage.py migrate 冲突")
        (skill_dir / "django-fix" / ".candidates.yml").write_text(
            "candidates:\n- atom_id: atom_a\n  weightscore: 5\n",
            encoding="utf-8",
        )
        block = build_skill_catalog_block(skill_dir, max_chars=20000)
        assert "django-fix[baby]" in block
        assert "修复 Django manage.py migrate 冲突" in block
        assert "1 cand" in block

    def test_staging_state_when_staging_branch_exists(self, tmp_path):
        """main + staging 双分支 → state=staging（灰度中）。"""
        skill_dir = tmp_path / "skill"
        _make_skill_on_staging(skill_dir, "in-canary",
                                main_desc="main 版本", staging_desc="staging 候选")
        block = build_skill_catalog_block(skill_dir, max_chars=20000)
        assert "in-canary[staging]" in block
        # staging checkout 后回到 main，desc 从 main 取
        assert "main 版本" in block

    def test_empty_skill_dir_returns_placeholder(self, tmp_path):
        skill_dir = tmp_path / "empty_skill"
        skill_dir.mkdir()
        block = build_skill_catalog_block(skill_dir, max_chars=10000)
        assert "no skills" in block.lower()

    @pytest.mark.performance_contract
    def test_projection_snapshot_singleflights_until_generation_changes(
        self, tmp_path, monkeypatch,
    ):
        """同 generation 并发 batch 只读一次目录行；写入后下一批立即刷新。"""
        from xskill.skill import catalog_store

        skill_dir = tmp_path / "skill"
        registry = tmp_path / "registry.db"
        _make_skill(skill_dir, "alpha", "first description")

        calls: list[None] = []
        disk_reads: list[None] = []
        original = catalog_store.list_native_cluster_catalog
        original_read = catalog_store._read_native_row

        def counted(*args, **kwargs):
            calls.append(None)
            return original(*args, **kwargs)

        def counted_disk_read(*args, **kwargs):
            disk_reads.append(None)
            return original_read(*args, **kwargs)

        monkeypatch.setattr(catalog_store, "list_native_cluster_catalog", counted)
        monkeypatch.setattr(catalog_store, "_read_native_row", counted_disk_read)
        monkeypatch.setattr(
            task_cluster_module,
            "_scan_skill_state",
            lambda _path: pytest.fail("projection path must not run legacy Git scan"),
        )
        with ThreadPoolExecutor(max_workers=8) as executor:
            blocks = list(executor.map(
                lambda _: build_skill_catalog_block(
                    skill_dir, max_chars=20000, db_path=registry,
                ),
                range(16),
            ))

        assert len(calls) == 1
        assert len(disk_reads) == 1
        assert all("alpha[main]: first description" in block for block in blocks)

        _make_skill_on_baby(skill_dir, "beta", "new baby description")
        catalog_store.upsert_native_skill(skill_dir / "beta", db_path=registry)
        refreshed = build_skill_catalog_block(
            skill_dir, max_chars=20000, db_path=registry,
        )
        assert len(calls) == 2
        assert len(disk_reads) == 2
        assert "beta[baby]: new baby description (0 cand in buffer)" in refreshed

    def test_projection_failure_falls_back_to_disk_scan(self, tmp_path, monkeypatch):
        from xskill.skill import catalog_store

        skill_dir = tmp_path / "skill"
        skill_dir.mkdir()

        def unavailable(*_args, **_kwargs):
            raise OSError("registry unavailable")

        monkeypatch.setattr(catalog_store, "native_catalog_generation", unavailable)
        monkeypatch.setattr(
            task_cluster_module,
            "_scan_skill_state",
            lambda _path: [("fallback", "main", "disk truth")],
        )

        block = build_skill_catalog_block(
            skill_dir, max_chars=10000, db_path=tmp_path / "registry.db",
        )
        assert block == "- fallback[main]: disk truth"

    def test_replaced_registry_file_cannot_reuse_old_generation(self, tmp_path, monkeypatch):
        from xskill.skill import catalog_store

        skill_dir = tmp_path / "skill"
        registry = tmp_path / "registry.db"
        rows = [{
            "name": "alpha", "state": "main", "description": "first",
            "candidates_count": 0,
        }]
        calls: list[None] = []
        identity = {"value": (1, 1)}

        monkeypatch.setattr(
            catalog_store, "native_catalog_generation", lambda *_args, **_kwargs: 1,
        )
        monkeypatch.setattr(
            catalog_store,
            "list_native_cluster_catalog",
            lambda *_args, **_kwargs: calls.append(None) or list(rows),
        )
        monkeypatch.setattr(
            task_cluster_module,
            "_catalog_db_identity",
            lambda _path: identity["value"],
        )

        first = build_skill_catalog_block(
            skill_dir, max_chars=10000, db_path=registry,
        )
        assert build_skill_catalog_block(
            skill_dir, max_chars=10000, db_path=registry,
        ) == first
        assert len(calls) == 1

        rows[0] = {
            "name": "beta", "state": "main", "description": "replacement",
            "candidates_count": 0,
        }
        identity["value"] = (1, 2)
        replaced = build_skill_catalog_block(
            skill_dir, max_chars=10000, db_path=registry,
        )
        assert "beta[main]: replacement" in replaced
        assert len(calls) == 2

    def test_projection_cache_has_hard_entry_and_size_bounds(self):
        value = "x" * 100
        for generation in range(40):
            key = ("db", 1, 1, "root", 1000, generation)
            task_cluster_module._cache_catalog_block(key, lambda: value)

        assert len(task_cluster_module._CATALOG_CACHE) == 32
        assert sum(
            len(block) for block in task_cluster_module._CATALOG_CACHE.values()
        ) <= task_cluster_module._CATALOG_CACHE_MAX_CHARS

        _clear_catalog_block_cache()
        large_value = "x" * 600_000
        for generation in range(2):
            key = ("db", 1, 1, "root", 1000, generation)
            task_cluster_module._cache_catalog_block(key, lambda: large_value)
        assert len(task_cluster_module._CATALOG_CACHE) == 1
        assert sum(
            len(block) for block in task_cluster_module._CATALOG_CACHE.values()
        ) <= task_cluster_module._CATALOG_CACHE_MAX_CHARS


class TestClusterAgentPrompt:
    def test_prompt_includes_atom_id_and_skill_catalog(self, tmp_path):
        skill_dir = tmp_path / "skill"
        _make_skill(skill_dir, "fix-django", "django migration 修复")
        store = AtomTaskStore(root=tmp_path / "cc-sessions")
        atom = AtomTask(
            atom_id="atom_t_0001", traj_id="t",
            offset_start=0, offset_end=10,
            intent="修 django", summary="跑了 makemigrations",
            tags=["django"], used_skills=[], ux_score=7,
            pre_atom_id=None, post_atom_id=None,
            context_prefix="", raw_segment="",
        )
        store.save(atom)

        captured = {}

        class _StubAgnoAgent:
            def __init__(self, *, instructions, tools):
                captured["instructions"] = instructions
                captured["tools"] = tools

            def run(self, user_msg, **kw):
                captured["user_msg"] = user_msg
                class _R: pass
                r = _R(); r.content = ""
                return r

        def _factory(*, instructions, tools):
            return _StubAgnoAgent(instructions=instructions, tools=tools)

        agent = TaskClusterAgent(
            skill_dir=skill_dir, store=store,
            agno_agent_factory=_factory, llm_cfg={}, tools=[],
            logs_dir=tmp_path / "instance-logs",
        )
        agent.process(atom)
        assert (
            tmp_path / "instance-logs" / "agents"
            / "task_cluster_agents" / "t" / "atom_t_0001.log"
        ).is_file()
        # User msg contains atom_id + intent + summary
        assert "atom_t_0001" in captured["user_msg"]
        assert "跑了 makemigrations" in captured["user_msg"]
        # System prompt含 skill 路由表 + 严格分档表
        sys_text = captured["instructions"][0]
        assert "fix-django" in sys_text
        assert "weightscore" in sys_text.lower() or "权重" in sys_text
        # 严格分档表关键词
        assert "10" in sys_text and "立即触发" in sys_text

    def test_process_batch_lists_all_atoms_in_one_call(self, tmp_path):
        """process_batch 把整批 atom 的位置列进**一次** user_msg，每个 atom_id 都在。"""
        skill_dir = tmp_path / "skill"
        _make_skill(skill_dir, "fix-django", "django migration 修复")
        store = AtomTaskStore(root=tmp_path / "cc-sessions")
        atoms = []
        for i in range(3):
            a = AtomTask(
                atom_id=f"atom_t_{i:04d}", traj_id="t",
                offset_start=i * 10, offset_end=i * 10 + 5,
                intent=f"intent {i}", summary=f"summary {i}",
                tags=[], used_skills=[], ux_score=7,
            )
            store.save(a)
            atoms.append(a)

        captured = {"runs": 0}

        class _StubAgnoAgent:
            def __init__(self, *, instructions, tools):
                captured["instructions"] = instructions

            def run(self, user_msg, **kw):
                captured["runs"] += 1
                captured["user_msg"] = user_msg
                class _R: pass
                r = _R(); r.content = ""
                return r

        agent = TaskClusterAgent(
            skill_dir=skill_dir, store=store,
            agno_agent_factory=lambda **kw: _StubAgnoAgent(**kw),
            llm_cfg={}, tools=[],
        )
        agent.process_batch(atoms)
        # 一批 = 一次 LLM 调用（少往返的本质）
        assert captured["runs"] == 1
        # 三个 atom 的位置都在同一条 user_msg 里
        for i in range(3):
            assert f"atom_t_{i:04d}" in captured["user_msg"]
        assert "共 3 个" in captured["user_msg"]
        assert "add_tasks_to_skill" in captured["user_msg"]
        assert "本次只提供 add_tasks_to_skill" in captured["instructions"][0]

    def test_process_batch_empty_is_noop(self, tmp_path):
        """空批次直接返回空串，不调 agent。"""
        skill_dir = tmp_path / "skill"; skill_dir.mkdir()
        store = AtomTaskStore(root=tmp_path / "cc-sessions")
        called = {"n": 0}

        def _factory(**kw):
            called["n"] += 1
            raise AssertionError("空批次不应构造 agent")

        agent = TaskClusterAgent(
            skill_dir=skill_dir, store=store,
            agno_agent_factory=_factory, llm_cfg={}, tools=[],
        )
        assert agent.process_batch([]) == ""
        assert called["n"] == 0
