"""Restricted publication validates the delivered tree, without executing code."""
import pytest
from dulwich import porcelain


def _validator():
    from xskill.skill.artifact_validation import (
        ArtifactValidationError, validate_skill_artifact,
    )
    return ArtifactValidationError, validate_skill_artifact


def _skill(tmp_path, body="Run the checks, compare the output, and record failures."):
    path = tmp_path / "target-skill"
    path.mkdir()
    (path / "SKILL.md").write_text(
        "---\nname: target-skill\ndescription: Diagnose target failures\n---\n\n"
        + body + "\n", encoding="utf-8",
    )
    return path


def _commit(path, paths=None):
    porcelain.add(str(path), paths=paths)
    return porcelain.commit(
        str(path), message=b"test artifact", author=b"Test <test@local>",
        committer=b"Test <test@local>",
    ).decode("ascii")


def test_valid_workspace_links_and_explicit_references(tmp_path):
    _, validate = _validator()
    path = _skill(tmp_path, """# Procedure
Read [guide](references/guide.md#checks), [spaced](<references/a b.txt>),
[reference][guide] and [anchor](#procedure). See https://example.com/scripts/remote.py
and [external](https://example.com/no-file), <https://example.com/elsewhere>.
Then run `python scripts/check.py` and compare its output.
[guide]: references/guide.md "Guide title"
""")
    (path / "references").mkdir()
    (path / "references/guide.md").write_text("# Checks\nVerify results.\n")
    (path / "references/a b.txt").write_text("reference")
    (path / "scripts").mkdir()
    # Validation must never execute this script.
    (path / "scripts/check.py").write_text("raise RuntimeError('must not run')")
    result = validate(path, expected_name="target-skill")
    assert result["name"] == "target-skill"
    assert result["ref"] is None
    assert {"SKILL.md", "scripts/check.py", "references/guide.md"} <= set(result["files"])


@pytest.mark.parametrize("body, expected", [
    ("Run `python scripts/missing.py` to compare checks.", "scripts/missing.py"),
    ("Read [missing](references/missing.md).", "references/missing.md"),
    ("Read [outside](../outside.md).", "unsafe"),
    ("Read [absolute](C:/outside.md).", "unsafe"),
    ("Read [encoded](%2e%2e/outside.md).", "unsafe"),
    ("Run `scripts/{name}.py` and inspect results.", "unverified"),
    ("# Title\n\nTODO", "placeholder"),
    ("# Title\n\nTODO: write the actual procedure later.", "placeholder"),
    ("# Title\n\nPlaceholder body will be generated later.", "placeholder"),
])
def test_rejects_incomplete_or_unsafe_artifacts(tmp_path, body, expected):
    error, validate = _validator()
    with pytest.raises(error, match=expected):
        validate(_skill(tmp_path, body))


def test_rejects_existing_baby_placeholder(tmp_path):
    from xskill.skill.git import BABY_STUB_BODY_MARKER
    error, validate = _validator()
    with pytest.raises(error, match="placeholder"):
        validate(_skill(tmp_path, "# Skill\n" + BABY_STUB_BODY_MARKER))


def test_rejects_name_mismatch_and_bad_frontmatter(tmp_path):
    error, validate = _validator()
    path = _skill(tmp_path)
    with pytest.raises(error, match="name"):
        validate(path, expected_name="other-skill")
    (path / "SKILL.md").write_text("# no frontmatter\nDo checks.")
    with pytest.raises(error, match="frontmatter"):
        validate(path)


def test_complete_workspace_does_not_hide_incomplete_committed_tree(tmp_path):
    error, validate = _validator()
    path = _skill(tmp_path, "Run `python scripts/check.py` and verify output.")
    with porcelain.init(str(path)) as repo:
        repo.refs.set_symbolic_ref(b"HEAD", b"refs/heads/main")
    sha = _commit(path, ["SKILL.md"])
    (path / "scripts").mkdir()
    (path / "scripts/check.py").write_text("print('ready')")
    validate(path)
    for ref in ("main", sha):
        with pytest.raises(error, match="scripts/check.py"):
            validate(path, ref=ref)
    delivered = _commit(path)
    result = validate(path, ref="main")
    assert result["sha"] == delivered
    assert result["ref"] == "main"
    # Workspace damage must not corrupt validation of a real delivered tree.
    (path / "SKILL.md").write_text("broken workspace")
    assert validate(path, ref=delivered)["sha"] == delivered


