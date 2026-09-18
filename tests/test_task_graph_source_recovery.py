"""A temporarily unreadable Session must not look like deleted evidence."""

from pathlib import Path

import pytest

from tests.test_task_graph_runtime import _add_source, _build
from xskill.pipeline.atom import AtomTaskStore
from xskill.tasks.projection import list_dirty_sources, list_logical_tasks
from xskill.tasks.service import TaskGraphService


def _fail_read(monkeypatch, path, error_type, *, once=False):
    original = Path.read_bytes
    calls = []

    def read_bytes(candidate):
        if candidate == path and (not once or not calls):
            calls.append(candidate)
            raise error_type("injected temporary read failure")
        return original(candidate)

    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    return calls


@pytest.mark.parametrize("error_type", [PermissionError, OSError])
@pytest.mark.parametrize("restart", [False, True])
def test_dirty_source_read_failure_preserves_graph_until_retry(
    tmp_path, monkeypatch, error_type, restart
):
    db_path = tmp_path / "registry.db"
    source = _add_source(
        tmp_path,
        db_path,
        source_name="read-retry",
        atoms=[{"intent": "Inspect source evidence"}],
    )
    service = _build(tmp_path, db_path, [source])
    tenant_id = service.resolver.tenant_id
    tasks = list_logical_tasks(tenant_id, db_path=db_path)
    graph_store = service.store_for_scope(tasks[0]["task_scope_id"])
    before = graph_store.load_current().generation_id
    service.mark_dirty(*source, reason="session_updated")
    with monkeypatch.context() as patch:
        _fail_read(patch, tmp_path / "trajectories" / source[1], error_type)
        result = service.process_dirty()
    assert result["sources"] == 0
    assert graph_store.load_current().generation_id == before
    assert list_logical_tasks(tenant_id, db_path=db_path) == tasks
    assert len(list_dirty_sources(db_path=db_path)) == 1

    if restart:
        service = TaskGraphService(state_root=tmp_path, db_path=db_path)
    assert service.process_dirty()["sources"] == 1
    assert list_dirty_sources(db_path=db_path) == []
    assert graph_store.load_current().generation_id == before
    assert list_logical_tasks(tenant_id, db_path=db_path) == tasks


@pytest.mark.parametrize("failed_source_is_dirty", [False, True])
def test_scope_update_cannot_publish_around_an_unreadable_peer(
    tmp_path, monkeypatch, failed_source_is_dirty
):
    db_path = tmp_path / "registry.db"
    failed = _add_source(
        tmp_path, db_path, source_name="blocked", atoms=[{"intent": "Inspect logs"}]
    )
    changed = _add_source(
        tmp_path, db_path, source_name="changed", atoms=[{"intent": "Write docs"}]
    )
    service = _build(tmp_path, db_path, [failed, changed])
    tenant_id = service.resolver.tenant_id
    tasks = list_logical_tasks(tenant_id, db_path=db_path)
    scope_id = tasks[0]["task_scope_id"]
    graph_store = service.store_for_scope(scope_id)
    before = graph_store.load_current().generation_id
    atoms = AtomTaskStore(tmp_path / "trajectories")
    updated_atom = atoms.list_by_traj(changed[1][:-3])[0]
    updated_atom.summary = "Documentation now includes recovery instructions"
    atoms.save(updated_atom)
    if failed_source_is_dirty:
        # Keep a warm cache and fail only the initial collection. A rebuild
        # must not silently substitute the previously cached source revision.
        service.mark_dirty(*failed, reason="session_updated")
    else:
        # A restarted worker must reload unchanged peers while rebuilding.
        service = TaskGraphService(state_root=tmp_path, db_path=db_path)
    service.mark_dirty(*changed, reason="atom_updated")
    with monkeypatch.context() as patch:
        calls = _fail_read(
            patch,
            tmp_path / "trajectories" / failed[1],
            PermissionError,
            once=True,
        )
        result = service.process_dirty()
    assert len(calls) == 1
    assert result["sources"] == 0
    assert result["failed_scopes"] == [scope_id]
    assert graph_store.load_current().generation_id == before
    assert list_logical_tasks(tenant_id, db_path=db_path) == tasks
    assert len(list_dirty_sources(db_path=db_path)) == 1 + failed_source_is_dirty

    assert service.process_dirty()["sources"] == 1 + failed_source_is_dirty
    assert list_dirty_sources(db_path=db_path) == []
    assert graph_store.load_current().generation_id != before
    assert {
        task["task_id"] for task in list_logical_tasks(tenant_id, db_path=db_path)
    } == {task["task_id"] for task in tasks}


