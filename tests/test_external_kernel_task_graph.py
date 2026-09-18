"""External Skill production must not disable Session/Atom task tracking."""

import pytest

from tests.test_task_graph_runtime import _add_source
from xskill.pipeline.atom import AtomTaskStore
from xskill.pipeline.runner import DirectoryWatcher
from xskill.tasks.projection import list_dirty_sources, list_logical_tasks


@pytest.mark.parametrize("server_mode", [False, True])
@pytest.mark.parametrize("enabled", [False, True])
def test_external_kernel_preserves_task_graph_backfill_and_updates(
    tmp_path, monkeypatch, server_mode, enabled
):
    db_path = tmp_path / "registry.db"
    source = _add_source(
        tmp_path,
        db_path,
        source_name="external-kernel",
        atoms=[{"intent": "Inspect task evidence"}],
    )
    skill_dir = tmp_path / "skills"
    skill_dir.mkdir()
    watcher = DirectoryWatcher(
        llm=None,
        embed_client=None,
        config={"task_graph": {"enabled": enabled}},
        skill_dir=skill_dir,
        db_path=db_path,
        home_root=tmp_path,
        xskill_home=tmp_path / "xskill-state",
        native_distill=False,
        server_mode=server_mode,
    )

    def native_skill_work():
        pytest.fail("external kernels must retain ownership of Skill production")

    monkeypatch.setattr(watcher, "_submit_cluster_batches", native_skill_work)
    monkeypatch.setattr(watcher, "_run_skill_edit_step", native_skill_work)

    def scan_and_finish():
        watcher._scan_once()
        for future in list(watcher._futures):
            future.result(timeout=10)
        watcher._harvest()

    try:
        scan_and_finish()
        service = watcher._task_graph_service
        if not enabled:
            assert watcher.stats["task_graph_generations"] == 0
            assert service.resolver.existing_tenant_id is None
            return

        tenant_id = service.resolver.existing_tenant_id
        assert tenant_id is not None
        tasks = list_logical_tasks(tenant_id, db_path=db_path)
        assert len(tasks) == 1
        assert watcher.stats["task_graph_generations"] == 1
        store = service.store_for_scope(tasks[0]["task_scope_id"])
        before = store.load_current().generation_id

        atoms = AtomTaskStore(tmp_path / "trajectories")
        atom = atoms.list_by_traj(source[1][:-3])[0]
        atom.summary = "The source now includes verified recovery instructions"
        atoms.save(atom)
        service.mark_dirty(*source, reason="atom_updated")
        scan_and_finish()

        assert list_dirty_sources(db_path=db_path) == []
        assert store.load_current().generation_id != before
        assert [
            task["task_id"] for task in list_logical_tasks(tenant_id, db_path=db_path)
        ] == [tasks[0]["task_id"]]
        assert not watcher.pending_atoms
        assert list(skill_dir.iterdir()) == []
    finally:
        watcher.stop()
        watcher._task_graph_pool.shutdown(wait=True, cancel_futures=True)
