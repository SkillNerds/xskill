from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from xskill.dashboard.auth import (
    build_auth_router,
    configure_auth,
    ensure_dashboard_secret,
)
from xskill.dashboard.router import build_dashboard_router
from xskill.pipeline.atom import AtomTask, AtomTaskStore
from xskill.pipeline.registry import (
    discover_trajectories,
    get_connection,
    pooled_connection,
    register_dir,
    unregister_dir,
)
from xskill.tasks.models import stable_ref_key
from xskill.tasks.projection import (
    enqueue_untracked_sources,
    get_logical_task,
    list_dirty_task_scopes,
    list_dirty_sources,
    list_logical_tasks,
    list_task_scopes,
    task_graph_overview,
    tasks_for_atom,
)
from xskill.tasks.scopes import ScopeResolver
from xskill.tasks.service import TaskGraphService
from xskill.tasks.store import TaskGraphStore


def _add_source(
    root: Path,
    db_path: Path,
    *,
    source_name: str,
    atoms: list[dict],
    workspace: str = "/workspace/demo",
    usage: dict | None = None,
    final_status: str | None = None,
) -> tuple[int, str]:
    watch_dir = root / "trajectories"
    watch_dir.mkdir(parents=True, exist_ok=True)
    filename = f"traj_{source_name}.md"
    trajectory_path = watch_dir / filename
    lines = ["# Trajectory", ""]
    for atom in atoms:
        lines.extend(("## User", atom["intent"], "", "## Assistant", atom.get("summary", "done"), ""))
    trajectory_path.write_text("\n".join(lines), encoding="utf-8")
    metadata = {
        "source": "codex_rollout_jsonl",
        "source_model": "model-test",
        "model_provider": "provider-test",
        "cwd": workspace,
    }
    if usage is not None:
        metadata["execution_usage_events"] = [{
            "source_event_id": f"usage-{source_name}",
            "usage": usage,
        }]
    if final_status is not None:
        metadata["final_status"] = final_status
    trajectory_path.with_suffix(".json").write_text(
        json.dumps(metadata), encoding="utf-8",
    )
    trajectory_id = filename[:-3]
    offset = 1
    atom_records = []
    for index, atom in enumerate(atoms, 1):
        span = int(atom.get("span", 5))
        atom_records.append(AtomTask(
            atom_id=f"atom_{trajectory_id}_{index:04d}",
            traj_id=trajectory_id,
            offset_start=offset,
            offset_end=offset + span,
            intent=atom["intent"],
            summary=atom.get("summary", atom["intent"]),
            used_skills=list(atom.get("used_skills") or ()),
            pre_atom_id=(
                f"atom_{trajectory_id}_{index - 1:04d}" if index > 1 else None
            ),
            post_atom_id=(
                f"atom_{trajectory_id}_{index + 1:04d}"
                if index < len(atoms) else None
            ),
            raw_segment=f"{atom['intent']}\n{atom.get('summary', '')}",
            source_model="model-test",
        ))
        offset += span
    store = AtomTaskStore(watch_dir)
    for atom_record in atom_records:
        store.save(atom_record)
    watch_dir_id = register_dir(watch_dir, db_path=db_path)
    discover_trajectories(watch_dir_id, watch_dir, db_path=db_path)
    with pooled_connection(db_path) as connection:
        connection.execute(
            "UPDATE trajectories SET status='split_done',tasks_extracted=?"
            " WHERE watch_dir_id=? AND filename=?",
            (len(atom_records), watch_dir_id, filename),
        )
        connection.commit()
    return watch_dir_id, filename


def _build(root: Path, db_path: Path, sources: list[tuple[int, str]]) -> TaskGraphService:
    service = TaskGraphService(
        state_root=root,
        db_path=db_path,
        config={"task_graph": {"enabled": True}},
    )
    for watch_dir_id, filename in sources:
        service.mark_dirty(watch_dir_id, filename, reason="test")
    result = service.process_dirty()
    assert result["sources"] == len(sources)
    return service


def test_reading_missing_tenant_identity_has_no_filesystem_side_effect(tmp_path):
    resolver = ScopeResolver(tmp_path, db_path=tmp_path / "registry.db")
    assert resolver.existing_tenant_id is None
    assert not (tmp_path / "task_graph").exists()


def test_task_graph_is_enabled_by_default_and_explicit_false_still_wins(tmp_path):
    default_service = TaskGraphService(
        state_root=tmp_path,
        db_path=tmp_path / "registry.db",
        config={},
    )
    disabled_service = TaskGraphService(
        state_root=tmp_path,
        db_path=tmp_path / "registry.db",
        config={"task_graph": {"enabled": False}},
    )

    assert default_service.enabled is True
    assert disabled_service.enabled is False


def test_initial_backfill_enqueues_in_committed_bounded_batches(
    tmp_path, monkeypatch,
):
    db_path = tmp_path / "registry.db"
    for index in range(5):
        _add_source(
            tmp_path,
            db_path,
            source_name=f"backfill-{index}",
            atoms=[{"intent": f"回填任务 {index}", "summary": "加入持久队列"}],
        )
    connection = get_connection(db_path)
    connection.execute("DELETE FROM task_graph_dirty_sources")
    connection.commit()

    class CountingConnection:
        def __init__(self, wrapped):
            self.wrapped = wrapped
            self.commits = 0

        def __getattr__(self, name):
            return getattr(self.wrapped, name)

        def commit(self):
            self.commits += 1
            self.wrapped.commit()

    counted = CountingConnection(connection)

    @contextmanager
    def counted_pool(_db_path):
        yield counted

    monkeypatch.setattr(
        "xskill.tasks.projection.pooled_connection",
        counted_pool,
    )

    assert enqueue_untracked_sources(batch_size=2, db_path=db_path) == 5
    assert counted.commits == 3
    assert enqueue_untracked_sources(batch_size=2, db_path=db_path) == 0
    assert counted.commits == 3
    assert connection.execute(
        "SELECT COUNT(*) FROM task_graph_dirty_sources",
    ).fetchone()[0] == 5
    tracked = connection.execute(
        "SELECT t.watch_dir_id,t.filename,w.source_scope_id"
        " FROM trajectories t JOIN watch_dirs w ON w.id=t.watch_dir_id"
        " ORDER BY t.id LIMIT 1",
    ).fetchone()
    connection.execute(
        "DELETE FROM task_graph_dirty_sources WHERE watch_dir_id=? AND filename=?",
        (tracked["watch_dir_id"], tracked["filename"]),
    )
    connection.execute(
        "INSERT INTO task_graph_source_state("
        "tenant_id,task_scope_id,source_scope_id,watch_dir_id,traj_id,"
        "source_revision,generation_id) VALUES(?,?,?,?,?,?,?)",
        (
            "ten_test",
            "tsc_test",
            tracked["source_scope_id"],
            tracked["watch_dir_id"],
            tracked["filename"].removesuffix(".md"),
            "rev_test",
            "gen_test",
        ),
    )
    connection.commit()
    assert enqueue_untracked_sources(batch_size=2, db_path=db_path) == 0
    assert connection.execute(
        "SELECT COUNT(*) FROM task_graph_dirty_sources",
    ).fetchone()[0] == 4
    connection.close()


