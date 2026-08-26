"""Atom SQLite 向量投影的增量复杂度、恢复与迁移测试。"""
from __future__ import annotations

import pickle
import sqlite3
from pathlib import Path

import numpy as np
import pytest

from tests.test_atom_task_store import _atom, _FakeEmbed, _SpyEmbed
from xskill.pipeline import atom_vector_index as vector_module
from xskill.pipeline.atom import AtomTaskStore


def _store(tmp_path: Path) -> AtomTaskStore:
    return AtomTaskStore(tmp_path / "store")


def _vector_rows(store: AtomTaskStore) -> list[sqlite3.Row]:
    connection = sqlite3.connect(store.root / store.VECTOR_INDEX_FILE)
    connection.row_factory = sqlite3.Row
    try:
        return connection.execute(
            """
            SELECT atom_id, traj_id, text_sha, generation, embedding, dim
            FROM atom_vectors ORDER BY atom_id
            """
        ).fetchall()
    finally:
        connection.close()


def _model(store: AtomTaskStore) -> str:
    connection = sqlite3.connect(store.root / store.VECTOR_INDEX_FILE)
    try:
        row = connection.execute(
            "SELECT model FROM atom_vector_meta WHERE singleton=1"
        ).fetchone()
        return row[0] if row else ""
    finally:
        connection.close()


def _seed(store: AtomTaskStore, count: int, *, traj_id: str = "t") -> None:
    store.save_many([
        _atom(
            atom_id=f"atom_{traj_id}_{index:04d}",
            traj_id=traj_id,
            offset_start=index * 10,
            offset_end=(index + 1) * 10,
            summary=f"task {index}",
        )
        for index in range(count)
    ])


@pytest.mark.performance_contract
def test_incremental_add_reads_no_historical_json(tmp_path, monkeypatch):
    store = _store(tmp_path)
    _seed(store, 100)
    store.rebuild_vector_index(_SpyEmbed())

    store.save(_atom(
        atom_id="atom_t_0100", traj_id="t", offset_start=1000,
        offset_end=1010, summary="task 100",
    ))
    reads = []
    original = vector_module._json_vector_input

    def counted(path, traj_id):
        reads.append(path)
        return original(path, traj_id)

    monkeypatch.setattr(vector_module, "_json_vector_input", counted)
    embed = _SpyEmbed()
    stats = store.rebuild_vector_index(embed)
    assert stats == {
        "mode": "incremental",
        "scanned": 0,
        "reused": 0,
        "embedded": 1,
        "changed_trajs": 0,
        "deleted": 0,
    }
    assert reads == []
    assert embed.batches == [["task 100"]]
    assert len(_vector_rows(store)) == 101


def test_non_vector_atom_update_is_a_noop(tmp_path, monkeypatch):
    store = _store(tmp_path)
    atom = _atom(atom_id="atom_t_0001", traj_id="t", summary="stable")
    store.save(atom)
    store.rebuild_vector_index(_SpyEmbed())
    marker_mtime = (store.root / store.INDEX_FILE).stat().st_mtime_ns
    atom.clustered = True
    atom.ux_score = 9
    store.save(atom)

    writes = []
    monkeypatch.setattr(
        store._vector_projection,
        "_write_marker",
        lambda: writes.append(True),
    )
    embed = _SpyEmbed()
    stats = store.rebuild_vector_index(embed)
    assert stats["embedded"] == stats["deleted"] == 0
    assert embed.batches == []
    assert writes == []
    assert (store.root / store.INDEX_FILE).stat().st_mtime_ns == marker_mtime


def test_projection_batch_failure_rolls_back_without_losing_json(
    tmp_path, monkeypatch,
):
    store = _store(tmp_path)
    atoms = [
        _atom(atom_id=f"atom_t_{index:04d}", traj_id="t", summary=f"task {index}")
        for index in range(3)
    ]
    projection = store._vector_projection
    original = projection._upsert_target
    calls = 0

    def fail_second(connection, target):
        nonlocal calls
        calls += 1
        original(connection, target)
        if calls == 2:
            raise sqlite3.OperationalError("injected projection failure")

    monkeypatch.setattr(projection, "_upsert_target", fail_second)
    store.save_many(atoms)

    assert len(store.list_by_traj("t")) == 3
    assert _vector_rows(store) == []
    assert store.vector_index_reconcile_due()


def test_projection_write_failure_after_complete_index_stays_due(
    tmp_path, monkeypatch,
):
    store = _store(tmp_path)
    _seed(store, 2)
    store.rebuild_vector_index(_SpyEmbed())
    assert not store.vector_index_reconcile_due()

    def fail(_connection, _target):
        raise sqlite3.OperationalError("injected projection failure")

    monkeypatch.setattr(store._vector_projection, "_upsert_target", fail)
    store.save(_atom(
        atom_id="atom_t_0002", traj_id="t", offset_start=20,
        offset_end=30, summary="late",
    ))

    assert store.load("atom_t_0002").summary == "late"
    assert store.vector_index_reconcile_due()


