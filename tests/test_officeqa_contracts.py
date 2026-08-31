"""SkillOpt 数据划分与实验规范测试。"""

from __future__ import annotations

from pathlib import Path

from scripts.bench.officeqa.contracts import (
    FULL_MANIFEST,
    SPLIT_MANIFEST,
    load_json,
    required_keys_present,
    sha256_json,
    uids_for_split,
)
from scripts.bench.officeqa.litellm_usage import (
    aggregate_spend_logs_by_uid,
    merge_usage_into_results,
)

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "benchmarks" / "officeqa" / "examples"


def test_skillopt_split_counts_and_union_match_full():
    split = load_json(SPLIT_MANIFEST)
    full = load_json(FULL_MANIFEST)

    assert split["counts"] == {"train": 50, "val": 24, "test": 172}
    assert len(split["splits"]["train"]) == 50
    assert len(split["splits"]["val"]) == 24
    assert len(split["splits"]["test"]) == 172

    split_uids = set(uids_for_split("full", manifest=split))
    full_uids = {sample["uid"] for sample in full["samples"]}
    assert split_uids == full_uids
    assert len(split_uids) == 246

    difficulty = {sample["uid"]: sample["difficulty"] for sample in full["samples"]}
    for part in ("train", "val", "test"):
        for item in split["splits"][part]:
            assert set(item) == {"uid", "difficulty"}
            assert item["difficulty"] == difficulty[item["uid"]]

    assert sha256_json(split["splits"]) == split["splits_sha256"]


def test_example_contracts_have_required_fields():
    run_config = load_json(EXAMPLES / "run_config.example.json")
    train = load_json(EXAMPLES / "train_provenance.example.json")
    result = load_json(EXAMPLES / "result_record.example.json")
    summary = load_json(EXAMPLES / "summary.example.json")

    assert not required_keys_present(
        run_config,
        [
            "schema_version",
            "run_id",
            "phase",
            "algo",
            "split_manifest",
            "split_name",
            "full_manifest",
            "model",
            "harness",
            "scorer",
        ],
    )
    assert not required_keys_present(
        train,
        [
            "schema_version",
            "run_id",
            "algo",
            "split_manifest",
            "merge_val_into_train",
            "skill_sha256",
        ],
    )
    assert not required_keys_present(
        result,
        ["schema_version", "run_id", "uid", "status"],
    )
    assert not required_keys_present(
        summary,
        [
            "schema_version",
            "run_id",
            "n_total",
            "n_pass",
            "accuracy",
            "status_counts",
        ],
    )
    assert run_config["split_name"] == "test"
    assert train["merge_val_into_train"] is True


def test_litellm_usage_aggregate_and_merge():
    logs = [
        {
            "request_id": "r1",
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "total_tokens": 15,
            "spend": 0.01,
            "metadata": {"run_id": "demo", "uid": "UID0002"},
        },
        {
            "request_id": "r2",
            "prompt_tokens": 3,
            "completion_tokens": 2,
            "total_tokens": 5,
            "spend": 0.002,
            "metadata": {"run_id": "demo", "uid": "UID0002"},
        },
        {
            "request_id": "other",
            "prompt_tokens": 9,
            "completion_tokens": 1,
            "total_tokens": 10,
            "spend": 0.1,
            "metadata": {"run_id": "other-run", "uid": "UID0002"},
        },
    ]
    by_uid = aggregate_spend_logs_by_uid(logs, run_id="demo")
    assert by_uid["UID0002"]["input_tokens"] == 13
    assert by_uid["UID0002"]["output_tokens"] == 7
    assert abs(by_uid["UID0002"]["cost_usd"] - 0.012) < 1e-9
    assert by_uid["UID0002"]["request_ids"] == ["r1", "r2"]

    results = [{"uid": "UID0002", "status": "pass", "usage": {"source": "missing"}}]
    merged = merge_usage_into_results(results, by_uid)
    assert merged[0]["usage"]["source"] == "litellm"
    assert merged[0]["usage"]["total_tokens"] == 20
