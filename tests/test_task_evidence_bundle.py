from __future__ import annotations

from dataclasses import replace

import pytest

from xskill.tasks.evidence_bundle import (
    MAX_TASK_EVIDENCE_BUNDLE_BYTES,
    TaskEvidenceBundle,
    TaskEvidenceBundleError,
    TaskEvidenceLimits,
    build_task_evidence_bundle,
)
from xskill.tasks.models import (
    AtomRef,
    AttemptRelation,
    EvidenceRange,
    LogicalTask,
    SessionRef,
    TaskAtomMembership,
    TaskAttempt,
    TaskGraphGeneration,
    UsageAllocation,
)
from xskill.tasks.store import TaskGraphStore


def _membership(membership_id, atom, observed_at):
    fixed = ("primary", None, "confirmed", "rules", "v1", ())
    return TaskAtomMembership(membership_id, "task-a", atom, *fixed, observed_at)


def _attempt(
    attempt_id,
    times,
    outcome,
    disposition,
    evidence,
):
    state = ("finished", outcome, "verified", disposition, (evidence,))
    return TaskAttempt(
        attempt_id,
        "task-a",
        *times,
        *state,
        execution_identity={"run_id": attempt_id.replace("attempt", "run")},
    )


def _evidence(evidence_id, session, atom, start, *, skills=()):
    suffix = evidence_id.removeprefix("evidence-")
    return EvidenceRange(
        evidence_id,
        session,
        "trajectory_line",
        start,
        start + 4,
        f"hash-{suffix}",
        atom_ref=atom,
        model={"model_id": "model-a"},
        harness={"name": "runtime-a"},
        skills=skills,
    )


def _generation(*, verified: bool = True) -> TaskGraphGeneration:
    session = SessionRef("tenant", "scope", "source", "traj")
    atom_a = AtomRef("tenant", "scope", "source", "traj", "atom-a")
    atom_b = AtomRef("tenant", "scope", "source", "traj", "atom-b")
    evidence_a = _evidence(
        "evidence-a",
        session,
        atom_a,
        1,
        skills=({"name": "skill-a", "version": "sha-a"},),
    )
    evidence_b = _evidence("evidence-b", session, atom_b, 5)
    task = LogicalTask(
        "task-a",
        "repair",
        "repair and verify",
        "2026-09-01T00:00:00Z",
        lifecycle="closed",
        outcome="succeeded",
        verification="verified" if verified else "unverified",
        user_disposition="accepted",
    )
    memberships = (
        _membership("membership-b", atom_b, "2026-09-01T00:00:02Z"),
        _membership("membership-a", atom_a, "2026-09-01T00:00:01Z"),
    )
    attempts = (
        _attempt(
            "attempt-b",
            ("2026-09-01T00:01:00Z", "2026-09-01T00:02:00Z"),
            "succeeded",
            "accepted",
            evidence_b,
        ),
        _attempt(
            "attempt-a",
            ("2026-09-01T00:00:00Z", "2026-09-01T00:01:00Z"),
            "failed",
            "corrected",
            evidence_a,
        ),
    )
    return TaskGraphGeneration(
        generation_id="generation-a",
        tenant_id="tenant",
        task_scope_id="scope",
        source_revision="source-revision-a",
        generator={"name": "linker", "version": "v1"},
        base_override_seq=0,
        created_at="2026-09-01T00:03:00Z",
        tasks=(task,),
        memberships=memberships,
        relations=(),
        attempts=attempts,
        attempt_relations=(
            AttemptRelation(
                "attempt-relation-a",
                *("attempt-a", "attempt-b", "correction_of"),
                *(None, "confirmed", "rules", "v1", ()),
                "2026-09-01T00:02:00Z",
            ),
        ),
        usage_allocations=(
            UsageAllocation(
                "allocation-a",
                *("usage-a", "execution", "direct", 1.0),
                task_id="task-a",
                attempt_id="attempt-b",
                total_tokens=None,
                cost_usd=None,
                method="evidence_span",
                method_version="v1",
            ),
        ),
    )


def test_bundle_is_deterministic_bounded_and_round_trips_strictly():
    generation = _generation()
    bundle = build_task_evidence_bundle(generation, "task-a")

    atom_ids = [item.atom_ref.atom_id for item in bundle.confirmed_memberships]
    assert atom_ids == ["atom-a", "atom-b"]
    assert [item.attempt_id for item in bundle.attempts] == ["attempt-a", "attempt-b"]
    assert [
        evidence.evidence_id
        for attempt in bundle.attempts
        for evidence in attempt.evidence_ranges
    ] == ["evidence-a", "evidence-b"]
    assert bundle.learning_eligibility == "eligible"
    assert bundle.eligibility_reasons == ("verified_terminal_task",)
    assert bundle.to_dict() == TaskEvidenceBundle.from_dict(bundle.to_dict()).to_dict()
    rebuilt = build_task_evidence_bundle(generation, "task-a")
    assert bundle.bundle_fingerprint == rebuilt.bundle_fingerprint


def test_published_generation_reload_preserves_bundle_fingerprint(tmp_path):
    store = TaskGraphStore(tmp_path)
    store.publish(_generation())
    first = build_task_evidence_bundle(store.load_current(), "task-a")
    reloaded = TaskGraphStore(tmp_path).load_current()

    assert reloaded is not None
    assert build_task_evidence_bundle(reloaded, "task-a").bundle_fingerprint == (
        first.bundle_fingerprint
    )


