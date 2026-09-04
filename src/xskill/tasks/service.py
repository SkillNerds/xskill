"""Production orchestration for dirty-source Task Graph rebuilds."""
from __future__ import annotations

import hashlib
import json
import logging
import uuid
from collections import OrderedDict, deque
from operator import attrgetter
from pathlib import Path

from xskill.tasks.adjudicator import LLMTaskLinkAdjudicator
from xskill.tasks.evidence import (
    ScopedTrajectoryEvidence,
    collect_trajectory_evidence,
)
from xskill.tasks.linker import BoundedTaskLinker
from xskill.tasks.locking import task_file_lock
from xskill.tasks.models import (
    ATTEMPT_LIFECYCLES,
    ATTEMPT_OUTCOMES,
    ATTEMPT_RELATION_TYPES,
    TASK_LIFECYCLES,
    TASK_OUTCOMES,
    TASK_RELATION_TYPES,
    USER_DISPOSITIONS,
    VERIFICATIONS,
    stable_ref_key,
)
from xskill.tasks.projection import (
    acknowledge_dirty_task_scopes,
    acknowledge_dirty_sources,
    enqueue_changed_generator_scopes,
    enqueue_untracked_sources,
    list_dirty_task_scopes,
    list_dirty_sources,
    live_source_row,
    mark_task_graph_scope_dirty,
    mark_task_graph_dirty,
    project_generation,
    source_states_for_scope,
    upsert_execution_usage_events,
)
from xskill.tasks.scopes import ScopeResolver
from xskill.tasks.store import OverrideEvent, TaskGraphStore
from xskill.utils.llm import LLMClient

logger = logging.getLogger("xskill.task_graph")


def _task_relation_edge(
    from_task_id: str, to_task_id: str, relation_type: str,
) -> tuple[str, str]:
    if relation_type == "subtask":
        return to_task_id, from_task_id
    return from_task_id, to_task_id


def _require_acyclic(edges: dict[str, set[str]], message: str) -> None:
    indegree = {task_id: 0 for task_id in edges}
    for targets in edges.values():
        for target_id in targets:
            indegree[target_id] += 1
    ready = deque(
        task_id for task_id, degree in indegree.items() if degree == 0
    )
    visited = 0
    while ready:
        task_id = ready.popleft()
        visited += 1
        for target_id in edges[task_id]:
            indegree[target_id] -= 1
            if indegree[target_id] == 0:
                ready.append(target_id)
    if visited != len(edges):
        raise ValueError(message)


