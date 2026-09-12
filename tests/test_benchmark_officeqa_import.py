"""第二步：从榜上冻结评测导入四个合同文件。不调模型。"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BENCH = ROOT / "benchmarks"
IMPORTER = ROOT / "scripts" / "bench" / "officeqa" / "import_leaderboard_eval.py"
MINI = ROOT / "tests" / "fixtures" / "bench_officeqa_import_mini"
KIND_RESULTS = (
    "/models/xarena/officeqa-full-official-test172/"
    "eval-sub-200-5aa5bd/eval/results.jsonl"
)

sys.path.insert(0, str(BENCH))
sys.path.insert(0, str(IMPORTER.parent))
import import_leaderboard_eval as importer  # noqa: E402
import validate  # noqa: E402


def _load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_splitter_readme_still_untouched():
    text = (ROOT / "scripts" / "bench" / "README.md").read_text(encoding="utf-8")
    assert "轨迹拆分" in text
    assert "officeqa" not in text.lower()
    assert "import_leaderboard" not in text


def test_classify_timeout_only_from_fail_reason():
    assert importer.classify_status({"hard": 1, "success": True, "fail_reason": ""}) == "pass"
    assert (
        importer.classify_status(
            {"hard": 0, "success": False, "fail_reason": "predicted '1' but expected '2'"}
        )
        == "fail"
    )
    assert (
        importer.classify_status(
            {"hard": 0, "success": False, "fail_reason": "claude exited with rc=124"}
        )
        == "timeout"
    )
    assert (
        importer.classify_status(
            {
                "hard": 0,
                "success": False,
                "fail_reason": "predicted 'timeout later' but expected '3'",
            }
        )
        == "fail"
    )
    assert (
        importer.classify_status(
            {"hard": 0, "success": False, "fail_reason": "no result json (rc=1)"}
        )
        == "infra_error"
    )


def test_mini_import_writes_four_files(tmp_path: Path):
    out = tmp_path / "run"
    proc = subprocess.run(
        [
            sys.executable,
            str(IMPORTER),
            "--source-dir",
            str(MINI),
            "--output-dir",
            str(out),
            "--run-id",
            "eval-officeqa-xskill-mini-import",
            "--allow-partial-split",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    for name in ("run_config.json", "train_provenance.json", "results.jsonl", "summary.json"):
        assert (out / name).is_file(), name

    rows = [
        json.loads(line)
        for line in (out / "results.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    by_uid = {r["uid"]: r for r in rows}
    assert by_uid["UID0004"]["status"] == "pass" and by_uid["UID0004"]["is_correct"] is True
    assert by_uid["UID0006"]["status"] == "fail" and by_uid["UID0006"]["is_correct"] is False
    assert by_uid["UID0009"]["status"] == "timeout" and by_uid["UID0009"]["is_correct"] is False
    assert by_uid["UID0009"]["pred_answer"] is None

    summary = _load(out / "summary.json")
    assert summary["n_total"] == 3
    assert summary["n_pass"] == 1
    assert summary["status_counts"]["timeout"] == 1
    assert summary["status_counts"]["fail"] == 1
    assert abs(summary["accuracy"] - 1 / 3) < 1e-9
    assert summary["usage_totals"]["usage_missing_n"] == 3

    cfg = _load(out / "run_config.json")
    assert cfg["harness"]["tools"] == ["Read", "Bash", "Skill"]
    assert "Write" not in cfg["harness"]["tools"]
    assert "Write" in cfg["harness"]["notes"]
    assert cfg["scorer"]["id"] == validate.OFFICEQA_SCORER_ID
    assert validate.validate_run_config(cfg, official=True) == []

    prov = _load(out / "train_provenance.json")
    assert prov["merge_val_into_train"] is False
    assert prov["algo_components"]["train_submission_id"] == 197
    assert validate.validate_train_provenance(prov, official=False) == []
    official_prov = validate.validate_train_provenance(prov, official=True)
    assert any("merge" in e for e in official_prov)


def test_mini_import_does_not_touch_splitter_files():
    replay = ROOT / "scripts" / "bench" / "algorithm_replay" / "evaluate.py"
    assert replay.is_file()
    text = replay.read_text(encoding="utf-8")
    assert "import_leaderboard_eval" not in text


def _kind_sub200_available() -> bool:
    proc = subprocess.run(
        ["docker", "exec", "lb-control-plane", "test", "-f", KIND_RESULTS],
        capture_output=True,
        check=False,
    )
    return proc.returncode == 0


@pytest.mark.skipif(not _kind_sub200_available(), reason="kind sub200 artifacts missing")
def test_kind_sub200_import_counts(tmp_path: Path):
    out = tmp_path / "sub200"
    proc = subprocess.run(
        [
            sys.executable,
            str(IMPORTER),
            "--from-kind-job",
            "eval-sub-200-5aa5bd",
            "--output-dir",
            str(out),
            "--run-id",
            "eval-officeqa-xskill-sub200-import",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout
    summary = _load(out / "summary.json")
    assert summary["n_total"] == 172
    assert summary["n_pass"] == 120
    assert summary["status_counts"]["timeout"] == 11
    assert summary["status_counts"]["fail"] == 41
    assert summary["status_counts"]["invalid"] == 0
    assert abs(summary["accuracy"] - 120 / 172) < 1e-9
    assert importer.validate_imported_run(out) == []

    rows = [
        json.loads(line)
        for line in (out / "results.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    manifest = _load(
        ROOT / "benchmarks" / "officeqa" / "manifests" / "officeqa_skillopt_id_split.json"
    )
    assert {r["uid"] for r in rows} == set(manifest["test"])
    timeout_uids = {r["uid"] for r in rows if r["status"] == "timeout"}
    assert "UID0010" in timeout_uids
    assert all(r["is_correct"] is False for r in rows if r["status"] == "timeout")