def test_text_change_invalidates_only_one_vector(tmp_path):
    store = _store(tmp_path)
    _seed(store, 3)
    store.rebuild_vector_index(_SpyEmbed())
    before = {row["atom_id"]: row["generation"] for row in _vector_rows(store)}

    store.save(_atom(
        atom_id="atom_t_0001", traj_id="t", offset_start=10,
        offset_end=20, summary="changed text",
    ))
    embed = _SpyEmbed()
    stats = store.rebuild_vector_index(embed)
    after = {row["atom_id"]: row["generation"] for row in _vector_rows(store)}
    assert stats["embedded"] == 1
    assert embed.batches == [["changed text"]]
    assert after["atom_t_0001"] == before["atom_t_0001"] + 1
    assert after["atom_t_0000"] == before["atom_t_0000"]
    assert after["atom_t_0002"] == before["atom_t_0002"]


def test_incremental_embedding_failure_keeps_old_index_and_recovers(tmp_path):
    store = _store(tmp_path)
    _seed(store, 2)
    store.rebuild_vector_index(_SpyEmbed())
    store.save(_atom(
        atom_id="atom_t_0002", traj_id="t", offset_start=20,
        offset_end=30, summary="late",
    ))

    class FailingEmbed(_FakeEmbed):
        def encode_batch(self, _texts):
            raise RuntimeError("backend unavailable")

    with pytest.raises(RuntimeError, match="backend unavailable"):
        store.rebuild_vector_index(FailingEmbed())
    rows = _vector_rows(store)
    assert sum(row["embedding"] is not None for row in rows) == 2
    assert sum(row["embedding"] is None for row in rows) == 1
    assert len(store.vector_search("task 0", _FakeEmbed(), top_k=5)) == 2

    recovered = store.rebuild_vector_index(_SpyEmbed())
    assert recovered["embedded"] == 1
    assert all(row["embedding"] is not None for row in _vector_rows(store))


def test_late_text_update_cannot_be_cleared_by_stale_embedding(tmp_path):
    store = _store(tmp_path)
    atom_v1 = _atom(atom_id="atom_t_0001", traj_id="t", summary="v1")
    store.save(atom_v1)
    store.rebuild_vector_index(_SpyEmbed())
    store.save(_atom(atom_id="atom_t_0001", traj_id="t", summary="v2"))

    class RacingEmbed(_SpyEmbed):
        def encode_batch(self, texts):
            self.batches.append(list(texts))
            if texts == ["v2"]:
                store.save(_atom(
                    atom_id="atom_t_0001", traj_id="t", summary="v3",
                ))
            return _FakeEmbed().encode_batch(texts)

    embed = RacingEmbed()
    stats = store.rebuild_vector_index(embed)
    assert embed.batches == [["v2"], ["v3"]]
    assert stats["embedded"] == 1
    row = _vector_rows(store)[0]
    expected = _FakeEmbed().encode("v3")
    expected = expected / (float(np.linalg.norm(expected)) or 1.0)
    assert np.allclose(np.frombuffer(row["embedding"], dtype=np.float32), expected)
    assert not store.vector_index_reconcile_due()


def test_failed_model_switch_does_not_expose_mixed_vectors(tmp_path):
    store = _store(tmp_path)
    _seed(store, 3)
    store.rebuild_vector_index(_SpyEmbed(model="old"))

    class FailingNewModel(_SpyEmbed):
        def __init__(self):
            super().__init__(model="new")

        def encode_batch(self, _texts):
            raise RuntimeError("switch failed")

    with pytest.raises(RuntimeError, match="switch failed"):
        store.rebuild_vector_index(FailingNewModel())
    assert _model(store) == "old"
    assert len(store.vector_search("task", _SpyEmbed(model="old"), top_k=5)) == 3
    assert store.vector_search("task", _SpyEmbed(model="new"), top_k=5) == []

    switched = store.rebuild_vector_index(_SpyEmbed(model="new"))
    assert switched["mode"] == "full"
    assert switched["embedded"] == 3
    assert _model(store) == "new"


def test_force_full_repairs_in_place_json_change(tmp_path):
    store = _store(tmp_path)
    _seed(store, 3)
    store.rebuild_vector_index(_SpyEmbed())
    atom_path = store.root / "t" / "tasks" / "atom_t_0001.json"
    atom = _atom(
        atom_id="atom_t_0001", traj_id="t", offset_start=10,
        offset_end=20, summary="externally changed",
    )
    # direct write intentionally bypasses save()/directory-mtime event semantics
    atom_path.write_text(atom.to_json(), encoding="utf-8")

    embed = _SpyEmbed()
    stats = store.rebuild_vector_index(embed, force_full=True)
    assert stats["mode"] == "full"
    assert stats["scanned"] == 3
    assert stats["reused"] == 2
    assert stats["embedded"] == 1
    assert embed.batches == [["externally changed"]]


@pytest.mark.performance_contract
def test_embedding_batches_have_a_fixed_memory_bound(tmp_path, monkeypatch):
    store = _store(tmp_path)
    _seed(store, 7)
    monkeypatch.setattr(vector_module, "EMBED_BATCH_SIZE", 2)
    embed = _SpyEmbed()
    stats = store.rebuild_vector_index(embed)
    assert stats["embedded"] == 7
    assert [len(batch) for batch in embed.batches] == [2, 2, 2, 1]


