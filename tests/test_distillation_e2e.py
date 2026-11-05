"""Real candidate/editor/Git lifecycle with deterministic agents and temp state."""
import hashlib
import json
from types import SimpleNamespace

import pytest

from xskill.agents import agent_tools
from xskill.pipeline.atom import AtomTask, AtomTaskStore
from xskill.pipeline.distillation import DistillationStore, source_key_for
from xskill.pipeline.runner import DirectoryWatcher
from xskill.skill import candidates as C
from xskill.skill.git import init_skill_repo_on_baby, commit_baby_to_main_branch, run_git
from xskill.skill.write_policy import library_write_guard, use_write_policy


def _body(name, revision):
    return (f"---\nname: {name}\ndescription: Diagnose integration permissions\n---\n\n"
            f"# Diagnose permissions {revision}\n\n"
            "Inspect the failing request and compare granted scopes with the API's required scope.\n"
            "After adding the missing scope, retry the same request and verify the response.\n"
            "Run `scripts/check.py` to inspect the result.\n")


def _seed_main(root, name):
    target = root / name
    init_skill_repo_on_baby(str(target), name, "Diagnose integration permissions")
    (target / "scripts").mkdir(exist_ok=True)
    (target / "scripts" / "check.py").write_text("print('inspect result')\n", encoding="utf-8")
    (target / "references").mkdir(exist_ok=True)
    (target / "references" / "sample.bin").write_bytes(b"\x00\xff\x01")
    (target / "SKILL.md").write_text(_body(name, "initial"), encoding="utf-8")
    assert commit_baby_to_main_branch(str(target), "seed")
    return target


def _snapshot(root):
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob("*") if p.is_file()}


class _EditorFactory:
    def __call__(self, *, instructions, tools, **kwargs):
        tools = {t.name: t for t in tools}

        def run(message):
            context = agent_tools.current_agent_tool_context()
            target = context.distillation_policy.target_skill
            for path, content in [
                ("scripts/check.py", "print('inspect result')\n"),
                ("SKILL.md", _body(target, "learned")),
            ]:
                result = tools["write_file"].entrypoint(path, content)
                assert not result.startswith("error:"), result
            commit = "commit_baby" if "commit_baby" in tools else "commit_to_staging"
            result = tools[commit].entrypoint(target, "learn from observed permission fix")
            assert not result.startswith("error:"), result
            return SimpleNamespace(content="committed")

        return SimpleNamespace(run=run)


@pytest.mark.parametrize("mode", ["create_one", "update_only"])
def test_target_lifecycle_preserves_other_skill_and_backlog(tmp_path, mode):
    root = tmp_path / "skills"
    root.mkdir()
    other = _seed_main(root, "other-skill")
    C.add_atom_contributions(other, [("old-other", 10, "keep pending")])
    (other / ".scripting_requested").write_text("requested\n", encoding="utf-8")
    name = "feishu-debug"
    if mode == "update_only":
        target = _seed_main(root, name)
        (target / ".ux_scores.jsonl").write_text(json.dumps({
            "atom_id": "seed-score", "skill_name": name, "side": "main",
            "commit_sha": "seed", "score": 8, "scored_at": "2026-11-05T00:00:00Z",
        }) + "\n", encoding="utf-8")
    baseline = _snapshot(other)
    runs = DistillationStore(tmp_path / "registry.db", root)
    runs.configure(mode, skill=name,
                   description="Diagnose integration permissions" if mode == "create_one" else None)
    atoms = AtomTaskStore(tmp_path / "history")
    from xskill.pipeline.registry import register_dir
    register_dir(atoms.root, db_path=runs.db_path)
    atom = AtomTask("atom_evidence", "traj-test", 1, 4,
                    "Fix Feishu scope", "Add missing scope and verify response",
                    raw_segment="request fails, scope is added, same request succeeds", clustered=True)
    atoms.save(atom)
    policy = runs.policy()
    home = tmp_path / "fake-home"
    home.mkdir()
    watcher = DirectoryWatcher(
        config={"skill_opt": {"enabled": False}}, skill_dir=root,
        db_path=runs.db_path, store=atoms, home_root=home, xskill_home=tmp_path,
        agno_agent_factory=_EditorFactory(), distillation_store=runs,
        distillation_policy=policy,
    )
    try:
        with library_write_guard(root, policy, db_path=runs.db_path), use_write_policy(policy):
            target = runs.ensure_target()
            runs.record_atom(atom, "pending", source_key=source_key_for(atoms.root))
            C.add_atom_contributions(target, [(atom.atom_id, 10, "verified fix")])
            runs.record_atom(atom, "accepted", source_key=source_key_for(atoms.root), weightscore=10)
            watcher._check_pending_skill_edits()
            watcher._drain_futures(stage="skill_edit", timeout=60)
            watcher._refresh_distillation_artifact()
            expected = "main" if mode == "create_one" else "staging"
            code, content, _ = run_git(["show", f"{expected}:SKILL.md"], cwd=str(target))
            assert code == 0 and "learned" in content
            assert C.load_candidates(target)["candidates"] == []
            if mode == "update_only":
                assert (root / ".canary" / name / "scripts" / "check.py").read_text(
                    encoding="utf-8",
                ) == "print('inspect result')\n"
                assert (root / ".canary" / name / "references" / "sample.bin").read_bytes() == b"\x00\xff\x01"
            assert runs.status()["run"]["artifact_state"] == (
                "main_committed" if mode == "create_one" else "candidate_committed"
            )
            assert _snapshot(other) == baseline
            assert atoms.load(atom.atom_id).clustered is True
    finally:
        watcher.stop(wait=True)


