"""tests/test_registry_overlap_isolation.py -- Issue #236 扫描语义统一与物理路径去重专项测试"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import pytest

from xskill.pipeline.registry import (
    register_dir,
    list_watch_dirs,
    discover_trajectories,
    find_overlapping_watch_dirs,
    Registry,
)
from xskill.cli import cmd_registry


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "test_registry.db"


class TestRegistryOverlapAndDedup:
    """Issue #236 三层防御体系验证用例集"""

    def test_tc01_only_parent_registered_flat_scan(self, tmp_path: Path, db_path: Path):
        """TC-01: 仅注册父目录时，仅平铺扫描直接轨迹，不穿透深层子目录。"""
        parent_dir = tmp_path / "team_trajectories"
        child_dir = parent_dir / "clients" / "user_a" / "sessions"
        child_dir.mkdir(parents=True)

        (parent_dir / "traj_parent_01.md").write_text("# parent traj 1")
        (child_dir / "traj_child_01.md").write_text("# child traj 1")

        parent_wid = register_dir(parent_dir, label="parent", db_path=db_path)
        discovered = discover_trajectories(parent_wid, parent_dir, db_path=db_path)

        assert "traj_parent_01.md" in discovered
        assert "traj_child_01.md" not in discovered
        assert len(discovered) == 1

    def test_tc02_only_child_registered(self, tmp_path: Path, db_path: Path):
        """TC-02: 仅注册子目录时，准确识别子目录直接轨迹。"""
        parent_dir = tmp_path / "team_trajectories"
        child_dir = parent_dir / "clients" / "user_a" / "sessions"
        child_dir.mkdir(parents=True)

        (child_dir / "traj_child_01.md").write_text("# child traj 1")
        (child_dir / "traj_child_02.md").write_text("# child traj 2")

        child_wid = register_dir(child_dir, label="child", db_path=db_path)
        discovered = discover_trajectories(child_wid, child_dir, db_path=db_path)

        assert sorted(discovered) == ["traj_child_01.md", "traj_child_02.md"]

    def test_tc03_parent_and_child_overlap_dedup(self, tmp_path: Path, db_path: Path):
        """TC-03: 父子目录重叠侦测与物理去重验证。"""
        parent_dir = tmp_path / "team_trajectories"
        child_dir = parent_dir / "clients" / "user_a" / "sessions"
        child_dir.mkdir(parents=True)

        (parent_dir / "traj_parent_01.md").write_text("# parent traj 1")
        (child_dir / "traj_child_01.md").write_text("# child traj 1")

        parent_wid = register_dir(parent_dir, label="parent", db_path=db_path)
        child_wid = register_dir(child_dir, label="child", db_path=db_path)

        # 侦测重叠
        reg = Registry(db_path=db_path)
        overlaps = reg.find_overlapping()
        assert len(overlaps) == 1
        assert overlaps[0]["parent"]["id"] == parent_wid
        assert overlaps[0]["child"]["id"] == child_wid

        # 各自平铺扫描
        p_files = discover_trajectories(parent_wid, parent_dir, db_path=db_path)
        c_files = discover_trajectories(child_wid, child_dir, db_path=db_path)

        # 物理路径聚合去重
        candidate_paths = [parent_dir / f for f in p_files] + [child_dir / f for f in c_files]
        seen_canonical_paths: set[Path] = set()
        deduped_paths: list[Path] = []
        for p in candidate_paths:
            canonical = p.resolve()
            if canonical not in seen_canonical_paths:
                seen_canonical_paths.add(canonical)
                deduped_paths.append(canonical)

        assert len(deduped_paths) == 2
        assert (parent_dir / "traj_parent_01.md").resolve() in deduped_paths
        assert (child_dir / "traj_child_01.md").resolve() in deduped_paths

    def test_tc04_distinct_directories_same_traj_name(self, tmp_path: Path, db_path: Path):
        """TC-04: 两个不同目录下的同名合法轨迹，均正常保留不被误去重。"""
        dir_a = tmp_path / "client_a" / "sessions"
        dir_b = tmp_path / "client_b" / "sessions"
        dir_a.mkdir(parents=True)
        dir_b.mkdir(parents=True)

        file_a = dir_a / "traj_0001.md"
        file_b = dir_b / "traj_0001.md"
        file_a.write_text("# client A traj")
        file_b.write_text("# client B traj")

        wid_a = register_dir(dir_a, label="user_a", db_path=db_path)
        wid_b = register_dir(dir_b, label="user_b", db_path=db_path)

        disc_a = discover_trajectories(wid_a, dir_a, db_path=db_path)
        disc_b = discover_trajectories(wid_b, dir_b, db_path=db_path)

        assert disc_a == ["traj_0001.md"]
        assert disc_b == ["traj_0001.md"]

        candidate_paths = [dir_a / f for f in disc_a] + [dir_b / f for f in disc_b]
        seen_canonical_paths: set[Path] = set()
        deduped = []
        for p in candidate_paths:
            c = p.resolve()
            if c not in seen_canonical_paths:
                seen_canonical_paths.add(c)
                deduped.append(c)

        # 物理路径不同，绝不按名字去重
        assert len(deduped) == 2
        assert file_a.resolve() in deduped
        assert file_b.resolve() in deduped

    def test_tc05_symlink_alias_dedup(self, tmp_path: Path, db_path: Path):
        """TC-05: 软链接指向同一物理文件时，按真实物理路径去重。"""
        real_dir = tmp_path / "real_store"
        link_dir = tmp_path / "link_store"
        real_dir.mkdir()
        link_dir.symlink_to(real_dir)

        real_file = real_dir / "traj_0001.md"
        real_file.write_text("# real traj")

        candidate_paths = [real_dir / "traj_0001.md", link_dir / "traj_0001.md"]
        seen: set[Path] = set()
        deduped: list[Path] = []
        for p in candidate_paths:
            c = p.resolve()
            if c not in seen:
                seen.add(c)
                deduped.append(c)

        # 软链接解析后同一物理文件，精确去重为 1
        assert len(deduped) == 1
        assert deduped[0] == real_file.resolve()

    def test_tc06_cli_registry_list_overlap_warning(self, tmp_path: Path, db_path: Path, capsys):
        """TC-06: 验证 xskill registry list 输出包含 WARNING 提示。"""
        parent_dir = tmp_path / "team_trajectories"
        child_dir = parent_dir / "clients" / "user_a" / "sessions"
        child_dir.mkdir(parents=True)

        reg = Registry(db_path=db_path)
        reg.add(parent_dir, label="parent", ecosystem="manual")
        reg.add(child_dir, label="user_a", ecosystem="team_client")

        fake_xskill = SimpleNamespace(registry=reg)
        fake_args = SimpleNamespace(registry_action="list")

        ret = cmd_registry(fake_args, fake_xskill)
        assert ret == 0

        captured = capsys.readouterr().out
        assert "WARNING: overlapping registry roots detected" in captured
        assert "Parent: id=" in captured
        assert "Child:  id=" in captured
        assert str(parent_dir.resolve()) in captured
        assert str(child_dir.resolve()) in captured
        assert "ID\tECOSYSTEM\tTRAJ\tINDEXED\tLABEL\tPATH" in captured
