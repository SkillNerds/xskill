from __future__ import annotations

from dataclasses import replace

import pytest

from xskill.skill.evidence_candidate_refs import EvidenceCandidateError
from xskill.skill.evidence_candidates import (
    TaskSkillCandidate,
    migrate_legacy_atom_candidate,
    migrate_legacy_candidate_buffer,
    upsert_evidence_candidates,
)


def _fallback(*, weightscore: int = 5) -> TaskSkillCandidate:
    return TaskSkillCandidate.from_atom_fallback(
        atom_id="atom-a",
        skill_name="safe-skill",
        weightscore=weightscore,
        fallback_reason="task_unresolved",
    )


def test_legacy_atom_migration_is_deterministic_and_non_destructive():
    legacy = {"atom_id": "atom-a", "weightscore": 5, "note": "keep me"}

    first = migrate_legacy_atom_candidate(legacy, skill_name="safe-skill")
    second = migrate_legacy_atom_candidate(dict(legacy), skill_name="safe-skill")

    assert first == second
    assert first.fallback_reason == "legacy_atom_candidate"
    assert first.note == "keep me"
    assert legacy == {"atom_id": "atom-a", "weightscore": 5, "note": "keep me"}


def test_buffer_migration_preserves_legacy_patterns_and_does_not_mutate_input():
    pattern = {"pattern": "legacy pattern", "supporting_trajs": ["traj-a"]}
    source = {
        "metadata": {"owner": "test"},
        "candidates": [pattern, {"atom_id": "atom-a", "weightscore": 4}],
    }

    migrated, count = migrate_legacy_candidate_buffer(
        source, skill_name="safe-skill"
    )

    assert count == 1
    assert migrated["metadata"] == source["metadata"]
    assert migrated["candidates"][0] == pattern
    assert migrated["candidates"][1]["evidence_unit"] == "atom_fallback"
    assert "schema_version" not in source["candidates"][1]
    migrated["metadata"]["owner"] = "changed"
    migrated["candidates"][0]["supporting_trajs"].append("traj-b")
    assert source["metadata"]["owner"] == "test"
    assert source["candidates"][0]["supporting_trajs"] == ["traj-a"]


class _CountingList(list):
    iterations = 0

    def __iter__(self):
        self.iterations += 1
        return super().__iter__()


def test_upsert_replaces_stable_identity_in_one_scan_without_double_counting():
    original = _fallback(weightscore=4)
    changed = replace(original, weightscore=9)
    buffer = _CountingList((original.to_dict(),))
    data = {"candidates": buffer}

    flags, total = upsert_evidence_candidates(data, (changed,))

    assert flags == [False]
    assert total == 9
    assert len(buffer) == 1
    assert buffer[0]["weightscore"] == 9
    assert buffer.iterations == 1


def test_upsert_keeps_legacy_entries_and_rejects_duplicate_input():
    candidate = _fallback()
    data = {"candidates": [{"atom_id": "legacy", "weightscore": 3}]}

    assert upsert_evidence_candidates(data, (candidate,)) == ([True], 8)
    assert data["candidates"][0]["atom_id"] == "legacy"
    with pytest.raises(EvidenceCandidateError, match="duplicate stable identities"):
        upsert_evidence_candidates(data, (candidate, candidate))

    other_skill = TaskSkillCandidate.from_atom_fallback(
        atom_id="atom-b",
        skill_name="other-skill",
        weightscore=5,
        fallback_reason="task_unresolved",
    )
    with pytest.raises(EvidenceCandidateError, match="cannot cross Skills"):
        upsert_evidence_candidates(
            {"candidates": []}, (candidate, other_skill)
        )


@pytest.mark.parametrize(
    "legacy",
    [
        {"weightscore": 5},
        {"atom_id": "atom-a", "weightscore": 5, "extra": True},
        {"atom_id": "atom-a", "weightscore": True},
    ],
)
def test_legacy_migration_fails_closed_on_ambiguous_records(legacy):
    with pytest.raises(EvidenceCandidateError):
        migrate_legacy_atom_candidate(legacy, skill_name="safe-skill")


def test_buffer_migration_rejects_cross_skill_versioned_candidates():
    candidate = _fallback().to_dict()
    candidate["skill_name"] = "another-skill"

    with pytest.raises(EvidenceCandidateError):
        migrate_legacy_candidate_buffer(
            {"candidates": [candidate]}, skill_name="safe-skill"
        )
