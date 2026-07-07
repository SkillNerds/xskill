"""test_user_profile.py — §4 ClientInterest (numpy k-means) + ClientUser + ProfileStore

TDD: ≤5 聚类中心、k 上限、冷启动 None、mean_tensor、used_skills/recommended_skills、持久化。
"""
from __future__ import annotations

import subprocess
import sys

import numpy as np

from xskill.recommend.client_interest import (
    ClientInterest,
    _kmeans,
)
from xskill.recommend.client_user import ClientUser
from xskill.recommend.profile_store import ProfileStore


# ── _kmeans ──────────────────────────────────────────────────────

class TestKmeans:
    def test_deterministic_same_input_same_centers(self):
        pts = np.random.default_rng(0).standard_normal((30, 4))
        c1 = _kmeans(pts, k=3, seed=42)
        c2 = _kmeans(pts, k=3, seed=42)
        assert np.allclose(c1, c2)

    def test_returns_k_centers(self):
        pts = np.random.default_rng(1).standard_normal((30, 4))
        c = _kmeans(pts, k=5, seed=42)
        assert c.shape == (5, 4)

    def test_no_heavy_deps_imported(self):
        """聚类模块不引入 sklearn/scipy。"""
        out = subprocess.run(
            [sys.executable, "-c",
             " ".join([
                 "import xskill.recommend.client_interest as m;",
                 "import sys;",
                 "assert 'sklearn' not in sys.modules, 'sklearn leaked';",
                 "assert 'scipy' not in sys.modules, 'scipy leaked'",
             ])],
            capture_output=True, text=True,
        )
        assert out.returncode == 0, out.stderr

    def test_separable_clusters_converge(self):
        """3 个清晰分开的簇 → k=3 中心应各自接近簇心。"""
        rng = np.random.default_rng(2)
        a = rng.standard_normal((20, 2)) + [0, 0]
        b = rng.standard_normal((20, 2)) + [10, 0]
        c = rng.standard_normal((20, 2)) + [0, 10]
        pts = np.vstack([a, b, c])
        centers = _kmeans(pts, k=3, seed=42)
        # 三个中心应分别接近 (0,0),(10,0),(0,10)（顺序无关）
        targets = [(0, 0), (10, 0), (0, 10)]
        for t in targets:
            dists = np.linalg.norm(centers - np.array(t), axis=1)
            assert dists.min() < 1.0, f"无中心接近 {t}: {centers}"


# ── ClientInterest ───────────────────────────────────────────────

class TestClientInterest:
    def test_many_points_5_centers(self):
        pts = np.random.default_rng(0).standard_normal((60, 4))
        ci = ClientInterest("u1", points=pts, cluster_centers_max=5)
        assert ci.feature_tensor.shape == (5, 4)

    def test_few_points_fewer_centers(self):
        pts = np.random.default_rng(0).standard_normal((4, 4))
        ci = ClientInterest("u1", points=pts, cluster_centers_max=5)
        # k = min(5, max(1, 4//3)) = 1
        assert ci.feature_tensor.shape == (1, 4)

    def test_cold_start_no_points(self):
        ci = ClientInterest("u1", points=None)
        assert ci.feature_tensor is None
        assert ci.mean_tensor is None

    def test_mean_tensor_from_centers(self):
        pts = np.random.default_rng(0).standard_normal((60, 4))
        ci = ClientInterest("u1", points=pts)
        m = ci.mean_tensor
        assert m.shape == (4,)
        assert abs(np.linalg.norm(m) - 1.0) < 1e-6  # L2 归一

    def test_precomputed_feature_tensor_passthrough(self):
        """从 db 加载的预计算 tensor 直接透传，不重算。"""
        ft = np.array([[1.0, 0.0], [0.0, 1.0]])
        ci = ClientInterest("u1", feature_tensor=ft)
        assert ci.feature_tensor is ft
        assert np.allclose(ci.mean_tensor, ft.mean(axis=0) / np.linalg.norm(ft.mean(axis=0)))


# ── ClientUser ───────────────────────────────────────────────────

class TestClientUser:
    def test_defaults(self):
        u = ClientUser("u1")
        assert u.user_id == "u1"
        assert u.used_skills == []
        assert u.recommended_skills == []
        assert u.client_interest is None

    def test_used_skills_tracking(self):
        u = ClientUser("u1", used_skills=[
            {"name": "foo", "use_count": 3, "avg_score": 8.0},
        ])
        assert u.used_skills[0]["name"] == "foo"

    def test_recommended_skills_tracking(self):
        u = ClientUser("u1", recommended_skills=[
            {"skill": "bar", "branch": "staging", "hash": "abc123"},
        ])
        assert u.recommended_skills[0]["branch"] == "staging"


# ── ProfileStore 持久化 ──────────────────────────────────────────

class TestProfileStore:
    def test_upsert_and_load(self, tmp_path):
        store = ProfileStore(tmp_path / "profile.db")
        ft = np.array([[1.0, 0.0], [0.0, 1.0]])
        mt = np.array([0.7071, 0.7071])
        used = [{"name": "foo", "use_count": 2, "avg_score": 9.0}]
        store.upsert("u1", feature_tensor=ft, mean_tensor=mt, used_skills=used)
        row = store.load("u1")
        assert row is not None
        assert np.allclose(row["feature_tensor"], ft)
        assert np.allclose(row["mean_tensor"], mt)
        assert row["used_skills"] == used

    def test_load_missing_returns_none(self, tmp_path):
        store = ProfileStore(tmp_path / "profile.db")
        assert store.load("nobody") is None

    def test_upsert_overwrites(self, tmp_path):
        store = ProfileStore(tmp_path / "profile.db")
        store.upsert("u1", feature_tensor=np.array([[1.0]]),
                     mean_tensor=np.array([1.0]), used_skills=[])
        store.upsert("u1", feature_tensor=np.array([[2.0], [3.0]]),
                     mean_tensor=np.array([2.5]), used_skills=[{"name": "x", "use_count": 1, "avg_score": 7.0}])
        row = store.load("u1")
        assert row["feature_tensor"].shape == (2, 1)
        assert row["used_skills"][0]["name"] == "x"

    def test_survives_reopen(self, tmp_path):
        db = tmp_path / "profile.db"
        ProfileStore(db).upsert("u1", feature_tensor=np.array([[1.0]]),
                                mean_tensor=np.array([1.0]), used_skills=[])
        row = ProfileStore(db).load("u1")  # 新实例重开
        assert row is not None
        assert np.allclose(row["feature_tensor"], [[1.0]])
