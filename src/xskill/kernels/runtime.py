"""Kernel execution, run attribution, and algorithm-level evaluation."""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from xskill._sqlite_connect import connect_with_lock
from xskill.kernels.base import KernelInvocation, KernelRunResult, KernelTrigger
from xskill.kernels.catalog import KernelCatalog, KernelDescriptor
from xskill.kernels.context import (
    KernelContext,
    SkillPublisher,
    SkillReader,
    TrajectoryReader,
)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS kernel_runs (
    run_id          TEXT PRIMARY KEY,
    kernel_id       TEXT NOT NULL,
    kernel_version  TEXT NOT NULL,
    trigger         TEXT NOT NULL,
    dataset_id      TEXT NOT NULL,
    status          TEXT NOT NULL CHECK(status IN ('success','error')),
    started_at      TEXT NOT NULL,
    duration_s      REAL NOT NULL,
    input_count     INTEGER NOT NULL DEFAULT 0,
    output_count    INTEGER NOT NULL DEFAULT 0,
    outputs_json    TEXT NOT NULL DEFAULT '[]',
    metrics_json    TEXT NOT NULL DEFAULT '{}',
    notes           TEXT NOT NULL DEFAULT '',
    error           TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_kernel_runs_kernel_started
    ON kernel_runs(kernel_id, started_at DESC);
"""


@dataclass(frozen=True)
class KernelRunRecord:
    run_id: str
    kernel_id: str
    kernel_version: str
    trigger: str
    dataset_id: str
    status: str
    started_at: str
    duration_s: float
    input_count: int
    output_count: int
    outputs: tuple[str, ...]
    metrics: dict
    notes: str = ""
    error: str = ""


@dataclass(frozen=True)
class KernelExecutionLayout:
    """Optional per-run filesystem layout, used by isolated evaluations."""

    skill_dir: Path
    registry_db_path: Path
    evaluation_store: "KernelEvaluationStore"
    workspace: Path
    config_path: Path


class KernelEvaluationStore:
    def __init__(self, path: Path):
        self.path = Path(path)

    def append(self, record: KernelRunRecord) -> None:
        json.dumps(record.metrics, ensure_ascii=False)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connection()) as connection:
            connection.execute(
                "INSERT INTO kernel_runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    record.run_id,
                    record.kernel_id,
                    record.kernel_version,
                    record.trigger,
                    record.dataset_id,
                    record.status,
                    record.started_at,
                    record.duration_s,
                    record.input_count,
                    record.output_count,
                    json.dumps(record.outputs, ensure_ascii=False),
                    json.dumps(record.metrics, ensure_ascii=False),
                    record.notes,
                    record.error[:4000],
                ),
            )
            connection.commit()

    def list_runs(
        self,
        *,
        limit: int = 50,
        kernel_id: str | None = None,
    ) -> list[dict]:
        if limit < 1 or limit > 500:
            raise ValueError("kernel run limit must be in [1, 500]")
        if not self.path.exists():
            return []
        query = "SELECT * FROM kernel_runs"
        params: list[object] = []
        if kernel_id is not None:
            query += " WHERE kernel_id=?"
            params.append(kernel_id)
        query += " ORDER BY started_at DESC LIMIT ?"
        params.append(limit)
        with closing(self._connection()) as connection:
            rows = connection.execute(query, params).fetchall()
        return [self._row_dict(row) for row in rows]

    def summaries(
        self,
        *,
        kernel_ids: list[str],
        skill_dir: Path,
    ) -> list[dict]:
        runs = self.list_runs(limit=500) if self.path.exists() else []
        feedback = self._feedback_by_kernel(skill_dir)
        result: list[dict] = []
        for kernel_id in kernel_ids:
            own_runs = [run for run in runs if run["kernel_id"] == kernel_id]
            successes = [run for run in own_runs if run["status"] == "success"]
            durations = [run["duration_s"] for run in own_runs]
            fb = feedback.get(kernel_id, {"skills": 0, "scores": []})
            scores = fb["scores"]
            result.append({
                "kernel_id": kernel_id,
                "runs": len(own_runs),
                "success_rate": (
                    len(successes) / len(own_runs) if own_runs else None
                ),
                "avg_duration_s": (
                    sum(durations) / len(durations) if durations else None
                ),
                "input_count": sum(run["input_count"] for run in own_runs),
                "output_count": sum(run["output_count"] for run in own_runs),
                "skills_owned": fb["skills"],
                "avg_ux": sum(scores) / len(scores) if scores else None,
                "ux_samples": len(scores),
                "last_run_at": own_runs[0]["started_at"] if own_runs else None,
                "last_status": own_runs[0]["status"] if own_runs else None,
                "last_error": own_runs[0]["error"] if own_runs else "",
                "last_metrics": own_runs[0]["metrics"] if own_runs else {},
            })
        return result

    def _connection(self) -> sqlite3.Connection:
        connection = connect_with_lock(
            sqlite3.connect, str(self.path), timeout=10,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=10000")
        connection.executescript(_SCHEMA)
        return connection

    @staticmethod
    def _row_dict(row: sqlite3.Row) -> dict:
        data = dict(row)
        data["outputs"] = json.loads(data.pop("outputs_json") or "[]")
        data["metrics"] = json.loads(data.pop("metrics_json") or "{}")
        return data

    @staticmethod
    def _feedback_by_kernel(skill_dir: Path) -> dict[str, dict]:
        from xskill import canary
        from xskill.skill.frontmatter import parse

        grouped: dict[str, dict] = {}
        root = Path(skill_dir)
        if not root.is_dir():
            return grouped
        for skill_path in root.iterdir():
            skill_md = skill_path / "SKILL.md"
            if not skill_path.is_dir() or not skill_md.is_file():
                continue
            try:
                frontmatter, _ = parse(skill_md.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            metadata = frontmatter.get("metadata", {}) or {}
            kernel_meta = metadata.get("kernel", {}) if isinstance(metadata, dict) else {}
            kernel_id = (
                str(kernel_meta.get("id") or "native")
                if isinstance(kernel_meta, dict)
                else "native"
            )
            bucket = grouped.setdefault(kernel_id, {"skills": 0, "scores": []})
            bucket["skills"] += 1
            current_main_sha = canary.main_sha(skill_path)
            for score in canary.load_ux_scores(skill_path):
                value = score.get("score")
                if (
                    current_main_sha
                    and score.get("commit_sha") == current_main_sha
                    and isinstance(value, (int, float))
                    and not isinstance(value, bool)
                ):
                    bucket["scores"].append(float(value))
        return grouped


class KernelRuntime:
    def __init__(
        self,
        *,
        active_kernel: str,
        catalog: KernelCatalog,
        skill_dir: Path,
        registry_db_path: Path,
        evaluation_store: KernelEvaluationStore,
    ):
        self.active_kernel = active_kernel
        self.catalog = catalog
        self.skill_dir = Path(skill_dir)
        self.registry_db_path = Path(registry_db_path)
        self.evaluations = evaluation_store

    def run_active(
        self,
        *,
        trigger: KernelTrigger = "scheduled",
        dataset_id: str = "live",
        changed_trajectory_ids: tuple[str, ...] = (),
        full_rebuild: bool = False,
        native_runner: Callable[[KernelInvocation], KernelRunResult],
        execution_layout: KernelExecutionLayout | None = None,
        run_id: str | None = None,
    ) -> tuple[KernelDescriptor, KernelRunResult]:
        descriptor = self.catalog.get(self.active_kernel)
        if not descriptor.available:
            self.catalog.create(self.active_kernel)  # raises the detailed error
        if trigger not in descriptor.triggers:
            raise ValueError(
                f"kernel {descriptor.id} does not support trigger {trigger!r}"
            )
        run_id = str(run_id or uuid.uuid4().hex)
        invocation = KernelInvocation(
            run_id=run_id,
            trigger=trigger,
            dataset_id=str(dataset_id or "live"),
            changed_trajectory_ids=tuple(changed_trajectory_ids),
            full_rebuild=bool(full_rebuild),
        )
        started_wall = datetime.now(timezone.utc).isoformat(timespec="seconds")
        started = time.monotonic()
        published_names: tuple[str, ...] = ()
        skill_dir = (
            execution_layout.skill_dir if execution_layout else self.skill_dir
        )
        registry_db_path = (
            execution_layout.registry_db_path
            if execution_layout else self.registry_db_path
        )
        evaluation_store = (
            execution_layout.evaluation_store
            if execution_layout else self.evaluations
        )
        try:
            if descriptor.id == "native":
                result = native_runner(invocation)
            else:
                workspace = Path(
                    execution_layout.workspace
                    if execution_layout else descriptor.workspace
                ).resolve()
                workspace.mkdir(parents=True, exist_ok=True)
                config_path = (
                    execution_layout.config_path
                    if execution_layout else descriptor.config_path
                )
                if config_path is None:
                    raise RuntimeError(
                        f"kernel {descriptor.id} has no private config path"
                    )
                config_path.parent.mkdir(parents=True, exist_ok=True)
                publisher = SkillPublisher(
                    skill_dir=skill_dir,
                    kernel_id=descriptor.id,
                    kernel_version=descriptor.version,
                    run_id=run_id,
                )
                context = KernelContext(
                    invocation=invocation,
                    workspace=workspace,
                    config_path=config_path,
                    trajectories=TrajectoryReader(registry_db_path),
                    skills=SkillReader(skill_dir, workspace=workspace),
                    publisher=publisher,
                )
                kernel = self.catalog.create(descriptor.id)
                result = kernel.run(context)
                published_names = tuple(item.name for item in publisher.published)
            if not isinstance(result, KernelRunResult):
                raise TypeError(
                    f"kernel {descriptor.id} returned {type(result).__name__}; "
                    "expected KernelRunResult"
                )
            outputs = tuple(dict.fromkeys(
                list(result.submitted_skills) + list(published_names)
            ))
            normalized_result = KernelRunResult(
                processed_trajectory_ids=tuple(result.processed_trajectory_ids),
                submitted_skills=outputs,
                metrics=dict(result.metrics),
                notes=result.notes,
            )
            self._record(
                descriptor, invocation, started_wall, started,
                result=normalized_result,
                evaluation_store=evaluation_store,
            )
            return descriptor, normalized_result
        except BaseException as exc:
            self._record(
                descriptor, invocation, started_wall, started,
                error=f"{type(exc).__name__}: {exc}",
                evaluation_store=evaluation_store,
            )
            raise

    def _record(
        self,
        descriptor: KernelDescriptor,
        invocation: KernelInvocation,
        started_wall: str,
        started: float,
        *,
        result: KernelRunResult | None = None,
        error: str = "",
        evaluation_store: KernelEvaluationStore | None = None,
    ) -> None:
        outputs = tuple(result.submitted_skills) if result else ()
        (evaluation_store or self.evaluations).append(KernelRunRecord(
            run_id=invocation.run_id,
            kernel_id=descriptor.id,
            kernel_version=descriptor.version,
            trigger=invocation.trigger,
            dataset_id=invocation.dataset_id,
            status="error" if error else "success",
            started_at=started_wall,
            duration_s=max(0.0, time.monotonic() - started),
            input_count=(
                len(result.processed_trajectory_ids) if result else 0
            ),
            output_count=len(outputs),
            outputs=outputs,
            metrics=dict(result.metrics) if result else {},
            notes=result.notes if result else "",
            error=error,
        ))
