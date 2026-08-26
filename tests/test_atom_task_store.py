"""AtomTaskStore 落盘 / 读取 / offset / 向量索引单测"""
from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import numpy as np

from xskill.pipeline.atom import AtomTask, AtomTaskStore


# ────────────────────────────────────────────────────────────────────
# Shared fake embed (8-dim deterministic hash). Used by hybrid_search and
# other downstream test modules; keep it importable as
# ``tests.test_atom_task_store._FakeEmbed``.
# ────────────────────────────────────────────────────────────────────

class _FakeEmbed:
    dim = 8
    model = "fake"
    base_url = "test://"

    def encode(self, text: str) -> np.ndarray:
        h = hashlib.sha256(text.encode("utf-8")).digest()
        return np.frombuffer(h[:8], dtype=np.uint8).astype(np.float32) / 255.0

    def encode_batch(self, texts: list[str]) -> np.ndarray:
        return np.stack([self.encode(t) for t in texts])


def _store(tmp_path: Path) -> AtomTaskStore:
    return AtomTaskStore(root=tmp_path / "cc-sessions")


def _atom(**overrides) -> AtomTask:
    defaults = dict(
        atom_id="atom_t_0001",
        traj_id="t",
        offset_start=0,
        offset_end=10,
        intent="i",
        summary="s",
        tags=[],
        used_skills=[],
        ux_score=None,
        pre_atom_id=None,
        post_atom_id=None,
        context_prefix="",
        raw_segment="",
    )
    defaults.update(overrides)
    return AtomTask(**defaults)


class TestAtomTaskRoundtrip:
    def test_save_then_load_returns_equivalent_atom(self, tmp_path):
        store = _store(tmp_path)
        atom = _atom(
            atom_id="atom_traj_cc_admin_05eed20c_0001",
            traj_id="traj_cc_admin_05eed20c",
            offset_start=120,
            offset_end=2400,
            intent="部署 xquiz 到 1717 端口",
            summary="agent 克隆 repo、读 README、未实际部署即停止",
            tags=["deploy", "fastapi"],
            context_prefix="<sysprompt>...</sysprompt>\n\n[省略 100 字符]",
        )
        store.save(atom)
        loaded = store.load(atom.atom_id)
        assert loaded == atom

    def test_list_atoms_by_traj_sorted_by_atom_id(self, tmp_path):
        store = _store(tmp_path)
        store.save(_atom(atom_id="atom_t_0002", traj_id="t",
                         offset_start=10, offset_end=20,
                         pre_atom_id="atom_t_0001"))
        store.save(_atom(atom_id="atom_t_0001", traj_id="t",
                         offset_start=0, offset_end=10,
                         post_atom_id="atom_t_0002"))
        listed = store.list_by_traj("t")
        assert [a.atom_id for a in listed] == ["atom_t_0001", "atom_t_0002"]


class TestIterTags:
    """iter_tags 只产出 tags 列表(标签云聚合用),不构建完整 AtomTask 对象。"""

    def test_iter_tags_yields_each_atom_tags_across_trajs(self, tmp_path):
        store = _store(tmp_path)
        store.save(_atom(atom_id="atom_a_0001", traj_id="a", tags=["deploy", "fastapi"]))
        store.save(_atom(atom_id="atom_a_0002", traj_id="a", tags=["deploy"]))
        store.save(_atom(atom_id="atom_b_0001", traj_id="b", tags=["testing"]))
        store.save(_atom(atom_id="atom_c_0001", traj_id="c", tags=[]))
        collected = sorted(tuple(tags) for tags in store.iter_tags())
        assert collected == [(), ("deploy",), ("deploy", "fastapi"), ("testing",)]

    def test_iter_tags_matches_all_atoms_tags(self, tmp_path):
        store = _store(tmp_path)
        store.save(_atom(atom_id="atom_a_0001", traj_id="a", tags=["x", "y"]))
        store.save(_atom(atom_id="atom_a_0002", traj_id="a", tags=["z"]))
        via_all_atoms = sorted(tuple(a.tags) for a in store.all_atoms())
        via_iter_tags = sorted(tuple(tags) for tags in store.iter_tags())
        assert via_iter_tags == via_all_atoms

    def test_iter_tags_empty_when_root_missing(self, tmp_path):
        store = AtomTaskStore(root=tmp_path / "does-not-exist")
        assert list(store.iter_tags()) == []


class TestOffsetPointer:
    def test_last_offset_zero_when_no_atoms(self, tmp_path):
        store = _store(tmp_path)
        assert store.last_offset("traj_x") == 0
        assert store.last_atom_id("traj_x") is None

    def test_last_offset_returns_max_end(self, tmp_path):
        store = _store(tmp_path)
        store.save(_atom(atom_id="atom_x_0001", traj_id="x",
                         offset_start=0, offset_end=2400))
        assert store.last_offset("x") == 2400
        assert store.last_atom_id("x") == "atom_x_0001"


