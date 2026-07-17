"""PPT 配套的最小 XSkill 算法内核 SDK。

真实实现时，本文件应由 xskill-kernel-sdk 包提供。演示项目把它放在本地，
目的是让算法团队可以直接运行并理解稳定接口，不依赖尚未落地的重构代码。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


@dataclass(frozen=True)
class KernelManifest:
    name: str
    version: str
    api_version: str = "1"
    description: str = ""
    capabilities: tuple[str, ...] = ()


@dataclass(frozen=True)
class TrajectoryRef:
    trajectory_id: str
    path: Path
    user_id: str


@dataclass(frozen=True)
class KernelRunRequest:
    run_id: str
    trajectories: Sequence[TrajectoryRef]
    config_revision: int


@dataclass(frozen=True)
class SkillArtifact:
    name: str
    content: str
    source_trajectory_ids: tuple[str, ...]
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class KernelRunResult:
    artifacts: Sequence[SkillArtifact]
    metrics: Mapping[str, float]
    lineage: Mapping[str, Sequence[str]]


@dataclass(frozen=True)
class KernelServices:
    """平台提供的受控服务；内核不直接访问上传库、用户库和下发库。"""

    read_text: Callable[[Path], str]
    emit_event: Callable[[str, Mapping[str, Any]], None]


class SkillGenerationKernel(ABC):
    """所有技能生产管线必须实现的稳定接口。"""

    @classmethod
    @abstractmethod
    def manifest(cls) -> KernelManifest:
        raise NotImplementedError

    @classmethod
    @abstractmethod
    def config_schema(cls) -> Mapping[str, Any]:
        raise NotImplementedError

    @abstractmethod
    def validate_config(self) -> None:
        """配置不合法时抛 ValueError；平台激活版本前会调用。"""

    @abstractmethod
    def run(
        self,
        request: KernelRunRequest,
        services: KernelServices,
    ) -> KernelRunResult:
        """只做轨迹到 SkillArtifact 的转换，不负责上传、统计或下发。"""

