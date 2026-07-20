"""test_dashboard_cluster_graph_cache.py —— §2.5 聚类 graph 的短时缓存 + 单飞。

cluster_graph 每次要给每个用户读两次画像库（load + load_points）再做 O(用户²)
两两相似度，且 admin 面板每次打开就整块重算——这里覆盖：
TTL 内不重算 / 并发只算一次 / 过期能看到新画像 / 返回独立副本 / 口径不变。
"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np

import xskill.dashboard.metrics as metrics_module
import xskill.dashboard.profile_viz as profile_viz_module
from xskill.dashboard.profile_viz import ProfileViz
from xskill.recommend.profile_store import ProfileStore


def _seed_two_similar_users(pdb: Path) -> ProfileStore:
    """alice/bob 同向 mean（应连边），carol 反向（孤立）。"""
    store = ProfileStore(pdb)
    points = np.array([[1.0, 0.1, 0, 0], [0.9, 0.2, 0, 0]], dtype=float)
    points /= np.linalg.norm(points, axis=1, keepdims=True)
    meta = [{"atom_id": "atom_t_0000", "summary": "s0", "ux": 8, "tags": ["git"]},
            {"atom_id": "atom_t_0001", "summary": "s1", "ux": 8, "tags": ["git"]}]
    store.upsert("alice", feature_tensor=None, mean_tensor=points.mean(0),
                 used_skills=[{"name": "sk1", "use_count": 3}],
                 points=points, point_meta=meta)
    store.upsert("bob", feature_tensor=None, mean_tensor=points.mean(0),
                 used_skills=[{"name": "sk1", "use_count": 1}],
                 points=points[:1], point_meta=meta[:1])
    store.upsert("carol", feature_tensor=None, mean_tensor=-points.mean(0),
                 used_skills=[])
    return store


def _count_profile_loads(monkeypatch) -> list[str]:
    """统计画像库读次数（每个用户每次重算读 load + load_points 各一次）。"""
    loads: list[str] = []
    loads_lock = threading.Lock()
    real_load_points = ProfileStore.load_points

    def counting_load_points(self, user_key):
        with loads_lock:
            loads.append(user_key)
        return real_load_points(self, user_key)

    monkeypatch.setattr(ProfileStore, "load_points", counting_load_points)
    return loads


def test_cluster_graph_repeated_calls_recompute_once(tmp_path, monkeypatch):
    pdb = tmp_path / "p.db"
    _seed_two_similar_users(pdb)
    profile_viz_module._cluster_graph_cache.clear()
    loads = _count_profile_loads(monkeypatch)

    viz = ProfileViz(pdb)
    first = viz.cluster_graph()
    for _ in range(5):
        assert viz.cluster_graph() == first

    assert len(loads) == 3        # 3 个用户各读一次 = 只算了一遍
    assert {node["user"] for node in first["nodes"]} == {"alice", "bob", "carol"}
    assert len(first["edges"]) == 1
    assert {first["edges"][0]["source"], first["edges"][0]["target"]} == {"alice", "bob"}
    assert first["edges"][0]["common_skills"] == ["sk1"]
    assert next(n for n in first["nodes"] if n["user"] == "carol")["isolated"]


def test_cluster_graph_concurrent_requests_share_one_computation(
        tmp_path, monkeypatch):
    """面板并发/缓存到期瞬间不许惊群跑 O(用户²) 相似度。"""
    pdb = tmp_path / "p.db"
    _seed_two_similar_users(pdb)
    profile_viz_module._cluster_graph_cache.clear()
    loads = _count_profile_loads(monkeypatch)

    viz = ProfileViz(pdb)
    barrier = threading.Barrier(16)

    def load():
        barrier.wait()
        return viz.cluster_graph()

    with ThreadPoolExecutor(max_workers=16) as pool:
        results = [future.result(timeout=10)
                   for future in [pool.submit(load) for _ in range(16)]]

    assert len(loads) == 3
    assert all(graph == results[0] for graph in results)


def test_cluster_graph_ttl_expiry_picks_up_new_profiles(tmp_path, monkeypatch):
    pdb = tmp_path / "p.db"
    store = _seed_two_similar_users(pdb)
    profile_viz_module._cluster_graph_cache.clear()
    current_time = [100.0]
    monkeypatch.setattr(
        metrics_module.time, "monotonic", lambda: current_time[0])
    monkeypatch.setattr(
        profile_viz_module._cluster_graph_cache, "ttl_seconds", 5.0)

    viz = ProfileViz(pdb)
    assert len(viz.cluster_graph()["nodes"]) == 3
    store.upsert("dave", feature_tensor=None, mean_tensor=np.ones(4),
                 used_skills=[])
    assert len(viz.cluster_graph()["nodes"]) == 3      # TTL 内仍是旧的

    current_time[0] += 5.1
    assert {node["user"] for node in viz.cluster_graph()["nodes"]} == {
        "alice", "bob", "carol", "dave"}


def test_cluster_graph_isolates_profile_dbs(tmp_path):
    """不同画像库/不同阈值各自成键，不串味。"""
    pdb_a = tmp_path / "a.db"
    pdb_b = tmp_path / "b.db"
    _seed_two_similar_users(pdb_a)
    ProfileStore(pdb_b).upsert("zoe", feature_tensor=None,
                               mean_tensor=np.ones(4), used_skills=[])
    profile_viz_module._cluster_graph_cache.clear()

    assert len(ProfileViz(pdb_a).cluster_graph()["nodes"]) == 3
    assert [n["user"] for n in ProfileViz(pdb_b).cluster_graph()["nodes"]] == ["zoe"]
    # 阈值是缓存键的一部分：调高到 1.0 后一条边都不该连
    assert ProfileViz(pdb_a).cluster_graph(threshold=1.0)["edges"] == []
    assert len(ProfileViz(pdb_a).cluster_graph()["edges"]) == 1


def test_cluster_graph_returns_independent_copies(tmp_path):
    pdb = tmp_path / "p.db"
    _seed_two_similar_users(pdb)
    profile_viz_module._cluster_graph_cache.clear()

    viz = ProfileViz(pdb)
    first = viz.cluster_graph()
    first["nodes"][0]["atoms"] = 999
    first["nodes"][0]["top_tags"].append("intruder")
    first["edges"].clear()
    first["threshold"] = -1

    second = viz.cluster_graph()
    assert second["threshold"] == profile_viz_module.SIMILARITY_THRESHOLD
    assert len(second["edges"]) == 1
    assert all("intruder" not in node["top_tags"] for node in second["nodes"])
    assert 999 not in [node["atoms"] for node in second["nodes"]]
