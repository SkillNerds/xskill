"""冷启动批量 flush 屏障控制器（轨迹堰塞修复）。

问题：默认在线流水线里，每个 cluster batch 一到晋升阈值就触发 SkillEdit；
小数据冷启动场景下 atom 稀少且散落，weightscore 永远到不了阈值 → 没有任何
技能毕业到 main → 交付空壳。即便到了阈值，也是在 atom 池不完整时过早写正文。

修复：冷启动阶段（前 ``epochs`` 个批量 flush 轮次）**hold 住所有增量
SkillEdit**，让本轮导入的全部子轨迹 atom 攒进各 skill 的 ``.candidates.yml``；
外部编排在一轮导入完成后落一个 sentinel 文件（屏障）。watcher 检出屏障后，用
``flush_threshold`` 对每个有候选的 skill 一次性批量写正文——引用本轮累积的全部
子轨迹 atom——把 baby 技能批量毕业到 main。消费屏障后轮次计数 +1；跑满
``epochs`` 即转入正常在线增量 + 灰度路径。

多轮批量进化语义（``epochs`` ≥ 2 时）：``epochs`` 是总批量 flush 轮数。
第 1 轮把 baby 技能毕业到 main；第 2..N 轮的 flush 走 ``cold_flush`` 路径——
main 上的技能跳过 ux_score 守门，基于"现有正文 + 本轮新 candidates 的 atom"
原地重新精炼并直接 commit 回 main（version 逐轮递增），不开 staging / 不走灰度。
这适用于批量冷启动期间还没有真实用户流量、但需要多轮历史轨迹继续完善首版技能库
的场景；跑满 ``epochs`` 后自动回到正常在线增量 + 灰度路径。

设计原则：默认 ``enabled=False`` → 对既有部署零行为变化；非法配置直接抛错
（不做兜底回退）。屏障用文件 sentinel 而非新增 CLI 子命令，外部批量导入编排
在一轮导入结束后 ``touch`` 约定路径即可触发。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


DEFAULT_BARRIER_FILENAME = "COLD_START_FLUSH"


@dataclass
class ColdStartController:
    """持有冷启动批量 flush 状态，回答 watcher 两个问题：当前是否该 hold 增量
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
        - ``epochs``：hold+flush 的冷启动批量轮次数（默认 1）；必须 ≥1。
        - ``barrier_path``：sentinel 绝对路径；缺省
          ``<default_base>/COLD_START_FLUSH``。
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
        barrier_path = (
            Path(bp) if bp else (Path(default_base) / DEFAULT_BARRIER_FILENAME)
        )
        return cls(
            enabled=bool(sec.get("enabled", False)),
            flush_threshold=flush_threshold,
            epochs=epochs,
            barrier_path=barrier_path,
        )

    @property
    def active(self) -> bool:
        """仍处于冷启动阶段：已启用且未跑满预定 flush 轮数。"""
        return self.enabled and self._epochs_done < self.epochs

    def barrier_reached(self) -> bool:
        """sentinel 存在 = 当前冷启动批量导入轮次结束，可批量 flush。"""
        return (
            self.active
            and self.barrier_path is not None
            and self.barrier_path.exists()
        )

    def consume_barrier(self) -> None:
        """消费屏障：删 sentinel + 轮次计数 +1（跑满后 active 自动转 False）。"""
        if self.barrier_path is not None and self.barrier_path.exists():
            self.barrier_path.unlink()
        self._epochs_done += 1
