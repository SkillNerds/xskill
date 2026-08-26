"""Typed Logical Task graph records and production invariants.

The graph deliberately does not extend :class:`AtomTask`.  Atom-to-Skill routing
and Atom-to-Task membership are independent relations with different keys and
different cardinality rules.
"""
from __future__ import annotations

import math
from collections import deque
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from typing import Any

SCHEMA_VERSION = 1
MEMBERSHIP_ROLES = frozenset(("primary", "context"))
DECISIONS = frozenset(("proposed", "confirmed", "rejected", "needs_review"))
TASK_LIFECYCLES = frozenset(("open", "blocked", "closed"))
TASK_OUTCOMES = frozenset((
    "succeeded", "partially_succeeded", "failed", "cancelled",
    "abandoned", "unknown",
))
ATTEMPT_LIFECYCLES = frozenset(("running", "finished"))
ATTEMPT_OUTCOMES = frozenset((
    "succeeded", "partially_succeeded", "failed", "blocked", "cancelled",
    "unknown",
))
VERIFICATIONS = frozenset((
    "unverified", "verified", "contradicted", "conflicted", "not_applicable",
))
USER_DISPOSITIONS = frozenset((
    "accepted", "rejected", "corrected", "cancelled", "unknown",
))
TASK_RELATION_TYPES = frozenset(("parent", "subtask", "depends_on", "follows_up"))
ATTEMPT_RELATION_TYPES = frozenset((
    "continuation_of", "retry_of", "correction_of", "supersedes",
))
USAGE_PLANES = frozenset(("execution", "xskill_processing"))
MEASUREMENT_QUALITIES = frozenset(("measured", "estimated", "unavailable"))
ALLOCATION_MODES = frozenset(("direct", "shared", "unattributed"))
DECISION_VALUES = {
    "lifecycle": TASK_LIFECYCLES | ATTEMPT_LIFECYCLES,
    "outcome": TASK_OUTCOMES | ATTEMPT_OUTCOMES,
    "verification": VERIFICATIONS,
    "user_disposition": USER_DISPOSITIONS,
}


def _required(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    if value != value.strip():
        raise ValueError(f"{name} must not contain surrounding whitespace")
    return value.strip()


def _choice(value: str, choices: frozenset[str], name: str) -> str:
    if value not in choices:
        raise ValueError(f"{name} must be one of {sorted(choices)!r}, got {value!r}")
    return value


def _confidence(value: float | None, name: str = "confidence") -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be numeric")
    result = float(value)
    if not math.isfinite(result) or result < 0 or result > 1:
        raise ValueError(f"{name} must be within [0, 1]")
    return result


@dataclass(frozen=True)
class SessionRef:
    tenant_id: str
    task_scope_id: str
    source_scope_id: str
    traj_id: str

    def __post_init__(self) -> None:
        for field_name in ("tenant_id", "task_scope_id", "source_scope_id", "traj_id"):
            _required(getattr(self, field_name), field_name)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict) -> SessionRef:
        return cls(**value)


@dataclass(frozen=True)
class AtomRef:
    tenant_id: str
    task_scope_id: str
    source_scope_id: str
    traj_id: str
    atom_id: str

    def __post_init__(self) -> None:
        for field_name in (
            "tenant_id", "task_scope_id", "source_scope_id", "traj_id", "atom_id",
        ):
            _required(getattr(self, field_name), field_name)

    @property
    def session_ref(self) -> SessionRef:
        return SessionRef(
            tenant_id=self.tenant_id,
            task_scope_id=self.task_scope_id,
            source_scope_id=self.source_scope_id,
            traj_id=self.traj_id,
        )

    @property
    def key(self) -> tuple[str, str, str, str, str]:
        return (
            self.tenant_id, self.task_scope_id, self.source_scope_id,
            self.traj_id, self.atom_id,
        )

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict) -> AtomRef:
        return cls(**value)


