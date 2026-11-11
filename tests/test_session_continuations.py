"""Session append replay: immutable Atoms and versioned Task evidence."""

import pytest

from tests.test_task_agent import _AutoSplitAgno
from xskill.agents.task_agent import TaskAgent
from xskill.pipeline.atom import AtomTaskStore
from xskill.pipeline.registry import (
    discover_trajectories,
    register_dir,
    update_traj_status,
)
from xskill.tasks.projection import list_logical_tasks
from xskill.tasks.service import TaskGraphService


def _source(tmp_path):
    path = tmp_path / "traj_tail.md"
    path.write_text(
        "# Session\n\n## User\n\nInspect logs.\n\n## Assistant\n\nReading logs...\n",
        encoding="utf-8",
    )
    store = AtomTaskStore(tmp_path)
    agent = TaskAgent(agno_agent_factory=_AutoSplitAgno, store=store)
    agent.run(traj_id=path.stem, traj_path=path)
    return path, store, agent


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_assistant_only_append_reaches_task_evidence(tmp_path, newline):
    path, atoms, agent = _source(tmp_path)
    db_path = tmp_path / "registry.db"
    watch_dir_id = register_dir(tmp_path, db_path=db_path)
    discover_trajectories(watch_dir_id, tmp_path, db_path=db_path)
    update_traj_status(watch_dir_id, path.name, "split_done", db_path=db_path)
    service = TaskGraphService(state_root=tmp_path, db_path=db_path)
    service.mark_dirty(watch_dir_id, path.name, reason="atom_split")
    assert service.process_dirty()["sources"] == 1

    before = service.store_for_scope(
        list_logical_tasks(service.resolver.tenant_id, db_path=db_path)[0][
            "task_scope_id"
        ]
    ).load_current()
    path.write_text(
        path.read_text(encoding="utf-8")
        + "\n## Assistant\n\nThe log issue is resolved.\n",
        encoding="utf-8",
    )
    path.write_bytes(path.read_text().replace("\n", newline).encode("utf-8"))
    added = agent.run(traj_id=path.stem, traj_path=path)
    service.mark_dirty(watch_dir_id, path.name, reason="session_append")
    assert service.process_dirty()["sources"] == 1
    task = list_logical_tasks(service.resolver.tenant_id, db_path=db_path)[0]
    generation = service.store_for_scope(task["task_scope_id"]).load_current()
    assert generation.source_revision != before.source_revision
    assert {t.task_id for t in generation.tasks} == {t.task_id for t in before.tasks}
    assert len(generation.memberships) == len(before.memberships)
    assert {a.attempt_id for a in generation.attempts} == {
        a.attempt_id for a in before.attempts
    }
    evidence_end = max(
        evidence.end
        for attempt in generation.attempts
        for evidence in attempt.evidence_ranges
    )
    expected_end = len(path.read_text(encoding="utf-8").splitlines()) + 1
    assert evidence_end == expected_end, {
        "new_atoms": len(added),
        "atom_end": atoms.last_offset(path.stem),
        "task_evidence_end": evidence_end,
        "session_end": expected_end,
    }


def test_old_objective_result_is_not_attached_to_the_next_user_goal(tmp_path):
    path, _atoms, agent = _source(tmp_path)
    suffix = "\n## Assistant\n\nThe log issue is resolved.\n"
    path.write_text(path.read_text(encoding="utf-8") + suffix, encoding="utf-8")
    agent.run(traj_id=path.stem, traj_path=path)
    path.write_text(
        path.read_text(encoding="utf-8")
        + "\n## User\n\nNow translate a poem.\n\n## Assistant\n\nStarting translation.\n",
        encoding="utf-8",
    )
    added = agent.run(traj_id=path.stem, traj_path=path)
    assert len(added) == 1
    assert "The log issue is resolved." not in added[0].raw_segment


def test_continuation_preserves_atom_and_replays_after_restart(tmp_path):
    from xskill.pipeline.atom_continuations import project_continuation

    path, store, agent = _source(tmp_path)
    original = store.list_by_traj(path.stem)[0]
    atom_path = store.path_for_atom(original.atom_id)
    before = atom_path.read_bytes()
    path.write_text(path.read_text() + "\n## Tool Output\n\nValidation passed.\n")
    assert agent.run(traj_id=path.stem, traj_path=path) == []
    assert atom_path.read_bytes() == before
    records = list((tmp_path / path.stem / "continuations").glob("*.json"))
    assert len(records) == 1
    snapshot = records[0].read_bytes()
    restarted = TaskAgent(
        agno_agent_factory=_AutoSplitAgno, store=AtomTaskStore(tmp_path)
    )
    assert restarted.run(traj_id=path.stem, traj_path=path) == []
    assert records[0].read_bytes() == snapshot
    assert len(store.list_by_traj(path.stem)) == 1
    view = project_continuation(
        tmp_path, original, path.read_text().splitlines(keepends=True)
    )
    assert "Validation passed." in view.raw_segment
    assert view.offset_end == len(path.read_text().splitlines()) + 1
    assert view.atom_id == original.atom_id


def test_tail_and_new_user_arriving_together_still_have_correct_owner(tmp_path):
    from xskill.pipeline.atom_continuations import project_continuation

    path, store, agent = _source(tmp_path)
    path.write_text(
        path.read_text() + "\n## Tool Output\n\nValidation passed.\n"
        "\n## User\n\nTranslate a poem.\n\n## Assistant\n\nTranslating.\n"
    )
    added = agent.run(traj_id=path.stem, traj_path=path)
    assert len(added) == 1
    assert "Validation passed." not in added[0].raw_segment
    old = store.list_by_traj(path.stem)[0]
    view = project_continuation(
        tmp_path, old, path.read_text().splitlines(keepends=True)
    )
    assert "Validation passed." in view.raw_segment
    assert view.offset_end == added[0].offset_start


def test_changed_source_is_rejected_instead_of_reassigning_evidence(tmp_path):
    from xskill.pipeline.atom_continuations import project_continuation

    path, store, agent = _source(tmp_path)
    path.write_text(path.read_text() + "\n## Assistant\n\nResolved.\n")
    agent.run(traj_id=path.stem, traj_path=path)
    old = store.list_by_traj(path.stem)[0]
    for text in [
        path.read_text().replace("Resolved.", "Different."),
        path.read_text().replace("Inspect logs.", "Translate poem."),
        "# Truncated\n",
    ]:
        with pytest.raises(ValueError):
            project_continuation(tmp_path, old, text.splitlines(keepends=True))
