"""Collect scoped Atom, Session and execution-usage evidence for Task Graphs."""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import cached_property
from pathlib import Path
from typing import Any

from xskill.pipeline.atom import AtomTask, AtomTaskStore
from xskill.tasks.models import (
    MEASUREMENT_QUALITIES,
    AtomRef,
    SessionRef,
    model_from_dict,
)
from xskill.tasks.scopes import ScopeIdentity


def _hash_json(value: Any) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _read_object(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"invalid trajectory metadata: {path}") from error
    if not isinstance(value, dict):
        raise RuntimeError(f"trajectory metadata must contain an object: {path}")
    return value


def _number(value: Any, *, integer: bool = True) -> int | float | None:
    if isinstance(value, bool):
        return None
    if integer:
        if not isinstance(value, int):
            return None
        normalized: int | float = value
    else:
        if not isinstance(value, (int, float)):
            return None
        normalized = float(value)
    return normalized if math.isfinite(float(normalized)) and normalized >= 0 else None


def _overlay_identity(defaults: dict, override: Any) -> dict:
    result = dict(defaults)
    if isinstance(override, dict):
        result.update({
            key: value for key, value in override.items()
            if value not in (None, "", [], {})
        })
    return model_from_dict(result)


def _first_number(value: dict, keys: tuple[str, ...]) -> int | None:
    for key in keys:
        number = _number(value.get(key))
        if number is not None:
            return int(number)
    return None


@dataclass(frozen=True)
class ExecutionUsageEvent:
    usage_event_id: str
    source_event_id: str
    session_ref: SessionRef
    model: dict
    harness: dict
    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None
    cache_read_tokens: int | None
    cost_usd: float | None
    measurement_quality: str
    estimation_method: str
    unavailable_reason: str
    observed_at: str

    def __post_init__(self) -> None:
        if not self.usage_event_id or not self.source_event_id:
            raise ValueError("execution usage event identities must be non-empty")
        if self.measurement_quality not in MEASUREMENT_QUALITIES:
            raise ValueError("invalid execution usage measurement_quality")
        if not self.observed_at:
            raise ValueError("execution usage observed_at must be non-empty")
        if self.measurement_quality == "unavailable" and not self.unavailable_reason:
            raise ValueError("unavailable execution usage requires a reason")
        if self.measurement_quality == "estimated" and not self.estimation_method:
            raise ValueError("estimated execution usage requires estimation_method")
        for field_name in (
            "prompt_tokens", "completion_tokens", "total_tokens",
            "cache_read_tokens",
        ):
            value = getattr(self, field_name)
            if value is not None and (
                isinstance(value, bool)
                or not isinstance(value, int)
                or value < 0
            ):
                raise ValueError(f"{field_name} must be a non-negative integer or None")
        if self.cost_usd is not None and (
            isinstance(self.cost_usd, bool)
            or not isinstance(self.cost_usd, (int, float))
            or not math.isfinite(float(self.cost_usd))
            or self.cost_usd < 0
        ):
            raise ValueError("cost_usd must be a non-negative number or None")
        if self.measurement_quality == "unavailable" and any(
            value is not None
            for value in (
                self.prompt_tokens, self.completion_tokens,
                self.total_tokens, self.cache_read_tokens, self.cost_usd,
            )
        ):
            raise ValueError("unavailable execution usage cannot contain numeric values")
        if self.measurement_quality != "unavailable" and all(
            value is None
            for value in (
                self.prompt_tokens, self.completion_tokens,
                self.total_tokens, self.cache_read_tokens, self.cost_usd,
            )
        ):
            raise ValueError("measured or estimated execution usage needs a value")

    def to_record(self) -> dict:
        return {
            "usage_event_id": self.usage_event_id,
            "usage_plane": "execution",
            "source_event_id": self.source_event_id,
            "tenant_id": self.session_ref.tenant_id,
            "task_scope_id": self.session_ref.task_scope_id,
            "source_scope_id": self.session_ref.source_scope_id,
            "traj_id": self.session_ref.traj_id,
            "model": self.model,
            "harness": self.harness,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cost_usd": self.cost_usd,
            "measurement_quality": self.measurement_quality,
            "estimation_method": self.estimation_method,
            "unavailable_reason": self.unavailable_reason,
            "observed_at": self.observed_at,
        }


@dataclass(frozen=True)
class ScopedAtomEvidence:
    atom: AtomTask
    atom_ref: AtomRef
    atom_hash: str
    session_hash: str
    source_model: dict
    source_harness: dict
    observed_at: str

    @property
    def text(self) -> str:
        return "\n".join(
            item for item in (self.atom.intent, self.atom.summary, self.atom.raw_segment)
            if item
        )


