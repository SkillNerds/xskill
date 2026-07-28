"""Kernel trajectory atom-split view tests."""

from __future__ import annotations

import json

from xskill.kernels.context import (
    TrajectoryReader,
    resolve_atom_split_status,
)
from xskill.pipeline.atom import AtomTask, AtomTaskStore
from xskill.pipeline.registry import (
    discover_trajectories,
    register_dir,
    update_traj_status,
)

def _write_atom(store: AtomTaskStore, *, traj_id: str, atom_id: str, ux_score, used_skills=(), raw_segment="segment text"):
    store.save(AtomTask(
        atom_id=atom_id,
        traj_id=traj_id,
        offset_start=1,
        offset_end=10,
        intent="intent",
        summary="summary",
        used_skills=list(used_skills),
        ux_score=ux_score,
        raw_segment=raw_segment,
    ))


def test_resolve_atom_split_status_mapping():
    assert resolve_atom_split_status("discovered", atom_count=0) == "pending"
    assert resolve_atom_split_status("splitting", atom_count=0) == "pending"
    assert resolve_atom_split_status("splitting", atom_count=2) == "updated"
    assert resolve_atom_split_status("updated", atom_count=2) == "updated"
    assert resolve_atom_split_status("split_done", atom_count=2) == "ready"
    assert resolve_atom_split_status("done", atom_count=0) == "ready"
    assert resolve_atom_split_status(None, atom_count=0) == "pending"
    assert resolve_atom_split_status(None, atom_count=1) == "ready"


def test_trajectory_reader_pending_hides_atoms(tmp_path):
    registry_db = tmp_path / "registry.db"
    trajectory_dir = tmp_path / "watch"
    trajectory_dir.mkdir()
    traj = trajectory_dir / "traj_pending.md"
    traj.write_text("## User\n\nhello\n", encoding="utf-8")
    watch_dir_id = register_dir(trajectory_dir, label="w", db_path=registry_db)
    discover_trajectories(watch_dir_id, trajectory_dir, db_path=registry_db)
    store = AtomTaskStore(root=trajectory_dir)
    _write_atom(
        store,
        traj_id="traj_pending",
        atom_id="atom_traj_pending_0001",
        ux_score=8,
        used_skills=["skill-a"],
    )
    # discovered => pending; atoms must be empty even if files exist on disk
    resources = list(TrajectoryReader(registry_db).iter())
    assert len(resources) == 1
    assert resources[0].atom_split_status == "pending"
    assert resources[0].atoms == ()
    assert "ux_score" not in resources[0].__dataclass_fields__


def test_trajectory_reader_ready_exposes_atoms_and_none_score(tmp_path):
    registry_db = tmp_path / "registry.db"
    trajectory_dir = tmp_path / "watch"
    trajectory_dir.mkdir()
    traj = trajectory_dir / "traj_ready.md"
    traj.write_text("## User\n\nready\n", encoding="utf-8")
    watch_dir_id = register_dir(trajectory_dir, label="w", db_path=registry_db)
    discover_trajectories(watch_dir_id, trajectory_dir, db_path=registry_db)
    update_traj_status(
        watch_dir_id, traj.name, status="split_done", db_path=registry_db,
    )
    store = AtomTaskStore(root=trajectory_dir)
    _write_atom(
        store,
        traj_id="traj_ready",
        atom_id="atom_traj_ready_0001",
        ux_score=7,
        used_skills=["alpha", "beta"],
    )
    _write_atom(
        store,
        traj_id="traj_ready",
        atom_id="atom_traj_ready_0002",
        ux_score=None,
        used_skills=[],
    )
    # invalid score becomes None
    bad = AtomTask(
        atom_id="atom_traj_ready_0003",
        traj_id="traj_ready",
        offset_start=11,
        offset_end=20,
        intent="",
        summary="",
        ux_score=99,
    )
    # bypass dataclass validation by writing raw json
    path = trajectory_dir / "traj_ready" / "tasks" / f"{bad.atom_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.loads(bad.to_json())
    payload["ux_score"] = 99
    path.write_text(json.dumps(payload), encoding="utf-8")

    resource = TrajectoryReader(registry_db).list()[0]
    assert resource.atom_split_status == "ready"
    assert "ux_score" not in resource.__dataclass_fields__
    assert len(resource.atoms) == 3
    by_id = {atom.atom_id: atom for atom in resource.atoms}
    assert by_id["atom_traj_ready_0001"].ux_score == 7
    assert by_id["atom_traj_ready_0001"].used_skills == ("alpha", "beta")
    assert by_id["atom_traj_ready_0002"].ux_score is None
    assert by_id["atom_traj_ready_0003"].ux_score is None
    assert by_id["atom_traj_ready_0001"].content == "segment text"


