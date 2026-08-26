"""用户画像脏队列的合并、CAS 清理与低频对账。"""
from __future__ import annotations

import pytest

from xskill.pipeline.registry import get_connection
from xskill.pipeline.registry import discover_trajectories, register_dir
from xskill.pipeline.atom import AtomTask, AtomTaskStore
from xskill.pipeline.runner import DirectoryWatcher
from xskill.recommend.profile_dirty import (
    clear_profile_dirty,
    list_dirty_profiles,
    mark_profile_dirty,
    profile_user_key_for_store_root,
    reconcile_profile_dirty,
)


@pytest.fixture
def registry_db(tmp_path):
    path = tmp_path / "registry.db"
    connection = get_connection(path)
    connection.close()
    return path


def test_team_store_root_maps_to_profile_user(tmp_path):
    root = tmp_path / "trajectories" / "clients" / "alice" / "sessions"
    assert profile_user_key_for_store_root(root) == "alice"
    assert profile_user_key_for_store_root(tmp_path / "local") == ""


def test_split_completion_marks_only_owning_team_user_dirty(tmp_path, registry_db):
    sessions = tmp_path / "trajectories" / "clients" / "alice" / "sessions"
    sessions.mkdir(parents=True)
    trajectory = sessions / "traj_team.md"
    trajectory.write_text("# trajectory", encoding="utf-8")
    watch_dir_id = register_dir(
        sessions, label="alice", ecosystem="team_client", db_path=registry_db,
    )
    discover_trajectories(watch_dir_id, sessions, db_path=registry_db)
    store = AtomTaskStore(sessions)
    atom = AtomTask(
        atom_id="atom_traj_team_0001",
        traj_id="traj_team",
        offset_start=1,
        offset_end=2,
        intent="intent",
        summary="summary",
    )
    store.save(atom)
    watcher = object.__new__(DirectoryWatcher)
    watcher.store = store
    watcher._stats = {"atoms_extracted": 0}

    watcher._on_split_done(
        watch_dir_id,
        trajectory.name,
        (trajectory.name, 1, 2, atom.atom_id, None),
        db_path=registry_db,
    )

    dirty = list_dirty_profiles(db_path=registry_db)
    assert [(row["user_key"], row["reason"]) for row in dirty] == [
        ("alice", "atom_split"),
    ]


def test_mark_profile_dirty_avoids_returning_clause():
    import inspect
    from xskill.recommend import profile_dirty
    source = inspect.getsource(profile_dirty.mark_profile_dirty_on_connection)
    assert "RETURNING" not in source


def test_multiple_changes_coalesce_and_late_change_survives_cas(registry_db):
    assert mark_profile_dirty("alice", reason="first", db_path=registry_db) == 1
    assert mark_profile_dirty("alice", reason="second", db_path=registry_db) == 2
    row = list_dirty_profiles(db_path=registry_db)[0]
    assert row["user_key"] == "alice"
    assert row["generation"] == 2
    assert row["reason"] == "second"

    assert mark_profile_dirty("alice", reason="late", db_path=registry_db) == 3
    assert clear_profile_dirty("alice", 2, db_path=registry_db) is False
    assert list_dirty_profiles(db_path=registry_db)[0]["generation"] == 3
    assert clear_profile_dirty("alice", 3, db_path=registry_db) is True
    assert list_dirty_profiles(db_path=registry_db) == []


def test_reconcile_only_marks_on_bootstrap_version_change_or_interval(registry_db):
    assert reconcile_profile_dirty(
        ["alice", "bob"], input_fingerprint="v1", db_path=registry_db,
        now=100, interval_seconds=1000,
    ) == "bootstrap"
    initial = list_dirty_profiles(db_path=registry_db)
    assert {row["user_key"] for row in initial} == {"alice", "bob"}
    for row in initial:
        assert clear_profile_dirty(
            row["user_key"], row["generation"], db_path=registry_db,
        )

    assert reconcile_profile_dirty(
        ["alice", "bob"], input_fingerprint="v1", db_path=registry_db,
        now=200, interval_seconds=1000,
    ) == ""
    assert list_dirty_profiles(db_path=registry_db) == []

    assert reconcile_profile_dirty(
        ["alice", "bob"], input_fingerprint="v2", db_path=registry_db,
        now=300, interval_seconds=1000,
    ) == "profile_input_changed"
    changed = list_dirty_profiles(db_path=registry_db)
    for row in changed:
        assert clear_profile_dirty(
            row["user_key"], row["generation"], db_path=registry_db,
        )

    assert reconcile_profile_dirty(
        ["alice", "bob"], input_fingerprint="v2", db_path=registry_db,
        now=1400, interval_seconds=1000,
    ) == "periodic_reconcile"
