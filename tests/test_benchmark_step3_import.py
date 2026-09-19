"""第三步：Spreadsheet、ALFWorld、no-skill 从榜上记录导入四个文件。"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
IMPORTER = ROOT / "scripts" / "bench" / "contract_import.py"
SS_MINI = ROOT / "tests" / "fixtures" / "bench_spreadsheet_import_mini"
ALF_MINI = ROOT / "tests" / "fixtures" / "bench_alfworld_import_mini"

sys.path.insert(0, str(ROOT / "scripts" / "bench"))
import contract_import as importer  # noqa: E402


def _load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _run_import(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(IMPORTER), *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def test_splitter_readme_untouched():
    text = (ROOT / "scripts" / "bench" / "README.md").read_text(encoding="utf-8")
    assert "轨迹拆分" in text
    assert "officeqa" not in text.lower()
    assert "spreadsheet-sub181" not in text


def test_classify_spreadsheet_timeout_labels():
    assert importer.classify_status({"hard": 1, "fail_reason": ""}) == "pass"
    assert (
        importer.classify_status({"hard": 0, "fail_reason": "eval-mismatch: value@A1"})
        == "fail"
    )
    assert (
        importer.classify_status({"hard": 0, "fail_reason": "task-timeout-600s", "error": "timeout"})
        == "timeout"
    )
    assert (
        importer.classify_status({"hard": 0, "fail_reason": "exec-error: timeout after 120s"})
        == "timeout"
    )
    assert (
        importer.classify_status(
            {"hard": 0, "fail_reason": "predicted 'timeout later' but expected '3'"}
        )
        == "fail"
    )


def test_spreadsheet_mini_import(tmp_path: Path):
    out = tmp_path / "ss"
    proc = _run_import(
        [
            "--preset",
            "spreadsheet-sub181",
            "--source-dir",
            str(SS_MINI),
            "--output-dir",
            str(out),
            "--run-id",
            "eval-spreadsheet-xskill-mini-import",
            "--allow-partial-split",
        ]
    )
    assert proc.returncode == 0, proc.stderr
    summary = _load(out / "summary.json")
    assert summary["n_total"] == 3
    assert summary["n_pass"] == 1
    assert summary["status_counts"] == {
        "pass": 1,
        "fail": 1,
        "timeout": 1,
        "invalid": 0,
        "infra_error": 0,
    }
    cfg = _load(out / "run_config.json")
    assert cfg["benchmark"] == "spreadsheet"
    assert cfg["harness"]["tools"] == ["Read", "Bash", "Skill"]
    assert importer.validate_imported_run(out) == []


def test_alfworld_mini_import(tmp_path: Path):
    out = tmp_path / "alf"
    proc = _run_import(
        [
            "--preset",
            "alfworld-sub81",
            "--source-dir",
            str(ALF_MINI),
            "--output-dir",
            str(out),
            "--run-id",
            "eval-alfworld-xskill-mini-import",
            "--allow-partial-split",
        ]
    )
    assert proc.returncode == 0, proc.stderr
    rows = [
        json.loads(line)
        for line in (out / "results.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    by = {r["uid"]: r for r in rows}
    assert by["test:0000"]["status"] == "pass"
    assert by["test:0001"]["status"] == "fail"
    assert by["test:0002"]["status"] == "timeout"
    assert by["test:0000"]["gold_answer"] == "won"
    cfg = _load(out / "run_config.json")
    assert cfg["benchmark"] == "alfworld"
    assert importer.validate_imported_run(out) == []


def _kind_has(board: str, job: str) -> bool:
    path = f"/models/xarena/{board}/{job}/eval/results.jsonl"
    try:
        proc = subprocess.run(
            ["docker", "exec", "lb-control-plane", "test", "-f", path],
            capture_output=True,
            check=False,
        )
    except FileNotFoundError:
        return False
    return proc.returncode == 0


KIND_CASES = [
    (
        "spreadsheet-sub181",
        "spreadsheet-full-official-test280",
        "eval-sub-181-fc3615",
        {"n_total": 280, "n_pass": 248, "timeout": 3, "fail": 29},
    ),
    (
        "spreadsheet-sub182",
        "spreadsheet-full-official-test280",
        "eval-sub-182-7fedec",
        {"n_total": 280, "n_pass": 246, "timeout": 2, "fail": 32},
    ),
    (
        "alfworld-sub81",
        "alfworld-full-official-test134",
        "eval-sub-81-9390c3",
        {"n_total": 134, "n_pass": 113, "timeout": 0, "fail": 21},
    ),
    (
        "alfworld-sub80",
        "alfworld-full-official-test134",
        "eval-sub-80-e1ab2b",
        {"n_total": 134, "n_pass": 104, "timeout": 0, "fail": 30},
    ),
    (
        "alfworld-sub86",
        "alfworld-full-official-test134",
        "eval-sub-86-388c4e",
        {"n_total": 134, "n_pass": 69, "timeout": 0, "fail": 65},
    ),
]


@pytest.mark.parametrize("preset,board,job,expect", KIND_CASES)
def test_kind_preset_counts(tmp_path: Path, preset: str, board: str, job: str, expect: dict):
    if not _kind_has(board, job):
        pytest.skip(f"kind missing {job}")
    out = tmp_path / preset
    proc = _run_import(["--preset", preset, "--output-dir", str(out)])
    assert proc.returncode == 0, proc.stderr + proc.stdout
    summary = _load(out / "summary.json")
    assert summary["n_total"] == expect["n_total"]
    assert summary["n_pass"] == expect["n_pass"]
    assert summary["status_counts"]["timeout"] == expect["timeout"]
    assert summary["status_counts"]["fail"] == expect["fail"]
    assert abs(summary["accuracy"] - expect["n_pass"] / expect["n_total"]) < 1e-9
    assert importer.validate_imported_run(out) == []
    cfg = _load(out / "run_config.json")
    if preset.endswith("sub182") or preset.endswith("sub80"):
        assert cfg["algo"] == "skillopt"
        assert cfg["harness"]["id"] == "skillopt_claude_code_exec"
        assert cfg["harness"]["tools"] == ["Read", "Bash"]
    elif preset.endswith("sub86"):
        assert cfg["algo"] == "noskill"
        assert not (out / "train_provenance.json").exists()
    else:
        assert cfg["algo"] == "xskill"
        assert cfg["harness"]["tools"] == ["Read", "Bash", "Skill"]
