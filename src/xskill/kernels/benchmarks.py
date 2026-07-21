"""Provider-neutral benchmark execution for algorithm-kernel evaluation.

XSkill runs an explicitly selected external evaluator after the kernel has
published Skills into an isolated directory.  The evaluator owns its dataset,
models, credentials, and scoring logic; XSkill only validates and renders the
standard result envelope.
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


_BENCHMARK_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


@dataclass(frozen=True)
class BenchmarkMetric:
    """One metric row produced by an external evaluator."""

    id: str
    dataset: str
    score: float
    passed: int
    total: int
    split: str
    source: str
    artifact: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "dataset": self.dataset,
            "score": round(self.score, 4),
            "passed": self.passed,
            "total": self.total,
            "split": self.split,
            "source": self.source,
            "artifact": self.artifact,
        }


@dataclass(frozen=True)
class BenchmarkCommand:
    """Validated command manifest owned by an evaluator provider."""

    id: str
    command: tuple[str, ...]
    timeout_s: int
    source_path: Path


def load_benchmark_command(path: Path) -> BenchmarkCommand:
    source = Path(path).expanduser().resolve()
    try:
        document = json.loads(source.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise FileNotFoundError(f"benchmark manifest does not exist: {source}") from None
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid benchmark manifest JSON: {source}: {exc}") from exc
    if not isinstance(document, dict):
        raise ValueError("benchmark manifest must contain a JSON object")
    if document.get("schema_version") != 1:
        raise ValueError("benchmark manifest schema_version must be 1")
    benchmark_id = document.get("id")
    if not isinstance(benchmark_id, str) or not _BENCHMARK_ID_RE.fullmatch(
        benchmark_id
    ):
        raise ValueError(
            "benchmark manifest id must match [a-z0-9][a-z0-9_-]{0,63}"
        )
    raw_command = document.get("command")
    if (
        not isinstance(raw_command, list)
        or not raw_command
        or not all(isinstance(item, str) and item for item in raw_command)
    ):
        raise ValueError("benchmark manifest command must be a non-empty string list")
    timeout_s = document.get("timeout_seconds", 3600)
    if not isinstance(timeout_s, int) or timeout_s < 1:
        raise ValueError("benchmark manifest timeout_seconds must be a positive integer")
    return BenchmarkCommand(
        id=benchmark_id.strip(),
        command=tuple(raw_command),
        timeout_s=timeout_s,
        source_path=source,
    )


def parse_benchmark_result(
    *,
    benchmark_id: str,
    result_path: Path,
) -> tuple[BenchmarkMetric, ...]:
    try:
        document = json.loads(result_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise FileNotFoundError(
            f"benchmark evaluator did not write result JSON: {result_path}"
        ) from None
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid benchmark result JSON: {result_path}: {exc}") from exc
    if not isinstance(document, dict) or document.get("schema_version") != 1:
        raise ValueError("benchmark result must be a schema_version 1 JSON object")
    rows = document.get("metrics")
    if not isinstance(rows, list) or not rows:
        raise ValueError("benchmark result metrics must be a non-empty list")

    metrics: list[BenchmarkMetric] = []
    seen_ids: set[str] = set()
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ValueError(f"benchmark metric row {index} must be an object")
        metric_id = str(row.get("id") or f"{benchmark_id}:{index + 1}")
        if metric_id in seen_ids:
            raise ValueError(f"duplicate benchmark metric id: {metric_id}")
        seen_ids.add(metric_id)
        dataset = row.get("dataset")
        if not isinstance(dataset, str) or not dataset.strip():
            raise ValueError(f"benchmark metric {metric_id} requires dataset")
        score_raw = row.get("score")
        passed_raw = row.get("passed")
        total_raw = row.get("total")
        if (
            isinstance(score_raw, bool)
            or not isinstance(score_raw, (int, float))
            or isinstance(passed_raw, bool)
            or not isinstance(passed_raw, int)
            or isinstance(total_raw, bool)
            or not isinstance(total_raw, int)
        ):
            raise ValueError(
                f"benchmark metric {metric_id} requires numeric score/passed/total"
            )
        score = float(score_raw)
        passed = passed_raw
        total = total_raw
        if not math.isfinite(score) or not 0.0 <= score <= 100.0:
            raise ValueError(
                f"benchmark metric {metric_id} score must be in [0, 100]"
            )
        if total < 1 or not 0 <= passed <= total:
            raise ValueError(
                f"benchmark metric {metric_id} passed/total are outside the valid range"
            )
        expected_score = round(passed / total * 100.0, 4)
        if abs(score - expected_score) > 0.01:
            raise ValueError(
                f"benchmark metric {metric_id} score {score} is inconsistent "
                f"with {passed}/{total} ({expected_score})"
            )
        split = row.get("split", "unspecified")
        source = row.get("source", benchmark_id)
        if not isinstance(split, str) or not split.strip():
            raise ValueError(f"benchmark metric {metric_id} requires a valid split")
        if not isinstance(source, str) or not source.strip():
            raise ValueError(f"benchmark metric {metric_id} requires a valid source")
        metrics.append(BenchmarkMetric(
            id=metric_id,
            dataset=dataset.strip(),
            score=score,
            passed=passed,
            total=total,
            split=split.strip(),
            source=source.strip(),
            artifact=str(result_path),
        ))
    return tuple(metrics)


def run_command_benchmark(
    *,
    manifest_path: Path,
    skills_dir: Path,
    artifact_dir: Path,
    timeout_override_s: int | None = None,
) -> tuple[BenchmarkMetric, ...]:
    """Run a trusted evaluator command without a shell and parse its metrics."""
    manifest = load_benchmark_command(manifest_path)
    skill_root = Path(skills_dir).resolve()
    if not any(skill_root.glob("*/SKILL.md")):
        raise ValueError("benchmark requires at least one generated */SKILL.md")
    timeout_s = manifest.timeout_s if timeout_override_s is None else timeout_override_s
    if timeout_s < 1:
        raise ValueError("benchmark timeout must be a positive number of seconds")

    output_root = Path(artifact_dir).resolve() / "benchmarks" / manifest.id
    output_root.mkdir(parents=True, mode=0o700, exist_ok=False)
    manifest_copy = output_root / "benchmark.json"
    shutil.copy2(manifest.source_path, manifest_copy)
    result_path = output_root / "result.json"
    log_path = output_root / "evaluator.log"
    replacements = {
        "{python}": sys.executable,
        "{skills_dir}": str(skill_root),
        "{artifact_dir}": str(output_root),
        "{result_path}": str(result_path),
    }
    command = []
    for argument in manifest.command:
        expanded = argument
        for marker, value in replacements.items():
            expanded = expanded.replace(marker, value)
        command.append(expanded)

    environment = os.environ.copy()
    environment.update({
        "XSKILL_EVAL_SKILLS_DIR": str(skill_root),
        "XSKILL_EVAL_ARTIFACT_DIR": str(output_root),
        "XSKILL_EVAL_RESULT_PATH": str(result_path),
    })
    try:
        with log_path.open("w", encoding="utf-8") as log:
            completed = subprocess.run(
                command,
                cwd=manifest.source_path.parent,
                env=environment,
                stdout=log,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=timeout_s,
                check=False,
            )
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError(
            f"benchmark exceeded {timeout_s} seconds; see {log_path}"
        ) from exc
    if completed.returncode != 0:
        raise RuntimeError(
            f"benchmark evaluator exited with code {completed.returncode}; see {log_path}"
        )
    return parse_benchmark_result(
        benchmark_id=manifest.id,
        result_path=result_path,
    )