def test_missing_or_failed_commit_is_not_a_valid_artifact(tmp_path):
    error, validate = _validator()
    path = _skill(tmp_path)
    with porcelain.init(str(path)):
        pass
    with pytest.raises(error, match="commit|ref"):
        validate(path, ref="main")


def test_rejects_workspace_symlink_without_reading_destination(tmp_path):
    error, validate = _validator()
    path = _skill(tmp_path, "Read [guide](references/guide.md) and verify checks.")
    (path / "references").mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("private")
    try:
        (path / "references/guide.md").symlink_to(outside)
    except OSError:
        pytest.skip("symlinks unavailable")
    with pytest.raises(error, match="unsafe|link|reparse"):
        validate(path)


def test_rejects_committed_symlink_even_if_workspace_is_regular(tmp_path):
    from dulwich.objects import Blob
    error, validate = _validator()
    path = _skill(tmp_path, "Read [guide](references.md) and verify checks.")
    with porcelain.init(str(path)) as repo:
        repo.refs.set_symbolic_ref(b"HEAD", b"refs/heads/main")
    (path / "references.md").write_text("A guide")
    _commit(path)
    # Build a symlink tree directly so the test also runs on Windows.
    with porcelain.open_repo(str(path)) as repo:
        commit = repo[repo.head()]
        tree = repo[commit.tree]
        blob = Blob.from_string(b"../outside.md")
        repo.object_store.add_object(blob)
        tree.add(b"references.md", 0o120000, blob.id)
        repo.object_store.add_object(tree)
        commit.tree = tree.id
        commit.message = b"unsafe tree"
        repo.object_store.add_object(commit)
        repo.refs[b"refs/heads/main"] = commit.id
    with pytest.raises(error, match="unsafe|link"):
        validate(path, ref="main")