@pytest.mark.parametrize("batch_size", [True, 0, -1, 1.5])
def test_initial_backfill_rejects_invalid_batch_size(tmp_path, batch_size):
    with pytest.raises(ValueError, match="batch_size"):
        enqueue_untracked_sources(
            batch_size=batch_size,
            db_path=tmp_path / "registry.db",
        )


def test_runtime_projects_task_attempt_evidence_and_conserved_usage(tmp_path):
    db_path = tmp_path / "registry.db"
    source = _add_source(
        tmp_path,
        db_path,
        source_name="usage",
        atoms=[
            {"intent": "修复登录问题", "summary": "修复认证错误", "span": 3, "used_skills": ["debug"]},
            {"intent": "编写发布说明", "summary": "整理版本文档", "span": 7, "used_skills": ["docs"]},
        ],
        usage={
            "input_tokens": 80,
            "output_tokens": 20,
            "total_tokens": 100,
            "cached_input_tokens": 40,
        },
    )
    service = _build(tmp_path, db_path, [source])
    tenant_id = service.resolver.tenant_id
    tasks = list_logical_tasks(tenant_id, db_path=db_path)
    assert len(tasks) == 2
    details = [
        get_logical_task(
            tenant_id, task["task_scope_id"], task["task_id"], db_path=db_path,
        )
        for task in tasks
    ]
    allocations = [
        allocation
        for detail in details
        for allocation in detail["usage_allocations"]
    ]
    assert sum(item["fraction"] for item in allocations) == pytest.approx(1.0)
    assert sum(item["total_tokens"] for item in allocations) == 100
    assert sum(item["cache_read_tokens"] for item in allocations) == 40
    assert {item["allocation_mode"] for item in allocations} == {"shared"}
    assert all(detail["attempts"][0]["lifecycle"] == "running" for detail in details)
    assert all(detail["usage_events"][0]["measurement_quality"] == "measured" for detail in details)
    overview = task_graph_overview(tenant_id, db_path=db_path)
    assert overview["execution_tokens"] == 100


def test_task_graph_dashboard_routes_require_admin_and_apply_override(
    tmp_path, monkeypatch,
):
    db_path = tmp_path / "registry.db"
    source = _add_source(
        tmp_path,
        db_path,
        source_name="dashboard-admin",
        atoms=[{"intent": "审核任务状态", "summary": "等待管理员修正"}],
    )
    service = _build(tmp_path, db_path, [source])
    tenant_id = service.resolver.tenant_id
    task = list_logical_tasks(tenant_id, db_path=db_path)[0]
    monkeypatch.setattr(
        "xskill.config.get_config",
        lambda: {"task_graph": {"enabled": True}},
    )
    configure_auth(
        secret=ensure_dashboard_secret(tmp_path / "dashboard-secret.json"),
        admins=["reviewer"],
        admin_password="secret",
    )
    app = FastAPI()
    app.include_router(build_auth_router())
    app.include_router(build_dashboard_router(db_path=db_path))

    anonymous = TestClient(app)
    assert anonymous.get(
        "/api/v1/dashboard/task-graph/overview",
    ).status_code == 401
    admin = TestClient(app)
    login = admin.post(
        "/api/v1/dashboard/login",
        json={"user_name": "reviewer", "secret": "secret"},
    )
    assert login.status_code == 200
    overview = admin.get(
        "/api/v1/dashboard/task-graph/overview",
    ).json()
    assert overview["tasks"] == 1
    assert overview["evidence_feed"] == {
        "pending": 1, "processed": 0, "fallback": 0, "rejected": 0,
    }
    response = admin.post(
        "/api/v1/dashboard/task-graph/override",
        json={
            "task_scope_id": task["task_scope_id"],
            "operation": "set_task_state",
            "target_id": task["task_id"],
            "payload": {"lifecycle": "blocked"},
        },
    )
    assert response.status_code == 200
    current = service.store_for_scope(task["task_scope_id"]).load_current()
    assert next(
        item for item in current.tasks if item.task_id == task["task_id"]
    ).lifecycle == "blocked"


def test_explicit_retry_reuses_task_and_creates_attempt_relation(tmp_path):
    db_path = tmp_path / "registry.db"
    source = _add_source(
        tmp_path,
        db_path,
        source_name="retry",
        atoms=[
            {"intent": "修复登录问题", "summary": "定位认证失败"},
            {"intent": "重试修复登录问题", "summary": "重新执行认证修复"},
        ],
    )
    service = _build(tmp_path, db_path, [source])
    tenant_id = service.resolver.tenant_id
    tasks = list_logical_tasks(tenant_id, db_path=db_path)
    assert len(tasks) == 1
    detail = get_logical_task(
        tenant_id, tasks[0]["task_scope_id"], tasks[0]["task_id"], db_path=db_path,
    )
    assert len(detail["attempts"]) == 2
    assert detail["attempt_relations"][0]["relation_type"] == "retry_of"
    assert detail["attempt_relations"][0]["decision"] == "confirmed"
    assert detail["usage_events"][0]["measurement_quality"] == "unavailable"
    assert detail["usage_events"][0]["unavailable_reason"] == (
        "source_did_not_report_usage"
    )


