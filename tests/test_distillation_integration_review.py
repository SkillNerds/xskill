"""Regression checks for fixed-target routing, feedback, and callback scope."""
from __future__ import annotations

import pytest

from xskill.agents import agent_tools
from xskill.canary import load_ux_scores
from xskill.pipeline import registry
from xskill.pipeline.atom import AtomTask, AtomTaskStore
from xskill.pipeline.distillation import DistillationStore, source_key_for
from xskill.pipeline.runner import DirectoryWatcher
from xskill.skill.git import (
    commit_baby_to_main_branch, init_skill_repo_on_baby,
)
from xskill.skill.write_policy import library_write_guard


def _make_main(root, name):
    path = root / name
    init_skill_repo_on_baby(str(path), name, "Diagnose integration failures")
    (path / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: Diagnose integration failures\n---\n"
        "Run the diagnostic checks and compare actual output with expected output.\n",
        encoding="utf-8",
    )
    assert commit_baby_to_main_branch(str(path), "ready for use")
    return path


@pytest.fixture
def review_env(tmp_path):
    root = tmp_path / "skills"
    root.mkdir()
    target = _make_main(root, "Target_skill")
    other = _make_main(root, "other-skill")
    runs = DistillationStore(tmp_path / "registry.db", root)
    runs.configure("update_only", skill=target.name)
    history = tmp_path / "history"
    history.mkdir()
    wd_id = registry.register_dir(history, db_path=runs.db_path)
    store = AtomTaskStore(history)
    # These callbacks do not need thread pools or model clients. Keep the
    # actual registry, policy, score writer, and reconciliation methods.
    watcher = object.__new__(DirectoryWatcher)
    watcher.skill_dir = root
    watcher.distillation_store = runs
    watcher.distillation_policy = runs.policy()
    watcher.restricted_distillation = True
    watcher.db_path = runs.db_path
    watcher.claimed_atoms = set()
    watcher._round_new_atoms = []
    watcher.max_retries = 3
    watcher._stats = {"scores": 0, "skills_edited": 0}
    watcher._skill_edit_retry_batch_sizes = {}
    watcher._skill_edit_n1_fail_counts = {}
    watcher._skill_edit_error_counts = {}
    watcher.skill_edit_batch_size = 5
    return watcher, runs, store, wd_id, target, other


def _usage(store, wd_id, runs, name, side):
    filename = f"traj_{name}_{side}.md"
    (store.root / filename).write_text(
        f"<!-- xskill:skill={name} side={side} sha=test-commit -->\n"
        "## User\nDiagnose the failure.\n", encoding="utf-8",
    )
    registry.discover_trajectories(wd_id, store.root, db_path=runs.db_path)
    atom = AtomTask(
        atom_id=f"atom_{name}_{side}", traj_id=filename[:-3],
        offset_start=1, offset_end=3, intent="diagnose failures",
        summary="Compare diagnostic output", raw_segment="Evidence",
        ux_score=8, clustered=True,
    )
    store.save(atom)
    return filename, atom


@pytest.mark.parametrize("side", ["main", "staging"])
def test_restricted_reconciliation_keeps_real_target_usage_feedback(
    review_env, monkeypatch, side,
):
    watcher, runs, store, wd_id, target, other = review_env
    filename, atom = _usage(store, wd_id, runs, target.name, side)
    before = store.load(atom.atom_id).to_json()
    events = []
    monkeypatch.setattr(watcher, "_emit_feedback_event", lambda *a, **kw: events.append(kw))
    with library_write_guard(target.parent, runs.policy(), db_path=runs.db_path):
        watcher._reconcile_indexed_traj(
            store, store.root, wd_id, filename, None, db_path=runs.db_path,
        )
    scores = load_ux_scores(target)
    assert [(s["skill_name"], s["side"], s["score"]) for s in scores] == [(target.name, side, 8.0)]
    assert load_ux_scores(other) == []
    assert store.load(atom.atom_id).to_json() == before
    assert events[0]["skill_name"] == target.name
    # Feedback must not mark a trajectory globally clustered/done.
    with registry.pooled_connection(runs.db_path) as connection:
        row = connection.execute(
            "SELECT status,process_action FROM trajectories WHERE watch_dir_id=? AND filename=?",
            (wd_id, filename),
        ).fetchone()
    assert row[0] == "discovered"
    assert row[1] != "clustered"


