"""test_dashboard_metrics.py —— DashboardMetrics 衍生指标"""
from __future__ import annotations

from xskill.pipeline.registry import get_connection, harness_share, model_share
from xskill.dashboard.metrics import DashboardMetrics, skills_catalog


def _seed_team(db):
    """一个 team server：自有 claude_code 本机目录 + 一个 team_client 上传目录。
    team 上传的轨迹有的带 source_harness（新 client），有的没带（旧 client）。"""
    conn = get_connection(db)
    conn.execute("INSERT INTO watch_dirs(id,path,label,ecosystem) VALUES(1,'/cc','cc','claude_code')")
    conn.execute("INSERT INTO watch_dirs(id,path,label,ecosystem) VALUES(2,'/tc','client-a','team_client')")
    rows = [  # (wd, source_harness)
        (1, None),          # 本机 cc 目录 → harness 从 ecosystem 推断 claude_code
        (2, 'codex'),       # team 上传，新 client 带了 harness
        (2, None),          # team 上传，旧 client 没带 → unknown
    ]
    for i, (wd, hn) in enumerate(rows):
        conn.execute("INSERT INTO trajectories(watch_dir_id,filename,status,source_harness)"
                     " VALUES(?,?,?,?)", (wd, f"traj_{i}.md", "done", hn))
    conn.commit()
    conn.close()


def test_harness_share_derives_from_ecosystem(tmp_path):
    db = tmp_path / "h.db"
    _seed_team(db)
    share = {h["harness"]: h["trajs"] for h in harness_share(db)}
    assert share == {"claude_code": 1, "codex": 1, "unknown": 1}
    # 内部标签 team_client 绝不暴露给用户
    assert "team_client" not in share


def test_by_ecosystem_replaces_team_client_with_harness(tmp_path):
    db = tmp_path / "h.db"
    _seed_team(db)
    ecos = {r["ecosystem"]: r["trajs"] for r in DashboardMetrics(db_path=db).by_ecosystem()}
    assert "team_client" not in ecos          # 不再把内部标签当生态
    assert ecos.get("claude_code") == 1
    assert ecos.get("codex") == 1             # team 上传带 harness → 归 codex
    assert ecos.get("unknown") == 1           # team 上传无 harness → unknown


def test_harness_share_custom_unknown_label(tmp_path):
    # config.dashboard.default_harness 覆盖：缺 harness 的轨迹归到指定桶，不再叫 unknown
    db = tmp_path / "h.db"
    _seed_team(db)
    share = {h["harness"]: h["trajs"] for h in harness_share(db, unknown_label="claude_code")}
    # 那条无 harness 的 team 上传并入 claude_code（本机 1 + 兜底 1）
    assert share == {"claude_code": 2, "codex": 1}
    assert "unknown" not in share


def test_by_ecosystem_custom_unknown_label(tmp_path):
    db = tmp_path / "h.db"
    _seed_team(db)
    m = DashboardMetrics(db_path=db, unknown_harness="codex")
    ecos = {r["ecosystem"]: r["trajs"] for r in m.by_ecosystem()}
    assert ecos.get("codex") == 2             # 自带 codex 1 + 兜底并入 1
    assert "unknown" not in ecos


def test_by_model_custom_unknown_label(tmp_path):
    db = tmp_path / "h.db"
    conn = get_connection(db)
    conn.execute("INSERT INTO watch_dirs(id,path,label,ecosystem) VALUES(1,'/cc','cc','claude_code')")
    conn.execute("INSERT INTO trajectories(watch_dir_id,filename,status,source_model)"
                 " VALUES(1,'a.md','done','deepseek-v4-pro')")
    conn.execute("INSERT INTO trajectories(watch_dir_id,filename,status,source_model)"
                 " VALUES(1,'b.md','done',NULL)")
    conn.commit(); conn.close()
    models = {r["model"]: r["trajs"] for r in
              DashboardMetrics(db_path=db, unknown_model="deepseek-v4-flash").by_model()}
    assert models == {"deepseek-v4-pro": 1, "deepseek-v4-flash": 1}