def test_ambiguous_similarity_stays_proposed_instead_of_silent_merge(tmp_path):
    db_path = tmp_path / "registry.db"
    source = _add_source(
        tmp_path,
        db_path,
        source_name="ambiguous",
        atoms=[
            {"intent": "优化数据库登录查询", "summary": "检查登录查询性能"},
            {"intent": "分析数据库登录查询", "summary": "分析登录查询索引"},
        ],
    )
    service = _build(tmp_path, db_path, [source])
    tenant_id = service.resolver.tenant_id
    tasks = list_logical_tasks(tenant_id, db_path=db_path)
    assert len(tasks) == 2
    with pooled_connection(db_path) as connection:
        proposed = connection.execute(
            "SELECT task_scope_id,membership_id,confidence,decision"
            " FROM task_atom_memberships"
            " WHERE decision='proposed'",
        ).fetchall()
    assert proposed
    assert all(row["confidence"] is None for row in proposed)
    service.append_override(
        task_scope_id=proposed[0]["task_scope_id"],
        operation="confirm_membership",
        target_id=proposed[0]["membership_id"],
        payload={},
        actor="reviewer",
    )
    service.mark_dirty(*source, reason="repeat_manual_membership")
    service.process_dirty()
    current = service.store_for_scope(proposed[0]["task_scope_id"]).load_current()
    confirmed_primary = [
        membership for membership in current.memberships
        if membership.role == "primary"
        and membership.decision == "confirmed"
        and not membership.stale
    ]
    assert len(confirmed_primary) == 2
    assert any(
        membership.decided_by == "human:reviewer"
        for membership in confirmed_primary
    )


def test_stable_ids_and_bounded_candidates_on_unchanged_rebuild(tmp_path):
    db_path = tmp_path / "registry.db"
    atoms = [
        {"intent": f"处理模块 {index} 性能", "summary": f"模块 {index} 性能分析"}
        for index in range(40)
    ]
    source = _add_source(
        tmp_path, db_path, source_name="bounded", atoms=atoms,
    )
    service = _build(tmp_path, db_path, [source])
    store = next((tmp_path / "task_graph" / "scopes").iterdir())
    first = service.store_for_scope(store.name).load_current()
    assert first.metrics["candidate_count"] <= len(atoms) * 8
    pointer = json.loads((store / "current.json").read_text(encoding="utf-8"))
    manifest = json.loads(
        (store / "generations" / pointer["generation_id"] / "manifest.json")
        .read_text(encoding="utf-8")
    )
    assert len(manifest["memberships"]) <= 2
    assert all("record_count" in shard for shard in manifest["memberships"])
    service.mark_dirty(*source, reason="repeat")
    service.process_dirty()
    second = service.store_for_scope(store.name).load_current()
    assert {task.task_id for task in first.tasks} == {task.task_id for task in second.tasks}
    assert second.generation_id == first.generation_id


def test_changed_linker_config_forces_a_new_generation(tmp_path):
    db_path = tmp_path / "registry.db"
    source = _add_source(
        tmp_path,
        db_path,
        source_name="generator-change",
        atoms=[{"intent": "检查生成器版本", "summary": "重建派生图"}],
    )
    service = _build(tmp_path, db_path, [source])
    tenant_id = service.resolver.tenant_id
    task = list_logical_tasks(tenant_id, db_path=db_path)[0]
    first = service.store_for_scope(task["task_scope_id"]).load_current()

    changed = TaskGraphService(
        state_root=tmp_path,
        db_path=db_path,
        config={"task_graph": {"enabled": True, "top_k": 3}},
    )
    result = changed.process_dirty()
    second = changed.store_for_scope(task["task_scope_id"]).load_current()

    assert result["failed_scopes"] == []
    assert second.generation_id != first.generation_id
    assert second.generator["top_k"] == 3
    assert {item.task_id for item in second.tasks} == {
        item.task_id for item in first.tasks
    }


def test_restart_reloads_manifest_shards_and_preserves_generation(tmp_path):
    db_path = tmp_path / "registry.db"
    source = _add_source(
        tmp_path,
        db_path,
        source_name="restart-replay",
        atoms=[
            {"intent": "验证重启恢复", "summary": "读取不可变分片"},
            {"intent": "编写恢复说明", "summary": "保持任务身份"},
        ],
        usage={
            "input_tokens": 10,
            "output_tokens": 2,
            "total_tokens": 12,
        },
    )
    first_service = _build(tmp_path, db_path, [source])
    tenant_id = first_service.resolver.tenant_id
    first_tasks = list_logical_tasks(tenant_id, db_path=db_path)
    scope_id = first_tasks[0]["task_scope_id"]
    first_generation = first_service.store_for_scope(scope_id).load_current()
    if os.name != "nt":
        scope_dir = tmp_path / "task_graph" / "scopes" / scope_id
        assert scope_dir.stat().st_mode & 0o777 == 0o700
        assert all(
            path.stat().st_mode & 0o777 == 0o600
            for path in scope_dir.rglob("*.json")
        )

    restarted = TaskGraphService(
        state_root=tmp_path,
        db_path=db_path,
        config={"task_graph": {"enabled": True}},
    )
    reloaded = restarted.store_for_scope(scope_id).load_current()
    assert reloaded.generation_id == first_generation.generation_id
    assert reloaded.to_dict() == first_generation.to_dict()

    restarted.mark_dirty(*source, reason="restart_reconcile")
    result = restarted.process_dirty()
    assert result["failed_scopes"] == []
    replayed = restarted.store_for_scope(scope_id).load_current()
    assert replayed.generation_id == first_generation.generation_id
    assert {
        task.task_id for task in replayed.tasks
    } == {
        task["task_id"] for task in first_tasks
    }


def test_invalid_current_pointer_is_reported_as_recoverable_store_error(tmp_path):
    store = TaskGraphStore(tmp_path / "task_graph" / "scopes" / ("tsc_" + "a" * 32))
    store.current_path.parent.mkdir(parents=True)
    store.current_path.write_text("[]", encoding="utf-8")
    with pytest.raises(RuntimeError, match="invalid Task Graph current pointer"):
        store.load_current()


def test_manual_membership_survives_deleted_atom_as_stale_fact(tmp_path):
    db_path = tmp_path / "registry.db"
    source = _add_source(
        tmp_path,
        db_path,
        source_name="delete",
        atoms=[{"intent": "修复删除测试", "summary": "保留人工事实"}],
    )
    service = _build(tmp_path, db_path, [source])
    tenant_id = service.resolver.tenant_id
    task = list_logical_tasks(tenant_id, db_path=db_path)[0]
    detail = get_logical_task(
        tenant_id, task["task_scope_id"], task["task_id"], db_path=db_path,
    )
    membership = detail["memberships"][0]
    service.append_override(
        task_scope_id=task["task_scope_id"],
        operation="confirm_membership",
        target_id=membership["membership_id"],
        payload={},
        actor="reviewer",
    )
    atom_path = next((tmp_path / "trajectories" / "traj_delete" / "tasks").glob("*.json"))
    atom_path.unlink()
    service.mark_dirty(*source, reason="deleted", deleted=True)
    service.process_dirty()
    current = service.store_for_scope(task["task_scope_id"]).load_current()
    stale = [item for item in current.memberships if item.decided_by == "human:reviewer"]
    assert len(stale) == 1
    assert stale[0].stale is True
    assert next(item for item in current.tasks if item.task_id == task["task_id"]).tombstoned
    assert current.attempts[0].outcome == "unknown"
    assert all(evidence.stale for evidence in current.attempts[0].evidence_ranges)


