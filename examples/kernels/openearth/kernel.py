"""XSkill bridge for the private OpenEarth Skill SDK wheel.

The SDK receives only ready trajectory resources.  It expands their Atom
views, using XSkill ``atom.ux_score`` for user trajectories and the OpenEarth
oracle score store for kernel-owned temporary benchmark trajectories.
"""

from __future__ import annotations

import json
from pathlib import Path

try:
    from openearth_skill_sdk import (
        ExistingSkillInput,
        SkillDraft,
        __version__,
        rebase_skill_draft,
        record_oracle_score,
        run_benchmark,
        train_skills,
    )
except ImportError as exc:
    raise ImportError(
        "OpenEarth kernel requires openearth-skill-sdk; install the supplied "
        "wheel in the same Python environment as xskill"
    ) from exc

from xskill.kernels import (
    BaseKernel,
    KernelMetadata,
    KernelRunResult,
    SkillSubmission,
)


_QUEUE_SCHEMA = 1
_QUEUE_FILENAME = "openearth-publication-queue.json"


def _skill_snapshot(skill) -> ExistingSkillInput:
    files = {
        relative_path: skill.read_text(relative_path)
        for relative_path in skill.list_files()
        if relative_path != "SKILL.md"
    }
    return ExistingSkillInput(
        name=skill.name,
        skill_md=skill.read_text(),
        files=files,
        version_token=skill.main_commit_sha,
    )


def _existing_skills(context) -> tuple[ExistingSkillInput, ...]:
    return tuple(_skill_snapshot(skill) for skill in context.skills.list())


def _queue_path(context) -> Path:
    return Path(context.workspace) / _QUEUE_FILENAME


def _load_queue(context) -> dict:
    path = _queue_path(context)
    if not path.is_file():
        return {"schema": _QUEUE_SCHEMA, "pending": {}}
    value = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(value, dict)
        or value.get("schema") != _QUEUE_SCHEMA
        or not isinstance(value.get("pending"), dict)
    ):
        raise ValueError(f"invalid OpenEarth publication queue: {path}")
    return value