def _scope_revision(sources: list[ScopedTrajectoryEvidence]) -> str:
    records = [
        {
            "tenant_id": source.session_ref.tenant_id,
            "task_scope_id": source.session_ref.task_scope_id,
            "source_scope_id": source.session_ref.source_scope_id,
            "traj_id": source.session_ref.traj_id,
            "source_revision": source.source_revision,
        }
        for source in sorted(
            sources,
            key=attrgetter(
                "session_ref.source_scope_id", "session_ref.traj_id",
            ),
        )
    ]
    payload = json.dumps(
        records, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


class TaskGraphService:
    """Consume durable changes, publish facts, then atomically project queries."""

    def __init__(
        self,
        *,
        state_root: Path,
        db_path: Path | None = None,
        server_mode: bool = False,
        config: dict | None = None,
        usage_ledger=None,
    ):
        self.state_root = Path(state_root).expanduser().resolve()
        self.db_path = Path(db_path) if db_path is not None else None
        self.server_mode = bool(server_mode)
        self.usage_ledger = usage_ledger
        if config is not None and not isinstance(config, dict):
            raise ValueError("Task Graph config must be a mapping")
        self.config = config or {}
        task_config = self.config.get("task_graph") or {}
        if not isinstance(task_config, dict):
            raise ValueError("task_graph config must be a mapping")
        enabled = task_config.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ValueError("task_graph.enabled must be a boolean")
        self.enabled = enabled

        def positive_integer(name: str, default: int) -> int:
            value = task_config.get(name, default)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"task_graph.{name} must be a positive integer")
            return value

        self.max_scopes_per_run = positive_integer("max_scopes_per_run", 4)
        self.source_cache_size = positive_integer("source_cache_size", 128)
        adjudication_config = task_config.get("llm_adjudication")
        if adjudication_config is None:
            adjudication_config = {}
        if not isinstance(adjudication_config, dict):
            raise ValueError("task_graph.llm_adjudication must be a mapping")
        adjudication_enabled = adjudication_config.get("enabled", False)
        auto_confirm = adjudication_config.get("auto_confirm", False)
        for name, value in (
            ("enabled", adjudication_enabled),
            ("auto_confirm", auto_confirm),
        ):
            if not isinstance(value, bool):
                raise ValueError(
                    f"task_graph.llm_adjudication.{name} must be a boolean"
                )
        max_model_judgements = adjudication_config.get(
            "max_judgements_per_build", 64,
        )
        if (
            isinstance(max_model_judgements, bool)
            or not isinstance(max_model_judgements, int)
            or max_model_judgements <= 0
        ):
            raise ValueError(
                "task_graph.llm_adjudication.max_judgements_per_build "
                "must be a positive integer"
            )
        llm_override = adjudication_config.get("llm")
        if llm_override is None:
            llm_override = {}
        if not isinstance(llm_override, dict):
            raise ValueError(
                "task_graph.llm_adjudication.llm must be a mapping"
            )
        adjudicator = None
        if adjudication_enabled:
            base_llm = self.config.get("llm")
            if base_llm is None:
                base_llm = {}
            if not isinstance(base_llm, dict):
                raise ValueError("llm config must be a mapping")
            llm_config = {**base_llm, **llm_override}
            requested_max_tokens = llm_config.get("max_tokens", 800)
            if (
                isinstance(requested_max_tokens, bool)
                or not isinstance(requested_max_tokens, int)
                or requested_max_tokens <= 0
            ):
                raise ValueError(
                    "task_graph.llm_adjudication.llm.max_tokens must be "
                    "a positive integer"
                )
            llm_config["max_tokens"] = min(requested_max_tokens, 800)
            if "temperature" not in llm_override:
                llm_config["temperature"] = 0.0
            adjudicator = LLMTaskLinkAdjudicator(
                LLMClient.from_config(
                    llm_config,
                    usage_ledger=self.usage_ledger,
                ),
                auto_confirm=auto_confirm,
            )
        self._backfill_enqueued = False
        self._evidence_cache: OrderedDict[
            tuple[str, str], ScopedTrajectoryEvidence
        ] = OrderedDict()
        self._stores: OrderedDict[str, TaskGraphStore] = OrderedDict()
        self.resolver = ScopeResolver(
            self.state_root,
            db_path=self.db_path,
            server_mode=self.server_mode,
        )
        self.linker = BoundedTaskLinker(
            top_k=positive_integer("top_k", 8),
            recent_k=positive_integer("recent_k", 6),
            posting_cap=positive_integer("posting_cap", 64),
            adjudicator=adjudicator,
            max_model_judgements_per_build=max_model_judgements,
        )

    def store_for_scope(self, task_scope_id: str) -> TaskGraphStore:
        store = self._stores.get(task_scope_id)
        if store is None:
            store = TaskGraphStore(self.resolver.scope_dir(task_scope_id))
            self._stores[task_scope_id] = store
            while len(self._stores) > 128:
                self._stores.popitem(last=False)
        else:
            self._stores.move_to_end(task_scope_id)
        return store

    def _cache_evidence(self, evidence: ScopedTrajectoryEvidence) -> None:
        key = (
            evidence.session_ref.source_scope_id,
            evidence.session_ref.traj_id,
        )
        self._evidence_cache[key] = evidence
        self._evidence_cache.move_to_end(key)
        while len(self._evidence_cache) > self.source_cache_size:
            self._evidence_cache.popitem(last=False)

    def mark_dirty(
        self, watch_dir_id: int, filename: str, *, reason: str,
        deleted: bool = False,
    ) -> None:
        # Keep the durable source fence while disabled so a later enable does
        # not miss changes to an already-projected trajectory.
        mark_task_graph_dirty(
            watch_dir_id, filename, reason=reason, deleted=deleted,
            tracked_only=not self.enabled,
            db_path=self.db_path,
        )

    def ensure_backfill_enqueued(self) -> int:
        """Queue pre-existing Atom sources exactly once for this worker."""
        if not self.enabled or self._backfill_enqueued:
            return 0
        queued = enqueue_untracked_sources(db_path=self.db_path)
        tenant_id = self.resolver.existing_tenant_id
        if tenant_id is not None:
            queued += enqueue_changed_generator_scopes(
                tenant_id,
                self.linker.generator_descriptor(),
                db_path=self.db_path,
            )
        self._backfill_enqueued = True
        return queued

    def process_dirty(self, *, max_sources: int = 128) -> dict:
        if not self.enabled:
            return {"enabled": False, "sources": 0, "scopes": 0}
        self.ensure_backfill_enqueued()
        dirty_rows = list_dirty_sources(
            limit=max_sources, db_path=self.db_path,
        )
        dirty_scope_rows = list_dirty_task_scopes(
            limit=self.max_scopes_per_run,
            db_path=self.db_path,
        )
        if not dirty_rows and not dirty_scope_rows:
            return {"enabled": True, "sources": 0, "scopes": 0}

        resolved: dict[tuple[int, str], ScopedTrajectoryEvidence | None] = {}
        collection_failed: set[tuple[int, str]] = set()
        old_scopes_by_key: dict[tuple[int, str], tuple[str, str] | None] = {}
        for row in dirty_rows:
            key = (int(row["watch_dir_id"]), str(row["filename"]))
            old_scope = self._old_scope(row)
            old_scopes_by_key[key] = old_scope
            if row.get("deleted"):
                resolved[key] = None
                source_scope_id = str(row.get("source_scope_id") or "")
                filename = str(row["filename"])
                traj_id = filename.removesuffix(".md")
                self._evidence_cache.pop((source_scope_id, traj_id), None)
                continue
            live = live_source_row(*key, db_path=self.db_path)
            if live is None:
                resolved[key] = None
                source_scope_id = str(row.get("source_scope_id") or "")
                filename = str(row["filename"])
                self._evidence_cache.pop(
                    (source_scope_id, filename.removesuffix(".md")), None,
                )
                continue
            watch_dir, trajectory = live
            try:
                scope = self.resolver.resolve(
                    watch_dir=watch_dir, trajectory=trajectory,
                )
                evidence = collect_trajectory_evidence(
                    watch_dir=watch_dir, trajectory=trajectory, scope=scope,
                )
            except FileNotFoundError:
                logger.info(
                    "Task Graph source disappeared before collection: %s/%s",
                    watch_dir.get("path"), row["filename"],
                )
                evidence = None
            except (OSError, RuntimeError, ValueError):
                logger.warning(
                    "Task Graph source collection failed; source remains dirty: %s/%s",
                    watch_dir.get("path"), row["filename"], exc_info=True,
                )
                collection_failed.add(key)
                continue
            resolved[key] = evidence
            if evidence is not None:
                self._cache_evidence(evidence)

        selected_scope_set: set[tuple[str, str]] = {
            (str(row["tenant_id"]), str(row["task_scope_id"]))
            for row in dirty_scope_rows
        }
        for row in dirty_rows:
            key = (int(row["watch_dir_id"]), str(row["filename"]))
            if key in collection_failed:
                continue
            current = resolved.get(key)
            row_scopes = {
                scope for scope in (
                    old_scopes_by_key.get(key),
                    (
                        (current.scope.tenant_id, current.scope.task_scope_id)
                        if current is not None else None
                    ),
                ) if scope is not None
            }
            proposed = selected_scope_set | row_scopes
            if (
                selected_scope_set
                and len(proposed) > self.max_scopes_per_run
            ):
                continue
            selected_scope_set = proposed
        selected_scopes = sorted(selected_scope_set)
        processed_rows: list[dict] = []
        built = []
        failed_scopes = []
        successful_scope_set: set[tuple[str, str]] = set()
        for tenant_id, task_scope_id in selected_scopes:
            try:
                generation, source_count = self._rebuild_scope(
                    tenant_id, task_scope_id, resolved,
                )
            except (OSError, RuntimeError, ValueError):
                logger.exception(
                    "Task Graph scope rebuild failed; durable sources remain dirty: %s",
                    task_scope_id,
                )
                failed_scopes.append(task_scope_id)
                continue
            successful_scope_set.add((tenant_id, task_scope_id))
            built.append({
                "tenant_id": tenant_id,
                "task_scope_id": task_scope_id,
                "generation_id": generation.generation_id,
                "sources": source_count,
                "tasks": generation.metrics.get("task_count", 0),
                "atoms": generation.metrics.get("atom_count", 0),
            })
        for row in dirty_rows:
            key = (int(row["watch_dir_id"]), str(row["filename"]))
            if key in collection_failed:
                continue
            old_scope = old_scopes_by_key.get(key)
            current = resolved.get(key)
            current_scope = (
                (current.scope.tenant_id, current.scope.task_scope_id)
                if current is not None else None
            )
            row_scopes = {scope for scope in (old_scope, current_scope) if scope is not None}
            if not row_scopes or row_scopes.issubset(successful_scope_set):
                processed_rows.append(row)
        acknowledge_dirty_sources(processed_rows, db_path=self.db_path)
        processed_scope_rows = [
            row for row in dirty_scope_rows
            if (str(row["tenant_id"]), str(row["task_scope_id"]))
            in successful_scope_set
        ]
        acknowledge_dirty_task_scopes(
            processed_scope_rows, db_path=self.db_path,
        )
        return {
            "enabled": True,
            "sources": len(processed_rows),
            "scopes": len(built),
            "override_scopes": len(processed_scope_rows),
            "failed_scopes": failed_scopes,
            "generations": built,
        }

    def _old_scope(self, row: dict) -> tuple[str, str] | None:
        tenant_id = str(row.get("tenant_id") or "")
        task_scope_id = str(row.get("task_scope_id") or "")
        if tenant_id and task_scope_id:
            return tenant_id, task_scope_id
        source_scope_id = str(row.get("source_scope_id") or "")
        filename = str(row.get("filename") or "")
        traj_id = filename.removesuffix(".md")
        if not source_scope_id:
            return None
        from xskill.pipeline.registry import pooled_connection

        with pooled_connection(self.db_path) as connection:
            state = connection.execute(
                "SELECT tenant_id,task_scope_id FROM task_graph_source_state"
                " WHERE source_scope_id=? AND traj_id=?",
                (source_scope_id, traj_id),
            ).fetchone()
        return (
            (str(state["tenant_id"]), str(state["task_scope_id"]))
            if state is not None else None
        )

    def _sources_for_scope(
        self,
        tenant_id: str,
        task_scope_id: str,
        resolved_dirty: dict[tuple[int, str], ScopedTrajectoryEvidence | None],
    ) -> list[ScopedTrajectoryEvidence]:
        sources: dict[tuple[str, str], ScopedTrajectoryEvidence] = {}
        states = source_states_for_scope(
            tenant_id, task_scope_id, db_path=self.db_path,
        )
        for state in states:
            filename = f"{state['traj_id']}.md"
            dirty_key = (int(state["watch_dir_id"]), filename)
            if dirty_key in resolved_dirty:
                evidence = resolved_dirty[dirty_key]
            else:
                source_key = (
                    str(state["source_scope_id"]), str(state["traj_id"]),
                )
                cached = self._evidence_cache.get(source_key)
                if (
                    cached is not None
                    and cached.source_revision == state["source_revision"]
                ):
                    self._evidence_cache.move_to_end(source_key)
                    evidence = cached
                    sources[source_key] = evidence
                    continue
                live = live_source_row(*dirty_key, db_path=self.db_path)
                if live is None:
                    evidence = None
                else:
                    watch_dir, trajectory = live
                    scope = self.resolver.resolve(
                        watch_dir=watch_dir, trajectory=trajectory,
                    )
                    if (
                        scope.tenant_id == tenant_id
                        and scope.task_scope_id == task_scope_id
                    ):
                        try:
                            evidence = collect_trajectory_evidence(
                                watch_dir=watch_dir,
                                trajectory=trajectory,
                                scope=scope,
                            )
                            self._cache_evidence(evidence)
                        except FileNotFoundError:
                            evidence = None
                    else:
                        evidence = None
            if (
                evidence is not None
                and evidence.scope.tenant_id == tenant_id
                and evidence.scope.task_scope_id == task_scope_id
            ):
                sources[(
                    evidence.session_ref.source_scope_id,
                    evidence.session_ref.traj_id,
                )] = evidence
        for evidence in resolved_dirty.values():
            if evidence is None:
                continue
            if (
                evidence.scope.tenant_id == tenant_id
                and evidence.scope.task_scope_id == task_scope_id
            ):
                sources[(
                    evidence.session_ref.source_scope_id,
                    evidence.session_ref.traj_id,
                )] = evidence
        return sorted(
            sources.values(),
            key=attrgetter(
                "session_ref.source_scope_id", "session_ref.traj_id",
            ),
        )

    def _rebuild_scope(
        self,
        tenant_id: str,
        task_scope_id: str,
        resolved_dirty: dict[
            tuple[int, str], ScopedTrajectoryEvidence | None
        ],
    ):
        store = self.store_for_scope(task_scope_id)
        with task_file_lock(store.lock_path):
            sources = self._sources_for_scope(
                tenant_id, task_scope_id, resolved_dirty,
            )
            generation = self._build_scope(
                tenant_id, task_scope_id, sources,
            )
            return generation, len(sources)

    def _build_scope(
        self,
        tenant_id: str,
        task_scope_id: str,
        sources: list[ScopedTrajectoryEvidence],
    ):
        store = self.store_for_scope(task_scope_id)
        previous = store.load_current()
        overrides = store.read_overrides()
        if any(
            event.tenant_id != tenant_id
            or event.task_scope_id != task_scope_id
            for event in overrides
        ):
            raise RuntimeError("Task Graph override log contains a cross-scope event")
        source_revision = _scope_revision(sources)
        if (
            previous is not None
            and previous.source_revision == source_revision
            and previous.base_override_seq == (overrides[-1].override_seq if overrides else 0)
            and previous.generator == self.linker.generator_descriptor()
        ):
            generation = previous
            should_publish = False
        else:
            generation = self.linker.build(
                tenant_id=tenant_id,
                task_scope_id=task_scope_id,
                trajectories=sources,
                previous=previous,
                overrides=overrides,
                source_revision=source_revision,
            )
            should_publish = True
        upsert_execution_usage_events(
            (
                event for source in sources for event in source.usage_events
            ),
            db_path=self.db_path,
        )
        if should_publish:
            store.publish(generation)
        project_generation(
            generation, sources=sources, db_path=self.db_path,
        )
        logger.info(
            "Task Graph published scope=%s generation=%s sources=%d atoms=%d tasks=%d",
            task_scope_id,
            generation.generation_id,
            len(sources),
            generation.metrics.get("atom_count", 0),
            generation.metrics.get("task_count", 0),
        )
        return generation

    def append_override(
        self,
        *,
        task_scope_id: str,
        operation: str,
        target_id: str,
        payload: dict,
        actor: str,
        evidence_refs=(),
        event_id: str | None = None,
    ) -> OverrideEvent:
        if not self.enabled:
            raise RuntimeError("Task Graph is disabled by task_graph.enabled")
        store = self.store_for_scope(task_scope_id)
        with task_file_lock(store.lock_path):
            return self._append_override_locked(
                store=store,
                task_scope_id=task_scope_id,
                operation=operation,
                target_id=target_id,
                payload=payload,
                actor=actor,
                evidence_refs=evidence_refs,
                event_id=event_id,
            )

    def _append_override_locked(
        self,
        *,
        store: TaskGraphStore,
        task_scope_id: str,
        operation: str,
        target_id: str,
        payload: dict,
        actor: str,
        evidence_refs,
        event_id: str | None,
    ) -> OverrideEvent:
        current = store.load_current()
        if current is None:
            raise KeyError(f"TaskScope has no current graph: {task_scope_id}")
        if current.tenant_id != self.resolver.tenant_id:
            raise ValueError("TaskScope does not belong to this xskill tenant")
        if current.task_scope_id != task_scope_id:
            raise ValueError("TaskScope store identity does not match the request")
        if not isinstance(actor, str) or not actor.strip():
            raise ValueError("override actor must be a non-empty string")
        actor = actor.strip()
        if not isinstance(target_id, str) or not target_id.strip():
            raise ValueError("override target_id must be a non-empty string")
        target_id = target_id.strip()
        if len(actor) > 200 or len(target_id) > 200:
            raise ValueError("override actor and target_id are limited to 200 characters")
        if not isinstance(payload, dict):
            raise ValueError("override payload must be an object")
        try:
            payload_size = len(json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8"))
        except (TypeError, ValueError) as error:
            raise ValueError("override payload must be strict JSON") from error
        if payload_size > 65_536:
            raise ValueError("override payload is limited to 64 KiB")
        if isinstance(evidence_refs, (str, bytes)):
            raise ValueError("override evidence_refs must be an iterable of strings")
        evidence_refs = tuple(evidence_refs)
        if len(evidence_refs) > 100 or any(
            not isinstance(item, str) or not item or len(item) > 200
            for item in evidence_refs
        ):
            raise ValueError(
                "override evidence_refs are limited to 100 non-empty 200-character ids"
            )
        if event_id is not None:
            if not isinstance(event_id, str) or not event_id.strip():
                raise ValueError("override event_id must be a non-empty string")
            event_id = event_id.strip()
            if len(event_id) > 200:
                raise ValueError("override event_id is limited to 200 characters")
        if event_id:
            for existing in store.read_overrides():
                if existing.event_id != event_id:
                    continue
                supplied_payload = dict(payload or {})
                for unordered_field in ("task_ids", "atom_keys"):
                    supplied = supplied_payload.get(unordered_field)
                    if isinstance(supplied, list) and all(
                        isinstance(item, str) for item in supplied
                    ):
                        supplied_payload[unordered_field] = sorted(set(supplied))
                compatible_payload = all(
                    existing.payload.get(key) == value
                    for key, value in supplied_payload.items()
                )
                if not (
                    existing.tenant_id == current.tenant_id
                    and existing.task_scope_id == task_scope_id
                    and existing.operation == operation
                    and existing.target_id == target_id
                    and existing.actor == actor
                    and existing.evidence_refs == evidence_refs
                    and compatible_payload
                ):
                    raise ValueError(
                        "override event_id already exists with different content"
                    )
                self._rebuild_after_override(
                    current=current,
                    task_scope_id=task_scope_id,
                )
                return existing
        normalized_payload = self._validate_override(
            current, operation, target_id, payload,
        )
        dirty_scope = mark_task_graph_scope_dirty(
            current.tenant_id,
            task_scope_id,
            reason="manual_override",
            db_path=self.db_path,
        )
        event = store.append_override(
            tenant_id=current.tenant_id,
            task_scope_id=task_scope_id,
            operation=operation,
            target_id=target_id,
            payload=normalized_payload,
            evidence_refs=evidence_refs,
            actor=actor,
            event_id=event_id,
        )
        self._rebuild_after_override(
            current=current,
            task_scope_id=task_scope_id,
            dirty_scope=dirty_scope,
        )
        return event

    def _rebuild_after_override(
        self, *, current, task_scope_id: str, dirty_scope: dict | None = None,
    ) -> None:
        """Fence and materialize an override, including idempotent retries."""
        if dirty_scope is None:
            dirty_scope = mark_task_graph_scope_dirty(
                current.tenant_id,
                task_scope_id,
                reason="manual_override_retry",
                db_path=self.db_path,
            )
        self._rebuild_scope(current.tenant_id, task_scope_id, {})
        acknowledge_dirty_task_scopes(
            [dirty_scope], db_path=self.db_path,
        )

    @staticmethod
    def _validate_override(current, operation: str, target_id: str, payload: dict) -> dict:
        normalized = dict(payload or {})
        tasks = {task.task_id: task for task in current.tasks}
        attempts = {attempt.attempt_id: attempt for attempt in current.attempts}
        memberships = {
            membership.membership_id: membership
            for membership in current.memberships
        }
        relations = {
            relation.relation_id: relation for relation in current.relations
        }
        attempt_relations = {
            relation.relation_id: relation
            for relation in current.attempt_relations
        }

        def only_fields(allowed: set[str]) -> None:
            unknown = set(normalized) - allowed
            if unknown:
                raise ValueError(
                    f"unsupported {operation} payload fields: {sorted(unknown)!r}"
                )

        def required_id(field_name: str, *, fallback: str | None = None) -> str:
            value = normalized.get(field_name, fallback)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(
                    f"{operation} {field_name} must be a non-empty string"
                )
            value = value.strip()
            if len(value) > 200:
                raise ValueError(
                    f"{operation} {field_name} is limited to 200 characters"
                )
            return value

        def required_id_set(field_name: str) -> set[str]:
            value = normalized.get(field_name)
            if not isinstance(value, list):
                raise ValueError(f"{operation} {field_name} must be a JSON list")
            if not value:
                raise ValueError(f"{operation} {field_name} cannot be empty")
            if any(
                not isinstance(item, str)
                or not item.strip()
                or len(item.strip()) > 200
                for item in value
            ):
                raise ValueError(
                    f"{operation} {field_name} must contain non-empty strings "
                    "of at most 200 characters"
                )
            return {item.strip() for item in value}

        if operation in {"confirm_membership", "reject_membership"}:
            only_fields({"atom_key", "atom_ref", "task_id", "role"})
            membership = memberships.get(target_id)
            if membership is not None:
                expected = {
                    "atom_key": stable_ref_key(membership.atom_ref),
                    "atom_ref": membership.atom_ref.to_dict(),
                    "task_id": membership.task_id,
                    "role": membership.role,
                }
                for field_name, field_value in expected.items():
                    supplied = normalized.get(field_name)
                    if supplied is not None and supplied != field_value:
                        raise ValueError(
                            "membership override payload conflicts with target "
                            f"{field_name}"
                        )
                normalized.update(expected)
            if not normalized.get("task_id") or normalized["task_id"] not in tasks:
                raise ValueError("membership override references a missing Task")
            if normalized.get("role") not in {"primary", "context"}:
                raise ValueError("membership role must be primary or context")
            if not normalized.get("atom_key") or not isinstance(
                normalized.get("atom_ref"), dict,
            ):
                raise ValueError("membership override requires a scoped Atom reference")
            try:
                from xskill.tasks.models import AtomRef

                atom_ref = AtomRef.from_dict(normalized["atom_ref"])
            except (KeyError, TypeError, ValueError) as error:
                raise ValueError("membership override has an invalid AtomRef") from error
            if stable_ref_key(atom_ref) != normalized["atom_key"]:
                raise ValueError("membership atom_key does not match atom_ref")
            if (
                atom_ref.tenant_id != current.tenant_id
                or atom_ref.task_scope_id != current.task_scope_id
            ):
                raise ValueError("membership override cannot cross TaskScope")
            known_atom_keys = {
                stable_ref_key(item.atom_ref) for item in current.memberships
            }
            if normalized["atom_key"] not in known_atom_keys:
                raise ValueError("membership override references a missing scoped Atom")
            return normalized

        if operation == "set_task_state":
            only_fields({"lifecycle", "outcome", "verification", "user_disposition"})
            if target_id not in tasks:
                raise ValueError("Task state override references a missing Task")
            choices = {
                "lifecycle": TASK_LIFECYCLES,
                "outcome": TASK_OUTCOMES,
                "verification": VERIFICATIONS,
                "user_disposition": USER_DISPOSITIONS,
            }
            if not normalized:
                raise ValueError("Task state override must change at least one dimension")
            for field_name, value in normalized.items():
                if value not in choices[field_name]:
                    raise ValueError(f"invalid Task {field_name}: {value!r}")
            task = tasks[target_id]
            if "outcome" in normalized and "lifecycle" not in normalized:
                normalized["lifecycle"] = (
                    "open" if normalized["outcome"] == "unknown" else "closed"
                )
            if normalized.get("lifecycle") in {"open", "blocked"}:
                normalized.setdefault("outcome", "unknown")
            effective_lifecycle = normalized.get("lifecycle", task.lifecycle)
            effective_outcome = normalized.get("outcome", task.outcome)
            if (
                effective_lifecycle in {"open", "blocked"}
                and effective_outcome != "unknown"
            ):
                raise ValueError("open or blocked Task outcome must be unknown")
            if effective_lifecycle == "closed" and effective_outcome == "unknown":
                raise ValueError("closed Task requires a terminal outcome")
            return normalized

        if operation == "set_attempt_state":
            only_fields({"lifecycle", "outcome", "verification", "user_disposition"})
            if target_id not in attempts:
                raise ValueError("Attempt state override references a missing Attempt")
            choices = {
                "lifecycle": ATTEMPT_LIFECYCLES,
                "outcome": ATTEMPT_OUTCOMES,
                "verification": VERIFICATIONS,
                "user_disposition": USER_DISPOSITIONS,
            }
            if not normalized:
                raise ValueError("Attempt state override must change at least one dimension")
            for field_name, value in normalized.items():
                if value not in choices[field_name]:
                    raise ValueError(f"invalid Attempt {field_name}: {value!r}")
            if normalized.get("lifecycle") == "running":
                if normalized.get("outcome", "unknown") != "unknown":
                    raise ValueError("running Attempt outcome must be unknown")
                normalized.setdefault("outcome", "unknown")
                normalized.setdefault("verification", "unverified")
            elif (
                attempts[target_id].lifecycle == "running"
                and normalized.get("outcome") not in (None, "unknown")
            ):
                normalized.setdefault("lifecycle", "finished")
            return normalized

        if operation == "upsert_attempt_relation":
            only_fields({"from_attempt_id", "to_attempt_id", "relation_type"})
            from_attempt_id = required_id(
                "from_attempt_id", fallback=target_id,
            )
            to_attempt_id = required_id("to_attempt_id")
            relation_type = required_id("relation_type")
            if from_attempt_id not in attempts or to_attempt_id not in attempts:
                raise ValueError("Attempt relation references a missing Attempt")
            if from_attempt_id == to_attempt_id:
                raise ValueError("Attempt relation cannot point to itself")
            if attempts[from_attempt_id].task_id != attempts[to_attempt_id].task_id:
                raise ValueError("Attempt relations cannot cross Logical Tasks")
            if relation_type not in ATTEMPT_RELATION_TYPES:
                raise ValueError(
                    f"invalid Attempt relation type: {relation_type!r}"
                )
            normalized.update({
                "from_attempt_id": from_attempt_id,
                "to_attempt_id": to_attempt_id,
                "relation_type": relation_type,
            })
            TaskGraphService._validate_attempt_relation_dag(
                current, from_attempt_id, to_attempt_id, relation_type,
            )
            return normalized

        if operation == "reject_attempt_relation":
            only_fields(set())
            if target_id not in attempt_relations:
                raise ValueError("Attempt relation override references a missing relation")
            return normalized

        if operation == "upsert_task_relation":
            only_fields({"from_task_id", "to_task_id", "relation_type"})
            from_task_id = required_id("from_task_id", fallback=target_id)
            to_task_id = required_id("to_task_id")
            relation_type = required_id("relation_type")
            if from_task_id not in tasks or to_task_id not in tasks:
                raise ValueError("Task relation references a missing Task")
            if from_task_id == to_task_id:
                raise ValueError("Task relation cannot point to itself")
            if relation_type not in TASK_RELATION_TYPES:
                raise ValueError(f"invalid Task relation type: {relation_type!r}")
            normalized.update({
                "from_task_id": from_task_id,
                "to_task_id": to_task_id,
                "relation_type": relation_type,
            })
            TaskGraphService._validate_relation_dag(
                current, from_task_id, to_task_id, relation_type,
            )
            return normalized

        if operation == "reject_task_relation":
            only_fields(set())
            if target_id not in relations:
                raise ValueError("relation override references a missing relation")
            return normalized

        if operation == "merge_tasks":
            only_fields({"task_ids"})
            task_ids = required_id_set("task_ids")
            if target_id not in tasks:
                raise ValueError("merge requires a canonical Task and at least one source Task")
            if target_id in task_ids or not task_ids.issubset(tasks):
                raise ValueError("merge references an invalid source Task")
            TaskGraphService._validate_merge_dag(
                current, target_id, task_ids,
            )
            normalized["task_ids"] = sorted(task_ids)
            return normalized

        if operation == "move_atoms":
            only_fields({"target_task_id", "atom_keys"})
            target_task_id = required_id(
                "target_task_id", fallback=target_id,
            )
            atom_keys = required_id_set("atom_keys")
            known_atom_keys = {
                stable_ref_key(membership.atom_ref)
                for membership in current.memberships
            }
            if target_task_id not in tasks:
                raise ValueError("move_atoms references a missing target Task")
            if not atom_keys or not atom_keys.issubset(known_atom_keys):
                raise ValueError("move_atoms references missing scoped Atoms")
            TaskGraphService._validate_atom_move(
                current, atom_keys, target_task_id,
            )
            normalized["target_task_id"] = target_task_id
            normalized["atom_keys"] = sorted(atom_keys)
            normalized["atom_refs"] = TaskGraphService._atom_ref_payloads(
                current, atom_keys,
            )
            return normalized

        if operation == "split_task":
            only_fields({"atom_keys", "title", "summary"})
            if target_id not in tasks:
                raise ValueError("split_task references a missing source Task")
            atom_keys = required_id_set("atom_keys")
            source_atom_keys = {
                stable_ref_key(membership.atom_ref)
                for membership in current.memberships
                if membership.task_id == target_id
                and membership.role == "primary"
                and membership.decision == "confirmed"
                and not membership.stale
            }
            if not atom_keys or not atom_keys < source_atom_keys:
                raise ValueError(
                    "split_task must move a non-empty proper subset of source Atoms"
                )
            TaskGraphService._validate_atom_move(
                current, atom_keys, "__new_split_task__",
            )
            title = normalized.get("title", tasks[target_id].title)
            summary = normalized.get("summary", tasks[target_id].summary)
            if not isinstance(title, str) or not isinstance(summary, str):
                raise ValueError("split_task title and summary must be strings")
            normalized.update({
                "atom_keys": sorted(atom_keys),
                "atom_refs": TaskGraphService._atom_ref_payloads(
                    current, atom_keys,
                ),
                "new_task_id": f"tsk_{uuid.uuid4().hex}",
                "title": title[:240],
                "summary": summary[:1000],
            })
            return normalized

        raise ValueError(f"unknown Task Graph override operation: {operation!r}")

    @staticmethod
    def _atom_ref_payloads(current, atom_keys: set[str]) -> dict[str, dict]:
        result = {}
        for membership in current.memberships:
            atom_key = stable_ref_key(membership.atom_ref)
            if atom_key in atom_keys:
                result[atom_key] = membership.atom_ref.to_dict()
        if set(result) != atom_keys:
            raise ValueError("override cannot resolve every scoped Atom reference")
        return result

    @staticmethod
    def _validate_atom_move(
        current, atom_keys: set[str], target_task_id: str,
    ) -> None:
        owner_by_atom = {
            stable_ref_key(membership.atom_ref): membership.task_id
            for membership in current.memberships
            if membership.role == "primary"
            and membership.decision == "confirmed"
            and not membership.stale
        }
        attempt_atom_keys: dict[str, set[str]] = {}
        attempt_task_ids: dict[str, str] = {}
        for attempt in current.attempts:
            keys = {
                stable_ref_key(evidence.atom_ref)
                for evidence in attempt.evidence_ranges
                if evidence.atom_ref is not None and not evidence.stale
            }
            if keys:
                attempt_atom_keys[attempt.attempt_id] = keys
                attempt_task_ids[attempt.attempt_id] = attempt.task_id
            overlap = keys & atom_keys
            if overlap and overlap != keys:
                raise ValueError(
                    "Atom move cannot divide one existing Attempt; split or "
                    "correct the Attempt boundary first"
                )
        moved_attempt_ids = {
            attempt_id
            for attempt_id, keys in attempt_atom_keys.items()
            if keys and keys <= atom_keys
        }
        resulting_task = {
            attempt_id: (
                target_task_id
                if attempt_id in moved_attempt_ids
                else attempt_task_ids[attempt_id]
            )
            for attempt_id in attempt_task_ids
        }
        for relation in current.attempt_relations:
            if (
                relation.decision != "confirmed"
                or not relation.decided_by.startswith("human:")
            ):
                continue
            if (
                resulting_task.get(relation.from_attempt_id)
                != resulting_task.get(relation.to_attempt_id)
            ):
                raise ValueError(
                    "Atom move would separate a human-confirmed Attempt relation"
                )
        if not atom_keys.issubset(owner_by_atom):
            raise ValueError("Atom move requires live confirmed primary ownership")

    @staticmethod
    def _validate_relation_dag(
        current, from_task_id: str, to_task_id: str, relation_type: str,
    ) -> None:
        if relation_type in {"parent", "subtask"}:
            child_id = (
                to_task_id if relation_type == "parent" else from_task_id
            )
            parent_id = (
                from_task_id if relation_type == "parent" else to_task_id
            )
            for relation in current.relations:
                if (
                    relation.decision != "confirmed"
                    or relation.stale
                    or relation.relation_type not in {"parent", "subtask"}
                ):
                    continue
                existing_child = (
                    relation.to_task_id
                    if relation.relation_type == "parent"
                    else relation.from_task_id
                )
                existing_parent = (
                    relation.from_task_id
                    if relation.relation_type == "parent"
                    else relation.to_task_id
                )
                if existing_child == child_id and existing_parent != parent_id:
                    raise ValueError(
                        "a Task may have at most one confirmed primary parent"
                    )
        edges: dict[str, set[str]] = {
            task.task_id: set() for task in current.tasks
        }
        for relation in current.relations:
            if relation.decision != "confirmed" or relation.stale:
                continue
            if (
                relation.from_task_id == from_task_id
                and relation.to_task_id == to_task_id
                and relation.relation_type == relation_type
            ):
                continue
            edge_from, edge_to = _task_relation_edge(
                relation.from_task_id,
                relation.to_task_id,
                relation.relation_type,
            )
            edges[edge_from].add(edge_to)
        edge_from, edge_to = _task_relation_edge(
            from_task_id, to_task_id, relation_type,
        )
        edges[edge_from].add(edge_to)
        _require_acyclic(
            edges, "confirmed Task relations must form a DAG",
        )

    @staticmethod
    def _validate_attempt_relation_dag(
        current, from_attempt_id: str, to_attempt_id: str,
        relation_type: str,
    ) -> None:
        edges: dict[str, set[str]] = {
            attempt.attempt_id: set() for attempt in current.attempts
        }
        for relation in current.attempt_relations:
            if relation.decision != "confirmed":
                continue
            if (
                relation.from_attempt_id == from_attempt_id
                and relation.to_attempt_id == to_attempt_id
                and relation.relation_type == relation_type
            ):
                continue
            edges[relation.from_attempt_id].add(relation.to_attempt_id)
        edges[from_attempt_id].add(to_attempt_id)
        _require_acyclic(
            edges, "confirmed Attempt relations must form a DAG",
        )

    @staticmethod
    def _validate_merge_dag(current, canonical_id: str, merge_ids: set[str]) -> None:
        def canonical(task_id: str) -> str:
            return canonical_id if task_id in merge_ids else task_id

        edges: dict[str, set[str]] = {
            canonical(task.task_id): set() for task in current.tasks
        }
        parent_by_child: dict[str, str] = {}
        for relation in current.relations:
            if relation.decision != "confirmed" or relation.stale:
                continue
            from_task_id = canonical(relation.from_task_id)
            to_task_id = canonical(relation.to_task_id)
            if from_task_id == to_task_id:
                continue
            edge_from, edge_to = _task_relation_edge(
                from_task_id, to_task_id, relation.relation_type,
            )
            edges[edge_from].add(edge_to)
            if relation.relation_type in {"parent", "subtask"}:
                child_id = (
                    to_task_id
                    if relation.relation_type == "parent"
                    else from_task_id
                )
                parent_id = (
                    from_task_id
                    if relation.relation_type == "parent"
                    else to_task_id
                )
                existing = parent_by_child.setdefault(child_id, parent_id)
                if existing != parent_id:
                    raise ValueError(
                        "merge would give a Task more than one primary parent"
                    )
        _require_acyclic(edges, "merge would create a Task relation cycle")