def test_invalid_relation_cycle_is_rejected_before_override_log_append(tmp_path):
    db_path = tmp_path / "registry.db"
    source = _add_source(
        tmp_path,
        db_path,
        source_name="cycle",
        atoms=[
            {"intent": "目标甲", "summary": "完成甲"},
            {"intent": "目标乙", "summary": "完成乙"},
        ],
    )
    service = _build(tmp_path, db_path, [source])
    tenant_id = service.resolver.tenant_id
    tasks = list_logical_tasks(tenant_id, db_path=db_path)
    scope_id = tasks[0]["task_scope_id"]
    first_id, second_id = sorted(task["task_id"] for task in tasks)
    service.append_override(
        task_scope_id=scope_id,
        operation="upsert_task_relation",
        target_id=first_id,
        payload={
            "from_task_id": first_id,
            "to_task_id": second_id,
            "relation_type": "depends_on",
        },
        actor="reviewer",
    )
    with pytest.raises(ValueError, match="DAG"):
        service.append_override(
            task_scope_id=scope_id,
            operation="upsert_task_relation",
            target_id=second_id,
            payload={
                "from_task_id": second_id,
                "to_task_id": first_id,
                "relation_type": "depends_on",
            },
            actor="reviewer",
        )
    assert service.store_for_scope(scope_id).override_watermark() == 1


def test_override_ids_are_not_coerced_from_json_numbers(tmp_path):
    db_path = tmp_path / "registry.db"
    source = _add_source(
        tmp_path,
        db_path,
        source_name="strict-override-ids",
        atoms=[
            {"intent": "目标甲", "summary": "完成甲"},
            {"intent": "目标乙", "summary": "完成乙"},
        ],
    )
    service = _build(tmp_path, db_path, [source])
    tenant_id = service.resolver.tenant_id
    tasks = list_logical_tasks(tenant_id, db_path=db_path)
    scope_id = tasks[0]["task_scope_id"]
    canonical_id = tasks[0]["task_id"]
    with pytest.raises(ValueError, match="must contain non-empty strings"):
        service.append_override(
            task_scope_id=scope_id,
            operation="merge_tasks",
            target_id=canonical_id,
            payload={"task_ids": [8]},
            actor="reviewer",
        )
    with pytest.raises(ValueError, match="event_id"):
        service.append_override(
            task_scope_id=scope_id,
            operation="set_task_state",
            target_id=canonical_id,
            payload={"lifecycle": "open"},
            actor="reviewer",
            event_id=8,
        )
    assert service.store_for_scope(scope_id).override_watermark() == 0


def test_idempotent_override_retry_rebuilds_an_unmaterialized_event(
    tmp_path, monkeypatch,
):
    db_path = tmp_path / "registry.db"
    source = _add_source(
        tmp_path,
        db_path,
        source_name="override-retry",
        atoms=[{"intent": "修正任务状态", "summary": "验证幂等恢复"}],
    )
    service = _build(tmp_path, db_path, [source])
    tenant_id = service.resolver.tenant_id
    task = list_logical_tasks(tenant_id, db_path=db_path)[0]
    scope_id = task["task_scope_id"]
    original_rebuild = service._rebuild_scope

    def fail_rebuild(*_args, **_kwargs):
        raise RuntimeError("injected projection failure")

    monkeypatch.setattr(service, "_rebuild_scope", fail_rebuild)
    with pytest.raises(RuntimeError, match="injected projection failure"):
        service.append_override(
            task_scope_id=scope_id,
            operation="set_task_state",
            target_id=task["task_id"],
            payload={"lifecycle": "blocked"},
            actor="reviewer",
            event_id="override-retry-1",
        )
    store = service.store_for_scope(scope_id)
    assert store.override_watermark() == 1
    assert store.load_current().base_override_seq == 0

    monkeypatch.setattr(service, "_rebuild_scope", original_rebuild)
    retried = service.append_override(
        task_scope_id=scope_id,
        operation="set_task_state",
        target_id=task["task_id"],
        payload={"lifecycle": "blocked"},
        actor="reviewer",
        event_id="override-retry-1",
    )
    assert retried.override_seq == 1
    current = store.load_current()
    assert current.base_override_seq == 1
    assert next(
        item for item in current.tasks if item.task_id == task["task_id"]
    ).lifecycle == "blocked"
    assert list_dirty_sources(db_path=db_path) == []
    assert list_dirty_task_scopes(db_path=db_path) == []


def test_unregister_dir_reconciles_the_removed_source(tmp_path):
    db_path = tmp_path / "registry.db"
    source = _add_source(
        tmp_path,
        db_path,
        source_name="unregister",
        atoms=[{"intent": "注销轨迹目录", "summary": "清理派生任务"}],
    )
    service = _build(tmp_path, db_path, [source])
    tenant_id = service.resolver.tenant_id
    task = list_logical_tasks(tenant_id, db_path=db_path)[0]

    assert unregister_dir(tmp_path / "trajectories", db_path=db_path)
    dirty = list_dirty_sources(db_path=db_path)
    assert len(dirty) == 1
    assert dirty[0]["deleted"] == 1
    assert dirty[0]["task_scope_id"] == task["task_scope_id"]

    restarted = TaskGraphService(
        state_root=tmp_path,
        db_path=db_path,
        config={"task_graph": {"enabled": True}},
    )
    result = restarted.process_dirty()

    assert result["failed_scopes"] == []
    assert result["sources"] == 1
    assert list_dirty_sources(db_path=db_path) == []
    assert list_logical_tasks(tenant_id, db_path=db_path) == []
    historical = list_logical_tasks(
        tenant_id, include_tombstones=True, db_path=db_path,
    )
    assert len(historical) == 1
    assert historical[0]["task_id"] == task["task_id"]


