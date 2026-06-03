"""tests/test_registry.py -- registry CRUD + trajectory tracking"""

from __future__ import annotations

import os
import pickle
from pathlib import Path

import pytest

from xskill.pipeline.registry import (
    get_connection,
    register_dir,
    unregister_dir,
    list_watch_dirs,
    get_watch_dir,
    discover_trajectories,
    mark_meta_done,
    mark_indexed,
    mark_skill_used,
    get_unindexed,
    get_needs_meta,
    get_needs_embedding,
    all_index_paths,
    find_traj_file,
    update_traj_status,
    get_trajs_by_status,
)


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_registry.db"


@pytest.fixture()
def traj_dir(tmp_path):
    d = tmp_path / "dataset"
    d.mkdir()
    (d / "traj_0001.md").write_text("# traj 1\nagent did X")
    (d / "traj_0002.md").write_text("# traj 2\nagent did Y")
    (d / "traj_0003.md.meta").write_text("{}")  # should be skipped
    (d / "readme.md").write_text("not a traj")  # should be skipped
    return d


# ---- Watch dir CRUD ----

class TestWatchDirCRUD:
    def test_register_and_list(self, tmp_path, db_path):
        d = tmp_path / "data"
        d.mkdir()
        wid = register_dir(d, label="test", db_path=db_path)
        assert wid >= 1

        dirs = list_watch_dirs(db_path=db_path)
        assert len(dirs) == 1
        assert dirs[0]["path"] == str(d.resolve())
        assert dirs[0]["label"] == "test"
        assert dirs[0]["traj_count"] == 0

    def test_register_default_ecosystem_is_manual(self, tmp_path, db_path):
        d = tmp_path / "data"
        d.mkdir()
        register_dir(d, label="cli", db_path=db_path)
        rows = list_watch_dirs(db_path=db_path)
        assert rows[0]["ecosystem"] == "manual"

    def test_register_with_ecosystem(self, tmp_path, db_path):
        d = tmp_path / "cc_sessions"
        d.mkdir()
        register_dir(d, label="cc", ecosystem="claude_code", db_path=db_path)
        rows = list_watch_dirs(db_path=db_path)
        assert rows[0]["ecosystem"] == "claude_code"

    def test_register_updates_ecosystem_on_reregister(self, tmp_path, db_path):
        d = tmp_path / "data"
        d.mkdir()
        register_dir(d, db_path=db_path)
        assert list_watch_dirs(db_path=db_path)[0]["ecosystem"] == "manual"
        register_dir(d, ecosystem="claude_code", db_path=db_path)
        assert list_watch_dirs(db_path=db_path)[0]["ecosystem"] == "claude_code"

    def test_register_idempotent(self, tmp_path, db_path):
        d = tmp_path / "data"
        d.mkdir()
        id1 = register_dir(d, label="v1", db_path=db_path)
        id2 = register_dir(d, label="v2", db_path=db_path)
        assert id1 == id2
        dirs = list_watch_dirs(db_path=db_path)
        assert len(dirs) == 1
        assert dirs[0]["label"] == "v2"  # updated

    def test_unregister(self, tmp_path, db_path):
        d = tmp_path / "data"
        d.mkdir()
        register_dir(d, db_path=db_path)
        assert unregister_dir(d, db_path=db_path) is True
        assert list_watch_dirs(db_path=db_path) == []

    def test_unregister_not_found(self, tmp_path, db_path):
        assert unregister_dir(tmp_path / "nope", db_path=db_path) is False

    def test_get_watch_dir(self, tmp_path, db_path):
        d = tmp_path / "data"
        d.mkdir()
        register_dir(d, label="mine", db_path=db_path)
        wd = get_watch_dir(d, db_path=db_path)
        assert wd is not None
        assert wd["label"] == "mine"
        assert get_watch_dir(tmp_path / "nope", db_path=db_path) is None

    def test_unregister_cascades_trajectories(self, traj_dir, db_path):
        wid = register_dir(traj_dir, db_path=db_path)
        discover_trajectories(wid, traj_dir, db_path=db_path)
        assert len(get_unindexed(wid, db_path=db_path)) > 0
        unregister_dir(traj_dir, db_path=db_path)
        # After unregister, re-registering should have 0 trajectories
        wid2 = register_dir(traj_dir, db_path=db_path)
        assert get_unindexed(wid2, db_path=db_path) == []


