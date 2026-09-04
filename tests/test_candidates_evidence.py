from __future__ import annotations

import pytest

from xskill.skill.candidates import (
    add_evidence_candidates,
    load_candidates,
    save_candidates,
)
from xskill.skill.evidence_candidate_refs import EvidenceCandidateError
from xskill.skill.evidence_candidates import TaskSkillCandidate


def _candidate(skill_name: str, *, weightscore: int) -> TaskSkillCandidate:
    return TaskSkillCandidate.from_atom_fallback(
        atom_id="atom-a",
        skill_name=skill_name,
        weightscore=weightscore,
        fallback_reason="task_graph_disabled",
    )


def test_evidence_candidate_uses_existing_atomic_buffer_and_upsert_semantics(
    tmp_path,
):
    skill_dir = tmp_path / "safe-skill"
    save_candidates(
        skill_dir,
        {"candidates": [{"atom_id": "legacy", "weightscore": 3}]},
    )

    assert add_evidence_candidates(
        skill_dir, (_candidate("safe-skill", weightscore=4),)
    ) == ([True], 7)
    assert add_evidence_candidates(
        skill_dir, (_candidate("safe-skill", weightscore=9),)
    ) == ([False], 12)

    data = load_candidates(skill_dir)
    assert len(data["candidates"]) == 2
    assert data["candidates"][0]["atom_id"] == "legacy"
    assert data["candidates"][1]["weightscore"] == 9


def test_evidence_candidate_cannot_cross_the_containing_skill(tmp_path):
    skill_dir = tmp_path / "safe-skill"

    with pytest.raises(EvidenceCandidateError, match="containing Skill"):
        add_evidence_candidates(
            skill_dir,
            (_candidate("another-skill", weightscore=5),),
        )

    assert not (skill_dir / ".candidates.yml").exists()