class TestPaths:
    def test_files_land_under_traj_id_subdir(self, tmp_path):
        store = _store(tmp_path)
        store.save(_atom(atom_id="atom_y_0001", traj_id="y"))
        assert (tmp_path / "cc-sessions" / "y" / "tasks" /
                "atom_y_0001.json").is_file()


class TestAtomLocationProjection:
    def test_save_many_updates_projection_in_one_batch(
        self,
        tmp_path,
        monkeypatch,
    ):
        store = _store(tmp_path)
        store.root.mkdir(parents=True)
        store.ensure_location_index()
        atoms = [
            _atom(atom_id=f"atom_batch_{index:04d}", traj_id="batch")
            for index in range(3)
        ]
        original = store._upsert_atom_locations
        batches = []

        def record_batch(locations):
            batches.append(list(locations))
            original(locations)

        monkeypatch.setattr(store, "_upsert_atom_locations", record_batch)

        paths = store.save_many(atoms)

        assert len(batches) == 1
        assert len(batches[0]) == 3
        assert [store.load(atom.atom_id) for atom in atoms] == atoms
        assert all(path.is_file() for path in paths)

    def test_legacy_store_is_fully_projected_on_first_lookup(
        self,
        tmp_path,
        monkeypatch,
    ):
        store = _store(tmp_path)
        atoms = [
            _atom(atom_id=f"atom_legacy_{index:04d}", traj_id=f"legacy-{index}")
            for index in range(2)
        ]
        for atom in atoms:
            atom_path = store._path(atom)
            atom_path.parent.mkdir(parents=True)
            atom_path.write_text(atom.to_json(), encoding="utf-8")

        assert store.load(atoms[0].atom_id) == atoms[0]
        restarted = AtomTaskStore(store.root)
        original_iterdir = Path.iterdir

        def reject_root_scan(path):
            if path == store.root:
                raise AssertionError("complete legacy projection rescanned root")
            return original_iterdir(path)

        monkeypatch.setattr(Path, "iterdir", reject_root_scan)
        assert restarted.load(atoms[1].atom_id) == atoms[1]

    def test_restart_lookup_uses_projection_without_scanning_traj_dirs(
        self,
        tmp_path,
        monkeypatch,
    ):
        store = _store(tmp_path)
        for index in range(50):
            (store.root / f"empty-{index:02d}").mkdir(parents=True)
        atom = _atom(atom_id="atom_last_0001", traj_id="zz-last")
        expected = store.save(atom)
        restarted = AtomTaskStore(store.root)
        original_iterdir = Path.iterdir

        def reject_root_scan(path):
            if path == store.root:
                raise AssertionError("location lookup scanned trajectory root")
            return original_iterdir(path)

        monkeypatch.setattr(Path, "iterdir", reject_root_scan)

        assert restarted.path_for_atom(atom.atom_id) == expected
        assert restarted.load(atom.atom_id) == atom

    def test_legacy_miss_scans_once_then_repairs_projection(
        self,
        tmp_path,
        monkeypatch,
    ):
        store = _store(tmp_path)
        atom = _atom(atom_id="atom_legacy_0001", traj_id="legacy")
        atom_path = store._path(atom)
        atom_path.parent.mkdir(parents=True)
        atom_path.write_text(atom.to_json(), encoding="utf-8")

        assert store.path_for_atom(atom.atom_id) == atom_path
        original_iterdir = Path.iterdir

        def reject_root_scan(path):
            if path == store.root:
                raise AssertionError("repaired lookup rescanned trajectory root")
            return original_iterdir(path)

        monkeypatch.setattr(Path, "iterdir", reject_root_scan)
        assert store.path_for_atom(atom.atom_id) == atom_path

    def test_stale_path_falls_back_and_repairs_external_move(self, tmp_path):
        store = _store(tmp_path)
        atom = _atom(atom_id="atom_move_0001", traj_id="before")
        old_path = store.save(atom)
        new_path = store.root / "after" / "tasks" / old_path.name
        new_path.parent.mkdir(parents=True)
        old_path.replace(new_path)

        assert store.path_for_atom(atom.atom_id) == new_path
        restarted = AtomTaskStore(store.root)
        assert restarted.path_for_atom(atom.atom_id) == new_path

    def test_corrupt_projection_is_rebuilt_from_atom_json(self, tmp_path):
        store = _store(tmp_path)
        atom = _atom(atom_id="atom_rebuild_0001", traj_id="rebuild")
        atom_path = store.save(atom)
        store._location_index_path().write_bytes(b"not sqlite")
        restarted = AtomTaskStore(store.root)

        assert restarted.path_for_atom(atom.atom_id) == atom_path
        assert restarted.rebuild_location_index() == 1

    def test_missing_atom_clears_stale_projection_row(self, tmp_path):
        store = _store(tmp_path)
        atom = _atom(atom_id="atom_deleted_0001", traj_id="deleted")
        store.save(atom).unlink()

        assert store.path_for_atom(atom.atom_id) is None
        connection = store._location_connection()
        try:
            count = connection.execute(
                "SELECT COUNT(*) FROM atom_locations WHERE atom_id=?",
                (atom.atom_id,),
            ).fetchone()[0]
        finally:
            connection.close()
        assert count == 0


