"""Contracts for the deterministic Logical Task and Attempt replay."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

from scripts.bench.task_graph_replay import evaluate_linker
from scripts.bench.task_graph_replay.evaluate import (
    ERROR_EXAMPLE_LIMIT,
    TaskReplayValidationError,
    evaluate_suite,
    load_suite,
    main,
    render_text,
    validate_suite,
)

FIXTURE_DIR = (
    Path(__file__).parent.parent
    / "scripts"
    / "bench"
    / "task_graph_replay"
    / "fixtures"
)
BASELINE_PATH = FIXTURE_DIR / "baseline_v1.json"
REPORT_PATH = FIXTURE_DIR / "baseline_v1.report.json"
LINKER_FIXTURE_PATH = FIXTURE_DIR / "linker_structure_v1.json"
LINKER_REPORT_PATH = FIXTURE_DIR / "linker_structure_v1.report.json"


pytestmark = pytest.mark.algorithm_replay


def test_baseline_report_matches_checked_in_snapshot():
    report = evaluate_suite(load_suite(BASELINE_PATH))

    assert report == json.loads(REPORT_PATH.read_text(encoding="utf-8"))


def test_baseline_exposes_expected_regression_signals():
    report = evaluate_suite(load_suite(BASELINE_PATH))
    metrics = report["metrics"]

    assert metrics["task_grouping"]["pairwise"]["f1"] < 1.0
    assert metrics["task_grouping"]["b3"]["f1"] < 1.0
    assert metrics["relations"]["macro"]["f1"] < 1.0
    assert set(metrics["relations"]["by_type"]) >= {
        "task:parent",
        "attempt:continuation_of",
        "attempt:correction_of",
        "attempt:retry_of",
    }
    assert metrics["attempt_detection"]["precision"] < 1.0
    assert metrics["membership_detection"]["f1"] == 1.0
    assert metrics["attempt_outcome"]["accuracy"] < 1.0
    assert metrics["confidence"]["membership"]["brier"] > 0.0
    assert metrics["confidence"]["attempt_outcome"]["ece"] > 0.0
    assert all(
        values["accuracy"] == 1.0 and values["coverage"] == 1.0
        for values in metrics["execution_attribution"].values()
    )
    assert metrics["evidence_coverage"] < 1.0
    assert metrics["usage"]["execution"]["conservation_rate"] == 1.0
    assert metrics["usage"]["xskill_processing"]["conservation_rate"] == 1.0
    assert metrics["usage"]["execution"]["shared_fraction"] > 0.0
    assert metrics["usage"]["execution"]["unattributed_fraction"] > 0.0
    assert metrics["usage"]["execution"]["unavailable_events"] == 1
    assert metrics["usage"]["xskill_processing"]["estimated_events"] == 1
    assert report["error_count"] > 0


def test_valid_false_merge_reduces_pairwise_precision():
    suite = load_suite(BASELINE_PATH)
    case = suite["cases"][1]
    case["prediction"]["memberships"][1]["task_id"] = "pred-task-a"
    case["prediction"]["attempts"][1]["task_id"] = "pred-task-a"
    case["prediction"]["usage_allocations"][1]["task_id"] = "pred-task-a"

    metrics = evaluate_suite(suite)["cases"][1]["metrics"]

    assert metrics["task_grouping"]["pairwise"]["precision"] < 1.0


def test_numeric_usage_imbalance_is_reported_without_hiding_the_event():
    suite = load_suite(BASELINE_PATH)
    allocation = suite["cases"][2]["prediction"]["usage_allocations"][0]
    allocation["total_tokens"] = 59

    case = evaluate_suite(suite)["cases"][2]

    assert case["metrics"]["usage"]["execution"]["conservation_rate"] == 0.0
    assert any(error["type"] == "usage_not_conserved" for error in case["errors"])


@pytest.mark.parametrize(
    "field",
    ["prompt_tokens", "completion_tokens", "total_tokens", "cache_read_tokens"],
)
def test_each_token_component_participates_in_conservation(field):
    suite = load_suite(BASELINE_PATH)
    allocation = suite["cases"][0]["prediction"]["usage_allocations"][0]
    allocation[field] -= 1

    case = evaluate_suite(suite)["cases"][0]

    assert case["metrics"]["usage"]["execution"]["conservation_rate"] == 0.0
    error = next(
        error for error in case["errors"] if error["type"] == "usage_not_conserved"
    )
    assert error["token_deltas"][field] == -1


def test_missing_singleton_membership_reduces_detection_and_confidence_coverage():
    suite = load_suite(BASELINE_PATH)
    suite["cases"][5]["prediction"]["memberships"].pop(0)

    case = evaluate_suite(suite)["cases"][5]

    assert case["metrics"]["task_grouping"]["b3"]["f1"] == 1.0
    assert case["metrics"]["membership_detection"]["recall"] == 0.666667
    assert case["metrics"]["confidence"]["membership"]["coverage"] == 0.666667
    assert any(error["type"] == "membership_missing" for error in case["errors"])


def test_parent_and_inverse_subtask_are_the_same_semantic_relation():
    suite = load_suite(BASELINE_PATH)
    suite["cases"][3]["prediction"]["task_relations"][0] = {
        "from_task_id": "pred-child",
        "to_task_id": "pred-parent",
        "relation_type": "subtask",
        "confidence": 0.9,
    }

    case = evaluate_suite(suite)["cases"][3]

    assert case["metrics"]["relations"]["micro"]["f1"] == 1.0
    assert not any(error["type"].startswith("relation_") for error in case["errors"])


@pytest.mark.parametrize("field", ["model", "harness", "skills", "execution_identity"])
def test_execution_attribution_changes_are_reported(field):
    suite = load_suite(BASELINE_PATH)
    attempt = suite["cases"][4]["prediction"]["attempts"][0]
    attempt[field] = (
        [{"name": "different", "version": "v9"}]
        if field == "skills"
        else {"name": "different", "version": "v9"}
    )

    case = evaluate_suite(suite)["cases"][4]

    assert case["metrics"]["execution_attribution"][field]["accuracy"] < 1.0
    assert any(
        error["type"] == "execution_attribution_mismatch" and error["field"] == field
        for error in case["errors"]
    )


def test_large_false_split_keeps_exact_count_and_bounds_error_examples():
    suite = load_suite(BASELINE_PATH)
    atom_count = 150
    suite["cases"] = [
        {
            "case_id": "bounded-pair-errors",
            "atoms": [
                {
                    "atom_id": f"atom-{index}",
                    "traj_id": f"traj-{index}",
                    "start": 1,
                    "end": 2,
                }
                for index in range(atom_count)
            ],
            "gold": {
                "memberships": [
                    {"atom_id": f"atom-{index}", "task_id": "gold-task"}
                    for index in range(atom_count)
                ],
                "task_relations": [],
                "attempts": [],
                "attempt_relations": [],
            },
            "prediction": {
                "memberships": [
                    {
                        "atom_id": f"atom-{index}",
                        "task_id": f"pred-task-{index}",
                        "confidence": 0.9,
                    }
                    for index in range(atom_count)
                ],
                "task_relations": [],
                "attempts": [],
                "attempt_relations": [],
                "usage_allocations": [],
            },
            "usage_events": [],
        }
    ]

    report = evaluate_suite(suite)
    expected_pairs = atom_count * (atom_count - 1) // 2

    assert (
        report["metrics"]["task_grouping"]["pairwise"]["false_negative"]
        == expected_pairs
    )
    assert report["error_count"] == expected_pairs
    assert len(report["cases"][0]["errors"]) == ERROR_EXAMPLE_LIMIT
    assert report["cases"][0]["error_examples_truncated"] is True


def test_multiple_confirmed_primary_memberships_use_production_invariant():
    suite = load_suite(BASELINE_PATH)
    broken = deepcopy(suite)
    broken["cases"][1]["prediction"]["memberships"][3]["decision"] = "confirmed"

    with pytest.raises(
        TaskReplayValidationError,
        match="at most one confirmed primary membership",
    ):
        validate_suite(broken)


def test_unknown_usage_event_fails_loudly():
    suite = load_suite(BASELINE_PATH)
    suite["cases"][0]["prediction"]["usage_allocations"][0]["usage_event_id"] = (
        "missing-event"
    )

    with pytest.raises(TaskReplayValidationError, match="unknown usage event"):
        validate_suite(suite)


def test_out_of_range_confidence_fails_loudly():
    suite = load_suite(BASELINE_PATH)
    suite["cases"][0]["prediction"]["memberships"][0]["confidence"] = 1.1

    with pytest.raises(TaskReplayValidationError, match=r"within \[0, 1\]"):
        validate_suite(suite)


def test_missing_gold_atom_membership_fails_loudly():
    suite = load_suite(BASELINE_PATH)
    suite["cases"][0]["gold"]["memberships"].pop()

    with pytest.raises(TaskReplayValidationError, match="every annotated Atom"):
        validate_suite(suite)


def test_unsupported_schema_version_fails_loudly():
    suite = load_suite(BASELINE_PATH)
    suite["schema_version"] = 2

    with pytest.raises(TaskReplayValidationError, match="supported=1, got=2"):
        validate_suite(suite)


def test_cli_renders_text_and_json(capsys):
    assert main([str(BASELINE_PATH)]) == 0
    assert capsys.readouterr().out == (
        render_text(evaluate_suite(load_suite(BASELINE_PATH))) + "\n"
    )

    assert main([str(BASELINE_PATH), "--format", "json"]) == 0
    assert json.loads(capsys.readouterr().out) == evaluate_suite(
        load_suite(BASELINE_PATH)
    )


def test_linker_structure_report_matches_checked_in_snapshot():
    report = evaluate_linker.evaluate_suite(
        evaluate_linker.load_suite(LINKER_FIXTURE_PATH)
    )

    assert report == json.loads(LINKER_REPORT_PATH.read_text(encoding="utf-8"))


def test_production_linker_beats_session_and_atom_grouping_baselines():
    metrics = evaluate_linker.evaluate_suite(
        evaluate_linker.load_suite(LINKER_FIXTURE_PATH)
    )["metrics"]
    grouping = metrics["grouping"]

    assert grouping["task_graph"]["pairwise"]["precision"] == 1.0
    assert (
        grouping["task_graph"]["pairwise"]["f1"]
        > grouping["session_as_task"]["pairwise"]["f1"]
    )
    assert (
        grouping["task_graph"]["pairwise"]["f1"]
        > grouping["atom_as_task"]["pairwise"]["f1"]
    )
    assert grouping["task_graph_oracle_review"]["pairwise"]["f1"] == 1.0
    assert metrics["proposals"]["recoverable_recall"] == 1.0
    assert metrics["proposals"]["max_proposals_per_atom"] <= 8
    assert metrics["attempts"]["exact_case_rate"] == 1.0
    assert metrics["attempt_relations"]["f1"] == 1.0


def test_linker_structure_keeps_a_b_a_and_cross_session_uncertainty_visible():
    report = evaluate_linker.evaluate_suite(
        evaluate_linker.load_suite(LINKER_FIXTURE_PATH)
    )
    cases = {case["case_id"]: case for case in report["cases"]}

    a_b_a = cases["same-session-a-b-a"]
    assert a_b_a["predicted_task_count"] == 2
    assert a_b_a["grouping"]["task_graph"]["pairwise"]["f1"] == 1.0
    assert a_b_a["predicted_attempt_relation_types"] == ["continuation_of:proposed"]

    phase_shift = cases["cross-session-phase-shift"]
    assert (
        phase_shift["grouping"]["task_graph"]["pairwise"]["recall"] == 1.0
        or phase_shift["proposals"]["recoverable_recall"] == 1.0
    )
    assert phase_shift["grouping"]["task_graph_oracle_review"]["pairwise"]["f1"] == 1.0

    similar_negative = cases["cross-session-similar-negative"]
    assert similar_negative["grouping"]["task_graph"]["pairwise"]["f1"] == 1.0
    if similar_negative["proposals"]["proposed"]:
        assert similar_negative["proposals"]["precision"] == 0.0


def test_linker_proposal_pair_recovery_deduplicates_bidirectional_candidates():
    counts, reviewed = evaluate_linker._proposal_counts(
        {"atom-a": "gold", "atom-b": "gold"},
        {"atom-a": "task-a", "atom-b": "task-b"},
        [("atom-a", "task-b"), ("atom-b", "task-a")],
    )

    assert counts["false_split_pairs"] == 1
    assert counts["recoverable_false_split_pairs"] == 1
    assert counts["useful"] == 2
    assert len(set(reviewed.values())) == 1


def test_linker_proposal_does_not_treat_a_mixed_target_as_useful():
    counts, reviewed = evaluate_linker._proposal_counts(
        {"atom-a1": "gold-a", "atom-a2": "gold-a", "atom-b": "gold-b"},
        {"atom-a1": "task-a", "atom-a2": "task-mixed", "atom-b": "task-mixed"},
        [("atom-a1", "task-mixed")],
    )

    assert counts["useful"] == 0
    assert counts["recoverable_false_split_pairs"] == 0
    assert reviewed == {
        "atom-a1": "task-a",
        "atom-a2": "task-mixed",
        "atom-b": "task-mixed",
    }


def test_linker_structure_fixture_rejects_incomplete_gold():
    suite = evaluate_linker.load_suite(LINKER_FIXTURE_PATH)
    suite["cases"][0]["gold_memberships"].pop()

    with pytest.raises(
        evaluate_linker.LinkerReplayValidationError,
        match="every Atom needs one membership",
    ):
        evaluate_linker.validate_suite(suite)


def test_linker_structure_fixture_rejects_invalid_config():
    suite = evaluate_linker.load_suite(LINKER_FIXTURE_PATH)
    suite["linker_config"]["top_k"] = True

    with pytest.raises(
        evaluate_linker.LinkerReplayValidationError,
        match="suite.linker_config.top_k: expected int, got bool",
    ):
        evaluate_linker.validate_suite(suite)


def test_linker_cli_renders_text_and_json(capsys):
    report = evaluate_linker.evaluate_suite(
        evaluate_linker.load_suite(LINKER_FIXTURE_PATH)
    )

    assert evaluate_linker.main([str(LINKER_FIXTURE_PATH)]) == 0
    assert capsys.readouterr().out == evaluate_linker.render_text(report) + "\n"
    assert evaluate_linker.main([str(LINKER_FIXTURE_PATH), "--format", "json"]) == 0
    assert json.loads(capsys.readouterr().out) == report
