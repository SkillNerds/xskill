"""BDD scenarios for kernel trajectory feed and create_temp (no live LLM).

Feature: Platform feeds ready trajectories with atom views to algorithm kernels
  As an algorithm kernel developer
  I want create_temp + ready-only feeding with stable atom_ids
  So that I can consume sub-trajectories without waiting on pending splits
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from xskill._workers import _trajectory_snapshot
from xskill.kernels.context import TrajectoryReader
from xskill.kernels.distillation import run_offline_distillation
from xskill.pipeline.atom import AtomTask, AtomTaskStore
from xskill.pipeline.registry import (
    discover_trajectories,
    register_dir,
    update_traj_status,
)


PLATFORM_MD = """## User

Please summarize the patent claim.

## Assistant

I will read the claim text and summarize it.
"""


def _write_atom(
    store: AtomTaskStore,
    *,
    traj_id: str,
    atom_id: str,
    ux_score,
    raw_segment: str,
    used_skills=(),
    offset_start: int = 1,
    offset_end: int = 10,
) -> None:
    store.save(AtomTask(
        atom_id=atom_id,
        traj_id=traj_id,
        offset_start=offset_start,
        offset_end=offset_end,
        intent="intent",
        summary="summary",
        used_skills=list(used_skills),
        ux_score=ux_score,
        raw_segment=raw_segment,
    ))


def _feed_changed(
    previous: dict[str, tuple[int, int, tuple[str, ...]]],
    reader: TrajectoryReader,
) -> tuple[str, ...]:
    current = _trajectory_snapshot(reader)
    changed = tuple(sorted(
        resource_id
        for resource_id, fingerprint in current.items()
        if previous.get(resource_id) != fingerprint
    ))
    return changed


def _simulate_split_ready(
    *,
    registry_db: Path,
    watch_dir_id: int,
    traj_path: Path,
    atoms: list[dict],
) -> None:
    """Stand in for TaskAgent: mark split_done and materialize atom JSON."""
    update_traj_status(
        watch_dir_id, traj_path.name, status="split_done", db_path=registry_db,
    )
    store = AtomTaskStore(root=traj_path.parent)
    for index, atom in enumerate(atoms, start=1):
        _write_atom(
            store,
            traj_id=traj_path.stem,
            atom_id=atom["atom_id"],
            ux_score=atom.get("ux_score"),
            raw_segment=atom["content"],
            used_skills=atom.get("used_skills", ()),
            offset_start=index,
            offset_end=index + 1,
        )


class TestFeatureCreateTempAndReadyFeed:
    """Scenario outline for the main success path."""

    def test_scenario_create_temp_pending_then_ready_feed_exposes_atoms(
        self, tmp_path,
    ):
        """
        Scenario: create_temp succeeds, stays out of feed until ready, then feeds atoms
          Given platform-shaped markdown
          When the kernel creates a temp trajectory
          Then it is pending with empty atoms and absent from the feed
          When the platform finishes splitting
          Then it enters the feed as ready with readable atom content
        """
        # Given
        registry_db = tmp_path / "registry.db"
        temp_root = tmp_path / "temp_trajectories"
        reader = TrajectoryReader(registry_db, temp_root=temp_root)
        previous: dict[str, tuple[int, int, tuple[str, ...]]] = {}

        # When: create_temp
        created = reader.create_temp(
            PLATFORM_MD,
            trajectory_id="traj_temp_bdd_001",
        )

        # Then: pending, not fed
        assert created.source == "temp"
        assert created.atom_split_status == "pending"
        assert created.atoms == ()
        assert created.read_text() == PLATFORM_MD
        assert _feed_changed(previous, reader) == ()
        assert created.id not in _trajectory_snapshot(reader)

        # When: split completes (simulated; no live LLM)
        watch_dirs = reader.directories()
        temp_watch = next(item for item in watch_dirs if item.ecosystem == "kernel-temp")
        _simulate_split_ready(
            registry_db=registry_db,
            watch_dir_id=int(temp_watch.id),
            traj_path=created.path,
            atoms=[{
                "atom_id": "atom_traj_temp_bdd_001_0001",
                "ux_score": 8,
                "content": "Please summarize the patent claim.",
                "used_skills": ("patent-summary",),
            }],
        )

        # Then: ready feed with atoms/content
        changed = _feed_changed(previous, reader)
        assert len(changed) == 1
        fed = reader.get(changed[0])
        assert fed.atom_split_status == "ready"
        assert fed.source == "temp"
        assert len(fed.atoms) == 1
        atom = fed.atoms[0]
        assert atom.atom_id == "atom_traj_temp_bdd_001_0001"
        assert atom.content == "Please summarize the patent claim."
        assert atom.ux_score == 8
        assert atom.used_skills == ("patent-summary",)

    def test_scenario_invalid_markdown_is_rejected(self, tmp_path):
        """
        Scenario: create_temp rejects non-platform markdown
          Given OpenEarth-style evidence markdown without ## User
          When create_temp is called
          Then validation fails with a platform format hint
        """
        reader = TrajectoryReader(
            tmp_path / "registry.db",
            temp_root=tmp_path / "temp_trajectories",
        )
        bad = (
            "# OpenEarth dataset trajectory\n\n"
            "## Task context\n\nquestion\n\n"
            "## Target-agent transcript\n\nflattened text\n"
        )
        with pytest.raises(ValueError, match="## User"):
            reader.create_temp(bad, trajectory_id="traj_temp_bad")

    def test_scenario_pending_user_trajectory_never_enters_feed(self, tmp_path):
        """
        Scenario: pending user trajectories are not fed
          Given a discovered user trajectory that is still pending
          When the host builds the feed snapshot
          Then the trajectory id is absent from the feed
        """
        registry_db = tmp_path / "registry.db"
        watch = tmp_path / "watch"
        watch.mkdir()
        traj = watch / "traj_user_pending.md"
        traj.write_text(PLATFORM_MD, encoding="utf-8")
        watch_dir_id = register_dir(watch, label="w", db_path=registry_db)
        discover_trajectories(watch_dir_id, watch, db_path=registry_db)
        reader = TrajectoryReader(registry_db)
        resources = reader.list()
        assert len(resources) == 1
        assert resources[0].atom_split_status == "pending"
        assert resources[0].source == "user"
        assert _trajectory_snapshot(reader) == {}

    def test_scenario_incremental_ready_refeeds_full_atoms_kernel_dedups(
        self, tmp_path,
    ):
        """
        Scenario: after incremental split, traj re-enters feed; kernel dedups by atom_id
          Given a ready trajectory already consumed by the kernel
          When more atoms are split and it becomes ready again
          Then the feed changes again with the full atom list
          And the kernel only processes atom_ids it has not seen
        """
        registry_db = tmp_path / "registry.db"
        watch = tmp_path / "watch"
        watch.mkdir()
        traj = watch / "traj_user_incremental.md"
        traj.write_text(PLATFORM_MD, encoding="utf-8")
        watch_dir_id = register_dir(watch, label="w", db_path=registry_db)
        discover_trajectories(watch_dir_id, watch, db_path=registry_db)
        _simulate_split_ready(
            registry_db=registry_db,
            watch_dir_id=watch_dir_id,
            traj_path=traj,
            atoms=[{
                "atom_id": "atom_traj_user_incremental_0001",
                "ux_score": 7,
                "content": "first segment",
            }],
        )
        reader = TrajectoryReader(registry_db)
        previous: dict[str, tuple[int, int, tuple[str, ...]]] = {}
        first_changed = _feed_changed(previous, reader)
        assert len(first_changed) == 1
        first = reader.get(first_changed[0])
        seen = {atom.atom_id for atom in first.atoms}
        previous = _trajectory_snapshot(reader)

        # When: incremental atoms appear (same ready status, longer atom list)
        store = AtomTaskStore(root=watch)
        _write_atom(
            store,
            traj_id="traj_user_incremental",
            atom_id="atom_traj_user_incremental_0002",
            ux_score=4,
            raw_segment="second segment",
            offset_start=2,
            offset_end=3,
        )
        # Touch mtime so fingerprint updates even if only atoms changed
        traj.write_text(PLATFORM_MD + "\n", encoding="utf-8")
        update_traj_status(
            watch_dir_id, traj.name, status="split_done", db_path=registry_db,
        )

        second_changed = _feed_changed(previous, reader)
        assert second_changed == first_changed
        fed = reader.get(second_changed[0])
        assert fed.atom_split_status == "ready"
        assert {a.atom_id for a in fed.atoms} == {
            "atom_traj_user_incremental_0001",
            "atom_traj_user_incremental_0002",
        }
        newly = [a for a in fed.atoms if a.atom_id not in seen]
        assert len(newly) == 1
        assert newly[0].atom_id == "atom_traj_user_incremental_0002"
        assert newly[0].content == "second segment"

    def test_scenario_demo_kernel_distill_still_succeeds_on_mock_trajectories(
        self, tmp_path,
    ):
        """
        Scenario: existing demo kernel remains compatible (no atom dependency)
          Given mock runtime trajectories and your-demo-algo-kernel
          When offline distill runs
          Then it completes successfully and writes skills
        """
        repo = Path(__file__).resolve().parents[1]
        plugin_dir = tmp_path / "kernels"
        plugin_dir.mkdir()
        demo_src = repo / "examples" / "kernels" / "your-demo-algo-kernel"
        demo_dst = plugin_dir / "your-demo-algo-kernel"
        demo_dst.mkdir()
        (demo_dst / "kernel.py").write_text(
            (demo_src / "kernel.py").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        traj_dir = repo / "examples" / "kernels" / "mock-runtime-trajectories"
        output = tmp_path / "out"
        report = run_offline_distillation(
            kernel_id="your-demo-algo-kernel",
            trajectory_dir=traj_dir,
            plugin_dir=plugin_dir,
            xskill_home=tmp_path / "home",
            output_dir=output,
            no_progress=True,
        )
        assert report.status == "success"
        assert report.submitted_skills
        assert (output / "skills").is_dir()
        result = json.loads((output / "result.json").read_text(encoding="utf-8"))
        assert result["status"] == "success"
