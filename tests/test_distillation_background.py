"""A restricted watcher leaves other skills' durable work intact."""
from xskill.pipeline.distillation import DistillationStore
from xskill.pipeline.runner import DirectoryWatcher
from xskill.skill import candidates as C
from xskill.skill.git import init_skill_repo_on_baby, run_git
from xskill.skill.write_policy import library_write_guard, use_write_policy


def _snapshot(root):
    return {p.relative_to(root).as_posix(): p.read_bytes()
            for p in root.rglob("*") if p.is_file()}


def test_one_scan_preserves_unrelated_candidates_markers_refs_and_generate_queue(tmp_path, monkeypatch):
    from xskill.pipeline.registry import list_skill_edit_dirty
    from xskill.team.server import generate_jobs

    root = tmp_path / "skills"
    root.mkdir()
    other = root / "other"
    init_skill_repo_on_baby(str(other), "other", "Other troubleshooting")
    db = tmp_path / "registry.db"
    runs = DistillationStore(db, root)
    # Candidate notification must use this instance's registry, not the default.
    from xskill.agents import agent_tools
    context = agent_tools.create_agent_tool_context(skill_dir=root, registry_db_path=db)
    with agent_tools.use_agent_tool_context(context):
        C.add_atom_contributions(other, [("atom_other", 10, "keep")])
    (other / ".scripting_requested").write_text("requested\n", encoding="utf-8")
    code, _, _ = run_git(["branch", "staging", "baby"], cwd=str(other))
    assert code == 0
    before = _snapshot(other)
    old_dirty = [r for r in list_skill_edit_dirty(root, db_path=db) if r["skill"] == "other"]
    runs.configure("create_one", skill="target", description="Target troubleshooting")
    policy = runs.policy()
    watcher = DirectoryWatcher(
        config={}, skill_dir=root, db_path=db, home_root=tmp_path,
        xskill_home=tmp_path, distillation_store=runs,
        logs_dir=tmp_path / "logs",
    )

    def forbidden(*args, **kwargs):
        raise AssertionError("restricted scan must not claim or reclaim generate jobs")

    monkeypatch.setattr(generate_jobs, "reclaim_orphans", forbidden)
    monkeypatch.setattr(generate_jobs, "try_claim", forbidden)
    try:
        with library_write_guard(root, policy, db_path=db), use_write_policy(policy):
            runs.ensure_target()
            watcher._scan_once()
        assert _snapshot(other) == before
        assert [r for r in list_skill_edit_dirty(root, db_path=db) if r["skill"] == "other"] == old_dirty
    finally:
        watcher.stop(wait=True)