def test_non_target_usage_returns_before_resolving_or_marking_used(review_env, monkeypatch):
    watcher, runs, store, wd_id, target, other = review_env
    filename, atom = _usage(store, wd_id, runs, other.name, "main")
    before = store.load(atom.atom_id).to_json()

    def forbidden(*a, **kw):
        raise AssertionError("non-target usage reached scoring side effects")

    monkeypatch.setattr(watcher, "_resolve_skill_for_scoring", forbidden)
    monkeypatch.setattr("xskill.pipeline.runner.mark_skill_used", forbidden)
    monkeypatch.setattr(watcher, "_emit_feedback_event", forbidden)
    with library_write_guard(target.parent, runs.policy(), db_path=runs.db_path):
        watcher._reconcile_indexed_traj(
            store, store.root, wd_id, filename, None, db_path=runs.db_path,
        )
    assert not (other / ".ux_scores.jsonl").exists()
    assert not (target / ".ux_scores.jsonl").exists()
    assert store.load(atom.atom_id).to_json() == before


def test_old_non_target_edit_callback_preserves_dirty_row(review_env, monkeypatch):
    watcher, runs, _store, _wd_id, target, other = review_env
    registry.mark_skill_edit_dirty(other, reason="old-candidates", db_path=runs.db_path)
    before = registry.list_skill_edit_dirty(target.parent, db_path=runs.db_path)
    installed = []
    monkeypatch.setattr(watcher, "_install_skill_to_all_detected", installed.append)
    with library_write_guard(target.parent, runs.policy(), db_path=runs.db_path):
        with pytest.raises(PermissionError, match="outside|target"):
            watcher._on_skill_edit_done((other, True, 5))
    assert registry.list_skill_edit_dirty(target.parent, db_path=runs.db_path) == before
    assert installed == []
    assert watcher._stats["skills_edited"] == 0


def test_old_non_target_scripting_callback_never_clears_request(review_env, monkeypatch):
    from xskill.skill.scripting import marker_path

    watcher, runs, _store, _wd_id, target, other = review_env
    marker = marker_path(other)
    marker.write_text("old request", encoding="utf-8")
    cleared = []
    monkeypatch.setattr("xskill.skill.scripting.clear_scripting_request", cleared.append)
    with library_write_guard(target.parent, runs.policy(), db_path=runs.db_path):
        with pytest.raises(PermissionError, match="outside|target"):
            watcher._on_skill_scripting_done((other, True))
    assert marker.read_text(encoding="utf-8") == "old request"
    assert cleared == []


def test_existing_target_identity_is_preserved_in_bound_tools(review_env):
    watcher, runs, store, _wd_id, target, _other = review_env
    context = agent_tools.create_agent_tool_context(
        skill_dir=target.parent, atom_skill_dir=target.parent, atom_store=store,
        config={}, distillation_policy=runs.policy(),
    )
    with library_write_guard(target.parent, runs.policy(), db_path=runs.db_path):
        with agent_tools.use_agent_tool_context(context):
            with agent_tools.use_skill_edit_batch(target.name, ["atom-test"]):
                assert agent_tools.current_agent_tool_context().skill_edit_skill_name == target.name
                assert agent_tools._writable_skill_root() == target
            with agent_tools.use_skill_write_target(target.name):
                assert agent_tools._writable_skill_root() == target


def test_stale_skip_context_cannot_record_in_replacement_run(review_env):
    _watcher, runs, store, _wd_id, target, _other = review_env
    atom = AtomTask(
        atom_id="atom_stale", traj_id="traj-stale", offset_start=1,
        offset_end=3, intent="unrelated", summary="unrelated", raw_segment="Evidence",
    )
    store.save(atom)
    outcomes = {}
    old_policy = runs.policy()
    context = agent_tools.create_agent_tool_context(
        skill_dir=target.parent, atom_skill_dir=target.parent, atom_store=store,
        distillation_policy=old_policy, distillation_store=runs,
        distillation_sources={atom.atom_id: source_key_for(store.root)},
        distillation_results=outcomes,
    )
    runs.configure("update_only", skill=target.name)
    assert runs.policy().run_id != old_policy.run_id
    with agent_tools.use_agent_tool_context(context):
        with agent_tools.use_cluster_batch([atom.atom_id]):
            result = agent_tools.skip_atom.entrypoint(atom.atom_id, "unrelated")
    assert result.startswith("error:") and "stale" in result
    assert outcomes == {}
    assert runs.atom_record(atom, source_key=source_key_for(store.root)) is None