def test_done_history_is_eligible_without_resetting_global_progress(tmp_path):
    from xskill.pipeline.registry import register_dir, discover_trajectories, update_traj_status

    root = tmp_path / "skills"
    root.mkdir()
    history = tmp_path / "history"
    history.mkdir()
    (history / "traj_old.md").write_text("## User\nFix permissions\n", encoding="utf-8")
    atoms = AtomTaskStore(history)
    atom = AtomTask("atom_old", "traj_old", 1, 3, "permissions", "fixed scopes", clustered=True)
    atoms.save(atom)
    runs = DistillationStore(tmp_path / "registry.db", root)
    wd = register_dir(history, db_path=runs.db_path)
    discover_trajectories(wd, history, db_path=runs.db_path)
    update_traj_status(wd, "traj_old.md", "done", db_path=runs.db_path)
    runs.configure("create_one", skill="feishu-debug", description="Debug Feishu")
    watcher = DirectoryWatcher(skill_dir=root, db_path=runs.db_path,
                               home_root=tmp_path, xskill_home=tmp_path,
                               distillation_store=runs)
    try:
        watcher._reconcile_indexed_atoms(history, wd, None, db_path=runs.db_path)
        assert watcher._round_new_atoms == ["atom_old"]
        assert atoms.load(atom.atom_id).clustered is True
    finally:
        watcher.stop(wait=True)


def test_materialization_failure_does_not_report_success(tmp_path, monkeypatch):
    from xskill import canary
    from xskill.skill.git import commit_to_staging_branch, current_branch
    from xskill.skill.write_policy import safe_write_text

    root = tmp_path / "skills"
    root.mkdir()
    target = _seed_main(root, "feishu-debug")
    runs = DistillationStore(tmp_path / "registry.db", root)
    runs.configure("update_only", skill=target.name)
    policy = runs.policy()

    def fail_materialization(*args, **kwargs):
        raise OSError("simulated materialization failure")

    monkeypatch.setattr(canary, "materialize_staging", fail_materialization)
    with library_write_guard(root, policy, db_path=runs.db_path):
        safe_write_text(target / "SKILL.md", _body(target.name, "updated"))
        assert commit_to_staging_branch(str(target), "update") is False
        assert current_branch(str(target)) == "main"


def test_unchanged_update_does_not_create_successful_candidate(tmp_path):
    from xskill.skill.git import commit_to_staging_branch, current_branch

    root = tmp_path / "skills"
    root.mkdir()
    target = _seed_main(root, "feishu-debug")
    runs = DistillationStore(tmp_path / "registry.db", root)
    runs.configure("update_only", skill=target.name)
    with library_write_guard(root, runs.policy(), db_path=runs.db_path):
        assert commit_to_staging_branch(str(target), "no effective changes") is False
        assert current_branch(str(target)) == "main"
        assert run_git(["rev-parse", "--verify", "staging"], cwd=str(target))[0] != 0


def test_later_broken_candidate_is_not_masked_by_previous_main_success(tmp_path):
    from xskill.skill.git import commit_update_main_branch, commit_to_staging_branch
    from xskill.skill.write_policy import safe_write_text

    root = tmp_path / "skills"
    root.mkdir()
    target = _seed_main(root, "feishu-debug")
    runs = DistillationStore(tmp_path / "registry.db", root)
    runs.configure("update_only", skill=target.name)
    watcher = DirectoryWatcher(skill_dir=root, db_path=runs.db_path,
                               home_root=tmp_path, xskill_home=tmp_path,
                               distillation_store=runs)
    try:
        with library_write_guard(root, runs.policy(), db_path=runs.db_path):
            safe_write_text(target / "SKILL.md", _body(target.name, "first completed"))
            assert commit_update_main_branch(str(target), "completed")
            safe_write_text(target / "SKILL.md", _body(target.name, "later candidate"))
            assert commit_to_staging_branch(str(target), "candidate")
            (root / ".canary" / target.name / "scripts" / "check.py").unlink()
            watcher._refresh_distillation_artifact()
            assert runs.run()["artifact_state"] == "failed"
    finally:
        watcher.stop(wait=True)
