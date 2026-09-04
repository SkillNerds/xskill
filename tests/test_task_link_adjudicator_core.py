from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from xskill.tasks.adjudicator import (
    MAX_TASK_LINK_CANDIDATES,
    LLMTaskLinkAdjudicator,
    TaskAdjudicationError,
    TaskLinkCandidate,
    TaskLinkJudgement,
    TaskLinkQuestion,
)


class _FakeLLM:
    model = "fake-model"

    def __init__(self, response: str):
        self.response = response
        self.calls = []

    def chat(self, prompt: str, system: str = "") -> str:
        self.calls.append((prompt, system))
        return self.response


def _response(decision="same_task", task_id="task-allowed", reason="same_objective"):
    return json.dumps(
        {"decision": decision, "task_id": task_id, "reason_code": reason}
    )


def _question(*, candidates=None) -> TaskLinkQuestion:
    if candidates is None:
        candidates = (
            TaskLinkCandidate(
                task_id="task-allowed",
                title="完善 Task linker",
                summary="增加真实评测与模型判别",
                lexical_score=0.1,
                same_session_recent=True,
            ),
        )
    return TaskLinkQuestion(
        tenant_id="tenant-test",
        task_scope_id="scope-test",
        source_scope_id="source-test",
        traj_id="traj-test",
        atom_id="atom-test",
        intent="按照你的建议推进",
        summary="继续既有目标",
        explicit_marker="",
        candidates=candidates,
    )


def test_adjudicator_emits_a_bounded_prompt_and_accepts_structured_output():
    llm = _FakeLLM(_response())

    judgement = LLMTaskLinkAdjudicator(llm).judge(_question())

    assert judgement == TaskLinkJudgement(
        "same_task", "task-allowed", "same_objective"
    )
    prompt, system = llm.calls[0]
    assert json.loads(prompt)["candidates"][0]["task_id"] == "task-allowed"
    assert "untrusted evidence" in system


def test_adjudicator_accepts_abstain_without_selecting_a_candidate():
    llm = _FakeLLM(_response("abstain", None, "insufficient_evidence"))

    judgement = LLMTaskLinkAdjudicator(llm).judge(_question())

    assert judgement == TaskLinkJudgement("abstain", None, "insufficient_evidence")


def test_descriptor_tracks_output_config_without_exposing_secrets():
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


def test_audit_contract_excludes_untrusted_task_and_atom_text():
    question = _question()

    serialized = json.dumps(question.to_audit_dict(), ensure_ascii=False)

    assert "按照你的建议推进" not in serialized
    assert "完善 Task linker" not in serialized
    assert question.candidates[0].task_id in serialized


def test_adjudicator_rejects_candidate_escape():
    llm = _FakeLLM(_response(task_id="task-outside-scope"))

    with pytest.raises(TaskAdjudicationError, match="outside bounded"):
        LLMTaskLinkAdjudicator(llm).judge(_question())


@pytest.mark.parametrize(
    "response",
    [
        {"decision": "same_task", "task_id": None, "reason_code": "same_objective"},
        {"decision": "new_task", "task_id": None},
        {
            "decision": "new_task",
            "task_id": None,
            "reason_code": "separate_objective",
            "confidence": 0.9,
        },
        {
            "decision": "abstain",
            "task_id": None,
            "reason_code": "free_text_is_not_allowed",
        },
    ],
)
def test_adjudicator_rejects_incomplete_or_unbounded_output(response):
    with pytest.raises(TaskAdjudicationError):
        LLMTaskLinkAdjudicator(_FakeLLM(json.dumps(response))).judge(_question())


@pytest.mark.parametrize(
    "judgement",
    [
        ("same_task", "task-allowed", "separate_objective"),
        ("new_task", None, "same_objective"),
        ("abstain", None, "continuation"),
    ],
)
def test_judgement_rejects_semantically_contradictory_reason(judgement):
    with pytest.raises(TaskAdjudicationError, match="contradicts"):
        TaskLinkJudgement(*judgement)


def test_adjudicator_rejects_duplicate_or_oversized_candidate_sets_before_llm_call():
    duplicate = replace(_question().candidates[0], title="duplicate")
    llm = _FakeLLM(_response())
    with pytest.raises(TaskAdjudicationError, match="unique"):
        LLMTaskLinkAdjudicator(llm).judge(
            _question(candidates=(_question().candidates[0], duplicate))
        )
    assert llm.calls == []

    candidates = tuple(
        replace(_question().candidates[0], task_id=f"task-{index}")
        for index in range(MAX_TASK_LINK_CANDIDATES + 1)
    )
    with pytest.raises(TaskAdjudicationError, match="limit exceeded"):
        LLMTaskLinkAdjudicator(llm).judge(_question(candidates=candidates))
    assert llm.calls == []
