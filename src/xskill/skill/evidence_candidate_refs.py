"""Strict, text-free references used by Task-grounded Skill candidates."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

from xskill.tasks.models import (
    ATTEMPT_LIFECYCLES,
    ATTEMPT_OUTCOMES,
    USER_DISPOSITIONS,
    VERIFICATIONS,
    AtomRef,
)

FINGERPRINT_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


class EvidenceCandidateError(ValueError):
    """A candidate record violates the versioned evidence contract."""


def ensure(condition: bool, message: str) -> None:
    if not condition:
        raise EvidenceCandidateError(message)


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def fingerprint(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(value)).hexdigest()


def stable_candidate_id(identity: dict[str, Any]) -> str:
    return "candidate_" + hashlib.sha256(canonical_json(identity)).hexdigest()


def strict_object(value: Any, expected: set[str], name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise EvidenceCandidateError(f"{name} must be an object")
    actual = set(value)
    if actual != expected:
        raise EvidenceCandidateError(
            f"invalid {name} fields: missing={sorted(expected - actual)}, "
            f"unknown={sorted(actual - expected)}"
        )
    return value


def required_string(value: Any, name: str, *, maximum: int = 500) -> str:
    ensure(isinstance(value, str) and bool(value.strip()), f"{name} must be non-empty")
    ensure(len(value) <= maximum, f"{name} exceeds {maximum} characters")
    return value


def optional_fingerprint(value: str | None, name: str) -> None:
    if value is not None:
        ensure(
            isinstance(value, str) and bool(FINGERPRINT_RE.fullmatch(value)),
            f"{name} must be a sha256 fingerprint",
        )


@dataclass(frozen=True)
class CandidateAtomRef:
    """Scoped Atom reference; legacy migration may retain only ``atom_id``."""

    atom_id: str
    tenant_id: str | None = None
    task_scope_id: str | None = None
    source_scope_id: str | None = None
    traj_id: str | None = None

    def __post_init__(self) -> None:
        required_string(self.atom_id, "atom_ref.atom_id", maximum=300)
        scopes = (
            self.tenant_id,
            self.task_scope_id,
            self.source_scope_id,
            self.traj_id,
        )
        ensure(
            all(value is None for value in scopes)
            or all(isinstance(value, str) and bool(value) for value in scopes),
            "atom_ref scope must be complete or entirely unavailable",
        )

    @property
    def scoped(self) -> bool:
        return self.tenant_id is not None

    @property
    def key(self) -> tuple[str, str, str, str, str]:
        return (
            self.tenant_id or "",
            self.task_scope_id or "",
            self.source_scope_id or "",
            self.traj_id or "",
            self.atom_id,
        )

    @classmethod
    def from_atom_ref(cls, atom_ref: AtomRef) -> CandidateAtomRef:
        return cls(
            atom_id=atom_ref.atom_id,
            tenant_id=atom_ref.tenant_id,
            task_scope_id=atom_ref.task_scope_id,
            source_scope_id=atom_ref.source_scope_id,
            traj_id=atom_ref.traj_id,
        )

    def to_dict(self) -> dict[str, str | None]:
        return {
            "atom_id": self.atom_id,
            "tenant_id": self.tenant_id,
            "task_scope_id": self.task_scope_id,
            "source_scope_id": self.source_scope_id,
            "traj_id": self.traj_id,
        }

    @classmethod
    def from_dict(cls, value: Any) -> CandidateAtomRef:
        data = strict_object(
            value,
            {"atom_id", "tenant_id", "task_scope_id", "source_scope_id", "traj_id"},
            "CandidateAtomRef",
        )
        return cls(**data)


@dataclass(frozen=True)
class CandidateAttemptRef:
    """Outcome-bearing Attempt reference without duplicated evidence text."""

    attempt_id: str
    started_at: str
    ended_at: str | None
    lifecycle: str
    outcome: str
    verification: str
    user_disposition: str
    evidence_range_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        required_string(self.attempt_id, "attempt_ref.attempt_id", maximum=300)
        required_string(self.started_at, "attempt_ref.started_at", maximum=100)
        if self.ended_at is not None:
            required_string(self.ended_at, "attempt_ref.ended_at", maximum=100)
        ensure(
            isinstance(self.lifecycle, str) and self.lifecycle in ATTEMPT_LIFECYCLES,
            "invalid Attempt lifecycle",
        )
        ensure(
            isinstance(self.outcome, str) and self.outcome in ATTEMPT_OUTCOMES,
            "invalid Attempt outcome",
        )
        ensure(
            isinstance(self.verification, str) and self.verification in VERIFICATIONS,
            "invalid Attempt verification",
        )
        ensure(
            isinstance(self.user_disposition, str)
            and self.user_disposition in USER_DISPOSITIONS,
            "invalid Attempt user disposition",
        )
        if self.lifecycle == "running":
            ensure(self.ended_at is None, "running Attempt cannot have ended_at")
            ensure(self.outcome == "unknown", "running Attempt outcome must be unknown")
        else:
            ensure(bool(self.ended_at), "finished Attempt requires ended_at")
        ensure(
            isinstance(self.evidence_range_ids, tuple)
            and all(isinstance(item, str) for item in self.evidence_range_ids),
            "evidence_range_ids must contain strings",
        )
        ensure(bool(self.evidence_range_ids), "Attempt needs evidence references")
        ensure(
            len(self.evidence_range_ids) == len(set(self.evidence_range_ids)),
            "Attempt evidence references must be unique",
        )
        ensure(
            self.evidence_range_ids == tuple(sorted(self.evidence_range_ids)),
            "Attempt evidence references must be ordered",
        )
        for evidence_id in self.evidence_range_ids:
            required_string(evidence_id, "evidence_range_id", maximum=300)

    def to_dict(self) -> dict[str, Any]:
        return {
            "attempt_id": self.attempt_id,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "lifecycle": self.lifecycle,
            "outcome": self.outcome,
            "verification": self.verification,
            "user_disposition": self.user_disposition,
            "evidence_range_ids": list(self.evidence_range_ids),
        }

    @classmethod
    def from_dict(cls, value: Any) -> CandidateAttemptRef:
        expected = set(cls.__dataclass_fields__)
        data = dict(strict_object(value, expected, "CandidateAttemptRef"))
        evidence_ids = data["evidence_range_ids"]
        ensure(isinstance(evidence_ids, list), "evidence_range_ids must be a list")
        data["evidence_range_ids"] = tuple(evidence_ids)
        return cls(**data)
