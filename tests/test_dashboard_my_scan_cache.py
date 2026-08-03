"""「我的」页全表扫收敛回归：三处 5s 单飞缓存 + atom_adoption(skill) 索引。

a4 线上事故：我的页端点每请求各自全表扫 trajectories / atom_adoption，
首屏叠出十余次全扫，SQLite gate 串行化拖慢全部面板接口。修复后同一波
请求只扫一次（TTL 内共享只读结果），adoption 按 skill 过滤走索引。
"""
from __future__ import annotations

from pathlib import Path

from xskill import events
from xskill.dashboard import console, explore
from xskill.pipeline.registry import get_connection


def _seed_db(db: Path) -> None:
    conn = get_connection(db)
    conn.execute(
        "INSERT INTO watch_dirs(id,path,label,ecosystem)"
        " VALUES(1,'/cc','alice','claude_code')",
    )
    conn.execute(
        "INSERT INTO trajectories(watch_dir_id,filename,status,user_key)"
        " VALUES(1,'traj_1.md','done','alice')",
    )
    conn.execute(
        "INSERT INTO trajectories(watch_dir_id,filename,status,user_key)"
        " VALUES(1,'traj_2.md','done','bob')",
    )
    conn.execute(
        "INSERT INTO atom_adoption(atom_id,skill,weightscore,ts)"
        " VALUES('atom_traj_1_0001','demo',3,'2026-07-30 00:00:00')",
    )
    conn.commit()
    conn.close()


def test_traj_user_map_cached_within_ttl(tmp_path):
    db = tmp_path / "r.db"
    _seed_db(db)
    events._traj_user_cache.clear()
    m1 = events._traj_user_map(db)
    assert m1["traj_1"] == "alice"

    conn = get_connection(db)
    conn.execute(
        "INSERT INTO trajectories(watch_dir_id,filename,status,user_key)"
        " VALUES(1,'traj_3.md','done','carol')",
    )
    conn.commit()
    conn.close()

    m2 = events._traj_user_map(db)
    assert m2 is m1            # TTL 内同一份共享对象，不重扫
    assert "traj_3" not in m2  # 短窗内允许略陈旧

    events._traj_user_cache.clear()
    m3 = events._traj_user_map(db)
    assert m3["traj_3"] == "carol"  # 清缓存后立刻可见


def test_visible_traj_info_cached_and_correct(tmp_path):
    db = tmp_path / "r.db"
    _seed_db(db)
    explore._visible_traj_cache.clear()
    t1 = explore._visible_traj_info(db)
    t2 = explore._visible_traj_info(db)
    assert t1 is t2
    assert t1["traj_1"]["user"] == "alice"
    assert t1["traj_2"]["user"] == "alice"  # 同 watch_dir 的 label


def test_adoption_rows_cached_and_fresh_after_clear(tmp_path):
    db = tmp_path / "r.db"
    _seed_db(db)
    console._adoption_rows_cache.clear()
    r1 = console._adoption_rows(db)
    r2 = console._adoption_rows(db)
    assert r1 is r2
    assert r1[0]["skill"] == "demo"

    conn = get_connection(db)
    conn.execute(
        "INSERT INTO atom_adoption(atom_id,skill,weightscore,ts)"
        " VALUES('atom_traj_2_0001','demo2',1,'2026-07-30 00:00:01')",
    )
    conn.commit()
    conn.close()
    assert len(console._adoption_rows(db)) == 1  # 仍走缓存
    console._adoption_rows_cache.clear()
    assert len(console._adoption_rows(db)) == 2


def test_atom_adoption_skill_index_exists(tmp_path):
    db = tmp_path / "r.db"
    _seed_db(db)
    conn = get_connection(db)
    names = {
        r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master"
            " WHERE type='index' AND tbl_name='atom_adoption'",
        )
    }
    conn.close()
    assert "idx_atom_adopt_skill" in names


def test_skill_lineage_correct_with_shared_traj_cache(tmp_path):
    db = tmp_path / "r.db"
    _seed_db(db)
    explore._visible_traj_cache.clear()
    skill_dir = tmp_path / "skills"
    (skill_dir / "demo").mkdir(parents=True)

    lin1 = explore.skill_lineage(skill_dir, "demo", db_path=db)
    lin2 = explore.skill_lineage(skill_dir, "demo", db_path=db)
    assert lin1["by_user"] == [{"user": "alice", "atoms": 1}]
    assert lin1["atoms"][0]["atom_id"] == "atom_traj_1_0001"
    assert lin2["by_user"] == lin1["by_user"]  # 缓存路径结果一致
