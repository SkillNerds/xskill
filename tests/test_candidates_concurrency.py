"""Candidate-buffer atomicity, locking, and batch complexity regressions."""
from __future__ import annotations

import multiprocessing
import os
import stat
import threading
from pathlib import Path

import pytest
import yaml

from xskill.agents import agent_tools
from xskill.skill import candidates


def _process_add_candidates(
    skill_dir: str,
    prefix: str,
    count: int,
    barrier,
    error_queue,
) -> None:
    try:
        barrier.wait(timeout=10)
        candidates.add_atom_contributions(
            Path(skill_dir),
            [
                (f"{prefix}-{index}", 1, "")
                for index in range(count)
            ],
        )
        error_queue.put(None)
    except BaseException as error:  # noqa: BLE001 - child error crosses process
        error_queue.put(f"{type(error).__name__}: {error}")


def _process_remove_candidates(
    skill_dir: str,
    atom_ids: set[str],
    barrier,
    error_queue,
) -> None:
    try:
        barrier.wait(timeout=10)
        candidates.remove_candidates(Path(skill_dir), atom_ids)
        error_queue.put(None)
    except BaseException as error:  # noqa: BLE001 - child error crosses process
        error_queue.put(f"{type(error).__name__}: {error}")


def _candidate_ids(skill_dir: Path) -> set[str]:
    return {
        str(candidate["atom_id"])
        for candidate in candidates.load_candidates(
            skill_dir,
        )["candidates"]
    }


def test_thread_barrier_add_and_remove_preserve_every_update(tmp_path):
    skill_dir = tmp_path / "thread-skill"
    skill_dir.mkdir()
    candidates.add_atom_contributions(
        skill_dir,
        [
            (f"remove-{index}", 1, "")
            for index in range(20)
        ],
    )

    barrier = threading.Barrier(5)
    errors: list[BaseException] = []

    def add_batch(prefix: str) -> None:
        try:
            barrier.wait(timeout=5)
            candidates.add_atom_contributions(
                skill_dir,
                [
                    (f"{prefix}-{index}", 2, "")
                    for index in range(25)
                ],
            )
        except BaseException as error:  # noqa: BLE001 - thread error handoff
            errors.append(error)

    def remove_batch(start: int) -> None:
        try:
            barrier.wait(timeout=5)
            candidates.remove_candidates(
                skill_dir,
                {
                    f"remove-{index}"
                    for index in range(start, start + 10)
                },
            )
        except BaseException as error:  # noqa: BLE001 - thread error handoff
            errors.append(error)

    threads = [
        threading.Thread(target=add_batch, args=("left",), daemon=True),
        threading.Thread(target=add_batch, args=("right",), daemon=True),
        threading.Thread(target=remove_batch, args=(0,), daemon=True),
        threading.Thread(target=remove_batch, args=(10,), daemon=True),
    ]
    for thread in threads:
        thread.start()
    barrier.wait(timeout=5)
    for thread in threads:
        thread.join(timeout=10)

    assert all(not thread.is_alive() for thread in threads)
    assert errors == []
    assert _candidate_ids(skill_dir) == {
        *(f"left-{index}" for index in range(25)),
        *(f"right-{index}" for index in range(25)),
    }


def test_process_barrier_add_and_remove_preserve_every_update(tmp_path):
    skill_dir = tmp_path / "process-skill"
    skill_dir.mkdir()
    candidates.add_atom_contributions(
        skill_dir,
        [
            (f"remove-{index}", 1, "")
            for index in range(20)
        ],
    )

    process_context = multiprocessing.get_context("spawn")
    barrier = process_context.Barrier(4)
    error_queue = process_context.Queue()
    processes = [
        process_context.Process(
            target=_process_add_candidates,
            args=(str(skill_dir), "left", 25, barrier, error_queue),
        ),
        process_context.Process(
            target=_process_add_candidates,
            args=(str(skill_dir), "right", 25, barrier, error_queue),
        ),
        process_context.Process(
            target=_process_remove_candidates,
            args=(
                str(skill_dir),
                {f"remove-{index}" for index in range(10)},
                barrier,
                error_queue,
            ),
        ),
        process_context.Process(
            target=_process_remove_candidates,
            args=(
                str(skill_dir),
                {f"remove-{index}" for index in range(10, 20)},
                barrier,
                error_queue,
            ),
        ),
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=20)

    stuck_processes = [
        process
        for process in processes
        if process.is_alive()
    ]
    for process in stuck_processes:
        process.terminate()
        process.join(timeout=5)
    assert stuck_processes == []
    assert [process.exitcode for process in processes] == [0, 0, 0, 0]
    assert [
        error_queue.get(timeout=5)
        for _process in processes
    ] == [None, None, None, None]
    assert _candidate_ids(skill_dir) == {
        *(f"left-{index}" for index in range(25)),
        *(f"right-{index}" for index in range(25)),
    }


