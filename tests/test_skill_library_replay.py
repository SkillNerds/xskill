"""Contracts for the deterministic library-aware Skill replay evaluator."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

from scripts.bench.skill_library_replay.evaluate import (
    LibraryReplayValidationError,
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
    / "skill_library_replay"
    / "fixtures"
)
BASELINE_PATH = FIXTURE_DIR / "baseline_v1.json"
REPORT_PATH = FIXTURE_DIR / "baseline_v1.report.json"

pytestmark = pytest.mark.algorithm_replay


def test_baseline_report_matches_checked_in_snapshot():
    report = evaluate_suite(load_suite(BASELINE_PATH))

    assert report == json.loads(REPORT_PATH.read_text(encoding="utf-8"))


def test_factorial_replay_separates_body_description_and_joint_effects():
    report = evaluate_suite(load_suite(BASELINE_PATH))
    effects = report["library_curve"][0]["factorial_effects"]

    assert report["isolated"]["body_effect"]["mean"] == 0.5
    assert report["isolated"]["positive_body_effect"]["mean"] == 1.0
    assert report["isolated"]["negative_body_effect"]["mean"] == 0.0
    assert effects["body_at_old_description"]["mean"] == 0.0
    assert effects["description_at_old_body"]["mean"] == 0.5
    assert effects["joint_deployed_effect"]["mean"] == 1.0
    assert effects["interaction"]["mean"] == 0.5
    assert report["library_curve"][0]["interference"]["mean"] == -0.5


def test_activation_metrics_do_not_hide_positive_and_negative_cancellation():
    activation = report_level(0)["activation_effects"]

    assert activation["target_activation_delta"]["mean"] == 0.0
    assert activation["positive_recall_delta"]["mean"] == 1.0
    assert activation["negative_false_positive_rate_delta"]["mean"] == -1.0


def test_library_curve_retains_resource_and_distractor_context():
    level = report_level(2)

    assert level["distractor_skills"] == [
        "spreadsheet-formatting",
        "spreadsheet-charting",
    ]
    assert level["resource_effects"]["catalog_tokens"]["mean"] == 4.0
    assert level["resource_effects"]["cost_usd"]["mean"] == -0.00015
    old_cell = level["cells"]["old_body__old_description"]
    assert old_cell["activated_skill_counts"] == {
        "spreadsheet-formula-repair": 1,
        "spreadsheet-formatting": 1,
    }
    assert old_cell["error_types"] == {
        "harmful_activation": 1,
        "wrong_skill_activation": 1,
    }


def report_level(distractor_count: int) -> dict:
    report = evaluate_suite(load_suite(BASELINE_PATH))
    return next(
        level
        for level in report["library_curve"]
        if level["distractor_count"] == distractor_count
    )


def test_missing_factorial_cell_fails_loudly():
    suite = load_suite(BASELINE_PATH)
    del suite["cases"][0]["libraries"][0]["deployed"]["new_body__new_description"]

    with pytest.raises(LibraryReplayValidationError, match="expected exactly"):
        validate_suite(suite)


def test_each_case_must_cover_the_same_library_ladder():
    suite = load_suite(BASELINE_PATH)
    suite["cases"][0]["libraries"][1]["distractor_count"] = 1

    with pytest.raises(
        LibraryReplayValidationError, match="expected distractor counts"
    ):
        validate_suite(suite)


def test_library_ladder_must_include_zero_distractor_control():
    suite = load_suite(BASELINE_PATH)
    suite["library_ladder"] = suite["library_ladder"][1:]
    for case in suite["cases"]:
        case["libraries"] = case["libraries"][1:]

    with pytest.raises(LibraryReplayValidationError, match="must start"):
        validate_suite(suite)


def test_library_growth_must_preserve_distractor_order():
    suite = load_suite(BASELINE_PATH)
    suite["library_ladder"].insert(
        1,
        {
            "distractor_count": 1,
            "distractor_catalog_fingerprint": (
                "sha256:8888888888888888888888888888888888888888888888888888888888888888"
            ),
            "distractor_skills": ["spreadsheet-charting"],
        },
    )
    for case in suite["cases"]:
        copied = deepcopy(case["libraries"][1])
        copied["distractor_count"] = 1
        case["libraries"].insert(1, copied)

    with pytest.raises(LibraryReplayValidationError, match="retain prior order"):
        validate_suite(suite)


def test_target_activation_flag_must_match_recorded_skill_sequence():
    suite = load_suite(BASELINE_PATH)
    observation = suite["cases"][0]["libraries"][0]["deployed"][
        "new_body__new_description"
    ]
    observation["target_activated"] = False

    with pytest.raises(LibraryReplayValidationError, match="disagrees"):
        validate_suite(suite)


def test_deployed_activation_cannot_reference_skill_outside_catalog():
    suite = load_suite(BASELINE_PATH)
    observation = suite["cases"][0]["libraries"][1]["deployed"][
        "old_body__old_description"
    ]
    observation["activated_skills"] = ["unlisted-skill"]

    with pytest.raises(LibraryReplayValidationError, match="unknown skills"):
        validate_suite(suite)


def test_isolated_run_must_force_only_the_target_skill():
    suite = load_suite(BASELINE_PATH)
    suite["cases"][0]["isolated"]["old_body"]["activated_skills"] = []
    suite["cases"][0]["isolated"]["old_body"]["target_activated"] = False

    with pytest.raises(LibraryReplayValidationError, match="force only target_skill"):
        validate_suite(suite)


def test_duplicate_run_id_fails_loudly():
    suite = load_suite(BASELINE_PATH)
    duplicate = suite["cases"][0]["isolated"]["old_body"]["run_id"]
    suite["cases"][0]["isolated"]["new_body"]["run_id"] = duplicate

    with pytest.raises(LibraryReplayValidationError, match="duplicate"):
        validate_suite(suite)


def test_old_and_new_artifact_fingerprints_must_differ():
    suite = load_suite(BASELINE_PATH)
    manifest = suite["run_manifest"]
    manifest["new_description_fingerprint"] = manifest["old_description_fingerprint"]

    with pytest.raises(LibraryReplayValidationError, match="must differ"):
        validate_suite(suite)


@pytest.mark.parametrize("score", [True, -0.1, 1.1, "1.0"])
def test_invalid_outcome_score_fails_loudly(score):
    suite = load_suite(BASELINE_PATH)
    suite["cases"][0]["isolated"]["old_body"]["score"] = score

    with pytest.raises(LibraryReplayValidationError, match="score"):
        validate_suite(suite)


def test_suite_requires_positive_and_negative_activation_cases():
    suite = load_suite(BASELINE_PATH)
    suite["cases"] = [suite["cases"][0]]

    with pytest.raises(LibraryReplayValidationError, match="positive and negative"):
        validate_suite(suite)


def test_bootstrap_bound_counts_every_reported_statistic():
    suite = load_suite(BASELINE_PATH)
    suite["metric_config"]["bootstrap_samples"] = 50_000
    repeated = deepcopy(suite["cases"][0])
    repeated["case_id"] += "-repeated"
    repeated["task_fingerprint"] = "sha256:" + "f" * 64
    for observation in repeated["isolated"].values():
        observation["run_id"] += "-repeated"
    for level in repeated["libraries"]:
        for observation in level["deployed"].values():
            observation["run_id"] += "-repeated"
    suite["cases"].append(repeated)

    with pytest.raises(LibraryReplayValidationError, match="bootstrap work"):
        validate_suite(suite)


def test_cli_renders_text_and_json(capsys):
    report = evaluate_suite(load_suite(BASELINE_PATH))

    assert main([str(BASELINE_PATH)]) == 0
    assert capsys.readouterr().out == render_text(report) + "\n"
    assert main([str(BASELINE_PATH), "--format", "json"]) == 0
    assert json.loads(capsys.readouterr().out) == report
