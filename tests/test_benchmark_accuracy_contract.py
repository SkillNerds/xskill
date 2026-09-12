"""精度评测第一步合同：只测 benchmarks/ 下的 schema、名单和样例。

不 import 生产管线，不改 scripts/bench 拆分器，不访问网络。
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BENCH = ROOT / "benchmarks"
VALIDATE = BENCH / "validate.py"

sys.path.insert(0, str(BENCH))
import validate  # noqa: E402


def _load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_splitter_bench_untouched():
    readme = ROOT / "scripts" / "bench" / "README.md"
    assert readme.is_file()
    text = readme.read_text(encoding="utf-8")
    assert "轨迹拆分" in text
    assert "officeqa" not in text.lower()


def test_validate_cli_official_tree_ok():
    proc = subprocess.run(
        [sys.executable, str(VALIDATE)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith("ok ")


@pytest.mark.parametrize("bench,counts", [
    ("officeqa", (50, 24, 172)),
    ("spreadsheet", (80, 40, 280)),
    ("alfworld", (39, 18, 134)),
])
def test_manifest_official_counts_and_id_only(bench, counts):
    path = BENCH / bench / "manifests" / f"{bench}_skillopt_id_split.json"
    doc = _load(path)
    assert validate.validate_split_manifest(doc) == []
    assert (len(doc["train"]), len(doc["val"]), len(doc["test"])) == counts
    blob = json.dumps(doc)
    assert "question" not in doc
    assert "ground_truth" not in blob or bench != "officeqa" or "ground_truth" not in doc
    for uid in doc["train"][:3] + doc["test"][:3]:
        assert isinstance(uid, str) and uid


def test_officeqa_readme_rejects_reward_py():
    text = (BENCH / "officeqa" / "README.md").read_text(encoding="utf-8")
    assert "reward.py" in text
    assert "不用" in text or "不要" in text
    assert "evaluate" in text


def test_officeqa_eval_example_uses_skillopt_scorer():
    cfg = _load(BENCH / "officeqa" / "examples" / "eval_xskill" / "run_config.json")
    assert cfg["scorer"]["id"] == validate.OFFICEQA_SCORER_ID
    assert "reward" not in cfg["scorer"]["id"]
    assert cfg["harness"]["tools"] == ["Read", "Bash", "Skill"]


def test_xskill_train_example_merges_val():
    doc = _load(BENCH / "officeqa" / "examples" / "train_xskill" / "train_provenance.json")
    assert doc["merge_val_into_train"] is True
    assert doc["selection_split"] is None
    manifest = _load(BENCH / "officeqa" / "manifests" / "officeqa_skillopt_id_split.json")
    expected = validate.sha256_sorted_uids(manifest["train"] + manifest["val"])
    assert doc["train_uids_sha256"] == expected


def test_skillopt_train_example_keeps_val_gate():
    doc = _load(BENCH / "officeqa" / "examples" / "train_skillopt" / "train_provenance.json")
    assert doc["merge_val_into_train"] is False
    assert doc["selection_split"] == "val"
    assert doc["optimizer_backend"] == "openai_chat"


def test_timeout_row_is_not_correct():
    rows = [
        json.loads(line)
        for line in (BENCH / "officeqa" / "examples" / "eval_xskill" / "results.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    timeout = next(r for r in rows if r["status"] == "timeout")
    assert timeout["is_correct"] is False
    summary = _load(BENCH / "officeqa" / "examples" / "eval_xskill" / "summary.json")
    assert summary["n_total"] == 3
    assert summary["n_pass"] == 1
    assert abs(summary["accuracy"] - 1 / 3) < 1e-9
    assert summary["status_counts"]["timeout"] == 1


def _file_sha(path: Path) -> str:
    data = path.read_bytes().replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    return hashlib.sha256(data).hexdigest()


def test_summary_refs_match_files():
    ex = BENCH / "officeqa" / "examples" / "eval_xskill"
    summary = _load(ex / "summary.json")
    assert summary["refs"]["run_config_sha256"] == _file_sha(ex / "run_config.json")
    assert summary["refs"]["results_sha256"] == _file_sha(ex / "results.jsonl")
    manifest = BENCH / "officeqa" / "manifests" / "officeqa_skillopt_id_split.json"
    assert summary["refs"]["split_manifest_sha256"] == _file_sha(manifest)


def test_missing_run_id_rejected():
    cfg = _load(BENCH / "officeqa" / "examples" / "eval_xskill" / "run_config.json")
    del cfg["run_id"]
    errors = validate.validate_run_config(cfg, official=False)
    assert any("run_id" in e for e in errors)


def test_write_tool_rejected():
    cfg = _load(BENCH / "officeqa" / "examples" / "eval_xskill" / "run_config.json")
    cfg["harness"]["tools"] = ["Read", "Write", "Bash", "Skill"]
    errors = validate.validate_run_config(cfg, official=True)
    assert any("Write" in e or "tools" in e for e in errors)


def test_officeqa_reward_py_scorer_rejected():
    cfg = _load(BENCH / "officeqa" / "examples" / "eval_xskill" / "run_config.json")
    cfg["scorer"]["id"] = "databricks/officeqa/reward.py"
    errors = validate.validate_run_config(cfg, official=True)
    assert any("reward.py" in e or "OFFICEQA_SCORER" in e or "must be" in e for e in errors)


def test_xskill_without_merge_val_rejected():
    doc = _load(BENCH / "officeqa" / "examples" / "train_xskill" / "train_provenance.json")
    doc["merge_val_into_train"] = False
    errors = validate.validate_train_provenance(doc, official=True)
    assert any("merge" in e for e in errors)


def test_pass_with_is_correct_false_rejected():
    rec = {
        "schema_version": 1,
        "run_id": "x",
        "uid": "UID1",
        "gold_answer": "1",
        "pred_answer": "1",
        "status": "pass",
        "is_correct": False,
    }
    errors = validate.validate_result_record(rec)
    assert errors


def test_accuracy_mismatch_rejected():
    summary = _load(BENCH / "officeqa" / "examples" / "eval_xskill" / "summary.json")
    bad = deepcopy(summary)
    bad["accuracy"] = 0.99
    errors = validate.validate_summary(bad)
    assert any("accuracy" in e for e in errors)


def test_schemas_are_objects():
    for name in (
        "run_config.schema.json",
        "train_provenance.schema.json",
        "result_record.schema.json",
        "summary.schema.json",
        "split_manifest.schema.json",
    ):
        doc = _load(BENCH / "schemas" / name)
        assert doc["type"] == "object"
        assert "required" in doc