@dataclass(frozen=True)
class ScopedTrajectoryEvidence:
    watch_dir_id: int
    watch_dir_path: Path
    filename: str
    scope: ScopeIdentity
    session_ref: SessionRef
    session_hash: str
    metadata: dict
    atoms: tuple[ScopedAtomEvidence, ...]
    usage_events: tuple[ExecutionUsageEvent, ...]
    explicit_outcome: dict

    @cached_property
    def source_revision(self) -> str:
        return _hash_json({
            "session_ref": self.session_ref.to_dict(),
            "session_hash": self.session_hash,
            "atoms": [
                {
                    "atom_ref": atom.atom_ref.to_dict(),
                    "atom_hash": atom.atom_hash,
                    "locator": [
                        atom.atom.offset_start, atom.atom.offset_end,
                    ],
                    "neighbors": [
                        atom.atom.pre_atom_id, atom.atom.post_atom_id,
                    ],
                    "raw_segment_hash": hashlib.sha256(
                        atom.atom.raw_segment.encode("utf-8")
                    ).hexdigest(),
                    "used_skills": list(atom.atom.used_skills),
                    "source_model": atom.source_model,
                    "source_harness": atom.source_harness,
                }
                for atom in self.atoms
            ],
            "usage_events": [event.to_record() for event in self.usage_events],
            "explicit_outcome": self.explicit_outcome,
            "continuity": {
                "run_id": self.metadata.get("run_id"),
                "session_id": self.metadata.get("session_id"),
            },
        })


def _execution_identity(metadata: dict, trajectory: dict) -> tuple[dict, dict]:
    provider = (
        metadata.get("provider") or metadata.get("source_provider")
        or metadata.get("model_provider")
    )
    model_id = (
        metadata.get("model_id") or metadata.get("model")
        or metadata.get("source_model") or trajectory.get("source_model")
    )
    model = model_from_dict({
        "provider": provider or "unavailable",
        "model_id": model_id or "unavailable",
        "version": metadata.get("model_version") or metadata.get("model_snapshot"),
        "unavailable_reason": None if model_id else "source_did_not_report_model",
    })
    harness_name = (
        trajectory.get("source_harness") or metadata.get("source_harness")
        or metadata.get("harness")
    )
    harness = model_from_dict({
        "name": harness_name or metadata.get("source") or "unavailable",
        "version": (
            metadata.get("harness_version") or metadata.get("cli_version")
            or metadata.get("version")
        ),
        "unavailable_reason": (
            None if harness_name or metadata.get("source")
            else "source_did_not_report_harness"
        ),
    })
    return model, harness


def _normalize_usage_event(
    raw: dict,
    *,
    index: int,
    session_ref: SessionRef,
    default_model: dict,
    default_harness: dict,
) -> ExecutionUsageEvent:
    usage = raw.get("usage") if isinstance(raw.get("usage"), dict) else raw
    prompt = _first_number(usage, (
        "prompt_tokens", "input_tokens", "inputTokens", "input",
    ))
    completion = _first_number(usage, (
        "completion_tokens", "output_tokens", "outputTokens", "output",
    ))
    total = _first_number(usage, ("total_tokens", "totalTokens", "total"))
    if total is None and (prompt is not None or completion is not None):
        total = (prompt or 0) + (completion or 0)
    cache_read = _first_number(usage, (
        "cache_read_tokens", "cached_input_tokens", "cacheRead", "cache_read",
    ))
    cost_value = raw.get("cost_usd")
    if cost_value is None:
        cost_container = raw.get("cost")
        if cost_container is None:
            cost_container = usage.get("cost")
        if isinstance(cost_container, dict):
            cost_value = cost_container.get("total")
    cost = _number(cost_value, integer=False)
    source_event_id = str(
        raw.get("source_event_id") or raw.get("event_id") or raw.get("response_id")
        or raw.get("run_id") or f"usage-{index}"
    )
    event_seed = "\x1f".join((
        session_ref.tenant_id, session_ref.task_scope_id, session_ref.source_scope_id,
        session_ref.traj_id, source_event_id,
    ))
    available = any(
        value is not None
        for value in (prompt, completion, total, cache_read, cost)
    )
    raw_quality = raw.get("measurement_quality")
    measurement_quality = (
        str(raw_quality)
        if raw_quality is not None
        else "measured" if available else "unavailable"
    )
    estimation_method = str(raw.get("estimation_method") or "")
    if measurement_quality == "unavailable":
        unavailable_reason = str(
            raw.get("unavailable_reason") or "source_did_not_report_usage"
        )
    else:
        missing_fields = [
            field_name
            for field_name, field_value in (
                ("prompt_tokens", prompt),
                ("completion_tokens", completion),
                ("total_tokens", total),
                ("cost_usd", cost),
            )
            if field_value is None
        ]
        unavailable_reason = str(raw.get("unavailable_reason") or "")
        if missing_fields and not unavailable_reason:
            unavailable_reason = "source_did_not_report:" + ",".join(
                missing_fields
            )
    return ExecutionUsageEvent(
        usage_event_id=f"use_{hashlib.sha256(event_seed.encode('utf-8')).hexdigest()[:32]}",
        source_event_id=source_event_id,
        session_ref=session_ref,
        model=_overlay_identity(default_model, raw.get("model")),
        harness=_overlay_identity(default_harness, raw.get("harness")),
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=total,
        cache_read_tokens=cache_read,
        cost_usd=float(cost) if cost is not None else None,
        measurement_quality=measurement_quality,
        estimation_method=estimation_method,
        unavailable_reason=unavailable_reason,
        observed_at=str(raw.get("observed_at") or "unavailable"),
    )


