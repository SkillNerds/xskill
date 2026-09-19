from __future__ import annotations

from dataclasses import replace

import pytest

from xskill.skill.evidence_candidate_refs import (
    CandidateAtomRef,
    CandidateAttemptRef,
    EvidenceCandidateError,
)
from xskill.tasks.models import AtomRef


def test_atom_reference_supports_scoped_and_explicit_legacy_forms():
    source = AtomRef("tenant", "scope", "source", "traj", "atom-a")
    scoped = CandidateAtomRef.from_atom_ref(source)
    legacy = CandidateAtomRef(atom_id="atom-a")

    assert scoped.scoped is True
    assert CandidateAtomRef.from_dict(scoped.to_dict()) == scoped
    assert legacy.scoped is False
    assert CandidateAtomRef.from_dict(legacy.to_dict()) == legacy


def test_atom_reference_rejects_partial_scope_and_unknown_fields():
    with pytest.raises(EvidenceCandidateError, match="scope must be complete"):
        CandidateAtomRef(atom_id="atom-a", tenant_id="tenant")

    payload = CandidateAtomRef(atom_id="atom-a").to_dict()
    payload["unknown"] = True
    with pytest.raises(EvidenceCandidateError, match="unknown"):
        CandidateAtomRef.from_dict(payload)


def _attempt_ref() -> CandidateAttemptRef:
    return CandidateAttemptRef(
        attempt_id="attempt-a",
        started_at="2026-09-01T00:00:00Z",
        ended_at="2026-09-01T00:01:00Z",
        lifecycle="finished",
        outcome="succeeded",
        verification="verified",
        user_disposition="accepted",
        evidence_range_ids=("evidence-a", "evidence-b"),
    )


def test_attempt_reference_round_trips_outcome_and_ordered_evidence():
    attempt = _attempt_ref()

    assert CandidateAttemptRef.from_dict(attempt.to_dict()) == attempt
    assert attempt.to_dict()["outcome"] == "succeeded"


@pytest.mark.parametrize(
    "changed",
    [
        {"lifecycle": []},
        {"outcome": "invented"},
        {"lifecycle": "running"},
        {"evidence_range_ids": ("evidence-b", "evidence-a")},
        {"evidence_range_ids": ("evidence-a", "evidence-a")},
        {"evidence_range_ids": ({},)},
    ],
)
def test_attempt_reference_fails_closed_on_invalid_values(changed):
    with pytest.raises(EvidenceCandidateError):
        replace(_attempt_ref(), **changed)