class TestVectorIndex:
    def test_build_then_query_returns_top_k(self, tmp_path):
        store = _store(tmp_path)
        embed = _FakeEmbed()
        for i in range(3):
            store.save(_atom(
                atom_id=f"atom_t_000{i}", traj_id="t",
                offset_start=i * 10, offset_end=(i + 1) * 10,
                intent=f"task{i}", summary=f"do thing {i}",
            ))
        store.rebuild_vector_index(embed)
        hits = store.vector_search("do thing 1", embed, top_k=2)
        assert len(hits) == 2
        assert hits[0]["atom_id"] == "atom_t_0001"  # exact match wins

    def test_index_persists_to_disk(self, tmp_path):
        store = _store(tmp_path)
        store.save(_atom(atom_id="atom_q_0001", traj_id="q", summary="lonely"))
        store.rebuild_vector_index(_FakeEmbed())
        idx_path = tmp_path / "cc-sessions" / "index.pkl"
        assert idx_path.is_file()

    def test_query_with_no_index_returns_empty(self, tmp_path):
        store = _store(tmp_path)
        hits = store.vector_search("anything", _FakeEmbed(), top_k=5)
        assert hits == []


class _SpyEmbed(_FakeEmbed):
    """记录每次 ``encode_batch`` 输入文本，用于断言增量复用只 embed 新原子。"""

    def __init__(self, model: str = "fake"):
        self.model = model
        self.batches: list[list[str]] = []

    def encode_batch(self, texts: list[str]) -> np.ndarray:
        self.batches.append(list(texts))
        return super().encode_batch(texts)


def _read_index(tmp_path: Path, store_name: str = "cc-sessions") -> dict:
    path = tmp_path / store_name / AtomTaskStore.VECTOR_INDEX_FILE
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            """
            SELECT atom_id, embedding, dim FROM atom_vectors
            WHERE embedding IS NOT NULL ORDER BY traj_id, atom_id
            """
        ).fetchall()
        meta = connection.execute(
            "SELECT model FROM atom_vector_meta WHERE singleton=1"
        ).fetchone()
    finally:
        connection.close()
    vectors = [
        np.frombuffer(row["embedding"], dtype=np.float32, count=int(row["dim"]))
        for row in rows
    ]
    return {
        "atom_ids": [row["atom_id"] for row in rows],
        "embeddings": np.stack(vectors) if vectors else np.empty((0, 0)),
        "model": meta["model"] if meta else "",
    }