def _usage_events(
    metadata: dict,
    *,
    session_ref: SessionRef,
    model: dict,
    harness: dict,
) -> tuple[ExecutionUsageEvent, ...]:
    raw_events = metadata.get("execution_usage_events")
    if not isinstance(raw_events, list):
        raw_usage = metadata.get("usage") or metadata.get("token_usage")
        raw_events = [raw_usage] if isinstance(raw_usage, dict) else []
    events = []
    seen: dict[str, dict] = {}
    for index, raw in enumerate(raw_events):
        if not isinstance(raw, dict):
            continue
        event = _normalize_usage_event(
            raw,
            index=index,
            session_ref=session_ref,
            default_model=model,
            default_harness=harness,
        )
        record = event.to_record()
        previous = seen.get(event.usage_event_id)
        if previous is not None:
            if previous != record:
                raise RuntimeError(
                    "execution usage source_event_id is duplicated with "
                    f"different content: {event.source_event_id}"
                )
            continue
        seen[event.usage_event_id] = record
        events.append(event)
    if not events:
        events.append(_normalize_usage_event(
            {
                "source_event_id": "session-usage-unavailable",
                "measurement_quality": "unavailable",
                "unavailable_reason": "source_did_not_report_usage",
            },
            index=0,
            session_ref=session_ref,
            default_model=model,
            default_harness=harness,
        ))
    return tuple(events)


def _explicit_outcome(metadata: dict) -> dict:
    value = (
        metadata.get("final_status") or metadata.get("exit_status")
        or metadata.get("result_status") or metadata.get("session_status")
    )
    if value is None and isinstance(metadata.get("result"), dict):
        value = metadata["result"].get("status")
    normalized = str(value or "").strip().lower()
    if normalized in {"success", "succeeded", "completed", "passed", "ok"}:
        return {
            "outcome": "succeeded",
            "verification": "unverified",
            "source": "structured_harness_status",
        }
    if normalized in {"partial", "partially_succeeded", "partially-succeeded"}:
        return {
            "outcome": "partially_succeeded",
            "verification": "unverified",
            "source": "structured_harness_status",
        }
    if normalized in {"failure", "failed", "error", "timed_out", "timeout"}:
        return {"outcome": "failed", "verification": "contradicted", "source": "structured_harness_status"}
    if normalized in {"cancelled", "canceled", "aborted"}:
        return {"outcome": "cancelled", "verification": "unverified", "source": "structured_harness_status"}
    return {}


def collect_trajectory_evidence(
    *, watch_dir: dict, trajectory: dict, scope: ScopeIdentity,
) -> ScopedTrajectoryEvidence:
    filename = str(trajectory["filename"])
    traj_id = filename.removesuffix(".md")
    root = Path(watch_dir["path"])
    markdown_path = root / filename
    metadata = _read_object(root / f"{traj_id}.json")
    try:
        markdown_payload = markdown_path.read_bytes()
        observed_at = datetime.fromtimestamp(
            markdown_path.stat().st_mtime, tz=timezone.utc,
        ).isoformat(timespec="milliseconds")
    except OSError as error:
        raise FileNotFoundError(f"trajectory evidence is unavailable: {markdown_path}") from error
    session_hash = hashlib.sha256(markdown_payload).hexdigest()
    session_ref = SessionRef(
        tenant_id=scope.tenant_id,
        task_scope_id=scope.task_scope_id,
        source_scope_id=scope.source_scope_id,
        traj_id=traj_id,
    )
    model, harness = _execution_identity(metadata, trajectory)
    atoms = []
    for atom in AtomTaskStore(root).list_by_traj(traj_id):
        atom_ref = AtomRef(
            tenant_id=scope.tenant_id,
            task_scope_id=scope.task_scope_id,
            source_scope_id=scope.source_scope_id,
            traj_id=traj_id,
            atom_id=atom.atom_id,
        )
        atom_hash = _hash_json({
            "intent": atom.intent,
            "summary": atom.summary,
        })
        atom_model = dict(model)
        if atom.source_model:
            atom_model["model_id"] = atom.source_model
            atom_model.pop("unavailable_reason", None)
        atoms.append(ScopedAtomEvidence(
            atom=atom,
            atom_ref=atom_ref,
            atom_hash=atom_hash,
            session_hash=session_hash,
            source_model=atom_model,
            source_harness=harness,
            observed_at=observed_at,
        ))
    return ScopedTrajectoryEvidence(
        watch_dir_id=int(watch_dir["id"]),
        watch_dir_path=root,
        filename=filename,
        scope=scope,
        session_ref=session_ref,
        session_hash=session_hash,
        metadata=metadata,
        atoms=tuple(atoms),
        usage_events=_usage_events(
            metadata, session_ref=session_ref, model=model, harness=harness,
        ),
        explicit_outcome=_explicit_outcome(metadata),
    )