@dataclass(frozen=True)
class EvidenceRange:
    evidence_id: str
    session_ref: SessionRef
    locator_kind: str
    start: int | str
    end: int | str
    content_hash: str
    atom_hash: str = ""
    atom_ref: AtomRef | None = None
    stale: bool = False
    model: dict = field(default_factory=dict)
    harness: dict = field(default_factory=dict)
    skills: tuple[dict, ...] = ()

    def __post_init__(self) -> None:
        _required(self.evidence_id, "evidence_id")
        _required(self.locator_kind, "locator_kind")
        _required(self.content_hash, "content_hash")
        if isinstance(self.start, bool) or isinstance(self.end, bool):
            raise ValueError("EvidenceRange locators cannot be booleans")
        if not isinstance(self.start, (int, str)) or not isinstance(
            self.end, (int, str)
        ):
            raise ValueError("EvidenceRange locators must be integers or strings")
        if type(self.start) is not type(self.end):
            raise ValueError("EvidenceRange start and end locator types must match")
        if isinstance(self.start, str):
            _required(self.start, "evidence.start")
            _required(self.end, "evidence.end")
        if isinstance(self.start, int) and isinstance(self.end, int) and self.end <= self.start:
            raise ValueError("EvidenceRange uses half-open [start, end) with end > start")
        if self.atom_ref is not None and self.atom_ref.session_ref != self.session_ref:
            raise ValueError("atom_ref and session_ref scopes must match")
        if not isinstance(self.model, dict) or not isinstance(self.harness, dict):
            raise ValueError("EvidenceRange model and harness must be objects")
        if not all(isinstance(skill, dict) for skill in self.skills):
            raise ValueError("EvidenceRange skills must contain objects")

    def to_dict(self) -> dict:
        result = asdict(self)
        result["session_ref"] = self.session_ref.to_dict()
        result["atom_ref"] = self.atom_ref.to_dict() if self.atom_ref else None
        result["skills"] = list(self.skills)
        return result

    @classmethod
    def from_dict(cls, value: dict) -> EvidenceRange:
        data = dict(value)
        data["session_ref"] = SessionRef.from_dict(data["session_ref"])
        if data.get("atom_ref"):
            data["atom_ref"] = AtomRef.from_dict(data["atom_ref"])
        data["skills"] = tuple(data.get("skills") or ())
        return cls(**data)


@dataclass(frozen=True)
class DecisionRecord:
    decision_id: str
    dimension: str
    value: str
    confidence: float | None
    decision: str
    decided_by: str
    algorithm_version: str
    evidence_refs: tuple[str, ...]
    observed_at: str
    stale: bool = False

    def __post_init__(self) -> None:
        _required(self.decision_id, "decision_id")
        _required(self.dimension, "dimension")
        _required(self.value, "value")
        if self.dimension not in DECISION_VALUES:
            raise ValueError(f"unsupported decision dimension: {self.dimension!r}")
        if self.value not in DECISION_VALUES[self.dimension]:
            raise ValueError(
                f"invalid {self.dimension} decision value: {self.value!r}"
            )
        _confidence(self.confidence)
        _choice(self.decision, DECISIONS, "decision")
        _required(self.decided_by, "decided_by")
        _required(self.algorithm_version, "algorithm_version")
        _required(self.observed_at, "observed_at")
        if not all(isinstance(item, str) and item for item in self.evidence_refs):
            raise ValueError("decision evidence_refs must contain non-empty strings")

    def to_dict(self) -> dict:
        result = asdict(self)
        result["evidence_refs"] = list(self.evidence_refs)
        return result

    @classmethod
    def from_dict(cls, value: dict) -> DecisionRecord:
        data = dict(value)
        data["evidence_refs"] = tuple(data.get("evidence_refs") or ())
        return cls(**data)


