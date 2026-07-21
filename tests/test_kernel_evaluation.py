"""Offline algorithm-kernel evaluation contract tests."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from xskill.cli import build_parser
from xskill.kernels.benchmarks import (
    BenchmarkMetric,
    load_benchmark_command,
    parse_benchmark_result,
)
from xskill.kernels.evaluation import (
    KernelEvaluationReport,
    _redact,
    render_report_table,
    resolve_dataset,
    run_local_evaluation,
)


def _dataset(root: Path, count: int = 4) -> Path:
    root.mkdir()
    for index in range(count):
        path = root / f"traj_{index}.md"
        path.write_text(f"## User\n\ncase {index}\n", encoding="utf-8")
        path.with_name(path.name + ".meta").write_text(
            '{"kernel_demo": true, "success": true}', encoding="utf-8",
        )
    return root


def test_eval_cli_uses_named_kernel_dataset_and_float_sample():
    args = build_parser().parse_args([
        "eval",
        "--kernel", "your-demo-algo-kernel",
        "--dataset", "./dataset",
        "--sample", "0.25",
    ])
    assert args.kernel_id == "your-demo-algo-kernel"
    assert args.dataset == "./dataset"
    assert args.sample == 0.25
    assert args.benchmark is None

    with pytest.raises(SystemExit):
        build_parser().parse_args([
            "eval", "your-demo-algo-kernel", "./dataset",
        ])
    with pytest.raises(SystemExit):
        build_parser().parse_args([
            "eval",
            "--kernel", "your-demo-algo-kernel",
            "--dataset", "./dataset",
            "--sample", "1/4",
        ])


def test_dataset_fraction_is_deterministic_and_content_addressed(tmp_path):
    dataset = _dataset(tmp_path / "dataset")
    first = resolve_dataset(dataset, sample=0.25, seed=42)
    second = resolve_dataset(dataset, sample=0.25, seed=42)
    assert len(first.items) == 1
    assert first.selection_sha256 == second.selection_sha256
    assert first.items[0].id == second.items[0].id

    first.items[0].path.write_text("changed\n", encoding="utf-8")
    changed = resolve_dataset(dataset, sample=0.25, seed=42)
    assert changed.selection_sha256 != first.selection_sha256


@pytest.mark.parametrize("sample", [0, -0.1, 1.01])
def test_dataset_fraction_rejects_values_outside_open_closed_range(
    tmp_path, sample,
):
    dataset = _dataset(tmp_path / "dataset")
    with pytest.raises(ValueError, match=r"\(0, 1\]"):
        resolve_dataset(dataset, sample=sample, seed=42)


def test_dataset_fraction_rounds_selected_count_up(tmp_path):
    dataset = _dataset(tmp_path / "dataset", count=5)
    selection = resolve_dataset(dataset, sample=0.25, seed=42)
    assert len(selection.items) == 2


def test_local_evaluation_uses_isolated_runtime_and_standard_artifacts(tmp_path):
    plugin_dir = tmp_path / "plugins"
    shutil.copytree(
        Path(__file__).parents[1]
        / "examples" / "kernels" / "your-demo-algo-kernel",
        plugin_dir / "your-demo-algo-kernel",
    )
    dataset = _dataset(tmp_path / "dataset")
    xskill_home = tmp_path / "home"
    production_registry = xskill_home / "registry.db"
    production_skills = xskill_home / "skill"
    output = tmp_path / "artifacts"

    report = run_local_evaluation(
        kernel_id="your-demo-algo-kernel",
        dataset=dataset,
        plugin_dir=plugin_dir,
        xskill_home=xskill_home,
        sample=0.25,
        seed=42,
        output_dir=output,
        no_progress=True,
    )

    assert report.status == "success"
    assert report.selected == 1
    assert report.processed == 1
    assert len(report.submitted_skills) == 1
    assert not production_registry.exists()
    assert not production_skills.exists()
    assert (output / "registry.db").is_file()
    assert (output / "kernel_runs.db").is_file()
    assert (output / "input" / "selection.json").is_file()
    assert (output / "result.json").is_file()
    assert (output / "events.jsonl").is_file()
    run = json.loads((output / "run.json").read_text(encoding="utf-8"))
    assert run["status"] == "success"
    assert run["dataset"]["selected"] == 1
    assert run["config"]["copied_to_artifacts"] is False


def test_provider_metrics_are_recursively_redacted():
    assert _redact({
        "model": {"api_key": "secret", "name": "demo"},
        "auth-token": "secret-two",
    }) == {
        "model": {"api_key": "[REDACTED]", "name": "demo"},
        "auth-token": "[REDACTED]",
    }


def _benchmark_manifest() -> Path:
    return (
        Path(__file__).parents[1]
        / "examples" / "kernels" / "benchmarks"
        / "micro-skill-quality" / "benchmark.json"
    )


def test_benchmark_manifest_is_provider_neutral_command():
    benchmark = load_benchmark_command(_benchmark_manifest())

    assert benchmark.id == "micro-skill-quality"
    assert benchmark.command == ("{python}", "evaluate.py")
    assert benchmark.timeout_s == 60


def test_benchmark_manifest_rejects_path_traversal_id(tmp_path):
    manifest = tmp_path / "benchmark.json"
    manifest.write_text(json.dumps({
        "schema_version": 1,
        "id": "../../outside",
        "command": ["{python}", "evaluate.py"],
    }), encoding="utf-8")

    with pytest.raises(ValueError, match="must match"):
        load_benchmark_command(manifest)


def test_benchmark_result_validates_score_and_passed_count(tmp_path):
    result_path = tmp_path / "result.json"
    result_path.write_text(json.dumps({
        "schema_version": 1,
        "metrics": [{
            "dataset": "spreadsheet",
            "score": 60.0,
            "passed": 6,
            "total": 10,
            "split": "validation",
        }],
    }), encoding="utf-8")

    metric = parse_benchmark_result(
        benchmark_id="provider-benchmark",
        result_path=result_path,
    )[0]

    assert metric.score == 60.0
    assert metric.passed == 6
    assert metric.total == 10
    assert metric.split == "validation"
    assert metric.source == "provider-benchmark"

    document = json.loads(result_path.read_text(encoding="utf-8"))
    document["metrics"][0]["score"] = 86.0
    result_path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ValueError, match="inconsistent"):
        parse_benchmark_result(
            benchmark_id="provider-benchmark",
            result_path=result_path,
        )


def test_local_evaluation_runs_external_benchmark_after_kernel(tmp_path):
    plugin_dir = tmp_path / "plugins"
    shutil.copytree(
        Path(__file__).parents[1]
        / "examples" / "kernels" / "your-demo-algo-kernel",
        plugin_dir / "your-demo-algo-kernel",
    )
    report = run_local_evaluation(
        kernel_id="your-demo-algo-kernel",
        dataset=_dataset(tmp_path / "dataset"),
        plugin_dir=plugin_dir,
        xskill_home=tmp_path / "home",
        sample=0.25,
        seed=42,
        output_dir=tmp_path / "artifacts",
        benchmark_manifest=_benchmark_manifest(),
        no_progress=True,
    )

    assert len(report.benchmarks) == 1
    assert report.benchmarks[0].dataset == "micro-skill-quality"
    assert report.benchmarks[0].score == 100.0
    assert report.benchmarks[0].passed == 1
    assert (tmp_path / "artifacts" / "benchmarks"
            / "micro-skill-quality" / "evaluator.log").is_file()


def test_report_prints_independent_benchmark_metric_table(tmp_path):
    report = KernelEvaluationReport(
        run_id="run-1",
        status="success",
        kernel_id="demo",
        kernel_version="1.0.0",
        dataset_id="train@abc",
        selected=4,
        processed=4,
        submitted_skills=("demo-skill",),
        duration_s=1.25,
        artifact_dir=tmp_path,
        metrics={"provider": "diagnostic-only"},
        notes="",
        benchmarks=(BenchmarkMetric(
            id="provider-validation",
            dataset="spreadsheet",
            score=78.5714,
            passed=11,
            total=14,
            split="validation",
            source="provider-evaluator",
            artifact=str(tmp_path / "benchmark.json"),
        ),),
    )

    rendered = render_report_table(report)
    assert "BENCHMARK METRICS" in rendered
    assert "spreadsheet" in rendered
    assert "78.57%" in rendered
    assert "11/14" in rendered
    assert report.as_dict()["quality_score"] == 78.5714
