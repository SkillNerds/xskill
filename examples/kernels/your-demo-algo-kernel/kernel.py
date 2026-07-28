"""Runnable example implementation for the XSkill algorithm-kernel API.

Copy this directory to ``~/.xskill/kernels/your-demo-algo-kernel``. The implementation is
deliberately deterministic: it processes at most ``max_per_run`` trajectories
per invocation. Replace ``_build_skill_md`` with your own SDK call.

Trajectory / atom feed contract this demo illustrates:

- Prefer ``context.invocation.changed_trajectory_ids`` when present (ready-only
  feed from the host). Offline ``xskill distill`` sets ``full_rebuild=True`` with
  an empty changed list — then iterate all trajectories.
- When ``traj.atoms`` is non-empty, consume atom ``content`` and dedupe by
  ``atom_id`` in workspace. Mother trajectory ``read_text()`` is always available.
- If atoms are empty (mock offline inputs with no TaskAgent split), fall back to
  whole-trajectory markdown so distill still succeeds.
- Do **not** spin-wait on ``atom_split_status == "pending"``.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import yaml

from xskill.kernels import (
    BaseKernel,
    KernelContext,
    KernelMetadata,
    KernelRunResult,
    SkillSubmission,
    TrajectoryResource,
)


class YourDemoAlgoKernel(BaseKernel):
    metadata = KernelMetadata(
        id="your-demo-algo-kernel",
        name="Your Demo Algorithm Kernel",
        version="0.1.0",
        description="Runnable template for your own XSkill algorithm kernel.",
        triggers=("scheduled", "manual"),
        api_version=2,
    )

    _DEFAULT_CONFIG = {
        "max_per_run": 1,
    }

    def run(
        self,
        context: KernelContext,
        run_interval: int = 30,
    ) -> KernelRunResult:
        config = self._load_config(context.config_path)
        cursor_path = context.workspace / "processed.json"
        seen_atoms_path = context.workspace / "seen_atoms.json"
        processed = self._load_processed(cursor_path)
        seen_atoms = self._load_seen_atoms(seen_atoms_path)
        max_per_run = int(config.get("max_per_run", 1))
        if max_per_run < 1:
            raise ValueError("max_per_run must be >= 1")

        pool = self._candidate_trajectories(context)
        selected = [
            trajectory
            for trajectory in pool
            if trajectory.id not in processed
        ][:max_per_run]

        submitted: list[str] = []
        for trajectory in selected:
            name = "demo-kernel-" + hashlib.sha256(
                trajectory.id.encode("utf-8")
            ).hexdigest()[:10]
            context.publisher.submit(SkillSubmission(
                name=name,
                skill_md=self._build_skill_md(name, trajectory, seen_atoms),
                source_trajectory_ids=(trajectory.id,),
                message=f"demo distillation from {trajectory.id}",
            ))
            submitted.append(name)
            processed.add(trajectory.id)
            # Persist after each successful publication. If a later draft in
            # the same run fails, already-published inputs remain idempotent.
            self._save_processed(cursor_path, processed)
            self._save_seen_atoms(seen_atoms_path, seen_atoms)

        return KernelRunResult(
            processed_trajectory_ids=tuple(item.id for item in selected),
            submitted_skills=tuple(submitted),
            metrics={
                "selected": len(selected),
                "published": len(submitted),
                "dataset_id": context.invocation.dataset_id,
                "seen_atoms": len(seen_atoms),
            },
            notes=(
                "Demo kernel prefers changed_trajectory_ids when present; "
                "falls back to mother trajectory markdown when atoms are empty."
            ),
        )

    @staticmethod
    def _candidate_trajectories(context: KernelContext) -> list[TrajectoryResource]:
        """Prefer the ready-only changed feed; else walk all trajectories.

        Offline distill passes ``full_rebuild=True`` with an empty changed list.
        The host never puts pending trajectories in ``changed_trajectory_ids``.
        """
        changed = tuple(context.invocation.changed_trajectory_ids)
        if changed:
            by_id = {item.id: item for item in context.trajectories.list()}
            return [by_id[item_id] for item_id in changed if item_id in by_id]
        return list(context.trajectories.list())

    @staticmethod
    def _excerpt_for_skill(
        trajectory: TrajectoryResource,
        seen_atoms: set[str],
    ) -> str:
        """Prefer newly seen atom contents; fall back to mother markdown."""
        new_atoms = [
            atom for atom in trajectory.atoms
            if atom.atom_id not in seen_atoms
        ]
        if new_atoms:
            parts: list[str] = []
            for atom in new_atoms:
                seen_atoms.add(atom.atom_id)
                body = (atom.content or "").strip()
                if body:
                    parts.append(body)
            if parts:
                return "\n\n".join(parts)
        # Empty atoms (mock offline / no split): mother trajectory always readable.
        return trajectory.read_text()

    @classmethod
    def _build_skill_md(
        cls,
        name: str,
        trajectory: TrajectoryResource,
        seen_atoms: set[str],
    ) -> str:
        """Replace this method with a call to your own algorithm package."""
        excerpt = cls._excerpt_for_skill(trajectory, seen_atoms).strip()[:1200]
        quoted_excerpt = "\n".join(
            f"> {line}" if line else ">" for line in excerpt.splitlines()
        )
        return (
            "---\n"
            f"name: {name}\n"
            "description: Skill generated by the demo algorithm kernel.\n"
            "metadata: {}\n"
            "---\n\n"
            f"# {name}\n\n"
            "Replace this deterministic generator with your algorithm SDK.\n\n"
            "## Source excerpt\n\n"
            f"{quoted_excerpt}\n"
        )

    @classmethod
    def _load_config(cls, path: Path) -> dict:
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_text(
                yaml.safe_dump(cls._DEFAULT_CONFIG, sort_keys=False),
                encoding="utf-8",
            )
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(loaded, dict):
            raise ValueError("demo kernel config.yaml must be a mapping")
        return loaded

    @staticmethod
    def _load_processed(path: Path) -> set[str]:
        if not path.exists():
            return set()
        loaded = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(loaded, list):
            raise ValueError("processed.json must contain a JSON list")
        return {str(item) for item in loaded}

    @staticmethod
    def _save_processed(path: Path, processed: set[str]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(sorted(processed), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(path)

    @staticmethod
    def _load_seen_atoms(path: Path) -> set[str]:
        if not path.exists():
            return set()
        loaded = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(loaded, list):
            raise ValueError("seen_atoms.json must contain a JSON list")
        return {str(item) for item in loaded}

    @staticmethod
    def _save_seen_atoms(path: Path, seen: set[str]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(sorted(seen), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(path)


KERNEL_CLASS = YourDemoAlgoKernel
