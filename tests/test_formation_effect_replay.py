"""Contracts for the deterministic Formation-method effect replay."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

from scripts.bench.formation_effect_replay.evaluate import (
    FormationEffectValidationError,
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
    / "formation_effect_replay"
    / "fixtures"
)
BASELINE_PATH = FIXTURE_DIR / "baseline_v1.json"
REPORT_PATH = FIXTURE_DIR / "baseline_v1.report.json"

pytestmark = pytest.mark.algorithm_replay


def _mode(report: dict, mode_id: str) -> dict:
    return next(mode for mode in report["modes"] if mode["mode_id"] == mode_id)


def _suffix_run_ids(case: dict, suffix: str) -> None:
    case["control"]["run_id"] += suffix
    for observations in case["observations"].values():
        for observation in observations.values():
            observation["run_id"] += suffix


def test_baseline_report_matches_checked_in_snapshot():
    report = evaluate_suite(load_suite(BASELINE_PATH))

    assert report == json.loads(REPORT_PATH.read_text(encoding="utf-8"))


def test_primary_contrast_measures_method_effect_and_quality_guards():
    report = evaluate_suite(load_suite(BASELINE_PATH))
    primary = _mode(report, "natural_output")["contrasts"][
        "task_grounded_minus_atom"
    ]

    assert primary["pass_rate_delta"]["mean"] == 0.833333
    assert primary["pass_rate_delta"]["confidence_interval"] == [0.5, 1.0]
    assert primary["false_trigger_rate_delta"]["mean"] == -1.0
    assert primary["known_pass_rate_delta"]["mean"] == 0.5
    assert primary["mcnemar_pass"] == {
        "treated_only_pass": 5,
        "control_only_pass": 0,
        "discordant_pairs": 5,
        "exact_two_sided_p": 0.0625,
    }
    assert report["decision"]["passed"] is True


def test_report_retains_floor_ceiling_and_utility_density():
    mode = _mode(evaluate_suite(load_suite(BASELINE_PATH)), "natural_output")

    assert mode["methods"]["no_skill"]["pass_rate"] == 0.666667
    assert mode["methods"]["gold_task"]["pass_rate"] == 1.0
    assert mode["utility_density_per_1000_skill_tokens"]["atom"] < 0
    assert mode["utility_density_per_1000_skill_tokens"]["task_grounded"] > 0
    gold_gap = mode["contrasts"]["gold_task_minus_task_grounded"]
    assert gold_gap["pass_rate_delta"]["mean"] == 0.0


def test_provenance_is_runtime_agnostic_and_method_names_are_fixed():
    report = evaluate_suite(load_suite(BASELINE_PATH))

    assert set(report["run_manifest"]) == {
        "repository_revision",
        "recorded_at",
        "training_set_fingerprint",
        "held_out_task_set_fingerprint",
        "evaluation_protocol_fingerprint",
        "runtime_config_fingerprint",
        "scorer_fingerprint",
        "formation_config_fingerprint",
        "skill_generation_config_fingerprint",
        "activation_config_fingerprint",
    }
    assert set(_mode(report, "natural_output")["methods"]) == {
        "no_skill",
        "session",
        "atom",
        "task_grounded",
        "gold_task",
    }


def test_matched_budget_requires_equal_serialized_skill_tokens():
    suite = load_suite(BASELINE_PATH)
    suite["modes"][1]["arms"]["atom"]["serialized_skill_tokens"] -= 1

    with pytest.raises(FormationEffectValidationError, match="matched budget"):
        validate_suite(suite)


def test_budget_modes_must_reuse_the_same_method_evidence():
    suite = load_suite(BASELINE_PATH)
    suite["modes"][1]["arms"]["atom"]["evidence_fingerprint"] = (
        "sha256:" + "b" * 64
    )

    with pytest.raises(FormationEffectValidationError, match="evidence must remain fixed"):
        validate_suite(suite)


def test_each_case_requires_every_method_in_every_mode():
    suite = load_suite(BASELINE_PATH)
    del suite["cases"][0]["observations"]["natural_output"]["atom"]

    with pytest.raises(FormationEffectValidationError, match="expected exactly"):
        validate_suite(suite)


def test_control_cannot_activate_a_skill():
    suite = load_suite(BASELINE_PATH)
    suite["cases"][0]["control"]["activated_skills"] = ["unexpected-skill"]

    with pytest.raises(FormationEffectValidationError, match="control cannot activate"):
        validate_suite(suite)


def test_negative_case_cannot_claim_relevant_activation():
    suite = load_suite(BASELINE_PATH)
    observation = suite["cases"][2]["observations"]["natural_output"]["atom"]
    observation["relevant_skill_activated"] = True

    with pytest.raises(FormationEffectValidationError, match="negative case"):
        validate_suite(suite)


def test_training_and_held_out_fingerprints_must_differ():
    suite = load_suite(BASELINE_PATH)
    suite["run_manifest"]["held_out_task_set_fingerprint"] = suite[
        "run_manifest"
    ]["training_set_fingerprint"]

    with pytest.raises(FormationEffectValidationError, match="must differ"):
        validate_suite(suite)


def test_duplicate_run_id_fails_loudly():
    suite = load_suite(BASELINE_PATH)
    duplicate = suite["cases"][0]["control"]["run_id"]
    suite["cases"][1]["control"]["run_id"] = duplicate

    with pytest.raises(FormationEffectValidationError, match="duplicate"):
        validate_suite(suite)


def test_repeated_seeds_are_clustered_by_task():
    suite = load_suite(BASELINE_PATH)
    for original in list(suite["cases"]):
        repeated = deepcopy(original)
        repeated["case_id"] += "-seed-2"
        repeated["seed"] = 2
        _suffix_run_ids(repeated, "-seed-2")
        suite["cases"].append(repeated)

    report = evaluate_suite(suite)
    effect = _mode(report, "natural_output")["contrasts"][
        "task_grounded_minus_atom"
    ]["pass_rate_delta"]

    assert effect["n_pairs"] == 12
    assert effect["n_tasks"] == 6


def test_unbalanced_seed_sets_fail_loudly():
    suite = load_suite(BASELINE_PATH)
    repeated = deepcopy(suite["cases"][0])
    repeated["case_id"] = "data-novel-seed-2"
    repeated["seed"] = 2
    _suffix_run_ids(repeated, "-seed-2")
    suite["cases"].append(repeated)

    with pytest.raises(FormationEffectValidationError, match="same seed set"):
        validate_suite(suite)


def test_bootstrap_work_is_bounded_by_total_statistics():
    suite = load_suite(BASELINE_PATH)
    suite["metric_config"]["bootstrap_samples"] = 50_000
    for original in list(suite["cases"]):
        repeated = deepcopy(original)
        repeated["case_id"] += "-seed-2"
        repeated["seed"] = 2
        _suffix_run_ids(repeated, "-seed-2")
        suite["cases"].append(repeated)

    with pytest.raises(FormationEffectValidationError, match="bootstrap work"):
        validate_suite(suite)


@pytest.mark.parametrize("score", [True, -0.1, 1.1, "1"])
def test_invalid_scores_fail_loudly(score):
    suite = load_suite(BASELINE_PATH)
    suite["cases"][0]["control"]["score"] = score

    with pytest.raises(FormationEffectValidationError, match="score"):
        validate_suite(suite)


def test_unknown_observation_field_fails_loudly():
    suite = load_suite(BASELINE_PATH)
    suite["cases"][0]["control"]["undocumented"] = True

    with pytest.raises(FormationEffectValidationError, match="field mismatch"):
        validate_suite(suite)


def test_cli_renders_text_and_json(capsys):
    report = evaluate_suite(load_suite(BASELINE_PATH))

    assert main([str(BASELINE_PATH)]) == 0
    assert capsys.readouterr().out == render_text(report) + "\n"
    assert main([str(BASELINE_PATH), "--format", "json"]) == 0
    assert json.loads(capsys.readouterr().out) == report
