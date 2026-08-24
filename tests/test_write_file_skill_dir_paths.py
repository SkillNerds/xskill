"""write_file 相对路径按 skill_dir 解析，不按进程 cwd。"""
from __future__ import annotations

from pathlib import Path

import pytest

from xskill.agents import agent_tools


SKILL_MD = "---\nname: demo-skill\ndescription: x\n---\n# demo\n"


@pytest.fixture
def skill_repo(tmp_path):
    parent = tmp_path / "workspace" / "skill"
    repo = parent / "demo-skill"
    repo.mkdir(parents=True)
    (repo / "scripts").mkdir()
    snap = agent_tools.agent_tool_config.snapshot()
    agent_tools.init_skill_authoring_tool_context(parent, parent, {})
    agent_tools.init_atom_task_tool_context(
        skill_dir=repo,
        atom_store=None,
        default_traj_root=repo,
    )
    yield repo
    agent_tools.agent_tool_config.restore(snap)


def test_relative_skill_md_writes_inside_repo_from_other_cwd(skill_repo, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    out = agent_tools.write_file.entrypoint("SKILL.md", SKILL_MD)
    assert not out.startswith("error"), out
    assert (skill_repo / "SKILL.md").is_file()
    assert not (tmp_path / "SKILL.md").exists()
    assert "name: demo-skill" in (skill_repo / "SKILL.md").read_text(encoding="utf-8")


def test_relative_script_writes_inside_repo_from_other_cwd(skill_repo, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    out = agent_tools.write_file.entrypoint("scripts/check.py", "print(1)\n")
    assert not out.startswith("error"), out
    assert (skill_repo / "scripts" / "check.py").read_text(encoding="utf-8") == "print(1)\n"


def test_skill_prefix_is_rejected_with_location(skill_repo, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for path in (
        "skill/SKILL.md",
        "./skill/SKILL.md",
        "skill/scripts/check.py",
    ):
        out = agent_tools.write_file.entrypoint(path, SKILL_MD)
        assert out.startswith("error:"), path
        assert "do not prefix skill/" in out
        assert f"skill_dir: {skill_repo.resolve()}" in out
        assert "example: SKILL.md" in out
        assert not (skill_repo / "skill").exists()


def test_outside_absolute_path_names_skill_dir(skill_repo, tmp_path):
    outsider = tmp_path / "other" / "SKILL.md"
    outsider.parent.mkdir()
    out = agent_tools.write_file.entrypoint(str(outsider), SKILL_MD)
    assert "writes restricted to skill_dir" in out
    assert f"skill_dir: {skill_repo.resolve()}" in out
    assert not outsider.exists()


def test_edit_relative_path_from_other_cwd(skill_repo, tmp_path, monkeypatch):
    target = skill_repo / "notes.md"
    target.write_text("hello\n", encoding="utf-8")
    agent_tools.write_file.entrypoint("notes.md", "hello\n")
    monkeypatch.chdir(tmp_path)
    out = agent_tools.edit_file.entrypoint("notes.md", "hello", "world")
    assert not out.startswith("error"), out
    assert target.read_text(encoding="utf-8") == "world\n"
