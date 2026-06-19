"""冷启动 epoch 屏障控制器（轨迹堰塞修复）。

问题：默认在线流水线里，每个 cluster batch 一到晋升阈值就触发 SkillEdit；
小数据冷启动场景下 atom 稀少且散落，weightscore 永远到不了阈值 → 没有任何
技能毕业到 main → 交付空壳。即便到了阈值，也是在 atom 池不完整时过早写正文。

修复：冷启动阶段（前 ``epochs`` 个 epoch）**hold 住所有增量 SkillEdit**，让整个
epoch 的全部子轨迹 atom 攒进各 skill 的 ``.candidates.yml``；epoch 训练结束后由
算法落一个 sentinel 文件（屏障）。watcher 检出屏障后，用极低的 ``flush_threshold``
对每个有候选的 skill 一次性批量写正文——引用该 epoch 累积的全部子轨迹 atom——
把 baby 技能批量毕业到 main。消费屏障后 epoch 计数 +1；跑满 ``epochs`` 即转入
正常在线增量 + 灰度路径。

多 epoch 在线进化语义（``epochs`` ≥ 2 时）：``epochs`` 是**总 force-flush 轮数**，
每个训练 epoch 末落一次屏障 → flush 一次。第 1 个 epoch 把 baby 技能毕业到 main；
第 2..N 个 epoch 的 flush 走 ``cold_flush`` 路径——main 上的技能跳过 ux_score 守门，
基于"现有正文 + 该 epoch 新 candidates 的 atom"**原地重新精炼并直接 commit 回
main**（version 逐 epoch 递增），不开 staging / 不走灰度。训练容器没有真实用户
反馈，灰度无从决策，故 cold_flush 让技能在 main 上逐 epoch 原地进化，跑满
``epochs`` 后 collect 即可拿到进化后的 main 技能。

设计原则：默认 ``enabled=False`` → 对既有部署零行为变化；非法配置直接抛错
（不做兜底回退）。屏障用文件 sentinel 而非新增 CLI 子命令，算法在 epoch 训练
结束后 ``touch`` 约定路径即可触发。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass
class ColdStartController:
    """持有冷启动 epoch 状态，回答 watcher 两个问题：当前是否该 hold 增量
    SkillEdit、屏障是否到达可以批量 flush。"""

    enabled: bool = False
    flush_threshold: int = 1
    epochs: int = 1
    barrier_path: Path | None = None
    _epochs_done: int = 0

    @classmethod
    def from_config(cls, config: dict | None, default_base: Path) -> "ColdStartController":
        """从 ``config['cold_start']`` 段构造。字段缺省即关闭冷启动。

        - ``enabled``：是否启用冷启动屏障（默认 False）。
        - ``flush_threshold``：屏障 flush 时的 weightscore 门槛（默认 1，即任何
          有候选的 skill 都批量毕业）；必须 ≥1。
        - ``epochs``：hold+flush 的冷启动 epoch 数（默认 1）；必须 ≥1。
        - ``barrier_path``：sentinel 绝对路径；缺省 ``<default_base>/EPOCH_FLUSH``。
        """
        sec = (config or {}).get("cold_start", {}) or {}
        flush_threshold = int(sec.get("flush_threshold", 1))
        if flush_threshold < 1:
            raise ValueError(
                f"cold_start.flush_threshold 必须 ≥1，得到 {flush_threshold}")
        epochs = int(sec.get("epochs", 1))
        if epochs < 1:
            raise ValueError(f"cold_start.epochs 必须 ≥1，得到 {epochs}")
        bp = sec.get("barrier_path")
        barrier_path = Path(bp) if bp else (Path(default_base) / "EPOCH_FLUSH")
        return cls(
            enabled=bool(sec.get("enabled", False)),
            flush_threshold=flush_threshold,
            epochs=epochs,
            barrier_path=barrier_path,
        )

    @property
    def active(self) -> bool:
        """仍处于冷启动阶段：已启用且未跑满预定 epoch 数。"""
        return self.enabled and self._epochs_done < self.epochs

    def barrier_reached(self) -> bool:
        """sentinel 存在 = 当前 epoch 训练结束，可批量 flush。"""
        return (
            self.active
            and self.barrier_path is not None
            and self.barrier_path.exists()
        )

    def consume_barrier(self) -> None:
        """消费屏障：删 sentinel + epoch 计数 +1（跑满后 active 自动转 False）。"""
        if self.barrier_path is not None and self.barrier_path.exists():
            self.barrier_path.unlink()
        self._epochs_done += 1
