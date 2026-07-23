"""Kernel execution, run attribution, and operational reporting."""

from __future__ import annotations

import inspect
import json
import os
import sqlite3
import time
import uuid
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator, Mapping

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


def _kernel_environment_values(
    config: Mapping[str, object] | None,
) -> dict[str, str]:
    """Build the documented provider environment without exposing secrets."""
    source = config or {}
    llm = source.get("llm") or {}
    embedding = source.get("embedding") or {}
    if not isinstance(llm, Mapping) or not isinstance(embedding, Mapping):
        raise ValueError("llm and embedding config sections must be mappings")
    candidates = {
        "LLM_BASE_URL": llm.get("base_url"),
        "LLM_MODEL_NAME": llm.get("model"),
        "LLM_API_KEY": llm.get("api_key"),
        "EMBED_BASE_URL": embedding.get("base_url"),
        "EMBED_MODEL_NAME": embedding.get("model"),
        "EMBED_API_KEY": embedding.get("api_key"),
    }
    return {
        key: str(value)
        for key, value in candidates.items()
        if value is not None and str(value)
    }


@contextmanager
def kernel_environment(
    config: Mapping[str, object] | None,
) -> Iterator[None]:
    """Temporarily inject model settings while third-party kernel code runs."""
    updates = _kernel_environment_values(config)
    previous = {key: os.environ.get(key) for key in updates}
    os.environ.update(updates)
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _kernel_model_clients(config: Mapping[str, object] | None):
    """Create lazy-network clients which share XSkill's process limiters."""
    if not config:
        return None, None
    from xskill.utils.llm import EmbedClient, LLMClient

    llm_cfg = config.get("llm") or {}
    embedding_cfg = config.get("embedding") or {}
    if not isinstance(llm_cfg, dict) or not isinstance(embedding_cfg, dict):
        raise ValueError("llm and embedding config sections must be mappings")
    llm = (
        LLMClient.from_config(llm_cfg)
        if llm_cfg.get("base_url") and llm_cfg.get("model")
        else None
    )
    embedding = (
        EmbedClient.from_config(embedding_cfg)
        if embedding_cfg.get("base_url") and embedding_cfg.get("model")
        else None
    )
    return llm, embedding


