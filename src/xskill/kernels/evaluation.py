"""Isolated local contract evaluation for algorithm kernels.

This runner measures operational behavior (inputs processed, Skills emitted,
duration, and provider metrics).  Benchmark quality and simulated user UX are
separate evaluator backends and are never inferred from self-reported metrics.
"""

from __future__ import annotations

import hashlib
import json
import math
import shutil
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from xskill.kernels.base import KernelRunResult
from xskill.kernels.catalog import KernelCatalog
from xskill.kernels.runtime import (
    KernelEvaluationStore,
    KernelExecutionLayout,
    KernelRuntime,
)
from xskill.pipeline.registry import register_dir


_SENSITIVE_KEY_PARTS = (
    "api_key", "apikey", "token", "secret", "password", "credential",
    "authorization", "auth",
)


@dataclass(frozen=True)
class DatasetItem:
    id: str
    path: Path
    relative_path: str
    md_sha256: str
    sidecars: tuple[tuple[Path, str], ...]


@dataclass(frozen=True)
class DatasetSelection:
    dataset_id: str
    source: Path
    eligible: int
    items: tuple[DatasetItem, ...]
    selection_sha256: str
    sample: float
    seed: int


@dataclass(frozen=True)
class KernelEvaluationReport:
    run_id: str
    status: str
    kernel_id: str
    kernel_version: str
    dataset_id: str
    selected: int
    processed: int
    submitted_skills: tuple[str, ...]
    duration_s: float
    artifact_dir: Path
    metrics: dict
    notes: str

    def as_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "status": self.status,
            "kernel": {
                "id": self.kernel_id,
                "version": self.kernel_version,
            },
            "dataset_id": self.dataset_id,
            "selected": self.selected,
            "processed": self.processed,
            "submitted_skills": list(self.submitted_skills),
            "duration_s": round(self.duration_s, 4),
            "artifact_dir": str(self.artifact_dir),
            "metrics": _redact(self.metrics),
            "notes": self.notes,
            "quality_score": None,
            "quality_score_note": (
                "Local contract evaluation does not simulate user UX or a "
                "held-out benchmark. Configure a benchmark backend for quality."
            ),
        }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _redact(value):
    """Recursively remove secrets from provider-controlled report objects."""
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            normalized = str(key).lower().replace("-", "_")
            if any(part in normalized for part in _SENSITIVE_KEY_PARTS):
                result[str(key)] = "[REDACTED]"
            else:
                result[str(key)] = _redact(item)
        return result
    if isinstance(value, (list, tuple)):
        return [_redact(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return repr(value)


def resolve_dataset(path: Path, *, sample: float, seed: int) -> DatasetSelection:
    root = Path(path).expanduser().resolve()
    if not root.is_dir():
        raise NotADirectoryError(f"evaluation dataset is not a directory: {root}")
    discovered = sorted(root.rglob("traj_*.md"))
    if not discovered:
        raise ValueError(f"dataset contains no traj_*.md files: {root}")

    items: list[DatasetItem] = []
    seen_ids: set[str] = set()
    for path_item in discovered:
        if path_item.is_symlink() or not path_item.is_file():
            continue
        relative = path_item.relative_to(root).as_posix()
        item_id = relative.removesuffix(".md")
        if item_id in seen_ids:
            raise ValueError(f"duplicate dataset item id: {item_id}")
        seen_ids.add(item_id)
        sidecars: list[tuple[Path, str]] = []
        for candidate in (
            path_item.with_suffix(".json"),
            path_item.with_name(path_item.name + ".meta"),
        ):
            if candidate.is_file() and not candidate.is_symlink():
                sidecars.append((candidate, _sha256(candidate)))
        items.append(DatasetItem(
            id=item_id,
            path=path_item,
            relative_path=relative,
            md_sha256=_sha256(path_item),
            sidecars=tuple(sidecars),
        ))

    fraction = float(sample)
    if not 0 < fraction <= 1:
        raise ValueError("sample must be a floating-point ratio in (0, 1]")
    ordered = sorted(
        items,
        key=lambda item: hashlib.sha256(
            f"{seed}\0{item.id}".encode("utf-8")
        ).hexdigest(),
    )
    selected_count = max(1, math.ceil(len(ordered) * fraction))
    selected = tuple(ordered[:selected_count])
    identity_rows = []
    for item in sorted(selected, key=lambda entry: entry.id):
        identity_rows.append({
            "id": item.id,
            "path": item.relative_path,
            "md_sha256": item.md_sha256,
            "sidecars": [
                {
                    "name": sidecar.name,
                    "sha256": digest,
                }
                for sidecar, digest in item.sidecars
            ],
        })
    selection_sha = hashlib.sha256(json.dumps(
        identity_rows, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()
    return DatasetSelection(
        dataset_id=f"{root.name}@{selection_sha[:12]}",
        source=root,
        eligible=len(items),
        items=selected,
        selection_sha256=selection_sha,
        sample=sample,
        seed=seed,
    )


def _write_json(path: Path, value: object, *, mode: int = 0o600) -> None:
    temporary = path.with_name(path.name + ".tmp")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary.write_text(
        json.dumps(_redact(value), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.chmod(mode)
    temporary.replace(path)


def _append_event(path: Path, *, phase: str, current: int, total: int, message: str) -> None:
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "phase": phase,
        "current": current,
        "total": total,
        "message": message,
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def _materialize_selection(
    selection: DatasetSelection,
    *,
    input_root: Path,
    registry_db: Path,
) -> list[Path]:
    grouped: dict[str, Path] = {}
    for item in selection.items:
        source_parent = item.path.parent.relative_to(selection.source).as_posix()
        source_id = hashlib.sha256(source_parent.encode("utf-8")).hexdigest()[:12]
        target_root = input_root / "watch_dirs" / source_id
        target_root.mkdir(parents=True, exist_ok=True)
        target = target_root / item.path.name
        if target.exists():
            raise ValueError(
                f"dataset contains colliding trajectory basename: {item.path.name}"
            )
        shutil.copy2(item.path, target)
        for sidecar, _digest in item.sidecars:
            shutil.copy2(sidecar, target_root / sidecar.name)
        grouped[source_id] = target_root
    for source_id, path in sorted(grouped.items()):
        register_dir(
            path,
            label=f"evaluation:{selection.dataset_id}:{source_id}",
            auto_index=False,
            ecosystem="evaluation",
            db_path=registry_db,
        )
    return list(grouped.values())


def run_local_evaluation(
    *,
    kernel_id: str,
    dataset: Path,
    plugin_dir: Path,
    xskill_home: Path,
    sample: float = 1.0,
    seed: int = 42,
    output_dir: Path | None = None,
    json_output: bool = False,
    no_progress: bool = False,
) -> KernelEvaluationReport:
    """Execute a third-party kernel against an isolated trajectory snapshot."""
    from tqdm import tqdm

    selection = resolve_dataset(dataset, sample=sample, seed=seed)
    catalog = KernelCatalog(plugin_dir=plugin_dir, xskill_home=xskill_home)
    descriptor = catalog.get(kernel_id)
    if "evaluation" not in descriptor.triggers:
        raise ValueError(
            f"kernel {kernel_id} does not declare the 'evaluation' trigger"
        )
    run_id = uuid.uuid4().hex
    if output_dir is None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        output_dir = (
            Path(xskill_home) / "evaluations"
            / f"{stamp}-{kernel_id}-{selection.source.name}-{run_id[:8]}"
        )
    artifact_dir = Path(output_dir).expanduser().resolve()
    if artifact_dir.exists():
        raise FileExistsError(f"evaluation artifact directory exists: {artifact_dir}")
    artifact_dir.mkdir(parents=True, mode=0o700)

    manifest_path = artifact_dir / "run.json"
    events_path = artifact_dir / "events.jsonl"
    input_root = artifact_dir / "input"
    registry_db = artifact_dir / "registry.db"
    isolated_skills = artifact_dir / "skills"
    workspace = artifact_dir / "kernel" / "workspace"
    evaluation_store = KernelEvaluationStore(artifact_dir / "kernel_runs.db")
    isolated_config = artifact_dir / "kernel" / "config.yaml"
    # A configured provider keeps ownership of its private config.  It is read
    # in place and never copied into artifacts; default-only examples can create
    # an isolated config without exposing production secrets.
    config_path = (
        descriptor.config_path
        if descriptor.config_path is not None and descriptor.config_path.is_file()
        else isolated_config
    )
    started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    manifest = {
        "schema_version": 1,
        "run_id": run_id,
        "status": "running",
        "started_at": started_at,
        "kernel": {
            "id": descriptor.id,
            "version": descriptor.version,
            "api_version": 2,
        },
        "dataset": {
            "id": selection.dataset_id,
            "source_name": selection.source.name,
            "eligible": selection.eligible,
            "selected": len(selection.items),
            "sample": selection.sample,
            "seed": selection.seed,
            "selection_sha256": selection.selection_sha256,
        },
        "config": {
            "copied_to_artifacts": False,
            "provider_owned": bool(config_path == descriptor.config_path),
        },
    }
    _write_json(manifest_path, manifest)
    progress = tqdm(
        total=4,
        desc=f"xskill eval {kernel_id}",
        unit="phase",
        disable=no_progress or json_output,
        file=sys.stderr,
    )
    started = time.monotonic()
    try:
        _append_event(events_path, phase="snapshot", current=0, total=4,
                      message="materializing selected trajectories")
        _materialize_selection(
            selection, input_root=input_root, registry_db=registry_db,
        )
        selection_document = {
            "schema_version": 1,
            "dataset_id": selection.dataset_id,
            "selection_sha256": selection.selection_sha256,
            "items": [
                {
                    "id": item.id,
                    "relative_path": item.relative_path,
                    "md_sha256": item.md_sha256,
                    "sidecars": [
                        {"name": source.name, "sha256": digest}
                        for source, digest in item.sidecars
                    ],
                }
                for item in selection.items
            ],
        }
        _write_json(input_root / "selection.json", selection_document)
        progress.update(1)

        _append_event(events_path, phase="kernel", current=1, total=4,
                      message="running kernel in isolated layout")
        runtime = KernelRuntime(
            active_kernel=kernel_id,
            catalog=catalog,
            skill_dir=Path(xskill_home) / "skill",
            registry_db_path=Path(xskill_home) / "registry.db",
            evaluation_store=KernelEvaluationStore(
                Path(xskill_home) / "kernel_runs.db"
            ),
        )
        layout = KernelExecutionLayout(
            skill_dir=isolated_skills,
            registry_db_path=registry_db,
            evaluation_store=evaluation_store,
            workspace=workspace,
            config_path=config_path,
        )

        def native_not_supported(_invocation) -> KernelRunResult:
            raise RuntimeError(
                "native kernel local evaluation needs a benchmark adapter"
            )

        _, result = runtime.run_active(
            trigger="evaluation",
            dataset_id=selection.dataset_id,
            full_rebuild=True,
            native_runner=native_not_supported,
            execution_layout=layout,
            run_id=run_id,
        )
        progress.update(1)

        _append_event(events_path, phase="report", current=3, total=4,
                      message="writing standardized result")
        duration = max(0.0, time.monotonic() - started)
        report = KernelEvaluationReport(
            run_id=run_id,
            status="success",
            kernel_id=descriptor.id,
            kernel_version=descriptor.version,
            dataset_id=selection.dataset_id,
            selected=len(selection.items),
            processed=len(result.processed_trajectory_ids),
            submitted_skills=tuple(result.submitted_skills),
            duration_s=duration,
            artifact_dir=artifact_dir,
            metrics=dict(result.metrics),
            notes=result.notes,
        )
        _write_json(artifact_dir / "result.json", report.as_dict())
        progress.update(1)
        manifest.update({
            "status": "success",
            "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "result": "result.json",
        })
        _write_json(manifest_path, manifest)
        _append_event(events_path, phase="finalize", current=4, total=4,
                      message="evaluation complete")
        progress.update(1)
        return report
    except BaseException as exc:
        manifest.update({
            "status": "error",
            "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "error": f"{type(exc).__name__}: {exc}",
        })
        _write_json(manifest_path, manifest)
        _append_event(events_path, phase="error", current=progress.n, total=4,
                      message=f"{type(exc).__name__}: {exc}")
        raise
    finally:
        progress.close()


def render_report_table(report: KernelEvaluationReport) -> str:
    headers = (
        "KERNEL", "VERSION", "DATASET", "SELECTED", "PROCESSED",
        "SKILLS", "STATUS", "DURATION", "QUALITY", "ARTIFACTS",
    )
    row = (
        report.kernel_id,
        report.kernel_version,
        report.dataset_id,
        str(report.selected),
        str(report.processed),
        str(len(report.submitted_skills)),
        report.status,
        f"{report.duration_s:.2f}s",
        "n/a*",
        str(report.artifact_dir),
    )
    widths = [max(len(headers[index]), len(row[index])) for index in range(len(row))]
    header_line = "  ".join(
        value.ljust(widths[index]) for index, value in enumerate(headers)
    )
    row_line = "  ".join(
        value.ljust(widths[index]) for index, value in enumerate(row)
    )
    return (
        f"{header_line}\n{row_line}\n"
        "* quality/UX is intentionally not inferred from kernel metrics; "
        "use a benchmark backend."
    )
