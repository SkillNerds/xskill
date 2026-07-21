"""Illustrative local bridge for an SDK package named ``openearth``.

Copy this directory to ``~/.xskill/kernels/openearth`` and replace the marked
adapter call with the real SDK API.  XSkill imports this bridge as trusted code.
"""

from __future__ import annotations

try:
    import openearth
except ImportError as exc:
    raise ImportError(
        "OpenEarth kernel requires its SDK package; install it in the same "
        "Python environment as xskill"
    ) from exc

from xskill.kernels import (
    BaseKernel,
    KernelManifest,
    KernelRunResult,
    SkillSubmission,
)


class OpenEarthKernel(BaseKernel):
    manifest = KernelManifest(
        id="openearth",
        name="OpenEarth",
        version=getattr(openearth, "__version__", "unknown"),
        description="OpenEarth SDK bridge example.",
        triggers=("scheduled", "manual", "evaluation"),
        api_version=2,
    )

    def run(self, context) -> KernelRunResult:
        trajectories = context.trajectories.list()

        # Adapt this single call to the real SDK.  The SDK owns and parses its
        # config; XSkill only passes the opaque path and standard inputs.
        drafts = openearth.xskill_distill(  # type: ignore[attr-defined]
            config_path=context.config_path,
            workspace=context.workspace,
            trajectories=trajectories,
            dataset_id=context.invocation.dataset_id,
        )

        submitted = []
        consumed = []
        for draft in drafts:
            base_commit = None
            try:
                base_commit = context.skills.get(draft.name).main_commit_sha
            except KeyError:
                pass
            context.publisher.submit(SkillSubmission(
                name=draft.name,
                skill_md=draft.skill_md,
                files=draft.files,
                source_trajectory_ids=tuple(draft.trajectory_ids),
                message=f"OpenEarth run {context.run_id}",
                base_commit_sha=base_commit,
            ))
            submitted.append(draft.name)
            consumed.extend(draft.trajectory_ids)
        return KernelRunResult(
            processed_trajectory_ids=tuple(dict.fromkeys(consumed)),
            submitted_skills=tuple(submitted),
            metrics={"sdk_outputs": len(submitted)},
        )


KERNEL_CLASS = OpenEarthKernel