class TestVectorIndexIncremental:
    def test_first_rebuild_embeds_all(self, tmp_path):
        store = _store(tmp_path)
        embed = _SpyEmbed()
        for i in range(3):
            store.save(_atom(atom_id=f"atom_t_000{i}", traj_id="t",
                             offset_start=i * 10, offset_end=(i + 1) * 10,
                             summary=f"do thing {i}"))
        store.rebuild_vector_index(embed)
        # 首次：一次 batch 覆盖全部 3 个原子
        assert embed.batches == [["do thing 0", "do thing 1", "do thing 2"]]
        idx = _read_index(tmp_path)
        assert idx["atom_ids"] == ["atom_t_0000", "atom_t_0001", "atom_t_0002"]

    def test_added_atom_only_embeds_the_new_one(self, tmp_path):
        store = _store(tmp_path)
        embed = _SpyEmbed()
        for i in range(2):
            store.save(_atom(atom_id=f"atom_t_000{i}", traj_id="t",
                             offset_start=i * 10, offset_end=(i + 1) * 10,
                             summary=f"do thing {i}"))
        store.rebuild_vector_index(embed)
        embed.batches.clear()
        # 新增第 3 个原子后再 rebuild：只 embed 新原子，其余复用缓存
        store.save(_atom(atom_id="atom_t_0002", traj_id="t",
                         offset_start=20, offset_end=30, summary="do thing 2"))
        store.rebuild_vector_index(embed)
        assert embed.batches == [["do thing 2"]]
        idx = _read_index(tmp_path)
        assert idx["atom_ids"] == [
            "atom_t_0000", "atom_t_0001", "atom_t_0002"]

    def test_reused_rows_match_full_rebuild(self, tmp_path):
        # 增量拼出来的向量与整批重算逐行一致（行顺序对齐 all_atoms()）
        store = _store(tmp_path)
        for i in range(2):
            store.save(_atom(atom_id=f"atom_t_000{i}", traj_id="t",
                             offset_start=i * 10, offset_end=(i + 1) * 10,
                             summary=f"do thing {i}"))
        store.rebuild_vector_index(_SpyEmbed())
        store.save(_atom(atom_id="atom_t_0002", traj_id="t",
                         offset_start=20, offset_end=30, summary="do thing 2"))
        store.rebuild_vector_index(_SpyEmbed())
        incremental = _read_index(tmp_path)["embeddings"]

        # 另建等价 store 一次性整批重建作为基准
        ref_store = AtomTaskStore(root=tmp_path / "ref")
        for i in range(3):
            ref_store.save(_atom(atom_id=f"atom_t_000{i}", traj_id="t",
                                 offset_start=i * 10, offset_end=(i + 1) * 10,
                                 summary=f"do thing {i}"))
        ref_store.rebuild_vector_index(_SpyEmbed())
        full = _read_index(tmp_path, "ref")["embeddings"]
        assert np.allclose(incremental, full)

    def test_model_change_reembeds_all(self, tmp_path):
        store = _store(tmp_path)
        for i in range(2):
            store.save(_atom(atom_id=f"atom_t_000{i}", traj_id="t",
                             offset_start=i * 10, offset_end=(i + 1) * 10,
                             summary=f"do thing {i}"))
        store.rebuild_vector_index(_SpyEmbed(model="fake"))
        # 换 embedding 模型：旧向量作废，全部重算
        embed2 = _SpyEmbed(model="other")
        store.rebuild_vector_index(embed2)
        assert embed2.batches == [["do thing 0", "do thing 1"]]
        assert _read_index(tmp_path)["model"] == "other"

    def test_deleted_atom_dropped_from_index(self, tmp_path):
        store = _store(tmp_path)
        for i in range(3):
            store.save(_atom(atom_id=f"atom_t_000{i}", traj_id="t",
                             offset_start=i * 10, offset_end=(i + 1) * 10,
                             summary=f"do thing {i}"))
        store.rebuild_vector_index(_SpyEmbed())
        # 删掉一个原子文件后 rebuild：索引里不再有它
        (tmp_path / "cc-sessions" / "t" / "tasks" /
         "atom_t_0001.json").unlink()
        embed = _SpyEmbed()
        store.rebuild_vector_index(embed, force_full=True)
        idx = _read_index(tmp_path)
        assert idx["atom_ids"] == ["atom_t_0000", "atom_t_0002"]
        # 复用命中，无需再 embed 任何原子
        assert embed.batches == []
        assert idx["embeddings"].shape[0] == 2

    def test_empty_store_writes_no_file(self, tmp_path):
        store = _store(tmp_path)
        store.rebuild_vector_index(_SpyEmbed())
        assert not (tmp_path / "cc-sessions" / "index.pkl").exists()


class TestAllAtoms:
    def test_iterates_across_trajs(self, tmp_path):
        store = _store(tmp_path)
        store.save(_atom(atom_id="atom_a_0001", traj_id="a"))
        store.save(_atom(atom_id="atom_b_0001", traj_id="b"))
        ids = {a.atom_id for a in store.all_atoms()}
        assert ids == {"atom_a_0001", "atom_b_0001"}


# ────────────────────────────────────────────────────────────────────
# source_model 继承(batch3):AtomTask 带 model + json 往返兼容旧文件
# ────────────────────────────────────────────────────────────────────

def test_atom_source_model_json_roundtrip():
    a = AtomTask(atom_id="atom_t_0001", traj_id="t", offset_start=1,
                 offset_end=2, intent="i", summary="s",
                 source_model="claude-opus-4-7")
    back = AtomTask.from_json(a.to_json())
    assert back.source_model == "claude-opus-4-7"


def test_atom_from_json_without_source_model_defaults_empty():
    # 旧 json(无 source_model 字段)仍能加载,默认空串
    legacy = ('{"atom_id":"a","traj_id":"t","offset_start":1,"offset_end":2,'
              '"intent":"i","summary":"s"}')
    assert AtomTask.from_json(legacy).source_model == ""


def test_task_agent_sidecar_model_reader(tmp_path):
    from xskill.agents.task_agent import _sidecar_model
    md = tmp_path / "traj_cc_x_001.md"
    md.write_text("# body", encoding="utf-8")
    assert _sidecar_model(md) == ""                       # 无 sidecar → 空
    (tmp_path / "traj_cc_x_001.json").write_text(
        '{"model": "deepseek-v4-flash"}', encoding="utf-8")
    assert _sidecar_model(md) == "deepseek-v4-flash"      # 有 sidecar → 读出
