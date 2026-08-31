from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from xskill.config import normalize_runtime_config
from xskill.pipeline.atom import AtomTask
from xskill.tasks.adjudicator import (
    LLMTaskLinkAdjudicator,
    TaskAdjudicationError,
    TaskLinkCandidate,
    TaskLinkJudgement,
    TaskLinkQuestion,
)
from xskill.tasks.evidence import ScopedAtomEvidence, ScopedTrajectoryEvidence
from xskill.tasks.linker import BoundedTaskLinker
from xskill.tasks.models import AtomRef, SessionRef
from xskill.tasks.scopes import ScopeIdentity
from xskill.tasks.service import TaskGraphService


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _trajectory(*intents: str) -> ScopedTrajectoryEvidence:
    tenant_id = "tenant-test"
    task_scope_id = "task-scope-test"
    source_scope_id = "source-test"
    traj_id = "traj-test"
    session_ref = SessionRef(
        tenant_id=tenant_id,
        task_scope_id=task_scope_id,
        source_scope_id=source_scope_id,
        traj_id=traj_id,
    )
    scope = ScopeIdentity(
        tenant_id=tenant_id,
        task_scope_id=task_scope_id,
        source_scope_id=source_scope_id,
        actor_id="actor-test",
        workspace_id="workspace-test",
    )
    atoms = []
    for index, intent in enumerate(intents, 1):
        atom_id = f"atom-{index}"
        atom = AtomTask(
            atom_id=atom_id,
            traj_id=traj_id,
            offset_start=index,
            offset_end=index + 1,
            intent=intent,
            summary=intent,
            pre_atom_id=f"atom-{index - 1}" if index > 1 else None,
            post_atom_id=f"atom-{index + 1}" if index < len(intents) else None,
            raw_segment=intent,
        )
        atom_ref = AtomRef(
            tenant_id=tenant_id,
            task_scope_id=task_scope_id,
            source_scope_id=source_scope_id,
            traj_id=traj_id,
            atom_id=atom_id,
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
    ):
        self.decision = decision
        self.auto_confirm = auto_confirm
        self.fail = fail
        self.questions: list[TaskLinkQuestion] = []

    def descriptor(self):
        return {
            "name": "test-adjudicator",
            "version": "test-v1",
            "model": "test-model",
            "auto_confirm": self.auto_confirm,
        }

    def judge(self, question: TaskLinkQuestion) -> TaskLinkJudgement:
        self.questions.append(question)
        if self.fail:
            raise RuntimeError("offline")
        task_id = (
            question.candidates[0].task_id if self.decision != "new_task" else None
        )
        return TaskLinkJudgement(self.decision, task_id, "bounded test")


def _live_task_count(generation) -> int:
    return sum(not task.tombstoned for task in generation.tasks)


def test_rules_only_behavior_remains_the_default():
    generation = _build(
        BoundedTaskLinker(),
        "阅读项目",
        "阅读并理解项目",
    )

    assert _live_task_count(generation) == 2
    assert generation.metrics["model_judgement_count"] == 0
    assert "adjudicator" not in generation.generator


def test_model_can_confirm_an_implicit_same_task_within_bounded_candidates():
    adjudicator = _FakeAdjudicator(auto_confirm=True)
    generation = _build(
        BoundedTaskLinker(adjudicator=adjudicator),
        "阅读项目",
        "阅读并理解项目",
    )

    assert _live_task_count(generation) == 1
    assert len(adjudicator.questions) == 1
    assert len(adjudicator.questions[0].candidates) == 1
    assert generation.metrics["model_judgement_count"] == 1
    assert generation.metrics["model_confirmed_membership_count"] == 1
    assert any(
        membership.decided_by == "model:test-v1:same_task"
        for membership in generation.memberships
    )


def test_model_same_task_stays_proposed_without_explicit_auto_confirm():
    adjudicator = _FakeAdjudicator(auto_confirm=False)
    generation = _build(
        BoundedTaskLinker(adjudicator=adjudicator),
        "阅读项目",
        "阅读并理解项目",
    )

    assert _live_task_count(generation) == 2
    assert generation.metrics["model_proposed_membership_count"] == 1
    assert any(
        membership.decision == "proposed"
        and membership.decided_by == "model:test-v1:same_task"
        for membership in generation.memberships
    )


