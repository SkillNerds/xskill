"""test_dashboard_metrics.py —— DashboardMetrics 衍生指标"""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

import xskill.dashboard.metrics as dashboard_metrics
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
    # 终态口径（审计 P2-10）：3 done + 1 splitting(在途,不进分母) → 100%
    assert o["success_rate"] == 100.0
    assert o["retry_rate"] == 25.0                  # 1 retried / 4
    # trajectories.ux_score 是死列（审计 P1-5）：无使用记录 → None 不显示假数
    assert o["avg_ux"] is None and o["ux_n"] == 0
    assert "skill_yield" not in o                   # 指标已下线（审计 P2-8）


def test_overview_empty_db_no_zerodiv(tmp_path):
    db = tmp_path / "e.db"
    get_connection(db).close()
    o = DashboardMetrics(db_path=db).overview()
    assert o == {"trajs": 0, "atoms": 0, "avg_atoms_per_traj": 0.0,
                 "success_rate": 0.0, "filtered": 0, "retry_rate": 0.0,
                 "avg_ux": None, "ux_n": 0}


def test_by_ecosystem(tmp_path):
    db = tmp_path / "r.db"
    _seed(db)
    rows = {r["ecosystem"]: r for r in DashboardMetrics(db_path=db).by_ecosystem()}
    assert rows["claude_code"]["trajs"] == 3 and rows["claude_code"]["atoms"] == 12
    assert "skills" not in rows["claude_code"]   # skill_generated 死列已下线
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


def test_skills_catalog_native_source_tag(tmp_path):
    """向后兼容：不传 skillhub 时,自产条目统一带 source='native',无 skillhub 混入。"""
    from xskill.skill.git import init_skill_repo_on_baby
    sd = tmp_path / "skill"; sd.mkdir()
    init_skill_repo_on_baby(str(sd / "wip-skill"), name="wip-skill", description="草稿")
    cat = skills_catalog(sd)
    assert len(cat) == 1
    assert cat[0]["source"] == "native"
    assert all(s.get("source") != "skillhub" for s in cat)


