"""MultiAtomTaskStore + 跨 store atom_task_read/read_traj 单测（team-CS 多 client）

复现并锁定 bug：team-CS server 有多个 watch_dir（每个 client 一个 store），
SkillEdit 的 atom_task_read/read_traj 只绑第一个 store → 跨 client 的 atom
返回 not found。修复后 MultiAtomTaskStore 跨 store 路由能读到任意 client 的
atom 原文；单 store 路径保持不变。
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest

from xskill.pipeline.atom import AtomTask, AtomTaskStore, MultiAtomTaskStore
from xskill.agents import agent_tools


def _atom(atom_id: str, traj_id: str, *, summary: str, raw: str = "RAW") -> AtomTask:
    return AtomTask(
        atom_id=atom_id, traj_id=traj_id,
        offset_start=1, offset_end=3,
        intent="i", summary=summary,
        tags=[], used_skills=[], ux_score=None,
        pre_atom_id=None, post_atom_id=None,
        context_prefix="", raw_segment=raw,
    )


def _two_client_stores(tmp_path: Path) -> tuple[AtomTaskStore, AtomTaskStore]:
    """造两个 client 的独立 store。atom 故意落在**第二个**（非第一个）store。"""
    root_a = tmp_path / "client_a"
    root_b = tmp_path / "client_b"
    root_a.mkdir()
    root_b.mkdir()
    store_a = AtomTaskStore(root=root_a)
    store_b = AtomTaskStore(root=root_b)
    # client A 的 atom + traj.md
    store_a.save(_atom("atom_traj_cc_a_0001", "traj_cc_a", summary="client A openpyxl"))
    (root_a / "traj_cc_a.md").write_text("A1\nA2\nA3\n", encoding="utf-8")
    # client B 的 atom + traj.md（这条是跨 store 查找的关键）
    store_b.save(_atom("atom_traj_cc_b_0042", "traj_cc_b", summary="client B merged cells"))
    (root_b / "traj_cc_b.md").write_text("B1\nB2\nB3\nB4\n", encoding="utf-8")
    return store_a, store_b


def _set_mtime(path: Path, ts: int) -> None:
    os.utime(path, (ts, ts))


class TestMultiStoreRouting:
    @pytest.mark.performance_contract
    def test_load_does_not_scan_any_store_trajectory_directories(
        self,
        tmp_path,
        monkeypatch,
    ):
        store_a, store_b = _two_client_stores(tmp_path)
        multi = MultiAtomTaskStore([store_a, store_b])
        roots = {store_a.root, store_b.root}
        original_iterdir = Path.iterdir

        def reject_store_scan(path):
            if path in roots:
                raise AssertionError("multi-store atom lookup scanned trajectory root")
            return original_iterdir(path)

        monkeypatch.setattr(Path, "iterdir", reject_store_scan)

        atom = multi.load("atom_traj_cc_b_0042")
        assert atom.summary == "client B merged cells"

    def test_load_finds_atom_in_non_first_store(self, tmp_path):
        store_a, store_b = _two_client_stores(tmp_path)
        multi = MultiAtomTaskStore([store_a, store_b])
        # 第二个 store 的 atom 能被读到
        a = multi.load("atom_traj_cc_b_0042")
        assert a.summary == "client B merged cells"
        # 第一个 store 的 atom 也能读到
        a2 = multi.load("atom_traj_cc_a_0001")
        assert a2.summary == "client A openpyxl"

    def test_load_missing_raises(self, tmp_path):
        store_a, store_b = _two_client_stores(tmp_path)
        multi = MultiAtomTaskStore([store_a, store_b])
        import pytest
        with pytest.raises(FileNotFoundError):
            multi.load("atom_nonexistent")

    def test_traj_root_for_routes_to_owning_client(self, tmp_path):
        store_a, store_b = _two_client_stores(tmp_path)
        multi = MultiAtomTaskStore([store_a, store_b])
        assert multi.traj_root_for("traj_cc_b") == store_b.root
        assert multi.traj_root_for("traj_cc_a") == store_a.root
        assert multi.traj_root_for("traj_missing") is None

    def test_list_by_traj_crosses_stores(self, tmp_path):
        store_a, store_b = _two_client_stores(tmp_path)
        multi = MultiAtomTaskStore([store_a, store_b])
        atoms = multi.list_by_traj("traj_cc_b")
        assert [a.atom_id for a in atoms] == ["atom_traj_cc_b_0042"]

    def test_all_atoms_iterates_all_stores(self, tmp_path):
        store_a, store_b = _two_client_stores(tmp_path)
        multi = MultiAtomTaskStore([store_a, store_b])
        ids = {a.atom_id for a in multi.all_atoms()}
        assert ids == {"atom_traj_cc_a_0001", "atom_traj_cc_b_0042"}

    def test_save_lands_in_owning_store(self, tmp_path):
        store_a, store_b = _two_client_stores(tmp_path)
        multi = MultiAtomTaskStore([store_a, store_b])
        a = multi.load("atom_traj_cc_b_0042")
        a.ux_score = 9
        multi.save(a)
        # 落回 client B 的 store（atom 本来就在那），不是 client A
        assert store_b.load("atom_traj_cc_b_0042").ux_score == 9
        assert store_a.list_by_traj("traj_cc_b") == []

    def test_empty_stores_rejected(self):
        import pytest
        with pytest.raises(ValueError):
            MultiAtomTaskStore([])

    def test_duplicate_atom_id_uses_newest_and_warns(self, tmp_path, caplog):
        store_a, store_b = _two_client_stores(tmp_path)
        # 用户可能多次加入同一个服务器，重复上传同一个原子。
        path_a = store_a.save(_atom(
            "atom_traj_same_0001", "traj_same",
            summary="client A duplicate atom",
        ))
        path_b = store_b.save(_atom(
            "atom_traj_same_0001", "traj_same",
            summary="client B duplicate atom",
        ))
        _set_mtime(path_a, 1000)
        _set_mtime(path_b, 2000)

        multi = MultiAtomTaskStore([store_a, store_b])
        caplog.set_level(logging.WARNING, logger="xskill.ux_score")

        atom = multi.load("atom_traj_same_0001")

        assert atom.summary == "client B duplicate atom"
        assert "atom id atom_traj_same_0001 duplicated across atom stores" in caplog.text

    def test_duplicate_atom_id_tie_uses_first_store(self, tmp_path, caplog):
        store_a, store_b = _two_client_stores(tmp_path)
        path_a = store_a.save(_atom(
            "atom_traj_same_0001", "traj_same",
            summary="client A duplicate atom",
        ))
        path_b = store_b.save(_atom(
            "atom_traj_same_0001", "traj_same",
            summary="client B duplicate atom",
        ))
        _set_mtime(path_a, 1000)
        _set_mtime(path_b, 1000)

        multi = MultiAtomTaskStore([store_a, store_b])
        caplog.set_level(logging.WARNING, logger="xskill.ux_score")

        atom = multi.load("atom_traj_same_0001")

        assert atom.summary == "client A duplicate atom"
        assert "atom id atom_traj_same_0001 duplicated across atom stores" in caplog.text

    def test_duplicate_traj_id_uses_newest_and_warns(self, tmp_path, caplog):
        store_a, store_b = _two_client_stores(tmp_path)
        store_a.save(_atom(
            "atom_traj_same_0001", "traj_same",
            summary="client A duplicate traj",
        ))
        store_b.save(_atom(
            "atom_traj_same_0002", "traj_same",
            summary="client B duplicate traj",
        ))
        (store_a.root / "traj_same.md").write_text("A\n", encoding="utf-8")
        (store_b.root / "traj_same.md").write_text("B\n", encoding="utf-8")
        _set_mtime(store_a.root / "traj_same.md", 1000)
        _set_mtime(store_b.root / "traj_same.md", 2000)

        multi = MultiAtomTaskStore([store_a, store_b])
        caplog.set_level(logging.WARNING, logger="xskill.ux_score")

        atoms = multi.list_by_traj("traj_same")

        assert [a.atom_id for a in atoms] == ["atom_traj_same_0002"]
        assert multi.traj_root_for("traj_same") == store_b.root
        assert "traj id traj_same duplicated across atom stores" in caplog.text


class TestSkillToolsAcrossStores:
    """SkillEdit 工具（atom_task_read / read_traj）在多 store ctx 下能读跨 client atom。

    这是 bug 的直接复现：修复前 atom_task_read 只绑第一个 store → 第二个 client
    的 atom 返回 'error: atom not found'。
    """

    def test_atom_task_read_reads_cross_client_atom(self, tmp_path):
        store_a, store_b = _two_client_stores(tmp_path)
        multi = MultiAtomTaskStore([store_a, store_b])
        skill_dir = tmp_path / "skill"
        skill_dir.mkdir()
        agent_tools.init_atom_task_tool_context(
            skill_dir=skill_dir, atom_store=multi,
            default_traj_root=store_a.root,
        )
        # 第二个 client 的 atom——修复前这里返回 not found
        out = agent_tools.atom_task_read.entrypoint("atom_traj_cc_b_0042")
        assert "atom_traj_cc_b_0042" in out
        assert "merged cells" in out
        assert not out.startswith("error")

    def test_read_traj_reads_cross_client_traj(self, tmp_path):
        store_a, store_b = _two_client_stores(tmp_path)
        multi = MultiAtomTaskStore([store_a, store_b])
        skill_dir = tmp_path / "skill"
        skill_dir.mkdir()
        # traj_root 绑成 client A 的 root；client B 的 traj 仍要能读到
        agent_tools.init_atom_task_tool_context(
            skill_dir=skill_dir, atom_store=multi,
            default_traj_root=store_a.root,
        )
        out = agent_tools.read_traj.entrypoint("traj_cc_b", offset_start=1, offset_end=3)
        assert out == "B1\nB2\n"

    def test_read_traj_reads_newest_duplicate_traj(self, tmp_path, caplog):
        store_a, store_b = _two_client_stores(tmp_path)
        (store_a.root / "traj_same.md").write_text("A\n", encoding="utf-8")
        (store_b.root / "traj_same.md").write_text("B\n", encoding="utf-8")
        _set_mtime(store_a.root / "traj_same.md", 1000)
        _set_mtime(store_b.root / "traj_same.md", 2000)
        multi = MultiAtomTaskStore([store_a, store_b])
        skill_dir = tmp_path / "skill"
        skill_dir.mkdir()
        agent_tools.init_atom_task_tool_context(
            skill_dir=skill_dir, atom_store=multi,
            default_traj_root=store_a.root,
        )
        caplog.set_level(logging.WARNING, logger="xskill.ux_score")

        out = agent_tools.read_traj.entrypoint("traj_same", offset_start=1, offset_end=2)
        assert out == "B\n"
        assert "traj id traj_same duplicated across atom stores" in caplog.text

    def test_single_store_behavior_unchanged(self, tmp_path):
        """单 store 路径（单机/cold_flush）：直接绑 AtomTaskStore，行为不回归。"""
        root = tmp_path / "cc_sessions"
        root.mkdir()
        store = AtomTaskStore(root=root)
        store.save(_atom("atom_traj_solo_0001", "traj_solo", summary="solo summary"))
        (root / "traj_solo.md").write_text("S1\nS2\nS3\n", encoding="utf-8")
        skill_dir = tmp_path / "skill"
        skill_dir.mkdir()
        agent_tools.init_atom_task_tool_context(
            skill_dir=skill_dir, atom_store=store,
            default_traj_root=root,
        )
        out = agent_tools.atom_task_read.entrypoint("atom_traj_solo_0001")
        assert "solo summary" in out
        # read_traj 在单 store（无 traj_root_for）下走绑定 traj_root
        assert agent_tools.read_traj.entrypoint("traj_solo", offset_start=1, offset_end=3) == "S1\nS2\n"
