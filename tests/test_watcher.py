"""tests/test_watcher.py -- DirectoryWatcher unit tests（v2: AtomTask 流水线后）

只保留与流水线 stage 解耦的通用 case：
- discovery（发现新 traj）
- stats（轮询计数）
- ux_score header 触发
- start/stop lifecycle

v2 AtomTask 流水线特有的测试（splitting/split_done/clustering 状态流转、
僵尸清理、cold-start gate、整链路打通）放在 ``tests/test_watcher_atom.py``。

历史注记：旧版本（meta_extracting / processing 状态）的 zombie + cold-start
+ process_traj mock 测试在 AtomTask 重构时移到 ``tests/test_watcher_atom.py``
的等价覆盖。
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from xskill.registry import register_dir, discover_trajectories, get_unindexed
from xskill.watcher import DirectoryWatcher


def _drain(watcher: DirectoryWatcher) -> None:
    """等所有提交到 watcher._pool 的 future 跑完，再触发一次 _harvest 把
    收割回调（更新 DB 状态、移除 future 记录）跑齐。供测试用。
    """
    for fut in list(watcher._futures):
        fut.result()
    watcher._harvest()


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test.db"


@pytest.fixture()
def traj_dir(tmp_path):
    d = tmp_path / "dataset"
    d.mkdir()
    (d / "traj_0001.md").write_text("# Traj 1\nagent did X then Y")
    (d / "traj_0001.json").write_text("{}")
    return d


@pytest.fixture()
def skill_dir(tmp_path):
    d = tmp_path / "skill"
    d.mkdir()
    return d


class TestWatcherDiscovery:
    """Test that _scan_once discovers files and updates DB."""

    def test_discover_new_files(self, traj_dir, skill_dir, db_path, tmp_path):
        wid = register_dir(traj_dir, db_path=db_path)

        watcher = DirectoryWatcher(
            llm=None, embed_client=None, config={},
            skill_dir=skill_dir, poll_interval=1, db_path=db_path,
            home_root=tmp_path,
        )
        watcher._scan_once()
        _drain(watcher)

        # File should be discovered (no LLM/embed means no meta extraction).
        from xskill.registry import get_connection
        conn = get_connection(db_path)
        rows = conn.execute("SELECT filename FROM trajectories WHERE watch_dir_id=?", (wid,)).fetchall()
        conn.close()
        assert len(rows) == 1
        assert rows[0]["filename"] == "traj_0001.md"

    def test_stats_updated(self, traj_dir, skill_dir, db_path, tmp_path):
        register_dir(traj_dir, db_path=db_path)

        watcher = DirectoryWatcher(
            llm=None, embed_client=None, config={},
            skill_dir=skill_dir, poll_interval=1, db_path=db_path,
            home_root=tmp_path,
        )
        watcher._scan_once()

        assert watcher.stats["polls"] == 1
        assert watcher.stats["new_trajs"] == 1


class TestWatcherStartStop:
    """Test thread lifecycle."""

    def test_start_and_stop(self, tmp_path, db_path):
        watcher = DirectoryWatcher(
            llm=None, embed_client=None, config={},
            skill_dir=tmp_path, poll_interval=0.1, db_path=db_path,
            home_root=tmp_path,
        )
        assert not watcher.is_running
        watcher.start()
        assert watcher.is_running
        watcher.stop()
        assert not watcher.is_running

    def test_double_start_noop(self, tmp_path, db_path):
        watcher = DirectoryWatcher(
            llm=None, embed_client=None, config={},
            skill_dir=tmp_path, poll_interval=0.1, db_path=db_path,
            home_root=tmp_path,
        )
        watcher.start()
        watcher.start()  # should not error
        watcher.stop()
