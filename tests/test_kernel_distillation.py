"""Offline trajectory-to-Skill command tests."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from xskill.cli import build_parser
from xskill.kernels.distillation import (
    OfflineDistillationReport,
    _redact,
    render_distillation_table,
    resolve_trajectory_directory,
    run_offline_distillation,
)


def _trajectory_dir(root: Path, count: int = 4) -> Path:
    root.mkdir()
    for index in range(count):
        path = root / f"traj_{index}.md"
        path.write_text(f"## User\n\ncase {index}\n", encoding="utf-8")
        path.with_name(path.name + ".meta").write_text(
            '{"kernel_demo": true, "success": true}', encoding="utf-8",
        )
    return root


def test_distill_cli_uses_named_kernel_and_trajectory_directory():
    args = build_parser().parse_args([
        "distill",
        "--kernel", "your-demo-algo-kernel",
        "--trajectory-dir", "./trajectories",
    ])
    assert args.kernel_id == "your-demo-algo-kernel"
    assert args.trajectory_dir == "./trajectories"

    with pytest.raises(SystemExit):
        build_parser().parse_args([
            "distill", "your-demo-algo-kernel", "./trajectories",
        ])


def test_trajectory_directory_reads_all_files_and_is_content_addressed(tmp_path):
    trajectory_dir = _trajectory_dir(tmp_path / "trajectories")
    first = resolve_trajectory_directory(trajectory_dir)
    second = resolve_trajectory_directory(trajectory_dir)
    assert len(first.items) == 4
    assert first.content_sha256 == second.content_sha256
    assert first.trajectory_set_id == second.trajectory_set_id

    first.items[0].path.write_text("changed\n", encoding="utf-8")
    changed = resolve_trajectory_directory(trajectory_dir)
    assert changed.content_sha256 != first.content_sha256


def test_trajectory_directory_requires_trajectory_markdown(tmp_path):
    trajectory_dir = tmp_path / "trajectories"
    trajectory_dir.mkdir()
    with pytest.raises(ValueError, match=r"no traj_\*\.md"):
        resolve_trajectory_directory(trajectory_dir)


def test_offline_distillation_uses_isolated_runtime_and_standard_artifacts(
    tmp_path,
):
    plugin_dir = tmp_path / "plugins"
    shutil.copytree(
        Path(__file__).parents[1]
        / "examples" / "kernels" / "your-demo-algo-kernel",
        plugin_dir / "your-demo-algo-kernel",
    )
    trajectory_dir = _trajectory_dir(tmp_path / "trajectories")
    xskill_home = tmp_path / "home"
    production_registry = xskill_home / "registry.db"
    production_skills = xskill_home / "skill"
    output = tmp_path / "artifacts"

    report = run_offline_distillation(
        kernel_id="your-demo-algo-kernel",
        trajectory_dir=trajectory_dir,
        plugin_dir=plugin_dir,
        xskill_home=xskill_home,
        output_dir=output,
        no_progress=True,
    )

    assert report.status == "success"
    assert report.trajectories == 4
    assert report.processed == 1
    assert len(report.submitted_skills) == 1
    assert not production_registry.exists()
    assert not production_skills.exists()
    assert (output / "registry.db").is_file()
    assert (output / "kernel_runs.db").is_file()
    assert (output / "input" / "trajectories.json").is_file()
    assert (output / "result.json").is_file()
    assert (output / "events.jsonl").is_file()
    assert list((output / "skills").glob("*/SKILL.md"))
    run = json.loads((output / "run.json").read_text(encoding="utf-8"))
    assert run["status"] == "success"
    assert run["input"]["trajectories"] == 4
    assert run["config"]["copied_to_artifacts"] is False


def test_provider_metrics_are_recursively_redacted():
    assert _redact({
        "model": {"api_key": "secret", "name": "demo"},
        "auth-token": "secret-two",
    }) == {
        "model": {"api_key": "[REDACTED]", "name": "demo"},
        "auth-token": "[REDACTED]",
    }


def test_distillation_report_contains_counts_without_quality_score(tmp_path):
    report = OfflineDistillationReport(
        run_id="run-1",
        status="success",
        kernel_id="demo",
        kernel_version="1.0.0",
        trajectory_set_id="input@abc",
        trajectories=4,
        processed=4,
        submitted_skills=("demo-skill",),
        duration_s=1.25,
        artifact_dir=tmp_path,
        metrics={"provider": "diagnostic-only"},
        notes="",
    )

    rendered = render_distillation_table(report)
    assert "TRAJECTORIES" in rendered
    assert "PROCESSED" in rendered
    assert "QUALITY" not in rendered
    assert "BENCHMARK" not in rendered
    assert "quality_score" not in report.as_dict()
