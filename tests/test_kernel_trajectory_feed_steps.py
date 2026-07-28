"""pytest-bdd step definitions for
``tests/features/kernel_trajectory_feed.feature`` (no live LLM).

The platform's TaskAgent split is stood in for by ``_simulate_split_ready``,
which marks a trajectory ``split_done`` and materializes its atom JSON
directly, matching the current helpers used before this file existed as
plain-pytest BDD scenarios.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pytest_bdd import given, scenarios, then, when

from xskill._workers import _trajectory_snapshot
from xskill.kernels.context import TrajectoryReader
from xskill.kernels.distillation import run_offline_distillation
from xskill.pipeline.atom import AtomTask, AtomTaskStore
from xskill.pipeline.registry import (
    discover_trajectories,
    register_dir,
    update_traj_status,
)

scenarios("features/kernel_trajectory_feed.feature")


PLATFORM_MD = """## User

Please summarize the patent claim.

## Assistant

I will read the claim text and summarize it.
"""

NON_PLATFORM_MD = (
    "# OpenEarth dataset trajectory\n\n"
    "## Task context\n\nquestion\n\n"
    "## Target-agent transcript\n\nflattened text\n"
)


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
    return tuple(sorted(
        resource_id
        for resource_id, fingerprint in current.items()
        if previous.get(resource_id) != fingerprint
    ))


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


@pytest.fixture
def context() -> dict:
    """Mutable bag shared across Given/When/Then steps within one scenario."""
    return {}


# ── Scenario: create_temp pending -> ready feed with atoms ─────────────


@given("a trajectory reader with a temp root")
def given_reader_with_temp_root(tmp_path, context):
    registry_db = tmp_path / "registry.db"
    temp_root = tmp_path / "temp_trajectories"
    context["registry_db"] = registry_db
    context["reader"] = TrajectoryReader(registry_db, temp_root=temp_root)
    context["previous"] = {}


@when("the kernel creates a temp trajectory from platform-shaped markdown")
def when_create_temp_platform_markdown(context):
    reader = context["reader"]
    context["created"] = reader.create_temp(
        PLATFORM_MD, trajectory_id="traj_temp_bdd_001",
    )


@then("the temp trajectory is pending with no atoms")
def then_temp_trajectory_pending_no_atoms(context):
    created = context["created"]
    assert created.source == "temp"
    assert created.atom_split_status == "pending"
    assert created.atoms == ()
    assert created.read_text() == PLATFORM_MD


@then("the temp trajectory is absent from the feed")
def then_temp_trajectory_absent_from_feed(context):
    reader = context["reader"]
    created = context["created"]
    assert _feed_changed(context["previous"], reader) == ()
    assert created.id not in _trajectory_snapshot(reader)


@when("the platform finishes splitting the temp trajectory into one atom")
def when_platform_splits_temp_trajectory(context):
    reader = context["reader"]
    created = context["created"]
    watch_dirs = reader.directories()
    temp_watch = next(item for item in watch_dirs if item.ecosystem == "kernel-temp")
    _simulate_split_ready(
        registry_db=context["registry_db"],
        watch_dir_id=int(temp_watch.id),
        traj_path=created.path,
        atoms=[{
            "atom_id": "atom_traj_temp_bdd_001_0001",
            "ux_score": 8,
            "content": "Please summarize the patent claim.",
            "used_skills": ("patent-summary",),
        }],
    )


@then("the temp trajectory enters the feed as ready")
def then_temp_trajectory_enters_feed_ready(context):
    reader = context["reader"]
    changed = _feed_changed(context["previous"], reader)
    assert len(changed) == 1
    fed = reader.get(changed[0])
    assert fed.atom_split_status == "ready"
    assert fed.source == "temp"
    context["fed"] = fed


@then("the fed atom exposes its content, ux_score and used_skills")
def then_fed_atom_exposes_content_ux_used_skills(context):
    fed = context["fed"]
    assert len(fed.atoms) == 1
    atom = fed.atoms[0]
    assert atom.atom_id == "atom_traj_temp_bdd_001_0001"
    assert atom.content == "Please summarize the patent claim."
    assert atom.ux_score == 8
    assert atom.used_skills == ("patent-summary",)


# ── Scenario: create_temp rejects non-platform markdown ─────────────────


@when("the kernel creates a temp trajectory from evidence markdown without a User section")
def when_create_temp_non_platform_markdown(context):
    reader = context["reader"]
    try:
        reader.create_temp(NON_PLATFORM_MD, trajectory_id="traj_temp_bad")
    except ValueError as exc:
        context["error"] = exc
    else:
        context["error"] = None


@then("create_temp raises a validation error mentioning the platform format")
def then_create_temp_raises_platform_format_error(context):
    error = context["error"]
    assert error is not None
    assert "## User" in str(error)


# ── Scenario: pending user trajectories never enter the feed ────────────


@given("a discovered user trajectory that is still pending")
def given_discovered_pending_user_trajectory(tmp_path, context):
    registry_db = tmp_path / "registry.db"
    watch = tmp_path / "watch"
    watch.mkdir()
    traj = watch / "traj_user_pending.md"
    traj.write_text(PLATFORM_MD, encoding="utf-8")
    watch_dir_id = register_dir(watch, label="w", db_path=registry_db)
    discover_trajectories(watch_dir_id, watch, db_path=registry_db)
    context["registry_db"] = registry_db
    context["reader"] = TrajectoryReader(registry_db)


@when("the host builds the feed snapshot")
def when_host_builds_feed_snapshot(context):
    reader = context["reader"]
    context["resources"] = reader.list()
    context["snapshot"] = _trajectory_snapshot(reader)


@then("the pending trajectory is absent from the feed")
def then_pending_trajectory_absent_from_feed(context):
    resources = context["resources"]
    assert len(resources) == 1
    assert resources[0].atom_split_status == "pending"
    assert resources[0].source == "user"
    assert context["snapshot"] == {}


# ── Scenario: incremental ready re-feeds full atoms; kernel dedups ──────


@given("a ready user trajectory with one atom already consumed by the kernel")
def given_ready_user_trajectory_one_atom_consumed(tmp_path, context):
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
    first_changed = _feed_changed({}, reader)
    assert len(first_changed) == 1
    first = reader.get(first_changed[0])
    context.update({
        "registry_db": registry_db,
        "watch": watch,
        "watch_dir_id": watch_dir_id,
        "traj": traj,
        "reader": reader,
        "seen": {atom.atom_id for atom in first.atoms},
        "first_changed": first_changed,
        "previous": _trajectory_snapshot(reader),
    })


@when("the platform splits one more atom for the trajectory")
def when_platform_splits_one_more_atom(context):
    store = AtomTaskStore(root=context["watch"])
    _write_atom(
        store,
        traj_id="traj_user_incremental",
        atom_id="atom_traj_user_incremental_0002",
        ux_score=4,
        raw_segment="second segment",
        offset_start=2,
        offset_end=3,
    )
    traj = context["traj"]
    # Touch mtime so fingerprint updates even if only atoms changed.
    traj.write_text(PLATFORM_MD + "\n", encoding="utf-8")
    update_traj_status(
        context["watch_dir_id"], traj.name, status="split_done", db_path=context["registry_db"],
    )


@then("the trajectory re-enters the feed as ready")
def then_trajectory_reenters_feed_ready(context):
    reader = context["reader"]
    second_changed = _feed_changed(context["previous"], reader)
    assert second_changed == context["first_changed"]
    fed = reader.get(second_changed[0])
    assert fed.atom_split_status == "ready"
    context["fed"] = fed


@then("the fed atoms include both the previously seen atom and the newly split atom")
def then_fed_atoms_include_both_atoms(context):
    fed = context["fed"]
    assert {a.atom_id for a in fed.atoms} == {
        "atom_traj_user_incremental_0001",
        "atom_traj_user_incremental_0002",
    }


@then("only the newly split atom_id is unseen by the kernel")
def then_only_newly_split_atom_unseen(context):
    fed = context["fed"]
    newly = [a for a in fed.atoms if a.atom_id not in context["seen"]]
    assert len(newly) == 1
    assert newly[0].atom_id == "atom_traj_user_incremental_0002"
    assert newly[0].content == "second segment"


# ── Scenario: demo kernel offline distillation remains compatible ───────


@given("the demo algorithm kernel and the mock runtime trajectories")
def given_demo_kernel_and_mock_trajectories(tmp_path, context):
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
    context["plugin_dir"] = plugin_dir
    context["traj_dir"] = repo / "examples" / "kernels" / "mock-runtime-trajectories"
    context["output"] = tmp_path / "out"
    context["xskill_home"] = tmp_path / "home"


@when("offline distillation runs against the demo kernel")
def when_offline_distillation_runs(context):
    context["report"] = run_offline_distillation(
        kernel_id="your-demo-algo-kernel",
        trajectory_dir=context["traj_dir"],
        plugin_dir=context["plugin_dir"],
        xskill_home=context["xskill_home"],
        output_dir=context["output"],
        no_progress=True,
    )


@then("the distillation report status is success with submitted skills")
def then_distillation_report_success(context):
    report = context["report"]
    output = context["output"]
    assert report.status == "success"
    assert report.submitted_skills
    assert (output / "skills").is_dir()
    result = json.loads((output / "result.json").read_text(encoding="utf-8"))
    assert result["status"] == "success"