# ---- Trajectory tracking ----

class TestTrajectoryTracking:
    def test_discover(self, traj_dir, db_path):
        wid = register_dir(traj_dir, db_path=db_path)
        new = discover_trajectories(wid, traj_dir, db_path=db_path)
        assert sorted(new) == ["traj_0001.md", "traj_0002.md"]

    def test_discover_idempotent(self, traj_dir, db_path):
        wid = register_dir(traj_dir, db_path=db_path)
        discover_trajectories(wid, traj_dir, db_path=db_path)
        new2 = discover_trajectories(wid, traj_dir, db_path=db_path)
        assert new2 == []  # no new files

    def test_discover_detects_new_files(self, traj_dir, db_path):
        wid = register_dir(traj_dir, db_path=db_path)
        discover_trajectories(wid, traj_dir, db_path=db_path)
        (traj_dir / "traj_0003.md").write_text("# new")
        new = discover_trajectories(wid, traj_dir, db_path=db_path)
        assert new == ["traj_0003.md"]


class TestContinuationDetection:
    """续写重拆触发：已落定的 traj 文件 mtime 增大 → 翻 ``updated``，
    不再当作新文件，等待 watcher 重新 split。"""

    def _bump_mtime(self, p: Path):
        st = p.stat()
        os.utime(p, (st.st_atime + 100, st.st_mtime + 100))

    def test_appended_done_traj_flips_to_updated(self, traj_dir, db_path):
        wid = register_dir(traj_dir, db_path=db_path)
        discover_trajectories(wid, traj_dir, db_path=db_path)
        # 模拟已处理完成
        update_traj_status(wid, "traj_0001.md", "done", db_path=db_path)
        # 客户端续写后重传：内容变 + mtime 增大
        f = traj_dir / "traj_0001.md"
        f.write_text("# traj 1\nagent did X\n## User\nmore\n")
        self._bump_mtime(f)
        new = discover_trajectories(wid, traj_dir, db_path=db_path)
        # 不是"新文件"
        assert "traj_0001.md" not in new
        # 翻成 updated 等重拆
        assert "traj_0001.md" in get_trajs_by_status(wid, "updated", db_path=db_path)
        assert "traj_0001.md" not in get_trajs_by_status(wid, "done", db_path=db_path)

    def test_unchanged_done_traj_stays_done(self, traj_dir, db_path):
        wid = register_dir(traj_dir, db_path=db_path)
        discover_trajectories(wid, traj_dir, db_path=db_path)
        update_traj_status(wid, "traj_0001.md", "done", db_path=db_path)
        # 不动文件，再扫一遍 → 仍 done，不翻 updated
        discover_trajectories(wid, traj_dir, db_path=db_path)
        assert "traj_0001.md" in get_trajs_by_status(wid, "done", db_path=db_path)
        assert get_trajs_by_status(wid, "updated", db_path=db_path) == []

    def test_active_status_not_disturbed_by_mtime_change(self, traj_dir, db_path):
        """in-flight（splitting/clustering）期间 mtime 变更不翻 updated（避免打架）。"""
        wid = register_dir(traj_dir, db_path=db_path)
        discover_trajectories(wid, traj_dir, db_path=db_path)
        update_traj_status(wid, "traj_0001.md", "splitting", db_path=db_path)
        f = traj_dir / "traj_0001.md"
        f.write_text("# traj 1\nchanged mid-flight\n")
        self._bump_mtime(f)
        discover_trajectories(wid, traj_dir, db_path=db_path)
        # 仍在 splitting，未被翻 updated
        assert "traj_0001.md" in get_trajs_by_status(wid, "splitting", db_path=db_path)
        assert get_trajs_by_status(wid, "updated", db_path=db_path) == []

    def test_mark_meta_and_indexed(self, traj_dir, db_path):
        wid = register_dir(traj_dir, db_path=db_path)
        discover_trajectories(wid, traj_dir, db_path=db_path)

        assert sorted(get_needs_meta(wid, db_path=db_path)) == ["traj_0001.md", "traj_0002.md"]

        mark_meta_done(wid, "traj_0001.md", db_path=db_path)
        assert get_needs_meta(wid, db_path=db_path) == ["traj_0002.md"]
        assert get_needs_embedding(wid, db_path=db_path) == ["traj_0001.md"]

        mark_indexed(wid, "traj_0001.md", db_path=db_path)
        assert get_needs_embedding(wid, db_path=db_path) == []
        assert "traj_0001.md" not in get_unindexed(wid, db_path=db_path)

    def test_mark_skill_used(self, traj_dir, db_path):
        wid = register_dir(traj_dir, db_path=db_path)
        discover_trajectories(wid, traj_dir, db_path=db_path)
        mark_skill_used(wid, "traj_0001.md", "fix_django", "staging", db_path=db_path)

        conn = get_connection(db_path)
        row = conn.execute(
            "SELECT skill_used, canary_side FROM trajectories WHERE filename='traj_0001.md'"
        ).fetchone()
        conn.close()
        assert row["skill_used"] == "fix_django"
        assert row["canary_side"] == "staging"