def test_restricted_final_turn_refuses_incomplete_committed_tree(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from xskill.agents.skill_edit_agent import SkillEditAgent
    from xskill.skill import write_policy

    path = _skill(tmp_path, "Run `scripts/check.py` and compare the result.")
    with porcelain.init(str(path)) as repo:
        repo.refs.set_symbolic_ref(b"HEAD", b"refs/heads/main")
    sha = _commit(path)
    with porcelain.open_repo(str(path)) as repo:
        repo.refs[b"refs/heads/staging"] = sha.encode("ascii")
    (path / "scripts").mkdir()
    (path / "scripts/check.py").write_text("print('worktree only')")
    monkeypatch.setattr(write_policy, "current_write_policy", lambda root: SimpleNamespace(mode="update_only", target_skill=path.name))
    agent = SkillEditAgent(path, None, lambda **_: None, {}, tmp_path)
    monkeypatch.setattr(agent, "_make_batches", lambda _: [[{}]])
    monkeypatch.setattr(agent, "_run_normal_round", lambda *a, **kw: None)
    cleanups = []
    monkeypatch.setattr(agent, "_cleanup_turn_branches", lambda branch: cleanups.append(branch))
    # Exercise the final artifact gate; the public wrapper now additionally
    # requires real framework candidate identities and run authority.
    assert not agent._run_bound([{}], current_branch_name="main")
    assert cleanups == []


def test_restricted_graduation_refuses_failed_commit(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from xskill.agents import agent_tools
    from xskill.agents.skill_edit_agent import SkillEditAgent
    from xskill.skill import write_policy

    path = _skill(tmp_path)
    with porcelain.init(str(path)) as repo:
        repo.refs.set_symbolic_ref(b"HEAD", b"refs/heads/baby")
    _commit(path)
    monkeypatch.setattr(write_policy, "current_write_policy", lambda root: SimpleNamespace(mode="create_one", target_skill=path.name))
    # A stale/incorrect success response must not count as committed main.
    monkeypatch.setattr(agent_tools, "graduate_baby_to_main", lambda *a: True)
    agent = SkillEditAgent(path, None, lambda **_: None, {}, tmp_path)
    monkeypatch.setattr(agent, "_trace_path", lambda: None)
    assert not agent._graduate_completed_baby()


def test_supports_parentheses_in_static_markdown_links(tmp_path):
    _, validate = _validator()
    path = _skill(tmp_path, "Read [checks](references/check(1).md) and compare output.")
    (path / "references").mkdir()
    (path / "references/check(1).md").write_text("Verify the expected output.")
    assert "references/check(1).md" in validate(path)["files"]


def test_explicit_dynamic_angle_reference_is_not_verified(tmp_path):
    error, validate = _validator()
    path = _skill(tmp_path, "Run `python scripts/<tool>.py` to check the result.")
    with pytest.raises(error, match="unverified"):
        validate(path)


def test_expected_name_must_also_match_directory_identity(tmp_path):
    error, validate = _validator()
    path = _skill(tmp_path)
    (path / "SKILL.md").write_text(
        "---\nname: other-skill\ndescription: Diagnose failures\n---\nVerify results."
    )
    with pytest.raises(error, match="name|identity"):
        validate(path, expected_name="other-skill")


def test_windows_junction_is_refused(tmp_path):
    import os
    import subprocess

    if os.name != "nt":
        pytest.skip("Windows junction case")
    error, validate = _validator()
    path = _skill(tmp_path, "Read [guide](references/guide.md) and verify results.")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "guide.md").write_text("External data")
    subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(path / "references"), str(outside)],
        check=True, capture_output=True,
    )
    try:
        with pytest.raises(error, match="unsafe|reparse"):
            validate(path)
    finally:
        # rmdir on the junction itself leaves the target intact.
        os.rmdir(path / "references")
    assert (outside / "guide.md").read_text() == "External data"


def test_git_reference_directory_junction_is_refused(tmp_path):
    import os
    import subprocess

    if os.name != "nt":
        pytest.skip("Windows junction case")
    error, validate = _validator()
    path = _skill(tmp_path)
    with porcelain.init(str(path)) as repo:
        repo.refs.set_symbolic_ref(b"HEAD", b"refs/heads/main")
    _commit(path)
    outside = tmp_path / "outside-refs"
    (path / ".git/refs").rename(outside)
    subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(path / ".git/refs"), str(outside)],
        check=True, capture_output=True,
    )
    try:
        with pytest.raises(error, match="unsafe|reparse"):
            validate(path, ref="main")
    finally:
        os.rmdir(path / ".git/refs")


def test_auto_mode_keeps_existing_final_checkpoint_behavior(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from xskill.agents.skill_edit_agent import SkillEditAgent
    from xskill.skill import write_policy

    path = _skill(tmp_path, "Run `scripts/pending.py` and compare output.")
    with porcelain.init(str(path)) as repo:
        repo.refs.set_symbolic_ref(b"HEAD", b"refs/heads/main")
    sha = _commit(path)
    with porcelain.open_repo(str(path)) as repo:
        repo.refs[b"refs/heads/staging"] = sha.encode("ascii")
    monkeypatch.setattr(write_policy, "current_write_policy", lambda root: SimpleNamespace(mode="auto"))
    agent = SkillEditAgent(path, None, lambda **_: None, {}, tmp_path)
    monkeypatch.setattr(agent, "_make_batches", lambda _: [[{}]])
    monkeypatch.setattr(agent, "_run_normal_round", lambda *a, **kw: None)
    monkeypatch.setattr(agent, "_cleanup_turn_branches", lambda _: None)
    assert agent._run([{}], current_branch_name="main")


def test_broken_repository_is_reported_as_artifact_error(tmp_path):
    error, validate = _validator()
    path = _skill(tmp_path)
    (path / ".git").mkdir()
    with pytest.raises(error):
        validate(path, ref="main")
