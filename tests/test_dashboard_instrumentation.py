"""test_dashboard_instrumentation.py —— 三类埋点的记录 + 衍生率聚合 + 可靠性边界

重点验证可靠性：重复聚类不虚高(DISTINCT atom)、反复同步不虚低(DISTINCT client)、
除零安全、used 封顶 100%。
"""
from __future__ import annotations

from xskill.pipeline.registry import (
    get_connection, record_recommendation, record_atom_adoption,
    record_canary_decision,
)
from xskill.dashboard.metrics import DashboardMetrics


def _conn(db):
    return get_connection(db)


# ── 记录函数确实写入 ──────────────────────────────────────────────
def test_record_helpers_write_rows(tmp_path):
    db = tmp_path / "r.db"
    record_recommendation(client_id="c1", skill="s1", side="main", bucket="recommended", db_path=db)
    record_atom_adoption(atom_id="a1", skill="s1", weightscore=5, was_new=True, db_path=db)
    record_canary_decision(skill="s1", action="promoted", main_avg=8.0, staging_avg=8.5,
                           main_samples=6, staging_samples=6, age_days=2.0, db_path=db)
    conn = _conn(db)
    assert conn.execute("SELECT COUNT(*) FROM recommendation_log").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM atom_adoption").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM canary_decision").fetchone()[0] == 1
    conn.close()


# ── 原子采纳率：重复聚类同一 atom 不虚高(DISTINCT atom_id) ─────────
def test_adoption_rate_dedupes_atom(tmp_path):
    db = tmp_path / "r.db"
    conn = _conn(db)
    conn.execute("INSERT INTO watch_dirs(path,label) VALUES('/w','w')")
    # 总原子 = 10
    conn.execute("INSERT INTO trajectories(watch_dir_id,filename,tasks_extracted) VALUES(1,'f1',6)")
    conn.execute("INSERT INTO trajectories(watch_dir_id,filename,tasks_extracted) VALUES(1,'f2',4)")
    conn.commit(); conn.close()
    # a1 被聚类 3 次(覆盖更新)，a2 一次 → 采纳去重应是 2
    record_atom_adoption(atom_id="a1", skill="s", weightscore=5, was_new=True, db_path=db)
    record_atom_adoption(atom_id="a1", skill="s", weightscore=7, was_new=False, db_path=db)
    record_atom_adoption(atom_id="a1", skill="s", weightscore=8, was_new=False, db_path=db)
    record_atom_adoption(atom_id="a2", skill="s", weightscore=4, was_new=True, db_path=db)
    r = DashboardMetrics(db_path=db).adoption_rate()
    assert r == {"adopted": 2, "total": 10, "rate": 20.0}   # 2/10，重复不计


def test_adoption_rate_empty_no_zerodiv(tmp_path):
    db = tmp_path / "e.db"
    _conn(db).close()
    assert DashboardMetrics(db_path=db).adoption_rate() == {"adopted": 0, "total": 0, "rate": 0.0}


# ── 推荐触发率：反复同步不虚低(DISTINCT client) + used 封顶 ─────────
def test_trigger_rate_dedupes_client_and_caps(tmp_path):
    db = tmp_path / "r.db"
    conn = _conn(db)
    conn.execute("INSERT INTO watch_dirs(path,label) VALUES('/w','w')")
    # s1 被用 5 次；s2 没被用过
    for i in range(5):
        conn.execute("INSERT INTO trajectories(watch_dir_id,filename,skill_used) VALUES(1,?, 's1')",
                     (f"u{i}",))
    conn.commit(); conn.close()
    # s1 推给 c1 三次(反复同步)+c2 一次 → 去重后 distinct client = 2
    for _ in range(3):
        record_recommendation(client_id="c1", skill="s1", side="main", bucket="recommended", db_path=db)
    record_recommendation(client_id="c2", skill="s1", side="main", bucket="recommended", db_path=db)
    record_recommendation(client_id="c1", skill="s2", side="main", bucket="recommended", db_path=db)
    r = DashboardMetrics(db_path=db).trigger_rate()
    by = {x["skill"]: x for x in r["by_skill"]}
    assert by["s1"]["recommended"] == 2          # 去重：c1+c2，不是 4
    assert by["s1"]["used"] == 5
    assert by["s1"]["rate"] == 100.0             # 5/2 封顶 100%
    assert by["s2"]["recommended"] == 1 and by["s2"]["used"] == 0 and by["s2"]["rate"] == 0.0
    assert r["overall"] == 50.0                  # 2 个被推荐 skill,1 个被用过 → 50%


def test_trigger_rate_empty(tmp_path):
    db = tmp_path / "e.db"
    _conn(db).close()
    assert DashboardMetrics(db_path=db).trigger_rate() == {"overall": 0.0, "by_skill": []}


# ── canary 晋升率：只按已裁决算，waiting/no_staging 不入表 ─────────
def test_promotion_rate(tmp_path):
    db = tmp_path / "r.db"
    _conn(db).close()
    for _ in range(3):
        record_canary_decision(skill="x", action="promoted", main_avg=8, staging_avg=9,
                               main_samples=6, staging_samples=6, age_days=2, db_path=db)
    record_canary_decision(skill="y", action="rejected", main_avg=8, staging_avg=7,
                           main_samples=6, staging_samples=6, age_days=1, db_path=db)
    record_canary_decision(skill="z", action="timeout_discarded", main_avg=0, staging_avg=0,
                           main_samples=1, staging_samples=0, age_days=14, db_path=db)
    r = DashboardMetrics(db_path=db).promotion_rate()
    assert r == {"promoted": 3, "decided": 5, "rate": 60.0}   # 3/(3+1+1)


def test_promotion_rate_empty(tmp_path):
    db = tmp_path / "e.db"
    _conn(db).close()
    assert DashboardMetrics(db_path=db).promotion_rate() == {"promoted": 0, "decided": 0, "rate": 0.0}