def test_model_can_leave_a_bounded_candidate_for_human_review():
    adjudicator = _FakeAdjudicator("needs_review", auto_confirm=True)
    generation = _build(
        BoundedTaskLinker(adjudicator=adjudicator),
        "阅读项目",
        "阅读并理解项目",
    )

    assert _live_task_count(generation) == 2
    assert generation.metrics["model_needs_review_membership_count"] == 1
    assert any(
        membership.decision == "needs_review"
        and membership.decided_by == "model:test-v1:needs_review"
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
    assert generation.metrics["model_judgement_count"] == 0


def test_first_model_failure_opens_build_local_circuit_and_falls_back():
    adjudicator = _FakeAdjudicator(auto_confirm=True, fail=True)
    generation = _build(
        BoundedTaskLinker(adjudicator=adjudicator),
        "阅读项目",
        "理解项目",
        "分析项目",
    )

    assert _live_task_count(generation) == 3
    assert len(adjudicator.questions) == 1
    assert generation.metrics["model_judgement_count"] == 1
    assert generation.metrics["model_judgement_failure_count"] == 1


def test_model_judgements_have_a_hard_per_build_bound():
    adjudicator = _FakeAdjudicator("new_task", auto_confirm=True)
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


class _EscapingAdjudicator(_FakeAdjudicator):
    def judge(self, question: TaskLinkQuestion) -> TaskLinkJudgement:
        self.questions.append(question)
        return TaskLinkJudgement("same_task", "task-outside-candidates", "invalid")


def test_linker_rejects_candidate_escape_from_any_adjudicator():
    adjudicator = _EscapingAdjudicator(auto_confirm=True)
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


class _FakeLLM:
    model = "fake-model"

    def __init__(self, response: str):
        self.response = response
        self.calls = []

    def chat(self, prompt: str, system: str = "") -> str:
        self.calls.append((prompt, system))
        return self.response


def _question() -> TaskLinkQuestion:
    return TaskLinkQuestion(
        tenant_id="tenant-test",
        task_scope_id="scope-test",
        source_scope_id="source-test",
        traj_id="traj-test",
        atom_id="atom-test",
        intent="按照你的建议推进",
        summary="继续既有目标",
        explicit_marker="",
        candidates=(
            TaskLinkCandidate(
                task_id="task-allowed",
                title="完善 Task linker",
                summary="增加真实评测与模型判别",
                lexical_score=0.1,
                same_session_recent=True,
            ),
        ),
    )


def test_llm_adjudicator_accepts_only_bounded_structured_output():
    llm = _FakeLLM(
        json.dumps(
            {
                "decision": "same_task",
                "task_id": "task-allowed",
                "reason": "目标与完成条件保持一致",
            }
        )
    )
    adjudicator = LLMTaskLinkAdjudicator(llm, auto_confirm=False)

    judgement = adjudicator.judge(_question())

    assert judgement.decision == "same_task"
    assert judgement.task_id == "task-allowed"
    prompt, system = llm.calls[0]
    assert json.loads(prompt)["candidates"][0]["task_id"] == "task-allowed"
    assert "untrusted evidence" in system


def test_adjudicator_descriptor_tracks_output_config_without_exposing_secrets():
    client = SimpleNamespace(
        model="model-v1",
        base_url="https://private.example/v1/",
        api_key="secret-key-must-not-leak",
        max_tokens=800,
        temperature=0.0,
        rate_limit_cfg={"rpm": 10},
    )
    descriptor = LLMTaskLinkAdjudicator(client).descriptor()

    assert descriptor["model"] == "model-v1"
    assert descriptor["max_tokens"] == 800
    assert descriptor["temperature"] == 0.0
    assert descriptor["endpoint_fingerprint"]
    assert descriptor["rate_limit_fingerprint"]
    serialized = json.dumps(descriptor, sort_keys=True)
    assert "private.example" not in serialized
    assert "secret-key-must-not-leak" not in serialized

    changed = SimpleNamespace(**{**vars(client), "temperature": 0.2})
    assert LLMTaskLinkAdjudicator(changed).descriptor() != descriptor


def test_llm_adjudicator_rejects_candidate_escape():
    llm = _FakeLLM(
        json.dumps(
            {
                "decision": "same_task",
                "task_id": "task-outside-scope",
                "reason": "invalid",
            }
        )
    )

    with pytest.raises(TaskAdjudicationError, match="outside bounded"):
        LLMTaskLinkAdjudicator(llm).judge(_question())


@pytest.mark.parametrize(
    "response",
    [
        {"decision": "needs_review", "task_id": None, "reason": "ambiguous"},
        {"decision": "new_task", "task_id": None},
        {
            "decision": "new_task",
            "task_id": None,
            "reason": "separate objective",
            "confidence": 0.9,
        },
    ],
)
def test_llm_adjudicator_rejects_incomplete_or_unbounded_contracts(response):
    llm = _FakeLLM(json.dumps(response))

    with pytest.raises(TaskAdjudicationError):
        LLMTaskLinkAdjudicator(llm).judge(_question())


@pytest.mark.parametrize(
    ("config", "message"),
    [
        ({"llm_adjudication": []}, "必须是 mapping"),
        ({"llm_adjudication": {"enabled": "yes"}}, "enabled 必须是布尔"),
        (
            {"llm_adjudication": {"max_judgements_per_build": 0}},
            "max_judgements_per_build 必须是正整数",
        ),
        ({"llm_adjudication": {"llm": []}}, "llm 必须是 mapping"),
    ],
)
def test_runtime_config_validates_task_adjudication(config, message):
    with pytest.raises(ValueError, match=message):
        normalize_runtime_config(
            {
                "llm": {},
                "embedding": {},
                "task_graph": config,
            }
        )


def test_service_builds_opt_in_adjudicator_without_changing_global_llm_budget(
    tmp_path,
):
    config = {
        "llm": {
            "base_url": "https://example.test/v1",
            "model": "base-model",
            "api_key": "test-key",
            "max_tokens": 10000,
        },
        "task_graph": {
            "llm_adjudication": {
                "enabled": True,
                "auto_confirm": False,
                "max_judgements_per_build": 7,
            },
        },
    }

    usage_ledger = object()
    service = TaskGraphService(
        state_root=tmp_path,
        config=config,
        usage_ledger=usage_ledger,
    )

    assert service.linker.adjudicator is not None
    assert service.linker.max_model_judgements_per_build == 7
    assert service.linker.adjudicator.llm_client.max_tokens == 800
    assert service.linker.adjudicator.llm_client.usage_ledger is usage_ledger
    assert config["llm"]["max_tokens"] == 10000


@pytest.mark.parametrize(
    "task_config",
    [
        {"llm_adjudication": []},
        {"llm_adjudication": {"llm": []}},
    ],
)
def test_service_rejects_malformed_adjudication_config_while_disabled(
    tmp_path,
    task_config,
):
    with pytest.raises(ValueError, match="must be a mapping"):
        TaskGraphService(
            state_root=tmp_path,
            config={"task_graph": task_config},
        )
