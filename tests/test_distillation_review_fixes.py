"""Regression cases found by the independent fixed-target implementation review."""
from __future__ import annotations

import os
import stat

import pytest
from dulwich import porcelain

from xskill.pipeline.distillation import DistillationStore
from xskill.skill import git
from xskill.skill.artifact_validation import ArtifactValidationError, validate_skill_artifact
from xskill.skill.write_policy import SkillWriteDenied, library_write_guard


def _seed(root, name="target"):
    path = root / name
    path.mkdir(parents=True)
    (path / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: Diagnose integration failures\n---\n"
        "Compare actual output with expected output, then verify the repair.\n",
        encoding="utf-8",
    )
    with porcelain.init(str(path)) as repo:
        repo.refs.set_symbolic_ref(b"HEAD", b"refs/heads/main")
    porcelain.add(str(path))
    porcelain.commit(str(path), message=b"initial", author=b"Test <test@local>")
    return path


def _snapshot(path):
    return {
        p.relative_to(path).as_posix(): p.read_bytes()
        for p in path.rglob("*") if p.is_file()
    }


@pytest.mark.parametrize("explicit_ref", [True, False])
@pytest.mark.parametrize("pathspec", ["../other", "..\\other", "absolute", ".git", "mixed", ".git.", "SKILL.md:stream", "."])
def test_checkout_rejects_unsafe_paths_without_partial_mutation(tmp_path, pathspec, explicit_ref):
    root = tmp_path / "skills"
    target = _seed(root)
    other = _seed(root, "other")
    original = (target / "SKILL.md").read_text(encoding="utf-8")
    runs = DistillationStore(tmp_path / "registry.db", root)
    runs.configure("update_only", skill="target")
    # A valid first member must not be restored before rejecting the next one.
    (target / "SKILL.md").write_text(original + "Uncommitted target edit.\n", encoding="utf-8")
    before_target, before_other = _snapshot(target), _snapshot(other)
    paths = [str(other)] if pathspec == "absolute" else [pathspec]
    if pathspec == "mixed":
        paths = ["SKILL.md", "../other"]
    args = ["checkout", *(["HEAD"] if explicit_ref else []), "--", *paths]
    with library_write_guard(root, runs.policy(), db_path=runs.db_path):
        with pytest.raises(SkillWriteDenied):
            git.run_git(args, cwd=str(target))
    assert _snapshot(target) == before_target
    assert _snapshot(other) == before_other


@pytest.mark.parametrize("restricted", [True, False])
def test_checkout_restores_valid_target_relative_file(tmp_path, restricted):
    root = tmp_path / "skills"
    target = _seed(root)
    expected = (target / "SKILL.md").read_text(encoding="utf-8")
    runs = DistillationStore(tmp_path / "registry.db", root)
    if restricted:
        runs.configure("update_only", skill="target")
    (target / "SKILL.md").write_text(expected + "Uncommitted target edit.\n", encoding="utf-8")
    with library_write_guard(root, runs.policy(), db_path=runs.db_path if restricted else None):
        assert git.run_git(["checkout", "HEAD", "--", "./SKILL.md"], str(target)) == (0, "", "")
    assert (target / "SKILL.md").read_text(encoding="utf-8") == expected


@pytest.mark.parametrize("restricted", [True, False])
def test_checkout_restores_valid_directory_without_moving_head(tmp_path, restricted):
    root = tmp_path / "skills"
    target = _seed(root)
    (target / "scripts").mkdir()
    script = target / "scripts/check.py"
    script.write_bytes(b"print('original')\n")
    porcelain.add(str(target))
    previous = porcelain.commit(str(target), message=b"script", author=b"Test <test@local>")
    script.write_bytes(b"print('current')\n")
    porcelain.add(str(target))
    current = porcelain.commit(str(target), message=b"new script", author=b"Test <test@local>")
    runs = DistillationStore(tmp_path / "registry.db", root)
    if restricted:
        runs.configure("update_only", skill="target")
    with library_write_guard(root, runs.policy(), db_path=runs.db_path if restricted else None):
        assert git.run_git(["checkout", previous.decode(), "--", "scripts/"], str(target)) == (0, "", "")
        assert git.run_git(["rev-parse", "HEAD"], str(target))[1].strip() == current.decode()
    assert script.read_bytes() == b"print('original')\n"


@pytest.mark.parametrize("reference", [
    "./scripts/missing.py", "./references/missing.md", "../scripts/missing.py",
    "/scripts/missing.py", "C:/scripts/missing.py", "./scripts/{name}.py",
    "../../other/scripts/missing.py", "foo/scripts/missing.py",
])
def test_dot_relative_and_unsafe_plain_references_are_not_ignored(tmp_path, reference):
    target = _seed(tmp_path)
    with (target / "SKILL.md").open("a", encoding="utf-8") as stream:
        stream.write(f"Run `python {reference}` and inspect the result.\n")
    with pytest.raises(ArtifactValidationError):
        validate_skill_artifact(target)


def test_dot_relative_references_exist_in_actual_commit(tmp_path):
    target = _seed(tmp_path)
    (target / "scripts").mkdir()
    (target / "scripts/check.py").write_text("raise RuntimeError('never execute validation input')", encoding="utf-8")
    with (target / "SKILL.md").open("a", encoding="utf-8") as stream:
        stream.write("Run `python ./scripts/check.py` and inspect the result.\n")
    porcelain.add(str(target), paths=["SKILL.md"])
    porcelain.commit(str(target), message=b"reference only", author=b"Test <test@local>")
    validate_skill_artifact(target)
    with pytest.raises(ArtifactValidationError, match="missing"):
        validate_skill_artifact(target, ref="main")
    porcelain.add(str(target))
    porcelain.commit(str(target), message=b"add support script", author=b"Test <test@local>")
    assert "scripts/check.py" in validate_skill_artifact(target, ref="main")["files"]


