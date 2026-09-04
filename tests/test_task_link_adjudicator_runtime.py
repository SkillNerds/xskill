from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace

import pytest

from xskill.config import normalize_runtime_config
from xskill.tasks.adjudicator import TaskLinkCandidate, TaskLinkQuestion
from xskill.tasks.service import TaskGraphService
from xskill.usage import PriceTable, UsageLedger


class _FakeCompletions:
    def create(self, **request):
        assert request["max_tokens"] == 800
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=json.dumps(
                            {
                                "decision": "same_task",
                                "task_id": "task-allowed",
                                "reason_code": "same_objective",
                            }
                        )
                    )
                )
            ],
            usage=SimpleNamespace(
                prompt_tokens=20,
                completion_tokens=5,
                total_tokens=25,
            ),
        )


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
            {"llm": {}, "embedding": {}, "task_graph": config}
        )


def test_service_builds_opt_in_adjudicator_with_a_separate_bounded_budget(
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


def test_service_allows_a_lower_adjudication_output_budget(tmp_path):
    service = TaskGraphService(
        state_root=tmp_path,
        config={
            "llm": {
                "base_url": "https://example.test/v1",
                "model": "base-model",
            },
            "task_graph": {
                "llm_adjudication": {
                    "enabled": True,
                    "llm": {"max_tokens": 300},
                },
            },
        },
    )

    assert service.linker.adjudicator.llm_client.max_tokens == 300


def test_adjudication_usage_is_persisted_with_task_and_atom_scope(tmp_path):
    db_path = tmp_path / "registry.db"
    ledger = UsageLedger(
        PriceTable(
            {},
            {"default": {"input_per_1m": 1.0, "output_per_1m": 3.0}},
        ),
        db_path=db_path,
    )
    service = TaskGraphService(
        state_root=tmp_path,
        db_path=db_path,
        config={
            "llm": {
                "base_url": "https://example.test/v1",
                "model": "base-model",
            },
            "task_graph": {"llm_adjudication": {"enabled": True}},
        },
        usage_ledger=ledger,
    )
    adjudicator = service.linker.adjudicator
    assert adjudicator is not None
    adjudicator.llm_client._client = SimpleNamespace(
        chat=SimpleNamespace(completions=_FakeCompletions())
    )

    adjudicator.judge(
        TaskLinkQuestion(
            tenant_id="tenant-test",
            task_scope_id="task-scope-test",
            source_scope_id="source-test",
            traj_id="traj-test",
            atom_id="atom-test",
            intent="continue the objective",
            summary="same bounded objective",
            explicit_marker="continuation",
            candidates=(
                TaskLinkCandidate(
                    task_id="task-allowed",
                    title="existing objective",
                    summary="bounded candidate",
                    lexical_score=0.5,
                    same_session_recent=True,
                ),
            ),
        )
    )

    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            "SELECT step,model,total,cost_usd,usage_plane,allocation_mode,"
            "tenant_id,task_scope_id,source_scope_id,traj_id,atom_id "
            "FROM llm_usage"
        ).fetchone()
    assert row == (
        "task_link",
        "base-model",
        25,
        pytest.approx(0.000035),
        "xskill_processing",
        "direct",
        "tenant-test",
        "task-scope-test",
        "source-test",
        "traj-test",
        "atom-test",
    )
    assert ledger.snapshot()["by_step"]["task_link"] == {
        "calls": 1,
        "tokens": 25,
        "cost_usd": 0.000035,
    }


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


@pytest.mark.parametrize("value", [True, 0, -1, 8.5, "800"])
def test_service_rejects_invalid_adjudication_output_budgets(tmp_path, value):
    with pytest.raises(ValueError, match="max_tokens must be a positive integer"):
        TaskGraphService(
            state_root=tmp_path,
            config={
                "llm": {
                    "base_url": "https://example.test/v1",
                    "model": "base-model",
                },
                "task_graph": {
                    "llm_adjudication": {
                        "enabled": True,
                        "llm": {"max_tokens": value},
                    },
                },
            },
        )
