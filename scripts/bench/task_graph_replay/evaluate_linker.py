"""Evaluate the production Task linker against structural grouping baselines."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from xskill.pipeline.atom import AtomTask
from xskill.tasks.evidence import ScopedAtomEvidence, ScopedTrajectoryEvidence
from xskill.tasks.linker import BoundedTaskLinker
from xskill.tasks.models import (
    ATTEMPT_RELATION_TYPES,
    AtomRef,
    SessionRef,
    TaskAttempt,
)
from xskill.tasks.scopes import ScopeIdentity

SCHEMA_VERSION = 1
ATTEMPT_DECISIONS = frozenset(("confirmed", "proposed"))


class LinkerReplayValidationError(ValueError):
    """Raised when a structural linker fixture violates its contract."""


def _require(mapping: dict[str, Any], key: str, expected: type, context: str) -> Any:
    if key not in mapping:
        raise LinkerReplayValidationError(f"{context}: missing required field {key!r}")
    value = mapping[key]
    if expected is int and isinstance(value, bool):
        raise LinkerReplayValidationError(f"{context}.{key}: expected int, got bool")
    if not isinstance(value, expected):
        raise LinkerReplayValidationError(
            f"{context}.{key}: expected {expected.__name__}, got {type(value).__name__}"
        )
    return value


def _positive_integer(value: Any, context: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise LinkerReplayValidationError(f"{context}: expected a positive integer")
    return value


def validate_suite(suite: Any) -> None:
    if not isinstance(suite, dict):
        raise LinkerReplayValidationError("suite: expected an object")
    version = _require(suite, "schema_version", int, "suite")
    if version != SCHEMA_VERSION:
        raise LinkerReplayValidationError(
            f"suite.schema_version: supported={SCHEMA_VERSION}, got={version}"
        )
    suite_id = _require(suite, "suite_id", str, "suite")
    if not suite_id.strip():
        raise LinkerReplayValidationError("suite.suite_id: expected a non-empty string")
    manifest = _require(suite, "run_manifest", dict, "suite")
    for key in ("repository_revision", "generated_at", "fixture_kind"):
        if not _require(manifest, key, str, "suite.run_manifest"):
            raise LinkerReplayValidationError(
                f"suite.run_manifest.{key}: expected a non-empty string"
            )
    linker_config = _require(suite, "linker_config", dict, "suite")
    expected_config_keys = {"top_k", "recent_k", "posting_cap"}
    if set(linker_config) != expected_config_keys:
        raise LinkerReplayValidationError(
            f"suite.linker_config: expected exactly {sorted(expected_config_keys)!r}"
        )
    for key in ("top_k", "recent_k", "posting_cap"):
        _positive_integer(
            _require(linker_config, key, int, "suite.linker_config"),
            f"suite.linker_config.{key}",
        )
    cases = _require(suite, "cases", list, "suite")
    if not cases:
        raise LinkerReplayValidationError("suite.cases must not be empty")
    seen_case_ids: set[str] = set()
    for case_index, case in enumerate(cases):
        context = f"suite.cases[{case_index}]"
        if not isinstance(case, dict):
            raise LinkerReplayValidationError(f"{context}: expected an object")
        case_id = _require(case, "case_id", str, context)
        if not case_id or case_id in seen_case_ids:
            raise LinkerReplayValidationError(f"{context}.case_id: empty or duplicate")
        seen_case_ids.add(case_id)
        sessions = _require(case, "sessions", list, context)
        if not sessions:
            raise LinkerReplayValidationError(f"{context}.sessions must not be empty")
        atom_ids: set[str] = set()
        atom_ranges: dict[str, tuple[int, int]] = {}
        session_ids: set[tuple[str, str]] = set()
        for session_index, session in enumerate(sessions):
            session_context = f"{context}.sessions[{session_index}]"
            if not isinstance(session, dict):
                raise LinkerReplayValidationError(
                    f"{session_context}: expected an object"
                )
            source_scope_id = _require(session, "source_scope_id", str, session_context)
            traj_id = _require(session, "traj_id", str, session_context)
            if not source_scope_id or not traj_id:
                raise LinkerReplayValidationError(
                    f"{session_context}: source_scope_id and traj_id must be non-empty"
                )
            session_key = source_scope_id, traj_id
            if session_key in session_ids:
                raise LinkerReplayValidationError(
                    f"{session_context}: duplicate source_scope_id/traj_id"
                )
            session_ids.add(session_key)
            atoms = _require(session, "atoms", list, session_context)
            if not atoms:
                raise LinkerReplayValidationError(
                    f"{session_context}.atoms must not be empty"
                )
            offset = 1
            for atom_index, atom in enumerate(atoms):
                atom_context = f"{session_context}.atoms[{atom_index}]"
                if not isinstance(atom, dict):
                    raise LinkerReplayValidationError(
                        f"{atom_context}: expected an object"
                    )
                atom_id = _require(atom, "atom_id", str, atom_context)
                if not atom_id or atom_id in atom_ids:
                    raise LinkerReplayValidationError(
                        f"{atom_context}.atom_id: empty or duplicate"
                    )
                atom_ids.add(atom_id)
                for key in ("intent", "summary", "raw_segment"):
                    if not _require(atom, key, str, atom_context):
                        raise LinkerReplayValidationError(
                            f"{atom_context}.{key}: expected a non-empty string"
                        )
                raw_segment = atom["raw_segment"]
                span = max(1, len(raw_segment.splitlines()))
                atom_ranges[atom_id] = (offset, offset + span)
                offset += span
                skills = atom.get("skills", [])
                if not isinstance(skills, list) or not all(
                    isinstance(skill, str) and skill.strip() for skill in skills
                ):
                    raise LinkerReplayValidationError(
                        f"{atom_context}.skills: expected non-empty strings"
                    )
        gold = _require(case, "gold_memberships", list, context)
        gold_by_atom: dict[str, str] = {}
        for membership_index, membership in enumerate(gold):
            membership_context = f"{context}.gold_memberships[{membership_index}]"
            if not isinstance(membership, dict):
                raise LinkerReplayValidationError(
                    f"{membership_context}: expected an object"
                )
            atom_id = _require(membership, "atom_id", str, membership_context)
            task_id = _require(membership, "task_id", str, membership_context)
            if atom_id not in atom_ids or atom_id in gold_by_atom or not task_id:
                raise LinkerReplayValidationError(
                    f"{membership_context}: invalid Atom membership"
                )
            gold_by_atom[atom_id] = task_id
        if set(gold_by_atom) != atom_ids:
            raise LinkerReplayValidationError(
                f"{context}.gold_memberships: every Atom needs one membership"
            )
        expected_attempts = _require(case, "expected_attempt_count", int, context)
        if expected_attempts < 0:
            raise LinkerReplayValidationError(
                f"{context}.expected_attempt_count must be non-negative"
            )
        relations = _require(case, "expected_attempt_relations", list, context)
        for relation_index, relation in enumerate(relations):
            relation_context = f"{context}.expected_attempt_relations[{relation_index}]"
            if not isinstance(relation, dict):
                raise LinkerReplayValidationError(
                    f"{relation_context}: expected an object"
                )
            relation_type = _require(relation, "relation_type", str, relation_context)
            if relation_type not in ATTEMPT_RELATION_TYPES:
                raise LinkerReplayValidationError(
                    f"{relation_context}.relation_type: unsupported value "
                    f"{relation_type!r}"
                )
            decision = _require(relation, "decision", str, relation_context)
            if decision not in ATTEMPT_DECISIONS:
                raise LinkerReplayValidationError(
                    f"{relation_context}.decision: unsupported value {decision!r}"
                )
            endpoint_signatures = []
            for endpoint in ("from_evidence", "to_evidence"):
                evidence_specs = _require(relation, endpoint, list, relation_context)
                if not evidence_specs:
                    raise LinkerReplayValidationError(
                        f"{relation_context}.{endpoint}: must not be empty"
                    )
                signature = []
                for evidence_index, evidence in enumerate(evidence_specs):
                    evidence_context = (
                        f"{relation_context}.{endpoint}[{evidence_index}]"
                    )
                    if not isinstance(evidence, dict):
                        raise LinkerReplayValidationError(
                            f"{evidence_context}: expected an object"
                        )
                    atom_id = _require(evidence, "atom_id", str, evidence_context)
                    start = _require(evidence, "start", int, evidence_context)
                    end = _require(evidence, "end", int, evidence_context)
                    if atom_id not in atom_ranges:
                        raise LinkerReplayValidationError(
                            f"{evidence_context}.atom_id: unknown Atom {atom_id!r}"
                        )
                    atom_start, atom_end = atom_ranges[atom_id]
                    if not atom_start <= start < end <= atom_end:
                        raise LinkerReplayValidationError(
                            f"{evidence_context}: range must stay within "
                            f"[{atom_start}, {atom_end})"
                        )
                    signature.append((atom_id, start, end))
                if len(set(signature)) != len(signature):
                    raise LinkerReplayValidationError(
                        f"{relation_context}.{endpoint}: duplicate evidence range"
                    )
                endpoint_signatures.append(tuple(sorted(signature)))
            if endpoint_signatures[0] == endpoint_signatures[1]:
                raise LinkerReplayValidationError(
                    f"{relation_context}: Attempt relation endpoints must differ"
                )
            endpoint_gold_tasks = [
                {gold_by_atom[atom_id] for atom_id, _start, _end in signature}
                for signature in endpoint_signatures
            ]
            if (
                len(endpoint_gold_tasks[0]) != 1
                or endpoint_gold_tasks[0] != endpoint_gold_tasks[1]
            ):
                raise LinkerReplayValidationError(
                    f"{relation_context}: Attempt relation endpoints must belong "
                    "to the same gold Task"
                )


def load_suite(path: Path | str) -> dict[str, Any]:
    suite_path = Path(path)
    try:
        suite = json.loads(suite_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise LinkerReplayValidationError(
            f"invalid JSON in {suite_path}: {error}"
        ) from error
    validate_suite(suite)
    return suite


def _hash(value: Any) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _compile_case(case: dict[str, Any], case_index: int):
    tenant_id = "tenant-linker-replay"
    task_scope_id = f"task-scope-{case['case_id']}"
    scope = ScopeIdentity(
        tenant_id=tenant_id,
        task_scope_id=task_scope_id,
        source_scope_id="source-placeholder",
        actor_id="actor-linker-replay",
        workspace_id=f"workspace-{case['case_id']}",
    )
    trajectories = []
    session_assignment = {}
    atom_ids = []
    base_time = datetime(2026, 8, 30, tzinfo=timezone.utc) + timedelta(
        minutes=case_index
    )
    for session_index, session in enumerate(case["sessions"]):
        source_scope_id = session["source_scope_id"]
        traj_id = session["traj_id"]
        session_scope = ScopeIdentity(
            tenant_id=scope.tenant_id,
            task_scope_id=scope.task_scope_id,
            source_scope_id=source_scope_id,
            actor_id=scope.actor_id,
            workspace_id=scope.workspace_id,
        )
        session_ref = SessionRef(
            tenant_id=tenant_id,
            task_scope_id=task_scope_id,
            source_scope_id=source_scope_id,
            traj_id=traj_id,
        )
        observed_at = (base_time + timedelta(seconds=session_index)).isoformat()
        atom_specs = session["atoms"]
        atoms = []
        session_atom_ids = []
        offset = 1
        for atom_index, atom_spec in enumerate(atom_specs):
            atom_id = atom_spec["atom_id"]
            raw_segment = atom_spec["raw_segment"]
            span = max(1, len(raw_segment.splitlines()))
            atom = AtomTask(
                atom_id=atom_id,
                traj_id=traj_id,
                offset_start=offset,
                offset_end=offset + span,
                intent=atom_spec["intent"],
                summary=atom_spec["summary"],
                used_skills=list(atom_spec.get("skills") or ()),
                pre_atom_id=(
                    atom_specs[atom_index - 1]["atom_id"] if atom_index > 0 else None
                ),
                post_atom_id=(
                    atom_specs[atom_index + 1]["atom_id"]
                    if atom_index + 1 < len(atom_specs)
                    else None
                ),
                raw_segment=raw_segment,
                source_model="recorded-fixture",
            )
            atom_ref = AtomRef(
                tenant_id=tenant_id,
                task_scope_id=task_scope_id,
                source_scope_id=source_scope_id,
                traj_id=traj_id,
                atom_id=atom_id,
            )
            atoms.append(
                ScopedAtomEvidence(
                    atom=atom,
                    atom_ref=atom_ref,
                    atom_hash=_hash({"intent": atom.intent, "summary": atom.summary}),
                    session_hash=_hash(
                        {"source_scope_id": source_scope_id, "traj_id": traj_id}
                    ),
                    source_model={
                        "provider": "fixture",
                        "model_id": "recorded-fixture",
                    },
                    source_harness={"name": "offline-linker-replay"},
                    observed_at=observed_at,
                )
            )
            session_assignment[atom_id] = f"{source_scope_id}:{traj_id}"
            atom_ids.append(atom_id)
            session_atom_ids.append(atom_id)
            offset += span
        session_hash = _hash(
            {
                "source_scope_id": source_scope_id,
                "traj_id": traj_id,
                "atoms": session_atom_ids,
            }
        )
        trajectories.append(
            ScopedTrajectoryEvidence(
                watch_dir_id=session_index + 1,
                watch_dir_path=Path(f"/fixture/{source_scope_id}"),
                filename=f"{traj_id}.md",
                scope=session_scope,
                session_ref=session_ref,
                session_hash=session_hash,
                metadata={},
                atoms=tuple(atoms),
                usage_events=(),
                explicit_outcome={},
            )
        )
    return tuple(trajectories), session_assignment, atom_ids


def _partition_counts(gold: dict[str, str], predicted: dict[str, str]) -> dict:
    gold_sizes = Counter(gold.values())
    predicted_sizes = Counter(predicted.values())
    contingency = Counter((gold[atom_id], predicted[atom_id]) for atom_id in gold)
    true_positive_pairs = sum(
        count * (count - 1) // 2 for count in contingency.values()
    )
    gold_pairs = sum(count * (count - 1) // 2 for count in gold_sizes.values())
    predicted_pairs = sum(
        count * (count - 1) // 2 for count in predicted_sizes.values()
    )
    b3_precision_sum = 0.0
    b3_recall_sum = 0.0
    for atom_id, gold_task_id in gold.items():
        predicted_task_id = predicted[atom_id]
        overlap = contingency[(gold_task_id, predicted_task_id)]
        b3_precision_sum += overlap / predicted_sizes[predicted_task_id]
        b3_recall_sum += overlap / gold_sizes[gold_task_id]
    return {
        "atom_count": len(gold),
        "gold_pairs": gold_pairs,
        "predicted_pairs": predicted_pairs,
        "true_positive_pairs": true_positive_pairs,
        "b3_precision_sum": b3_precision_sum,
        "b3_recall_sum": b3_recall_sum,
    }


def _ratio(numerator: float, denominator: float, *, empty: float = 1.0) -> float:
    return round(numerator / denominator, 6) if denominator else empty


def _public_partition(counts: dict) -> dict:
    true_positive = counts["true_positive_pairs"]
    false_positive = counts["predicted_pairs"] - true_positive
    false_negative = counts["gold_pairs"] - true_positive
    precision = _ratio(true_positive, true_positive + false_positive)
    recall = _ratio(true_positive, true_positive + false_negative)
    pairwise_f1 = (
        2 * precision * recall / (precision + recall) if precision + recall else 0.0
    )
    b3_precision = _ratio(counts["b3_precision_sum"], counts["atom_count"])
    b3_recall = _ratio(counts["b3_recall_sum"], counts["atom_count"])
    b3_f1 = (
        2 * b3_precision * b3_recall / (b3_precision + b3_recall)
        if b3_precision + b3_recall
        else 0.0
    )
    return {
        "pairwise": {
            "true_positive": true_positive,
            "false_positive": false_positive,
            "false_negative": false_negative,
            "precision": precision,
            "recall": recall,
            "f1": round(pairwise_f1, 6),
        },
        "b3": {
            "precision": b3_precision,
            "recall": b3_recall,
            "f1": round(b3_f1, 6),
        },
    }


def _merge_partition_counts(target: dict, source: dict) -> None:
    for key, value in source.items():
        target[key] = target.get(key, 0) + value


def _proposal_counts(
    gold: dict[str, str],
    confirmed: dict[str, str],
    proposed: list[tuple[str, str]],
) -> tuple[dict, dict[str, str]]:
    confirmed_clusters: dict[str, set[str]] = defaultdict(set)
    for atom_id, task_id in confirmed.items():
        confirmed_clusters[task_id].add(atom_id)
    cluster_gold_tasks: dict[str, set[str]] = defaultdict(set)
    for atom_id, task_id in confirmed.items():
        cluster_gold_tasks[task_id].add(gold[atom_id])
    proposals_by_atom: dict[str, list[str]] = defaultdict(list)
    useful_proposals = []
    for atom_id, candidate_task_id in proposed:
        proposals_by_atom[atom_id].append(candidate_task_id)
        candidate_atoms = confirmed_clusters.get(candidate_task_id, set())
        source_task_id = confirmed[atom_id]
        expected_gold = {gold[atom_id]}
        if (
            candidate_atoms
            and atom_id not in candidate_atoms
            and cluster_gold_tasks[source_task_id] == expected_gold
            and cluster_gold_tasks[candidate_task_id] == expected_gold
        ):
            useful_proposals.append((atom_id, candidate_task_id))

    parent = {task_id: task_id for task_id in set(confirmed.values())}

    def find(task_id: str) -> str:
        while parent[task_id] != task_id:
            parent[task_id] = parent[parent[task_id]]
            task_id = parent[task_id]
        return task_id

    def union(left: str, right: str) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root != right_root:
            parent[max(left_root, right_root)] = min(left_root, right_root)

    for atom_id, candidate_task_id in set(useful_proposals):
        union(confirmed[atom_id], candidate_task_id)
    oracle_review = {atom_id: find(task_id) for atom_id, task_id in confirmed.items()}
    confirmed_counts = _partition_counts(gold, confirmed)
    oracle_counts = _partition_counts(gold, oracle_review)
    false_split_pair_count = (
        confirmed_counts["gold_pairs"] - confirmed_counts["true_positive_pairs"]
    )
    recoverable_pair_count = (
        oracle_counts["true_positive_pairs"] - confirmed_counts["true_positive_pairs"]
    )
    return (
        {
            "proposed": len(proposed),
            "useful": len(useful_proposals),
            "false_split_pairs": false_split_pair_count,
            "recoverable_false_split_pairs": recoverable_pair_count,
            "atoms_with_proposals": len(proposals_by_atom),
            "max_proposals_per_atom": max(
                (len(candidates) for candidates in proposals_by_atom.values()),
                default=0,
            ),
            "atom_count": len(gold),
        },
        oracle_review,
    )


def _public_proposals(counts: dict) -> dict:
    return {
        "proposed": counts["proposed"],
        "useful": counts["useful"],
        "precision": _ratio(counts["useful"], counts["proposed"], empty=1.0),
        "false_split_pairs": counts["false_split_pairs"],
        "recoverable_false_split_pairs": counts["recoverable_false_split_pairs"],
        "recoverable_recall": _ratio(
            counts["recoverable_false_split_pairs"],
            counts["false_split_pairs"],
            empty=1.0,
        ),
        "atoms_with_proposals": counts["atoms_with_proposals"],
        "candidates_per_atom": _ratio(
            counts["proposed"], counts["atom_count"], empty=0.0
        ),
        "max_proposals_per_atom": counts["max_proposals_per_atom"],
    }


def _relation_counts(expected: Counter, predicted: Counter) -> dict:
    true_positive = sum((expected & predicted).values())
    predicted_total = sum(predicted.values())
    expected_total = sum(expected.values())
    return {
        "true_positive": true_positive,
        "false_positive": predicted_total - true_positive,
        "false_negative": expected_total - true_positive,
    }


def _attempt_support(attempt: TaskAttempt) -> tuple[tuple[str, int, int], ...]:
    return tuple(
        sorted(
            (
                evidence.atom_ref.atom_id,
                evidence.start,
                evidence.end,
            )
            for evidence in attempt.evidence_ranges
            if evidence.atom_ref is not None and not evidence.stale
        )
    )


def _fixture_relation_key(relation: dict[str, Any]) -> tuple:
    def endpoint(name: str) -> tuple[tuple[str, int, int], ...]:
        return tuple(
            sorted(
                (item["atom_id"], item["start"], item["end"]) for item in relation[name]
            )
        )

    return (
        endpoint("from_evidence"),
        endpoint("to_evidence"),
        relation["relation_type"],
        relation["decision"],
    )


def _public_relation_keys(relations: Counter) -> list[dict[str, Any]]:
    result = []
    for from_evidence, to_evidence, relation_type, decision in sorted(
        relations.elements()
    ):
        result.append(
            {
                "from_evidence": [
                    {"atom_id": atom_id, "start": start, "end": end}
                    for atom_id, start, end in from_evidence
                ],
                "to_evidence": [
                    {"atom_id": atom_id, "start": start, "end": end}
                    for atom_id, start, end in to_evidence
                ],
                "relation_type": relation_type,
                "decision": decision,
            }
        )
    return result


def _public_relation(counts: dict) -> dict:
    true_positive = counts["true_positive"]
    false_positive = counts["false_positive"]
    false_negative = counts["false_negative"]
    precision = _ratio(true_positive, true_positive + false_positive)
    recall = _ratio(true_positive, true_positive + false_negative)
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        **counts,
        "precision": precision,
        "recall": recall,
        "f1": round(f1, 6),
    }


def _evaluate_case(
    case: dict[str, Any],
    case_index: int,
    linker: BoundedTaskLinker,
) -> tuple[dict, dict]:
    trajectories, session_assignment, atom_ids = _compile_case(case, case_index)
    generation = linker.build(
        tenant_id="tenant-linker-replay",
        task_scope_id=f"task-scope-{case['case_id']}",
        trajectories=trajectories,
        source_revision=f"sha256:{_hash(case)}",
    )
    gold = {
        membership["atom_id"]: membership["task_id"]
        for membership in case["gold_memberships"]
    }
    confirmed = {
        membership.atom_ref.atom_id: membership.task_id
        for membership in generation.memberships
        if membership.role == "primary"
        and membership.decision == "confirmed"
        and not membership.stale
    }
    if set(confirmed) != set(gold):
        raise RuntimeError(
            f"{case['case_id']}: production linker omitted confirmed Atom memberships"
        )
    proposed = [
        (membership.atom_ref.atom_id, membership.task_id)
        for membership in generation.memberships
        if membership.role == "primary"
        and membership.decision == "proposed"
        and not membership.stale
    ]
    proposal_counts, oracle_review = _proposal_counts(gold, confirmed, proposed)
    baselines = {
        "session_as_task": session_assignment,
        "atom_as_task": {atom_id: atom_id for atom_id in atom_ids},
        "task_graph": confirmed,
        "task_graph_oracle_review": oracle_review,
    }
    grouping_counts = {
        name: _partition_counts(gold, predicted)
        for name, predicted in baselines.items()
    }
    expected_relations = Counter(
        _fixture_relation_key(relation)
        for relation in case["expected_attempt_relations"]
    )
    attempt_support_by_id = {
        attempt.attempt_id: _attempt_support(attempt) for attempt in generation.attempts
    }
    predicted_relations = Counter(
        (
            attempt_support_by_id[relation.from_attempt_id],
            attempt_support_by_id[relation.to_attempt_id],
            relation.relation_type,
            relation.decision,
        )
        for relation in generation.attempt_relations
    )
    relation_counts = _relation_counts(expected_relations, predicted_relations)
    expected_attempt_count = case["expected_attempt_count"]
    attempt_count = len(generation.attempts)
    case_report = {
        "case_id": case["case_id"],
        "atom_count": len(atom_ids),
        "gold_task_count": len(set(gold.values())),
        "predicted_task_count": len(set(confirmed.values())),
        "grouping": {
            name: _public_partition(counts) for name, counts in grouping_counts.items()
        },
        "proposals": _public_proposals(proposal_counts),
        "attempts": {
            "expected": expected_attempt_count,
            "predicted": attempt_count,
            "absolute_error": abs(attempt_count - expected_attempt_count),
        },
        "attempt_relations": _public_relation(relation_counts),
        "expected_attempt_relations": _public_relation_keys(expected_relations),
        "predicted_attempt_relations": _public_relation_keys(predicted_relations),
    }
    raw_counts = {
        "grouping": grouping_counts,
        "proposals": proposal_counts,
        "relation": relation_counts,
        "attempt_absolute_error": abs(attempt_count - expected_attempt_count),
        "attempt_exact": int(attempt_count == expected_attempt_count),
    }
    return case_report, raw_counts


def evaluate_suite(suite: dict[str, Any]) -> dict[str, Any]:
    validate_suite(suite)
    linker = BoundedTaskLinker(**suite["linker_config"])
    case_results = [
        _evaluate_case(case, index, linker) for index, case in enumerate(suite["cases"])
    ]
    merged_grouping: dict[str, dict] = defaultdict(dict)
    merged_proposals = {
        "proposed": 0,
        "useful": 0,
        "false_split_pairs": 0,
        "recoverable_false_split_pairs": 0,
        "atoms_with_proposals": 0,
        "max_proposals_per_atom": 0,
        "atom_count": 0,
    }
    merged_relations = {
        "true_positive": 0,
        "false_positive": 0,
        "false_negative": 0,
    }
    attempt_absolute_error = 0
    attempt_exact_cases = 0
    for _case_report, counts in case_results:
        for name, grouping_counts in counts["grouping"].items():
            _merge_partition_counts(merged_grouping[name], grouping_counts)
        for key, value in counts["proposals"].items():
            if key == "max_proposals_per_atom":
                merged_proposals[key] = max(merged_proposals[key], value)
            else:
                merged_proposals[key] += value
        for key, value in counts["relation"].items():
            merged_relations[key] += value
        attempt_absolute_error += counts["attempt_absolute_error"]
        attempt_exact_cases += counts["attempt_exact"]
    report = {
        "schema_version": SCHEMA_VERSION,
        "suite_id": suite["suite_id"],
        "run_manifest": suite["run_manifest"],
        "linker": linker.generator_descriptor(),
        "metrics": {
            "grouping": {
                name: _public_partition(counts)
                for name, counts in sorted(merged_grouping.items())
            },
            "proposals": _public_proposals(merged_proposals),
            "attempts": {
                "case_count": len(case_results),
                "exact_cases": attempt_exact_cases,
                "exact_case_rate": _ratio(
                    attempt_exact_cases, len(case_results), empty=0.0
                ),
                "absolute_error": attempt_absolute_error,
            },
            "attempt_relations": _public_relation(merged_relations),
        },
        "cases": [case_report for case_report, _counts in case_results],
    }
    canonical_report = json.loads(
        json.dumps(report, ensure_ascii=False, sort_keys=True)
    )
    payload = json.dumps(canonical_report, ensure_ascii=False, sort_keys=True).encode(
        "utf-8"
    )
    canonical_report["report_sha256"] = hashlib.sha256(payload).hexdigest()
    return canonical_report


def render_text(report: dict[str, Any]) -> str:
    grouping = report["metrics"]["grouping"]
    proposals = report["metrics"]["proposals"]
    attempts = report["metrics"]["attempts"]
    relations = report["metrics"]["attempt_relations"]
    return "\n".join(
        (
            f"suite: {report['suite_id']}",
            f"linker: {report['linker']['version']}",
            (
                "session_as_task "
                f"pairwise_f1={grouping['session_as_task']['pairwise']['f1']:.3f} "
                f"b3_f1={grouping['session_as_task']['b3']['f1']:.3f}"
            ),
            (
                "atom_as_task "
                f"pairwise_f1={grouping['atom_as_task']['pairwise']['f1']:.3f} "
                f"b3_f1={grouping['atom_as_task']['b3']['f1']:.3f}"
            ),
            (
                "task_graph "
                f"pairwise_f1={grouping['task_graph']['pairwise']['f1']:.3f} "
                f"b3_f1={grouping['task_graph']['b3']['f1']:.3f}"
            ),
            (
                "oracle_review_upper_bound "
                f"pairwise_f1={grouping['task_graph_oracle_review']['pairwise']['f1']:.3f} "
                f"b3_f1={grouping['task_graph_oracle_review']['b3']['f1']:.3f}"
            ),
            (
                f"proposal_precision={proposals['precision']:.3f} "
                f"recoverable_recall={proposals['recoverable_recall']:.3f} "
                f"candidates_per_atom={proposals['candidates_per_atom']:.3f}"
            ),
            (
                f"attempt_exact_case_rate={attempts['exact_case_rate']:.3f} "
                f"attempt_relation_f1={relations['f1']:.3f}"
            ),
            f"report_sha256={report['report_sha256']}",
        )
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Evaluate xskill's production Task linker on structural cases."
    )
    parser.add_argument("suite", type=Path, help="Path to the linker replay JSON")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    report = evaluate_suite(load_suite(args.suite))
    if args.format == "json":
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(render_text(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
