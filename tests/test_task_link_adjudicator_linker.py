from __future__ import annotations

import hashlib
import json
from pathlib import Path

from xskill.pipeline.atom import AtomTask
from xskill.tasks.adjudicator import (
    MAX_TASK_LINK_CANDIDATES,
    TaskLinkJudgement,
    TaskLinkQuestion,
)
from xskill.tasks.evidence import ScopedAtomEvidence, ScopedTrajectoryEvidence
from xskill.tasks.linker import BoundedTaskLinker
from xskill.tasks.models import AtomRef, SessionRef
from xskill.tasks.scopes import ScopeIdentity
from xskill.tasks.store import TaskGraphStore


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _trajectory(*intents: str) -> ScopedTrajectoryEvidence:
    session_ref = SessionRef("tenant-test", "task-scope-test", "source-test", "traj-test")
    scope = ScopeIdentity(
        tenant_id="tenant-test",
        task_scope_id="task-scope-test",
        source_scope_id="source-test",
        actor_id="actor-test",
        workspace_id="workspace-test",
    )
    atoms = []
    for index, intent in enumerate(intents, 1):
        atom_id = f"atom-{index}"
        atom = AtomTask(
            atom_id=atom_id,
            traj_id="traj-test",
            offset_start=index,
            offset_end=index + 1,
            intent=intent,
            summary=intent,
            pre_atom_id=f"atom-{index - 1}" if index > 1 else None,
            post_atom_id=f"atom-{index + 1}" if index < len(intents) else None,
            raw_segment=intent,
        )
        atom_ref = AtomRef(
            "tenant-test",
            "task-scope-test",
            "source-test",
            "traj-test",
            atom_id,
        )
        atoms.append(
            ScopedAtomEvidence(
                atom=atom,
                atom_ref=atom_ref,
                atom_hash=_sha(intent),
                session_hash="session-hash",
                source_model={"provider": "test", "model_id": "test"},
                source_harness={"name": "test"},
                observed_at=f"2026-08-31T00:00:{index:02d}+00:00",
            )
        )
    return ScopedTrajectoryEvidence(
        watch_dir_id=1,
        watch_dir_path=Path("/fixture"),
        filename="traj-test.md",
        scope=scope,
        session_ref=session_ref,
        session_hash="session-hash",
        metadata={},
        atoms=tuple(atoms),
        usage_events=(),
        explicit_outcome={},
    )


def _build(linker: BoundedTaskLinker, *intents: str):
    return linker.build(
        tenant_id="tenant-test",
        task_scope_id="task-scope-test",
        trajectories=(_trajectory(*intents),),
        source_revision="revision-test",
    )


class _FakeAdjudicator:
    def __init__(
        self,
        decision: str = "same_task",
        *,
        auto_confirm: bool = True,
        fail: bool = False,
        select_candidate: bool = True,
    ):
        self.decision = decision
        self.auto_confirm = auto_confirm
        self.fail = fail
        self.select_candidate = select_candidate
        self.questions: list[TaskLinkQuestion] = []

    def descriptor(self):
        return {
            "name": "test-adjudicator",
            "version": "test-v1",
            "model": "test-model",
            "auto_confirm": self.auto_confirm,
        }

    def judge(
        self,
        question: TaskLinkQuestion,
        *,
        timeout_seconds: float,
    ) -> TaskLinkJudgement:
        assert timeout_seconds > 0
        self.questions.append(question)
        if self.fail:
            raise RuntimeError("offline detail must not enter audit")
        task_id = None
        if self.decision == "same_task" or (
            self.decision == "abstain" and self.select_candidate
        ):
            task_id = question.candidates[0].task_id
        reason_code = {
            "same_task": "same_objective",
            "new_task": "separate_objective",
            "abstain": "insufficient_evidence",
        }[self.decision]
        return TaskLinkJudgement(self.decision, task_id, reason_code)


def _live_task_count(generation) -> int:
    return sum(not task.tombstoned for task in generation.tasks)


def test_rules_only_behavior_remains_the_default():
    generation = _build(BoundedTaskLinker(), "阅读项目", "阅读并理解项目")

    assert _live_task_count(generation) == 2
    assert generation.metrics["model_judgement_count"] == 0
    assert "adjudicator" not in generation.generator


def test_model_can_confirm_only_a_bounded_candidate():
    adjudicator = _FakeAdjudicator(auto_confirm=True)
    generation = _build(
        BoundedTaskLinker(adjudicator=adjudicator),
        "阅读项目",
        "阅读并理解项目",
    )

    assert _live_task_count(generation) == 1
    assert len(adjudicator.questions) == 1
    assert len(adjudicator.questions[0].candidates) == 1
    assert generation.metrics["model_confirmed_membership_count"] == 1


