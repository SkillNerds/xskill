"""xskill — 从 AI Agent 执行轨迹自动蒸馏可复用 Skill。

公开 SDK：
    from xskill import XSkill, Skill, Trajectory
    from xskill.types import (
        WatchDir, SkillHit, TrajectoryHit,
        Candidate, UxScoreResult,
    )

进阶（少数场景，例如单测直接拿子系统）：
    from xskill import Registry, SkillRepo
"""

from __future__ import annotations

# 版本号唯一真源是 setuptools_scm 生成的 _version.py（git tag 派生）。
# 安装包里一定有 _version.py；源码 checkout 没 build 过时 fallback。
try:
    from xskill._version import __version__
except ImportError:  # 未经 build 的源码树
    __version__ = "0.0.0+unknown"

# 顶级公开面：3 个核心类
from xskill.core import XSkill
from xskill.skill.skill import Skill
from xskill.pipeline.trajectory import Trajectory

# 进阶：子系统类（不必常用）
from xskill.pipeline.registry import Registry
from xskill.skill.repo import SkillRepo

__all__ = [
    "__version__",
    "XSkill", "Skill", "Trajectory",
    "Registry", "SkillRepo",
]