def test_override_scope_queue_recovers_without_any_live_source(
    tmp_path, monkeypatch,
):
    db_path = tmp_path / "registry.db"
    source = _add_source(
        tmp_path,
        db_path,
        source_name="scope-only-recovery",
        atoms=[{"intent": "保留删除后的任务", "summary": "修正 tombstone"}],
    )
    service = _build(tmp_path, db_path, [source])
    tenant_id = service.resolver.tenant_id
    task = list_logical_tasks(
        tenant_id, include_tombstones=True, db_path=db_path,
    )[0]
    scope_id = task["task_scope_id"]
    service.mark_dirty(*source, reason="source_deleted", deleted=True)
    service.process_dirty()
    assert list_dirty_sources(db_path=db_path) == []

    def fail_rebuild(*_args, **_kwargs):
        raise RuntimeError("injected no-source projection failure")

    monkeypatch.setattr(service, "_rebuild_scope", fail_rebuild)
    with pytest.raises(RuntimeError, match="injected no-source projection failure"):
        service.append_override(
            task_scope_id=scope_id,
            operation="set_task_state",
            target_id=task["task_id"],
            payload={"lifecycle": "blocked"},
            actor="reviewer",
            event_id="scope-only-recovery-1",
        )
    assert len(list_dirty_task_scopes(db_path=db_path)) == 1

    restarted = TaskGraphService(
        state_root=tmp_path,
        db_path=db_path,
        config={"task_graph": {"enabled": True}},
    )
    result = restarted.process_dirty()
    assert result["override_scopes"] == 1
    assert list_dirty_task_scopes(db_path=db_path) == []
    current = restarted.store_for_scope(scope_id).load_current()
    assert current.base_override_seq == 1
    assert next(
        item for item in current.tasks if item.task_id == task["task_id"]
    ).lifecycle == "blocked"


def test_parent_and_subtask_directions_share_one_dag(tmp_path):
    db_path = tmp_path / "registry.db"
    source = _add_source(
        tmp_path,
        db_path,
        source_name="parent-subtask-cycle",
        atoms=[
            {"intent": "目标甲", "summary": "完成甲"},
            {"intent": "目标乙", "summary": "完成乙"},
        ],
    )
    service = _build(tmp_path, db_path, [source])
    tenant_id = service.resolver.tenant_id
    tasks = list_logical_tasks(tenant_id, db_path=db_path)
    scope_id = tasks[0]["task_scope_id"]
    first_id, second_id = sorted(task["task_id"] for task in tasks)
    service.append_override(
        task_scope_id=scope_id,
        operation="upsert_task_relation",
        target_id=first_id,
        payload={
            "from_task_id": first_id,
            "to_task_id": second_id,
            "relation_type": "parent",
        },
        actor="reviewer",
    )
    with pytest.raises(ValueError, match="DAG"):
        service.append_override(
            task_scope_id=scope_id,
            operation="upsert_task_relation",
            target_id=first_id,
            payload={
                "from_task_id": first_id,
                "to_task_id": second_id,
                "relation_type": "subtask",
            },
            actor="reviewer",
        )
    assert service.store_for_scope(scope_id).override_watermark() == 1


def test_scoped_atom_lookup_does_not_use_bare_atom_id(tmp_path):
    db_path = tmp_path / "registry.db"
    source = _add_source(
        tmp_path,
        db_path,
        source_name="lookup",
        atoms=[{"intent": "检查作用域", "summary": "验证 scoped ref"}],
    )
    service = _build(tmp_path, db_path, [source])
    tenant_id = service.resolver.tenant_id
    task = list_logical_tasks(tenant_id, db_path=db_path)[0]
    detail = get_logical_task(
        tenant_id, task["task_scope_id"], task["task_id"], db_path=db_path,
    )
    membership = detail["memberships"][0]
    hit = tasks_for_atom(
        tenant_id,
        membership["source_scope_id"],
        membership["traj_id"],
        membership["atom_id"],
        db_path=db_path,
    )
    miss = tasks_for_atom(
        tenant_id,
        "src_wrong",
        membership["traj_id"],
        membership["atom_id"],
        db_path=db_path,
    )
    assert len(hit["tasks"]) == 1
    assert miss == {"tasks": [], "memberships": []}


def test_registry_migration_preserves_legacy_usage_without_inventing_scope(tmp_path):
    db_path = tmp_path / "registry.db"
    connection = sqlite3.connect(db_path)
    connection.executescript(
        "CREATE TABLE watch_dirs("
        "id INTEGER PRIMARY KEY AUTOINCREMENT,path TEXT UNIQUE NOT NULL,"
        "label TEXT DEFAULT '',auto_index INTEGER DEFAULT 1,"
        "ecosystem TEXT DEFAULT 'manual',created_at TEXT);"
        "CREATE TABLE trajectories("
        "id INTEGER PRIMARY KEY AUTOINCREMENT,watch_dir_id INTEGER NOT NULL,"
        "filename TEXT NOT NULL,has_meta INTEGER DEFAULT 0,"
        "has_embedding INTEGER DEFAULT 0,UNIQUE(watch_dir_id,filename));"
        "CREATE TABLE llm_usage("
        "id INTEGER PRIMARY KEY AUTOINCREMENT,ts TEXT,step TEXT,model TEXT,"
        "prompt INTEGER DEFAULT 0,completion INTEGER DEFAULT 0,"
        "total INTEGER DEFAULT 0,cost_usd REAL DEFAULT 0,price_source TEXT);"
        "CREATE TABLE task_graph_generations("
        "tenant_id TEXT NOT NULL,task_scope_id TEXT NOT NULL,"
        "generation_id TEXT NOT NULL,source_revision TEXT NOT NULL,"
        "base_override_seq INTEGER NOT NULL,created_at TEXT NOT NULL,"
        "task_count INTEGER NOT NULL DEFAULT 0,atom_count INTEGER NOT NULL DEFAULT 0,"
        "candidate_count INTEGER NOT NULL DEFAULT 0,"
        "model_judgement_count INTEGER NOT NULL DEFAULT 0,"
        "PRIMARY KEY(tenant_id,task_scope_id));"
        "INSERT INTO watch_dirs(path,label,auto_index,ecosystem)"
        " VALUES('/legacy/path','legacy',1,'manual');"
        "INSERT INTO llm_usage(step,model,prompt,completion,total,cost_usd,price_source)"
        " VALUES('split','legacy-model',10,2,12,0.1,'static');"
        "INSERT INTO task_graph_generations("
        "tenant_id,task_scope_id,generation_id,source_revision,base_override_seq,"
        "created_at) VALUES('ten_legacy','tsc_legacy','gen_legacy','rev_legacy',0,"
        "'2026-01-01T00:00:00Z');"
    )
    connection.commit()
    connection.close()
    migrated = get_connection(db_path)
    watch_dir = migrated.execute(
        "SELECT source_scope_id FROM watch_dirs",
    ).fetchone()
    usage = migrated.execute(
        "SELECT usage_event_id,usage_plane,measurement_quality,tenant_id,"
        "total,cost_usd,legacy FROM llm_usage",
    ).fetchone()
    generation = migrated.execute(
        "SELECT generator_json FROM task_graph_generations",
    ).fetchone()
    migrated.close()
    assert watch_dir["source_scope_id"].startswith("src_")
    assert usage["usage_event_id"].startswith("xsp_legacy_")
    assert usage["usage_plane"] == "xskill_processing"
    assert usage["measurement_quality"] is None
    assert usage["tenant_id"] is None
    assert usage["total"] == 12
    assert usage["cost_usd"] == pytest.approx(0.1)
    assert usage["legacy"] == 1
    assert generation["generator_json"] == "{}"


