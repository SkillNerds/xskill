"""Offline trajectory-to-Skill command tests."""

from __future__ import annotations

import json
import os
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
        "--output", "./artifacts",
    ])
    assert args.kernel_id == "your-demo-algo-kernel"
    assert args.trajectory_dir == "./trajectories"
    assert args.output == "./artifacts"

    with pytest.raises(SystemExit):
        build_parser().parse_args([
            "distill", "your-demo-algo-kernel", "./trajectories",
        ])

    with pytest.raises(SystemExit):
        build_parser().parse_args([
            "distill",
            "--kernel", "your-demo-algo-kernel",
            "--trajectory-dir", "./trajectories",
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
    assert (output / ".xskill" / "registry.db").is_file()
    assert (output / ".xskill" / "kernel_runs.db").is_file()
    assert (output / ".xskill" / "input" / "manifest.json").is_file()
    assert (output / "result.json").is_file()
    assert list((output / "skills").glob("*/SKILL.md"))
    assert {path.name for path in output.iterdir()} == {
        ".xskill", "result.json", "skills",
    }
    result = json.loads((output / "result.json").read_text(encoding="utf-8"))
    assert result["status"] == "success"
    assert result["trajectories"] == {"total": 4, "processed": 1}
    assert len(result["skills"]) == 1
    assert "run_id" not in result
    assert "artifact_dir" not in result
    assert "trajectory_set_id" not in result


def test_offline_distillation_exposes_input_root_and_output_workspace(tmp_path):
    plugin_dir = tmp_path / "plugins"
    kernel_dir = plugin_dir / "root-probe"
    kernel_dir.mkdir(parents=True)
    (kernel_dir / "kernel.py").write_text(
        "from xskill.kernels import BaseKernel, KernelMetadata, KernelRunResult\n"
        "class RootProbe(BaseKernel):\n"
        "    metadata = KernelMetadata(id='root-probe', name='Root Probe', "
        "version='1', description='test', triggers=('manual',))\n"
        "    def run(self, context):\n"
        "        return KernelRunResult(\n"
        "            processed_trajectory_ids=tuple(\n"
        "                item.id for item in context.trajectories.iter()\n"
        "            ),\n"
        "            metrics={\n"
        "                'trajectory_root': str(context.trajectory_root),\n"
        "                'workspace': str(context.workspace),\n"
        "            },\n"
        "        )\n"
        "KERNEL_CLASS = RootProbe\n",
        encoding="utf-8",
    )
    trajectory_dir = _trajectory_dir(tmp_path / "explicit-input", count=2)

    report = run_offline_distillation(
        kernel_id="root-probe",
        trajectory_dir=trajectory_dir,
        plugin_dir=plugin_dir,
        xskill_home=tmp_path / "home",
        output_dir=tmp_path / "artifacts",
        no_progress=True,
    )

    assert report.metrics["trajectory_root"] == str(trajectory_dir.resolve())
    assert report.metrics["workspace"] == str(
        (tmp_path / "artifacts" / ".xskill" / "workspace").resolve()
    )
    assert report.processed == 2


def test_offline_distillation_writes_one_concise_result_on_failure(tmp_path):
    plugin_dir = tmp_path / "plugins"
    kernel_dir = plugin_dir / "failing-kernel"
    kernel_dir.mkdir(parents=True)
    (kernel_dir / "kernel.py").write_text(
        "from xskill.kernels import BaseKernel, KernelMetadata\n"
        "class FailingKernel(BaseKernel):\n"
        "    metadata = KernelMetadata(id='failing-kernel', name='Failing', "
        "version='1', description='test', triggers=('manual',))\n"
        "    def run(self, context):\n"
        "        raise RuntimeError('expected failure')\n"
        "KERNEL_CLASS = FailingKernel\n",
        encoding="utf-8",
    )
    output = tmp_path / "artifacts"

    with pytest.raises(RuntimeError, match="expected failure"):
        run_offline_distillation(
            kernel_id="failing-kernel",
            trajectory_dir=_trajectory_dir(tmp_path / "input", count=1),
            plugin_dir=plugin_dir,
            xskill_home=tmp_path / "home",
            output_dir=output,
            no_progress=True,
        )

    assert {path.name for path in output.iterdir()} == {
        ".xskill", "result.json", "skills",
    }
    result = json.loads((output / "result.json").read_text(encoding="utf-8"))
    assert result["status"] == "error"
    assert result["kernel"] == {"id": "failing-kernel", "version": "1"}
    assert result["trajectories"] == {"total": 1}
    assert result["error"] == "RuntimeError: expected failure"
    assert not (output / "run.json").exists()
    assert not (output / "events.jsonl").exists()


def test_offline_distillation_injects_models_config_and_ignores_interval(
    tmp_path, monkeypatch,
):
    monkeypatch.delenv("LLM_MODEL_NAME", raising=False)
    plugin_dir = tmp_path / "plugins"
    kernel_dir = plugin_dir / "model-probe"
    kernel_dir.mkdir(parents=True)
    (kernel_dir / "kernel.py").write_text(
        "import os\n"
        "from xskill.kernels import BaseKernel, KernelMetadata, KernelRunResult\n"
        "class ModelProbe(BaseKernel):\n"
        "    metadata = KernelMetadata(id='model-probe', name='Model Probe', "
        "version='1', description='test', triggers=('manual',))\n"
        "    def run(self, context, run_interval=0.0001):\n"
        "        return KernelRunResult(metrics={\n"
        "            'llm_model': context.llm.model,\n"
        "            'llm_env': os.environ.get('LLM_MODEL_NAME'),\n"
        "            'config_path': str(context.xskill_config_path),\n"
        "        })\n"
        "KERNEL_CLASS = ModelProbe\n",
        encoding="utf-8",
    )
    xskill_home = tmp_path / "home"
    xskill_home.mkdir()
    config_path = xskill_home / "config.yaml"
    config_path.write_text(
        "llm:\n"
        "  base_url: https://llm.invalid/v1\n"
        "  model: distill-llm\n"
        "  api_key: test-only\n"
        "embedding:\n"
        "  base_url: https://embed.invalid/v1\n"
        "  model: distill-embed\n"
        "  api_key: test-only\n",
        encoding="utf-8",
    )

    report = run_offline_distillation(
        kernel_id="model-probe",
        trajectory_dir=_trajectory_dir(tmp_path / "input", count=1),
        plugin_dir=plugin_dir,
        xskill_home=xskill_home,
        output_dir=tmp_path / "artifacts",
        no_progress=True,
    )

    assert report.metrics == {
        "llm_model": "distill-llm",
        "llm_env": "distill-llm",
        "config_path": str(config_path.resolve()),
    }
    assert "LLM_MODEL_NAME" not in os.environ


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
    assert report.as_dict() == {
        "status": "success",
        "kernel": {"id": "demo", "version": "1.0.0"},
        "trajectories": {"total": 4, "processed": 4},
        "skills": ["demo-skill"],
        "duration_s": 1.25,
        "metrics": {"provider": "diagnostic-only"},
    }