@dataclass(frozen=True)
class LogicalTask:
    task_id: str
    title: str
    summary: str
    created_at: str
    lifecycle: str = "open"
    outcome: str = "unknown"
    verification: str = "unverified"
    user_disposition: str = "unknown"
    decisions: tuple[DecisionRecord, ...] = ()
    aliases: tuple[str, ...] = ()
    tombstoned: bool = False

    def __post_init__(self) -> None:
        _required(self.task_id, "task_id")
        _required(self.created_at, "created_at")
        _choice(self.lifecycle, TASK_LIFECYCLES, "task.lifecycle")
        _choice(self.outcome, TASK_OUTCOMES, "task.outcome")
        _choice(self.verification, VERIFICATIONS, "task.verification")
        _choice(self.user_disposition, USER_DISPOSITIONS, "task.user_disposition")
        if not isinstance(self.title, str) or not isinstance(self.summary, str):
            raise ValueError("Task title and summary must be strings")
        if not isinstance(self.tombstoned, bool):
            raise ValueError("Task tombstoned must be a boolean")
        if not all(isinstance(alias, str) and alias for alias in self.aliases):
            raise ValueError("Task aliases must contain non-empty strings")
        if len(set(self.aliases)) != len(self.aliases):
            raise ValueError("Task aliases must be unique")
        if self.lifecycle in {"open", "blocked"} and self.outcome != "unknown":
            raise ValueError("open or blocked Task outcome must be unknown")
        if self.lifecycle == "closed" and self.outcome == "unknown":
            raise ValueError("closed Task requires a terminal outcome")
        for decision in self.decisions:
            if decision.dimension == "lifecycle" and decision.value not in TASK_LIFECYCLES:
                raise ValueError("Task lifecycle decision uses an Attempt value")
            if decision.dimension == "outcome" and decision.value not in TASK_OUTCOMES:
                raise ValueError("Task outcome decision uses an Attempt value")

    def to_dict(self) -> dict:
        result = asdict(self)
        result["decisions"] = [decision.to_dict() for decision in self.decisions]
        result["aliases"] = list(self.aliases)
        return result

    @classmethod
    def from_dict(cls, value: dict) -> LogicalTask:
        data = dict(value)
        data["decisions"] = tuple(
            DecisionRecord.from_dict(item) for item in data.get("decisions") or ()
        )
        data["aliases"] = tuple(data.get("aliases") or ())
        return cls(**data)


@dataclass(frozen=True)
class TaskAtomMembership:
    membership_id: str
    task_id: str
    atom_ref: AtomRef
    role: str
    confidence: float | None
    decision: str
    decided_by: str
    algorithm_version: str
    evidence_refs: tuple[str, ...]
    observed_at: str
    stale: bool = False

    def __post_init__(self) -> None:
        _required(self.membership_id, "membership_id")
        _required(self.task_id, "task_id")
        _choice(self.role, MEMBERSHIP_ROLES, "membership.role")
        _confidence(self.confidence)
        _choice(self.decision, DECISIONS, "membership.decision")
        _required(self.decided_by, "membership.decided_by")
        _required(self.algorithm_version, "membership.algorithm_version")
        _required(self.observed_at, "membership.observed_at")
        if not isinstance(self.stale, bool):
            raise ValueError("membership.stale must be a boolean")
        if not all(
            isinstance(item, str) and item for item in self.evidence_refs
        ):
            raise ValueError("membership evidence_refs must contain strings")

    def to_dict(self) -> dict:
        result = asdict(self)
        result["atom_ref"] = self.atom_ref.to_dict()
        result["evidence_refs"] = list(self.evidence_refs)
        return result

    @classmethod
    def from_dict(cls, value: dict) -> TaskAtomMembership:
        data = dict(value)
        data["atom_ref"] = AtomRef.from_dict(data["atom_ref"])
        data["evidence_refs"] = tuple(data.get("evidence_refs") or ())
        return cls(**data)


@dataclass(frozen=True)
class TaskRelation:
    relation_id: str
    from_task_id: str
    to_task_id: str
    relation_type: str
    confidence: float | None
    decision: str
    decided_by: str
    algorithm_version: str
    evidence_refs: tuple[str, ...]
    observed_at: str
    stale: bool = False

    def __post_init__(self) -> None:
        _required(self.relation_id, "relation_id")
        _required(self.from_task_id, "from_task_id")
        _required(self.to_task_id, "to_task_id")
        if self.from_task_id == self.to_task_id:
            raise ValueError("task relation cannot point to itself")
        _choice(self.relation_type, TASK_RELATION_TYPES, "task_relation.relation_type")
        _confidence(self.confidence)
        _choice(self.decision, DECISIONS, "task_relation.decision")
        _required(self.decided_by, "task_relation.decided_by")
        _required(self.algorithm_version, "task_relation.algorithm_version")
        _required(self.observed_at, "task_relation.observed_at")
        if not isinstance(self.stale, bool):
            raise ValueError("task relation stale must be a boolean")
        if not all(
            isinstance(item, str) and item for item in self.evidence_refs
        ):
            raise ValueError("Task relation evidence_refs must contain strings")

    def to_dict(self) -> dict:
        result = asdict(self)
        result["evidence_refs"] = list(self.evidence_refs)
        return result

    @classmethod
    def from_dict(cls, value: dict) -> TaskRelation:
        data = dict(value)
        data["evidence_refs"] = tuple(data.get("evidence_refs") or ())
        return cls(**data)