def kernel_run_interval(kernel, *, default: float = 30.0) -> float:
    """Read the scheduling interval declared by ``run_interval``'s default."""
    parameter = inspect.signature(kernel.run).parameters.get("run_interval")
    value = default if parameter is None else parameter.default
    if value is inspect.Parameter.empty:
        raise TypeError("kernel run_interval must declare a default value")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError("kernel run_interval default must be a number")
    interval = float(value)
    if interval <= 0:
        raise ValueError("kernel run_interval default must be > 0")
    return interval


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
    """Optional per-run filesystem layout, used by isolated executions."""

    skill_dir: Path
    registry_db_path: Path
    evaluation_store: "KernelEvaluationStore"
    workspace: Path
    config_path: Path
    trajectory_root: Path | None = None


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

    def export_report(
        self,
        *,
        kernel_id: str,
        skill_dir: Path,
        registry_db_path: Path | None = None,
        limit: int = 500,
    ) -> dict:
        """Export auditable runs and downstream feedback for one kernel."""
        from xskill import canary
        from xskill.pipeline.registry import pooled_connection
        from xskill.skill.frontmatter import parse

        runs = self.list_runs(limit=limit, kernel_id=kernel_id)
        output_names = {
            str(name)
            for run in runs
            for name in run.get("outputs", [])
        }
        summary = self.summaries(
            kernel_ids=[kernel_id], skill_dir=skill_dir,
        )[0]
        skills: list[dict] = []
        skill_names: list[str] = []
        root = Path(skill_dir)
        if root.is_dir():
            for skill_path in sorted(root.iterdir(), key=lambda item: item.name):
                skill_md = skill_path / "SKILL.md"
                if not skill_path.is_dir() or not skill_md.is_file():
                    continue
                try:
                    frontmatter, _ = parse(skill_md.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                metadata = frontmatter.get("metadata", {}) or {}
                kernel_meta = (
                    metadata.get("kernel", {}) if isinstance(metadata, dict) else {}
                )
                owner = (
                    str(kernel_meta.get("id") or "native")
                    if isinstance(kernel_meta, dict)
                    else "native"
                )
                if owner != kernel_id and skill_path.name not in output_names:
                    continue
                run_versions = sorted({
                    str(run["kernel_version"])
                    for run in runs
                    if skill_path.name in run.get("outputs", [])
                })
                skill_names.append(skill_path.name)
                ux_events = canary.load_ux_scores(skill_path)
                skills.append({
                    "name": skill_path.name,
                    "main_kernel": dict(kernel_meta),
                    "run_kernel_versions": run_versions,
                    "main_commit_sha": canary.main_sha(skill_path),
                    "staging_commit_sha": canary.staging_sha(skill_path),
                    "ux_by_version": canary.aggregate_ux_by_version(ux_events),
                    "ux_events": ux_events,
                })

        decisions: list[dict] = []
        if registry_db_path is not None and skill_names:
            placeholders = ",".join("?" for _ in skill_names)
            with pooled_connection(registry_db_path) as connection:
                rows = connection.execute(
                    "SELECT ts,skill,action,main_avg,staging_avg,"
                    "main_samples,staging_samples,age_days,main_sha,staging_sha "
                    f"FROM canary_decision WHERE skill IN ({placeholders}) "
                    "ORDER BY ts DESC",
                    skill_names,
                ).fetchall()
            decisions = [dict(row) for row in rows]

        return {
            "exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "kernel_id": kernel_id,
            "run_window": {
                "runs_limit": limit,
                "runs_returned": len(runs),
                "summary_limit": 500,
                "order": "started_at_desc",
            },
            "summary": summary,
            "runs": runs,
            "skills": skills,
            "canary_decisions": decisions,
        }

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

        def owner(skill_md: Path, *, default: str = "native") -> str:
            try:
                frontmatter, _ = parse(skill_md.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return default
            metadata = frontmatter.get("metadata", {}) or {}
            kernel_meta = (
                metadata.get("kernel", {}) if isinstance(metadata, dict) else {}
            )
            return (
                str(kernel_meta.get("id") or default)
                if isinstance(kernel_meta, dict)
                else default
            )

        root = Path(skill_dir)
        if not root.is_dir():
            return grouped
        for skill_path in root.iterdir():
            skill_md = skill_path / "SKILL.md"
            if not skill_path.is_dir() or not skill_md.is_file():
                continue
            main_owner = owner(skill_md)
            versions = [(canary.main_sha(skill_path), main_owner)]
            current_staging_sha = canary.staging_sha(skill_path)
            if current_staging_sha:
                staging_md = root / ".canary" / skill_path.name / "SKILL.md"
                versions.append((
                    current_staging_sha,
                    owner(staging_md, default=main_owner),
                ))
            scores = canary.load_ux_scores(skill_path)
            for commit_sha, kernel_id in versions:
                if not commit_sha:
                    continue
                bucket = grouped.setdefault(
                    kernel_id, {"skill_names": set(), "scores": []},
                )
                bucket["skill_names"].add(skill_path.name)
                for score in scores:
                    value = score.get("score")
                    if (
                        score.get("commit_sha") == commit_sha
                        and isinstance(value, (int, float))
                        and not isinstance(value, bool)
                    ):
                        bucket["scores"].append(float(value))
        return {
            kernel_id: {
                "skills": len(bucket["skill_names"]),
                "scores": bucket["scores"],
            }
            for kernel_id, bucket in grouped.items()
        }


class KernelRuntime:
    def __init__(
        self,
        *,
        active_kernel: str,
        catalog: KernelCatalog,
        skill_dir: Path,
        registry_db_path: Path,
        evaluation_store: KernelEvaluationStore,
        trajectory_root: Path | None = None,
        xskill_config: Mapping[str, object] | None = None,
        xskill_config_path: Path | None = None,
    ):
        self.active_kernel = active_kernel
        self.catalog = catalog
        self.skill_dir = Path(skill_dir)
        self.registry_db_path = Path(registry_db_path)
        self.evaluations = evaluation_store
        self.xskill_config = dict(xskill_config or {})
        self.xskill_config_path = Path(
            xskill_config_path
            if xskill_config_path is not None
            else catalog.xskill_home / "config.yaml"
        ).expanduser().resolve()
        self.llm, self.embedding = _kernel_model_clients(self.xskill_config)
        self._kernel_instances = {}
        self.trajectory_root = Path(
            trajectory_root
            if trajectory_root is not None
            else catalog.xskill_home / "team_trajectories" / "clients"
        ).expanduser().resolve()

    def external_run_interval(self) -> float:
        """Return the selected external kernel's declared interval."""
        if self.active_kernel == "native":
            raise ValueError("native kernel uses the XSkill worker scheduler")
        return kernel_run_interval(self._external_kernel(self.active_kernel))

    def _external_kernel(self, kernel_id: str):
        kernel = self._kernel_instances.get(kernel_id)
        if kernel is None:
            with kernel_environment(self.xskill_config):
                kernel = self.catalog.create(kernel_id)
            self._kernel_instances[kernel_id] = kernel
        return kernel

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
        trajectory_root = Path(
            execution_layout.trajectory_root
            if execution_layout and execution_layout.trajectory_root is not None
            else self.trajectory_root
        ).expanduser().resolve()
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
                    xskill_config_path=self.xskill_config_path,
                    trajectories=TrajectoryReader(
                        registry_db_path,
                        root=trajectory_root,
                    ),
                    skills=SkillReader(skill_dir, workspace=workspace),
                    publisher=publisher,
                    llm=self.llm,
                    embedding=self.embedding,
                )
                kernel = self._external_kernel(descriptor.id)
                with kernel_environment(self.xskill_config):
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