def test_trajectory_reader_ready_atom_content_empty_string_not_none(tmp_path):
    registry_db = tmp_path / "registry.db"
    trajectory_dir = tmp_path / "watch"
    trajectory_dir.mkdir()
    traj = trajectory_dir / "traj_empty_segment.md"
    traj.write_text("## User\n\nready\n", encoding="utf-8")
    watch_dir_id = register_dir(trajectory_dir, label="w", db_path=registry_db)
    discover_trajectories(watch_dir_id, trajectory_dir, db_path=registry_db)
    update_traj_status(
        watch_dir_id, traj.name, status="split_done", db_path=registry_db,
    )
    store = AtomTaskStore(root=trajectory_dir)
    _write_atom(
        store,
        traj_id="traj_empty_segment",
        atom_id="atom_traj_empty_segment_0001",
        ux_score=None,
        raw_segment="",
    )
    atom = TrajectoryReader(registry_db).list()[0].atoms[0]
    assert atom.content == ""
    assert atom.content is not None


def test_create_temp_rejects_bad_markdown(tmp_path):
    reader = TrajectoryReader(
        tmp_path / "registry.db",
        temp_root=tmp_path / "temp",
    )
    for bad in ("", "hello", "## Assistant\n\nno user\n", "## user\n\nlower case\n"):
        try:
            reader.create_temp(bad, trajectory_id="traj_bad")
        except ValueError as exc:
            assert "## User" in str(exc)
        else:
            raise AssertionError(f"expected ValueError for {bad!r}")


def test_create_temp_accepts_minimal_user_markdown(tmp_path):
    registry_db = tmp_path / "registry.db"
    temp_root = tmp_path / "temp"
    reader = TrajectoryReader(registry_db, temp_root=temp_root)
    resource = reader.create_temp(
        "## User\n\nhello temp\n",
        trajectory_id="traj_kernel_temp",
    )
    assert resource.source == "temp"
    assert resource.atom_split_status == "pending"
    assert resource.atoms == ()
    assert resource.trajectory_id == "traj_kernel_temp"
    written = temp_root / "traj_kernel_temp.md"
    assert written.is_file()
    assert written.read_text(encoding="utf-8") == "## User\n\nhello temp\n"


def test_feed_snapshot_excludes_pending_trajectories(tmp_path):
    from xskill._workers import _trajectory_snapshot

    registry_db = tmp_path / "registry.db"
    trajectory_dir = tmp_path / "watch"
    trajectory_dir.mkdir()
    pending = trajectory_dir / "traj_pending_feed.md"
    pending.write_text("## User\n\npending\n", encoding="utf-8")
    ready = trajectory_dir / "traj_ready_feed.md"
    ready.write_text("## User\n\nready\n", encoding="utf-8")
    watch_dir_id = register_dir(trajectory_dir, label="w", db_path=registry_db)
    discover_trajectories(watch_dir_id, trajectory_dir, db_path=registry_db)
    update_traj_status(
        watch_dir_id, ready.name, status="split_done", db_path=registry_db,
    )
    store = AtomTaskStore(root=trajectory_dir)
    _write_atom(
        store,
        traj_id="traj_ready_feed",
        atom_id="atom_traj_ready_feed_0001",
        ux_score=5,
    )
    reader = TrajectoryReader(registry_db)
    snapshot = _trajectory_snapshot(reader)
    assert len(snapshot) == 1
    ready_resource = next(
        resource for resource in reader.iter()
        if resource.trajectory_id == "traj_ready_feed"
    )
    assert ready_resource.id in snapshot
    pending_resource = next(
        resource for resource in reader.iter()
        if resource.trajectory_id == "traj_pending_feed"
    )
    assert pending_resource.atom_split_status == "pending"
    assert pending_resource.id not in snapshot