def test_unknown_label_is_sql_injection_safe(tmp_path):
    # 自由字符串经命名绑定参数注入：带引号/分号的标签原样出现，不破坏 SQL
    db = tmp_path / "h.db"
    _seed_team(db)
    weird = "o'brien; DROP TABLE trajectories;--"
    share = {h["harness"]: h["trajs"] for h in harness_share(db, unknown_label=weird)}
    assert share.get(weird) == 1              # 兜底标签原样作为分组键
    # 表没被删：再查一次仍有数据
    assert sum(h["trajs"] for h in harness_share(db)) == 3


def test_model_share_default_label_unchanged(tmp_path):
    # 不传 unknown_label → 仍是 'unknown'（保护 canary/stats 的哨兵语义）
    db = tmp_path / "h.db"
    conn = get_connection(db)
    conn.execute("INSERT INTO watch_dirs(id,path,label,ecosystem) VALUES(1,'/cc','cc','claude_code')")
    conn.execute("INSERT INTO trajectories(watch_dir_id,filename,status,source_model) VALUES(1,'a.md','done',NULL)")
    conn.commit(); conn.close()
    assert model_share(db)[0]["model"] == "unknown"


def _seed(db):
    conn = get_connection(db)
    conn.execute("INSERT INTO watch_dirs(path,label,ecosystem) VALUES('/cc','cc','claude_code')")
    conn.execute("INSERT INTO watch_dirs(path,label,ecosystem) VALUES('/oc','oc','opencode')")
    rows = [  # (wd, status, atoms, skill_generated, retry, ux, model)
        (1, 'done', 6, 'nginx-skill', 0, 8.0, 'deepseek-v4-pro'),
        (1, 'done', 4, '', 1, 7.0, 'deepseek-v4-flash'),
        (1, 'splitting', 2, None, 0, None, 'deepseek-v4-flash'),
        (2, 'done', 3, 'oc-skill', 0, 7.5, 'deepseek-v4-flash'),
    ]
    for wd, st, a, sg, rt, ux, m in rows:
        conn.execute("INSERT INTO trajectories(watch_dir_id,filename,status,tasks_extracted,"
                     "skill_generated,retry_count,ux_score,source_model) VALUES(?,?,?,?,?,?,?,?)",
                     (wd, f"f{a}{st}", st, a, sg, rt, ux, m))
    conn.commit()
    conn.close()


def test_overview_ratios(tmp_path):
    db = tmp_path / "r.db"
    _seed(db)
    o = DashboardMetrics(db_path=db).overview()
    assert o["trajs"] == 4 and o["atoms"] == 15
    assert o["avg_atoms_per_traj"] == 3.75          # 15/4
    assert o["success_rate"] == 75.0                # 3 done / 4
    assert o["skill_yield"] == 50.0                 # 2 有 skill / 4
    assert o["retry_rate"] == 25.0                  # 1 retried / 4
    assert round(o["avg_ux"], 2) == 7.5             # (8+7+7.5)/3


def test_overview_empty_db_no_zerodiv(tmp_path):
    db = tmp_path / "e.db"
    get_connection(db).close()
    o = DashboardMetrics(db_path=db).overview()
    assert o == {"trajs": 0, "atoms": 0, "avg_atoms_per_traj": 0.0, "success_rate": 0.0,
                 "skill_yield": 0.0, "retry_rate": 0.0, "avg_ux": 0.0}


def test_by_ecosystem(tmp_path):
    db = tmp_path / "r.db"
    _seed(db)
    rows = {r["ecosystem"]: r for r in DashboardMetrics(db_path=db).by_ecosystem()}
    assert rows["claude_code"]["trajs"] == 3 and rows["claude_code"]["atoms"] == 12
    assert rows["claude_code"]["skills"] == 1
    assert rows["opencode"]["trajs"] == 1


