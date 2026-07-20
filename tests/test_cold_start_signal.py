"""冷启动 COLD_START 信号回归测试（#87 快照终态语义）。

验证：
  1. rebuild 写入的 ``COLD_START`` 携带本批轨迹 id 快照。
  2. hold 只看快照内轨迹是否到终态——rebuild 之后新进的轨迹不延长等待。
  3. ≤0.6.11 的空 touch 信号文件被现场补录成快照（存量 rebuild 兼容）。
  4. 超过最长持有时限强制 flush（快照内个别轨迹卡死的安全网）。
"""
from __future__ import annotations

import json
import threading
import time

from xskill.pipeline.atom import AtomTaskStore
from xskill.pipeline.cold_start import (
    COLD_START_FILENAME,
    COLD_START_MAX_HOLD_SECONDS,
    ColdStartSignal,
)
from xskill.pipeline.registry import get_connection, register_dir
from xskill.pipeline.runner import DirectoryWatcher
from xskill.skill import candidates
from xskill.skill.git import init_skill_repo_on_baby, current_branch
from tests.test_atom_task_store import _FakeEmbed
from tests.test_task_agent import _AutoSplitLLM
from tests.test_skilledit_parallel import _make_barrier_agno


def _seed_baby_skill(skill_root, skill_name, weights):
    skill_directory = skill_root / skill_name
    init_skill_repo_on_baby(
        str(skill_directory), name=skill_name, description="stub",
    )
    candidate_data = {"candidates": []}
    for atom_index, weightscore in enumerate(weights, start=1):
        candidate_data, _ = candidates.add_atom_contribution(
            candidate_data, f"atom_{skill_name}_{atom_index:04d}", weightscore,
        )
    candidates.save_candidates(skill_directory, candidate_data)
    return skill_directory


def _make_watcher(tmp_path, skill_root):
    database_path = tmp_path / "test.db"
    watch_dir_path = tmp_path / "watch-dir"
    watch_dir_path.mkdir(exist_ok=True)
    register_dir(watch_dir_path, db_path=database_path)
    atom_store = AtomTaskStore(root=watch_dir_path)
    config = {"llm": {"base_url": "x", "model": "y", "api_key": "z"}}
    return DirectoryWatcher(
        llm=_AutoSplitLLM(),
        embed_client=_FakeEmbed(),
        config=config,
        skill_dir=skill_root,
        poll_interval=0.0,
        max_concurrent=4,
        db_path=database_path,
        store=atom_store,
        agno_agent_factory=_make_barrier_agno(1, threading.Barrier(1)),
        home_root=tmp_path,
        xskill_home=tmp_path,
    )


def _insert_trajectory(database_path, filename, status):
    connection = get_connection(database_path)
    try:
        cursor = connection.execute(
            "INSERT INTO trajectories (watch_dir_id, filename, status) "
            "VALUES (1, ?, ?)",
            (filename, status),
        )
        connection.commit()
        return cursor.lastrowid
    finally:
        connection.close()


class TestColdStartSignal:
    def test_signal_path_is_fixed_under_home(self, tmp_path):
        cold_start_signal = ColdStartSignal(tmp_path)

        assert cold_start_signal.file_path == tmp_path / COLD_START_FILENAME
        assert cold_start_signal.exists is False

    def test_create_writes_snapshot_payload(self, tmp_path):
        cold_start_signal = ColdStartSignal(tmp_path)
        payload = cold_start_signal.create([11, 22, 33])

        assert cold_start_signal.exists is True
        assert payload["trajectory_ids"] == [11, 22, 33]
        on_disk = cold_start_signal.snapshot()
        assert on_disk is not None
        assert on_disk["trajectory_ids"] == [11, 22, 33]
        assert on_disk["created_at"] > 0

        cold_start_signal.consume()
        assert cold_start_signal.exists is False

    def test_legacy_empty_touch_file_has_no_snapshot(self, tmp_path):
        (tmp_path / COLD_START_FILENAME).touch()

        assert ColdStartSignal(tmp_path).snapshot() is None

    def test_watcher_default_uses_xskill_home_not_ecosystem_home(
        self, tmp_path, monkeypatch,
    ):
        xskill_home = tmp_path / "xskill-home"
        ecosystem_home = tmp_path / "user-home"
        monkeypatch.setattr("xskill.config.XSKILL_HOME", xskill_home)

        watcher = DirectoryWatcher(home_root=ecosystem_home)
        try:
            assert watcher.home_root == ecosystem_home
            assert watcher._cold_start_signal.file_path == (
                xskill_home / COLD_START_FILENAME
            )
        finally:
            watcher.stop()