def test_full_rebuild_drops_trajectory_removed_during_streaming_scan(
    tmp_path, monkeypatch,
):
    store = _store(tmp_path)
    _seed(store, 2)
    projection = store._vector_projection
    original = projection._iter_task_paths

    def remove_after_first(tasks):
        paths = list(original(tasks))
        yield paths[0]
        for path in paths:
            if path.is_file():
                path.unlink()
        tasks.rmdir()
        yield from paths[1:]

    monkeypatch.setattr(projection, "_iter_task_paths", remove_after_first)
    stats = store.rebuild_vector_index(_SpyEmbed(), force_full=True)

    assert stats["mode"] == "full"
    assert _vector_rows(store) == []


def test_legacy_pickle_migration_reuses_vectors_without_embedding(tmp_path):
    store = _store(tmp_path)
    # Bypass save() to model a pre-migration store with JSON + full pickle only.
    atoms = [
        _atom(atom_id=f"atom_t_{index:04d}", traj_id="t", summary=f"task {index}")
        for index in range(3)
    ]
    tasks = store.root / "t" / "tasks"
    tasks.mkdir(parents=True)
    embed = _FakeEmbed()
    vectors = []
    for atom in atoms:
        (tasks / f"{atom.atom_id}.json").write_text(atom.to_json(), encoding="utf-8")
        vector = embed.encode(atom.summary)
        vectors.append(vector / (float(np.linalg.norm(vector)) or 1.0))
    (store.root / store.INDEX_FILE).write_bytes(pickle.dumps({
        "atom_ids": [atom.atom_id for atom in atoms],
        "embeddings": np.asarray(vectors),
        "model": "fake",
        "dim": 8,
    }))
    # 升级后首次 save 会先创建 incomplete SQLite；迁移完成前仍读 legacy 索引。
    store._vector_projection.record_atoms([atoms[0]])
    assert len(store.vector_search("task 1", embed, top_k=3)) == 3

    spy = _SpyEmbed()
    stats = store.rebuild_vector_index(spy)
    assert stats["reused"] == 3
    assert stats["embedded"] == 0
    assert spy.batches == []
    marker = pickle.loads((store.root / store.INDEX_FILE).read_bytes())
    assert marker["format"] == vector_module.FORMAT


def test_streaming_search_matches_full_matrix_reference(tmp_path):
    store = _store(tmp_path)
    _seed(store, 25)
    embed = _FakeEmbed()
    store.rebuild_vector_index(embed)
    query = "task 11"
    hits = store.vector_search(query, embed, top_k=7)

    query_vector = embed.encode(query)
    query_vector = query_vector / (float(np.linalg.norm(query_vector)) or 1.0)
    expected = []
    for row in _vector_rows(store):
        vector = np.frombuffer(row["embedding"], dtype=np.float32)
        expected.append((row["atom_id"], float(vector @ query_vector)))
    expected.sort(key=lambda item: (-item[1], item[0]))
    assert [hit["atom_id"] for hit in hits] == [item[0] for item in expected[:7]]
    assert np.allclose(
        [hit["similarity"] for hit in hits],
        [item[1] for item in expected[:7]],
    )


def test_non_positive_top_k_skips_query_embedding(tmp_path):
    store = _store(tmp_path)
    _seed(store, 3)
    store.rebuild_vector_index(_SpyEmbed())

    class QueryMustNotRun(_FakeEmbed):
        def encode(self, _text):
            raise AssertionError("top_k=0 must not encode or scan")

    assert store.vector_search("unused", QueryMustNotRun(), top_k=0) == []


def test_incremental_retry_repairs_missing_compatibility_marker(tmp_path):
    store = _store(tmp_path)
    _seed(store, 3)
    store.rebuild_vector_index(_SpyEmbed())
    marker = store.root / store.INDEX_FILE
    marker.unlink()

    embed = _SpyEmbed()
    stats = store.rebuild_vector_index(embed)

    assert stats["mode"] == "incremental"
    assert stats["embedded"] == 0
    assert embed.batches == []
    assert marker.is_file()


def test_low_frequency_reconcile_reuses_all_vectors(tmp_path):
    store = _store(tmp_path)
    _seed(store, 4)
    store.rebuild_vector_index(_SpyEmbed())
    assert not store.vector_index_reconcile_due()
    connection = sqlite3.connect(store.root / store.VECTOR_INDEX_FILE)
    try:
        connection.execute(
            "UPDATE atom_vector_meta SET reconciled_at=0 WHERE singleton=1"
        )
        connection.commit()
    finally:
        connection.close()
    restarted = AtomTaskStore(store.root)
    assert restarted.vector_index_reconcile_due()

    embed = _SpyEmbed()
    stats = restarted.rebuild_vector_index(embed)
    assert stats["mode"] == "full"
    assert stats["scanned"] == stats["reused"] == 4
    assert stats["embedded"] == 0
    assert embed.batches == []
    assert not restarted.vector_index_reconcile_due()