def test_unconfirmed_model_link_stays_proposed_and_audit_is_private(tmp_path):
    adjudicator = _FakeAdjudicator(auto_confirm=False)
    generation = _build(
        BoundedTaskLinker(adjudicator=adjudicator),
        "阅读项目",
        "阅读并理解项目",
    )

    assert _live_task_count(generation) == 2
    assert generation.metrics["model_proposed_membership_count"] == 1
    audit = generation.metrics["model_adjudications"][0]
    assert audit["status"] == "succeeded"
    assert audit["decision"] == "same_task"
    assert "阅读" not in json.dumps(audit, ensure_ascii=False)

    store = TaskGraphStore(tmp_path / "scope")
    store.publish(generation)
    loaded = store.load_current()
    assert loaded is not None
    assert loaded.metrics["model_adjudications"] == generation.metrics[
        "model_adjudications"
    ]


def test_model_abstention_is_visible_for_review_without_forcing_a_link():
    adjudicator = _FakeAdjudicator("abstain", auto_confirm=True)
    generation = _build(
        BoundedTaskLinker(adjudicator=adjudicator),
        "阅读项目",
        "阅读并理解项目",
    )

    assert _live_task_count(generation) == 2
    assert generation.metrics["model_abstain_judgement_count"] == 1
    assert generation.metrics["model_needs_review_membership_count"] == 1
    assert any(
        membership.decision == "needs_review"
        for membership in generation.memberships
    )


def test_explicit_high_precision_rule_skips_model_call():
    adjudicator = _FakeAdjudicator(auto_confirm=True)
    generation = _build(
        BoundedTaskLinker(adjudicator=adjudicator),
        "修复登录认证",
        "继续修复登录认证",
    )

    assert _live_task_count(generation) == 1
    assert adjudicator.questions == []


def test_first_model_failure_opens_build_local_circuit_and_falls_back():
    adjudicator = _FakeAdjudicator(fail=True)
    generation = _build(
        BoundedTaskLinker(adjudicator=adjudicator),
        "阅读项目",
        "理解项目",
        "分析项目",
    )

    assert _live_task_count(generation) == 3
    assert len(adjudicator.questions) == 1
    assert generation.metrics["model_judgement_failure_count"] == 1
    audit = generation.metrics["model_adjudications"][0]
    assert audit["error_type"] == "RuntimeError"
    assert "offline detail" not in json.dumps(audit)


def test_model_judgements_have_a_hard_per_build_bound():
    adjudicator = _FakeAdjudicator("new_task")
    generation = _build(
        BoundedTaskLinker(
            adjudicator=adjudicator,
            max_model_judgements_per_build=2,
        ),
        "目标一",
        "目标二",
        "目标三",
        "目标四",
    )

    assert _live_task_count(generation) == 4
    assert len(adjudicator.questions) == 2
    assert generation.metrics["model_judgement_count"] == 2


def test_linker_caps_candidates_before_calling_the_adjudicator():
    adjudicator = _FakeAdjudicator("new_task")
    _build(
        BoundedTaskLinker(
            top_k=32,
            recent_k=32,
            adjudicator=adjudicator,
        ),
        *(f"独立目标 {index}" for index in range(12)),
    )

    assert adjudicator.questions
    assert max(len(question.candidates) for question in adjudicator.questions) == (
        MAX_TASK_LINK_CANDIDATES
    )


class _EscapingAdjudicator(_FakeAdjudicator):
    def judge(
        self,
        question: TaskLinkQuestion,
        *,
        timeout_seconds: float,
    ) -> TaskLinkJudgement:
        assert timeout_seconds > 0
        self.questions.append(question)
        return TaskLinkJudgement(
            "same_task", "task-outside-candidates", "same_objective"
        )


def test_linker_rejects_candidate_escape_from_any_adjudicator():
    adjudicator = _EscapingAdjudicator()
    generation = _build(
        BoundedTaskLinker(adjudicator=adjudicator),
        "阅读项目",
        "阅读并理解项目",
    )

    assert _live_task_count(generation) == 2
    assert generation.metrics["model_judgement_failure_count"] == 1
    assert not any(
        membership.task_id == "task-outside-candidates"
        for membership in generation.memberships
    )


def test_model_judgements_stop_at_the_build_wall_clock_budget():
    now = [0.0]

    class _BudgetAdjudicator(_FakeAdjudicator):
        def judge(self, question, *, timeout_seconds):
            judgement = super().judge(
                question,
                timeout_seconds=timeout_seconds,
            )
            now[0] += timeout_seconds
            return judgement

    adjudicator = _BudgetAdjudicator("new_task")
    generation = _build(
        BoundedTaskLinker(
            adjudicator=adjudicator,
            max_model_judgements_per_build=8,
            max_model_wall_time_seconds=1.0,
            _clock=lambda: now[0],
        ),
        "目标一",
        "目标二",
        "目标三",
        "目标四",
    )

    assert len(adjudicator.questions) == 1
    assert generation.metrics["model_judgement_budget_exhausted_count"] == 1