def test_skills_catalog_lists_skills(tmp_path):
    """技能库清单：分析式读 skill 目录,不依赖埋点 → 永远有内容。"""
    from xskill.skill.git import init_skill_repo_on_baby, commit_baby_to_main_branch
    sd = tmp_path / "skill"
    sd.mkdir()
    # 一个 baby、一个已 graduate 到 main
    init_skill_repo_on_baby(str(sd / "wip-skill"), name="wip-skill", description="草稿描述")
    init_skill_repo_on_baby(str(sd / "ready-skill"), name="ready-skill", description="正式描述")
    commit_baby_to_main_branch(str(sd / "ready-skill"), "graduate")
    (sd / ".hidden").mkdir()  # 隐藏目录应被跳过

    cat = skills_catalog(sd)
    names = {s["name"]: s for s in cat}
    assert set(names) == {"wip-skill", "ready-skill"}
    assert names["wip-skill"]["state"] == "baby"
    assert names["ready-skill"]["state"] == "main"
    assert "正式描述" in names["ready-skill"]["description"]
    # main 排在 baby 前
    assert cat[0]["name"] == "ready-skill"


def test_skills_catalog_empty_dir(tmp_path):
    assert skills_catalog(tmp_path / "nope") == []


def test_users_lists_team_clients(tmp_path):
    db = tmp_path / "u.db"
    conn = get_connection(db)
    conn.execute("INSERT INTO watch_dirs(id,path,label,ecosystem) VALUES(1,'/a','alice','team_client')")
    conn.execute("INSERT INTO watch_dirs(id,path,label,ecosystem) VALUES(2,'/b','bob','team_client')")
    conn.execute("INSERT INTO watch_dirs(id,path,label,ecosystem) VALUES(3,'/cc','local','claude_code')")
    conn.execute("INSERT INTO trajectories(watch_dir_id,filename,tasks_extracted) VALUES(1,'t1.md',3)")
    conn.execute("INSERT INTO trajectories(watch_dir_id,filename,tasks_extracted) VALUES(1,'t2.md',2)")
    conn.execute("INSERT INTO trajectories(watch_dir_id,filename,tasks_extracted) VALUES(2,'t3.md',1)")
    conn.commit(); conn.close()
    users = {u["client_id"]: u for u in DashboardMetrics(db_path=db).users()}
    assert set(users) == {"alice", "bob"}      # 本机 claude_code 不算"团队用户"
    assert users["alice"]["trajs"] == 2 and users["alice"]["atoms"] == 5
    assert users["bob"]["trajs"] == 1


def test_tag_cloud_aggregates_atom_tags(tmp_path):
    from xskill.pipeline.atom import AtomTask, AtomTaskStore
    wd = tmp_path / "wd"; wd.mkdir()
    store = AtomTaskStore(root=wd)
    for i, tags in enumerate([["django", "migrate"], ["django", "orm"], ["nginx"]]):
        store.save(AtomTask(
            atom_id=f"atom_t_{i:04d}", traj_id="t", offset_start=1, offset_end=2,
            intent="i", summary="s", tags=tags, used_skills=[], ux_score=7,
            pre_atom_id=None, post_atom_id=None, context_prefix="", raw_segment=""))
    db = tmp_path / "tg.db"
    conn = get_connection(db)
    conn.execute("INSERT INTO watch_dirs(path,label,ecosystem) VALUES(?,?,?)",
                 (str(wd), "w", "claude_code"))
    conn.commit(); conn.close()
    cloud = {t["tag"]: t["count"] for t in DashboardMetrics(db_path=db).tag_cloud()}
    assert cloud["django"] == 2
    assert cloud["migrate"] == 1 and cloud["nginx"] == 1


def test_by_model(tmp_path):
    db = tmp_path / "r.db"
    _seed(db)
    rows = {r["model"]: r for r in DashboardMetrics(db_path=db).by_model()}
    assert rows["deepseek-v4-flash"]["trajs"] == 3
    assert rows["deepseek-v4-pro"]["skills"] == 1