def test_real_session_deletion_still_removes_automatic_task(tmp_path):
    db_path = tmp_path / "registry.db"
    source = _add_source(
        tmp_path, db_path, source_name="removed", atoms=[{"intent": "Inspect logs"}]
    )
    service = _build(tmp_path, db_path, [source])
    tenant_id = service.resolver.tenant_id
    assert len(list_logical_tasks(tenant_id, db_path=db_path)) == 1
    (tmp_path / "trajectories" / source[1]).unlink()
    service.mark_dirty(*source, reason="source_missing")
    assert service.process_dirty()["sources"] == 1
    assert list_dirty_sources(db_path=db_path) == []
    assert list_logical_tasks(tenant_id, db_path=db_path) == []


def test_missing_atom_during_read_is_not_a_deleted_session(tmp_path, monkeypatch):
    db_path = tmp_path / "registry.db"
    source = _add_source(
        tmp_path, db_path, source_name="atom-race", atoms=[{"intent": "Inspect logs"}]
    )
    service = _build(tmp_path, db_path, [source])
    tenant_id = service.resolver.tenant_id
    tasks = list_logical_tasks(tenant_id, db_path=db_path)
    service.mark_dirty(*source, reason="atom_updated")
    original = Path.read_text

    def read_text(path, *args, **kwargs):
        if path.name.startswith("atom_") and path.suffix == ".json":
            raise FileNotFoundError("injected Atom replacement race")
        return original(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "read_text", read_text)
        assert service.process_dirty()["sources"] == 0
    assert list_logical_tasks(tenant_id, db_path=db_path) == tasks
    assert len(list_dirty_sources(db_path=db_path)) == 1
    assert service.process_dirty()["sources"] == 1


def test_new_unreadable_source_blocks_its_scope_but_not_another_workspace(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "registry.db"
    existing = _add_source(
        tmp_path, db_path, source_name="existing", atoms=[{"intent": "Inspect logs"}]
    )
    service = _build(tmp_path, db_path, [existing])
    tenant_id = service.resolver.tenant_id
    task = list_logical_tasks(tenant_id, db_path=db_path)[0]
    store = service.store_for_scope(task["task_scope_id"])
    before = store.load_current().generation_id
    blocked = _add_source(
        tmp_path, db_path, source_name="new-blocked", atoms=[{"intent": "Write docs"}]
    )
    independent = _add_source(
        tmp_path,
        db_path,
        source_name="independent",
        workspace="/workspace/other",
        atoms=[{"intent": "Build a dashboard"}],
    )
    for source in [existing, blocked, independent]:
        service.mark_dirty(*source, reason="session_updated")
    with monkeypatch.context() as patch:
        _fail_read(patch, tmp_path / "trajectories" / blocked[1], PermissionError)
        result = service.process_dirty()
    assert result["sources"] == 1
    assert result["failed_scopes"] == [task["task_scope_id"]]
    assert store.load_current().generation_id == before
    assert len(list_logical_tasks(tenant_id, db_path=db_path)) == 2
    assert {row["filename"] for row in list_dirty_sources(db_path=db_path)} == {
        existing[1],
        blocked[1],
    }
    assert service.process_dirty()["sources"] == 2
    assert len(list_logical_tasks(tenant_id, db_path=db_path)) == 3
