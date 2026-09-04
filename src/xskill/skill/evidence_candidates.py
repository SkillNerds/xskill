"""Versioned candidate records for Task-grounded Skill learning.

The existing ``.candidates.yml`` buffer remains the durable queue.  This
module defines the bounded record written into that queue; it stores stable
references and fingerprints, never copied trajectory or Task prompt text.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any, Iterable

from xskill.skill.evidence_candidate_refs import (
    CandidateAtomRef,
    CandidateAttemptRef,
    EvidenceCandidateError as EvidenceCandidateError,
    ensure as _ensure,
    fingerprint as _fingerprint,
    optional_fingerprint as _optional_fingerprint,
    required_string as _required_string,
    stable_candidate_id as _candidate_id,
    strict_object as _strict_object,
)
from xskill.tasks.evidence_bundle import (
    LEARNING_ELIGIBILITIES,
    TaskEvidenceBundle,
)
from xskill.tasks.models import (
    TASK_LIFECYCLES,
    TASK_OUTCOMES,
    USER_DISPOSITIONS,
    VERIFICATIONS,
    AtomRef,
)

CANDIDATE_SCHEMA_VERSION = 1
EVIDENCE_UNITS = frozenset(("logical_task", "atom_fallback"))
FALLBACK_REASONS = frozenset(
    (
        "task_graph_disabled",
        "task_unresolved",
        "task_evidence_unavailable",
        "legacy_atom_candidate",
    )
)
MAX_CANDIDATE_ATOM_REFS = 256
MAX_CANDIDATE_ATTEMPT_REFS = 128
MAX_CANDIDATE_NOTE_CHARS = 500


@dataclass(frozen=True)
class TaskSkillCandidate:
    """One Skill support record backed by a Task bundle or explicit fallback."""

    candidate_id: str
    evidence_unit: str
    skill_name: str
    weightscore: int
    note: str
    tenant_id: str | None
    task_scope_id: str | None
    task_id: str | None
    task_fingerprint: str | None
    generation_id: str | None
    generation_fingerprint: str | None
    bundle_fingerprint: str | None
    generator_fingerprint: str | None
    atom_refs: tuple[CandidateAtomRef, ...]
    attempt_refs: tuple[CandidateAttemptRef, ...]
    task_lifecycle: str | None
    task_outcome: str | None
    task_verification: str | None
    task_user_disposition: str | None
    learning_eligibility: str
    eligibility_reasons: tuple[str, ...]
    fallback_reason: str | None
    schema_version: int = CANDIDATE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _ensure(
            self.schema_version == CANDIDATE_SCHEMA_VERSION,
            f"unsupported candidate schema_version={self.schema_version}",
        )
        _ensure(
            isinstance(self.evidence_unit, str)
            and self.evidence_unit in EVIDENCE_UNITS,
            "invalid evidence_unit",
        )
        _required_string(self.skill_name, "skill_name", maximum=200)
        _ensure(
            not isinstance(self.weightscore, bool)
            and isinstance(self.weightscore, int)
            and 1 <= self.weightscore <= 10,
            "weightscore must be an integer from 1 through 10",
        )
        _ensure(isinstance(self.note, str), "note must be a string")
        _ensure(
            len(self.note) <= MAX_CANDIDATE_NOTE_CHARS,
            f"note exceeds {MAX_CANDIDATE_NOTE_CHARS} characters",
        )
        _ensure(
            isinstance(self.atom_refs, tuple)
            and all(isinstance(item, CandidateAtomRef) for item in self.atom_refs),
            "atom_refs must contain CandidateAtomRef values",
        )
        _ensure(bool(self.atom_refs), "candidate needs Atom references")
        _ensure(
            len(self.atom_refs) <= MAX_CANDIDATE_ATOM_REFS,
            "candidate Atom references exceed bound",
        )
        _ensure(
            isinstance(self.attempt_refs, tuple)
            and all(
                isinstance(item, CandidateAttemptRef)
                for item in self.attempt_refs
            ),
            "attempt_refs must contain CandidateAttemptRef values",
        )
        _ensure(
            len(self.attempt_refs) <= MAX_CANDIDATE_ATTEMPT_REFS,
            "candidate Attempt references exceed bound",
        )
        atom_keys = tuple(item.key for item in self.atom_refs)
        _ensure(len(atom_keys) == len(set(atom_keys)), "Atom references must be unique")
        _ensure(atom_keys == tuple(sorted(atom_keys)), "Atom references must be ordered")
        attempt_keys = tuple(
            (item.started_at, item.attempt_id) for item in self.attempt_refs
        )
        _ensure(
            len(attempt_keys) == len(set(attempt_keys)),
            "Attempt references must be unique",
        )
        _ensure(
            attempt_keys == tuple(sorted(attempt_keys)),
            "Attempt references must be ordered",
        )
        _ensure(
            isinstance(self.learning_eligibility, str)
            and self.learning_eligibility in LEARNING_ELIGIBILITIES,
            "invalid learning_eligibility",
        )
        _ensure(
            isinstance(self.eligibility_reasons, tuple),
            "eligibility_reasons must be a tuple",
        )
        _ensure(bool(self.eligibility_reasons), "eligibility_reasons must be non-empty")
        for reason in self.eligibility_reasons:
            _required_string(reason, "eligibility_reason", maximum=200)
        _ensure(
            len(self.eligibility_reasons) == len(set(self.eligibility_reasons)),
            "eligibility_reasons must be unique",
        )
        self._validate_evidence_shape()
        _ensure(
            self.candidate_id == self._expected_candidate_id(),
            "candidate_id does not match stable evidence identity",
        )

    def _validate_evidence_shape(self) -> None:
        fingerprint_fields = (
            (self.task_fingerprint, "task_fingerprint"),
            (self.generation_fingerprint, "generation_fingerprint"),
            (self.bundle_fingerprint, "bundle_fingerprint"),
            (self.generator_fingerprint, "generator_fingerprint"),
        )
        for value, name in fingerprint_fields:
            _optional_fingerprint(value, name)
        task_fields = (
            self.tenant_id,
            self.task_scope_id,
            self.task_id,
            self.task_fingerprint,
            self.generation_id,
            self.generation_fingerprint,
            self.bundle_fingerprint,
            self.generator_fingerprint,
            self.task_lifecycle,
            self.task_outcome,
            self.task_verification,
            self.task_user_disposition,
        )
        if self.evidence_unit == "logical_task":
            _ensure(all(task_fields), "logical_task candidate needs Task provenance")
            for value, name in (
                (self.tenant_id, "tenant_id"),
                (self.task_scope_id, "task_scope_id"),
                (self.task_id, "task_id"),
                (self.generation_id, "generation_id"),
            ):
                _required_string(value, name, maximum=300)
            _ensure(
                isinstance(self.task_lifecycle, str)
                and self.task_lifecycle in TASK_LIFECYCLES,
                "invalid Task lifecycle",
            )
            _ensure(
                isinstance(self.task_outcome, str)
                and self.task_outcome in TASK_OUTCOMES,
                "invalid Task outcome",
            )
            _ensure(
                isinstance(self.task_verification, str)
                and self.task_verification in VERIFICATIONS,
                "invalid Task verification",
            )
            _ensure(
                isinstance(self.task_user_disposition, str)
                and self.task_user_disposition in USER_DISPOSITIONS,
                "invalid Task user disposition",
            )
            if self.task_lifecycle in {"open", "blocked"}:
                _ensure(
                    self.task_outcome == "unknown",
                    "open or blocked Task outcome must be unknown",
                )
            else:
                _ensure(
                    self.task_outcome != "unknown",
                    "closed Task requires a terminal outcome",
                )
            _ensure(self.fallback_reason is None, "logical_task cannot be a fallback")
            _ensure(all(item.scoped for item in self.atom_refs), "Task Atom refs need scope")
            _ensure(
                all(
                    item.tenant_id == self.tenant_id
                    and item.task_scope_id == self.task_scope_id
                    for item in self.atom_refs
                ),
                "Task Atom reference crosses scope",
            )
            return
        _ensure(
            all(value is None for value in task_fields),
            "atom_fallback cannot claim Task provenance",
        )
        _ensure(not self.attempt_refs, "atom_fallback cannot claim Attempts")
        _ensure(len(self.atom_refs) == 1, "atom_fallback requires exactly one Atom")
        _ensure(
            isinstance(self.fallback_reason, str)
            and self.fallback_reason in FALLBACK_REASONS,
            "invalid fallback_reason",
        )
        _ensure(
            self.learning_eligibility == "needs_review",
            "atom_fallback must remain visible as needs_review",
        )
        _ensure(
            self.eligibility_reasons == (f"atom_fallback:{self.fallback_reason}",),
            "atom_fallback eligibility reason must identify the fallback",
        )

    def _expected_candidate_id(self) -> str:
        if self.evidence_unit == "logical_task":
            identity = {
                "evidence_unit": self.evidence_unit,
                "tenant_id": self.tenant_id,
                "task_scope_id": self.task_scope_id,
                "task_id": self.task_id,
                "skill_name": self.skill_name,
            }
        else:
            identity = {
                "evidence_unit": self.evidence_unit,
                "atom_ref": self.atom_refs[0].to_dict(),
                "skill_name": self.skill_name,
            }
        return _candidate_id(identity)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "candidate_id": self.candidate_id,
            "evidence_unit": self.evidence_unit,
            "skill_name": self.skill_name,
            "weightscore": self.weightscore,
            "note": self.note,
            "tenant_id": self.tenant_id,
            "task_scope_id": self.task_scope_id,
            "task_id": self.task_id,
            "task_fingerprint": self.task_fingerprint,
            "generation_id": self.generation_id,
            "generation_fingerprint": self.generation_fingerprint,
            "bundle_fingerprint": self.bundle_fingerprint,
            "generator_fingerprint": self.generator_fingerprint,
            "atom_refs": [item.to_dict() for item in self.atom_refs],
            "attempt_refs": [item.to_dict() for item in self.attempt_refs],
            "task_lifecycle": self.task_lifecycle,
            "task_outcome": self.task_outcome,
            "task_verification": self.task_verification,
            "task_user_disposition": self.task_user_disposition,
            "learning_eligibility": self.learning_eligibility,
            "eligibility_reasons": list(self.eligibility_reasons),
            "fallback_reason": self.fallback_reason,
        }

    @classmethod
    def from_dict(cls, value: Any) -> TaskSkillCandidate:
        expected = set(cls.__dataclass_fields__)
        data = dict(_strict_object(value, expected, "TaskSkillCandidate"))
        atom_refs = data["atom_refs"]
        attempt_refs = data["attempt_refs"]
        eligibility_reasons = data["eligibility_reasons"]
        _ensure(isinstance(atom_refs, list), "atom_refs must be a list")
        _ensure(isinstance(attempt_refs, list), "attempt_refs must be a list")
        _ensure(
            isinstance(eligibility_reasons, list),
            "eligibility_reasons must be a list",
        )
        data["atom_refs"] = tuple(CandidateAtomRef.from_dict(item) for item in atom_refs)
        data["attempt_refs"] = tuple(
            CandidateAttemptRef.from_dict(item) for item in attempt_refs
        )
        data["eligibility_reasons"] = tuple(eligibility_reasons)
        return cls(**data)

    @classmethod
    def from_task_bundle(
        cls,
        bundle: TaskEvidenceBundle,
        *,
        skill_name: str,
        weightscore: int,
        note: str = "",
    ) -> TaskSkillCandidate:
        atom_refs = tuple(
            CandidateAtomRef.from_atom_ref(item.atom_ref)
            for item in bundle.confirmed_memberships
        )
        attempt_refs = tuple(
            CandidateAttemptRef(
                attempt_id=item.attempt_id,
                started_at=item.started_at,
                ended_at=item.ended_at,
                lifecycle=item.lifecycle,
                outcome=item.outcome,
                verification=item.verification,
                user_disposition=item.user_disposition,
                evidence_range_ids=tuple(
                    sorted(evidence.evidence_id for evidence in item.evidence_ranges)
                ),
            )
            for item in bundle.attempts
        )
        generation_fingerprint = _fingerprint(
            {
                "generation_id": bundle.generation_id,
                "source_revision": bundle.source_revision,
                "created_at": bundle.created_at,
                "generator_fingerprint": bundle.generator_fingerprint,
            }
        )
        candidate_identity = {
            "evidence_unit": "logical_task",
            "tenant_id": bundle.tenant_id,
            "task_scope_id": bundle.task_scope_id,
            "task_id": bundle.task.task_id,
            "skill_name": skill_name,
        }
        return cls(
            candidate_id=_candidate_id(candidate_identity),
            evidence_unit="logical_task",
            skill_name=skill_name,
            weightscore=weightscore,
            note=note,
            tenant_id=bundle.tenant_id,
            task_scope_id=bundle.task_scope_id,
            task_id=bundle.task.task_id,
            task_fingerprint=_fingerprint(bundle.task.to_dict()),
            generation_id=bundle.generation_id,
            generation_fingerprint=generation_fingerprint,
            bundle_fingerprint=bundle.bundle_fingerprint,
            generator_fingerprint=bundle.generator_fingerprint,
            atom_refs=atom_refs,
            attempt_refs=attempt_refs,
            task_lifecycle=bundle.task.lifecycle,
            task_outcome=bundle.task.outcome,
            task_verification=bundle.task.verification,
            task_user_disposition=bundle.task.user_disposition,
            learning_eligibility=bundle.learning_eligibility,
            eligibility_reasons=bundle.eligibility_reasons,
            fallback_reason=None,
        )

    @classmethod
    def from_atom_fallback(
        cls,
        *,
        atom_id: str,
        skill_name: str,
        weightscore: int,
        fallback_reason: str,
        atom_ref: AtomRef | None = None,
        note: str = "",
    ) -> TaskSkillCandidate:
        if atom_ref is not None:
            _ensure(atom_ref.atom_id == atom_id, "atom_id does not match atom_ref")
            candidate_atom = CandidateAtomRef.from_atom_ref(atom_ref)
        else:
            candidate_atom = CandidateAtomRef(atom_id=atom_id)
        candidate_identity = {
            "evidence_unit": "atom_fallback",
            "atom_ref": candidate_atom.to_dict(),
            "skill_name": skill_name,
        }
        return cls(
            candidate_id=_candidate_id(candidate_identity),
            evidence_unit="atom_fallback",
            skill_name=skill_name,
            weightscore=weightscore,
            note=note,
            tenant_id=None,
            task_scope_id=None,
            task_id=None,
            task_fingerprint=None,
            generation_id=None,
            generation_fingerprint=None,
            bundle_fingerprint=None,
            generator_fingerprint=None,
            atom_refs=(candidate_atom,),
            attempt_refs=(),
            task_lifecycle=None,
            task_outcome=None,
            task_verification=None,
            task_user_disposition=None,
            learning_eligibility="needs_review",
            eligibility_reasons=(f"atom_fallback:{fallback_reason}",),
            fallback_reason=fallback_reason,
        )


def migrate_legacy_atom_candidate(
    value: Any,
    *,
    skill_name: str,
) -> TaskSkillCandidate:
    """Deterministically wrap one current v2.1 Atom candidate as fallback."""
    if not isinstance(value, dict):
        raise EvidenceCandidateError("legacy Atom candidate must be an object")
    allowed = {"atom_id", "weightscore", "note"}
    unknown = set(value) - allowed
    _ensure(not unknown, f"unsupported legacy Atom candidate fields: {sorted(unknown)}")
    _ensure("atom_id" in value and "weightscore" in value, "legacy Atom fields missing")
    return TaskSkillCandidate.from_atom_fallback(
        atom_id=value["atom_id"],
        skill_name=skill_name,
        weightscore=value["weightscore"],
        note=value.get("note", ""),
        fallback_reason="legacy_atom_candidate",
    )


def migrate_legacy_candidate_buffer(
    data: dict[str, Any],
    *,
    skill_name: str,
) -> tuple[dict[str, Any], int]:
    """Return a migrated copy; legacy pattern candidates stay byte-for-byte data."""
    if not isinstance(data, dict):
        raise EvidenceCandidateError("candidate buffer must be an object")
    buffer = data.get("candidates", [])
    _ensure(isinstance(buffer, list), "candidates must be a list")
    migrated: list[dict[str, Any]] = []
    migrated_count = 0
    for value in buffer:
        if not isinstance(value, dict):
            raise EvidenceCandidateError("candidate buffer entries must be objects")
        if "schema_version" in value or "candidate_id" in value:
            parsed = TaskSkillCandidate.from_dict(value)
            _ensure(parsed.skill_name == skill_name, "candidate belongs to another Skill")
            migrated.append(parsed.to_dict())
        elif "atom_id" in value:
            migrated.append(
                migrate_legacy_atom_candidate(value, skill_name=skill_name).to_dict()
            )
            migrated_count += 1
        else:
            migrated.append(copy.deepcopy(value))
    result = copy.deepcopy(data)
    result["candidates"] = migrated
    return result, migrated_count


def upsert_evidence_candidates(
    data: dict[str, Any],
    candidates: Iterable[TaskSkillCandidate],
) -> tuple[list[bool], int]:
    """Upsert a bounded batch with one O(existing + incoming) buffer scan."""
    if not isinstance(data, dict):
        raise EvidenceCandidateError("candidate buffer must be an object")
    incoming = tuple(candidates)
    _ensure(bool(incoming), "candidate batch must not be empty")
    _ensure(
        all(isinstance(item, TaskSkillCandidate) for item in incoming),
        "candidate batch must contain TaskSkillCandidate values",
    )
    skill_name = incoming[0].skill_name
    _ensure(
        all(item.skill_name == skill_name for item in incoming),
        "candidate batch cannot cross Skills",
    )
    incoming_ids = [item.candidate_id for item in incoming]
    _ensure(
        len(incoming_ids) == len(set(incoming_ids)),
        "candidate batch contains duplicate stable identities",
    )
    buffer = data.setdefault("candidates", [])
    _ensure(isinstance(buffer, list), "candidates must be a list")
    positions: dict[str, int] = {}
    total = 0
    for index, value in enumerate(buffer):
        if not isinstance(value, dict):
            raise EvidenceCandidateError("candidate buffer entries must be objects")
        weightscore = value.get("weightscore", 0)
        _ensure(
            not isinstance(weightscore, bool) and isinstance(weightscore, int),
            "candidate weightscore must be an integer",
        )
        _ensure(
            "weightscore" not in value or 1 <= weightscore <= 10,
            "candidate weightscore must be from 1 through 10",
        )
        total += weightscore
        candidate_id = value.get("candidate_id")
        if candidate_id is not None:
            parsed = TaskSkillCandidate.from_dict(value)
            _ensure(parsed.skill_name == skill_name, "candidate belongs to another Skill")
            _ensure(parsed.candidate_id not in positions, "duplicate candidate_id in buffer")
            positions[parsed.candidate_id] = index
    new_flags: list[bool] = []
    for candidate in incoming:
        serialized = candidate.to_dict()
        position = positions.get(candidate.candidate_id)
        if position is None:
            positions[candidate.candidate_id] = len(buffer)
            buffer.append(serialized)
            total += candidate.weightscore
            new_flags.append(True)
        else:
            total -= int(buffer[position]["weightscore"])
            buffer[position] = serialized
            total += candidate.weightscore
            new_flags.append(False)
    return new_flags, total
