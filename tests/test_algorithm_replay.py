"""Contracts for the deterministic Atom splitting/routing replay evaluator."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

from scripts.bench.algorithm_replay.evaluate import (
    ReplayValidationError,
    detect_language,
    evaluate_suite,
    load_suite,
    main,
    render_text,
    validate_suite,
)

FIXTURE_DIR = (
    Path(__file__).parent.parent / "scripts" / "bench" / "algorithm_replay" / "fixtures"
)
BASELINE_PATH = FIXTURE_DIR / "baseline_v1.json"
REPORT_PATH = FIXTURE_DIR / "baseline_v1.report.json"
BOUNDARY_SCORE_PATH = FIXTURE_DIR / "baseline_v2.json"
BOUNDARY_SCORE_REPORT_PATH = FIXTURE_DIR / "baseline_v2.report.json"


pytestmark = pytest.mark.algorithm_replay


def test_baseline_report_matches_checked_in_snapshot():
    report = evaluate_suite(load_suite(BASELINE_PATH))

    assert report == json.loads(REPORT_PATH.read_text(encoding="utf-8"))


def test_boundary_score_report_matches_checked_in_snapshot():
    report = evaluate_suite(load_suite(BOUNDARY_SCORE_PATH))

    assert report == json.loads(
        BOUNDARY_SCORE_REPORT_PATH.read_text(encoding="utf-8")
    )


def test_boundary_scores_expose_routing_error_association():
    report = evaluate_suite(load_suite(BOUNDARY_SCORE_PATH))
    boundary_score = report["metrics"]["boundary_score"]
    association = report["metrics"]["routing_error_association"]

    assert report["boundary_algorithm_version"] == "synthetic-boundary-ranker-v1"
    # The one positive boundary outranks four negatives and ties the fifth.
    assert boundary_score == {
        "candidates": 6,
        "positive": 1,
        "negative": 5,
        "auroc": 0.9,
    }
    assert association["low_score_error_auroc"] == 1.0
    assert association["thresholds"] == [
        {
            "minimum_score": 0.0,
            "eligible": 2,
            "retained": 2,
            "coverage": 1.0,
            "routing_errors": 1,
            "routing_error_rate": 0.5,
        },
        {
            "minimum_score": 0.5,
            "eligible": 2,
            "retained": 1,
            "coverage": 0.5,
            "routing_errors": 0,
            "routing_error_rate": 0.0,
        },
        {
            "minimum_score": 0.8,
            "eligible": 2,
            "retained": 1,
            "coverage": 0.5,
            "routing_errors": 0,
            "routing_error_rate": 0.0,
        },
    ]
    text_report = render_text(report)
    assert "boundary_score: candidates=6 AUROC=0.9" in text_report
    assert "selected=2 errors=1 low_score_AUROC=1.0" in text_report


def test_one_class_boundary_or_routing_samples_have_no_auroc():
    report = evaluate_suite(load_suite(BOUNDARY_SCORE_PATH))

    assert report["cases"][1]["metrics"]["boundary_score"]["auroc"] is None
    assert (
        report["cases"][0]["metrics"]["routing_error_association"][
            "low_score_error_auroc"
        ]
        is None
    )


def test_baseline_exposes_expected_regression_signals():
    report = evaluate_suite(load_suite(BASELINE_PATH))

    assert report["metrics"]["coverage"] == 1.0
    assert report["metrics"]["overlap_rate"] > 0.0
    assert report["metrics"]["duplicate_rate"] > 0.0
    assert report["metrics"]["segmentation"]["pk"] > 0.0
    assert report["metrics"]["segmentation"]["window_diff"] > 0.0
    assert report["metrics"]["language_consistency"] == 1.0
    assert report["metrics"]["multi_skill_relation_retention"] == 1.0
    assert set(report["metrics"]["routing_macro"]) == {"precision", "recall", "f1"}


def test_duplicate_and_overlap_rates_match_handworked_case():
    report = evaluate_suite(load_suite(BASELINE_PATH))
    metrics = report["cases"][2]["metrics"]

    # Two predictions align with one gold Atom: one of two is extra. Their
    # 3-line overlap is measured against the 12-line scorable trajectory.
    assert metrics["duplicate_rate"] == 0.5
    assert metrics["overlap_rate"] == 0.25


def test_missing_second_skill_reduces_multi_skill_retention():
    suite = load_suite(BASELINE_PATH)
    suite["cases"][3]["predicted_atoms"][0]["skills"] = [
        "spreadsheet-formatting"
    ]

    metrics = evaluate_suite(suite)["cases"][3]["metrics"]

    assert metrics["routing_micro"]["recall"] == 0.5
    assert metrics["multi_skill_relation_retention"] == 0.5


def test_recall_at_k_uses_ordered_candidates():
    suite = load_suite(BASELINE_PATH)
    suite["metric_config"]["routing_recall_k"] = 1
    suite["cases"][0]["predicted_atoms"][0]["candidates"] = [
        "python-file-utility-scripts",
        "python-file-grouping",
    ]

    metrics = evaluate_suite(suite)["cases"][0]["metrics"]

    assert metrics["routing_recall_at_k"] == 0.0


def test_prediction_below_alignment_threshold_is_unmatched():
    suite = load_suite(BASELINE_PATH)
    predicted = suite["cases"][0]["predicted_atoms"][0]
    predicted["end_line"] = 2

    metrics = evaluate_suite(suite)["cases"][0]["metrics"]

    assert metrics["routing_micro"]["true_positive"] == 0
    assert metrics["routing_micro"]["false_positive"] == 1
    assert metrics["routing_micro"]["false_negative"] == 1


def test_unknown_routing_label_fails_loudly():
    suite = load_suite(BASELINE_PATH)
    broken = deepcopy(suite)
    broken["cases"][0]["predicted_atoms"][0]["skills"] = ["unknown-skill"]

    with pytest.raises(ReplayValidationError, match="unknown skill labels"):
        validate_suite(broken)


def test_overlapping_gold_ranges_fail_loudly():
    suite = load_suite(BASELINE_PATH)
    broken = deepcopy(suite)
    broken["cases"][1]["gold_atoms"][1]["start_line"] = 8

    with pytest.raises(ReplayValidationError, match="gold atom ranges overlap"):
        validate_suite(broken)


def test_source_line_count_mismatch_fails_loudly():
    suite = load_suite(BASELINE_PATH)
    broken = deepcopy(suite)
    broken["cases"][0]["source_lines"].pop()

    with pytest.raises(ReplayValidationError, match="exactly 12 lines"):
        validate_suite(broken)


def test_unsupported_schema_version_fails_loudly():
    suite = load_suite(BASELINE_PATH)
    suite["schema_version"] = 4

    with pytest.raises(ReplayValidationError, match=r"supported=\[1, 2, 3\], got=4"):
        validate_suite(suite)


@pytest.mark.parametrize("score", [True, -0.1, 1.1, "0.8"])
def test_invalid_boundary_score_fails_loudly(score):
    suite = load_suite(BOUNDARY_SCORE_PATH)
    suite["cases"][0]["boundary_candidates"][0]["boundary_score"] = score

    with pytest.raises(ReplayValidationError, match="boundary_score must satisfy"):
        validate_suite(suite)


def test_boundary_score_thresholds_must_be_strictly_increasing():
    suite = load_suite(BOUNDARY_SCORE_PATH)
    suite["metric_config"]["boundary_score_thresholds"] = [0.5, 0.5]

    with pytest.raises(ReplayValidationError, match="strictly increasing"):
        validate_suite(suite)


def test_selected_boundary_must_map_to_matching_predicted_atom():
    suite = load_suite(BOUNDARY_SCORE_PATH)
    suite["cases"][0]["boundary_candidates"][1]["predicted_atom_id"] = (
        "pred-zh-migrate"
    )

    with pytest.raises(ReplayValidationError, match="selected line must equal"):
        validate_suite(suite)


def test_every_internal_predicted_atom_requires_selected_candidate():
    suite = load_suite(BOUNDARY_SCORE_PATH)
    suite["cases"][0]["boundary_candidates"][1]["selected"] = False
    suite["cases"][0]["boundary_candidates"][1]["predicted_atom_id"] = None

    with pytest.raises(
        ReplayValidationError, match=r"missing=\['pred-zh-validate'\]"
    ):
        validate_suite(suite)


def test_rejected_boundary_cannot_map_to_predicted_atom():
    suite = load_suite(BOUNDARY_SCORE_PATH)
    suite["cases"][0]["boundary_candidates"][0]["predicted_atom_id"] = (
        "pred-zh-migrate"
    )

    with pytest.raises(ReplayValidationError, match="must be null when rejected"):
        validate_suite(suite)


def test_boundary_scores_from_different_algorithm_versions_cannot_be_aggregated():
    suite = load_suite(BOUNDARY_SCORE_PATH)
    suite["cases"][1]["boundary_candidates"][0]["algorithm_version"] = "other-v2"

    with pytest.raises(ReplayValidationError, match="exactly one algorithm_version"):
        validate_suite(suite)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Group files after reading `src/xskill/task_agent.py`.", "en"),
        ("读取 `src/xskill/task_agent.py` 后整理文件。", "zh"),
        ("`src/xskill/task_agent.py`", "unknown"),
    ],
)
def test_language_detection_ignores_technical_tokens(text, expected):
    assert detect_language(text) == expected


def test_unknown_language_counts_as_inconsistent():
    suite = load_suite(BASELINE_PATH)
    suite["cases"][0]["predicted_atoms"][0]["intent"] = "`src/xskill/task_agent.py`"
    suite["cases"][0]["predicted_atoms"][0]["summary"] = "`pytest -q`"

    report = evaluate_suite(suite)

    assert report["cases"][0]["metrics"]["language_consistency"] == 0.0


def test_cli_renders_text_and_json(capsys):
    assert main([str(BASELINE_PATH)]) == 0
    text_output = capsys.readouterr().out
    assert text_output == render_text(evaluate_suite(load_suite(BASELINE_PATH))) + "\n"

    assert main([str(BASELINE_PATH), "--format", "json"]) == 0
    json_output = json.loads(capsys.readouterr().out)
    assert json_output == evaluate_suite(load_suite(BASELINE_PATH))


@pytest.mark.parametrize(
    ("fixture_path", "report_path"),
    [
        (BASELINE_PATH, REPORT_PATH),
        (BOUNDARY_SCORE_PATH, BOUNDARY_SCORE_REPORT_PATH),
    ],
)
def test_existing_json_cli_output_remains_byte_for_byte_stable(
    fixture_path, report_path, capsys
):
    assert main([str(fixture_path), "--format", "json"]) == 0

    assert capsys.readouterr().out == report_path.read_text(encoding="utf-8")
