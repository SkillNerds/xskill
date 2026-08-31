"""Real-model recorder contracts without network calls."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

from scripts.bench.algorithm_replay.evaluate import (
    ReplayValidationError,
    evaluate_suite,
    validate_suite,
)
from scripts.bench.algorithm_replay.record import (
    ModelCallResult,
    _write_json,
    main,
    record_suite,
    validate_source_suite,
)

FIXTURE_DIR = (
    Path(__file__).parent.parent / "scripts" / "bench" / "algorithm_replay" / "fixtures"
)
SOURCE_PATH = FIXTURE_DIR / "qwen38_baseline_v3.json"

pytestmark = pytest.mark.algorithm_replay


def _source() -> dict:
    return json.loads(SOURCE_PATH.read_text(encoding="utf-8"))


def _split_payload(source: dict) -> dict:
    cases = []
    for case in source["cases"]:
        atoms = []
        for gold in case["gold_atoms"]:
            atoms.append(
                {
                    "start_line": gold["start_line"],
                    "intent": gold["intent"],
                    "summary": gold["summary"],
                }
            )
        gold_by_start = {
            gold["start_line"]: gold
            for gold in case["gold_atoms"]
            if gold["start_line"] not in {item[0] for item in case["scorable_ranges"]}
        }
        candidates = []
        for line in case["candidate_lines"]:
            gold = gold_by_start.get(line)
            candidates.append(
                {
                    "line": line,
                    "boundary_score": 0.9 if gold else 0.2,
                    "selected": gold is not None,
                }
            )
        cases.append(
            {
                "case_id": case["case_id"],
                "boundary_candidates": candidates,
                "predicted_atoms": atoms,
            }
        )
    return {"cases": cases}


def _route_payload(source: dict, split_payload: dict) -> dict:
    source_by_id = {case["case_id"]: case for case in source["cases"]}
    cases = []
    for split_case in split_payload["cases"]:
        source_case = source_by_id[split_case["case_id"]]
        atoms = []
        for atom_index, (_predicted, gold) in enumerate(
            zip(split_case["predicted_atoms"], source_case["gold_atoms"]),
            start=1,
        ):
            atoms.append(
                {
                    "id": f"pred-{split_case['case_id']}-{atom_index:04d}",
                    "skills": gold["skills"],
                    "candidates": gold["candidates"],
                    "weight_scores": [
                        {"skill": skill, "weightscore": 8} for skill in gold["skills"]
                    ],
                }
            )
        cases.append({"case_id": split_case["case_id"], "atoms": atoms})
    return {"cases": cases}


def _config() -> dict:
    return {
        "llm": {
            "base_url": "http://model.invalid/v1",
            "model": "base-model",
            "api_key": "test-secret",
            "max_tokens": 2048,
            "temperature": 0.0,
        },
        "llm_agents": {
            "split": {"model": "split-model"},
            "cluster": {"model": "route-model"},
        },
        "pricing": {
            "split-model": {"input_per_1m": 0.0, "output_per_1m": 0.0},
            "route-model": {"input_per_1m": 0.0, "output_per_1m": 0.0},
        },
    }


def _caller_for(source: dict, *, broken_route_score: bool = False):
    split_payload = _split_payload(source)
    route_payload = _route_payload(source, split_payload)
    if broken_route_score:
        route_payload["cases"][0]["atoms"][0]["weight_scores"][0]["weightscore"] = "8"

    def call(stage, _config, _system, prompt, _seed, _top_p, _prices):
        prompt_input = json.loads(prompt.split("INPUT_JSON:\n", 1)[1])
        case_ids = {case["case_id"] for case in prompt_input["cases"]}
        full_payload = split_payload if stage == "split" else route_payload
        payload = {
            "cases": [
                case for case in full_payload["cases"] if case["case_id"] in case_ids
            ]
        }
        return ModelCallResult(
            content=json.dumps(payload, ensure_ascii=False),
            input_tokens=100 if stage == "split" else 80,
            output_tokens=40 if stage == "split" else 30,
            cache_read_tokens=10,
            cost_usd=0.0,
            price_source="config",
            generation_seconds=0.25,
        )

    return call


def test_record_source_fixture_is_privacy_safe_and_valid():
    source = _source()

    validate_source_suite(source)

    serialized = SOURCE_PATH.read_text(encoding="utf-8")
    assert "api_key" not in serialized
    assert "DESKTOP-" not in serialized
    assert "/home/" not in serialized
    assert {case["scenario"] for case in source["cases"]} == {
        "new_goal",
        "continuation",
        "user_correction",
        "failed_retry",
        "near_duplicate_request",
        "multi_skill",
    }


@pytest.mark.parametrize("source_version", [True, "1", 1.0, 2])
def test_source_schema_version_requires_the_exact_integer(source_version):
    source = _source()
    source["source_schema_version"] = source_version

    with pytest.raises(ReplayValidationError, match="source_schema_version"):
        validate_source_suite(source)


@pytest.mark.parametrize("source_version", [True, "1", 1.0, 2])
def test_recorded_v3_schema_version_requires_the_exact_integer(source_version):
    suite = _source()
    suite["source_schema_version"] = source_version

    with pytest.raises(ReplayValidationError, match="source_schema_version"):
        validate_suite(suite)


def test_checked_in_qwen_recording_replays_with_stable_provenance():
    suite = _source()

    validate_suite(suite)
    report = evaluate_suite(suite)

    assert report["report_sha256"] == (
        "babf67f9295f04fb26bdb5bb23dad4854788d41a9ae15d25b7d9e45e2fa072ed"
    )
    assert suite["stage_manifests"]["split"]["calls"] == 6
    assert suite["stage_manifests"]["route"]["calls"] == 1
    assert suite["stage_manifests"]["split"]["input_tokens"] == 2138
    assert suite["stage_manifests"]["split"]["output_tokens"] == 908
    assert suite["stage_manifests"]["route"]["input_tokens"] == 1249
    assert suite["stage_manifests"]["route"]["output_tokens"] == 400
    assert report["metrics"]["boundary"]["f1"] == 1.0
    assert report["metrics"]["coverage"] == 1.0
    assert report["metrics"]["routing_micro"]["f1"] == 1.0
    assert report["metrics"]["multi_skill_relation_retention"] == 1.0
    assert (
        report["metrics"]["routing_error_association"]["low_score_error_auroc"] is None
    )


def test_record_suite_keeps_stage_provenance_and_replays_offline():
    source = _source()
    calls = []
    caller = _caller_for(source)

    def recording_caller(*args):
        calls.append(args[0])
        return caller(*args)

    suite = record_suite(
        source,
        _config(),
        repository_revision="abc123",
        generated_at="2026-08-28T00:00:00Z",
        model_caller=recording_caller,
    )

    validate_suite(suite)
    report = evaluate_suite(suite)
    assert suite["schema_version"] == 3
    assert suite["source_schema_version"] == 1
    assert "run_manifest" not in suite
    assert suite["cases"][0]["scenario"] == "new_goal"
    assert suite["cases"][0]["candidate_lines"] == [5]
    assert suite["stage_manifests"]["split"]["model"] == "split-model"
    assert suite["stage_manifests"]["route"]["model"] == "route-model"
    assert suite["stage_manifests"]["split"]["input_tokens"] == 600
    assert suite["stage_manifests"]["route"]["output_tokens"] == 30
    assert suite["stage_manifests"]["split"]["calls"] == len(source["cases"])
    assert suite["stage_manifests"]["route"]["calls"] == 1
    assert calls == ["split"] * len(source["cases"]) + ["route"]
    assert "api_key" not in json.dumps(suite)
    assert "base_url" not in json.dumps(suite)
    assert report["boundary_algorithm_version"] == "offline-boundary-ranker-v1"
    assert report["route_algorithm_version"] == "offline-skill-router-v1"
    assert report["metrics"]["boundary"]["f1"] == 1.0
    assert report["metrics"]["routing_micro"]["f1"] == 1.0
    assert report["metrics"]["multi_skill_relation_retention"] == 1.0


def test_record_suite_rejects_missing_candidate_output_before_routing():
    source = _source()
    split_payload = _split_payload(source)
    split_payload["cases"][0]["boundary_candidates"].clear()
    calls = []

    def caller(stage, _config, _system, _prompt, _seed, _top_p, _prices):
        calls.append(stage)
        return ModelCallResult(
            content=json.dumps({"cases": [split_payload["cases"][0]]}),
            input_tokens=1,
            output_tokens=1,
            cache_read_tokens=0,
            cost_usd=0.0,
            price_source="config",
            generation_seconds=0.0,
        )

    with pytest.raises(ReplayValidationError, match="score every candidate line"):
        record_suite(
            source,
            _config(),
            repository_revision="abc123",
            model_caller=caller,
        )
    assert calls == ["split"]


def test_record_suite_rejects_non_integer_candidate_line_cleanly():
    source = _source()
    split_payload = _split_payload(source)
    split_payload["cases"][0]["boundary_candidates"][0]["line"] = [5]

    def caller(_stage, _config, _system, _prompt, _seed, _top_p, _prices):
        return ModelCallResult(
            content=json.dumps({"cases": [split_payload["cases"][0]]}),
            input_tokens=1,
            output_tokens=1,
            cache_read_tokens=0,
            cost_usd=0.0,
            price_source="config",
            generation_seconds=0.0,
        )

    with pytest.raises(
        ReplayValidationError, match="candidate line must be an integer"
    ):
        record_suite(
            source,
            _config(),
            repository_revision="abc123",
            model_caller=caller,
        )


def test_record_suite_rejects_string_weightscore_without_coercion():
    source = _source()

    with pytest.raises(ReplayValidationError, match=r"weight_scores\[0\]\.weightscore"):
        record_suite(
            source,
            _config(),
            repository_revision="abc123",
            model_caller=_caller_for(source, broken_route_score=True),
        )


def test_v3_rejects_stage_algorithm_mismatch():
    source = _source()
    suite = record_suite(
        source,
        _config(),
        repository_revision="abc123",
        model_caller=_caller_for(source),
    )
    broken = deepcopy(suite)
    broken["stage_manifests"]["split"]["algorithm_version"] = "other-split"

    with pytest.raises(ReplayValidationError, match="match the split manifest"):
        validate_suite(broken)


def test_v3_rejects_ambiguous_legacy_run_manifest():
    source = _source()
    suite = record_suite(
        source,
        _config(),
        repository_revision="abc123",
        model_caller=_caller_for(source),
    )
    suite["run_manifest"] = deepcopy(suite["stage_manifests"]["split"])

    with pytest.raises(ReplayValidationError, match="not allowed in schema v3"):
        validate_suite(suite)


def test_route_calls_follow_configured_atom_batch_size():
    source = _source()
    config = _config()
    config["agent_worker"] = {"pools": {"cluster": {"batch_size": 2}}}
    calls = []
    caller = _caller_for(source)

    def recording_caller(*args):
        calls.append(args[0])
        return caller(*args)

    suite = record_suite(
        source,
        config,
        repository_revision="abc123",
        model_caller=recording_caller,
    )

    assert suite["stage_manifests"]["route"]["calls"] == 4
    assert calls.count("route") == 4


def test_invalid_first_route_batch_stops_later_model_calls():
    source = _source()
    config = _config()
    config["agent_worker"] = {"pools": {"cluster": {"batch_size": 2}}}
    calls = []
    caller = _caller_for(source, broken_route_score=True)

    def recording_caller(*args):
        calls.append(args[0])
        return caller(*args)

    with pytest.raises(ReplayValidationError, match="weightscore"):
        record_suite(
            source,
            config,
            repository_revision="abc123",
            model_caller=recording_caller,
        )

    assert calls == ["split"] * len(source["cases"]) + ["route"]


def test_invalid_route_batch_size_fails_before_model_calls():
    source = _source()
    config = _config()
    config["agent_worker"] = {"pools": {"cluster": {"batch_size": 0}}}
    calls = []

    def caller(*args):
        calls.append(args[0])
        return _caller_for(source)(*args)

    with pytest.raises(ValueError, match="batch_size must be a positive integer"):
        record_suite(
            source,
            config,
            repository_revision="abc123",
            model_caller=caller,
        )

    assert calls == []


def test_recorder_does_not_overwrite_without_explicit_flag(tmp_path):
    output = tmp_path / "recorded.json"
    _write_json(output, {"first": True}, overwrite=False)

    with pytest.raises(FileExistsError, match="pass --overwrite"):
        _write_json(output, {"second": True}, overwrite=False)

    assert json.loads(output.read_text(encoding="utf-8")) == {"first": True}


def test_cli_rejects_existing_output_before_reading_config(tmp_path):
    output = tmp_path / "recorded.json"
    _write_json(output, {"first": True}, overwrite=False)

    with pytest.raises(FileExistsError, match="pass --overwrite"):
        main(
            [
                str(SOURCE_PATH),
                str(output),
                "--config",
                str(tmp_path / "missing.yaml"),
            ]
        )