def test_opposite_moves_use_one_canonical_lock_order(tmp_path):
    left_skill = tmp_path / "left"
    right_skill = tmp_path / "right"
    left_skill.mkdir()
    right_skill.mkdir()
    candidates.add_atom_contributions(
        left_skill,
        [("left-atom", 4, "")],
    )
    candidates.add_atom_contributions(
        right_skill,
        [("right-atom", 7, "")],
    )

    barrier = threading.Barrier(3)
    results: list[int | None] = []

    def move(
        source_skill: Path,
        target_skill: Path,
        atom_id: str,
    ) -> None:
        barrier.wait(timeout=5)
        results.append(
            candidates.move_atom_contribution(
                source_skill,
                target_skill,
                atom_id,
            ),
        )

    threads = [
        threading.Thread(
            target=move,
            args=(left_skill, right_skill, "left-atom"),
            daemon=True,
        ),
        threading.Thread(
            target=move,
            args=(right_skill, left_skill, "right-atom"),
            daemon=True,
        ),
    ]
    for thread in threads:
        thread.start()
    barrier.wait(timeout=5)
    for thread in threads:
        thread.join(timeout=10)

    assert all(not thread.is_alive() for thread in threads)
    assert sorted(results) == [4, 7]
    assert _candidate_ids(left_skill) == {"right-atom"}
    assert _candidate_ids(right_skill) == {"left-atom"}


def test_invalid_yaml_is_reported_without_overwriting_original(tmp_path):
    skill_dir = tmp_path / "invalid"
    skill_dir.mkdir()
    candidate_path = skill_dir / candidates.CANDIDATES_FILENAME
    original = "candidates:\n  - atom_id: [unterminated\n"
    candidate_path.write_text(original, encoding="utf-8")

    with pytest.raises(yaml.YAMLError):
        candidates.add_atom_contributions(
            skill_dir,
            [("new-atom", 3, "")],
        )

    assert candidate_path.read_text(encoding="utf-8") == original


def test_invalid_candidate_entry_is_reported_with_path_and_index(tmp_path):
    skill_dir = tmp_path / "invalid-entry"
    skill_dir.mkdir()
    candidate_path = skill_dir / candidates.CANDIDATES_FILENAME
    original = "candidates:\n  - 42\n"
    candidate_path.write_text(original, encoding="utf-8")

    with pytest.raises(
        ValueError,
        match=r"\.candidates\.yml index=0",
    ):
        candidates.add_atom_contributions(
            skill_dir,
            [("new-atom", 3, "")],
        )

    assert candidate_path.read_text(encoding="utf-8") == original


def test_replace_failure_keeps_original_and_removes_temp_file(
    tmp_path,
    monkeypatch,
):
    skill_dir = tmp_path / "replace-failure"
    skill_dir.mkdir()
    candidates.add_atom_contributions(
        skill_dir,
        [("original", 5, "")],
    )
    candidate_path = skill_dir / candidates.CANDIDATES_FILENAME
    original = candidate_path.read_bytes()

    def fail_replace(_source, _target):
        raise OSError("injected replace failure")

    monkeypatch.setattr(candidates.os, "replace", fail_replace)
    with pytest.raises(OSError, match="injected replace failure"):
        candidates.add_atom_contributions(
            skill_dir,
            [("new-atom", 3, "")],
        )

    assert candidate_path.read_bytes() == original
    assert list(skill_dir.glob("..candidates.yml.*.tmp")) == []


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory fsync contract")
def test_atomic_save_fsyncs_file_then_replacement_directory(
    tmp_path,
    monkeypatch,
):
    skill_dir = tmp_path / "durable-save"
    skill_dir.mkdir()
    events: list[str] = []
    original_fsync = candidates.os.fsync
    original_replace = candidates.os.replace

    def record_fsync(file_descriptor: int) -> None:
        mode = os.fstat(file_descriptor).st_mode
        events.append(
            "directory-fsync" if stat.S_ISDIR(mode) else "file-fsync",
        )
        original_fsync(file_descriptor)

    def record_replace(source, target) -> None:
        events.append("replace")
        original_replace(source, target)

    monkeypatch.setattr(candidates.os, "fsync", record_fsync)
    monkeypatch.setattr(candidates.os, "replace", record_replace)

    candidates.add_atom_contributions(
        skill_dir,
        [("durable-atom", 4, "")],
    )

    assert events == ["file-fsync", "replace", "directory-fsync"]


def test_move_source_replace_failure_keeps_at_least_one_durable_copy(
    tmp_path,
    monkeypatch,
):
    source_skill = tmp_path / "source"
    target_skill = tmp_path / "target"
    source_skill.mkdir()
    target_skill.mkdir()
    candidates.add_atom_contributions(
        source_skill,
        [("moving-atom", 8, "")],
    )
    candidates.save_candidates(target_skill, {"candidates": []})
    original_replace = candidates.os.replace
    replace_count = 0

    def fail_second_replace(source, target):
        nonlocal replace_count
        replace_count += 1
        if replace_count == 2:
            raise OSError("injected source replace failure")
        original_replace(source, target)

    monkeypatch.setattr(candidates.os, "replace", fail_second_replace)
    with pytest.raises(OSError, match="injected source replace failure"):
        candidates.move_atom_contribution(
            source_skill,
            target_skill,
            "moving-atom",
        )

    assert _candidate_ids(source_skill) == {"moving-atom"}
    assert _candidate_ids(target_skill) == {"moving-atom"}
    assert list(source_skill.glob("..candidates.yml.*.tmp")) == []


