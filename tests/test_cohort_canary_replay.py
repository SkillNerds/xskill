"""Deterministic contract tests for cohort-scoped publication replay."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

from scripts.bench.cohort_canary_replay.evaluate import (
    CohortReplayValidationError,
    evaluate_suite,
    load_suite,
    main,
    render_text,
    validate_suite,
)

FIXTURE_DIR = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "bench"
    / "cohort_canary_replay"
    / "fixtures"
)
BASELINE_PATH = FIXTURE_DIR / "baseline_v1.json"
REPORT_PATH = FIXTURE_DIR / "baseline_v1.report.json"

pytestmark = pytest.mark.algorithm_replay


def _suite() -> dict:
    return json.loads(BASELINE_PATH.read_text(encoding="utf-8"))


def _rewrite_run_ids(observation: dict, suffix: str) -> None:
    observation["case_id"] = f"{observation['case_id']}-{suffix}"
    for side in ("old", "new"):
        observation[side]["run_id"] = f"{observation[side]['run_id']}-{suffix}"


def test_baseline_report_matches_checked_in_snapshot():
    report = evaluate_suite(load_suite(BASELINE_PATH))

    assert report == json.loads(REPORT_PATH.read_text(encoding="utf-8"))


def test_baseline_detects_supported_reversal_and_avoids_scoped_harm():
    report = evaluate_suite(load_suite(BASELINE_PATH))
    first = report["updates"][0]

    assert first["supported_sign_reversal"] is True
    assert first["supported_positive_cohorts"] == ["cohort-a"]
    assert first["supported_negative_cohorts"] == ["cohort-b"]
    assert first["global"]["selection"] == "new"
    assert first["global"]["harmful_promoted_cohorts"] == ["cohort-b"]
    assert first["scoped"]["selection_by_cohort"] == {
        "cohort-a": "new",
        "cohort-b": "old",
    }
    assert first["scoped"]["harmful_promoted_cohorts"] == []
    assert first["scoped"]["oracle_value_gap_over_global"] == 0.05


def test_aggregate_keeps_policy_gain_harm_and_resource_cost_separate():
    aggregate = evaluate_suite(load_suite(BASELINE_PATH))["aggregate"]

    assert aggregate["supported_sign_reversal_rate"] == 0.5
    assert aggregate["global_mean_policy_gain"] == 0.1
    assert aggregate["scoped_mean_policy_gain"] == 0.125
    assert aggregate["global_harmful_promotion_rate"] == 0.25
    assert aggregate["scoped_harmful_promotion_rate"] == 0.0
    assert aggregate["scoped_mean_retained_versions"] == 1.5
    assert aggregate["evaluation_calls"] == 32
    assert aggregate["evaluation_cost_usd"] == 0.032


def test_cohort_identity_requires_scenario_model_and_runtime_uniqueness():
    suite = _suite()
    suite["cohorts"][1].update(
        {
            "scenario": suite["cohorts"][0]["scenario"],
            "model": suite["cohorts"][0]["model"],
            "runtime": suite["cohorts"][0]["runtime"],
        }
    )

    with pytest.raises(CohortReplayValidationError, match="duplicate scenario"):
        validate_suite(suite)


def test_traffic_weights_must_be_positive_and_sum_to_one():
    suite = _suite()
    suite["cohorts"][0]["traffic_weight"] = 0.8

    with pytest.raises(CohortReplayValidationError, match="sum to 1"):
        validate_suite(suite)

    suite = _suite()
    suite["cohorts"][1]["traffic_weight"] = 0.0
    suite["cohorts"][0]["traffic_weight"] = 1.0

    with pytest.raises(CohortReplayValidationError, match="greater than zero"):
        validate_suite(suite)


def test_every_update_must_cover_every_declared_cohort():
    suite = _suite()
    suite["updates"][0]["observations"] = [
        observation
        for observation in suite["updates"][0]["observations"]
        if observation["cohort_id"] != "cohort-b"
    ]

    with pytest.raises(CohortReplayValidationError, match="requires at least two Tasks"):
        validate_suite(suite)


def test_each_task_in_a_cohort_must_use_the_same_seed_set():
    suite = _suite()
    copied = deepcopy(suite["updates"][0]["observations"][0])
    copied["seed"] = 8
    _rewrite_run_ids(copied, "seed-8")
    suite["updates"][0]["observations"].append(copied)

    with pytest.raises(CohortReplayValidationError, match="same seed set"):
        validate_suite(suite)


def test_cohorts_share_a_matrix_and_decision_tasks_cannot_leak():
    suite = _suite()
    suite["updates"][0]["observations"][2]["task_fingerprint"] = (
        "sha256:" + "9" * 64
    )

    with pytest.raises(CohortReplayValidationError, match="same Task/seed matrix"):
        validate_suite(suite)

    suite = _suite()
    decision_task = next(
        observation["task_fingerprint"]
        for observation in suite["updates"][0]["observations"]
        if observation["role"] == "decision"
    )
    next(
        observation
        for observation in suite["updates"][0]["observations"]
        if observation["role"] == "evaluation"
    )["task_fingerprint"] = decision_task

    with pytest.raises(CohortReplayValidationError, match="must be disjoint"):
        validate_suite(suite)


def test_repeated_seeds_are_clustered_by_task_not_counted_as_new_tasks():
    suite = _suite()
    update = suite["updates"][0]
    for observation in list(update["observations"]):
        if observation["role"] != "decision":
            continue
        copied = deepcopy(observation)
        copied["seed"] = 8
        _rewrite_run_ids(copied, "seed-8")
        update["observations"].append(copied)

    report = evaluate_suite(suite)
    effect = report["updates"][0]["cohorts"][0]["decision_effect"]

    assert effect["n"] == 4
    assert effect["tasks"] == 2
    assert effect["wins"] == 2


def test_opposite_point_estimates_without_corrected_intervals_do_not_reverse():
    suite = _suite()
    observations = [
        observation
        for observation in suite["updates"][0]["observations"]
        if observation["cohort_id"] == "cohort-a"
    ]
    observations[0]["new"]["score"] = observations[0]["old"]["score"] + 0.2
    observations[1]["new"]["score"] = observations[1]["old"]["score"] - 0.01

    first = evaluate_suite(suite)["updates"][0]

    assert first["cohorts"][0]["decision_effect"]["mean"] > 0
    assert first["cohorts"][0]["decision_evidence_status"] == "unresolved"
    assert first["supported_sign_reversal"] is False


def test_non_evaluable_error_keeps_the_cohort_unresolved():
    suite = _suite()
    suite["updates"][0]["observations"][0]["new"][
        "error_type"
    ] = "infrastructure_failure"

    first = evaluate_suite(suite)["updates"][0]

    assert first["cohorts"][0]["decision_evidence_status"] == "unresolved"
    assert first["cohorts"][0]["decision_non_evaluable_errors"] == {
        "infrastructure_failure": 1
    }
    assert first["global"]["decision_evidence_status"] == "unresolved"


def test_held_out_evaluation_cannot_change_the_frozen_selection():
    suite = _suite()
    baseline = evaluate_suite(suite)["updates"][0]
    for observation in suite["updates"][0]["observations"]:
        if observation["role"] != "evaluation":
            continue
        observation["new"]["score"] = max(
            0.0, observation["old"]["score"] - 0.3
        )

    changed = evaluate_suite(suite)["updates"][0]

    assert changed["global"]["selection"] == baseline["global"]["selection"]
    assert (
        changed["scoped"]["selection_by_cohort"]
        == baseline["scoped"]["selection_by_cohort"]
    )
    assert changed["global"]["policy_gain"] < baseline["global"]["policy_gain"]
    assert changed["scoped"]["policy_gain"] < baseline["scoped"]["policy_gain"]


def test_outcomes_require_complete_old_new_pairs_and_unique_runs():
    suite = _suite()
    del suite["updates"][0]["observations"][0]["new"]

    with pytest.raises(CohortReplayValidationError, match="missing=.*new"):
        validate_suite(suite)

    suite = _suite()
    first = suite["updates"][0]["observations"][0]
    second = suite["updates"][0]["observations"][1]
    second["old"]["run_id"] = first["old"]["run_id"]

    with pytest.raises(CohortReplayValidationError, match="duplicate"):
        validate_suite(suite)


def test_update_stream_requires_distinct_versions_and_increasing_epochs():
    suite = _suite()
    suite["updates"][0]["new_skill_fingerprint"] = suite["updates"][0][
        "old_skill_fingerprint"
    ]

    with pytest.raises(CohortReplayValidationError, match="must differ"):
        validate_suite(suite)

    suite = _suite()
    suite["updates"][1]["epoch"] = 1

    with pytest.raises(CohortReplayValidationError, match="strictly increasing"):
        validate_suite(suite)


def test_unknown_fields_and_non_finite_scores_fail_closed():
    suite = _suite()
    suite["updates"][0]["observations"][0]["gold_label"] = "leak"

    with pytest.raises(CohortReplayValidationError, match="unknown=.*gold_label"):
        validate_suite(suite)

    suite = _suite()
    suite["updates"][0]["observations"][0]["new"]["score"] = float("nan")

    with pytest.raises(CohortReplayValidationError, match="finite"):
        validate_suite(suite)


def test_replay_work_has_a_deterministic_upper_bound():
    suite = _suite()
    suite["metric_config"]["bootstrap_samples"] = 1_000_000

    with pytest.raises(CohortReplayValidationError, match="work exceeds"):
        validate_suite(suite)


def test_cli_renders_text_and_json(capsys):
    report = evaluate_suite(load_suite(BASELINE_PATH))

    assert main([str(BASELINE_PATH)]) == 0
    assert capsys.readouterr().out == render_text(report) + "\n"

    assert main([str(BASELINE_PATH), "--format", "json"]) == 0
    assert json.loads(capsys.readouterr().out) == report
