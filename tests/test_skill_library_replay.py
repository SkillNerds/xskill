"""Contracts for the deterministic library-aware Skill replay evaluator."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

from scripts.bench.skill_library_replay.evaluate import (
    LibraryReplayValidationError,
    evaluate_admission,
    evaluate_suite,
    load_admission_policy,
    load_suite,
    main,
    render_text,
    validate_admission_policy,
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
POLICY_PATH = FIXTURE_DIR / "admission_policy_v1.json"

pytestmark = pytest.mark.algorithm_replay


def _copy_case(
    case: dict,
    *,
    suffix: str,
    task_fingerprint: str | None = None,
    seed: int | None = None,
) -> dict:
    copied = deepcopy(case)
    copied["case_id"] = f"{case['case_id']}-{suffix}"
    if task_fingerprint is not None:
        copied["task_fingerprint"] = task_fingerprint
    if seed is not None:
        copied["seed"] = seed

    def rewrite_run_ids(value):
        if isinstance(value, dict):
            if "run_id" in value:
                value["run_id"] = f"{value['run_id']}-{suffix}"
            for nested in value.values():
                rewrite_run_ids(nested)
        elif isinstance(value, list):
            for nested in value:
                rewrite_run_ids(nested)

    rewrite_run_ids(copied)
    return copied


def _expanded_policy_suite() -> tuple[dict, dict]:
    suite = load_suite(BASELINE_PATH)
    originals = list(suite["cases"])
    for index in range(1, 25):
        suite["cases"].append(
            _copy_case(
                originals[0],
                suffix=f"positive-copy-{index}",
                task_fingerprint=f"sha256:{index + 2:064x}",
            )
        )
        suite["cases"].append(
            _copy_case(
                originals[1],
                suffix=f"negative-copy-{index}",
                task_fingerprint=f"sha256:{index + 26:064x}",
            )
        )
    policy = load_admission_policy(POLICY_PATH, suite)
    return suite, policy


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


def test_effect_confidence_intervals_cluster_repeated_seeds_by_task():
    suite = load_suite(BASELINE_PATH)
    originals = list(suite["cases"])
    suite["cases"].extend(
        _copy_case(case, suffix="seed-8", seed=8) for case in originals
    )

    effect = evaluate_suite(suite)["library_curve"][0]["factorial_effects"][
        "joint_deployed_effect"
    ]

    assert effect["n"] == 4
    assert effect["tasks"] == 2
    assert effect["wins"] == 2


def test_every_task_must_use_the_same_seed_set():
    suite = load_suite(BASELINE_PATH)
    suite["cases"].append(_copy_case(suite["cases"][0], suffix="seed-8", seed=8))

    with pytest.raises(LibraryReplayValidationError, match="same seed set"):
        validate_suite(suite)


def test_activation_label_must_be_stable_across_task_seeds():
    suite = load_suite(BASELINE_PATH)
    copied = _copy_case(suite["cases"][0], suffix="seed-8", seed=8)
    copied["should_activate"] = False
    suite["cases"].append(copied)

    with pytest.raises(LibraryReplayValidationError, match="stable across seeds"):
        validate_suite(suite)


def test_small_synthetic_fixture_is_inconclusive_under_registered_policy():
    suite = load_suite(BASELINE_PATH)
    policy = load_admission_policy(POLICY_PATH, suite)

    admission = evaluate_admission(suite, policy)

    assert admission["status"] == "inconclusive"
    assert admission["reasons"][:3] == [
        "minimum_paired_tasks",
        "minimum_positive_tasks",
        "minimum_negative_tasks",
    ]


def test_admission_requires_all_pre_registered_effect_and_harm_gates():
    suite, policy = _expanded_policy_suite()

    admission = evaluate_admission(suite, policy)

    assert admission["status"] == "admit"
    assert admission["reasons"] == []
    assert all(gate["status"] == "pass" for gate in admission["gates"])
    assert (
        admission["effects"]["candidate_positive_task_activation_rate"][
            "confidence_interval"
        ][0]
        < 1
    )
    assert (
        admission["effects"]["candidate_negative_task_activation_rate"][
            "confidence_interval"
        ][1]
        > 0
    )


def test_admission_rejects_complete_evidence_below_minimum_gain():
    suite, policy = _expanded_policy_suite()
    for case in suite["cases"]:
        level = next(
            level
            for level in case["libraries"]
            if level["distractor_count"] == policy["primary_distractor_count"]
        )
        cells = level["deployed"]
        cells[policy["candidate_cell"]]["score"] = cells["old_body__old_description"][
            "score"
        ]

    admission = evaluate_admission(suite, policy)

    assert admission["status"] == "reject"
    assert "minimum_score_gain" in admission["reasons"]


def test_non_evaluable_errors_make_admission_inconclusive():
    suite, policy = _expanded_policy_suite()
    suite["cases"][0]["libraries"][1]["deployed"][policy["candidate_cell"]][
        "error_type"
    ] = "infrastructure_failure"

    admission = evaluate_admission(suite, policy)

    assert admission["status"] == "inconclusive"
    assert "non_evaluable_errors" in admission["reasons"]


def test_policy_must_be_registered_before_recorded_results():
    suite = load_suite(BASELINE_PATH)
    policy = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
    policy["registered_at"] = suite["run_manifest"]["generated_at"]

    with pytest.raises(LibraryReplayValidationError, match="earlier"):
        validate_admission_policy(policy, suite)


def test_policy_must_bind_the_selected_library_snapshot():
    suite = load_suite(BASELINE_PATH)
    policy = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
    policy["primary_distractor_catalog_fingerprint"] = "sha256:" + "8" * 64

    with pytest.raises(LibraryReplayValidationError, match="selected library level"):
        validate_admission_policy(policy, suite)


def test_policy_must_bind_the_candidate_skill_versions():
    suite = load_suite(BASELINE_PATH)
    policy = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
    policy["new_body_fingerprint"] = "sha256:" + "8" * 64

    with pytest.raises(LibraryReplayValidationError, match="new_body_fingerprint"):
        validate_admission_policy(policy, suite)


def test_policy_cannot_use_the_deployed_baseline_as_candidate():
    suite = load_suite(BASELINE_PATH)
    policy = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
    policy["candidate_cell"] = "old_body__old_description"

    with pytest.raises(LibraryReplayValidationError, match="candidate_cell"):
        validate_admission_policy(policy, suite)


def test_policy_rejects_vacuous_score_and_activation_thresholds():
    suite = load_suite(BASELINE_PATH)
    policy = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
    policy["maximum_negative_false_positive_rate_increase"] = 1.1

    with pytest.raises(LibraryReplayValidationError, match=r"within \[0, 1\]"):
        validate_admission_policy(policy, suite)


def test_policy_rejects_negative_gain_and_resource_limits():
    suite = load_suite(BASELINE_PATH)
    policy = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
    policy["minimum_score_gain"] = -0.1

    with pytest.raises(LibraryReplayValidationError, match="finite number >= 0"):
        validate_admission_policy(policy, suite)

    policy = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
    policy["resource_increase_limits"]["cost_usd"] = -0.1

    with pytest.raises(LibraryReplayValidationError, match="finite number >= 0"):
        validate_admission_policy(policy, suite)


def test_admission_rejects_bad_absolute_activation_even_without_regression():
    suite, policy = _expanded_policy_suite()
    for case in suite["cases"]:
        level = next(
            level
            for level in case["libraries"]
            if level["distractor_count"] == policy["primary_distractor_count"]
        )
        for cell in ("old_body__old_description", policy["candidate_cell"]):
            level["deployed"][cell]["target_activated"] = False
            level["deployed"][cell]["activated_skills"] = []

    admission = evaluate_admission(suite, policy)

    assert admission["status"] == "reject"
    assert "minimum_candidate_positive_task_activation_rate" in admission["reasons"]
    assert "maximum_positive_recall_drop" not in admission["reasons"]


def test_admission_rejects_absolute_false_activations_without_regression():
    suite, policy = _expanded_policy_suite()
    for case in suite["cases"]:
        if case["should_activate"]:
            continue
        level = next(
            level
            for level in case["libraries"]
            if level["distractor_count"] == policy["primary_distractor_count"]
        )
        for cell in ("old_body__old_description", policy["candidate_cell"]):
            level["deployed"][cell]["target_activated"] = True
            level["deployed"][cell]["activated_skills"] = [suite["target_skill"]]

    admission = evaluate_admission(suite, policy)

    assert admission["status"] == "reject"
    assert "maximum_candidate_negative_task_activation_rate" in admission["reasons"]
    assert "maximum_negative_false_positive_rate_increase" not in admission["reasons"]


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


def test_cli_renders_text_and_json(capsys):
    report = evaluate_suite(load_suite(BASELINE_PATH))

    assert main([str(BASELINE_PATH)]) == 0
    assert capsys.readouterr().out == render_text(report) + "\n"
    assert main([str(BASELINE_PATH), "--format", "json"]) == 0
    assert json.loads(capsys.readouterr().out) == report

    assert main([str(BASELINE_PATH), "--policy", str(POLICY_PATH)]) == 0
    policy_text = capsys.readouterr().out
    assert "status: inconclusive" in policy_text

    assert (
        main(
            [
                str(BASELINE_PATH),
                "--policy",
                str(POLICY_PATH),
                "--format",
                "json",
            ]
        )
        == 0
    )
    policy_report = json.loads(capsys.readouterr().out)
    assert policy_report["admission"]["policy_id"] == (
        "synthetic-library-admission-policy-v1"
    )
