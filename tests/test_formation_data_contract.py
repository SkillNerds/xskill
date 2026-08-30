from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from scripts.bench.algorithm_replay.evaluate import ReplayValidationError
from scripts.bench.algorithm_replay.formation_data import (
    _user_header_lines,
    build_formation_payload,
    formation_case_id_map,
    load_raw_suite,
    load_truth_suite,
    main,
    raw_content_sha256,
    validate_raw_suite,
    validate_truth_suite,
)
from xskill.agents.task_agent import _is_user_header as task_agent_is_user_header


FIXTURES = Path("scripts/bench/algorithm_replay/fixtures")
RAW_PATH = FIXTURES / "formation_raw_v1.json"
TRUTH_PATH = FIXTURES / "formation_truth_v1.json"


@pytest.fixture
def raw_suite():
    return json.loads(RAW_PATH.read_text(encoding="utf-8"))


@pytest.fixture
def truth_suite():
    return json.loads(TRUTH_PATH.read_text(encoding="utf-8"))


def test_checked_in_raw_and_truth_contracts_load_separately():
    raw = load_raw_suite(RAW_PATH)
    truth = load_truth_suite(TRUTH_PATH, raw_source=raw)

    assert [case["case_id"] for case in raw["cases"]] == [
        "formation-001",
        "formation-002",
    ]
    assert [case["case_id"] for case in truth["cases"]] == [
        "formation-001",
        "formation-002",
    ]


def test_formation_payload_contains_only_opaque_id_and_raw_content(
    raw_suite,
    truth_suite,
):
    payload = build_formation_payload(raw_suite)
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True)

    assert set(payload) == {"cases"}
    assert all(set(case) == {"case_id", "raw_content"} for case in payload["cases"])
    assert [case["case_id"] for case in payload["cases"]] == [
        "case-000001",
        "case-000002",
    ]
    assert formation_case_id_map(raw_suite) == {
        "case-000001": "formation-001",
        "case-000002": "formation-002",
    }
    assert "gold_atoms" not in serialized
    assert "raw_sha256" not in serialized
    assert "learning_eligibility" not in serialized
    assert "formation-001" not in serialized
    assert raw_suite["dataset_manifest"]["source_uri"] not in serialized
    assert truth_suite["cases"][0]["gold_atoms"][0]["eligibility_reasons"][0] not in serialized


def test_raw_contract_rejects_truth_fields(raw_suite):
    raw_suite["cases"][0]["gold_atoms"] = []

    with pytest.raises(ReplayValidationError, match=r"extra=\['gold_atoms'\]"):
        validate_raw_suite(raw_suite)


def test_truth_contract_rejects_raw_content(raw_suite, truth_suite):
    truth_suite["cases"][0]["raw_content"] = raw_suite["cases"][0]["raw_content"]

    with pytest.raises(ReplayValidationError, match=r"extra=\['raw_content'\]"):
        validate_truth_suite(truth_suite, raw_suite)


def test_truth_hash_binds_annotations_to_exact_raw_content(raw_suite, truth_suite):
    raw_suite["cases"][0]["raw_content"] += "\n## Assistant\nlate mutation"

    with pytest.raises(ReplayValidationError, match="raw_sha256"):
        validate_truth_suite(truth_suite, raw_suite)


def test_raw_content_hash_is_stable():
    assert raw_content_sha256("a\nb") == (
        "sha256:7e18f737311b2dc3b2f269dd78396b0351f14fb66efa879f768cb23181883c78"
    )


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("## User", True),
        ("## User metadata", True),
        ("## Initial Query", True),
        ("## Initial Query metadata", True),
        ("## user", False),
        ("  ## User", False),
        ("## User-guide", False),
        ("## Initial query", False),
    ],
)
def test_structural_user_header_grammar_matches_task_agent(line, expected):
    assert bool(_user_header_lines(f"{line}\ncontent")) is expected
    assert task_agent_is_user_header(line.rstrip()) is expected


def test_machine_noise_turn_remains_a_scorer_hard_negative():
    raw_content = (
        "## Initial Query\ngoal\n## Assistant\ndone\n"
        f"## User\n{'a' * 40}\n## Assistant\nignored"
    )

    assert _user_header_lines(raw_content) == [1, 5]


@pytest.mark.parametrize("invalid_version", [True, 1.0, "1"])
def test_raw_schema_version_requires_a_strict_integer(raw_suite, invalid_version):
    raw_suite["raw_schema_version"] = invalid_version

    with pytest.raises(ReplayValidationError, match="expected an integer"):
        validate_raw_suite(raw_suite)


@pytest.mark.parametrize(
    "field",
    ["truth_schema_version", "annotation_schema_version"],
)
@pytest.mark.parametrize("invalid_version", [True, 1.0, "1"])
def test_truth_schema_versions_require_strict_integers(
    raw_suite,
    truth_suite,
    field,
    invalid_version,
):
    truth_suite[field] = invalid_version

    with pytest.raises(ReplayValidationError, match="expected an integer"):
        validate_truth_suite(truth_suite, raw_suite)