@dataclass(frozen=True)
class AttemptRelation:
    relation_id: str
    from_attempt_id: str
    to_attempt_id: str
    relation_type: str
    confidence: float | None
    decision: str
    decided_by: str
    algorithm_version: str
    evidence_refs: tuple[str, ...]
    observed_at: str

    def __post_init__(self) -> None:
        _required(self.relation_id, "relation_id")
        _required(self.from_attempt_id, "from_attempt_id")
        _required(self.to_attempt_id, "to_attempt_id")
        if self.from_attempt_id == self.to_attempt_id:
            raise ValueError("attempt relation cannot point to itself")
        _choice(self.relation_type, ATTEMPT_RELATION_TYPES, "attempt_relation.relation_type")
        _confidence(self.confidence)
        _choice(self.decision, DECISIONS, "attempt_relation.decision")
        _required(self.decided_by, "attempt_relation.decided_by")
        _required(self.algorithm_version, "attempt_relation.algorithm_version")
        _required(self.observed_at, "attempt_relation.observed_at")
        if not all(
            isinstance(item, str) and item for item in self.evidence_refs
        ):
            raise ValueError("Attempt relation evidence_refs must contain strings")

    def to_dict(self) -> dict:
        result = asdict(self)
        result["evidence_refs"] = list(self.evidence_refs)
        return result

    @classmethod
    def from_dict(cls, value: dict) -> AttemptRelation:
        data = dict(value)
        data["evidence_refs"] = tuple(data.get("evidence_refs") or ())
        return cls(**data)


@dataclass(frozen=True)
class TaskAttempt:
    attempt_id: str
    task_id: str
    started_at: str
    ended_at: str | None
    lifecycle: str
    outcome: str
    verification: str
    user_disposition: str
    evidence_ranges: tuple[EvidenceRange, ...]
    decisions: tuple[DecisionRecord, ...] = ()
    execution_identity: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        _required(self.attempt_id, "attempt_id")
        _required(self.task_id, "attempt.task_id")
        _required(self.started_at, "attempt.started_at")
        _choice(self.lifecycle, ATTEMPT_LIFECYCLES, "attempt.lifecycle")
        _choice(self.outcome, ATTEMPT_OUTCOMES, "attempt.outcome")
        _choice(self.verification, VERIFICATIONS, "attempt.verification")
        _choice(self.user_disposition, USER_DISPOSITIONS, "attempt.user_disposition")
        if not self.evidence_ranges:
            raise ValueError("TaskAttempt must contain at least one EvidenceRange")
        if self.lifecycle == "running" and self.ended_at is not None:
            raise ValueError("running TaskAttempt cannot have ended_at")
        if self.lifecycle == "running" and self.outcome != "unknown":
            raise ValueError("running TaskAttempt outcome must be unknown")
        if self.lifecycle == "finished" and not self.ended_at:
            raise ValueError("finished TaskAttempt requires ended_at")
        if not isinstance(self.execution_identity, dict):
            raise ValueError("TaskAttempt execution_identity must be an object")
        for decision in self.decisions:
            if (
                decision.dimension == "lifecycle"
                and decision.value not in ATTEMPT_LIFECYCLES
            ):
                raise ValueError("Attempt lifecycle decision uses a Task value")
            if (
                decision.dimension == "outcome"
                and decision.value not in ATTEMPT_OUTCOMES
            ):
                raise ValueError("Attempt outcome decision uses a Task value")

    def to_dict(self) -> dict:
        result = asdict(self)
        result["evidence_ranges"] = [item.to_dict() for item in self.evidence_ranges]
        result["decisions"] = [item.to_dict() for item in self.decisions]
        return result

    @classmethod
    def from_dict(cls, value: dict) -> TaskAttempt:
        data = dict(value)
        data["evidence_ranges"] = tuple(
            EvidenceRange.from_dict(item) for item in data.get("evidence_ranges") or ()
        )
        data["decisions"] = tuple(
            DecisionRecord.from_dict(item) for item in data.get("decisions") or ()
        )
        return cls(**data)


