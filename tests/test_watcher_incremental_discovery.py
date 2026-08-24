from __future__ import annotations

import os
from pathlib import Path

from tests.pool_helpers import pool_config
from xskill.pipeline.registry import (
    get_trajs_by_status,
    list_watch_dirs,
    list_watcher_dirs,
    register_dir,
    update_traj_status,
)
from xskill.pipeline.runner import DirectoryWatcher


def _watcher(tmp_path: Path, db_path: Path, *, reconcile_interval: float):
    return DirectoryWatcher(
        config={
            "watcher": {"full_reconcile_interval": reconcile_interval},
        },
        poll_interval=5,
        pool_config=pool_config(workers=1),
        db_path=db_path,
        home_root=tmp_path,
        xskill_home=tmp_path,
    )


def test_watcher_dirs_omit_dashboard_history_counts(tmp_path):
    db_path = tmp_path / "registry.db"
    active = tmp_path / "active"
    paused = tmp_path / "paused"
    active.mkdir()
    paused.mkdir()
    register_dir(active, auto_index=True, db_path=db_path)
    register_dir(paused, auto_index=False, db_path=db_path)

    rows = list_watcher_dirs(db_path=db_path)

    assert [row["path"] for row in rows] == [
        str(active.resolve()),
        str(paused.resolve()),
    ]
    assert "traj_count" not in rows[0]
    assert "indexed_count" not in rows[0]
    assert "traj_count" in list_watch_dirs(db_path=db_path)[0]


def test_idle_poll_does_not_rescan_registry_or_trajectory_files(
    tmp_path,
    monkeypatch,
):
    db_path = tmp_path / "registry.db"
    watch_dir = tmp_path / "sessions"
    watch_dir.mkdir()
    for index in range(40):
        (watch_dir / f"traj_{index:04d}.md").write_text(
            "# trajectory\n",
            encoding="utf-8",
        )
    watch_dir_id = register_dir(watch_dir, db_path=db_path)
    watcher = _watcher(tmp_path, db_path, reconcile_interval=60)
    clock = [100.0]
    monkeypatch.setattr("xskill.pipeline.runner.time.monotonic", lambda: clock[0])
    original_stat = Path.stat
    trajectory_stats = 0

    def counting_stat(path, *args, **kwargs):
        nonlocal trajectory_stats
        if path.parent == watch_dir and path.name.startswith("traj_"):
            trajectory_stats += 1
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", counting_stat)
    try:
        assert len(watcher._discover_if_needed(watch_dir_id, watch_dir)) == 40
        assert trajectory_stats == 40

        clock[0] += 5
        assert watcher._discover_if_needed(watch_dir_id, watch_dir) == []
        assert trajectory_stats == 40
    finally:
        watcher.stop()


def test_directory_change_is_discovered_on_next_poll(tmp_path, monkeypatch):
    db_path = tmp_path / "registry.db"
    watch_dir = tmp_path / "sessions"
    watch_dir.mkdir()
    watch_dir_id = register_dir(watch_dir, db_path=db_path)
    watcher = _watcher(tmp_path, db_path, reconcile_interval=60)
    clock = [100.0]
    monkeypatch.setattr("xskill.pipeline.runner.time.monotonic", lambda: clock[0])
    try:
        assert watcher._discover_if_needed(watch_dir_id, watch_dir) == []
        before = watch_dir.stat()
        (watch_dir / "traj_new.md").write_text("# new\n", encoding="utf-8")
        os.utime(
            watch_dir,
            ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000),
        )

        clock[0] += 5
        assert watcher._discover_if_needed(watch_dir_id, watch_dir) == [
            "traj_new.md",
        ]
    finally:
        watcher.stop()


def test_periodic_reconcile_catches_in_place_rewrite(tmp_path, monkeypatch):
    db_path = tmp_path / "registry.db"
    watch_dir = tmp_path / "sessions"
    watch_dir.mkdir()
    trajectory = watch_dir / "traj_existing.md"
    trajectory.write_text("# original\n", encoding="utf-8")
    watch_dir_id = register_dir(watch_dir, db_path=db_path)
    watcher = _watcher(tmp_path, db_path, reconcile_interval=60)
    clock = [100.0]
    monkeypatch.setattr("xskill.pipeline.runner.time.monotonic", lambda: clock[0])
    try:
        assert watcher._discover_if_needed(watch_dir_id, watch_dir) == [
            trajectory.name,
        ]
        update_traj_status(
            watch_dir_id,
            trajectory.name,
            "done",
            db_path=db_path,
        )
        directory_stat = watch_dir.stat()
        trajectory_stat = trajectory.stat()
        trajectory.write_text("# rewritten in place\n", encoding="utf-8")
        os.utime(
            trajectory,
            ns=(
                trajectory_stat.st_atime_ns,
                trajectory_stat.st_mtime_ns + 1_000_000,
            ),
        )
        # Model a filesystem where overwriting an existing entry does not
        # update the directory mtime.
        os.utime(
            watch_dir,
            ns=(directory_stat.st_atime_ns, directory_stat.st_mtime_ns),
        )

        clock[0] += 5
        assert watcher._discover_if_needed(watch_dir_id, watch_dir) == []
        assert get_trajs_by_status(
            watch_dir_id,
            "done",
            db_path=db_path,
        ) == [trajectory.name]

        clock[0] += 55
        assert watcher._discover_if_needed(watch_dir_id, watch_dir) == []
        assert get_trajs_by_status(
            watch_dir_id,
            "updated",
            db_path=db_path,
        ) == [trajectory.name]
    finally:
        watcher.stop()