def test_staging_materialization_preserves_committed_script_mode(tmp_path, monkeypatch):
    from xskill import canary
    from xskill.skill import write_policy
    root = tmp_path / "skills"
    target = _seed(root)
    (target / "scripts").mkdir()
    (target / "scripts/check.sh").write_text("#!/bin/sh\nprintf 'verified'\n", encoding="utf-8")
    with (target / "SKILL.md").open("a", encoding="utf-8") as stream:
        stream.write("Run `./scripts/check.sh` and verify its output.\n")
    porcelain.add(str(target))
    porcelain.commit(str(target), message=b"script", author=b"Test <test@local>")
    with porcelain.open_repo(str(target)) as repo:
        commit = repo[repo.head()]
        tree = repo[commit.tree]
        scripts = repo[tree[b"scripts"][1]]
        _, blob = scripts[b"check.sh"]
        scripts.add(b"check.sh", 0o100755, blob)
        repo.object_store.add_object(scripts)
        tree.add(b"scripts", 0o040000, scripts.id)
        repo.object_store.add_object(tree)
        commit.tree = tree.id
        repo.object_store.add_object(commit)
        repo.refs[b"refs/heads/staging"] = commit.id
    runs = DistillationStore(tmp_path / "registry.db", root)
    runs.configure("update_only", skill="target")
    modes = []
    real_write = write_policy.safe_write_bytes

    def observe_write(path, content, **kwargs):
        if path.name == "check.sh":
            modes.append(kwargs.get("mode"))
        return real_write(path, content, **kwargs)

    monkeypatch.setattr(write_policy, "safe_write_bytes", observe_write)
    with library_write_guard(root, runs.policy(), db_path=runs.db_path):
        out = canary.materialize_staging(target, root / ".canary")
    assert modes and modes[-1] == 0o755
    if os.name != "nt":
        assert stat.S_IMODE((out / "scripts/check.sh").stat().st_mode) == 0o755


def test_bound_read_traj_uses_same_source_and_version_as_receipt(tmp_path):
    from xskill.agents import agent_tools
    from xskill.pipeline.atom import AtomTask, AtomTaskStore, MultiAtomTaskStore
    from xskill.pipeline.distillation import source_key_for

    root = tmp_path / "skills"
    target = _seed(root)
    runs = DistillationStore(tmp_path / "registry.db", root)
    runs.configure("update_only", skill=target.name)
    stores = [AtomTaskStore(tmp_path / name) for name in ("source-a", "source-b")]
    for index, store in enumerate(stores):
        store.root.mkdir()
        traj = store.root / "traj_shared.md"
        body = f"Evidence from source {index}.\n"
        traj.write_text("Heading\n" + body, encoding="utf-8")
        os.utime(traj, (1_700_000_000 + index, 1_700_000_000 + index))
        store.save(AtomTask(f"atom-{index}", "traj_shared", 2, 3, "diagnose", "observed result", raw_segment=body))
    first = stores[0].load("atom-0")
    runs.record_atom(first, "accepted", source_key=source_key_for(stores[0].root))
    ctx = agent_tools.create_agent_tool_context(
        skill_dir=root, atom_skill_dir=root, atom_store=MultiAtomTaskStore(stores),
        default_traj_root=stores[0].root, distillation_policy=runs.policy(), distillation_store=runs,
    )
    with library_write_guard(root, runs.policy(), db_path=runs.db_path):
        with agent_tools.use_agent_tool_context(ctx):
            with agent_tools.use_skill_edit_batch(target.name, [first.atom_id]):
                assert agent_tools.read_traj.entrypoint("traj_shared", 2, 3) == first.raw_segment
                (stores[0].root / "traj_shared.md").write_text("Heading\nNew content.\n", encoding="utf-8")
                assert agent_tools.read_traj.entrypoint("traj_shared", 2, 3) == first.raw_segment


def test_invalid_checkpointed_baby_reports_missing_file_not_waiting_evidence(tmp_path):
    from xskill.pipeline.runner import DirectoryWatcher

    root = tmp_path / "skills"
    runs = DistillationStore(tmp_path / "registry.db", root)
    runs.configure("create_one", skill="target", description="Diagnose integration failures")
    target = runs.ensure_target()
    with library_write_guard(root, runs.policy(), db_path=runs.db_path):
        (target / "SKILL.md").write_text(
            "---\nname: target\ndescription: Diagnose integration failures\n---\n"
            "Run `python ./scripts/missing.py` and compare output.\n", encoding="utf-8",
        )
        sha = git.commit_baby_checkpoint(str(target), "checkpoint before final validation")
        assert sha
        runs.record_atom({"atom_id": "atom-1", "content": "original evidence"}, "committed", source_key="source", checkpoint_sha=sha)
        watcher = object.__new__(DirectoryWatcher)
        watcher.skill_dir, watcher.distillation_store, watcher.distillation_policy = root, runs, runs.policy()
        watcher._refresh_distillation_artifact()
    result = runs.run()
    assert result["artifact_state"] == "failed"
    assert "scripts/missing.py" in result["error"]