def test_feed_snapshot_includes_trajectory_when_it_becomes_ready(tmp_path):
    from xskill._workers import _trajectory_snapshot

    registry_db = tmp_path / "registry.db"
    trajectory_dir = tmp_path / "watch"
    trajectory_dir.mkdir()
    traj = trajectory_dir / "traj_becomes_ready.md"
    traj.write_text("## User\n\nlater\n", encoding="utf-8")
    watch_dir_id = register_dir(trajectory_dir, label="w", db_path=registry_db)
    discover_trajectories(watch_dir_id, trajectory_dir, db_path=registry_db)
    reader = TrajectoryReader(registry_db)
    assert _trajectory_snapshot(reader) == {}

    update_traj_status(
        watch_dir_id, traj.name, status="split_done", db_path=registry_db,
    )
    store = AtomTaskStore(root=trajectory_dir)
    _write_atom(
        store,
        traj_id="traj_becomes_ready",
        atom_id="atom_traj_becomes_ready_0001",
        ux_score=6,
    )
    previous: dict[str, tuple[int, int, tuple[str, ...]]] = {}
    current = _trajectory_snapshot(reader)
    changed = tuple(sorted(
        resource_id
        for resource_id, fingerprint in current.items()
        if previous.get(resource_id) != fingerprint
    ))
    resource = reader.list()[0]
    assert resource.atom_split_status == "ready"
    assert changed == (resource.id,)


def test_trajectory_reader_updated_keeps_existing_atoms(tmp_path):
    registry_db = tmp_path / "registry.db"
    trajectory_dir = tmp_path / "watch"
    trajectory_dir.mkdir()
    traj = trajectory_dir / "traj_updated.md"
    traj.write_text("## User\n\nupdated\n", encoding="utf-8")
    watch_dir_id = register_dir(trajectory_dir, label="w", db_path=registry_db)
    discover_trajectories(watch_dir_id, trajectory_dir, db_path=registry_db)
    store = AtomTaskStore(root=trajectory_dir)
    _write_atom(
        store,
        traj_id="traj_updated",
        atom_id="atom_traj_updated_0001",
        ux_score=3,
        used_skills=["old-skill"],
    )
    update_traj_status(
        watch_dir_id, traj.name, status="updated", db_path=registry_db,
    )
    resource = TrajectoryReader(registry_db).list()[0]
    assert resource.atom_split_status == "updated"
    assert len(resource.atoms) == 1
    assert resource.atoms[0].atom_id == "atom_traj_updated_0001"
    assert resource.atoms[0].ux_score == 3


def test_manual_root_ready_when_atoms_present(tmp_path):
    root = tmp_path / "manual"
    root.mkdir()
    traj = root / "traj_manual.md"
    traj.write_text("## User\n\nmanual\n", encoding="utf-8")
    store = AtomTaskStore(root=root)
    _write_atom(
        store,
        traj_id="traj_manual",
        atom_id="atom_traj_manual_0001",
        ux_score=9,
    )
    resource = TrajectoryReader(tmp_path / "empty.db", root=root).list()[0]
    assert resource.status is None
    assert resource.atom_split_status == "ready"
    assert len(resource.atoms) == 1
    assert resource.atoms[0].ux_score == 9
