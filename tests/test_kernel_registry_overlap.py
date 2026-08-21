"""Issue #236: registered watch dirs are flat; Kernel must not rglob through children."""

from __future__ import annotations

from pathlib import Path

from xskill.kernels.context import TrajectoryReader
from xskill.pipeline.registry import register_dir


def _write_traj(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"## User\n\n{body}\n", encoding="utf-8")
    return path


def test_registered_parent_does_not_see_nested_sessions(tmp_path):
    registry_db = tmp_path / "registry.db"
    parent = tmp_path / "team_trajectories"
    nested = parent / "clients" / "user-a" / "sessions" / "traj_example.md"
    _write_traj(parent / "traj_parent.md", "parent only")
    _write_traj(nested, "nested session")

    register_dir(parent, label="all-trajectories", db_path=registry_db)
    resources = list(TrajectoryReader(registry_db).iter())

    assert [item.trajectory_id for item in resources] == ["traj_parent"]
    assert all(item.path.parent == parent.resolve() for item in resources)


def test_registered_sessions_dir_sees_its_direct_files(tmp_path):
    registry_db = tmp_path / "registry.db"
    sessions = tmp_path / "team_trajectories" / "clients" / "user-a" / "sessions"
    _write_traj(sessions / "traj_example.md", "session")

    child_id = register_dir(sessions, label="user-a", ecosystem="team_client", db_path=registry_db)
    resources = list(TrajectoryReader(registry_db).iter())

    assert len(resources) == 1
    assert resources[0].id == f"{child_id}:traj_example.md"
    assert resources[0].label == "user-a"
    assert resources[0].ecosystem == "team_client"


def test_parent_and_child_expose_same_physical_file_once(tmp_path):
    registry_db = tmp_path / "registry.db"
    parent = tmp_path / "team_trajectories"
    sessions = parent / "clients" / "user-a" / "sessions"
    parent_file = _write_traj(parent / "traj_parent.md", "parent")
    nested_file = _write_traj(sessions / "traj_example.md", "nested")

    register_dir(parent, label="all-trajectories", db_path=registry_db)
    child_id = register_dir(
        sessions, label="user-a", ecosystem="team_client", db_path=registry_db,
    )

    resources = list(TrajectoryReader(registry_db).iter())
    paths = [item.path.resolve() for item in resources]
    ids = [item.id for item in resources]

    assert parent_file.resolve() in paths
    assert nested_file.resolve() in paths
    assert len(paths) == 2
    assert len(set(paths)) == 2
    assert f"{child_id}:traj_example.md" in ids
    assert not any("clients/user-a/sessions/traj_example.md" in item_id for item_id in ids)


def test_distinct_directories_keep_same_traj_name(tmp_path):
    registry_db = tmp_path / "registry.db"
    dir_a = tmp_path / "client_a" / "sessions"
    dir_b = tmp_path / "client_b" / "sessions"
    file_a = _write_traj(dir_a / "traj_0001.md", "user a")
    file_b = _write_traj(dir_b / "traj_0001.md", "user b")

    register_dir(dir_a, label="user_a", db_path=registry_db)
    register_dir(dir_b, label="user_b", db_path=registry_db)

    resources = list(TrajectoryReader(registry_db).iter())
    paths = {item.path.resolve() for item in resources}

    assert paths == {file_a.resolve(), file_b.resolve()}
    assert [item.trajectory_id for item in resources] == ["traj_0001", "traj_0001"]


def test_symlink_watch_dir_alias_dedups_to_one_resource(tmp_path):
    registry_db = tmp_path / "registry.db"
    real_dir = tmp_path / "real_store"
    link_dir = tmp_path / "link_store"
    _write_traj(real_dir / "traj_0001.md", "real")
    link_dir.symlink_to(real_dir)

    register_dir(real_dir, label="real", db_path=registry_db)
    register_dir(link_dir, label="alias", db_path=registry_db)

    resources = list(TrajectoryReader(registry_db).iter())

    assert len(resources) == 1
    assert resources[0].path.resolve() == (real_dir / "traj_0001.md").resolve()