def test_move_rejects_identical_source_and_target_without_mutation(tmp_path):
    skill_dir = tmp_path / "same-skill"
    skill_dir.mkdir()
    candidates.add_atom_contributions(
        skill_dir,
        [("stationary-atom", 5, "")],
    )

    with pytest.raises(
        ValueError,
        match="source and target skill directories must differ",
    ):
        candidates.move_atom_contribution(
            skill_dir,
            skill_dir,
            "stationary-atom",
        )

    assert _candidate_ids(skill_dir) == {"stationary-atom"}


def test_move_rejects_symlink_alias_of_source_without_mutation(tmp_path):
    skill_dir = tmp_path / "real-skill"
    alias_dir = tmp_path / "skill-alias"
    skill_dir.mkdir()
    alias_dir.symlink_to(skill_dir, target_is_directory=True)
    candidates.add_atom_contributions(
        skill_dir,
        [("stationary-atom", 5, "")],
    )

    with pytest.raises(
        ValueError,
        match="source and target skill directories must differ",
    ):
        candidates.move_atom_contribution(
            skill_dir,
            alias_dir,
            "stationary-atom",
        )

    assert _candidate_ids(skill_dir) == {"stationary-atom"}


def test_archive_stale_filters_by_identity_without_quadratic_fuzzy_scan(
    tmp_path,
    monkeypatch,
):
    skill_dir = tmp_path / "stale-archive"
    skill_dir.mkdir()
    stale_entries = [
        {
            "pattern": f"old-pattern-{index}",
            "type": "step",
            "supporting_trajs": [],
            "first_seen": "2000-01-01",
            "promoted": False,
        }
        for index in range(1000)
    ]
    fresh_entry = {
        "pattern": "fresh-pattern",
        "type": "step",
        "supporting_trajs": [],
        "first_seen": "2999-01-01",
        "promoted": False,
    }
    candidates.save_candidates(
        skill_dir,
        {"candidates": [*stale_entries, fresh_entry]},
    )

    def reject_fuzzy_scan(_left: str, _right: str) -> bool:
        raise AssertionError("archive removal must not perform fuzzy scans")

    monkeypatch.setattr(candidates, "_fuzzy_equal", reject_fuzzy_scan)

    archived = candidates.archive_stale(skill_dir)

    assert len(archived) == 1000
    remaining = candidates.load_candidates(skill_dir)["candidates"]
    assert remaining == [fresh_entry]


def test_thousand_item_batch_loads_and_saves_once(tmp_path, monkeypatch):
    skill_dir = tmp_path / "large-batch"
    skill_dir.mkdir()
    call_counts = {"load": 0, "save": 0}
    original_save = candidates._atomic_save_candidates_unlocked

    class CountingCandidateList(list):
        full_scans = 0

        def __iter__(self):
            type(self).full_scans += 1
            return super().__iter__()

    candidate_buffer = CountingCandidateList()

    def counted_load(path: Path) -> dict:
        assert path == skill_dir
        call_counts["load"] += 1
        return {"candidates": candidate_buffer}

    def counted_save(path: Path, data: dict) -> None:
        call_counts["save"] += 1
        original_save(
            path,
            {
                **data,
                "candidates": list(data["candidates"]),
            },
        )

    monkeypatch.setattr(
        candidates,
        "_load_candidates_unlocked",
        counted_load,
    )
    monkeypatch.setattr(
        candidates,
        "_atomic_save_candidates_unlocked",
        counted_save,
    )

    tool_context = agent_tools.create_agent_tool_context(
        skill_dir=tmp_path,
        data_dir=tmp_path,
        config={},
        atom_skill_dir=tmp_path,
        atom_store=None,
        default_traj_root=tmp_path,
        cluster_batch_ids=[
            f"atom-{index:04d}"
            for index in range(1000)
        ],
    )
    with agent_tools.use_agent_tool_context(tool_context):
        result = agent_tools.add_tasks_to_skill.entrypoint(
            "large-batch",
            [
                {
                    "atom_id": f"atom-{index:04d}",
                    "weightscore": 1,
                }
                for index in range(1000)
            ],
        )

    assert "atoms=1000" in result
    assert "new=1000" in result
    assert "buffer_total=1000" in result
    assert call_counts == {"load": 1, "save": 1}
    assert CountingCandidateList.full_scans <= 3
    persisted = yaml.safe_load(
        (skill_dir / candidates.CANDIDATES_FILENAME).read_text(
            encoding="utf-8",
        ),
    )
    assert len(persisted["candidates"]) == 1000
