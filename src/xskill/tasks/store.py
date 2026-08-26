"""Versioned Task Graph fact store with atomic publication and overrides."""
from __future__ import annotations

import hashlib
import json
import os
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from xskill.tasks.locking import task_file_lock
from xskill.tasks.models import TaskGraphGeneration

OVERRIDE_OPERATIONS = frozenset((
    "confirm_membership", "reject_membership", "set_task_state",
    "set_attempt_state", "upsert_task_relation", "reject_task_relation",
    "upsert_attempt_relation", "reject_attempt_relation", "merge_tasks",
    "move_atoms", "split_task",
))
_ENTITY_FIELDS = (
    "tasks", "memberships", "relations", "attempts", "attempt_relations",
    "usage_allocations",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _write_atomic(path: Path, payload: bytes, *, mode: int | None = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with temporary.open("wb") as output:
        output.write(payload)
        output.flush()
        os.fsync(output.fileno())
    if mode is not None and os.name != "nt":
        temporary.chmod(mode)
    os.replace(temporary, path)
    if os.name != "nt":
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)


@dataclass(frozen=True)
class OverrideEvent:
    event_id: str
    override_seq: int
    tenant_id: str
    task_scope_id: str
    operation: str
    target_id: str
    payload: dict
    evidence_refs: tuple[str, ...]
    actor: str
    observed_at: str

    def __post_init__(self) -> None:
        if self.operation not in OVERRIDE_OPERATIONS:
            raise ValueError(
                f"unknown Task Graph override operation: {self.operation!r}"
            )
        if (
            isinstance(self.override_seq, bool)
            or not isinstance(self.override_seq, int)
            or self.override_seq <= 0
        ):
            raise ValueError("override_seq must be a positive integer")
        for field_name in (
            "event_id", "tenant_id", "task_scope_id", "target_id", "actor",
            "observed_at",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"override {field_name} must be a non-empty string")
            if field_name != "observed_at" and len(value.strip()) > 200:
                raise ValueError(
                    f"override {field_name} is limited to 200 characters"
                )
        if not isinstance(self.payload, dict):
            raise ValueError("override payload must be an object")
        try:
            payload_size = len(_canonical_json(self.payload))
        except (TypeError, ValueError) as error:
            raise ValueError("override payload must be strict JSON") from error
        if payload_size > 65_536:
            raise ValueError("override payload is limited to 64 KiB")
        if len(self.evidence_refs) > 100 or any(
            not isinstance(item, str) or not item or len(item) > 200
            for item in self.evidence_refs
        ):
            raise ValueError(
                "override evidence_refs are limited to 100 non-empty "
                "200-character strings"
            )

    def to_dict(self) -> dict:
        return {
            "event_id": self.event_id,
            "override_seq": self.override_seq,
            "tenant_id": self.tenant_id,
            "task_scope_id": self.task_scope_id,
            "operation": self.operation,
            "target_id": self.target_id,
            "payload": self.payload,
            "evidence_refs": list(self.evidence_refs),
            "actor": self.actor,
            "observed_at": self.observed_at,
        }

    @classmethod
    def from_dict(cls, value: dict) -> OverrideEvent:
        operation = value.get("operation")
        if not isinstance(operation, str):
            raise ValueError("override operation must be a string")
        if operation not in OVERRIDE_OPERATIONS:
            raise ValueError(f"unknown Task Graph override operation: {operation!r}")
        sequence = value.get("override_seq")
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence <= 0:
            raise ValueError("override_seq must be a positive integer")
        required_fields = {
            "event_id": value.get("event_id"),
            "tenant_id": value.get("tenant_id"),
            "task_scope_id": value.get("task_scope_id"),
            "target_id": value.get("target_id"),
            "actor": value.get("actor"),
            "observed_at": value.get("observed_at"),
        }
        for field_name, field_value in required_fields.items():
            if not isinstance(field_value, str) or not field_value.strip():
                raise ValueError(f"override {field_name} must be a non-empty string")
            if field_name != "observed_at" and len(field_value.strip()) > 200:
                raise ValueError(
                    f"override {field_name} is limited to 200 characters"
                )
        payload = value.get("payload")
        evidence_refs = value.get("evidence_refs")
        if not isinstance(payload, dict):
            raise ValueError("override payload must be an object")
        if (
            not isinstance(evidence_refs, list)
            or len(evidence_refs) > 100
            or not all(
                isinstance(item, str) and item and len(item) <= 200
                for item in evidence_refs
            )
        ):
            raise ValueError(
                "override evidence_refs must contain at most 100 non-empty "
                "200-character strings"
            )
        try:
            payload_size = len(_canonical_json(payload))
        except (TypeError, ValueError) as error:
            raise ValueError("override payload must be strict JSON") from error
        if payload_size > 65_536:
            raise ValueError("override payload is limited to 64 KiB")
        return cls(
            event_id=str(value["event_id"]),
            override_seq=sequence,
            tenant_id=str(value["tenant_id"]),
            task_scope_id=str(value["task_scope_id"]),
            operation=operation,
            target_id=str(value["target_id"]),
            payload=dict(payload),
            evidence_refs=tuple(evidence_refs),
            actor=str(value.get("actor") or "unknown"),
            observed_at=str(value["observed_at"]),
        )


class TaskGraphStore:
    """Immutable content-addressed generations plus an append-only override log."""

    SHARD_RECORD_LIMIT = 256

    def __init__(self, scope_dir: Path):
        self.scope_dir = Path(scope_dir)
        self.current_path = self.scope_dir / "current.json"
        self.generations_dir = self.scope_dir / "generations"
        self.shards_dir = self.scope_dir / "shards"
        self.overrides_path = self.scope_dir / "overrides.jsonl"
        self.lock_path = self.scope_dir / "transaction.lock"
        self._cached_pointer_payload: bytes | None = None
        self._cached_generation: TaskGraphGeneration | None = None

    def _secure_scope_dir(self) -> None:
        self.scope_dir.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            self.scope_dir.chmod(0o700)

    def _store_records(self, entity: str, records: list[dict]) -> list[dict]:
        references = []
        for offset in range(0, len(records), self.SHARD_RECORD_LIMIT):
            chunk = records[offset:offset + self.SHARD_RECORD_LIMIT]
            references.append(self._store_shard(entity, chunk))
        return references

    def _store_shard(self, entity: str, records: list[dict]) -> dict:
        payload = _canonical_json(records)
        digest = _sha(payload)
        relative = Path("shards") / entity / f"{digest}.json"
        path = self.scope_dir / relative
        if not path.is_file():
            _write_atomic(path, payload)
        elif os.name != "nt":
            path.chmod(0o600)
        return {
            "sha256": digest,
            "path": relative.as_posix(),
            "record_count": len(records),
        }

    def _read_shard(self, reference: dict) -> list[dict]:
        relative = Path(str(reference.get("path") or ""))
        if relative.is_absolute() or ".." in relative.parts:
            raise RuntimeError("Task Graph shard reference escapes scope directory")
        payload = (self.scope_dir / relative).read_bytes()
        expected = str(reference.get("sha256") or "")
        if _sha(payload) != expected:
            raise RuntimeError(f"Task Graph shard checksum mismatch: {relative}")
        value = json.loads(payload.decode("utf-8", errors="strict"))
        if isinstance(value, dict):
            return [value]
        if not isinstance(value, list) or not all(
            isinstance(record, dict) for record in value
        ):
            raise RuntimeError(
                f"Task Graph shard must contain records: {relative}"
            )
        expected_count = reference.get("record_count")
        if expected_count is not None and expected_count != len(value):
            raise RuntimeError(f"Task Graph shard count mismatch: {relative}")
        return value

    def publish(self, generation: TaskGraphGeneration) -> dict:
        """Durably write all immutable facts before switching ``current.json``."""
        with task_file_lock(self.lock_path):
            self._secure_scope_dir()
            if any(
                separator in generation.generation_id
                for separator in ("/", "\\", "..")
            ):
                raise ValueError("generation_id is not a safe opaque identifier")
            if self.current_path.is_file():
                current = self.load_current()
                if current is not None:
                    if current.tenant_id != generation.tenant_id:
                        raise ValueError("cannot publish another tenant into this TaskScope")
                    if current.task_scope_id != generation.task_scope_id:
                        raise ValueError("scope directory does not match Task Graph generation")
                    if generation.base_override_seq < current.base_override_seq:
                        raise ValueError("override watermark cannot move backwards")
            expanded = generation.to_dict()
            manifest = {
                key: expanded[key]
                for key in (
                    "schema_version", "generation_id", "tenant_id", "task_scope_id",
                    "source_revision", "generator", "base_override_seq", "created_at",
                    "metrics",
                )
            }
            for entity in _ENTITY_FIELDS:
                manifest[entity] = self._store_records(
                    entity, expanded[entity],
                )
            manifest_payload = _canonical_json(manifest)
            manifest_sha = _sha(manifest_payload)
            generation_dir = self.generations_dir / generation.generation_id
            manifest_path = generation_dir / "manifest.json"
            if manifest_path.is_file():
                existing = manifest_path.read_bytes()
                if existing != manifest_payload:
                    raise RuntimeError("generation_id already exists with different content")
                if os.name != "nt":
                    manifest_path.chmod(0o600)
            else:
                _write_atomic(manifest_path, manifest_payload)
            pointer = {
                "schema_version": generation.schema_version,
                "generation_id": generation.generation_id,
                "manifest_sha256": manifest_sha,
                "base_override_seq": generation.base_override_seq,
                "published_at": utc_now(),
            }
            pointer_payload = _canonical_json(pointer)
            _write_atomic(self.current_path, pointer_payload)
            self._cached_pointer_payload = pointer_payload
            self._cached_generation = generation
            return pointer

    def load_current(self) -> TaskGraphGeneration | None:
        if not self.current_path.is_file():
            return None
        try:
            pointer_payload = self.current_path.read_bytes()
            if (
                pointer_payload == self._cached_pointer_payload
                and self._cached_generation is not None
            ):
                return self._cached_generation
            pointer = json.loads(pointer_payload.decode("utf-8", errors="strict"))
            if not isinstance(pointer, dict):
                raise TypeError("Task Graph current pointer must be an object")
            generation_id = str(pointer["generation_id"])
            if any(
                separator in generation_id for separator in ("/", "\\", "..")
            ):
                raise ValueError("unsafe generation_id in current pointer")
            manifest_path = self.generations_dir / generation_id / "manifest.json"
            manifest_payload = manifest_path.read_bytes()
        except (
            OSError, json.JSONDecodeError, KeyError, TypeError, ValueError,
        ) as error:
            raise RuntimeError(f"invalid Task Graph current pointer: {self.current_path}") from error
        if _sha(manifest_payload) != pointer.get("manifest_sha256"):
            raise RuntimeError("Task Graph manifest checksum mismatch")
        try:
            manifest = json.loads(
                manifest_payload.decode("utf-8", errors="strict")
            )
            if not isinstance(manifest, dict):
                raise TypeError("Task Graph manifest must be an object")
            expanded = dict(manifest)
            for entity in _ENTITY_FIELDS:
                references = manifest[entity]
                if not isinstance(references, list) or not all(
                    isinstance(reference, dict) for reference in references
                ):
                    raise TypeError(
                        f"Task Graph manifest {entity} must contain shard objects"
                    )
                expanded[entity] = [
                    record
                    for reference in references
                    for record in self._read_shard(reference)
                ]
            generation = TaskGraphGeneration.from_dict(expanded)
        except (
            OSError, json.JSONDecodeError, KeyError, TypeError, ValueError,
        ) as error:
            raise RuntimeError(
                f"invalid Task Graph manifest: {manifest_path}"
            ) from error
        if generation.generation_id != generation_id:
            raise RuntimeError("Task Graph pointer and manifest generation_id differ")
        if pointer.get("schema_version") != generation.schema_version:
            raise RuntimeError("Task Graph pointer and manifest schema_version differ")
        if pointer.get("base_override_seq") != generation.base_override_seq:
            raise RuntimeError("Task Graph pointer and manifest override watermark differ")
        self._cached_pointer_payload = pointer_payload
        self._cached_generation = generation
        return generation

    def read_overrides(self, *, after_seq: int = 0) -> list[OverrideEvent]:
        if not self.overrides_path.is_file():
            return []
        events: list[OverrideEvent] = []
        previous_sequence = 0
        seen_ids: set[str] = set()
        with self.overrides_path.open(encoding="utf-8") as source:
            for line_number, raw_line in enumerate(source, 1):
                raw_line = raw_line.strip()
                if not raw_line:
                    continue
                try:
                    event = OverrideEvent.from_dict(json.loads(raw_line))
                except (json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
                    raise RuntimeError(
                        f"invalid override log at {self.overrides_path}:{line_number}"
                    ) from error
                if event.override_seq != previous_sequence + 1:
                    raise RuntimeError("Task Graph override sequence is not contiguous")
                if event.event_id in seen_ids:
                    raise RuntimeError("Task Graph override event_id is duplicated")
                previous_sequence = event.override_seq
                seen_ids.add(event.event_id)
                if event.override_seq > after_seq:
                    events.append(event)
        return events

    def override_watermark(self) -> int:
        events = self.read_overrides()
        return events[-1].override_seq if events else 0

    def append_override(
        self,
        *,
        tenant_id: str,
        task_scope_id: str,
        operation: str,
        target_id: str,
        payload: dict,
        evidence_refs: Iterable[str] = (),
        actor: str,
        event_id: str | None = None,
        observed_at: str | None = None,
    ) -> OverrideEvent:
        if operation not in OVERRIDE_OPERATIONS:
            raise ValueError(f"unknown Task Graph override operation: {operation!r}")
        if event_id is not None:
            if not isinstance(event_id, str) or not event_id.strip():
                raise ValueError("override event_id must be a non-empty string")
            event_id = event_id.strip()
            if len(event_id) > 200:
                raise ValueError("override event_id is limited to 200 characters")
        for field_name, field_value in (
            ("tenant_id", tenant_id), ("task_scope_id", task_scope_id),
            ("target_id", target_id), ("actor", actor),
        ):
            if not isinstance(field_value, str) or not field_value.strip():
                raise ValueError(f"override {field_name} must be a non-empty string")
        if not isinstance(payload, dict):
            raise ValueError("override payload must be an object")
        try:
            payload_size = len(_canonical_json(payload))
        except (TypeError, ValueError) as error:
            raise ValueError("override payload must be strict JSON") from error
        if payload_size > 65_536:
            raise ValueError("override payload is limited to 64 KiB")
        if isinstance(evidence_refs, (str, bytes)):
            raise ValueError("override evidence_refs must be an iterable of strings")
        normalized_payload = dict(payload)
        normalized_evidence_refs = tuple(evidence_refs)
        if len(normalized_evidence_refs) > 100 or any(
            not isinstance(item, str) or not item or len(item) > 200
            for item in normalized_evidence_refs
        ):
            raise ValueError(
                "override evidence_refs are limited to 100 non-empty "
                "200-character strings"
            )
        normalized_actor = actor.strip()
        with task_file_lock(self.lock_path):
            self._secure_scope_dir()
            existing = self.read_overrides()
            if event_id:
                for item in existing:
                    if item.event_id == event_id:
                        requested = {
                            "tenant_id": tenant_id,
                            "task_scope_id": task_scope_id,
                            "operation": operation,
                            "target_id": target_id,
                            "payload": normalized_payload,
                            "evidence_refs": normalized_evidence_refs,
                            "actor": normalized_actor,
                        }
                        actual = {
                            "tenant_id": item.tenant_id,
                            "task_scope_id": item.task_scope_id,
                            "operation": item.operation,
                            "target_id": item.target_id,
                            "payload": item.payload,
                            "evidence_refs": item.evidence_refs,
                            "actor": item.actor,
                        }
                        if requested != actual:
                            raise ValueError(
                                "override event_id already exists with different content"
                            )
                        return item
            event = OverrideEvent(
                event_id=event_id or f"ovr_{uuid.uuid4().hex}",
                override_seq=(existing[-1].override_seq + 1) if existing else 1,
                tenant_id=tenant_id,
                task_scope_id=task_scope_id,
                operation=operation,
                target_id=target_id,
                payload=normalized_payload,
                evidence_refs=normalized_evidence_refs,
                actor=normalized_actor,
                observed_at=observed_at or utc_now(),
            )
            if existing:
                first = existing[0]
                if first.tenant_id != tenant_id or first.task_scope_id != task_scope_id:
                    raise ValueError("override log scope mismatch")
            line = _canonical_json(event.to_dict()) + b"\n"
            existing_payload = (
                self.overrides_path.read_bytes()
                if self.overrides_path.is_file() else b""
            )
            _write_atomic(
                self.overrides_path, existing_payload + line, mode=0o600,
            )
            return event
