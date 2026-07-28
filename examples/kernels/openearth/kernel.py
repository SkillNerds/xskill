"""Illustrative local bridge for an SDK package named ``openearth``.

Copy this directory to ``~/.xskill/kernels/openearth`` and replace the marked
adapter call with the real SDK API.  XSkill imports this bridge as trusted code.

Trajectory / atom feed notes for OpenEarth adapters:

- Prefer ``context.invocation.changed_trajectory_ids`` (ready-only). Read
  ``traj.atoms`` and dedupe by ``atom_id`` in ``context.workspace``.
- Mother trajectory ``read_text()`` is always available; there is no
  trajectory-level ``ux_score`` (scores live on atoms / Skill versions).
- For algorithm-owned rollouts, convert OE harness markdown to **platform**
  style (``## User`` / ``## Assistant`` / …) on the OE side, then call
  ``context.trajectories.create_temp(markdown, trajectory_id=...)``.
  ``trajectory_id`` must match ``traj_[a-z0-9]...``. The returned resource is
  ``source="temp"`` / ``atom_split_status="pending"``; do **not** spin-wait —
  atoms arrive later via the ready feed.
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
    KernelMetadata,
    KernelRunResult,
    SkillSubmission,
)


class OpenEarthKernel(BaseKernel):
    metadata = KernelMetadata(
        id="openearth",
        name="OpenEarth",
        version=getattr(openearth, "__version__", "unknown"),
        description="OpenEarth SDK bridge example.",
        triggers=("scheduled", "manual"),
        api_version=2,
    )

    def run(self, context, run_interval: int = 30) -> KernelRunResult:
        # Prefer the ready-only changed feed when the host provides it.
        # Offline distill / empty changed → walk all mother trajectories.
        changed = tuple(context.invocation.changed_trajectory_ids)
        if changed:
            by_id = {item.id: item for item in context.trajectories.list()}
            trajectories = [by_id[item_id] for item_id in changed if item_id in by_id]
        else:
            trajectories = context.trajectories.list()

        # Illustrative create_temp path (adapter must convert markdown first):
        #
        #   platform_md = openearth.to_platform_markdown(oe_rollout_md)
        #   temp = context.trajectories.create_temp(
        #       platform_md,
        #       trajectory_id="traj_oe_rollout_001",
        #   )
        #   # temp.source == "temp"; temp.atom_split_status == "pending"
        #   # Do not poll — next ready feed will include atoms when split done.
        #
        # When consuming a ready trajectory:
        #   for atom in traj.atoms:  # atom.atom_id / content / ux_score / used_skills
        #       ...

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
