"""Offline algorithm-kernel evaluation contract tests."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from xskill.kernels.evaluation import (
    _redact,
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


def test_dataset_fraction_is_deterministic_and_content_addressed(tmp_path):
    dataset = _dataset(tmp_path / "dataset")
    first = resolve_dataset(dataset, sample="1/4", seed=42)
    second = resolve_dataset(dataset, sample="1/4", seed=42)
    assert len(first.items) == 1
    assert first.selection_sha256 == second.selection_sha256
    assert first.items[0].id == second.items[0].id

    first.items[0].path.write_text("changed\n", encoding="utf-8")
    changed = resolve_dataset(dataset, sample="1/4", seed=42)
    assert changed.selection_sha256 != first.selection_sha256


def test_local_evaluation_uses_isolated_runtime_and_standard_artifacts(tmp_path):
    plugin_dir = tmp_path / "plugins"
    shutil.copytree(
        Path(__file__).parents[1] / "examples" / "kernels" / "starter",
        plugin_dir / "starter",
    )
    dataset = _dataset(tmp_path / "dataset")
    xskill_home = tmp_path / "home"
    production_registry = xskill_home / "registry.db"
    production_skills = xskill_home / "skill"
    output = tmp_path / "artifacts"

    report = run_local_evaluation(
        kernel_id="starter",
        dataset=dataset,
        plugin_dir=plugin_dir,
        xskill_home=xskill_home,
        sample="1/4",
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
