"""Stable, driver-neutral contract for trajectory-to-skill kernels.

Kernels receive capability objects through :class:`KernelContext`.  They do
not receive writable database handles or the skill repository root.  The
Python implementation is still an in-process, trusted-plugin boundary; it is
an API boundary, not a security sandbox.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal, Mapping

if TYPE_CHECKING:
    from xskill.kernels.context import KernelContext


KERNEL_API_VERSION = 2
KernelTrigger = Literal["scheduled", "trajectory_changed", "manual"]
_KERNEL_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def validate_kernel_id(kernel_id: str) -> str:
    """Validate and normalize an ID used in config and filesystem paths."""
    normalized = str(kernel_id or "").strip()
    if not _KERNEL_ID_RE.fullmatch(normalized):
        raise ValueError(
            "kernel id must match [a-z0-9][a-z0-9_-]{0,63}: "
            f"{kernel_id!r}"
        )
    return normalized


@dataclass(frozen=True)
class KernelMetadata:
    """Static information safe to show before a kernel is instantiated."""

    id: str
    name: str
    version: str
    description: str
    triggers: tuple[KernelTrigger, ...] = ("scheduled",)
    api_version: int = KERNEL_API_VERSION

    def __post_init__(self) -> None:
        validate_kernel_id(self.id)
        if not self.name.strip():
            raise ValueError("kernel metadata name must not be empty")
        if not self.version.strip():
            raise ValueError("kernel metadata version must not be empty")
        if self.api_version != KERNEL_API_VERSION:
            raise ValueError(
                f"unsupported kernel API version {self.api_version}; "
                f"expected {KERNEL_API_VERSION}"
            )
        if not self.triggers:
            raise ValueError("kernel metadata must declare at least one trigger")


@dataclass(frozen=True)
class KernelInvocation:
    """Platform-created identity and input scope for one bounded ``run`` call.

    The invocation lives inside :class:`KernelContext`; it is not a second
    provider-facing argument. Most kernels only need ``context.run_id`` and
    the capability objects; input-set and trigger metadata are available when
    the algorithm needs reproducible processing.
    """

    run_id: str
    trigger: KernelTrigger
    dataset_id: str = "live"
    changed_trajectory_ids: tuple[str, ...] = ()
    full_rebuild: bool = False


@dataclass(frozen=True)
class SkillSubmission:
    """A draft bundle handed to the XSkill-owned publication gateway."""

    name: str
    skill_md: str
    files: Mapping[str, str] = field(default_factory=dict)
    source_trajectory_ids: tuple[str, ...] = ()
    message: str = "kernel generated skill"
    # Required when updating an existing skill.  This optimistic concurrency
    # token links the candidate to the exact main version the kernel read.
    base_commit_sha: str | None = None


@dataclass(frozen=True)
class PublishedSkill:
    name: str
    action: Literal["created", "staged"]
    commit_sha: str
    previous_commit_sha: str | None = None


@dataclass(frozen=True)
class KernelRunResult:
    """Operational result recorded by XSkill for one algorithm run."""

    processed_trajectory_ids: tuple[str, ...] = ()
    submitted_skills: tuple[str, ...] = ()
    metrics: Mapping[str, object] = field(default_factory=dict)
    notes: str = ""


class BaseKernel(ABC):
    """Base class implemented by bundled and third-party kernels."""

    metadata: KernelMetadata

    @abstractmethod
    def run(
        self,
        context: "KernelContext",
        run_interval: int = 30,
    ) -> KernelRunResult:
        """Complete one bounded invocation and return auditable counters.

        XSkill reads ``run_interval``'s default to schedule an online external
        kernel. Offline distillation invokes this method exactly once.
        """


# Compatibility alias for integrations written against the initial preview.
KernelManifest = KernelMetadata