def _save_queue(context, queue: dict) -> None:
    path = _queue_path(context)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(queue, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _draft_to_queue_entry(draft, *, run_id: str) -> dict:
    return {
        "name": draft.name,
        "skill_md": draft.skill_md,
        "files": dict(draft.files),
        "source_trajectory_ids": list(draft.source_trajectory_ids),
        "action": draft.action,
        "base_version_token": draft.base_version_token,
        "generated_run_id": run_id,
    }


def _draft_from_queue_entry(name: str, entry: dict) -> SkillDraft:
    if not isinstance(entry, dict) or entry.get("name") != name:
        raise ValueError(f"invalid queued OpenEarth Skill draft: {name!r}")
    files = entry.get("files", {})
    source_ids = entry.get("source_trajectory_ids", [])
    if not isinstance(files, dict) or not isinstance(source_ids, list):
        raise ValueError(f"invalid queued OpenEarth Skill bundle: {name!r}")
    return SkillDraft(
        name=name,
        skill_md=str(entry.get("skill_md") or ""),
        files=files,
        source_trajectory_ids=tuple(str(item) for item in source_ids),
        action=str(entry.get("action") or "update"),
        base_version_token=entry.get("base_version_token"),
    )


def _current_skill(context, name: str):
    try:
        return context.skills.get(name)
    except KeyError:
        return None


def _prepare_for_current_main(draft, current):
    if current is None:
        return draft, False
    if draft.base_version_token == current.main_commit_sha:
        return draft, False
    return rebase_skill_draft(draft, _skill_snapshot(current)), True


def _submit(context, draft, current, *, origin_run_id: str):
    prepared, rebased = _prepare_for_current_main(draft, current)
    published = context.publisher.submit(SkillSubmission(
        name=prepared.name,
        skill_md=prepared.skill_md,
        files=prepared.files,
        source_trajectory_ids=prepared.source_trajectory_ids,
        message=f"OpenEarth run {origin_run_id}",
        base_commit_sha=(
            current.main_commit_sha if current is not None else None
        ),
    ))
    return published, rebased


def _submit_or_observe_busy(context, draft, current, *, origin_run_id: str):
    """Submit once, refreshing main if canary changed during the attempt."""
    for attempt in range(2):
        if current is not None and current.staging_commit_sha:
            return None, False, True
        current_token = (
            current.main_commit_sha if current is not None else None
        )
        try:
            published, rebased = _submit(
                context,
                draft,
                current,
                origin_run_id=origin_run_id,
            )
            return published, rebased, False
        except RuntimeError:
            refreshed = _current_skill(context, draft.name)
            if refreshed is not None and refreshed.staging_commit_sha:
                return None, False, True
            refreshed_token = (
                refreshed.main_commit_sha if refreshed is not None else None
            )
            if attempt == 0 and refreshed_token != current_token:
                current = refreshed
                continue
            raise
    raise AssertionError("unreachable publication retry state")


def _drain_publication_queue(context, queue: dict):
    submitted = []
    metrics = {
        "queue_pending_before": len(queue["pending"]),
        "queue_waiting": 0,
        "queue_missing_skill": 0,
        "queue_drained": 0,
        "queue_rebased": 0,
    }
    for name in sorted(tuple(queue["pending"])):
        entry = queue["pending"][name]
        draft = _draft_from_queue_entry(name, entry)
        current = _current_skill(context, name)
        if current is None and draft.base_version_token is not None:
            metrics["queue_missing_skill"] += 1
            continue
        _published, rebased, busy = _submit_or_observe_busy(
            context,
            draft,
            current,
            origin_run_id=str(entry.get("generated_run_id") or context.run_id),
        )
        if busy:
            metrics["queue_waiting"] += 1
            continue
        if rebased:
            metrics["queue_rebased"] += 1
        submitted.append(name)
        metrics["queue_drained"] += 1
        del queue["pending"][name]
        _save_queue(context, queue)
    return submitted, metrics


def _enqueue(context, queue: dict, draft) -> bool:
    superseded = draft.name in queue["pending"]
    queue["pending"][draft.name] = _draft_to_queue_entry(
        draft,
        run_id=context.run_id,
    )
    _save_queue(context, queue)
    return superseded


class OpenEarthKernel(BaseKernel):
    metadata = KernelMetadata(
        id="openearth",
        name="OpenEarth",
        version=__version__,
        description="OpenEarth Atom-based Skill training bridge.",
        triggers=("scheduled", "manual"),
        api_version=2,
    )

    def run(self, context, run_interval: int = 30) -> KernelRunResult:
        del run_interval
        queue = _load_queue(context)
        submitted, queue_metrics = _drain_publication_queue(context, queue)
        changed = tuple(context.invocation.changed_trajectory_ids)
        full_rebuild = bool(context.invocation.full_rebuild)
        existing_snapshots = None
        benchmark_metrics = {
            "benchmark_enabled": False,
            "benchmark_selected": 0,
            "benchmark_created": 0,
            "benchmark_skipped": 0,
        }
        if full_rebuild:
            existing_snapshots = _existing_skills(context)

            def register_benchmark_trajectory(rollout):
                record_oracle_score(
                    workspace=context.workspace,
                    trajectory_id=rollout.trajectory_id,
                    ux_score=rollout.ux_score,
                    case_id=rollout.case_id,
                    metadata=rollout.metadata,
                )
                context.trajectories.create_temp(
                    markdown=rollout.markdown,
                    trajectory_id=rollout.trajectory_id,
                )

            benchmark_result = run_benchmark(
                config_path=context.config_path,
                workspace=context.workspace,
                existing_skills=existing_snapshots,
                run_id=context.run_id,
                on_trajectory=register_benchmark_trajectory,
            )
            benchmark_metrics = dict(benchmark_result.metrics)
        if changed:
            changed_ids = set(changed)
            selected = [
                trajectory
                for trajectory in context.trajectories.list()
                if trajectory.id in changed_ids
                and trajectory.atom_split_status == "ready"
            ]
        elif full_rebuild:
            selected = [
                trajectory
                for trajectory in context.trajectories.list()
                if trajectory.atom_split_status == "ready"
            ]
        else:
            return KernelRunResult(
                submitted_skills=tuple(submitted),
                metrics={
                    "selected_trajectories": 0,
                    "full_rebuild": False,
                    "no_changes": True,
                    "generated_drafts": 0,
                    "published_drafts": len(submitted),
                    "queued_drafts": 0,
                    "queue_superseded": 0,
                    "queue_pending": len(queue["pending"]),
                    **queue_metrics,
                    **benchmark_metrics,
                },
                notes=(
                    "No trajectory training input; OpenEarth only reconciled "
                    "its publication queue and did not invoke the SDK."
                ),
            )

        result = train_skills(
            config_path=context.config_path,
            workspace=context.workspace,
            trajectories=selected,
            existing_skills=(
                existing_snapshots
                if existing_snapshots is not None
                else _existing_skills(context)
            ),
            run_id=context.run_id,
            full_rebuild=full_rebuild,
        )

        queued = []
        superseded = []
        rebased_immediate = 0
        for draft in result.drafts:
            current = _current_skill(context, draft.name)
            _published, rebased, busy = _submit_or_observe_busy(
                context,
                draft,
                current,
                origin_run_id=context.run_id,
            )
            if busy:
                if _enqueue(context, queue, draft):
                    superseded.append(draft.name)
                queued.append(draft.name)
                continue
            if rebased:
                rebased_immediate += 1
            submitted.append(draft.name)

        metrics = {
            **dict(result.metrics),
            "selected_trajectories": len(selected),
            "full_rebuild": full_rebuild,
            "no_changes": False,
            "processed_atoms": len(result.processed_atom_ids),
            "generated_drafts": len(result.drafts),
            "published_drafts": len(submitted),
            "queued_drafts": len(queued),
            "queue_superseded": len(superseded),
            "queue_pending": len(queue["pending"]),
            "queue_rebased_immediate": rebased_immediate,
            **queue_metrics,
            **benchmark_metrics,
        }
        if result.candidate_dir:
            metrics["candidate_dir"] = result.candidate_dir
        return KernelRunResult(
            processed_trajectory_ids=result.processed_trajectory_ids,
            submitted_skills=tuple(submitted),
            metrics=metrics,
            notes=(
                "OpenEarth trained from ready trajectory Atom views; "
                "XSkill owns publication and active staging drafts are queued."
            ),
        )


KERNEL_CLASS = OpenEarthKernel
