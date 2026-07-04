"""client_interest.py — §4 用户画像：多兴趣聚类锚点

``ClientInterest`` 维护用户 trajectory atom 摘要 embedding 的聚类（≤5 中心）作为
多兴趣锚点 ``feature_tensor``，及其均值 ``mean_tensor``（供 find_friend）。

聚类用**纯 numpy k-means**（不引入 sklearn/scipy）。中心数 ``k = min(C, max(1, n//3))``，
atom 少时自动降 k，避免强分噪声簇。冷启动（无 atom）→ feature_tensor/mean_tensor 为 None。
"""
from __future__ import annotations

from typing import Optional

import numpy as np


def _kmeans(
    points: np.ndarray,
    k: int,
    *,
    seed: int = 42,
    max_iter: int = 50,
) -> np.ndarray:
    """纯 numpy Lloyd k-means，返回 (k, D) 中心。

    - 确定性：``np.random.default_rng(seed)`` 选初始中心。
    - 空簇：重置到距自身中心最远的点（打破空簇）。
    - 最多 ``max_iter`` 轮；中心不再移动即收敛提前停。
    """
    points = np.asarray(points, dtype=float)
    n = len(points)
    if k < 1:
        raise ValueError(f"k 必须 >= 1，got {k}")
    if n == 0:
        raise ValueError("kmeans: 空 point 集")
    k = min(k, n)
    rng = np.random.default_rng(seed)
    init_idx = rng.choice(n, size=k, replace=False)
    centers = points[init_idx].copy()

    for _ in range(max_iter):
        # 分配：每个点到最近中心
        dists = np.linalg.norm(points[:, None, :] - centers[None, :, :], axis=2)
        labels = np.argmin(dists, axis=1)
        new_centers = centers.copy()
        for j in range(k):
            mask = labels == j
            if not mask.any():
                # 空簇：重置到距中心 j 最远的点
                far = np.argmax(np.linalg.norm(points - centers[j], axis=1))
                new_centers[j] = points[far]
            else:
                new_centers[j] = points[mask].mean(axis=0)
        if np.allclose(new_centers, centers):
            break
        centers = new_centers
    return centers


class ClientInterest:
    """用户多兴趣画像。

    构造两种模式：
    - ``points=`` 给定 atom 摘要 embedding (n, D) → 懒聚类出 ``feature_tensor``。
    - ``feature_tensor=`` 给定预计算中心（从 db 加载）→ 直接透传，不重算。

    冷启动（无 points 且无 feature_tensor）→ ``feature_tensor`` / ``mean_tensor`` 为 None。
    """

    def __init__(
        self,
        user_id: str,
        *,
        points: Optional[np.ndarray] = None,
        feature_tensor: Optional[np.ndarray] = None,
        mean_tensor: Optional[np.ndarray] = None,
        cluster_centers_max: int = 5,
    ):
        self.user_id = user_id
        self._points = None if points is None else np.asarray(points, dtype=float)
        self._feature_tensor = (
            None if feature_tensor is None else np.asarray(feature_tensor, dtype=float)
        )
        self._mean_tensor = (
            None if mean_tensor is None else np.asarray(mean_tensor, dtype=float)
        )
        self._cluster_centers_max = cluster_centers_max

    @property
    def feature_tensor(self) -> Optional[np.ndarray]:
        """≤5 聚类中心 (≤C, D)；冷启动 None。"""
        if self._feature_tensor is None and self._points is not None and len(self._points) > 0:
            n = len(self._points)
            k = min(self._cluster_centers_max, max(1, n // 3))
            self._feature_tensor = _kmeans(self._points, k)
        return self._feature_tensor

    @property
    def mean_tensor(self) -> Optional[np.ndarray]:
        """feature_tensor 各行均值 L2 归一；冷启动 None。"""
        if self._mean_tensor is not None:
            return self._mean_tensor
        ft = self.feature_tensor
        if ft is None:
            return None
        m = ft.mean(axis=0)
        nrm = float(np.linalg.norm(m))
        self._mean_tensor = m / nrm if nrm > 0 else m
        return self._mean_tensor
