from __future__ import annotations

import json
from dataclasses import replace

import pytest

from xskill.skill.evidence_candidates import (
    MAX_CANDIDATE_ATOM_REFS,
    EvidenceCandidateError,
    TaskSkillCandidate,
)
from xskill.tasks.evidence_bundle import build_task_evidence_bundle
from xskill.tasks.models import (
    AtomRef,
    EvidenceRange,
    LogicalTask,
    SessionRef,
    TaskAtomMembership,
    TaskAttempt,
    TaskGraphGeneration,
)


def _bundle(*, verified: bool = True):
    session = SessionRef("tenant", "scope", "source", "traj")
    atom_a = AtomRef("tenant", "scope", "source", "traj", "atom-a")
    atom_b = AtomRef("tenant", "scope", "source", "traj", "atom-b")
    memberships = tuple(
        TaskAtomMembership(
            membership_id=f"membership-{atom.atom_id}",
            task_id="task-a",
            atom_ref=atom,
            role="primary",
            confidence=None,
            decision="confirmed",
            decided_by="rules",
            algorithm_version="v1",
            evidence_refs=(),
            observed_at=f"2026-09-01T00:00:0{index}Z",
        )
        for index, atom in enumerate((atom_b, atom_a), 1)
    )
    evidence = EvidenceRange(
        evidence_id="evidence-a",
        session_ref=session,
        locator_kind="trajectory_line",
        start=1,
        end=5,
        content_hash="hash-a",
        atom_ref=atom_a,
    )
    attempt = TaskAttempt(
        attempt_id="attempt-a",
        task_id="task-a",
        started_at="2026-09-01T00:00:00Z",
        ended_at="2026-09-01T00:01:00Z",
        lifecycle="finished",
        outcome="succeeded",
        verification="verified",
        user_disposition="accepted",
        evidence_ranges=(evidence,),
    )
    task = LogicalTask(
        task_id="task-a",
        title="private goal text",
        summary="private Task summary",
        created_at="2026-09-01T00:00:00Z",
        lifecycle="closed",
        outcome="succeeded",
        verification="verified" if verified else "unverified",
        user_disposition="accepted",
    )
    generation = TaskGraphGeneration(
        generation_id="generation-a",
        tenant_id="tenant",
        task_scope_id="scope",
        source_revision="source-revision-a",
        generator={"name": "linker", "version": "v1"},
        base_override_seq=0,
        created_at="2026-09-01T00:02:00Z",
        tasks=(task,),
        memberships=memberships,
        relations=(),
        attempts=(attempt,),
        attempt_relations=(),
        usage_allocations=(),
    )
    return build_task_evidence_bundle(generation, "task-a")


def test_task_candidate_is_versioned_bounded_private_and_round_trips():
    candidate = TaskSkillCandidate.from_task_bundle(
        _bundle(),
        skill_name="safe-skill",
        weightscore=8,
        note="bounded routing support",
    )

    payload = candidate.to_dict()
    serialized = json.dumps(payload, ensure_ascii=False)
    assert payload["schema_version"] == 1
    assert payload["evidence_unit"] == "logical_task"
    assert [item["atom_id"] for item in payload["atom_refs"]] == [
        "atom-a",
        "atom-b",
    ]
    assert payload["attempt_refs"][0]["outcome"] == "succeeded"
    assert payload["learning_eligibility"] == "eligible"
    assert "private goal text" not in serialized
    assert "private Task summary" not in serialized
    assert TaskSkillCandidate.from_dict(payload).to_dict() == payload


def test_stable_identity_survives_a_new_generation_revision():
    original_bundle = _bundle()
    changed_bundle = replace(
        original_bundle,
        generation_id="generation-b",
        source_revision="source-revision-b",
    )
    original = TaskSkillCandidate.from_task_bundle(
        original_bundle,
        skill_name="safe-skill",
        weightscore=6,
    )
    changed = TaskSkillCandidate.from_task_bundle(
        changed_bundle,
        skill_name="safe-skill",
        weightscore=9,
    )
    assert changed.candidate_id == original.candidate_id
    assert changed.bundle_fingerprint != original.bundle_fingerprint


def test_one_task_can_support_multiple_skills_without_identity_collision():
    first = TaskSkillCandidate.from_task_bundle(
        _bundle(), skill_name="skill-a", weightscore=7
    )
    second = TaskSkillCandidate.from_task_bundle(
        _bundle(), skill_name="skill-b", weightscore=5
    )

    assert first.candidate_id != second.candidate_id


def test_unverified_task_cannot_silently_become_eligible():
    candidate = TaskSkillCandidate.from_task_bundle(
        _bundle(verified=False),
        skill_name="safe-skill",
        weightscore=8,
    )

    assert candidate.learning_eligibility == "needs_review"
    assert "task_outcome_not_verified" in candidate.eligibility_reasons


def test_atom_fallback_is_explicit_and_never_claims_task_provenance():
    atom_ref = AtomRef("tenant", "scope", "source", "traj", "atom-a")
    candidate = TaskSkillCandidate.from_atom_fallback(
        atom_id="atom-a",
        atom_ref=atom_ref,
        skill_name="safe-skill",
        weightscore=4,
        fallback_reason="task_unresolved",
    )

    assert candidate.evidence_unit == "atom_fallback"
    assert candidate.task_id is None
    assert candidate.attempt_refs == ()
    assert candidate.learning_eligibility == "needs_review"
    assert candidate.eligibility_reasons == ("atom_fallback:task_unresolved",)


def test_new_schema_rejects_unknown_fields_and_identity_tampering():
    payload = TaskSkillCandidate.from_task_bundle(
        _bundle(), skill_name="safe-skill", weightscore=8
    ).to_dict()
    payload["unknown"] = True
    with pytest.raises(EvidenceCandidateError, match="unknown"):
        TaskSkillCandidate.from_dict(payload)

    payload.pop("unknown")
    payload["candidate_id"] = "candidate_tampered"
    with pytest.raises(EvidenceCandidateError, match="stable evidence identity"):
        TaskSkillCandidate.from_dict(payload)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("evidence_unit", []),
        ("task_lifecycle", []),
        ("learning_eligibility", []),
        ("eligibility_reasons", [{}]),
        ("atom_refs", [{}]),
        ("attempt_refs", [{}]),
    ],
)
def test_new_schema_rejects_type_confusion(field, value):
    payload = TaskSkillCandidate.from_task_bundle(
        _bundle(), skill_name="safe-skill", weightscore=8
    ).to_dict()
    payload[field] = value

    with pytest.raises(EvidenceCandidateError):
        TaskSkillCandidate.from_dict(payload)


def test_new_schema_rejects_inconsistent_task_state():
    payload = TaskSkillCandidate.from_task_bundle(
        _bundle(), skill_name="safe-skill", weightscore=8
    ).to_dict()
    payload["task_lifecycle"] = "open"

    with pytest.raises(EvidenceCandidateError, match="outcome must be unknown"):
        TaskSkillCandidate.from_dict(payload)


def test_candidate_atom_references_have_a_hard_bound():
    bundle = _bundle()
    candidate = TaskSkillCandidate.from_task_bundle(
        bundle, skill_name="safe-skill", weightscore=8
    )
    with pytest.raises(EvidenceCandidateError, match="exceed bound"):
        replace(
            candidate,
            atom_refs=tuple(
                replace(candidate.atom_refs[0], atom_id=f"atom-{index:04d}")
                for index in range(MAX_CANDIDATE_ATOM_REFS + 1)
            ),
        )