def _make_skillhub(tmp_path, name, description, sub="vendor/tool"):
    """构造启用态 SkillHub：hub_dir 下放一个三方 SKILL.md（embed_client 无需真实,
    技能库列表走 include_vec=False 分支）。"""
    from xskill.recommend.skillhub import SkillHub
    hub_dir = tmp_path / "hub"
    skdir = hub_dir / sub
    skdir.mkdir(parents=True)
    (skdir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n正文\n", encoding="utf-8")
    return SkillHub(enabled=True, hub_dir=hub_dir, embed_client=None)


def test_skills_catalog_merges_skillhub(tmp_path):
    """技能库列表既含自产(native)又含三方(skillhub)条目,字段符合契约。"""
    from xskill.skill.git import init_skill_repo_on_baby, commit_baby_to_main_branch
    sd = tmp_path / "skill"; sd.mkdir()
    init_skill_repo_on_baby(str(sd / "ready-skill"), name="ready-skill", description="正式描述")
    commit_baby_to_main_branch(str(sd / "ready-skill"), "graduate")
    hub = _make_skillhub(tmp_path, "vendor-skill", "三方能力描述", sub="vendor/tool")

    cat = skills_catalog(sd, skillhub=hub)
    by_source: dict = {}
    for s in cat:
        by_source.setdefault(s["source"], []).append(s)
    assert set(by_source) == {"native", "skillhub"}

    native = by_source["native"][0]
    assert native["name"] == "ready-skill" and native["state"] == "main"

    hub_row = by_source["skillhub"][0]
    assert hub_row["name"] == "vendor-skill"          # 展示名取 display_name
    assert hub_row["state"] == "skillhub"             # 无 git 分支
    assert hub_row["hub"] == "vendor/tool"            # skillhub 目录下相对路径
    assert hub_row["skill_id"].startswith("vendor-skill@")  # name@path_hash
    assert "三方能力描述" in hub_row["description"]
    assert hub_row["use_count"] == 0                  # 无使用记录 → 0
    # 自产排在三方之前
    assert cat.index(native) < cat.index(hub_row)


def test_skills_catalog_skillhub_none_is_noop(tmp_path):
    """skillhub=None（缺省/禁用）→ no-op：只有自产,不报错。"""
    from xskill.skill.git import init_skill_repo_on_baby
    sd = tmp_path / "skill"; sd.mkdir()
    init_skill_repo_on_baby(str(sd / "wip"), name="wip", description="d")
    assert skills_catalog(sd, skillhub=None) == skills_catalog(sd)
    # 禁用态 SkillHub 也应 no-op（其 _entries 内部判 enabled）
    from xskill.recommend.skillhub import SkillHub
    disabled = SkillHub(enabled=False, hub_dir=tmp_path / "nohub", embed_client=None)
    cat = skills_catalog(sd, skillhub=disabled)
    assert all(s["source"] == "native" for s in cat)


def test_skills_catalog_accepts_entry_list(tmp_path):
    """skillhub 入参也可直接是条目列表（契约：SkillHub 对象或 entries）。"""
    entries = [{
        "source": "skillhub", "name": "x@abc123", "skill_id": "x@abc123",
        "display_name": "x", "source_path": "team/x", "description": "e",
        "use_count": 5,
    }]
    cat = skills_catalog(tmp_path / "nope", skillhub=entries)
    assert len(cat) == 1
    row = cat[0]
    assert row["source"] == "skillhub" and row["hub"] == "team/x"
    assert row["skill_id"] == "x@abc123" and row["use_count"] == 5
    assert row["name"] == "x"


def _write_catalog_skill(root, name, description, branch="main"):
    skill = root / name
    (skill / ".git" / "refs" / "heads").mkdir(parents=True)
    (skill / ".git" / "refs" / "heads" / branch).write_text("sha\n", encoding="utf-8")
    (skill / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n"
        "metadata:\n  version: 1\n---\nbody\n",
        encoding="utf-8",
    )
    return skill


def test_skills_catalog_concurrent_calls_for_300_skills_scan_once(
        tmp_path, monkeypatch):
    root = tmp_path / "skills"
    root.mkdir()
    for i in range(300):
        _write_catalog_skill(root, f"skill-{i:03d}", f"description {i}")

    original = dashboard_metrics._build_skills_catalog_uncached
    calls = 0
    calls_lock = threading.Lock()

    def counted(*args, **kwargs):
        nonlocal calls
        with calls_lock:
            calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(dashboard_metrics, "_build_skills_catalog_uncached", counted)
    barrier = threading.Barrier(32)

    def load():
        barrier.wait()
        return skills_catalog(root)

    with ThreadPoolExecutor(max_workers=32) as pool:
        results = list(pool.map(lambda _i: load(), range(32)))
    assert calls == 1
    assert all(len(rows) == 300 for rows in results)


def test_skills_catalog_cache_ttl_reloads_refs_content_and_candidates(
        tmp_path, monkeypatch):
    root = tmp_path / "skills"
    root.mkdir()
    skill = _write_catalog_skill(root, "alpha", "version one", branch="main")
    monkeypatch.setattr(dashboard_metrics, "_SKILLS_CATALOG_TTL_SECONDS", 0.02)

    first = skills_catalog(root)[0]
    (skill / ".git" / "refs" / "heads" / "staging").write_text("sha2\n", encoding="utf-8")
    (skill / "SKILL.md").write_text(
        "---\nname: alpha\ndescription: version two\n"
        "metadata:\n  version: 2\n---\nbody\n",
        encoding="utf-8",
    )
    (skill / ".candidates.yml").write_text(
        "candidates:\n  - summary: one\n  - summary: two\n", encoding="utf-8")

    still_cached = skills_catalog(root)[0]
    assert (still_cached["state"], still_cached["description"], still_cached["candidates"]) == (
        "main", "version one", 0)
    time.sleep(0.04)
    refreshed = skills_catalog(root)[0]
    assert (refreshed["state"], refreshed["description"], refreshed["version"],
            refreshed["candidates"]) == ("staging", "version two", 2, 2)
    assert first == still_cached


def test_skills_catalog_cache_isolates_roots_and_skillhub_inputs(tmp_path):
    root_a = tmp_path / "a"; root_a.mkdir()
    root_b = tmp_path / "b"; root_b.mkdir()
    _write_catalog_skill(root_a, "same", "root a")
    _write_catalog_skill(root_b, "same", "root b")
    hub_a = [{"display_name": "hub-a", "source_path": "a/tool",
              "skill_id": "hub-a@1", "description": "A"}]
    hub_b = [{"display_name": "hub-b", "source_path": "b/tool",
              "skill_id": "hub-b@2", "description": "B"}]

    rows_a = skills_catalog(root_a, skillhub=hub_a)
    rows_b = skills_catalog(root_b, skillhub=hub_b)
    assert [(row["name"], row["description"]) for row in rows_a] == [
        ("same", "root a"), ("hub-a", "A")]
    assert [(row["name"], row["description"]) for row in rows_b] == [
        ("same", "root b"), ("hub-b", "B")]


def test_skills_catalog_equivalent_skillhub_instances_share_cache(
        tmp_path, monkeypatch):
    from xskill.recommend.skillhub import SkillHub

    root = tmp_path / "skills"; root.mkdir()
    hub_root = tmp_path / "hub"; hub_root.mkdir()
    _write_catalog_skill(hub_root, "vendor", "third party")
    first_hub = SkillHub(enabled=True, hub_dir=hub_root, embed_client=None)
    second_hub = SkillHub(enabled=True, hub_dir=hub_root, embed_client=object())
    original = dashboard_metrics._build_skills_catalog_uncached
    calls = 0

    def counted(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(
        dashboard_metrics, "_build_skills_catalog_uncached", counted)
    assert skills_catalog(root, skillhub=first_hub) == skills_catalog(
        root, skillhub=second_hub)
    assert calls == 1


def test_skills_catalog_failed_build_does_not_poison_cache(tmp_path):
    from xskill.recommend.skillhub import SkillHub

    root = tmp_path / "skills"; root.mkdir()
    hub_root = tmp_path / "hub"
    hub = SkillHub(enabled=True, hub_dir=hub_root, embed_client=None)
    with pytest.raises(FileNotFoundError):
        skills_catalog(root, skillhub=hub)

    _write_catalog_skill(hub_root, "vendor", "available now")
    rows = skills_catalog(root, skillhub=hub)
    assert [(row["name"], row["source"]) for row in rows] == [
        ("vendor", "skillhub")]


def test_skills_catalog_concurrent_failure_is_shared_and_cleans_flight(
        tmp_path, monkeypatch):
    root = tmp_path / "skills"; root.mkdir()
    original = dashboard_metrics._build_skills_catalog_uncached
    entered = threading.Event()
    release = threading.Event()
    calls = 0
    calls_lock = threading.Lock()

    def failing(*args, **kwargs):
        nonlocal calls
        with calls_lock:
            calls += 1
        entered.set()
        assert release.wait(timeout=5)
        raise FileNotFoundError("catalog unavailable")

    monkeypatch.setattr(
        dashboard_metrics, "_build_skills_catalog_uncached", failing)

    def load_error():
        try:
            skills_catalog(root)
        except BaseException as exc:  # 返回异常供主线程检查，不让 worker 中断
            return exc
        raise AssertionError("expected catalog failure")

    with ThreadPoolExecutor(max_workers=16) as pool:
        futures = [pool.submit(load_error) for _ in range(16)]
        assert entered.wait(timeout=5)
        time.sleep(0.05)
        release.set()
        errors = [future.result(timeout=5) for future in futures]

    assert calls == 1
    assert all(isinstance(error, FileNotFoundError) for error in errors)
    assert len({id(error) for error in errors}) == len(errors)
    key = dashboard_metrics._skills_catalog_cache_key(root, None)
    assert key not in dashboard_metrics._skills_catalog_flights
    assert key not in dashboard_metrics._skills_catalog_cache

    monkeypatch.setattr(
        dashboard_metrics, "_build_skills_catalog_uncached", original)
    assert skills_catalog(root) == []


def test_skills_catalog_cache_has_bounded_number_of_keys(tmp_path, monkeypatch):
    monkeypatch.setattr(dashboard_metrics, "_SKILLS_CATALOG_CACHE_MAX_ENTRIES", 2)
    with dashboard_metrics._skills_catalog_cache_lock:
        dashboard_metrics._skills_catalog_cache.clear()
        dashboard_metrics._skills_catalog_flights.clear()

    for index in range(3):
        root = tmp_path / f"skills-{index}"
        root.mkdir()
        _write_catalog_skill(root, f"skill-{index}", f"description {index}")
        assert len(skills_catalog(root)) == 1

    with dashboard_metrics._skills_catalog_cache_lock:
        assert len(dashboard_metrics._skills_catalog_cache) == 2
        assert not dashboard_metrics._skills_catalog_flights


def test_skills_catalog_returns_independent_copies(tmp_path):
    root = tmp_path / "skills"; root.mkdir()
    _write_catalog_skill(root, "alpha", "original")
    first = skills_catalog(root)
    first[0]["description"] = "caller mutation"
    first.append({"name": "injected"})

    second = skills_catalog(root)
    assert len(second) == 1
    assert second[0]["name"] == "alpha"
    assert second[0]["description"] == "original"


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


def test_tag_cloud_ttl_reuses_scan_and_refreshes_after_expiry(
        tmp_path, monkeypatch):
    from xskill.pipeline.atom import AtomTask, AtomTaskStore
    wd = tmp_path / "wd"; wd.mkdir()
    store = AtomTaskStore(root=wd)

    def save(index, tags):
        store.save(AtomTask(
            atom_id=f"atom_t_{index:04d}", traj_id="t",
            offset_start=1, offset_end=2, intent="i", summary="s",
            tags=tags, used_skills=[], ux_score=7,
            pre_atom_id=None, post_atom_id=None, context_prefix="", raw_segment="",
        ))

    save(0, ["first"])
    db = tmp_path / "tg-cache.db"
    conn = get_connection(db)
    conn.execute("INSERT INTO watch_dirs(path,label,ecosystem) VALUES(?,?,?)",
                 (str(wd), "w", "claude_code"))
    conn.commit(); conn.close()
    now = [0.0]
    metrics = DashboardMetrics(
        db_path=db, tag_cloud_ttl_seconds=5.0, clock=lambda: now[0],
    )
    original = AtomTaskStore.all_atoms
    scans = 0

    def counted(self):
        nonlocal scans
        scans += 1
        yield from original(self)

    monkeypatch.setattr(AtomTaskStore, "all_atoms", counted)
    assert metrics.tag_cloud() == [{"tag": "first", "count": 1, "users": []}]
    save(1, ["second"])
    assert metrics.tag_cloud() == [{"tag": "first", "count": 1, "users": []}]
    assert scans == 1

    now[0] = 6.0
    assert {row["tag"] for row in metrics.tag_cloud()} == {"first", "second"}
    assert scans == 2


def test_tag_cloud_concurrent_calls_share_one_scan(tmp_path, monkeypatch):
    db = tmp_path / "tg-flight.db"
    get_connection(db).close()
    metrics = DashboardMetrics(db_path=db)
    original = metrics._scan_tag_cloud
    entered = threading.Event()
    release = threading.Event()
    calls = 0
    calls_lock = threading.Lock()

    def counted():
        nonlocal calls
        with calls_lock:
            calls += 1
        entered.set()
        assert release.wait(timeout=5)
        return original()

    monkeypatch.setattr(metrics, "_scan_tag_cloud", counted)
    barrier = threading.Barrier(16)

    def load():
        barrier.wait()
        return metrics.tag_cloud()

    with ThreadPoolExecutor(max_workers=16) as pool:
        futures = [pool.submit(load) for _ in range(16)]
        assert entered.wait(timeout=5)
        release.set()
        assert all(future.result(timeout=5) == [] for future in futures)
    assert calls == 1


def test_by_model(tmp_path):
    db = tmp_path / "r.db"
    _seed(db)
    rows = {r["model"]: r for r in DashboardMetrics(db_path=db).by_model()}
    assert rows["deepseek-v4-flash"]["trajs"] == 3
    assert "skills" not in rows["deepseek-v4-pro"]  # 死列已下线