class TestColdStartFlush:
    def test_jam_reproduced_without_cold_start(self, tmp_path):
        skill_root = tmp_path / "skill"
        skill_root.mkdir()
        skill_directory = _seed_baby_skill(skill_root, "sk-jam", [3])
        watcher = _make_watcher(tmp_path, skill_root)

        watcher._run_skill_edit_step()
        watcher._drain_futures(stage="skill_edit")

        assert current_branch(str(skill_directory)) == "baby"
        assert candidates.load_candidates(skill_directory)["candidates"]

    def test_holds_while_snapshot_trajectory_pending(self, tmp_path):
        skill_root = tmp_path / "skill"
        skill_root.mkdir()
        skill_directory = _seed_baby_skill(skill_root, "sk-cold", [4, 4, 4])
        watcher = _make_watcher(tmp_path, skill_root)
        snapshot_trajectory_id = _insert_trajectory(
            tmp_path / "test.db", "traj_snap.md", "splitting",
        )
        ColdStartSignal(tmp_path).create([snapshot_trajectory_id])

        watcher._run_skill_edit_step()
        watcher._drain_futures(stage="skill_edit")

        assert current_branch(str(skill_directory)) == "baby"
        assert candidates.load_candidates(skill_directory)["candidates"]
        assert (tmp_path / COLD_START_FILENAME).exists()

    def test_flushes_when_snapshot_settled_despite_new_traffic(self, tmp_path):
        skill_root = tmp_path / "skill"
        skill_root.mkdir()
        skill_directory = _seed_baby_skill(skill_root, "sk-cold", [4, 4, 4])
        watcher = _make_watcher(tmp_path, skill_root)
        database_path = tmp_path / "test.db"
        snapshot_trajectory_id = _insert_trajectory(
            database_path, "traj_snap.md", "done",
        )
        # 活跃团队语义：快照之外一直有新轨迹在 pending，不得延长等待。
        _insert_trajectory(database_path, "traj_new_arrival.md", "discovered")
        ColdStartSignal(tmp_path).create([snapshot_trajectory_id])

        watcher._run_skill_edit_step()
        watcher._drain_futures(stage="skill_edit")

        assert current_branch(str(skill_directory)) == "main"
        assert candidates.load_candidates(skill_directory)["candidates"] == []
        assert not (tmp_path / COLD_START_FILENAME).exists()

    def test_legacy_signal_adopts_snapshot_then_converges(self, tmp_path):
        skill_root = tmp_path / "skill"
        skill_root.mkdir()
        skill_directory = _seed_baby_skill(skill_root, "sk-cold", [4, 4, 4])
        watcher = _make_watcher(tmp_path, skill_root)
        database_path = tmp_path / "test.db"
        legacy_trajectory_id = _insert_trajectory(
            database_path, "traj_legacy.md", "indexed",
        )
        (tmp_path / COLD_START_FILENAME).touch()

        watcher._run_skill_edit_step()
        watcher._drain_futures(stage="skill_edit")
        adopted = ColdStartSignal(tmp_path).snapshot()
        assert adopted is not None, "空信号文件应被补录成快照"
        assert adopted["trajectory_ids"] == [legacy_trajectory_id]
        assert current_branch(str(skill_directory)) == "baby"

        connection = get_connection(database_path)
        connection.execute(
            "UPDATE trajectories SET status='done' WHERE id=?",
            (legacy_trajectory_id,),
        )
        connection.commit()
        connection.close()
        watcher._run_skill_edit_step()
        watcher._drain_futures(stage="skill_edit")

        assert current_branch(str(skill_directory)) == "main"
        assert not (tmp_path / COLD_START_FILENAME).exists()

    def test_error_status_counts_as_terminal(self, tmp_path):
        skill_root = tmp_path / "skill"
        skill_root.mkdir()
        skill_directory = _seed_baby_skill(skill_root, "sk-cold", [4, 4, 4])
        watcher = _make_watcher(tmp_path, skill_root)
        failed_trajectory_id = _insert_trajectory(
            tmp_path / "test.db", "traj_failed.md", "error",
        )
        ColdStartSignal(tmp_path).create([failed_trajectory_id])

        watcher._run_skill_edit_step()
        watcher._drain_futures(stage="skill_edit")

        assert current_branch(str(skill_directory)) == "main"
        assert not (tmp_path / COLD_START_FILENAME).exists()

    def test_max_hold_deadline_forces_flush(self, tmp_path):
        skill_root = tmp_path / "skill"
        skill_root.mkdir()
        skill_directory = _seed_baby_skill(skill_root, "sk-cold", [4, 4, 4])
        watcher = _make_watcher(tmp_path, skill_root)
        stuck_trajectory_id = _insert_trajectory(
            tmp_path / "test.db", "traj_stuck.md", "splitting",
        )
        cold_start_signal = ColdStartSignal(tmp_path)
        payload = cold_start_signal.create([stuck_trajectory_id])
        payload["created_at"] = time.time() - COLD_START_MAX_HOLD_SECONDS - 60
        cold_start_signal.file_path.write_text(
            json.dumps(payload), encoding="utf-8",
        )

        watcher._run_skill_edit_step()
        watcher._drain_futures(stage="skill_edit")

        assert current_branch(str(skill_directory)) == "main"
        assert not (tmp_path / COLD_START_FILENAME).exists()