def test_structured_harness_success_finishes_attempt_without_verifying_task(tmp_path):
    db_path = tmp_path / "registry.db"
    source = _add_source(
        tmp_path,
        db_path,
        source_name="outcome",
        atoms=[{"intent": "完成发布", "summary": "生成发布产物"}],
        final_status="succeeded",
    )
    service = _build(tmp_path, db_path, [source])
    tenant_id = service.resolver.tenant_id
    task = list_logical_tasks(tenant_id, db_path=db_path)[0]
    assert task["lifecycle"] == "open"
    assert task["outcome"] == "unknown"
    detail = get_logical_task(
        tenant_id, task["task_scope_id"], task["task_id"], db_path=db_path,
    )
    assert detail["attempts"][0]["lifecycle"] == "finished"
    assert detail["attempts"][0]["outcome"] == "succeeded"
    assert detail["attempts"][0]["verification"] == "unverified"
    metadata_path = tmp_path / "trajectories" / "traj_outcome.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata.pop("final_status")
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    service.mark_dirty(*source, reason="outcome_removed")
    service.process_dirty()
    task = list_logical_tasks(tenant_id, db_path=db_path)[0]
    assert task["lifecycle"] == "open"
    assert task["outcome"] == "unknown"
    detail = get_logical_task(
        tenant_id, task["task_scope_id"], task["task_id"], db_path=db_path,
    )
    assert detail["attempts"][0]["lifecycle"] == "running"


def test_human_split_task_and_attempt_relation_overrides_are_replayable(tmp_path):
    db_path = tmp_path / "registry.db"
    source = _add_source(
        tmp_path,
        db_path,
        source_name="manual-graph",
        atoms=[
            {"intent": "修复支付失败", "summary": "定位支付错误"},
            {"intent": "重试修复支付失败", "summary": "再次执行支付修复"},
        ],
    )
    service = _build(tmp_path, db_path, [source])
    tenant_id = service.resolver.tenant_id
    task = list_logical_tasks(tenant_id, db_path=db_path)[0]
    current = service.store_for_scope(task["task_scope_id"]).load_current()
    relation = current.attempt_relations[0]
    service.append_override(
        task_scope_id=task["task_scope_id"],
        operation="reject_attempt_relation",
        target_id=relation.relation_id,
        payload={},
        actor="reviewer",
    )
    service.append_override(
        task_scope_id=task["task_scope_id"],
        operation="upsert_attempt_relation",
        target_id=relation.from_attempt_id,
        payload={
            "to_attempt_id": relation.to_attempt_id,
            "relation_type": "supersedes",
        },
        actor="reviewer",
    )
    current = service.store_for_scope(task["task_scope_id"]).load_current()
    supersedes = next(
        item for item in current.attempt_relations
        if item.relation_type == "supersedes"
    )
    service.append_override(
        task_scope_id=task["task_scope_id"],
        operation="reject_attempt_relation",
        target_id=supersedes.relation_id,
        payload={},
        actor="reviewer",
    )
    current = service.store_for_scope(task["task_scope_id"]).load_current()
    atom_keys = sorted(
        stable_ref_key(membership.atom_ref)
        for membership in current.memberships
        if membership.role == "primary"
        and membership.decision == "confirmed"
        and not membership.stale
    )
    service.append_override(
        task_scope_id=task["task_scope_id"],
        operation="split_task",
        target_id=task["task_id"],
        payload={
            "atom_keys": [atom_keys[-1]],
            "title": "单独处理第二次支付修复",
        },
        actor="reviewer",
    )
    current = service.store_for_scope(task["task_scope_id"]).load_current()
    active_tasks = [item for item in current.tasks if not item.tombstoned]
    assert len(active_tasks) == 2
    confirmed = [
        membership for membership in current.memberships
        if membership.role == "primary"
        and membership.decision == "confirmed"
        and not membership.stale
    ]
    assert len(confirmed) == 2
    assert len({membership.task_id for membership in confirmed}) == 2
    assert service.store_for_scope(task["task_scope_id"]).override_watermark() == 4


def test_invalid_metadata_stays_in_durable_dirty_queue(tmp_path):
    db_path = tmp_path / "registry.db"
    source = _add_source(
        tmp_path,
        db_path,
        source_name="invalid-metadata",
        atoms=[{"intent": "检查元数据", "summary": "不吞损坏输入"}],
    )
    service = TaskGraphService(
        state_root=tmp_path,
        db_path=db_path,
        config={"task_graph": {"enabled": True}},
    )
    (tmp_path / "trajectories" / "traj_invalid-metadata.json").write_text(
        "{invalid", encoding="utf-8",
    )
    service.mark_dirty(*source, reason="invalid_metadata")
    result = service.process_dirty()
    assert result["sources"] == 0
    assert len(list_dirty_sources(db_path=db_path)) == 1


def test_changed_usage_event_does_not_publish_a_conflicting_generation(tmp_path):
    db_path = tmp_path / "registry.db"
    source = _add_source(
        tmp_path,
        db_path,
        source_name="immutable-usage",
        atoms=[{"intent": "统计成本", "summary": "保存原始用量"}],
        usage={"input_tokens": 10, "output_tokens": 2, "total_tokens": 12},
    )
    service = _build(tmp_path, db_path, [source])
    tenant_id = service.resolver.tenant_id
    task = list_logical_tasks(tenant_id, db_path=db_path)[0]
    store = service.store_for_scope(task["task_scope_id"])
    generation_id = store.load_current().generation_id
    metadata_path = tmp_path / "trajectories" / "traj_immutable-usage.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["execution_usage_events"][0]["usage"]["total_tokens"] = 13
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    service.mark_dirty(*source, reason="conflicting_usage")
    result = service.process_dirty()
    assert result["failed_scopes"] == [task["task_scope_id"]]
    assert store.load_current().generation_id == generation_id
    assert len(list_dirty_sources(db_path=db_path)) == 1