# ---- Cross-dataset search support ----

class TestAllIndexPaths:
    def test_returns_dirs_with_index_pkl(self, tmp_path, db_path):
        d1 = tmp_path / "ds1"
        d1.mkdir()
        (d1 / "index.pkl").write_bytes(b"fake")

        d2 = tmp_path / "ds2"
        d2.mkdir()
        # no index.pkl

        register_dir(d1, db_path=db_path)
        register_dir(d2, db_path=db_path)

        paths = all_index_paths(db_path=db_path)
        assert len(paths) == 1
        assert paths[0] == d1.resolve()

    def test_empty_when_no_dirs(self, db_path):
        assert all_index_paths(db_path=db_path) == []


# ---- Stats in list ----

class TestListStats:
    def test_traj_count_and_indexed_count(self, traj_dir, db_path):
        wid = register_dir(traj_dir, db_path=db_path)
        discover_trajectories(wid, traj_dir, db_path=db_path)
        mark_meta_done(wid, "traj_0001.md", db_path=db_path)
        mark_indexed(wid, "traj_0001.md", db_path=db_path)

        dirs = list_watch_dirs(db_path=db_path)
        assert dirs[0]["traj_count"] == 2
        assert dirs[0]["indexed_count"] == 1


# ---- find_traj_file: replaces legacy `skill_dir.parent.parent/"data"` ----

class TestFindTrajFile:
    def test_returns_none_when_no_dirs_registered(self, db_path, caplog):
        caplog.set_level("WARNING")
        result = find_traj_file("traj_0001", ".md", db_path=db_path)
        assert result is None
        assert any("no watch dirs registered" in r.message for r in caplog.records)

    def test_finds_md_flat_in_registered_dir(self, traj_dir, db_path):
        register_dir(traj_dir, db_path=db_path)
        hit = find_traj_file("traj_0001", ".md", db_path=db_path)
        assert hit is not None
        assert hit.name == "traj_0001.md"
        assert hit.parent == traj_dir.resolve()

    def test_finds_json_suffix(self, tmp_path, db_path):
        d = tmp_path / "ds"
        d.mkdir()
        (d / "traj_0042.json").write_text('{"raw_metadata": {"instance_id": "x"}}')
        register_dir(d, db_path=db_path)
        hit = find_traj_file("traj_0042", ".json", db_path=db_path)
        assert hit is not None and hit.name == "traj_0042.json"

    def test_recursive_fallback_finds_nested_md(self, tmp_path, db_path):
        d = tmp_path / "ds"
        nested = d / "subdir" / "deeper"
        nested.mkdir(parents=True)
        (nested / "traj_0099.md").write_text("# nested")
        register_dir(d, db_path=db_path)
        hit = find_traj_file("traj_0099", ".md", db_path=db_path)
        assert hit is not None and hit.name == "traj_0099.md"

    def test_searches_all_registered_dirs(self, tmp_path, db_path):
        a = tmp_path / "a"
        b = tmp_path / "b"
        a.mkdir()
        b.mkdir()
        (b / "traj_0007.md").write_text("# in b")
        register_dir(a, db_path=db_path)
        register_dir(b, db_path=db_path)
        hit = find_traj_file("traj_0007", ".md", db_path=db_path)
        assert hit is not None and hit.parent.resolve() == b.resolve()

    def test_warns_when_not_found(self, traj_dir, db_path, caplog):
        register_dir(traj_dir, db_path=db_path)
        caplog.set_level("WARNING")
        result = find_traj_file("traj_9999", ".md", db_path=db_path)
        assert result is None
        msgs = [r.message for r in caplog.records]
        assert any("not found in any registered watch dir" in m for m in msgs)
