from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from xskill.bench.constants import (
    FAILURE_POLICY,
    HARNESS_NOTES,
    SCORERS,
    SKILLOPT_TOOLS,
    XSKILL_TOOLS,
)
from xskill.bench.paths import benchmarks_root, git_head, repo_root
from xskill.bench.skill_io import sha256_file
from xskill.bench.usage import empty_usage_totals, sum_usage


def write_json(path: Path, doc: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def load_validate():
    root = repo_root()
    bench = str(benchmarks_root(root))
    if bench not in sys.path:
        sys.path.insert(0, bench)
    import validate  # type: ignore

    return validate


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def tools_for(algo: str, harness_id: str) -> list[str]:
    if algo == "skillopt" or harness_id == "skillopt_claude_code_exec":
        return list(SKILLOPT_TOOLS)
    return list(XSKILL_TOOLS)


def build_run_config(
    *,
    run_id: str,
    phase: str,
    algo: str,
    benchmark: str,
    split_manifest: str,
    split_name: str,
    model: str,
    harness_id: str,
    skill_sha256: str | None,
    workers: int,
    timeout_s: float,
    max_tool_turns: int,
    seed: int,
    harness_notes: str | None = None,
) -> dict[str, Any]:
    tools = tools_for(algo, harness_id)
    return {
        "schema_version": 1,
        "run_id": run_id,
        "phase": phase,
        "algo": algo,
        "benchmark": benchmark,
        "split_manifest": split_manifest,
        "split_name": split_name,
        "model": model,
        "harness": {
            "id": harness_id,
            "tools": tools,
            # schema 不许加顶层字段，超参对齐说明只能落在这里。
            "notes": harness_notes or HARNESS_NOTES[harness_id],
        },
        "skill_sha256": skill_sha256,
        "scorer": dict(SCORERS[benchmark]),
        "code": {"xskill_commit": git_head()},
        "workers": workers,
        "timeout_s": timeout_s,
        "max_tool_turns": max_tool_turns,
        "retry": {"request_retries": 3, "case_retries": 1},
        "seed": seed,
        "failure_policy": FAILURE_POLICY,
    }


def build_summary(
    *,
    run_id: str,
    benchmark: str,
    split_name: str,
    model: str,
    rows: list[dict[str, Any]],
    run_config_path: Path,
    results_path: Path,
    manifest_path: Path,
    skill_sha256: str | None,
    started_at: str,
    finished_at: str,
    provenance_path: Path | None = None,
) -> dict[str, Any]:
    counts = {"pass": 0, "fail": 0, "timeout": 0, "invalid": 0, "infra_error": 0}
    for row in rows:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    n_total = len(rows)
    n_pass = counts["pass"]
    refs = {
        "run_config_sha256": sha256_file(run_config_path),
        "results_sha256": sha256_file(results_path),
        "skill_sha256": skill_sha256,
        "split_manifest_sha256": sha256_file(manifest_path),
    }
    if provenance_path is not None and provenance_path.is_file():
        refs["train_provenance_sha256"] = sha256_file(provenance_path)
    return {
        "schema_version": 1,
        "run_id": run_id,
        "benchmark": benchmark,
        "split_name": split_name,
        "model": model,
        "n_total": n_total,
        "n_pass": n_pass,
        "accuracy": (n_pass / n_total) if n_total else 0.0,
        "status_counts": counts,
        "usage_totals": sum_usage(rows) if rows else empty_usage_totals(missing_n=0),
        "refs": refs,
        "started_at": started_at,
        "finished_at": finished_at,
        "resumed": False,
    }


def validate_output_dir(output_dir: Path, *, official: bool = True) -> list[str]:
    validate = load_validate()
    return validate.validate_example_dir(output_dir, official=official)