def test_session_append_without_usage_reuses_the_unavailable_fact(tmp_path):
    db_path = tmp_path / "registry.db"
    source = _add_source(
        tmp_path,
        db_path,
        source_name="append-without-usage",
        atoms=[{"intent": "持续处理任务", "summary": "等待后续消息"}],
    )
    service = _build(tmp_path, db_path, [source])
    trajectory_path = tmp_path / "trajectories" / "traj_append-without-usage.md"
    trajectory_path.write_text(
        trajectory_path.read_text(encoding="utf-8") + "\n追加一轮消息\n",
        encoding="utf-8",
    )

    service.mark_dirty(*source, reason="session_appended")
    result = service.process_dirty()

    assert result["failed_scopes"] == []
    assert result["sources"] == 1
    assert list_dirty_sources(db_path=db_path) == []
    with pooled_connection(db_path) as connection:
        events = connection.execute(
            "SELECT observed_at,measurement_quality"
            " FROM execution_usage_events",
        ).fetchall()
    assert [(row["observed_at"], row["measurement_quality"]) for row in events] == [
        ("unavailable", "unavailable")
    ]


def test_atom_content_skill_and_score_updates_do_not_change_task_identity(tmp_path):
    db_path = tmp_path / "registry.db"
    source = _add_source(
        tmp_path,
        db_path,
        source_name="orthogonal",
        atoms=[{"intent": "优化缓存", "summary": "降低缓存延迟"}],
    )
    service = _build(tmp_path, db_path, [source])
    tenant_id = service.resolver.tenant_id
    first_task = list_logical_tasks(tenant_id, db_path=db_path)[0]
    scope_store = service.store_for_scope(first_task["task_scope_id"])
    first_generation = scope_store.load_current().generation_id
    atom_store = AtomTaskStore(tmp_path / "trajectories")
    atom = atom_store.list_by_traj("traj_orthogonal")[0]
    atom.used_skills = ["cache-analysis", "profiling"]
    atom.ux_score = 9
    atom.clustered = True
    atom.summary = "降低缓存延迟并减少重复读取"
    atom_store.save(atom)
    service.mark_dirty(*source, reason="skill_metadata_changed")
    service.process_dirty()
    second_task = list_logical_tasks(tenant_id, db_path=db_path)[0]
    assert second_task["task_id"] == first_task["task_id"]
    assert scope_store.load_current().generation_id != first_generation


def test_retry_inside_one_atom_creates_line_scoped_attempts(tmp_path):
    db_path = tmp_path / "registry.db"
    source = _add_source(
        tmp_path,
        db_path,
        source_name="intra-atom-retry",
        atoms=[{"intent": "修复登录问题", "summary": "执行并重试修复"}],
        usage={"input_tokens": 80, "output_tokens": 20, "total_tokens": 100},
        final_status="succeeded",
    )
    raw_segment = (
        "## User\n修复登录问题\n\n"
        "## Assistant\n第一次执行失败\n\n"
        "## User\n重试修复登录问题\n\n"
        "## Assistant\n第二次执行成功\n"
    )
    atom_store = AtomTaskStore(tmp_path / "trajectories")
    atom = atom_store.list_by_traj("traj_intra-atom-retry")[0]
    atom.raw_segment = raw_segment
    atom.offset_start = 1
    atom.offset_end = len(raw_segment.splitlines()) + 1
    atom_store.save(atom)
    service = _build(tmp_path, db_path, [source])
    tenant_id = service.resolver.tenant_id
    task = list_logical_tasks(tenant_id, db_path=db_path)[0]
    detail = get_logical_task(
        tenant_id, task["task_scope_id"], task["task_id"], db_path=db_path,
    )
    assert len(detail["attempts"]) == 2
    assert len(detail["evidence_ranges"]) == 2
    assert detail["evidence_ranges"][0]["locator_end"] == detail["evidence_ranges"][1]["locator_start"]
    assert detail["attempt_relations"][0]["relation_type"] == "retry_of"
    assert sum(
        allocation["total_tokens"]
        for allocation in detail["usage_allocations"]
    ) == 100
    assert sum(
        allocation["fraction"]
        for allocation in detail["usage_allocations"]
    ) == pytest.approx(1.0)


@pytest.mark.parametrize(
    "correction_text",
    (
        "不对，根因是缺少数据库索引，请回退超时改动",
        "错了，请回退刚才的修改并添加数据库索引",
        "刚才的方向有误，请改为添加数据库索引",
    ),
)
def test_correction_inside_one_atom_creates_line_scoped_attempts(
    tmp_path, correction_text,
):
    db_path = tmp_path / "registry.db"
    source = _add_source(
        tmp_path,
        db_path,
        source_name="intra-atom-correction",
        atoms=[{"intent": "修复登录问题", "summary": "纠正方案后完成修复"}],
    )
    raw_segment = (
        "## User\n修复登录问题\n\n"
        "## Assistant\n把请求超时改成 30 秒\n\n"
        f"## User\n{correction_text}\n\n"
        "## Assistant\n已回退并添加索引\n"
    )
    atom_store = AtomTaskStore(tmp_path / "trajectories")
    atom = atom_store.list_by_traj("traj_intra-atom-correction")[0]
    atom.raw_segment = raw_segment
    atom.offset_start = 1
    atom.offset_end = len(raw_segment.splitlines()) + 1
    atom_store.save(atom)

    service = _build(tmp_path, db_path, [source])
    tenant_id = service.resolver.tenant_id
    task = list_logical_tasks(tenant_id, db_path=db_path)[0]
    detail = get_logical_task(
        tenant_id, task["task_scope_id"], task["task_id"], db_path=db_path,
    )

    assert len(detail["attempts"]) == 2
    assert len(detail["evidence_ranges"]) == 2
    assert len(detail["attempt_relations"]) == 1
    assert detail["attempt_relations"][0]["relation_type"] == "correction_of"
    assert detail["attempt_relations"][0]["decision"] == "confirmed"