@dataclass(frozen=True)
class UsageAllocation:
    allocation_id: str
    usage_event_id: str
    usage_plane: str
    allocation_mode: str
    fraction: float
    task_id: str | None = None
    attempt_id: str | None = None
    processing_step: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    cache_read_tokens: int | None = None
    cost_usd: float | None = None
    method: str = ""
    method_version: str = ""

    def __post_init__(self) -> None:
        _required(self.allocation_id, "allocation_id")
        _required(self.usage_event_id, "usage_event_id")
        _choice(self.usage_plane, USAGE_PLANES, "usage_plane")
        _choice(self.allocation_mode, ALLOCATION_MODES, "allocation_mode")
        _confidence(self.fraction, "allocation.fraction")
        if self.fraction <= 0:
            raise ValueError("allocation.fraction must be greater than zero")
        if self.allocation_mode == "unattributed" and any((self.task_id, self.attempt_id)):
            raise ValueError("unattributed usage cannot target a Task or Attempt")
        if self.attempt_id and not self.task_id:
            raise ValueError("attempt allocation must also carry task_id")
        for field_name in (
            "prompt_tokens", "completion_tokens", "total_tokens",
            "cache_read_tokens",
        ):
            value = getattr(self, field_name)
            if value is not None and (
                isinstance(value, bool)
                or not isinstance(value, int)
                or value < 0
            ):
                raise ValueError(f"{field_name} must be a non-negative integer or None")
        if self.cost_usd is not None and (
            isinstance(self.cost_usd, bool)
            or not isinstance(self.cost_usd, (int, float))
            or not math.isfinite(float(self.cost_usd))
            or self.cost_usd < 0
        ):
            raise ValueError("cost_usd must be a non-negative number or None")

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict) -> UsageAllocation:
        return cls(**value)


@dataclass(frozen=True)
class TaskGraphGeneration:
    generation_id: str
    tenant_id: str
    task_scope_id: str
    source_revision: str
    generator: dict
    base_override_seq: int
    created_at: str
    tasks: tuple[LogicalTask, ...]
    memberships: tuple[TaskAtomMembership, ...]
    relations: tuple[TaskRelation, ...]
    attempts: tuple[TaskAttempt, ...]
    attempt_relations: tuple[AttemptRelation, ...]
    usage_allocations: tuple[UsageAllocation, ...]
    metrics: dict = field(default_factory=dict)
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        _required(self.generation_id, "generation_id")
        _required(self.tenant_id, "tenant_id")
        _required(self.task_scope_id, "task_scope_id")
        _required(self.source_revision, "source_revision")
        _required(self.created_at, "created_at")
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError(f"unsupported Task Graph schema_version={self.schema_version}")
        if (
            isinstance(self.base_override_seq, bool)
            or not isinstance(self.base_override_seq, int)
            or self.base_override_seq < 0
        ):
            raise ValueError("base_override_seq must be a non-negative integer")
        if not isinstance(self.generator, dict) or not isinstance(self.metrics, dict):
            raise ValueError("Task Graph generator and metrics must be objects")
        validate_generation(self)

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "generation_id": self.generation_id,
            "tenant_id": self.tenant_id,
            "task_scope_id": self.task_scope_id,
            "source_revision": self.source_revision,
            "generator": self.generator,
            "base_override_seq": self.base_override_seq,
            "created_at": self.created_at,
            "tasks": [item.to_dict() for item in self.tasks],
            "memberships": [item.to_dict() for item in self.memberships],
            "relations": [item.to_dict() for item in self.relations],
            "attempts": [item.to_dict() for item in self.attempts],
            "attempt_relations": [item.to_dict() for item in self.attempt_relations],
            "usage_allocations": [item.to_dict() for item in self.usage_allocations],
            "metrics": self.metrics,
        }

    @classmethod
    def from_dict(cls, value: dict) -> TaskGraphGeneration:
        return cls(
            schema_version=value.get("schema_version", 0),
            generation_id=value["generation_id"],
            tenant_id=value["tenant_id"],
            task_scope_id=value["task_scope_id"],
            source_revision=value["source_revision"],
            generator=dict(value.get("generator") or {}),
            base_override_seq=value.get("base_override_seq", 0),
            created_at=value["created_at"],
            tasks=tuple(LogicalTask.from_dict(item) for item in value.get("tasks") or ()),
            memberships=tuple(
                TaskAtomMembership.from_dict(item) for item in value.get("memberships") or ()
            ),
            relations=tuple(TaskRelation.from_dict(item) for item in value.get("relations") or ()),
            attempts=tuple(TaskAttempt.from_dict(item) for item in value.get("attempts") or ()),
            attempt_relations=tuple(
                AttemptRelation.from_dict(item) for item in value.get("attempt_relations") or ()
            ),
            usage_allocations=tuple(
                UsageAllocation.from_dict(item) for item in value.get("usage_allocations") or ()
            ),
            metrics=dict(value.get("metrics") or {}),
        )