def test_truth_must_label_every_internal_user_boundary(raw_suite, truth_suite):
    truth_suite["cases"][0]["boundaries"] = []

    with pytest.raises(ReplayValidationError, match="must label every candidate"):
        validate_truth_suite(truth_suite, raw_suite)


def test_boundary_line_rejects_boolean(raw_suite, truth_suite):
    truth_suite["cases"][0]["boundaries"][0]["line"] = True

    with pytest.raises(ReplayValidationError, match="expected an integer"):
        validate_truth_suite(truth_suite, raw_suite)


def test_new_goal_cannot_be_annotated_as_keep(raw_suite, truth_suite):
    truth_suite["cases"][0]["boundaries"][0]["decision"] = "keep"

    with pytest.raises(ReplayValidationError, match="new_goal must use"):
        validate_truth_suite(truth_suite, raw_suite)


def test_uncertain_boundary_must_be_marked_ambiguous(raw_suite, truth_suite):
    boundary = truth_suite["cases"][0]["boundaries"][0]
    boundary["type"] = "uncertain"
    boundary["ambiguous"] = False

    with pytest.raises(ReplayValidationError, match="requires ambiguous=true"):
        validate_truth_suite(truth_suite, raw_suite)


def test_noise_boundary_must_be_kept(raw_suite, truth_suite):
    boundary = truth_suite["cases"][0]["boundaries"][0]
    boundary["type"] = "noise"
    boundary["decision"] = "split"

    with pytest.raises(ReplayValidationError, match="noise must use decision='keep'"):
        validate_truth_suite(truth_suite, raw_suite)


def test_canonical_machine_noise_can_be_a_kept_hard_negative(
    raw_suite,
    truth_suite,
):
    raw_case = raw_suite["cases"][1]
    raw_lines = raw_case["raw_content"].splitlines()
    raw_lines[8] = "a" * 40
    raw_case["raw_content"] = "\n".join(raw_lines)

    truth_case = truth_suite["cases"][1]
    truth_case["raw_sha256"] = raw_content_sha256(raw_case["raw_content"])
    truth_case["boundaries"][0]["type"] = "noise"
    truth_case["gold_atoms"][0]["evidence"]["goal"] = [3]

    validate_truth_suite(truth_suite, raw_suite)


def test_evidence_reference_must_stay_inside_its_atom(raw_suite, truth_suite):
    truth_suite["cases"][0]["gold_atoms"][0]["evidence"]["goal"] = [11]

    with pytest.raises(ReplayValidationError, match="outside Atom range"):
        validate_truth_suite(truth_suite, raw_suite)


def test_evidence_reference_cannot_point_to_a_section_header(raw_suite, truth_suite):
    truth_suite["cases"][0]["gold_atoms"][0]["evidence"]["goal"] = [2]

    with pytest.raises(ReplayValidationError, match="does not contain evidence content"):
        validate_truth_suite(truth_suite, raw_suite)


def test_complete_evidence_requires_action_and_terminal_reference(
    raw_suite,
    truth_suite,
):
    atom = truth_suite["cases"][1]["gold_atoms"][0]
    atom["evidence"]["verification"] = []

    with pytest.raises(ReplayValidationError, match="complete evidence requires"):
        validate_truth_suite(truth_suite, raw_suite)


def test_known_outcome_requires_observable_outcome_evidence(raw_suite, truth_suite):
    atom = truth_suite["cases"][0]["gold_atoms"][0]
    atom["evidence"]["outcome"] = []

    with pytest.raises(ReplayValidationError, match="non-unknown outcome requires"):
        validate_truth_suite(truth_suite, raw_suite)


def test_atom_starts_must_match_split_decisions(raw_suite, truth_suite):
    changed = copy.deepcopy(truth_suite)
    changed["cases"][0]["boundaries"][0]["decision"] = "keep"
    changed["cases"][0]["boundaries"][0]["type"] = "uncertain"
    changed["cases"][0]["boundaries"][0]["ambiguous"] = True

    with pytest.raises(ReplayValidationError, match="split decisions"):
        validate_truth_suite(changed, raw_suite)


def test_payload_cli_cannot_emit_scorer_truth(capsys):
    assert main(["payload", str(RAW_PATH)]) == 0

    payload = capsys.readouterr().out
    assert "raw_content" in payload
    assert "gold_atoms" not in payload
    assert "learning_eligibility" not in payload


def test_validate_cli_reports_bound_pair(capsys):
    assert main(["validate", str(RAW_PATH), str(TRUTH_PATH)]) == 0

    report = json.loads(capsys.readouterr().out)
    assert report == {
        "status": "ok",
        "suite_id": "raw-only-formation-contract-v1",
        "case_count": 2,
    }
