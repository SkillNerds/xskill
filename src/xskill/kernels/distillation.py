"""Offline trajectory-to-Skill execution for algorithm kernels.

The command copies a trajectory directory into an isolated runtime, invokes one
kernel, and saves the generated Skills and run records.  It does not score the
algorithm or simulate user feedback.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import yaml

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
class TrajectoryInput:
    id: str
    path: Path
    relative_path: str
    md_sha256: str
    sidecars: tuple[tuple[Path, str], ...]


@dataclass(frozen=True)
class TrajectorySet:
    trajectory_set_id: str
    source: Path
    items: tuple[TrajectoryInput, ...]
    content_sha256: str


@dataclass(frozen=True)
class OfflineDistillationReport:
    run_id: str
    status: str
    kernel_id: str
    kernel_version: str
    trajectory_set_id: str
    trajectories: int
    processed: int
    submitted_skills: tuple[str, ...]
    duration_s: float
    artifact_dir: Path
    metrics: dict
    notes: str

    def as_dict(self, *, include_artifact_dir: bool = False) -> dict:
        result = {
            "status": self.status,
            "kernel": {
                "id": self.kernel_id,
                "version": self.kernel_version,
            },
            "trajectories": {
                "total": self.trajectories,
                "processed": self.processed,
            },
            "skills": list(self.submitted_skills),
            "duration_s": round(self.duration_s, 4),
        }
        if self.metrics:
            result["metrics"] = _redact(self.metrics)
        if self.notes:
            result["notes"] = self.notes
        if include_artifact_dir:
            result["artifact_dir"] = str(self.artifact_dir)
        return result


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


def resolve_trajectory_directory(path: Path) -> TrajectorySet:
    root = Path(path).expanduser().resolve()
    if not root.is_dir():
        raise NotADirectoryError(f"trajectory input is not a directory: {root}")

    items: list[TrajectoryInput] = []
    seen_ids: set[str] = set()
    for path_item in sorted(root.rglob("traj_*.md")):
        if path_item.is_symlink() or not path_item.is_file():
            continue
        relative = path_item.relative_to(root).as_posix()
        item_id = relative.removesuffix(".md")
        if item_id in seen_ids:
            raise ValueError(f"duplicate trajectory id: {item_id}")
        seen_ids.add(item_id)
        sidecars: list[tuple[Path, str]] = []
        for candidate in (
            path_item.with_suffix(".json"),
            path_item.with_name(path_item.name + ".meta"),
        ):
            if candidate.is_file() and not candidate.is_symlink():
                sidecars.append((candidate, _sha256(candidate)))
        items.append(TrajectoryInput(
            id=item_id,
            path=path_item,
            relative_path=relative,
            md_sha256=_sha256(path_item),
            sidecars=tuple(sidecars),
        ))
    if not items:
        raise ValueError(f"trajectory directory contains no traj_*.md files: {root}")

    identity_rows = [{
        "id": item.id,
        "path": item.relative_path,
        "md_sha256": item.md_sha256,
        "sidecars": [
            {"name": sidecar.name, "sha256": digest}
            for sidecar, digest in item.sidecars
        ],
    } for item in items]
    content_sha = hashlib.sha256(json.dumps(
        identity_rows, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()
    return TrajectorySet(
        trajectory_set_id=f"{root.name}@{content_sha[:12]}",
        source=root,
        items=tuple(items),
        content_sha256=content_sha,
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


def _materialize_trajectories(
    trajectory_set: TrajectorySet,
    *,
    input_root: Path,
    registry_db: Path,
) -> None:
    grouped: dict[str, Path] = {}
    for item in trajectory_set.items:
        source_parent = item.path.parent.relative_to(trajectory_set.source).as_posix()
        source_id = hashlib.sha256(source_parent.encode("utf-8")).hexdigest()[:12]
        target_root = input_root / "watch_dirs" / source_id
        target_root.mkdir(parents=True, exist_ok=True)
        target = target_root / item.path.name
        if target.exists():
            raise ValueError(
                f"trajectory directory contains colliding filename: {item.path.name}"
            )
        shutil.copy2(item.path, target)
        for sidecar, _digest in item.sidecars:
            shutil.copy2(sidecar, target_root / sidecar.name)
        grouped[source_id] = target_root
    for source_id, grouped_path in sorted(grouped.items()):
        register_dir(
            grouped_path,
            label=f"offline-distill:{trajectory_set.trajectory_set_id}:{source_id}",
            auto_index=False,
            ecosystem="offline-distill",
            db_path=registry_db,
        )


def run_offline_distillation(
    *,
    kernel_id: str,
    trajectory_dir: Path,
    plugin_dir: Path,
    xskill_home: Path,
    output_dir: Path,
    json_output: bool = False,
    no_progress: bool = False,
) -> OfflineDistillationReport:
    """Run a kernel against an isolated copy of a trajectory directory."""
    from tqdm import tqdm

    trajectory_set = resolve_trajectory_directory(trajectory_dir)
    xskill_config_path = (Path(xskill_home) / "config.yaml").resolve()
    xskill_config: dict = {}
    if xskill_config_path.is_file():
        loaded_config = yaml.safe_load(
            xskill_config_path.read_text(encoding="utf-8")
        ) or {}
        if not isinstance(loaded_config, dict):
            raise ValueError("xskill config must be a mapping")
        xskill_config = loaded_config
    catalog = KernelCatalog(plugin_dir=plugin_dir, xskill_home=xskill_home)
    descriptor = catalog.get(kernel_id)
    if "manual" not in descriptor.triggers:
        raise ValueError(f"kernel {kernel_id} does not declare the 'manual' trigger")

    run_id = uuid.uuid4().hex
    artifact_dir = Path(output_dir).expanduser().resolve()
    if artifact_dir.exists():
        raise FileExistsError(
            f"offline distillation artifact directory exists: {artifact_dir}"
        )
    artifact_dir.mkdir(parents=True, mode=0o700)

    result_path = artifact_dir / "result.json"
    internal_root = artifact_dir / ".xskill"
    input_root = internal_root / "input"
    registry_db = internal_root / "registry.db"
    isolated_skills = artifact_dir / "skills"
    isolated_skills.mkdir(mode=0o700)
    workspace = internal_root / "workspace"
    run_store = KernelEvaluationStore(internal_root / "kernel_runs.db")
    isolated_config = internal_root / "config.yaml"
    config_path = (
        descriptor.config_path
        if descriptor.config_path is not None and descriptor.config_path.is_file()
        else isolated_config
    )
    initial_result = {
        "status": "running",
        "kernel": {
            "id": descriptor.id,
            "version": descriptor.version,
        },
        "trajectories": {
            "total": len(trajectory_set.items),
        },
    }
    _write_json(result_path, initial_result)
    phase_total = 3
    progress = tqdm(
        total=phase_total,
        desc=f"xskill distill {kernel_id}",
        unit="phase",
        disable=no_progress or json_output,
        file=sys.stderr,
    )
    started = time.monotonic()
    try:
        _materialize_trajectories(
            trajectory_set, input_root=input_root, registry_db=registry_db,
        )
        _write_json(input_root / "manifest.json", {
            "run_id": run_id,
            "id": trajectory_set.trajectory_set_id,
            "content_sha256": trajectory_set.content_sha256,
            "files": [{
                "path": item.relative_path,
                "sha256": item.md_sha256,
                "sidecars": [
                    {"name": source.name, "sha256": digest}
                    for source, digest in item.sidecars
                ],
            } for item in trajectory_set.items],
        })
        progress.update(1)

        runtime = KernelRuntime(
            active_kernel=kernel_id,
            catalog=catalog,
            skill_dir=Path(xskill_home) / "skill",
            registry_db_path=Path(xskill_home) / "registry.db",
            evaluation_store=KernelEvaluationStore(
                Path(xskill_home) / "kernel_runs.db"
            ),
            xskill_config=xskill_config,
            xskill_config_path=xskill_config_path,
        )
        layout = KernelExecutionLayout(
            skill_dir=isolated_skills,
            registry_db_path=registry_db,
            evaluation_store=run_store,
            workspace=workspace,
            config_path=config_path,
            # Expose the exact directory selected by the user.  The standard
            # TrajectoryReader still consumes the isolated snapshot registered
            # above, while kernels may run their own read/grep/batch tooling
            # directly against this absolute source root.
            trajectory_root=trajectory_set.source,
        )

        def native_not_supported(_invocation) -> KernelRunResult:
            raise RuntimeError(
                "native kernel offline distillation is not supported"
            )

        _, result = runtime.run_active(
            trigger="manual",
            dataset_id=trajectory_set.trajectory_set_id,
            full_rebuild=True,
            native_runner=native_not_supported,
            execution_layout=layout,
            run_id=run_id,
        )
        progress.update(1)

        report = OfflineDistillationReport(
            run_id=run_id,
            status="success",
            kernel_id=descriptor.id,
            kernel_version=descriptor.version,
            trajectory_set_id=trajectory_set.trajectory_set_id,
            trajectories=len(trajectory_set.items),
            processed=len(result.processed_trajectory_ids),
            submitted_skills=tuple(result.submitted_skills),
            duration_s=max(0.0, time.monotonic() - started),
            artifact_dir=artifact_dir,
            metrics=dict(result.metrics),
            notes=result.notes,
        )
        _write_json(result_path, report.as_dict())
        progress.update(1)
        return report
    except BaseException as exc:
        _write_json(result_path, {
            "status": "error",
            "kernel": {
                "id": descriptor.id,
                "version": descriptor.version,
            },
            "trajectories": {
                "total": len(trajectory_set.items),
            },
            "duration_s": round(max(0.0, time.monotonic() - started), 4),
            "error": f"{type(exc).__name__}: {exc}",
        })
        raise
    finally:
        progress.close()


def render_distillation_table(report: OfflineDistillationReport) -> str:
    headers = (
        "KERNEL", "VERSION", "INPUT", "TRAJECTORIES", "PROCESSED",
        "SKILLS", "STATUS", "DURATION", "ARTIFACTS",
    )
    row = (
        report.kernel_id,
        report.kernel_version,
        report.trajectory_set_id,
        str(report.trajectories),
        str(report.processed),
        str(len(report.submitted_skills)),
        report.status,
        f"{report.duration_s:.2f}s",
        str(report.artifact_dir),
    )
    widths = [max(len(headers[index]), len(row[index])) for index in range(len(row))]
    return "\n".join((
        "  ".join(value.ljust(widths[index]) for index, value in enumerate(headers)),
        "  ".join(value.ljust(widths[index]) for index, value in enumerate(row)),
    ))
