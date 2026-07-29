"""XSkill bridge for the private OpenEarth Skill SDK wheel.

The SDK receives only ready trajectory resources.  It expands their Atom
views, using XSkill ``atom.ux_score`` for user trajectories and the OpenEarth
oracle score store for kernel-owned temporary benchmark trajectories.
"""

from __future__ import annotations

try:
    from openearth_skill_sdk import (
        ExistingSkillInput,
        __version__,
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


def _existing_skills(context) -> tuple[ExistingSkillInput, ...]:
    snapshots = []
    for skill in context.skills.list():
        files = {
            relative_path: skill.read_text(relative_path)
            for relative_path in skill.list_files()
            if relative_path != "SKILL.md"
        }
        snapshots.append(ExistingSkillInput(
            name=skill.name,
            skill_md=skill.read_text(),
            files=files,
            version_token=skill.main_commit_sha,
        ))
    return tuple(snapshots)


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
        changed = tuple(context.invocation.changed_trajectory_ids)
        full_rebuild = bool(context.invocation.full_rebuild)
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
                metrics={
                    "selected_trajectories": 0,
                    "full_rebuild": False,
                    "no_changes": True,
                },
                notes=(
                    "No changed trajectories and full_rebuild is false; "
                    "OpenEarth did not invoke the SDK."
                ),
            )

        result = train_skills(
            config_path=context.config_path,
            workspace=context.workspace,
            trajectories=selected,
            existing_skills=_existing_skills(context),
            run_id=context.run_id,
            full_rebuild=full_rebuild,
        )

        submitted = []
        skipped_active_staging = []
        for draft in result.drafts:
            base_commit = None
            try:
                current = context.skills.get(draft.name)
            except KeyError:
                current = None
            if current is not None:
                if current.staging_commit_sha:
                    skipped_active_staging.append(draft.name)
                    continue
                base_commit = current.main_commit_sha
            context.publisher.submit(SkillSubmission(
                name=draft.name,
                skill_md=draft.skill_md,
                files=draft.files,
                source_trajectory_ids=draft.source_trajectory_ids,
                message=f"OpenEarth run {context.run_id}",
                base_commit_sha=base_commit,
            ))
            submitted.append(draft.name)

        metrics = {
            **dict(result.metrics),
            "selected_trajectories": len(selected),
            "full_rebuild": full_rebuild,
            "no_changes": False,
            "processed_atoms": len(result.processed_atom_ids),
            "generated_drafts": len(result.drafts),
            "published_drafts": len(submitted),
            "skipped_active_staging": len(skipped_active_staging),
        }
        if result.candidate_dir:
            metrics["candidate_dir"] = result.candidate_dir
        return KernelRunResult(
            processed_trajectory_ids=result.processed_trajectory_ids,
            submitted_skills=tuple(submitted),
            metrics=metrics,
            notes=(
                "OpenEarth trained from ready trajectory Atom views; "
                "XSkill owns publication."
            ),
        )


KERNEL_CLASS = OpenEarthKernel