def _task_relation_edge(relation: TaskRelation) -> tuple[str, str]:
    if relation.relation_type == "subtask":
        return relation.to_task_id, relation.from_task_id
    return relation.from_task_id, relation.to_task_id


def _assert_acyclic(task_ids: set[str], relations: Iterable[TaskRelation]) -> None:
    graph = {task_id: [] for task_id in task_ids}
    indegree = {task_id: 0 for task_id in task_ids}
    for relation in relations:
        if relation.decision != "confirmed" or relation.stale:
            continue
        from_task_id, to_task_id = _task_relation_edge(relation)
        graph[from_task_id].append(to_task_id)
        indegree[to_task_id] += 1
    ready = deque(
        task_id for task_id, degree in indegree.items() if degree == 0
    )
    visited = 0
    while ready:
        task_id = ready.popleft()
        visited += 1
        for target_id in graph[task_id]:
            indegree[target_id] -= 1
            if indegree[target_id] == 0:
                ready.append(target_id)
    if visited != len(task_ids):
        raise ValueError("confirmed Task relations must form a DAG")


def validate_generation(generation: TaskGraphGeneration) -> None:
    """Validate the ADR invariants that are decidable from one generation."""
    task_ids = {task.task_id for task in generation.tasks}
    if len(task_ids) != len(generation.tasks):
        raise ValueError("task_id must be unique within a TaskScope generation")
    attempt_ids = {attempt.attempt_id for attempt in generation.attempts}
    if len(attempt_ids) != len(generation.attempts):
        raise ValueError("attempt_id must be unique within a TaskScope generation")

    def require_unique(records: Iterable[Any], attribute: str) -> None:
        values = [getattr(record, attribute) for record in records]
        if len(set(values)) != len(values):
            raise ValueError(
                f"{attribute} must be unique within a TaskScope generation"
            )

    require_unique(generation.memberships, "membership_id")
    require_unique(generation.relations, "relation_id")
    require_unique(generation.attempt_relations, "relation_id")
    require_unique(generation.usage_allocations, "allocation_id")

    confirmed_primary: dict[tuple[str, str, str, str, str], str] = {}
    primary_counts = {task_id: 0 for task_id in task_ids}
    evidence_ids: set[str] = set()
    for membership in generation.memberships:
        if membership.task_id not in task_ids:
            raise ValueError("membership references a missing Task")
        atom_ref = membership.atom_ref
        if atom_ref.tenant_id != generation.tenant_id or atom_ref.task_scope_id != generation.task_scope_id:
            raise ValueError("membership cannot cross TenantScope or TaskScope")
        if membership.role == "primary" and membership.decision == "confirmed" and not membership.stale:
            if atom_ref.key in confirmed_primary:
                raise ValueError(
                    "an Atom may have at most one confirmed primary membership"
                )
            confirmed_primary[atom_ref.key] = membership.task_id
            primary_counts[membership.task_id] += 1

    parent_targets: dict[str, str] = {}
    for relation in generation.relations:
        if relation.from_task_id not in task_ids or relation.to_task_id not in task_ids:
            raise ValueError("Task relation references a missing Task")
        if (
            relation.decision == "confirmed"
            and not relation.stale
            and relation.relation_type in ("parent", "subtask")
        ):
            child_id = (
                relation.to_task_id if relation.relation_type == "parent"
                else relation.from_task_id
            )
            parent_id = (
                relation.from_task_id if relation.relation_type == "parent"
                else relation.to_task_id
            )
            previous = parent_targets.setdefault(child_id, parent_id)
            if previous != parent_id:
                raise ValueError("a Task may have at most one confirmed primary parent")
    _assert_acyclic(task_ids, generation.relations)

    attempts_by_task: dict[str, set[str]] = {task_id: set() for task_id in task_ids}
    attempt_task_by_id: dict[str, str] = {}
    live_line_ranges: dict[
        tuple[str, str], list[tuple[int, int, str]]
    ] = {}
    for attempt in generation.attempts:
        if attempt.task_id not in task_ids:
            raise ValueError("Attempt references a missing Task")
        attempts_by_task[attempt.task_id].add(attempt.attempt_id)
        attempt_task_by_id[attempt.attempt_id] = attempt.task_id
        for evidence in attempt.evidence_ranges:
            if evidence.evidence_id in evidence_ids:
                raise ValueError("evidence_id must be unique within a generation")
            evidence_ids.add(evidence.evidence_id)
            if (
                evidence.session_ref.tenant_id != generation.tenant_id
                or evidence.session_ref.task_scope_id != generation.task_scope_id
            ):
                raise ValueError("Attempt evidence cannot cross TenantScope or TaskScope")
            if evidence.atom_ref is not None and not evidence.stale:
                owner = confirmed_primary.get(evidence.atom_ref.key)
                if owner is not None and owner != attempt.task_id:
                    raise ValueError("Attempt evidence primary Atom belongs to another Task")
            if (
                not evidence.stale
                and evidence.locator_kind == "trajectory_line"
                and isinstance(evidence.start, int)
                and isinstance(evidence.end, int)
            ):
                session_key = (
                    evidence.session_ref.source_scope_id,
                    evidence.session_ref.traj_id,
                )
                live_line_ranges.setdefault(session_key, []).append((
                    evidence.start,
                    evidence.end,
                    evidence.evidence_id,
                ))

    for ranges in live_line_ranges.values():
        ranges.sort()
        for previous, current in zip(ranges, ranges[1:]):
            if current[0] < previous[1]:
                raise ValueError(
                    "live Attempt EvidenceRanges cannot overlap within a Session"
                )

    for relation in generation.attempt_relations:
        if relation.from_attempt_id not in attempt_ids or relation.to_attempt_id not in attempt_ids:
            raise ValueError("Attempt relation references a missing Attempt")
        from_task = attempt_task_by_id[relation.from_attempt_id]
        to_task = attempt_task_by_id[relation.to_attempt_id]
        if from_task != to_task:
            raise ValueError("Attempt relations cannot cross Logical Tasks")

    attempt_edges = {attempt_id: [] for attempt_id in attempt_ids}
    attempt_indegree = {attempt_id: 0 for attempt_id in attempt_ids}
    for relation in generation.attempt_relations:
        if relation.decision != "confirmed":
            continue
        attempt_edges[relation.from_attempt_id].append(relation.to_attempt_id)
        attempt_indegree[relation.to_attempt_id] += 1
    attempt_ready = deque(
        attempt_id
        for attempt_id, degree in attempt_indegree.items()
        if degree == 0
    )
    attempt_visited = 0
    while attempt_ready:
        attempt_id = attempt_ready.popleft()
        attempt_visited += 1
        for target_id in attempt_edges[attempt_id]:
            attempt_indegree[target_id] -= 1
            if attempt_indegree[target_id] == 0:
                attempt_ready.append(target_id)
    if attempt_visited != len(attempt_ids):
        raise ValueError("confirmed Attempt relations must form a DAG")

    allocation_sums: dict[tuple[str, str], float] = {}
    for allocation in generation.usage_allocations:
        if allocation.task_id is not None and allocation.task_id not in task_ids:
            raise ValueError("usage allocation references a missing Task")
        if allocation.attempt_id is not None:
            if allocation.attempt_id not in attempts_by_task.get(allocation.task_id or "", set()):
                raise ValueError("usage allocation Attempt does not belong to Task")
        key = (allocation.usage_plane, allocation.usage_event_id)
        allocation_sums[key] = allocation_sums.get(key, 0.0) + allocation.fraction
    for key, total in allocation_sums.items():
        if abs(total - 1.0) > 1e-9:
            raise ValueError(
                f"usage allocation must conserve event {key!r}: fraction={total}"
            )

    task_by_id = {task.task_id: task for task in generation.tasks}
    for task_id, task in task_by_id.items():
        should_be_tombstoned = primary_counts[task_id] == 0
        if task.tombstoned != should_be_tombstoned:
            raise ValueError(
                "Task tombstone state must match live confirmed primary ownership"
            )


def stable_ref_key(atom_ref: AtomRef) -> str:
    """Canonical scoped Atom key used in JSON maps without path semantics."""
    return "\x1f".join(atom_ref.key)


def model_from_dict(value: dict[str, Any]) -> dict[str, Any]:
    """Drop empty execution-version fields while preserving unavailable reasons."""
    return {key: item for key, item in value.items() if item not in (None, "", [], {})}
