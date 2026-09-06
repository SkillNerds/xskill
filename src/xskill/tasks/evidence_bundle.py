"""Versioned, bounded Task-grounded evidence for Skill learning.

The bundle is a read contract over one immutable :class:`TaskGraphGeneration`.
It deliberately contains references and provenance rather than trajectory text.
Production routing and SkillEdit integration live outside this module.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from typing import Any, TypeVar

from xskill.tasks.models import (
    AttemptRelation,
    LogicalTask,
    TaskAtomMembership,
    TaskAttempt,
    TaskGraphGeneration,
    TaskRelation,
    UsageAllocation,
)

BUNDLE_SCHEMA_VERSION = 1
ELIGIBILITY_POLICY_VERSION = "task-outcome-v1"
LEARNING_ELIGIBILITIES = frozenset(("eligible", "ineligible", "needs_review"))
MAX_TASK_EVIDENCE_BUNDLE_BYTES = 1024 * 1024
_T = TypeVar("_T")


class TaskEvidenceBundleError(ValueError):
    """The current Task fact set cannot produce safe learning evidence."""


def _ensure(condition: bool, message: str) -> None:
    if not condition:
        raise TaskEvidenceBundleError(message)


@dataclass(frozen=True)
class TaskEvidenceLimits:
    memberships: int = 256
    review_memberships: int = 256
    task_relations: int = 256
    attempts: int = 128
    attempt_relations: int = 256
    evidence_ranges: int = 512
    usage_allocations: int = 512
    serialized_bytes: int = MAX_TASK_EVIDENCE_BUNDLE_BYTES

    def __post_init__(self) -> None:
        for field_name, value in vars(self).items():
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{field_name} limit must be a positive integer")
        if self.serialized_bytes > MAX_TASK_EVIDENCE_BUNDLE_BYTES:
            raise ValueError(
                "serialized_bytes cannot exceed the Task evidence hard bound"
            )


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _fingerprint(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical_json(value)).hexdigest()


def _ordered(values: Iterable[_T], key: Callable[[_T], Any]) -> tuple[_T, ...]:
    return tuple(sorted(values, key=key))


def _bounded_ordered(
    values: Iterable[_T],
    *,
    key: Callable[[_T], Any],
    maximum: int,
    field_name: str,
) -> tuple[_T, ...]:
    bounded: list[_T] = []
    for value in values:
        bounded.append(value)
        if len(bounded) > maximum:
            raise TaskEvidenceBundleError(
                f"Task evidence {field_name} exceeds bound: {len(bounded)}>{maximum}"
            )
    return tuple(sorted(bounded, key=key))


def _strict_object(value: dict[str, Any], expected: set[str]) -> None:
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        unknown = sorted(actual - expected)
        raise TaskEvidenceBundleError(
            f"invalid TaskEvidenceBundle fields: missing={missing}, unknown={unknown}"
        )


def _learning_eligibility(
    task: LogicalTask,
    attempts: tuple[TaskAttempt, ...],
    review_memberships: tuple[TaskAtomMembership, ...],
) -> tuple[str, tuple[str, ...]]:
    reasons: list[str] = []
    if review_memberships:
        reasons.append("unresolved_task_membership")
    if task.verification in {"contradicted", "conflicted"}:
        reasons.append(f"task_verification_{task.verification}")
        return "ineligible", tuple(reasons)
    if task.outcome in {"cancelled", "abandoned"}:
        reasons.append(f"task_outcome_{task.outcome}")
        return "ineligible", tuple(reasons)
    if task.lifecycle != "closed" or task.outcome == "unknown":
        reasons.append("task_not_terminal")
    if not attempts:
        reasons.append("no_task_attempt")
    elif any(attempt.lifecycle != "finished" for attempt in attempts):
        reasons.append("attempt_not_terminal")
    if task.verification not in {"verified", "not_applicable"}:
        reasons.append("task_outcome_not_verified")
    if reasons:
        return "needs_review", tuple(dict.fromkeys(reasons))
    return "eligible", ("verified_terminal_task",)


@dataclass(frozen=True)
class TaskEvidenceBundle:
    generation_id: str
    tenant_id: str
    task_scope_id: str
    source_revision: str
    created_at: str
    generator_fingerprint: str
    task: LogicalTask
    confirmed_memberships: tuple[TaskAtomMembership, ...]
    review_memberships: tuple[TaskAtomMembership, ...]
    task_relations: tuple[TaskRelation, ...]
    attempts: tuple[TaskAttempt, ...]
    attempt_relations: tuple[AttemptRelation, ...]
    usage_allocations: tuple[UsageAllocation, ...]
    learning_eligibility: str
    eligibility_reasons: tuple[str, ...]
    eligibility_policy_version: str = ELIGIBILITY_POLICY_VERSION
    schema_version: int = BUNDLE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _ensure(
            self.schema_version == BUNDLE_SCHEMA_VERSION,
            f"unsupported TaskEvidenceBundle schema_version={self.schema_version}",
        )
        for field_name in (
            "generation_id",
            "tenant_id",
            "task_scope_id",
            "source_revision",
            "created_at",
            "generator_fingerprint",
            "eligibility_policy_version",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise TaskEvidenceBundleError(f"{field_name} must be non-empty")
        _ensure(not self.task.tombstoned, "tombstoned Task is not learning evidence")
        _ensure(bool(self.confirmed_memberships), "Task needs confirmed primary Atom")
        _ensure(
            self.learning_eligibility in LEARNING_ELIGIBILITIES,
            "invalid learning_eligibility",
        )
        _ensure(bool(self.eligibility_reasons), "eligibility_reasons must be non-empty")
        _ensure(
            all(isinstance(item, str) and item for item in self.eligibility_reasons),
            "eligibility_reasons must contain strings",
        )
        _ensure(
            (self.learning_eligibility, self.eligibility_reasons)
            == _learning_eligibility(self.task, self.attempts, self.review_memberships),
            "learning eligibility does not match Task outcome",
        )
        confirmed_atoms = set()
        for membership in self.confirmed_memberships:
            _ensure(
                membership.task_id == self.task.task_id
                and membership.role == "primary"
                and membership.decision == "confirmed"
                and not membership.stale,
                "learning membership must be live confirmed primary",
            )
            self._check_atom_scope(membership)
            confirmed_atoms.add(membership.atom_ref.key)
        for membership in self.review_memberships:
            _ensure(
                membership.task_id == self.task.task_id
                and membership.decision in {"proposed", "needs_review"}
                and not membership.stale,
                "invalid review membership",
            )
            self._check_atom_scope(membership)
        attempt_ids = {attempt.attempt_id for attempt in self.attempts}
        for attempt in self.attempts:
            _ensure(attempt.task_id == self.task.task_id, "Attempt belongs elsewhere")
            for evidence in attempt.evidence_ranges:
                _ensure(
                    not evidence.stale, "stale EvidenceRange is not learning evidence"
                )
                _ensure(
                    evidence.session_ref.tenant_id == self.tenant_id
                    and evidence.session_ref.task_scope_id == self.task_scope_id,
                    "evidence crosses TaskScope",
                )
                _ensure(
                    not evidence.atom_ref or evidence.atom_ref.key in confirmed_atoms,
                    "Attempt evidence Atom lacks confirmed primary membership",
                )
        for relation in self.attempt_relations:
            _ensure(
                relation.from_attempt_id in attempt_ids
                and relation.to_attempt_id in attempt_ids,
                "Attempt relation leaves the Task bundle",
            )
        for relation in self.task_relations:
            _ensure(
                self.task.task_id in {relation.from_task_id, relation.to_task_id},
                "Task relation does not touch bundle Task",
            )
            _ensure(not relation.stale, "stale Task relation is not bundle evidence")
        for allocation in self.usage_allocations:
            _ensure(allocation.task_id == self.task.task_id, "usage belongs elsewhere")
            _ensure(
                not allocation.attempt_id or allocation.attempt_id in attempt_ids,
                "usage references another Attempt",
            )

    def _check_atom_scope(self, membership: TaskAtomMembership) -> None:
        atom_ref = membership.atom_ref
        if (
            atom_ref.tenant_id != self.tenant_id
            or atom_ref.task_scope_id != self.task_scope_id
        ):
            raise TaskEvidenceBundleError("membership crosses TaskScope")

    def _payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "generation_id": self.generation_id,
            "tenant_id": self.tenant_id,
            "task_scope_id": self.task_scope_id,
            "source_revision": self.source_revision,
            "created_at": self.created_at,
            "generator_fingerprint": self.generator_fingerprint,
            "task": self.task.to_dict(),
            "confirmed_memberships": [
                item.to_dict() for item in self.confirmed_memberships
            ],
            "review_memberships": [item.to_dict() for item in self.review_memberships],
            "task_relations": [item.to_dict() for item in self.task_relations],
            "attempts": [item.to_dict() for item in self.attempts],
            "attempt_relations": [item.to_dict() for item in self.attempt_relations],
            "usage_allocations": [item.to_dict() for item in self.usage_allocations],
            "learning_eligibility": self.learning_eligibility,
            "eligibility_reasons": list(self.eligibility_reasons),
            "eligibility_policy_version": self.eligibility_policy_version,
        }

    def _task_evidence_payload(self) -> dict[str, Any]:
        """Return generation-independent Task evidence for dirty detection."""
        payload = self._payload()
        for field_name in (
            "generation_id",
            "source_revision",
            "created_at",
            "generator_fingerprint",
        ):
            payload.pop(field_name)
        return payload

    @property
    def bundle_fingerprint(self) -> str:
        return _fingerprint(self._payload())

    @property
    def task_evidence_fingerprint(self) -> str:
        return _fingerprint(self._task_evidence_payload())

    def to_dict(self) -> dict[str, Any]:
        return {
            **self._payload(),
            "bundle_fingerprint": self.bundle_fingerprint,
            "task_evidence_fingerprint": self.task_evidence_fingerprint,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> TaskEvidenceBundle:
        if not isinstance(value, dict):
            raise TaskEvidenceBundleError("TaskEvidenceBundle must be an object")
        expected = set(cls.__dataclass_fields__) | {
            "bundle_fingerprint",
            "task_evidence_fingerprint",
        }
        _strict_object(value, expected)
        try:
            serialized_size = len(_canonical_json(value))
            _ensure(
                serialized_size <= MAX_TASK_EVIDENCE_BUNDLE_BYTES,
                "Task evidence serialized_bytes exceeds hard bound: "
                f"{serialized_size}>{MAX_TASK_EVIDENCE_BUNDLE_BYTES}",
            )
            bundle = cls(
                schema_version=value["schema_version"],
                generation_id=value["generation_id"],
                tenant_id=value["tenant_id"],
                task_scope_id=value["task_scope_id"],
                source_revision=value["source_revision"],
                created_at=value["created_at"],
                generator_fingerprint=value["generator_fingerprint"],
                task=LogicalTask.from_dict(value["task"]),
                confirmed_memberships=tuple(
                    TaskAtomMembership.from_dict(item)
                    for item in value["confirmed_memberships"]
                ),
                review_memberships=tuple(
                    TaskAtomMembership.from_dict(item)
                    for item in value["review_memberships"]
                ),
                task_relations=tuple(
                    TaskRelation.from_dict(item) for item in value["task_relations"]
                ),
                attempts=tuple(
                    TaskAttempt.from_dict(item) for item in value["attempts"]
                ),
                attempt_relations=tuple(
                    AttemptRelation.from_dict(item)
                    for item in value["attempt_relations"]
                ),
                usage_allocations=tuple(
                    UsageAllocation.from_dict(item)
                    for item in value["usage_allocations"]
                ),
                learning_eligibility=value["learning_eligibility"],
                eligibility_reasons=tuple(value["eligibility_reasons"]),
                eligibility_policy_version=value["eligibility_policy_version"],
            )
        except TaskEvidenceBundleError:
            raise
        except (KeyError, TypeError, ValueError) as exc:
            raise TaskEvidenceBundleError(
                f"invalid nested TaskEvidenceBundle value: {exc}"
            ) from exc
        if value["bundle_fingerprint"] != bundle.bundle_fingerprint:
            raise TaskEvidenceBundleError("TaskEvidenceBundle fingerprint mismatch")
        if value["task_evidence_fingerprint"] != bundle.task_evidence_fingerprint:
            raise TaskEvidenceBundleError(
                "TaskEvidenceBundle task evidence fingerprint mismatch"
            )
        return bundle


class TaskEvidenceBundleIndex:
    """Index one generation once, then build Task bundles without rescanning it."""

    def __init__(
        self,
        generation: TaskGraphGeneration,
        *,
        limits: TaskEvidenceLimits | None = None,
    ) -> None:
        self._generation = generation
        self._generator_fingerprint = _fingerprint(generation.generator)
        self._limits = limits or TaskEvidenceLimits()
        self._tasks = {item.task_id: item for item in generation.tasks}
        self._confirmed: dict[str, list[TaskAtomMembership]] = defaultdict(list)
        self._review: dict[str, list[TaskAtomMembership]] = defaultdict(list)
        self._relations: dict[str, list[TaskRelation]] = defaultdict(list)
        self._attempts: dict[str, list[TaskAttempt]] = defaultdict(list)
        self._attempt_relations: dict[str, list[AttemptRelation]] = defaultdict(list)
        self._usage: dict[str, list[UsageAllocation]] = defaultdict(list)

        def append_bounded(
            mapping: dict[str, list[Any]],
            task_id: str,
            item: Any,
            maximum: int,
        ) -> None:
            values = mapping[task_id]
            if len(values) <= maximum:
                values.append(item)

        for item in generation.memberships:
            if item.stale:
                continue
            if item.role == "primary" and item.decision == "confirmed":
                append_bounded(
                    self._confirmed,
                    item.task_id,
                    item,
                    self._limits.memberships,
                )
            elif item.decision in {"proposed", "needs_review"}:
                append_bounded(
                    self._review,
                    item.task_id,
                    item,
                    self._limits.review_memberships,
                )
        for item in generation.relations:
            if item.stale:
                continue
            append_bounded(
                self._relations,
                item.from_task_id,
                item,
                self._limits.task_relations,
            )
            if item.to_task_id != item.from_task_id:
                append_bounded(
                    self._relations,
                    item.to_task_id,
                    item,
                    self._limits.task_relations,
                )
        attempt_tasks: dict[str, str] = {}
        for item in generation.attempts:
            attempt_tasks[item.attempt_id] = item.task_id
            append_bounded(
                self._attempts,
                item.task_id,
                item,
                self._limits.attempts,
            )
        for item in generation.attempt_relations:
            task_id = attempt_tasks.get(item.from_attempt_id)
            if task_id and attempt_tasks.get(item.to_attempt_id) == task_id:
                append_bounded(
                    self._attempt_relations,
                    task_id,
                    item,
                    self._limits.attempt_relations,
                )
        for item in generation.usage_allocations:
            if item.task_id:
                append_bounded(
                    self._usage,
                    item.task_id,
                    item,
                    self._limits.usage_allocations,
                )

    def build(self, task_id: str) -> TaskEvidenceBundle:
        if not isinstance(task_id, str) or not task_id.strip():
            raise TaskEvidenceBundleError("task_id must be non-empty")
        task = self._tasks.get(task_id)
        if task is None:
            raise TaskEvidenceBundleError(f"Task not found in generation: {task_id}")
        if task.tombstoned:
            raise TaskEvidenceBundleError(
                "tombstoned Task cannot become learning evidence"
            )

        confirmed = _bounded_ordered(
            self._confirmed[task_id],
            key=lambda item: item.atom_ref.key,
            maximum=self._limits.memberships,
            field_name="memberships",
        )
        review = _bounded_ordered(
            self._review[task_id],
            key=lambda item: (item.atom_ref.key, item.membership_id),
            maximum=self._limits.review_memberships,
            field_name="review_memberships",
        )
        task_relations = _bounded_ordered(
            self._relations[task_id],
            key=lambda item: item.relation_id,
            maximum=self._limits.task_relations,
            field_name="task_relations",
        )
        raw_attempts = _bounded_ordered(
            self._attempts[task_id],
            key=lambda item: (item.started_at, item.attempt_id),
            maximum=self._limits.attempts,
            field_name="attempts",
        )
        evidence_range_count = 0
        attempts_list: list[TaskAttempt] = []
        for item in raw_attempts:
            evidence_range_count += len(item.evidence_ranges)
            if evidence_range_count > self._limits.evidence_ranges:
                raise TaskEvidenceBundleError(
                    "Task evidence evidence_ranges exceeds bound: "
                    f"{evidence_range_count}>{self._limits.evidence_ranges}"
                )
            attempts_list.append(
                replace(
                    item,
                    evidence_ranges=_ordered(
                        item.evidence_ranges,
                        lambda evidence: evidence.evidence_id,
                    ),
                )
            )
        attempts = tuple(attempts_list)
        attempt_relations = _bounded_ordered(
            self._attempt_relations[task_id],
            key=lambda item: item.relation_id,
            maximum=self._limits.attempt_relations,
            field_name="attempt_relations",
        )
        usage = _bounded_ordered(
            self._usage[task_id],
            key=lambda item: item.allocation_id,
            maximum=self._limits.usage_allocations,
            field_name="usage_allocations",
        )
        eligibility, reasons = _learning_eligibility(task, attempts, review)
        generation = self._generation
        bundle = TaskEvidenceBundle(
            generation_id=generation.generation_id,
            tenant_id=generation.tenant_id,
            task_scope_id=generation.task_scope_id,
            source_revision=generation.source_revision,
            created_at=generation.created_at,
            generator_fingerprint=self._generator_fingerprint,
            task=task,
            confirmed_memberships=confirmed,
            review_memberships=review,
            task_relations=task_relations,
            attempts=attempts,
            attempt_relations=attempt_relations,
            usage_allocations=usage,
            learning_eligibility=eligibility,
            eligibility_reasons=reasons,
        )
        serialized_size = len(_canonical_json(bundle.to_dict()))
        if serialized_size > self._limits.serialized_bytes:
            raise TaskEvidenceBundleError(
                "Task evidence serialized_bytes exceeds bound: "
                f"{serialized_size}>{self._limits.serialized_bytes}"
            )
        return bundle


def build_task_evidence_bundle(
    generation: TaskGraphGeneration,
    task_id: str,
    *,
    limits: TaskEvidenceLimits | None = None,
) -> TaskEvidenceBundle:
    """Build one safe learning bundle from an immutable generation."""
    return TaskEvidenceBundleIndex(generation, limits=limits).build(task_id)