@pytest.mark.parametrize(
    "ordinary_text",
    (
        "使用不对称加密保护登录凭据",
        "检查这个答案对不对",
        "如果结果不对，请保留诊断日志",
    ),
)
def test_non_correction_phrases_do_not_split_attempts(tmp_path, ordinary_text):
    db_path = tmp_path / "registry.db"
    source = _add_source(
        tmp_path,
        db_path,
        source_name="not-a-correction",
        atoms=[{"intent": "完善登录安全", "summary": "检查安全方案"}],
    )
    raw_segment = (
        "## User\n完善登录安全\n\n"
        "## Assistant\n开始检查\n\n"
        f"## User\n{ordinary_text}\n\n"
        "## Assistant\n检查完成\n"
    )
    atom_store = AtomTaskStore(tmp_path / "trajectories")
    atom = atom_store.list_by_traj("traj_not-a-correction")[0]
    atom.raw_segment = raw_segment
    atom.offset_start = 1
    atom.offset_end = len(raw_segment.splitlines()) + 1
    atom_store.save(atom)

    service = _build(tmp_path, db_path, [source])
    tenant_id = service.resolver.tenant_id
    task = list_logical_tasks(tenant_id, db_path=db_path)[0]
    detail = get_logical_task(
        tenant_id, task["task_scope_id"], task["task_id"], db_path=db_path,
    )

    assert len(detail["attempts"]) == 1
    assert detail["attempt_relations"] == []


def test_failed_attempt_does_not_close_logical_task(tmp_path):
    db_path = tmp_path / "registry.db"
    source = _add_source(
        tmp_path,
        db_path,
        source_name="attempt-failed",
        atoms=[{"intent": "修复支付问题", "summary": "执行修复"}],
        final_status="failed",
    )
    service = _build(tmp_path, db_path, [source])
    tenant_id = service.resolver.tenant_id
    task = list_logical_tasks(tenant_id, db_path=db_path)[0]
    assert task["lifecycle"] == "open"
    assert task["outcome"] == "unknown"
    detail = get_logical_task(
        tenant_id, task["task_scope_id"], task["task_id"], db_path=db_path,
    )
    assert detail["attempts"][0]["lifecycle"] == "finished"
    assert detail["attempts"][0]["outcome"] == "failed"


def test_standalone_actor_can_link_same_workspace_across_source_scopes(tmp_path):
    db_path = tmp_path / "registry.db"
    first = _add_source(
        tmp_path / "codex",
        db_path,
        source_name="cross-source-a",
        atoms=[{"intent": "实现任务图", "summary": "开始任务图实现"}],
        workspace="/workspace/shared",
    )
    second = _add_source(
        tmp_path / "openclaw",
        db_path,
        source_name="cross-source-b",
        atoms=[{"intent": "继续实现任务图", "summary": "补全任务图实现"}],
        workspace="/workspace/shared",
    )
    service = _build(tmp_path, db_path, [first, second])
    tenant_id = service.resolver.tenant_id
    tasks = list_logical_tasks(tenant_id, db_path=db_path)
    assert len({task["task_scope_id"] for task in tasks}) == 1
    with pooled_connection(db_path) as connection:
        source_scopes = connection.execute(
            "SELECT DISTINCT source_scope_id FROM task_atom_memberships",
        ).fetchall()
    assert len(source_scopes) == 2


def test_workspace_change_moves_source_between_task_scopes(tmp_path):
    db_path = tmp_path / "registry.db"
    source = _add_source(
        tmp_path,
        db_path,
        source_name="workspace-move",
        atoms=[{"intent": "修复部署", "summary": "修复发布流程"}],
        workspace="/workspace/first",
    )
    service = _build(tmp_path, db_path, [source])
    tenant_id = service.resolver.tenant_id
    first_task = list_logical_tasks(tenant_id, db_path=db_path)[0]
    metadata_path = tmp_path / "trajectories" / "traj_workspace-move.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["cwd"] = "/workspace/second"
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    service.mark_dirty(*source, reason="workspace_changed")
    result = service.process_dirty()
    assert result["failed_scopes"] == []
    assert result["sources"] == 1
    assert list_dirty_sources(db_path=db_path) == []
    live_tasks = list_logical_tasks(tenant_id, db_path=db_path)
    assert len(live_tasks) == 1
    assert live_tasks[0]["task_scope_id"] != first_task["task_scope_id"]
    assert len(list_task_scopes(tenant_id, db_path=db_path)) == 2


def test_disabled_runtime_keeps_dirty_fence_for_later_enable(tmp_path):
    db_path = tmp_path / "registry.db"
    source = _add_source(
        tmp_path,
        db_path,
        source_name="disabled-dirty-fence",
        atoms=[{"intent": "保留变更", "summary": "等待后续启用"}],
    )
    enabled_service = _build(tmp_path, db_path, [source])
    tenant_id = enabled_service.resolver.tenant_id
    first_task = list_logical_tasks(tenant_id, db_path=db_path)[0]
    service = TaskGraphService(
        state_root=tmp_path,
        db_path=db_path,
        config={"task_graph": {"enabled": False}},
    )
    service.mark_dirty(*source, reason="changed_while_disabled")
    assert service.process_dirty() == {
        "enabled": False,
        "sources": 0,
        "scopes": 0,
    }
    assert len(list_dirty_sources(db_path=db_path)) == 1
    assert first_task["task_scope_id"]


def test_disabled_runtime_does_not_queue_never_projected_sources(tmp_path):
    db_path = tmp_path / "registry.db"
    source = _add_source(
        tmp_path,
        db_path,
        source_name="disabled-untracked",
        atoms=[{"intent": "暂不启用", "summary": "等待首次回填"}],
    )
    service = TaskGraphService(
        state_root=tmp_path,
        db_path=db_path,
        config={"task_graph": {"enabled": False}},
    )
    service.mark_dirty(*source, reason="changed_while_never_enabled")
    assert list_dirty_sources(db_path=db_path) == []


def test_shared_usage_allocation_edges_have_a_hard_bound(tmp_path):
    db_path = tmp_path / "registry.db"
    atoms = [
        {"intent": f"处理独立目标 {index}", "summary": f"目标 {index} 的执行"}
        for index in range(65)
    ]
    source = _add_source(
        tmp_path,
        db_path,
        source_name="usage-edge-bound",
        atoms=atoms,
    )
    metadata_path = tmp_path / "trajectories" / "traj_usage-edge-bound.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["execution_usage_events"] = [
        {
            "source_event_id": f"usage-edge-{index}",
            "usage": {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12},
        }
        for index in range(65)
    ]
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    _build(tmp_path, db_path, [source])
    with pooled_connection(db_path) as connection:
        allocations = connection.execute(
            "SELECT allocation_mode,method,fraction,total_tokens"
            " FROM task_usage_allocations",
        ).fetchall()
    assert len(allocations) == 65
    assert {row["allocation_mode"] for row in allocations} == {"unattributed"}
    assert {row["method"] for row in allocations} == {
        "shared_allocation_edge_limit_exceeded"
    }
    assert all(row["fraction"] == 1.0 for row in allocations)
    assert sum(row["total_tokens"] for row in allocations) == 65 * 12
