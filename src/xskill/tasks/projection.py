"""Registry queue, Task Graph projection and bounded query functions."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from pathlib import Path

from xskill.pipeline.registry import pooled_connection
from xskill.tasks.evidence import ExecutionUsageEvent, ScopedTrajectoryEvidence
from xskill.tasks.evidence_bundle import (
    TaskEvidenceBundleError,
    TaskEvidenceBundleIndex,
)
from xskill.tasks.models import TaskGraphGeneration

DEFAULT_BACKFILL_BATCH_SIZE = 512


def _json(value) -> str:
    return json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), allow_nan=False,
    )


def _generator_json(value: dict) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def mark_task_graph_dirty(
    watch_dir_id: int,
    filename: str,
    *,
    reason: str,
    deleted: bool = False,
    tracked_only: bool = False,
    db_path: Path | None = None,
) -> None:
    """Durably mark one source; generation prevents lost concurrent updates."""
    with pooled_connection(db_path) as connection:
        watch_dir = connection.execute(
            "SELECT source_scope_id FROM watch_dirs WHERE id=?",
            (watch_dir_id,),
        ).fetchone()
        traj_id = filename.removesuffix(".md")
        state = None
        if watch_dir is not None and watch_dir["source_scope_id"]:
            state = connection.execute(
                "SELECT tenant_id, task_scope_id FROM task_graph_source_state"
                " WHERE source_scope_id=? AND traj_id=?",
                (watch_dir["source_scope_id"], traj_id),
            ).fetchone()
        if tracked_only and state is None:
            return
        connection.execute(
            "INSERT INTO task_graph_dirty_sources("
            "watch_dir_id,filename,source_scope_id,tenant_id,task_scope_id,"
            "deleted,generation,reason,marked_at)"
            " VALUES(?,?,?,?,?,?,1,?,datetime('now'))"
            " ON CONFLICT(watch_dir_id,filename) DO UPDATE SET"
            " source_scope_id=COALESCE(excluded.source_scope_id,source_scope_id),"
            " tenant_id=COALESCE(excluded.tenant_id,tenant_id),"
            " task_scope_id=COALESCE(excluded.task_scope_id,task_scope_id),"
            " deleted=excluded.deleted,generation=generation+1,"
            " reason=excluded.reason,marked_at=datetime('now')",
            (
                watch_dir_id, filename,
                watch_dir["source_scope_id"] if watch_dir is not None else None,
                state["tenant_id"] if state is not None else None,
                state["task_scope_id"] if state is not None else None,
                int(deleted), reason,
            ),
        )
        connection.commit()


def enqueue_untracked_sources(
    *,
    batch_size: int = DEFAULT_BACKFILL_BATCH_SIZE,
    db_path: Path | None = None,
) -> int:
    """Backfill existing Atom sources in bounded, independently committed batches."""
    if (
        isinstance(batch_size, bool)
        or not isinstance(batch_size, int)
        or batch_size <= 0
    ):
        raise ValueError("batch_size must be a positive integer")
    with pooled_connection(db_path) as connection:
        queued = 0
        last_trajectory_id = 0
        while True:
            rows = connection.execute(
                "SELECT t.id,t.watch_dir_id,t.filename,w.source_scope_id"
                " FROM trajectories t JOIN watch_dirs w ON w.id=t.watch_dir_id"
                " LEFT JOIN task_graph_source_state s"
                " ON s.source_scope_id=w.source_scope_id"
                " AND s.traj_id=CASE WHEN substr(t.filename,-3)='.md'"
                " THEN substr(t.filename,1,length(t.filename)-3) ELSE t.filename END"
                " LEFT JOIN task_graph_dirty_sources d"
                " ON d.watch_dir_id=t.watch_dir_id AND d.filename=t.filename"
                " WHERE t.id>?"
                " AND (t.status IN ('split_done','indexed','done')"
                " OR COALESCE(t.tasks_extracted,0)>0)"
                " AND s.source_scope_id IS NULL AND d.watch_dir_id IS NULL"
                " ORDER BY t.id LIMIT ?",
                (last_trajectory_id, batch_size),
            ).fetchall()
            if not rows:
                break
            last_trajectory_id = int(rows[-1]["id"])
            changes_before = connection.total_changes
            connection.executemany(
                "INSERT INTO task_graph_dirty_sources("
                "watch_dir_id,filename,source_scope_id,deleted,generation,reason,marked_at)"
                " VALUES(?,?,?,0,1,'initial_backfill',datetime('now'))"
                " ON CONFLICT(watch_dir_id,filename) DO NOTHING",
                [
                    (row["watch_dir_id"], row["filename"], row["source_scope_id"])
                    for row in rows
                ],
            )
            queued += connection.total_changes - changes_before
            connection.commit()
        return queued


def enqueue_changed_generator_scopes(
    tenant_id: str,
    generator: dict,
    *,
    db_path: Path | None = None,
) -> int:
    """Queue projected scopes whose deterministic generator has changed."""
    if not isinstance(tenant_id, str) or not tenant_id.strip():
        raise ValueError("tenant_id must be a non-empty string")
    generator_json = _generator_json(generator)
    with pooled_connection(db_path) as connection:
        cursor = connection.execute(
            "INSERT INTO task_graph_dirty_scopes("
            "tenant_id,task_scope_id,generation,reason,marked_at)"
            " SELECT tenant_id,task_scope_id,1,'generator_changed',datetime('now')"
            " FROM task_graph_generations"
            " WHERE tenant_id=? AND generator_json<>?"
            " ON CONFLICT(tenant_id,task_scope_id) DO NOTHING",
            (tenant_id, generator_json),
        )
        connection.commit()
        return max(0, cursor.rowcount)


def list_dirty_sources(
    *, limit: int = 128, db_path: Path | None = None,
) -> list[dict]:
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        raise ValueError("limit must be a positive integer")
    with pooled_connection(db_path) as connection:
        return [dict(row) for row in connection.execute(
            "SELECT * FROM task_graph_dirty_sources"
            " ORDER BY marked_at,watch_dir_id,filename LIMIT ?",
            (limit,),
        ).fetchall()]


def acknowledge_dirty_sources(
    rows: Iterable[dict], *, db_path: Path | None = None,
) -> None:
    with pooled_connection(db_path) as connection:
        connection.executemany(
            "DELETE FROM task_graph_dirty_sources"
            " WHERE watch_dir_id=? AND filename=? AND generation=?",
            [
                (row["watch_dir_id"], row["filename"], row["generation"])
                for row in rows
            ],
        )
        connection.commit()


def mark_task_graph_scope_dirty(
    tenant_id: str,
    task_scope_id: str,
    *,
    reason: str,
    db_path: Path | None = None,
) -> dict:
    """Fence a TaskScope mutation independently of live source rows."""
    for field_name, value in (
        ("tenant_id", tenant_id),
        ("task_scope_id", task_scope_id),
        ("reason", reason),
    ):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{field_name} must be a non-empty string")
    with pooled_connection(db_path) as connection:
        connection.execute(
            "INSERT INTO task_graph_dirty_scopes("
            "tenant_id,task_scope_id,generation,reason,marked_at)"
            " VALUES(?,?,1,?,datetime('now'))"
            " ON CONFLICT(tenant_id,task_scope_id) DO UPDATE SET"
            " generation=generation+1,reason=excluded.reason,"
            " marked_at=datetime('now')",
            (tenant_id, task_scope_id, reason),
        )
        row = connection.execute(
            "SELECT * FROM task_graph_dirty_scopes"
            " WHERE tenant_id=? AND task_scope_id=?",
            (tenant_id, task_scope_id),
        ).fetchone()
        connection.commit()
    return dict(row)


def list_dirty_task_scopes(
    *, limit: int = 128, db_path: Path | None = None,
) -> list[dict]:
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        raise ValueError("limit must be a positive integer")
    with pooled_connection(db_path) as connection:
        return [dict(row) for row in connection.execute(
            "SELECT * FROM task_graph_dirty_scopes"
            " ORDER BY marked_at,tenant_id,task_scope_id LIMIT ?",
            (limit,),
        ).fetchall()]


def acknowledge_dirty_task_scopes(
    rows: Iterable[dict], *, db_path: Path | None = None,
) -> None:
    with pooled_connection(db_path) as connection:
        connection.executemany(
            "DELETE FROM task_graph_dirty_scopes"
            " WHERE tenant_id=? AND task_scope_id=? AND generation=?",
            [
                (row["tenant_id"], row["task_scope_id"], row["generation"])
                for row in rows
            ],
        )
        connection.commit()


def source_states_for_scope(
    tenant_id: str, task_scope_id: str, *, db_path: Path | None = None,
) -> list[dict]:
    with pooled_connection(db_path) as connection:
        return [dict(row) for row in connection.execute(
            "SELECT * FROM task_graph_source_state"
            " WHERE tenant_id=? AND task_scope_id=?"
            " ORDER BY source_scope_id,traj_id",
            (tenant_id, task_scope_id),
        ).fetchall()]


def live_source_row(
    watch_dir_id: int, filename: str, *, db_path: Path | None = None,
) -> tuple[dict, dict] | None:
    with pooled_connection(db_path) as connection:
        row = connection.execute(
            "SELECT t.*,w.path,w.label,w.auto_index,w.ecosystem,w.source_scope_id"
            " FROM trajectories t JOIN watch_dirs w ON w.id=t.watch_dir_id"
            " WHERE t.watch_dir_id=? AND t.filename=?",
            (watch_dir_id, filename),
        ).fetchone()
        if row is None:
            return None
        combined = dict(row)
        watch_dir = {
            key: combined[key]
            for key in ("watch_dir_id", "path", "label", "auto_index", "ecosystem", "source_scope_id")
        }
        watch_dir["id"] = watch_dir.pop("watch_dir_id")
        return watch_dir, combined


def upsert_execution_usage_events(
    events: Iterable[ExecutionUsageEvent], *, db_path: Path | None = None,
) -> int:
    events_by_id: dict[str, ExecutionUsageEvent] = {}
    for event in events:
        previous = events_by_id.get(event.usage_event_id)
        if previous is not None and previous.to_record() != event.to_record():
            raise ValueError(
                f"execution usage event identity collision: {event.usage_event_id}"
            )
        events_by_id[event.usage_event_id] = event
    if not events_by_id:
        return 0
    with pooled_connection(db_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        existing_by_id: dict[str, dict] = {}
        event_ids = sorted(events_by_id)
        for offset in range(0, len(event_ids), 500):
            chunk = event_ids[offset:offset + 500]
            placeholders = ",".join("?" for _ in chunk)
            for row in connection.execute(
                "SELECT * FROM execution_usage_events"
                f" WHERE usage_event_id IN ({placeholders})",
                chunk,
            ).fetchall():
                existing_by_id[row["usage_event_id"]] = dict(row)
        inserted = 0
        rows = []
        for event_id, event in events_by_id.items():
            existing = existing_by_id.get(event_id)
            if existing is not None:
                expected = event.to_record()
                actual = {
                    "usage_event_id": existing["usage_event_id"],
                    "usage_plane": existing["usage_plane"],
                    "source_event_id": existing["source_event_id"],
                    "tenant_id": existing["tenant_id"],
                    "task_scope_id": existing["task_scope_id"],
                    "source_scope_id": existing["source_scope_id"],
                    "traj_id": existing["traj_id"],
                    "model": json.loads(existing["model_json"]),
                    "harness": json.loads(existing["harness_json"]),
                    "prompt_tokens": existing["prompt_tokens"],
                    "completion_tokens": existing["completion_tokens"],
                    "total_tokens": existing["total_tokens"],
                    "cache_read_tokens": existing["cache_read_tokens"],
                    "cost_usd": existing["cost_usd"],
                    "measurement_quality": existing["measurement_quality"],
                    "estimation_method": existing["estimation_method"],
                    "unavailable_reason": existing["unavailable_reason"],
                }
                expected.pop("observed_at", None)
                if actual != expected:
                    connection.rollback()
                    raise RuntimeError(
                        f"immutable execution usage event changed: {event_id}"
                    )
                continue
            rows.append(event)
            inserted += 1
        connection.executemany(
            "INSERT INTO execution_usage_events("
            "usage_event_id,usage_plane,source_event_id,tenant_id,task_scope_id,"
            "source_scope_id,traj_id,model_json,harness_json,prompt_tokens,"
            "completion_tokens,total_tokens,cache_read_tokens,cost_usd,"
            "measurement_quality,estimation_method,unavailable_reason,observed_at)"
            " VALUES(?,'execution',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(usage_event_id) DO NOTHING",
            [
                (
                    event.usage_event_id, event.source_event_id,
                    event.session_ref.tenant_id, event.session_ref.task_scope_id,
                    event.session_ref.source_scope_id, event.session_ref.traj_id,
                    _json(event.model), _json(event.harness), event.prompt_tokens,
                    event.completion_tokens, event.total_tokens,
                    event.cache_read_tokens, event.cost_usd,
                    event.measurement_quality, event.estimation_method,
                    event.unavailable_reason,
                    event.observed_at,
                )
                for event in rows
            ],
        )
        connection.commit()
        return inserted


_PROJECTED_TABLES = (
    "task_usage_allocations", "task_attempt_relations", "task_evidence_ranges",
    "task_attempts", "task_relations", "task_atom_memberships", "logical_tasks",
)


def _task_evidence_feed_rows(generation: TaskGraphGeneration) -> list[tuple]:
    """Build one coalesced feed row per Task with a single generation scan."""
    index = TaskEvidenceBundleIndex(generation)
    rows = []
    for task in sorted(generation.tasks, key=lambda item: item.task_id):
        try:
            bundle = index.build(task.task_id)
        except TaskEvidenceBundleError as exc:
            rejection = str(exc)
            payload = {
                "tenant_id": generation.tenant_id,
                "task_scope_id": generation.task_scope_id,
                "task": task.to_dict(),
                "rejection": rejection,
            }
            digest = hashlib.sha256(
                json.dumps(
                    payload,
                    ensure_ascii=False,
                    allow_nan=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            fingerprint = f"sha256:{digest}"
            eligibility = "ineligible"
            reasons = (rejection,)
            status = "rejected"
        else:
            fingerprint = bundle.task_evidence_fingerprint
            eligibility = bundle.learning_eligibility
            reasons = bundle.eligibility_reasons
            status = "pending"
        rows.append(
            (
                generation.tenant_id,
                generation.task_scope_id,
                task.task_id,
                generation.generation_id,
                fingerprint,
                eligibility,
                _json(list(reasons)),
                status,
            )
        )
    return rows


def project_generation(
    generation: TaskGraphGeneration,
    *,
    sources: Iterable[ScopedTrajectoryEvidence],
    db_path: Path | None = None,
) -> None:
    """Replace one effective TaskScope projection in a single transaction."""
    task_primary_counts: dict[str, int] = {}
    for membership in generation.memberships:
        if membership.role == "primary" and membership.decision == "confirmed" and not membership.stale:
            task_primary_counts[membership.task_id] = task_primary_counts.get(membership.task_id, 0) + 1
    task_attempt_counts: dict[str, int] = {}
    for attempt in generation.attempts:
        task_attempt_counts[attempt.task_id] = task_attempt_counts.get(attempt.task_id, 0) + 1
    task_tokens: dict[str, int] = {}
    task_costs: dict[str, float] = {}
    attempt_tokens: dict[str, int] = {}
    attempt_costs: dict[str, float] = {}
    task_has_tokens: set[str] = set()
    task_has_cost: set[str] = set()
    attempt_has_tokens: set[str] = set()
    attempt_has_cost: set[str] = set()
    for allocation in generation.usage_allocations:
        if allocation.task_id and allocation.total_tokens is not None:
            task_has_tokens.add(allocation.task_id)
            task_tokens[allocation.task_id] = task_tokens.get(allocation.task_id, 0) + allocation.total_tokens
        if allocation.task_id and allocation.cost_usd is not None:
            task_has_cost.add(allocation.task_id)
            task_costs[allocation.task_id] = task_costs.get(allocation.task_id, 0.0) + allocation.cost_usd
        if allocation.attempt_id and allocation.total_tokens is not None:
            attempt_has_tokens.add(allocation.attempt_id)
            attempt_tokens[allocation.attempt_id] = attempt_tokens.get(allocation.attempt_id, 0) + allocation.total_tokens
        if allocation.attempt_id and allocation.cost_usd is not None:
            attempt_has_cost.add(allocation.attempt_id)
            attempt_costs[allocation.attempt_id] = attempt_costs.get(allocation.attempt_id, 0.0) + allocation.cost_usd

    tenant_id = generation.tenant_id
    task_scope_id = generation.task_scope_id
    source_list = tuple(sources)
    evidence_feed_rows = _task_evidence_feed_rows(generation)
    with pooled_connection(db_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            for table_name in _PROJECTED_TABLES:
                connection.execute(
                    f"DELETE FROM {table_name} WHERE tenant_id=? AND task_scope_id=?",
                    (tenant_id, task_scope_id),
                )
            connection.execute(
                "INSERT INTO task_graph_generations("
                "tenant_id,task_scope_id,generation_id,source_revision,generator_json,"
                "base_override_seq,created_at,task_count,atom_count,candidate_count,"
                "model_judgement_count) VALUES(?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(tenant_id,task_scope_id) DO UPDATE SET"
                " generation_id=excluded.generation_id,"
                " source_revision=excluded.source_revision,"
                " generator_json=excluded.generator_json,"
                " base_override_seq=excluded.base_override_seq,"
                " created_at=excluded.created_at,task_count=excluded.task_count,"
                " atom_count=excluded.atom_count,candidate_count=excluded.candidate_count,"
                " model_judgement_count=excluded.model_judgement_count",
                (
                    tenant_id, task_scope_id, generation.generation_id,
                    generation.source_revision,
                    _generator_json(generation.generator),
                    generation.base_override_seq,
                    generation.created_at, generation.metrics.get("task_count", 0),
                    generation.metrics.get("atom_count", 0),
                    generation.metrics.get("candidate_count", 0),
                    generation.metrics.get("model_judgement_count", 0),
                ),
            )
            connection.executemany(
                "INSERT INTO logical_tasks("
                "tenant_id,task_scope_id,task_id,generation_id,title,summary,"
                "lifecycle,outcome,verification,user_disposition,created_at,"
                "tombstoned,aliases_json,decisions_json,primary_atom_count,"
                "attempt_count,execution_tokens,execution_cost_usd)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    (
                        tenant_id, task_scope_id, task.task_id,
                        generation.generation_id, task.title, task.summary,
                        task.lifecycle, task.outcome, task.verification,
                        task.user_disposition, task.created_at, int(task.tombstoned),
                        _json(list(task.aliases)),
                        _json([item.to_dict() for item in task.decisions]),
                        task_primary_counts.get(task.task_id, 0),
                        task_attempt_counts.get(task.task_id, 0),
                        task_tokens.get(task.task_id) if task.task_id in task_has_tokens else None,
                        task_costs.get(task.task_id) if task.task_id in task_has_cost else None,
                    )
                    for task in generation.tasks
                ],
            )
            connection.executemany(
                "INSERT INTO task_atom_memberships("
                "tenant_id,task_scope_id,membership_id,generation_id,task_id,"
                "source_scope_id,traj_id,atom_id,role,decision,confidence,"
                "decided_by,algorithm_version,evidence_refs_json,observed_at,stale)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    (
                        tenant_id, task_scope_id, item.membership_id,
                        generation.generation_id, item.task_id,
                        item.atom_ref.source_scope_id, item.atom_ref.traj_id,
                        item.atom_ref.atom_id, item.role, item.decision,
                        item.confidence, item.decided_by, item.algorithm_version,
                        _json(list(item.evidence_refs)), item.observed_at,
                        int(item.stale),
                    )
                    for item in generation.memberships
                ],
            )
            connection.executemany(
                "INSERT INTO task_relations("
                "tenant_id,task_scope_id,relation_id,generation_id,from_task_id,"
                "to_task_id,relation_type,decision,confidence,decided_by,"
                "algorithm_version,evidence_refs_json,observed_at,stale)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    (
                        tenant_id, task_scope_id, item.relation_id,
                        generation.generation_id, item.from_task_id,
                        item.to_task_id, item.relation_type, item.decision,
                        item.confidence, item.decided_by, item.algorithm_version,
                        _json(list(item.evidence_refs)), item.observed_at,
                        int(item.stale),
                    )
                    for item in generation.relations
                ],
            )
            connection.executemany(
                "INSERT INTO task_attempts("
                "tenant_id,task_scope_id,attempt_id,generation_id,task_id,"
                "started_at,ended_at,lifecycle,outcome,verification,"
                "user_disposition,decisions_json,execution_identity_json,"
                "evidence_count,execution_tokens,execution_cost_usd)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    (
                        tenant_id, task_scope_id, item.attempt_id,
                        generation.generation_id, item.task_id, item.started_at,
                        item.ended_at, item.lifecycle, item.outcome,
                        item.verification, item.user_disposition,
                        _json([decision.to_dict() for decision in item.decisions]),
                        _json(item.execution_identity), len(item.evidence_ranges),
                        attempt_tokens.get(item.attempt_id)
                        if item.attempt_id in attempt_has_tokens else None,
                        attempt_costs.get(item.attempt_id)
                        if item.attempt_id in attempt_has_cost else None,
                    )
                    for item in generation.attempts
                ],
            )
            evidence_rows = []
            for attempt in generation.attempts:
                for evidence in attempt.evidence_ranges:
                    evidence_rows.append((
                        tenant_id, task_scope_id, evidence.evidence_id,
                        generation.generation_id, attempt.attempt_id,
                        evidence.session_ref.source_scope_id,
                        evidence.session_ref.traj_id,
                        evidence.atom_ref.atom_id if evidence.atom_ref else None,
                        evidence.locator_kind, str(evidence.start), str(evidence.end),
                        evidence.content_hash, evidence.atom_hash,
                        int(evidence.stale),
                        _json(evidence.model), _json(evidence.harness),
                        _json(list(evidence.skills)),
                    ))
            connection.executemany(
                "INSERT INTO task_evidence_ranges("
                "tenant_id,task_scope_id,evidence_id,generation_id,attempt_id,"
                "source_scope_id,traj_id,atom_id,locator_kind,locator_start,"
                "locator_end,content_hash,atom_hash,stale,model_json,harness_json,skills_json)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                evidence_rows,
            )
            connection.executemany(
                "INSERT INTO task_attempt_relations("
                "tenant_id,task_scope_id,relation_id,generation_id,from_attempt_id,"
                "to_attempt_id,relation_type,decision,confidence,decided_by,"
                "algorithm_version,evidence_refs_json,observed_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    (
                        tenant_id, task_scope_id, item.relation_id,
                        generation.generation_id, item.from_attempt_id,
                        item.to_attempt_id, item.relation_type, item.decision,
                        item.confidence, item.decided_by, item.algorithm_version,
                        _json(list(item.evidence_refs)), item.observed_at,
                    )
                    for item in generation.attempt_relations
                ],
            )
            connection.executemany(
                "INSERT INTO task_usage_allocations("
                "tenant_id,task_scope_id,allocation_id,generation_id,usage_event_id,"
                "usage_plane,allocation_mode,fraction,task_id,attempt_id,"
                "processing_step,prompt_tokens,completion_tokens,total_tokens,"
                "cache_read_tokens,cost_usd,method,method_version)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    (
                        tenant_id, task_scope_id, item.allocation_id,
                        generation.generation_id, item.usage_event_id,
                        item.usage_plane, item.allocation_mode, item.fraction,
                        item.task_id, item.attempt_id, item.processing_step,
                        item.prompt_tokens, item.completion_tokens,
                        item.total_tokens, item.cache_read_tokens,
                        item.cost_usd, item.method,
                        item.method_version,
                    )
                    for item in generation.usage_allocations
                ],
            )
            connection.execute(
                "DELETE FROM task_graph_source_state"
                " WHERE tenant_id=? AND task_scope_id=?",
                (tenant_id, task_scope_id),
            )
            connection.executemany(
                "INSERT INTO task_graph_source_state("
                "tenant_id,task_scope_id,source_scope_id,watch_dir_id,traj_id,"
                "source_revision,generation_id,updated_at)"
                " VALUES(?,?,?,?,?,?,?,datetime('now'))"
                " ON CONFLICT(source_scope_id,traj_id) DO UPDATE SET"
                " tenant_id=excluded.tenant_id,"
                " task_scope_id=excluded.task_scope_id,"
                " watch_dir_id=excluded.watch_dir_id,"
                " source_revision=excluded.source_revision,"
                " generation_id=excluded.generation_id,"
                " updated_at=datetime('now')",
                [
                    (
                        tenant_id, task_scope_id, source.session_ref.source_scope_id,
                        source.watch_dir_id, source.session_ref.traj_id,
                        source.source_revision, generation.generation_id,
                    )
                    for source in source_list
                ],
            )
            connection.executemany(
                "INSERT INTO task_evidence_feed("
                "tenant_id,task_scope_id,task_id,task_generation_id,"
                "task_evidence_fingerprint,learning_eligibility,"
                "eligibility_reasons_json,status) VALUES(?,?,?,?,?,?,?,?)"
                " ON CONFLICT(tenant_id,task_scope_id,task_id) DO UPDATE SET"
                " task_generation_id=excluded.task_generation_id,"
                " task_evidence_fingerprint=excluded.task_evidence_fingerprint,"
                " learning_eligibility=excluded.learning_eligibility,"
                " eligibility_reasons_json=excluded.eligibility_reasons_json,"
                " status=CASE WHEN task_evidence_feed.task_evidence_fingerprint"
                "<>excluded.task_evidence_fingerprint THEN excluded.status"
                " ELSE task_evidence_feed.status END,"
                " generation=task_evidence_feed.generation+CASE WHEN"
                " task_evidence_feed.task_evidence_fingerprint"
                "<>excluded.task_evidence_fingerprint THEN 1 ELSE 0 END,"
                " marked_at=CASE WHEN task_evidence_feed.task_evidence_fingerprint"
                "<>excluded.task_evidence_fingerprint THEN datetime('now')"
                " ELSE task_evidence_feed.marked_at END,"
                " processed_at=CASE WHEN task_evidence_feed.task_evidence_fingerprint"
                "<>excluded.task_evidence_fingerprint THEN NULL"
                " ELSE task_evidence_feed.processed_at END",
                evidence_feed_rows,
            )
            connection.execute(
                "DELETE FROM task_evidence_feed"
                " WHERE tenant_id=? AND task_scope_id=?"
                " AND NOT EXISTS (SELECT 1 FROM logical_tasks"
                " WHERE logical_tasks.tenant_id=task_evidence_feed.tenant_id"
                " AND logical_tasks.task_scope_id=task_evidence_feed.task_scope_id"
                " AND logical_tasks.task_id=task_evidence_feed.task_id)",
                (tenant_id, task_scope_id),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise


def list_pending_task_evidence(
    *, limit: int = 128, db_path: Path | None = None,
) -> list[dict]:
    """Return pending Task bundles in stable order without loading payloads."""
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        raise ValueError("limit must be a positive integer")
    with pooled_connection(db_path) as connection:
        rows = connection.execute(
            "SELECT * FROM task_evidence_feed WHERE status='pending'"
            " ORDER BY marked_at,tenant_id,task_scope_id,task_id LIMIT ?",
            (limit,),
        ).fetchall()
    return [
        _decode_json_fields(dict(row), ("eligibility_reasons_json",))
        for row in rows
    ]


def acknowledge_task_evidence(
    rows: Iterable[dict], *, db_path: Path | None = None,
) -> int:
    """Acknowledge exactly the observed feed generations."""
    acknowledgements = [
        (
            row["tenant_id"],
            row["task_scope_id"],
            row["task_id"],
            row["generation"],
        )
        for row in rows
    ]
    if not acknowledgements:
        return 0
    with pooled_connection(db_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            cursor = connection.executemany(
                "UPDATE task_evidence_feed SET status='processed',"
                " processed_at=datetime('now')"
                " WHERE tenant_id=? AND task_scope_id=? AND task_id=?"
                " AND generation=? AND status='pending'",
                acknowledgements,
            )
            connection.commit()
            return max(0, cursor.rowcount)
        except Exception:
            connection.rollback()
            raise


def task_evidence_feed_counts(*, db_path: Path | None = None) -> dict[str, int]:
    """Return zero-filled operational counts for the durable feed."""
    counts = {status: 0 for status in ("pending", "processed", "fallback", "rejected")}
    with pooled_connection(db_path) as connection:
        rows = connection.execute(
            "SELECT status,COUNT(*) AS count FROM task_evidence_feed"
            " GROUP BY status"
        ).fetchall()
    counts.update({row["status"]: row["count"] for row in rows})
    return counts


def _decode_json_fields(row: dict, fields: Iterable[str]) -> dict:
    for field_name in fields:
        raw = row.get(field_name)
        if raw is None:
            continue
        try:
            row[field_name.removesuffix("_json")] = json.loads(raw)
        except json.JSONDecodeError:
            row[field_name.removesuffix("_json")] = None
        row.pop(field_name, None)
    return row


def list_task_scopes(
    tenant_id: str, *, limit: int = 100, offset: int = 0,
    db_path: Path | None = None,
) -> list[dict]:
    with pooled_connection(db_path) as connection:
        return [dict(row) for row in connection.execute(
            "SELECT * FROM task_graph_generations WHERE tenant_id=?"
            " ORDER BY created_at DESC,task_scope_id LIMIT ? OFFSET ?",
            (tenant_id, limit, offset),
        ).fetchall()]


def list_logical_tasks(
    tenant_id: str,
    *,
    task_scope_id: str | None = None,
    include_tombstones: bool = False,
    limit: int = 100,
    offset: int = 0,
    db_path: Path | None = None,
) -> list[dict]:
    clauses = ["tenant_id=?"]
    parameters: list = [tenant_id]
    if task_scope_id:
        clauses.append("task_scope_id=?")
        parameters.append(task_scope_id)
    if not include_tombstones:
        clauses.append("tombstoned=0")
    parameters.extend((limit, offset))
    with pooled_connection(db_path) as connection:
        rows = connection.execute(
            "SELECT * FROM logical_tasks WHERE " + " AND ".join(clauses)
            + " ORDER BY created_at DESC,task_id LIMIT ? OFFSET ?",
            parameters,
        ).fetchall()
    return [
        _decode_json_fields(dict(row), ("aliases_json", "decisions_json"))
        for row in rows
    ]


def get_logical_task(
    tenant_id: str, task_scope_id: str, task_id: str,
    *, db_path: Path | None = None,
) -> dict | None:
    with pooled_connection(db_path) as connection:
        task = connection.execute(
            "SELECT * FROM logical_tasks"
            " WHERE tenant_id=? AND task_scope_id=? AND task_id=?",
            (tenant_id, task_scope_id, task_id),
        ).fetchone()
        if task is None:
            return None
        memberships = [dict(row) for row in connection.execute(
            "SELECT * FROM task_atom_memberships"
            " WHERE tenant_id=? AND task_scope_id=? AND task_id=?"
            " ORDER BY observed_at,membership_id",
            (tenant_id, task_scope_id, task_id),
        ).fetchall()]
        attempts = [dict(row) for row in connection.execute(
            "SELECT * FROM task_attempts"
            " WHERE tenant_id=? AND task_scope_id=? AND task_id=?"
            " ORDER BY started_at,attempt_id",
            (tenant_id, task_scope_id, task_id),
        ).fetchall()]
        evidence = [dict(row) for row in connection.execute(
            "SELECT e.* FROM task_evidence_ranges e"
            " JOIN task_attempts a ON a.tenant_id=e.tenant_id"
            " AND a.task_scope_id=e.task_scope_id"
            " AND a.attempt_id=e.attempt_id"
            " WHERE a.tenant_id=? AND a.task_scope_id=? AND a.task_id=?"
            " ORDER BY e.source_scope_id,e.traj_id,"
            " CASE WHEN e.locator_kind='trajectory_line'"
            " THEN CAST(e.locator_start AS INTEGER) END,"
            " e.locator_start,e.evidence_id",
            (tenant_id, task_scope_id, task_id),
        ).fetchall()]
        relations = [dict(row) for row in connection.execute(
            "SELECT * FROM task_relations"
            " WHERE tenant_id=? AND task_scope_id=?"
            " AND (from_task_id=? OR to_task_id=?) ORDER BY relation_id",
            (tenant_id, task_scope_id, task_id, task_id),
        ).fetchall()]
        attempt_relations = [dict(row) for row in connection.execute(
            "SELECT r.* FROM task_attempt_relations r"
            " JOIN task_attempts a ON a.tenant_id=r.tenant_id"
            " AND a.task_scope_id=r.task_scope_id"
            " AND a.attempt_id=r.from_attempt_id"
            " WHERE a.tenant_id=? AND a.task_scope_id=? AND a.task_id=?"
            " ORDER BY r.relation_id",
            (tenant_id, task_scope_id, task_id),
        ).fetchall()]
        allocations = [dict(row) for row in connection.execute(
            "SELECT * FROM task_usage_allocations"
            " WHERE tenant_id=? AND task_scope_id=? AND task_id=?"
            " ORDER BY usage_plane,usage_event_id,allocation_id",
            (tenant_id, task_scope_id, task_id),
        ).fetchall()]
        usage_event_ids = sorted({
            row["usage_event_id"] for row in allocations
            if row["usage_plane"] == "execution"
        })
        usage_events = []
        if usage_event_ids:
            placeholders = ",".join("?" for _ in usage_event_ids)
            usage_events = [dict(row) for row in connection.execute(
                "SELECT * FROM execution_usage_events"
                " WHERE tenant_id=? AND task_scope_id=?"
                f" AND usage_event_id IN ({placeholders})"
                " ORDER BY observed_at,usage_event_id",
                [tenant_id, task_scope_id, *usage_event_ids],
            ).fetchall()]
    return {
        "task": _decode_json_fields(dict(task), ("aliases_json", "decisions_json")),
        "memberships": [
            _decode_json_fields(row, ("evidence_refs_json",)) for row in memberships
        ],
        "attempts": [
            _decode_json_fields(
                row, ("decisions_json", "execution_identity_json"),
            ) for row in attempts
        ],
        "evidence_ranges": [
            _decode_json_fields(
                row, ("model_json", "harness_json", "skills_json"),
            ) for row in evidence
        ],
        "relations": [
            _decode_json_fields(row, ("evidence_refs_json",)) for row in relations
        ],
        "attempt_relations": [
            _decode_json_fields(row, ("evidence_refs_json",))
            for row in attempt_relations
        ],
        "usage_allocations": allocations,
        "usage_events": [
            _decode_json_fields(row, ("model_json", "harness_json"))
            for row in usage_events
        ],
    }


def tasks_for_session(
    tenant_id: str, source_scope_id: str, traj_id: str,
    *, db_path: Path | None = None,
) -> dict:
    with pooled_connection(db_path) as connection:
        memberships = [dict(row) for row in connection.execute(
            "SELECT * FROM task_atom_memberships"
            " WHERE tenant_id=? AND source_scope_id=? AND traj_id=?"
            " ORDER BY observed_at,membership_id",
            (tenant_id, source_scope_id, traj_id),
        ).fetchall()]
        task_rows = connection.execute(
            "SELECT DISTINCT l.* FROM logical_tasks l"
            " JOIN task_atom_memberships m ON m.tenant_id=l.tenant_id"
            " AND m.task_scope_id=l.task_scope_id AND m.task_id=l.task_id"
            " WHERE m.tenant_id=? AND m.source_scope_id=? AND m.traj_id=?"
            " ORDER BY l.created_at,l.task_id",
            (tenant_id, source_scope_id, traj_id),
        ).fetchall()
        tasks = [
            _decode_json_fields(
                dict(row), ("aliases_json", "decisions_json"),
            )
            for row in task_rows
        ]
    return {
        "tasks": tasks,
        "memberships": [
            _decode_json_fields(row, ("evidence_refs_json",))
            for row in memberships
        ],
    }


def tasks_for_atom(
    tenant_id: str, source_scope_id: str, traj_id: str, atom_id: str,
    *, db_path: Path | None = None,
) -> dict:
    with pooled_connection(db_path) as connection:
        memberships = [dict(row) for row in connection.execute(
            "SELECT * FROM task_atom_memberships"
            " WHERE tenant_id=? AND source_scope_id=? AND traj_id=? AND atom_id=?"
            " ORDER BY observed_at,membership_id",
            (tenant_id, source_scope_id, traj_id, atom_id),
        ).fetchall()]
        task_rows = connection.execute(
            "SELECT DISTINCT l.* FROM logical_tasks l"
            " JOIN task_atom_memberships m ON m.tenant_id=l.tenant_id"
            " AND m.task_scope_id=l.task_scope_id AND m.task_id=l.task_id"
            " WHERE m.tenant_id=? AND m.source_scope_id=?"
            " AND m.traj_id=? AND m.atom_id=?"
            " ORDER BY l.created_at,l.task_id",
            (tenant_id, source_scope_id, traj_id, atom_id),
        ).fetchall()
    return {
        "tasks": [
            _decode_json_fields(
                dict(row), ("aliases_json", "decisions_json"),
            )
            for row in task_rows
        ],
        "memberships": [
            _decode_json_fields(row, ("evidence_refs_json",))
            for row in memberships
        ],
    }


def task_graph_overview(
    tenant_id: str, *, db_path: Path | None = None,
) -> dict:
    with pooled_connection(db_path) as connection:
        task_row = connection.execute(
            "SELECT COUNT(*) AS tasks,"
            " SUM(CASE WHEN outcome='unknown' THEN 1 ELSE 0 END) AS unknown_outcomes,"
            " SUM(CASE WHEN tombstoned=0 THEN primary_atom_count ELSE 0 END) AS primary_atoms,"
            " SUM(CASE WHEN tombstoned=0 THEN attempt_count ELSE 0 END) AS attempts,"
            " SUM(CASE WHEN tombstoned=0 THEN execution_tokens ELSE 0 END) AS execution_tokens,"
            " SUM(CASE WHEN tombstoned=0 THEN execution_cost_usd ELSE 0 END) AS execution_cost_usd"
            " FROM logical_tasks WHERE tenant_id=? AND tombstoned=0",
            (tenant_id,),
        ).fetchone()
        uncertain = connection.execute(
            "SELECT COUNT(*) FROM task_atom_memberships"
            " WHERE tenant_id=? AND decision IN ('proposed','needs_review')",
            (tenant_id,),
        ).fetchone()[0]
        scopes = connection.execute(
            "SELECT COUNT(*) FROM task_graph_generations WHERE tenant_id=?",
            (tenant_id,),
        ).fetchone()[0]
    return {
        "scopes": scopes,
        "tasks": task_row["tasks"] or 0,
        "primary_atoms": task_row["primary_atoms"] or 0,
        "attempts": task_row["attempts"] or 0,
        "unknown_outcomes": task_row["unknown_outcomes"] or 0,
        "uncertain_memberships": uncertain,
        "execution_tokens": task_row["execution_tokens"],
        "execution_cost_usd": task_row["execution_cost_usd"],
    }
