"""pytest-bdd + aimock: benchmark temp atoms must enter train_skills inputs."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib import request as urlrequest

import pytest
from pytest_bdd import given, scenarios, then, when

pytest.importorskip(
    "openearth_skill_sdk",
    reason="install OpenEarth wheel from PR #155 to run benchmark-atom BDD",
)

from openearth_skill_sdk.contracts import DistillationResult
from openearth_skill_sdk.xskill import record_oracle_score, train_skills

from xskill.kernels.context import TrajectoryReader
from xskill.pipeline.atom import AtomTask, AtomTaskStore
from xskill.pipeline.registry import update_traj_status

scenarios("features/kernel/benchmark_atoms_enter_training.feature")


PLATFORM_MD = """## User

Solve the spreadsheet formula for Q1 revenue.

## Assistant

I will open the workbook and compute the answer.
"""


def _write_atom(
    store: AtomTaskStore,
    *,
    traj_id: str,
    atom_id: str,
    raw_segment: str,
    offset_start: int,
    offset_end: int,
) -> None:
    store.save(AtomTask(
        atom_id=atom_id,
        traj_id=traj_id,
        offset_start=offset_start,
        offset_end=offset_end,
        intent="intent",
        summary="summary",
        used_skills=[],
        ux_score=None,
        raw_segment=raw_segment,
    ))


def _simulate_split_ready(
    *,
    registry_db: Path,
    watch_dir_id: int,
    traj_path: Path,
    atoms: list[dict[str, Any]],
) -> None:
    update_traj_status(
        watch_dir_id, traj_path.name, status="split_done", db_path=registry_db,
    )
    store = AtomTaskStore(root=traj_path.parent)
    for index, atom in enumerate(atoms, start=1):
        _write_atom(
            store,
            traj_id=traj_path.stem,
            atom_id=atom["atom_id"],
            raw_segment=atom["content"],
            offset_start=index,
            offset_end=index + 1,
        )


@pytest.fixture
def context() -> dict[str, Any]:
    return {}


@given("aimock 在随机本地端口启动 OpenAI-compatible 服务")
def given_aimock_running(aimock, context: dict[str, Any]) -> None:
    assert aimock.base_url.startswith("http://127.0.0.1:")
    context["aimock"] = aimock
    aimock.on_message("ping-train-path", {"content": "pong"})


@given("平台 TrajectoryReader 已配置 kernel-temp 且 auto_index 开启")
def given_reader_kernel_temp(tmp_path, context: dict[str, Any]) -> None:
    registry_db = tmp_path / "registry.db"
    temp_root = tmp_path / "temp_trajectories"
    workspace = tmp_path / "oe_workspace"
    workspace.mkdir()
    reader = TrajectoryReader(registry_db, temp_root=temp_root)
    context["registry_db"] = registry_db
    context["reader"] = reader
    context["workspace"] = workspace
    context["config_path"] = tmp_path / "openearth.yaml"
    context["config_path"].write_text(
        "reflect:\n  model: aimock-bench\n  base_url: http://127.0.0.1:9\n",
        encoding="utf-8",
    )


@given("OpenEarth workspace 已记录该临时轨的 oracle 分")
def given_oracle_score_prepared(context: dict[str, Any]) -> None:
    context["trajectory_id"] = "traj_temp_bench_bdd_001"
    context["oracle_score"] = 9
    record_oracle_score(
        workspace=context["workspace"],
        trajectory_id=context["trajectory_id"],
        case_id="case-bdd-1",
        ux_score=context["oracle_score"],
    )


@when("内核 create_temp 写入平台形做题轨迹")
def when_create_temp(context: dict[str, Any]) -> None:
    reader: TrajectoryReader = context["reader"]
    created = reader.create_temp(
        PLATFORM_MD, trajectory_id=context["trajectory_id"],
    )
    temp_watch = next(
        item for item in reader.directories() if item.ecosystem == "kernel-temp"
    )
    assert temp_watch.auto_index is True
    context["created"] = created
    context["temp_watch_id"] = int(temp_watch.id)


@when("平台将临时轨迹拆成多个 ready atom")
def when_split_multi_atoms(context: dict[str, Any]) -> None:
    created = context["created"]
    atoms = [
        {
            "atom_id": f"atom_{context['trajectory_id']}_0001",
            "content": "Solve the spreadsheet formula for Q1 revenue.",
        },
        {
            "atom_id": f"atom_{context['trajectory_id']}_0002",
            "content": "I will open the workbook and compute the answer.",
        },
    ]
    _simulate_split_ready(
        registry_db=context["registry_db"],
        watch_dir_id=context["temp_watch_id"],
        traj_path=created.path,
        atoms=atoms,
    )
    context["expected_atom_ids"] = {row["atom_id"] for row in atoms}
    ready = context["reader"].get(created.id)
    assert ready.atom_split_status == "ready"
    assert len(ready.atoms) == 2
    context["ready"] = ready


@when("配置指向 aimock 后调用 train_skills")
def when_train_skills_via_aimock(context: dict[str, Any], monkeypatch) -> None:
    aimock = context["aimock"]
    captured: list[Any] = []
    context["captured"] = captured
    context["train_error"] = None

    def _capturing_distill(
        self,
        atoms,
        existing_skills=(),
        *,
        run_id: str,
        full_rebuild: bool = False,
    ):
        items = list(atoms)
        captured.extend(items)
        return DistillationResult(
            run_id=run_id,
            candidate_dir=None,
            drafts=(),
            processed_trajectory_ids=tuple(
                dict.fromkeys(item.parent_trajectory_id for item in items)
            ),
            processed_atom_ids=tuple(item.atom_id for item in items),
            metrics={"oracle_scores": sum(
                1 for item in items if item.score_source == "oracle"
            )},
            notes="bdd-capture",
        )

    monkeypatch.setattr(
        "openearth_skill_sdk.service.TrajectorySkillDistiller.distill",
        _capturing_distill,
    )

    # Prove the aimock OpenAI-compatible boundary is reachable from this run.
    payload = json.dumps({
        "model": "aimock-bench",
        "messages": [{"role": "user", "content": "ping-train-path"}],
    }).encode("utf-8")
    req = urlrequest.Request(
        f"{aimock.base_url}/v1/chat/completions",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlrequest.urlopen(req, timeout=30) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    context["aimock_probe"] = body

    class _UnusedAnalyst:
        """train_skills still constructs Distiller; distill is monkeypatched."""

    try:
        train_skills(
            config_path=context["config_path"],
            workspace=context["workspace"],
            trajectories=[context["ready"]],
            existing_skills=(),
            run_id="bdd-bench-run",
            full_rebuild=True,
            analyst=_UnusedAnalyst(),
        )
    except Exception as exc:  # noqa: BLE001 - capture for Then steps
        context["train_error"] = exc


@then("train_skills 收到的 ScoredAtomInput 包含这些 temp atom_id")
def then_captured_contains_temp_atom_ids(context: dict[str, Any]) -> None:
    assert context["train_error"] is None
    got = {item.atom_id for item in context["captured"]}
    assert context["expected_atom_ids"] <= got


@then("对应 score_source 均为 oracle")
def then_score_source_oracle(context: dict[str, Any]) -> None:
    expected = context["expected_atom_ids"]
    matched = [
        item for item in context["captured"] if item.atom_id in expected
    ]
    assert matched
    assert all(item.score_source == "oracle" for item in matched)
    assert all(
        item.ux_score == context["oracle_score"] for item in matched
    )


@then("aimock 至少收到一次 chat completions 探活请求")
def then_aimock_got_probe(context: dict[str, Any]) -> None:
    body = context["aimock_probe"]
    assert body
    journal = context["aimock"].get_journal()
    assert journal, "aimock journal should record the probe request"


@then("train_skills 不因多 atom 抛错")
def then_no_multi_atom_error(context: dict[str, Any]) -> None:
    assert context["train_error"] is None
    assert "exactly one" not in str(context.get("train_error") or "")


@then("train_skills 收到的 ScoredAtomInput 数量等于拆出的 atom 数")
def then_captured_count_matches(context: dict[str, Any]) -> None:
    assert len(context["captured"]) == len(context["expected_atom_ids"])