def test_task_evidence_fingerprint_ignores_generation_metadata():
    generation = _generation()
    first = build_task_evidence_bundle(generation, "task-a")
    rebuilt = build_task_evidence_bundle(
        replace(
            generation,
            generation_id="generation-b",
            source_revision="source-revision-b",
            created_at="2026-09-02T00:00:00Z",
            generator={"name": "linker", "version": "v2"},
        ),
        "task-a",
    )

    assert rebuilt.bundle_fingerprint != first.bundle_fingerprint
    assert rebuilt.task_evidence_fingerprint == first.task_evidence_fingerprint


def test_bundle_rejects_unknown_fields_and_fingerprint_drift():
    payload = build_task_evidence_bundle(_generation(), "task-a").to_dict()
    payload["unknown"] = True
    with pytest.raises(TaskEvidenceBundleError, match="unknown"):
        TaskEvidenceBundle.from_dict(payload)

    payload = build_task_evidence_bundle(_generation(), "task-a").to_dict()
    payload["task"]["summary"] = "changed"
    with pytest.raises(TaskEvidenceBundleError, match="fingerprint"):
        TaskEvidenceBundle.from_dict(payload)


def test_bundle_normalizes_nested_parse_errors():
    payload = build_task_evidence_bundle(_generation(), "task-a").to_dict()
    payload["task"] = []

    with pytest.raises(TaskEvidenceBundleError, match="invalid nested"):
        TaskEvidenceBundle.from_dict(payload)


def test_unresolved_membership_is_observable_but_never_learning_evidence():
    generation = _generation()
    proposed = replace(
        generation.memberships[0],
        membership_id="membership-review",
        decision="needs_review",
    )
    generation = replace(generation, memberships=(*generation.memberships, proposed))
    bundle = build_task_evidence_bundle(generation, "task-a")

    assert [item.membership_id for item in bundle.review_memberships] == [
        "membership-review",
    ]
    assert all(
        item.membership_id != "membership-review"
        for item in bundle.confirmed_memberships
    )
    assert bundle.learning_eligibility == "needs_review"
    assert "unresolved_task_membership" in bundle.eligibility_reasons


def test_unverified_outcome_needs_review_without_inventing_a_score():
    bundle = build_task_evidence_bundle(_generation(verified=False), "task-a")
    assert bundle.learning_eligibility == "needs_review"
    assert bundle.eligibility_reasons == ("task_outcome_not_verified",)
    serialized = bundle.to_dict()
    assert "ux_score" not in str(serialized)
    allocation = serialized["usage_allocations"][0]
    assert allocation["total_tokens"] is None
    assert allocation["cost_usd"] is None
    with pytest.raises(TaskEvidenceBundleError, match="does not match"):
        replace(bundle, learning_eligibility="eligible")


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        (
            {"outcome": "cancelled", "verification": "not_applicable"},
            "task_outcome_cancelled",
        ),
        ({"verification": "contradicted"}, "task_verification_contradicted"),
    ],
)
def test_cancelled_or_contradicted_task_is_ineligible(changes, reason):
    generation = _generation()
    task = replace(generation.tasks[0], **changes)
    bundle = build_task_evidence_bundle(
        replace(generation, tasks=(task,)),
        "task-a",
    )
    assert bundle.learning_eligibility == "ineligible"
    assert bundle.eligibility_reasons == (reason,)


def test_tombstone_and_stale_evidence_fail_closed():
    generation = _generation()
    tombstoned_task = replace(generation.tasks[0], tombstoned=True)
    tombstoned_memberships = tuple(
        replace(item, stale=True) for item in generation.memberships
    )
    tombstoned = replace(
        generation,
        tasks=(tombstoned_task,),
        memberships=tombstoned_memberships,
    )
    with pytest.raises(TaskEvidenceBundleError, match="tombstoned"):
        build_task_evidence_bundle(tombstoned, "task-a")

    stale_range = replace(generation.attempts[0].evidence_ranges[0], stale=True)
    stale_attempt = replace(generation.attempts[0], evidence_ranges=(stale_range,))
    with pytest.raises(TaskEvidenceBundleError, match="stale EvidenceRange"):
        build_task_evidence_bundle(
            replace(generation, attempts=(stale_attempt, generation.attempts[1])),
            "task-a",
        )


def test_evidence_atom_must_have_confirmed_primary_membership():
    generation = _generation()
    with pytest.raises(TaskEvidenceBundleError, match="confirmed primary"):
        build_task_evidence_bundle(
            replace(generation, memberships=(generation.memberships[1],)),
            "task-a",
        )


def test_bundle_rejects_cross_scope_membership_defensively():
    bundle = build_task_evidence_bundle(_generation(), "task-a")
    membership = bundle.confirmed_memberships[0]
    foreign_atom = replace(membership.atom_ref, tenant_id="other-tenant")
    with pytest.raises(TaskEvidenceBundleError, match="crosses TaskScope"):
        replace(
            bundle,
            confirmed_memberships=(
                replace(membership, atom_ref=foreign_atom),
                *bundle.confirmed_memberships[1:],
            ),
        )


@pytest.mark.parametrize(
    ("limits", "message"),
    [
        (TaskEvidenceLimits(memberships=1), "memberships"),
        (TaskEvidenceLimits(attempts=1), "attempts"),
        (TaskEvidenceLimits(evidence_ranges=1), "evidence_ranges"),
    ],
)
def test_bundle_cardinality_is_bounded(limits, message):
    with pytest.raises(TaskEvidenceBundleError, match=message):
        build_task_evidence_bundle(_generation(), "task-a", limits=limits)


def test_bundle_serialized_size_is_bounded():
    generation = _generation()
    oversized_task = replace(
        generation.tasks[0],
        summary="x" * MAX_TASK_EVIDENCE_BUNDLE_BYTES,
    )

    with pytest.raises(TaskEvidenceBundleError, match="serialized_bytes"):
        build_task_evidence_bundle(
            replace(generation, tasks=(oversized_task,)),
            "task-a",
        )
