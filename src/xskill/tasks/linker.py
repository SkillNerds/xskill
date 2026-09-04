"""Deterministic bounded production linker for Logical Tasks and Attempts."""
from __future__ import annotations

import hashlib
import logging
import math
import re
import unicodedata
import uuid
from collections import defaultdict, deque
from collections.abc import Iterable
from dataclasses import dataclass, replace
from functools import partial
from operator import attrgetter

from xskill.tasks.adjudicator import (
    MAX_TASK_LINK_CANDIDATES,
    TaskLinkAdjudicator,
    TaskLinkCandidate,
    TaskLinkJudgement,
    TaskLinkQuestion,
)
from xskill.tasks.evidence import ScopedAtomEvidence, ScopedTrajectoryEvidence
from xskill.tasks.models import (
    AtomRef,
    AttemptRelation,
    DecisionRecord,
    EvidenceRange,
    LogicalTask,
    TaskAtomMembership,
    TaskAttempt,
    TaskGraphGeneration,
    TaskRelation,
    UsageAllocation,
    stable_ref_key,
)
from xskill.tasks.store import OverrideEvent, utc_now

ALGORITHM_VERSION = "bounded-rules-v1"
GENERATOR_NAME = "xskill.task_graph.linker"
MAX_SHARED_USAGE_ALLOCATION_EDGES = 4096
logger = logging.getLogger("xskill.task_graph")
_WORD_RE = re.compile(r"[a-z0-9_./+-]+", re.IGNORECASE)
_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]+")
_CONTINUATION_RE = re.compile(
    r"(?:继续|接着|续上|恢复|沿着|按照前面|continue|resume|carry\s+on)", re.IGNORECASE,
)
_RETRY_RE = re.compile(
    r"(?:重试|再试|重新执行|重新跑|retry|try\s+again|rerun)", re.IGNORECASE,
)
_CORRECTION_RE = re.compile(
    r"(?:纠正|更正|修正|改一下|不是.+是|"
    r"(?:^|[。！？!?\n]\s*)(?:(?<!对)不对(?!称)|错了|搞错了|有误)|"
    r"(?:刚才|前面|之前|上一个|这个).{0,20}"
    r"(?:(?<!对)不对(?!称)|错了|有误)|"
    r"correction|correct|fix\s+that)",
    re.IGNORECASE,
)
_STOP_TERMS = frozenset((
    "the", "a", "an", "to", "of", "and", "or", "is", "are", "in", "on",
    "for", "with", "this", "that", "it", "please", "帮我", "一下", "这个", "那个",
    "我们", "目前", "现在", "然后", "进行", "相关", "问题",
))


def _opaque(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _digest(prefix: str, *values: str) -> str:
    payload = "\x1f".join(values).encode("utf-8")
    return f"{prefix}_{hashlib.sha256(payload).hexdigest()[:32]}"


def _normalize(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text or "").lower()
    return " ".join(normalized.split())


def _terms(text: str, *, limit: int = 32) -> tuple[str, ...]:
    normalized = _normalize(text)
    terms: list[str] = []
    seen: set[str] = set()
    for match in _WORD_RE.finditer(normalized):
        term = match.group(0).strip("._/+-")
        if len(term) < 2 or term in _STOP_TERMS or term in seen:
            continue
        seen.add(term)
        terms.append(term)
        if len(terms) >= limit:
            return tuple(terms)
    for match in _CJK_RE.finditer(normalized):
        chunk = match.group(0)
        candidates = [chunk] if len(chunk) <= 4 else [chunk[index:index + 2] for index in range(len(chunk) - 1)]
        for term in candidates:
            if term in _STOP_TERMS or term in seen:
                continue
            seen.add(term)
            terms.append(term)
            if len(terms) >= limit:
                return tuple(terms)
    return tuple(terms)


def _similarity(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def _marker_kind(text: str) -> str:
    if _CORRECTION_RE.search(text):
        return "correction"
    if _RETRY_RE.search(text):
        return "retry"
    if _CONTINUATION_RE.search(text):
        return "continuation"
    return ""


def _atom_sort_key(atom: ScopedAtomEvidence) -> tuple:
    return (
        atom.observed_at,
        atom.atom_ref.source_scope_id,
        atom.atom_ref.traj_id,
        atom.atom.offset_start,
        atom.atom.atom_id,
    )


def _candidate_sort_key(item: tuple[str, float]) -> tuple[float, str]:
    return -item[1], item[0]


def _weighted_attempt_sort_key(item: tuple[TaskAttempt, int]) -> str:
    return item[0].attempt_id


@dataclass(frozen=True)
class _AttemptSegment:
    atom: ScopedAtomEvidence
    evidence: EvidenceRange
    marker: str
    previous_attempt_id: str | None = None


class _TaskAnchor:
    def __init__(self, *, term_cap: int = 256, intent_cap: int = 128):
        self.terms: set[str] = set()
        self.normalized_intents: set[str] = set()
        self.term_cap = term_cap
        self.intent_cap = intent_cap

    def add(self, atom: ScopedAtomEvidence) -> tuple[str, ...]:
        added_terms = []
        for term in _terms(f"{atom.atom.intent}\n{atom.atom.summary}"):
            if term in self.terms or len(self.terms) >= self.term_cap:
                continue
            self.terms.add(term)
            added_terms.append(term)
        normalized_intent = _normalize(atom.atom.intent)
        if (
            normalized_intent
            and (
                normalized_intent in self.normalized_intents
                or len(self.normalized_intents) < self.intent_cap
            )
        ):
            self.normalized_intents.add(normalized_intent)
        return tuple(added_terms)


class BoundedTaskLinker:
    """Link only bounded candidates and keep every uncertain alternative."""

    def __init__(self, *, top_k: int = 8, recent_k: int = 6,
                 posting_cap: int = 64,
                 adjudicator: TaskLinkAdjudicator | None = None,
                 max_model_judgements_per_build: int = 64):
        for name, value in (
            ("top_k", top_k),
            ("recent_k", recent_k),
            ("posting_cap", posting_cap),
            ("max_model_judgements_per_build", max_model_judgements_per_build),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        self.top_k = top_k
        self.recent_k = recent_k
        self.posting_cap = posting_cap
        self.adjudicator = adjudicator
        self.max_model_judgements_per_build = max_model_judgements_per_build
        self.auto_confirm_model_links = bool(
            adjudicator is not None
            and adjudicator.descriptor().get("auto_confirm", False)
        )

    def generator_descriptor(self) -> dict:
        """Return every input that can change deterministic linker output."""
        descriptor = {
            "name": GENERATOR_NAME,
            "version": ALGORITHM_VERSION,
            "top_k": self.top_k,
            "recent_k": self.recent_k,
            "posting_cap": self.posting_cap,
        }
        if self.adjudicator is not None:
            descriptor["adjudicator"] = {
                **self.adjudicator.descriptor(),
                "max_judgements_per_build": self.max_model_judgements_per_build,
            }
        return descriptor

    def build(
        self,
        *,
        tenant_id: str,
        task_scope_id: str,
        trajectories: Iterable[ScopedTrajectoryEvidence],
        previous: TaskGraphGeneration | None = None,
        overrides: Iterable[OverrideEvent] = (),
        source_revision: str,
    ) -> TaskGraphGeneration:
        now = utc_now()
        trajectory_list = tuple(trajectories)
        atoms = sorted(
            (atom for trajectory in trajectory_list for atom in trajectory.atoms),
            key=_atom_sort_key,
        )
        previous_tasks = {task.task_id: task for task in previous.tasks} if previous else {}
        previous_memberships = self._previous_memberships(previous)
        (
            previous_attempt_by_evidence,
            previous_attempt_ids_by_atom,
        ) = self._previous_attempt_indexes(previous)
        previous_membership_hash_by_atom = self._previous_membership_hash_by_atom(
            previous
        )
        tasks: dict[str, LogicalTask] = {
            task_id: replace(
                task,
                lifecycle="open",
                outcome="unknown",
                verification="unverified",
                user_disposition="unknown",
                decisions=tuple(
                    decision for decision in task.decisions
                    if decision.decided_by.startswith("human:")
                ),
                tombstoned=True,
            )
            for task_id, task in previous_tasks.items()
        }
        memberships: list[TaskAtomMembership] = []
        anchors: dict[str, _TaskAnchor] = {}
        postings: dict[str, deque[str]] = defaultdict(
            partial(deque, maxlen=self.posting_cap)
        )
        recent_by_session: dict[tuple[str, str], deque[str]] = defaultdict(
            partial(deque, maxlen=self.recent_k),
        )
        candidate_count = 0
        proposed_count = 0
        confirmed_reuse_count = 0
        changed_atom_count = 0
        model_judgement_count = 0
        model_judgement_failure_count = 0
        model_confirmed_count = 0
        model_proposed_count = 0
        model_needs_review_count = 0
        model_abstain_count = 0
        model_adjudications: list[dict] = []
        adjudicator_available = self.adjudicator is not None
        affected_task_ids: set[str] = set()
        atoms_by_key = {stable_ref_key(atom.atom_ref): atom for atom in atoms}

        reusable_memberships: dict[str, TaskAtomMembership] = {}
        for atom in atoms:
            atom_key = stable_ref_key(atom.atom_ref)
            old_membership = previous_memberships.get(atom_key)
            if (
                old_membership is None
                or old_membership.task_id not in tasks
                or previous_membership_hash_by_atom.get(atom_key) != atom.atom_hash
            ):
                continue
            reusable_memberships[atom_key] = old_membership
            task_id = old_membership.task_id
            tasks[task_id] = replace(tasks[task_id], tombstoned=False)
            anchor = anchors.setdefault(task_id, _TaskAnchor())
            anchor.add(atom)
        for task_id, anchor in anchors.items():
            self._index_anchor(postings, task_id, anchor)
        previous_attempt_ids_by_atom = {
            atom_key: attempt_ids
            for atom_key, attempt_ids in previous_attempt_ids_by_atom.items()
            if atom_key in reusable_memberships
        }

        for atom in atoms:
            atom_key = stable_ref_key(atom.atom_ref)
            old_membership = reusable_memberships.get(atom_key)
            if old_membership is not None:
                task_id = old_membership.task_id
                membership = replace(
                    old_membership,
                    membership_id=_digest("mem", task_id, atom_key, "primary"),
                    atom_ref=atom.atom_ref,
                    stale=False,
                )
                memberships.append(membership)
                recent_by_session[(atom.atom_ref.source_scope_id, atom.atom_ref.traj_id)].append(task_id)
                confirmed_reuse_count += 1
                continue

            changed_atom_count += 1
            candidates = self._candidates(atom, anchors, postings, recent_by_session)
            candidate_count += len(candidates)
            affected_task_ids.update(task_id for task_id, _score in candidates)
            marker = _marker_kind(atom.atom.intent)
            previous_membership = previous_memberships.get(atom_key)
            confirmed_task_id = (
                previous_membership.task_id
                if previous_membership is not None
                and previous_membership.task_id in tasks
                else self._confirmed_candidate(candidates, marker)
            )
            model_judgement: TaskLinkJudgement | None = None
            model_confirmed = False
            if (
                confirmed_task_id is None
                and previous_membership is None
                and adjudicator_available
                and model_judgement_count < self.max_model_judgements_per_build
                and self._should_adjudicate(atom, candidates, recent_by_session)
            ):
                model_judgement_count += 1
                model_question: TaskLinkQuestion | None = None
                try:
                    model_question = self._adjudication_question(
                        atom=atom,
                        candidates=candidates,
                        tasks=tasks,
                        recent_by_session=recent_by_session,
                        marker=marker,
                    )
                    model_judgement = self._adjudicate(model_question)
                except Exception as error:
                    model_judgement_failure_count += 1
                    adjudicator_available = False
                    if model_question is not None:
                        model_adjudications.append(
                            self._adjudication_audit(model_question, error=error)
                        )
                    logger.warning(
                        "Task link adjudication failed; using rules-only fallback: %s",
                        type(error).__name__,
                    )
                else:
                    model_adjudications.append(
                        self._adjudication_audit(
                            model_question,
                            judgement=model_judgement,
                        )
                    )
                    if model_judgement.decision == "abstain":
                        model_abstain_count += 1
                    if (
                        model_judgement.decision == "same_task"
                        and self.auto_confirm_model_links
                    ):
                        confirmed_task_id = model_judgement.task_id
                        model_confirmed = True
                        model_confirmed_count += 1
            if confirmed_task_id is None:
                task_id = _opaque("tsk")
                task = LogicalTask(
                    task_id=task_id,
                    title=(atom.atom.intent or atom.atom.summary or "Untitled task")[:240],
                    summary=(atom.atom.summary or atom.atom.intent)[:1000],
                    created_at=now,
                )
                tasks[task_id] = task
                affected_task_ids.add(task_id)
                memberships.append(TaskAtomMembership(
                    membership_id=_digest("mem", task_id, atom_key, "primary"),
                    task_id=task_id,
                    atom_ref=atom.atom_ref,
                    role="primary",
                    confidence=None,
                    decision="confirmed",
                    decided_by=(
                        self._model_decided_by("new_task")
                        if model_judgement is not None
                        and model_judgement.decision == "new_task"
                        else "rule:new_atom_boundary"
                    ),
                    algorithm_version=f"{ALGORITHM_VERSION}:uncalibrated",
                    evidence_refs=(),
                    observed_at=atom.observed_at,
                ))
                model_candidate_id = (
                    model_judgement.task_id
                    if model_judgement is not None
                    and model_judgement.decision in ("same_task", "abstain")
                    else None
                )
                for candidate_id, score in candidates:
                    is_model_candidate = candidate_id == model_candidate_id
                    if score < 0.18 and not is_model_candidate:
                        continue
                    membership_decision = (
                        "needs_review"
                        if is_model_candidate
                        and model_judgement.decision == "abstain"
                        else "proposed"
                    )
                    if membership_decision == "proposed":
                        proposed_count += 1
                    if (
                        is_model_candidate
                        and model_judgement.decision == "same_task"
                    ):
                        model_proposed_count += 1
                    elif membership_decision == "needs_review":
                        model_needs_review_count += 1
                    memberships.append(TaskAtomMembership(
                        membership_id=_digest("mem", candidate_id, atom_key, "proposed"),
                        task_id=candidate_id,
                        atom_ref=atom.atom_ref,
                        role="primary",
                        confidence=None,
                        decision=membership_decision,
                        decided_by=(
                            self._model_decided_by(model_judgement.decision)
                            if is_model_candidate
                            else f"heuristic:lexical_score={score:.4f}"
                        ),
                        algorithm_version=f"{ALGORITHM_VERSION}:uncalibrated",
                        evidence_refs=(),
                        observed_at=atom.observed_at,
                    ))
            else:
                task_id = confirmed_task_id
                affected_task_ids.add(task_id)
                tasks[task_id] = replace(tasks[task_id], tombstoned=False)
                stable_atom_reuse = previous_membership is not None
                if (
                    stable_atom_reuse
                    and previous_membership.decided_by.startswith("human:")
                ):
                    memberships.append(replace(
                        previous_membership,
                        membership_id=_digest(
                            "mem", task_id, atom_key, "primary",
                        ),
                        atom_ref=atom.atom_ref,
                        stale=False,
                    ))
                else:
                    memberships.append(TaskAtomMembership(
                        membership_id=_digest(
                            "mem", task_id, atom_key, "primary",
                        ),
                        task_id=task_id,
                        atom_ref=atom.atom_ref,
                        role="primary",
                        confidence=None,
                        decision="confirmed",
                        decided_by=(
                            "rule:stable_atom_identity"
                            if stable_atom_reuse
                            else (
                                self._model_decided_by("same_task")
                                if model_confirmed
                                else f"rule:explicit_{marker}"
                            )
                        ),
                        algorithm_version=f"{ALGORITHM_VERSION}:uncalibrated",
                        evidence_refs=(),
                        observed_at=atom.observed_at,
                    ))
            anchor = anchors.setdefault(task_id, _TaskAnchor())
            new_terms = anchor.add(atom)
            self._index_terms(postings, task_id, new_terms)
            recent_by_session[(atom.atom_ref.source_scope_id, atom.atom_ref.traj_id)].append(task_id)

        relations = list(previous.relations) if previous else []
        structural_operations = {
            "confirm_membership", "reject_membership", "upsert_task_relation",
            "reject_task_relation", "merge_tasks", "move_atoms", "split_task",
        }
        tasks, memberships, relations, _unused_attempts, _unused_attempt_relations = self._apply_overrides(
            tasks=tasks,
            memberships=memberships,
            relations=relations,
            attempts=[],
            attempt_relations=[],
            overrides=tuple(
                event for event in overrides if event.operation in structural_operations
            ),
            atoms_by_key=atoms_by_key,
        )
        active_task_ids = {
            membership.task_id
            for membership in memberships
            if membership.role == "primary"
            and membership.decision == "confirmed"
            and not membership.stale
        }
        tasks = {
            task_id: replace(
                task, tombstoned=task_id not in active_task_ids,
            )
            for task_id, task in tasks.items()
        }
        relations = [
            replace(
                relation,
                stale=(
                    tasks[relation.from_task_id].tombstoned
                    or tasks[relation.to_task_id].tombstoned
                ),
            )
            for relation in relations
            if relation.from_task_id in tasks and relation.to_task_id in tasks
        ]
        attempts, attempt_relations = self._build_attempts(
            tasks=tasks,
            memberships=memberships,
            atoms_by_key=atoms_by_key,
            trajectories=trajectory_list,
            previous_attempt_by_evidence=previous_attempt_by_evidence,
            previous_attempt_ids_by_atom=previous_attempt_ids_by_atom,
            now=now,
        )
        attempts, attempt_relations = self._carry_stale_attempts(
            previous=previous,
            tasks=tasks,
            attempts=attempts,
            attempt_relations=attempt_relations,
            now=now,
        )
        tasks, memberships, relations, attempts, attempt_relations = self._apply_overrides(
            tasks=tasks,
            memberships=memberships,
            relations=relations,
            attempts=attempts,
            attempt_relations=attempt_relations,
            overrides=tuple(
                event for event in overrides
                if event.operation in {
                    "set_task_state", "set_attempt_state",
                    "upsert_attempt_relation", "reject_attempt_relation",
                }
            ),
            atoms_by_key=atoms_by_key,
        )
        usage_allocations = self._allocate_usage(
            trajectory_list, memberships, attempts,
        )
        watermark = max((event.override_seq for event in overrides), default=0)
        generation = TaskGraphGeneration(
            generation_id=_opaque("gen"),
            tenant_id=tenant_id,
            task_scope_id=task_scope_id,
            source_revision=source_revision,
            generator=self.generator_descriptor(),
            base_override_seq=watermark,
            created_at=now,
            tasks=tuple(sorted(
                tasks.values(), key=attrgetter("created_at", "task_id"),
            )),
            memberships=tuple(sorted(
                memberships,
                key=attrgetter(
                    "atom_ref.source_scope_id", "atom_ref.traj_id",
                    "atom_ref.atom_id", "decision", "task_id",
                ),
            )),
            relations=tuple(sorted(relations, key=attrgetter("relation_id"))),
            attempts=tuple(sorted(
                attempts, key=attrgetter("started_at", "attempt_id"),
            )),
            attempt_relations=tuple(sorted(
                attempt_relations, key=attrgetter("relation_id"),
            )),
            usage_allocations=tuple(sorted(
                usage_allocations, key=attrgetter("allocation_id"),
            )),
            metrics={
                "atom_count": len(atoms),
                "task_count": sum(1 for task in tasks.values() if not task.tombstoned),
                "candidate_count": candidate_count,
                "model_judgement_count": model_judgement_count,
                "model_judgement_failure_count": model_judgement_failure_count,
                "model_confirmed_membership_count": model_confirmed_count,
                "model_proposed_membership_count": model_proposed_count,
                "model_needs_review_membership_count": model_needs_review_count,
                "model_abstain_judgement_count": model_abstain_count,
                "model_adjudications": model_adjudications,
                "proposed_membership_count": proposed_count,
                "reused_membership_count": confirmed_reuse_count,
                "max_candidates_per_atom": self.top_k,
                "changed_atom_count": changed_atom_count,
                "affected_component_size": len(affected_task_ids),
            },
        )
        return generation

    @staticmethod
    def _previous_memberships(
        previous: TaskGraphGeneration | None,
    ) -> dict[str, TaskAtomMembership]:
        if previous is None:
            return {}
        result = {}
        for membership in previous.memberships:
            if (
                membership.role == "primary"
                and membership.decision == "confirmed"
                and not membership.stale
            ):
                result[stable_ref_key(membership.atom_ref)] = membership
        return result

    @staticmethod
    def _previous_attempt_indexes(
        previous: TaskGraphGeneration | None,
    ) -> tuple[dict[str, str], dict[str, set[str]]]:
        by_evidence: dict[str, str] = {}
        by_atom: dict[str, set[str]] = defaultdict(set)
        if previous is None:
            return by_evidence, by_atom
        for attempt in previous.attempts:
            for evidence in attempt.evidence_ranges:
                if evidence.atom_ref is None or evidence.stale:
                    continue
                by_evidence[evidence.evidence_id] = attempt.attempt_id
                by_atom[stable_ref_key(evidence.atom_ref)].add(
                    attempt.attempt_id
                )
        return by_evidence, by_atom

    @staticmethod
    def _previous_membership_hash_by_atom(
        previous: TaskGraphGeneration | None,
    ) -> dict[str, str]:
        result = {}
        if previous is None:
            return result
        for attempt in previous.attempts:
            for evidence in attempt.evidence_ranges:
                if evidence.atom_ref is not None:
                    result[stable_ref_key(evidence.atom_ref)] = (
                        evidence.atom_hash or evidence.content_hash
                    )
        return result

    @staticmethod
    def _index_anchor(
        postings: dict[str, deque[str]], task_id: str, anchor: _TaskAnchor,
    ) -> None:
        BoundedTaskLinker._index_terms(postings, task_id, sorted(anchor.terms))

    @staticmethod
    def _index_terms(
        postings: dict[str, deque[str]], task_id: str,
        terms: Iterable[str],
    ) -> None:
        for term in terms:
            posting = postings[term]
            try:
                posting.remove(task_id)
            except ValueError:
                pass
            posting.append(task_id)

    @staticmethod
    def _should_adjudicate(
        atom: ScopedAtomEvidence,
        candidates: list[tuple[str, float]],
        recent_by_session: dict[tuple[str, str], deque[str]],
    ) -> bool:
        if not candidates:
            return False
        session_key = (
            atom.atom_ref.source_scope_id,
            atom.atom_ref.traj_id,
        )
        recent = set(recent_by_session.get(session_key, ()))
        return any(
            task_id in recent or score >= 0.18
            for task_id, score in candidates
        )

    def _adjudication_question(
        self,
        *,
        atom: ScopedAtomEvidence,
        candidates: list[tuple[str, float]],
        tasks: dict[str, LogicalTask],
        recent_by_session: dict[tuple[str, str], deque[str]],
        marker: str,
    ) -> TaskLinkQuestion:
        session_key = (
            atom.atom_ref.source_scope_id,
            atom.atom_ref.traj_id,
        )
        recent = set(recent_by_session.get(session_key, ()))
        bounded_candidates = tuple(
            TaskLinkCandidate(
                task_id=task_id,
                title=tasks[task_id].title,
                summary=tasks[task_id].summary,
                lexical_score=score,
                same_session_recent=task_id in recent,
            )
            for task_id, score in candidates[:MAX_TASK_LINK_CANDIDATES]
            if task_id in tasks and not tasks[task_id].tombstoned
        )
        if not bounded_candidates:
            raise RuntimeError("Task link candidates disappeared before adjudication")
        return TaskLinkQuestion(
            tenant_id=atom.atom_ref.tenant_id,
            task_scope_id=atom.atom_ref.task_scope_id,
            source_scope_id=atom.atom_ref.source_scope_id,
            traj_id=atom.atom_ref.traj_id,
            atom_id=atom.atom_ref.atom_id,
            intent=atom.atom.intent,
            summary=atom.atom.summary,
            explicit_marker=marker,
            candidates=bounded_candidates,
        )

    def _adjudicate(
        self,
        question: TaskLinkQuestion,
    ) -> TaskLinkJudgement:
        if self.adjudicator is None:
            raise RuntimeError("Task link adjudicator is not configured")
        judgement = self.adjudicator.judge(question)
        if not isinstance(judgement, TaskLinkJudgement):
            raise TypeError("Task link adjudicator returned an invalid judgement")
        candidate_ids = {candidate.task_id for candidate in question.candidates}
        if judgement.task_id is not None and judgement.task_id not in candidate_ids:
            raise ValueError("Task link adjudicator selected an unbounded candidate")
        return judgement

    def _adjudication_audit(
        self,
        question: TaskLinkQuestion,
        *,
        judgement: TaskLinkJudgement | None = None,
        error: Exception | None = None,
    ) -> dict:
        if self.adjudicator is None:
            raise RuntimeError("Task link adjudicator is not configured")
        descriptor = self.adjudicator.descriptor()
        record = {
            **question.to_audit_dict(),
            "status": "succeeded" if judgement is not None else "failed",
            "usage_step": "task_link",
            "adjudicator": {
                key: descriptor[key]
                for key in ("name", "version", "model", "prompt_fingerprint")
                if descriptor.get(key) not in (None, "")
            },
        }
        if judgement is not None:
            record.update(judgement.to_audit_dict())
        elif error is not None:
            record["error_type"] = type(error).__name__
        return record

    def _model_decided_by(self, decision: str) -> str:
        if self.adjudicator is None:
            return f"model:unavailable:{decision}"
        descriptor = self.adjudicator.descriptor()
        version = str(
            descriptor.get("version")
            or descriptor.get("name")
            or "unavailable"
        )
        return f"model:{version}:{decision}"

    def _candidates(
        self,
        atom: ScopedAtomEvidence,
        anchors: dict[str, _TaskAnchor],
        postings: dict[str, deque[str]],
        recent_by_session: dict[tuple[str, str], deque[str]],
    ) -> list[tuple[str, float]]:
        atom_terms = set(_terms(f"{atom.atom.intent}\n{atom.atom.summary}"))
        candidate_ids: set[str] = set()
        for term in sorted(atom_terms):
            candidate_ids.update(postings.get(term, ()))
        session_key = (atom.atom_ref.source_scope_id, atom.atom_ref.traj_id)
        candidate_ids.update(recent_by_session.get(session_key, ()))
        marker = _marker_kind(atom.atom.intent)
        scored = []
        normalized_intent = _normalize(atom.atom.intent)
        recent = set(recent_by_session.get(session_key, ()))
        for task_id in candidate_ids:
            anchor = anchors.get(task_id)
            if anchor is None:
                continue
            score = _similarity(atom_terms, anchor.terms)
            if normalized_intent and normalized_intent in anchor.normalized_intents:
                score = max(score, 0.75)
            if marker and task_id in recent:
                score = min(1.0, score + 0.35)
            scored.append((task_id, score))
        scored.sort(key=_candidate_sort_key)
        return scored[:self.top_k]

    @staticmethod
    def _confirmed_candidate(
        candidates: list[tuple[str, float]],
        marker: str,
    ) -> str | None:
        if not marker or not candidates:
            return None
        best_id, best_score = candidates[0]
        threshold = 0.52 if marker in ("retry", "correction") else 0.55
        if best_score < threshold:
            return None
        if len(candidates) > 1 and math.isclose(best_score, candidates[1][1], abs_tol=0.05):
            return None
        return best_id

    def _build_attempts(
        self,
        *,
        tasks: dict[str, LogicalTask],
        memberships: list[TaskAtomMembership],
        atoms_by_key: dict[str, ScopedAtomEvidence],
        trajectories: tuple[ScopedTrajectoryEvidence, ...],
        previous_attempt_by_evidence: dict[str, str],
        previous_attempt_ids_by_atom: dict[str, set[str]],
        now: str,
    ) -> tuple[list[TaskAttempt], list[AttemptRelation]]:
        task_atoms: dict[str, list[ScopedAtomEvidence]] = defaultdict(list)
        for membership in memberships:
            if membership.role != "primary" or membership.decision != "confirmed" or membership.stale:
                continue
            atom = atoms_by_key.get(stable_ref_key(membership.atom_ref))
            if atom is not None:
                task_atoms[membership.task_id].append(atom)
        trajectory_by_session = {
            (
                trajectory.session_ref.source_scope_id,
                trajectory.session_ref.traj_id,
            ): trajectory
            for trajectory in trajectories
        }
        task_ids_by_session: dict[tuple[str, str], set[str]] = defaultdict(set)
        for candidate_task_id, candidate_atoms in task_atoms.items():
            for candidate_atom in candidate_atoms:
                task_ids_by_session[(
                    candidate_atom.atom_ref.source_scope_id,
                    candidate_atom.atom_ref.traj_id,
                )].add(candidate_task_id)
        attempts: list[TaskAttempt] = []
        relations: list[AttemptRelation] = []
        for task_id, atom_list in task_atoms.items():
            atom_list.sort(key=_atom_sort_key)
            groups: list[list[_AttemptSegment]] = []
            relation_kinds: list[str] = []
            for atom in atom_list:
                atom_key = stable_ref_key(atom.atom_ref)
                previous_ids = previous_attempt_ids_by_atom.get(
                    atom_key, set(),
                )
                for segment_index, raw_segment in enumerate(
                    self._attempt_segments(atom)
                ):
                    previous_attempt_id = previous_attempt_by_evidence.get(
                        raw_segment.evidence.evidence_id
                    )
                    if (
                        previous_attempt_id is None
                        and segment_index == 0
                        and len(previous_ids) == 1
                    ):
                        previous_attempt_id = next(iter(previous_ids))
                    segment = replace(
                        raw_segment,
                        previous_attempt_id=previous_attempt_id,
                    )
                    if not groups:
                        groups.append([segment])
                        continue
                    previous_segment = groups[-1][-1]
                    previous_atom = previous_segment.atom
                    same_session = (
                        previous_atom.atom_ref.source_scope_id
                        == atom.atom_ref.source_scope_id
                        and previous_atom.atom_ref.traj_id
                        == atom.atom_ref.traj_id
                    )
                    same_atom = previous_atom.atom_ref.key == atom.atom_ref.key
                    adjacent = same_session and (
                        (
                            same_atom
                            and previous_segment.evidence.end
                            == segment.evidence.start
                        )
                        or previous_atom.atom.post_atom_id == atom.atom.atom_id
                    )
                    previous_trajectory = trajectory_by_session.get((
                        previous_atom.atom_ref.source_scope_id,
                        previous_atom.atom_ref.traj_id,
                    ))
                    current_trajectory = trajectory_by_session.get((
                        atom.atom_ref.source_scope_id,
                        atom.atom_ref.traj_id,
                    ))
                    same_run = bool(
                        previous_trajectory
                        and current_trajectory
                        and previous_trajectory.metadata.get("run_id")
                        and previous_trajectory.metadata.get("run_id")
                        == current_trajectory.metadata.get("run_id")
                        and previous_atom.source_harness.get("name")
                        == atom.source_harness.get("name")
                    )
                    group_attempt_ids = {
                        item.previous_attempt_id
                        for item in groups[-1]
                        if item.previous_attempt_id
                    }
                    preserves_attempt_boundary = bool(
                        segment.previous_attempt_id
                        and group_attempt_ids
                        and segment.previous_attempt_id
                        not in group_attempt_ids
                    )
                    if segment.marker not in ("retry", "correction") and (
                        adjacent or same_run
                    ) and not preserves_attempt_boundary:
                        groups[-1].append(segment)
                        continue
                    groups.append([segment])
                    relation_kinds.append(segment.marker or "continuation")

            task_attempts: list[TaskAttempt] = []
            last_group_by_session = {
                (
                    group[-1].atom.atom_ref.source_scope_id,
                    group[-1].atom.atom_ref.traj_id,
                ): index
                for index, group in enumerate(groups)
            }
            for group_index, group in enumerate(groups):
                reuse_ids = {
                    segment.previous_attempt_id
                    for segment in group
                    if segment.previous_attempt_id
                }
                if len(reuse_ids) > 1:
                    raise RuntimeError(
                        "incremental rebuild would collapse historical Attempt boundaries"
                    )
                attempt_id = min(reuse_ids) if reuse_ids else _opaque("att")
                evidence_ranges = tuple(
                    segment.evidence for segment in group
                )
                group_session = (
                    group[-1].atom.atom_ref.source_scope_id,
                    group[-1].atom.atom_ref.traj_id,
                )
                explicit = self._attempt_outcome(
                    [segment.atom for segment in group],
                    task_id=task_id,
                    task_ids_by_session=task_ids_by_session,
                    trajectory_by_session=trajectory_by_session,
                ) if last_group_by_session[group_session] == group_index else {}
                outcome = explicit.get("outcome", "unknown")
                verification = explicit.get("verification", "unverified")
                finished = bool(explicit) or group_index < len(groups) - 1
                decisions = (
                    DecisionRecord(
                        decision_id=_digest("dec", attempt_id, "outcome", outcome),
                        dimension="outcome",
                        value=outcome,
                        confidence=1.0 if explicit else None,
                        decision="confirmed" if explicit else "needs_review",
                        decided_by=explicit.get("source", "rule:no_attempt_terminal_evidence"),
                        algorithm_version=ALGORITHM_VERSION,
                        evidence_refs=tuple(item.evidence_id for item in evidence_ranges),
                        observed_at=group[-1].atom.observed_at,
                    ),
                )
                attempt = TaskAttempt(
                    attempt_id=attempt_id,
                    task_id=task_id,
                    started_at=group[0].atom.observed_at,
                    ended_at=group[-1].atom.observed_at if finished else None,
                    lifecycle="finished" if finished else "running",
                    outcome=outcome,
                    verification=verification,
                    user_disposition="unknown",
                    evidence_ranges=evidence_ranges,
                    decisions=decisions,
                    execution_identity={
                        "segments": [
                            {
                                "evidence_id": evidence.evidence_id,
                                "model": atom.source_model,
                                "harness": atom.source_harness,
                                "skills": [
                                    {"name": name, "version": "unavailable",
                                     "unavailable_reason": "skill_commit_not_captured"}
                                    for name in atom.atom.used_skills
                                ],
                            }
                            for segment, evidence in zip(group, evidence_ranges)
                            for atom in (segment.atom,)
                        ]
                    },
                )
                task_attempts.append(attempt)
                attempts.append(attempt)
            for index in range(1, len(task_attempts)):
                kind = relation_kinds[index - 1]
                relation_type = {
                    "retry": "retry_of",
                    "correction": "correction_of",
                }.get(kind, "continuation_of")
                confirmed = kind in ("retry", "correction")
                relations.append(AttemptRelation(
                    relation_id=_digest(
                        "arel", task_attempts[index].attempt_id,
                        task_attempts[index - 1].attempt_id, relation_type,
                    ),
                    from_attempt_id=task_attempts[index].attempt_id,
                    to_attempt_id=task_attempts[index - 1].attempt_id,
                    relation_type=relation_type,
                    confidence=None,
                    decision="confirmed" if confirmed else "proposed",
                    decided_by=(
                        f"rule:explicit_{kind}" if confirmed
                        else "rule:discontinuous_same_task"
                    ),
                    algorithm_version=f"{ALGORITHM_VERSION}:uncalibrated",
                    evidence_refs=tuple(
                        evidence.evidence_id
                        for evidence in task_attempts[index].evidence_ranges
                    ),
                    observed_at=task_attempts[index].started_at,
                ))
            task = tasks[task_id]
            latest = task_attempts[-1]
            # A succeeded sole-Task Attempt can prove the goal was satisfied.
            # A failed/cancelled execution only proves that Attempt ended; it
            # does not prove the Logical Task itself is terminal or abandoned.
            if (
                latest.outcome in {"succeeded", "partially_succeeded"}
                and latest.verification == "verified"
            ):
                mapped_outcome = latest.outcome
                tasks[task_id] = replace(
                    task,
                    lifecycle="closed",
                    outcome=mapped_outcome,
                    verification=latest.verification,
                    decisions=(
                        DecisionRecord(
                            decision_id=_digest("dec", task_id, "outcome", mapped_outcome),
                            dimension="outcome",
                            value=mapped_outcome,
                            confidence=1.0,
                            decision="confirmed",
                            decided_by="rule:sole_task_structured_attempt",
                            algorithm_version=ALGORITHM_VERSION,
                            evidence_refs=tuple(
                                evidence.evidence_id for evidence in latest.evidence_ranges
                            ),
                            observed_at=latest.ended_at or now,
                        ),
                    ),
                )
        return attempts, relations

    @staticmethod
    def _carry_stale_attempts(
        *,
        previous: TaskGraphGeneration | None,
        tasks: dict[str, LogicalTask],
        attempts: list[TaskAttempt],
        attempt_relations: list[AttemptRelation],
        now: str,
    ) -> tuple[list[TaskAttempt], list[AttemptRelation]]:
        if previous is None:
            return attempts, attempt_relations
        canonical_by_alias = {
            alias: task.task_id
            for task in tasks.values()
            for alias in task.aliases
        }
        attempt_by_id = {attempt.attempt_id: attempt for attempt in attempts}
        live_evidence_ids = {
            evidence.evidence_id
            for attempt in attempts
            for evidence in attempt.evidence_ranges
            if not evidence.stale
        }
        for old_attempt in previous.attempts:
            stale_evidence = [
                replace(evidence, stale=True)
                for evidence in old_attempt.evidence_ranges
                if evidence.evidence_id not in live_evidence_ids
            ]
            if not stale_evidence:
                continue
            current = attempt_by_id.get(old_attempt.attempt_id)
            if current is not None:
                existing_evidence_ids = {
                    evidence.evidence_id for evidence in current.evidence_ranges
                }
                merged_evidence = current.evidence_ranges + tuple(
                    evidence for evidence in stale_evidence
                    if evidence.evidence_id not in existing_evidence_ids
                )
                attempt_by_id[current.attempt_id] = replace(
                    current, evidence_ranges=merged_evidence,
                )
                continue
            task_id = canonical_by_alias.get(
                old_attempt.task_id, old_attempt.task_id,
            )
            if task_id not in tasks:
                continue
            stale_decision = DecisionRecord(
                decision_id=_digest(
                    "dec", old_attempt.attempt_id, "stale_evidence", now,
                ),
                dimension="verification",
                value="unverified",
                confidence=None,
                decision="needs_review",
                decided_by="rule:source_content_changed_or_deleted",
                algorithm_version=ALGORITHM_VERSION,
                evidence_refs=tuple(
                    evidence.evidence_id for evidence in stale_evidence
                ),
                observed_at=now,
            )
            attempt_by_id[old_attempt.attempt_id] = replace(
                old_attempt,
                task_id=task_id,
                lifecycle="finished",
                ended_at=old_attempt.ended_at or now,
                outcome="unknown",
                verification="unverified",
                evidence_ranges=tuple(stale_evidence),
                decisions=tuple(
                    decision for decision in old_attempt.decisions
                    if decision.decided_by
                    != "rule:source_content_changed_or_deleted"
                ) + (stale_decision,),
            )
        retained_attempt_ids = set(attempt_by_id)
        relation_by_id = {
            relation.relation_id: relation for relation in attempt_relations
        }
        task_by_attempt = {
            attempt_id: attempt.task_id
            for attempt_id, attempt in attempt_by_id.items()
        }
        for relation in previous.attempt_relations:
            if (
                relation.from_attempt_id not in retained_attempt_ids
                or relation.to_attempt_id not in retained_attempt_ids
                or task_by_attempt[relation.from_attempt_id]
                != task_by_attempt[relation.to_attempt_id]
            ):
                continue
            relation_by_id.setdefault(relation.relation_id, relation)
        return list(attempt_by_id.values()), list(relation_by_id.values())

    @staticmethod
    def _attempt_segments(atom: ScopedAtomEvidence) -> tuple[_AttemptSegment, ...]:
        """Split explicit retry/correction turns inside one Atom into ranges."""
        lines = atom.atom.raw_segment.splitlines(keepends=True)
        if not lines:
            lines = [atom.atom.raw_segment]
        boundary_markers: dict[int, str] = {
            0: _marker_kind(atom.atom.intent),
        }
        seen_user_turn = False
        heading_indexes = []
        in_fenced_block = False
        for line_index, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith("```"):
                in_fenced_block = not in_fenced_block
                continue
            if not in_fenced_block and stripped.startswith("## "):
                heading_indexes.append(line_index)
        for heading_position, line_index in enumerate(heading_indexes):
            if lines[line_index].strip().casefold() != "## user":
                continue
            content_end = (
                heading_indexes[heading_position + 1]
                if heading_position + 1 < len(heading_indexes)
                else len(lines)
            )
            marker = _marker_kind(
                "".join(lines[line_index + 1:content_end])
            )
            if seen_user_turn and marker in {"retry", "correction"}:
                boundary_markers[line_index] = marker
            elif not seen_user_turn and marker:
                boundary_markers[0] = marker
            seen_user_turn = True
        boundary_indexes = sorted(boundary_markers)
        segments = []
        for boundary_index, start_index in enumerate(boundary_indexes):
            next_index = (
                boundary_indexes[boundary_index + 1]
                if boundary_index + 1 < len(boundary_indexes)
                else len(lines)
            )
            start = atom.atom.offset_start + start_index
            end = (
                atom.atom.offset_start + next_index
                if next_index < len(lines)
                else atom.atom.offset_end
            )
            if end <= start:
                raise RuntimeError(
                    f"invalid Attempt evidence boundary for {atom.atom.atom_id}"
                )
            raw_segment = "".join(lines[start_index:next_index])
            evidence = BoundedTaskLinker._evidence_range(
                atom,
                start=start,
                end=end,
                raw_segment=raw_segment,
            )
            segments.append(_AttemptSegment(
                atom=atom,
                evidence=evidence,
                marker=boundary_markers[start_index],
            ))
        return tuple(segments)

    @staticmethod
    def _evidence_range(
        atom: ScopedAtomEvidence,
        *,
        start: int | None = None,
        end: int | None = None,
        raw_segment: str | None = None,
    ) -> EvidenceRange:
        atom_key = stable_ref_key(atom.atom_ref)
        locator_start = atom.atom.offset_start if start is None else start
        locator_end = atom.atom.offset_end if end is None else end
        content = atom.atom.raw_segment if raw_segment is None else raw_segment
        content_hash = hashlib.sha256(
            content.encode("utf-8")
        ).hexdigest()
        return EvidenceRange(
            evidence_id=_digest(
                "evd", atom_key, str(locator_start), str(locator_end),
                content_hash,
            ),
            session_ref=atom.atom_ref.session_ref,
            atom_ref=atom.atom_ref,
            locator_kind="trajectory_line",
            start=locator_start,
            end=locator_end,
            content_hash=content_hash,
            atom_hash=atom.atom_hash,
            model=atom.source_model,
            harness=atom.source_harness,
            skills=tuple(
                {"name": name, "version": "unavailable",
                 "unavailable_reason": "skill_commit_not_captured"}
                for name in atom.atom.used_skills
            ),
        )

    @staticmethod
    def _attempt_outcome(
        group: list[ScopedAtomEvidence],
        *,
        task_id: str,
        task_ids_by_session: dict[tuple[str, str], set[str]],
        trajectory_by_session: dict[tuple[str, str], ScopedTrajectoryEvidence],
    ) -> dict:
        last_atom = group[-1]
        session_key = (last_atom.atom_ref.source_scope_id, last_atom.atom_ref.traj_id)
        trajectory = trajectory_by_session.get(session_key)
        if trajectory is None or not trajectory.explicit_outcome:
            return {}
        session_task_ids = task_ids_by_session.get(session_key, set())
        if session_task_ids != {task_id}:
            return {}
        return trajectory.explicit_outcome

    @staticmethod
    def _integer_shares(total: int | None, weights: list[int]) -> list[int | None]:
        if total is None:
            return [None] * len(weights)
        weight_sum = sum(weights)
        if weight_sum <= 0:
            return [None] * len(weights)
        exact = [total * weight / weight_sum for weight in weights]
        result = [math.floor(value) for value in exact]
        remainder = total - sum(result)
        def remainder_key(index: int) -> tuple[float, int]:
            return -(exact[index] - result[index]), index

        order = sorted(range(len(weights)), key=remainder_key)
        for index in order[:remainder]:
            result[index] += 1
        return result

    def _allocate_usage(
        self,
        trajectories: tuple[ScopedTrajectoryEvidence, ...],
        memberships: list[TaskAtomMembership],
        attempts: list[TaskAttempt],
    ) -> list[UsageAllocation]:
        owner_by_atom = {
            stable_ref_key(membership.atom_ref): membership.task_id
            for membership in memberships
            if membership.role == "primary" and membership.decision == "confirmed"
            and not membership.stale
        }
        weighted_by_session: dict[
            tuple[str, str], dict[str, tuple[TaskAttempt, int]]
        ] = defaultdict(dict)
        attributed_weight_by_session: dict[tuple[str, str], int] = defaultdict(int)
        for attempt in attempts:
            for evidence in attempt.evidence_ranges:
                if evidence.stale:
                    continue
                if evidence.atom_ref is not None:
                    owner = owner_by_atom.get(stable_ref_key(evidence.atom_ref))
                    if owner != attempt.task_id:
                        continue
                if not isinstance(evidence.start, int) or not isinstance(
                    evidence.end, int,
                ):
                    continue
                session_key = (
                    evidence.session_ref.source_scope_id,
                    evidence.session_ref.traj_id,
                )
                span = max(1, evidence.end - evidence.start)
                current = weighted_by_session[session_key].get(
                    attempt.attempt_id
                )
                weighted_by_session[session_key][attempt.attempt_id] = (
                    attempt,
                    span + (current[1] if current else 0),
                )
                attributed_weight_by_session[session_key] += span
        allocations: list[UsageAllocation] = []
        for trajectory in trajectories:
            session_key = (
                trajectory.session_ref.source_scope_id,
                trajectory.session_ref.traj_id,
            )
            weighted_attempts = weighted_by_session.get(session_key, {})
            total_atom_weight = sum(
                max(1, atom.atom.offset_end - atom.atom.offset_start)
                for atom in trajectory.atoms
            )
            unattributed_weight = max(
                0,
                total_atom_weight
                - attributed_weight_by_session.get(session_key, 0),
            )
            ordered = sorted(
                weighted_attempts.values(), key=_weighted_attempt_sort_key,
            )
            target_count = len(ordered) + int(bool(unattributed_weight))
            allocation_overflow = (
                target_count > 1
                and len(trajectory.usage_events) * target_count
                > MAX_SHARED_USAGE_ALLOCATION_EDGES
            )
            for event in trajectory.usage_events:
                targets: list[tuple[TaskAttempt | None, int]] = (
                    [] if allocation_overflow else list(ordered)
                )
                if unattributed_weight and not allocation_overflow:
                    targets.append((None, unattributed_weight))
                if not targets:
                    targets.append((None, 1))
                weights = [item[1] for item in targets]
                weight_sum = sum(weights)
                prompt_shares = self._integer_shares(event.prompt_tokens, weights)
                completion_shares = self._integer_shares(event.completion_tokens, weights)
                total_shares = self._integer_shares(event.total_tokens, weights)
                cache_read_shares = self._integer_shares(
                    event.cache_read_tokens, weights,
                )
                cost_remaining = event.cost_usd
                for index, (attempt, weight) in enumerate(targets):
                    fraction = weight / weight_sum
                    if event.cost_usd is None:
                        cost_share = None
                    elif index == len(targets) - 1:
                        cost_share = cost_remaining
                    else:
                        cost_share = event.cost_usd * fraction
                        cost_remaining = (cost_remaining or 0.0) - cost_share
                    target_key = (
                        attempt.attempt_id if attempt is not None
                        else "unattributed"
                    )
                    allocations.append(UsageAllocation(
                        allocation_id=_digest(
                            "all", event.usage_event_id, target_key,
                        ),
                        usage_event_id=event.usage_event_id,
                        usage_plane="execution",
                        allocation_mode=(
                            "unattributed" if attempt is None
                            else "direct" if len(targets) == 1
                            else "shared"
                        ),
                        fraction=fraction,
                        task_id=attempt.task_id if attempt is not None else None,
                        attempt_id=(
                            attempt.attempt_id if attempt is not None else None
                        ),
                        prompt_tokens=prompt_shares[index],
                        completion_tokens=completion_shares[index],
                        total_tokens=total_shares[index],
                        cache_read_tokens=cache_read_shares[index],
                        cost_usd=cost_share,
                        method=(
                            "shared_allocation_edge_limit_exceeded"
                            if allocation_overflow
                            else "no_confirmed_primary_membership"
                            if attempt is None and len(targets) == 1
                            else "atom_line_span_with_unattributed_balance"
                        ),
                        method_version=ALGORITHM_VERSION,
                    ))
        return allocations

    def _apply_overrides(
        self,
        *,
        tasks: dict[str, LogicalTask],
        memberships: list[TaskAtomMembership],
        relations: list[TaskRelation],
        attempts: list[TaskAttempt],
        attempt_relations: list[AttemptRelation],
        overrides: tuple[OverrideEvent, ...],
        atoms_by_key: dict[str, ScopedAtomEvidence],
    ) -> tuple[
        dict[str, LogicalTask], list[TaskAtomMembership], list[TaskRelation],
        list[TaskAttempt], list[AttemptRelation],
    ]:
        for event in overrides:
            if event.operation in ("confirm_membership", "reject_membership"):
                memberships = self._override_membership(
                    memberships, tasks, event, atoms_by_key,
                )
            elif event.operation == "set_task_state":
                task = tasks.get(event.target_id)
                if task is None:
                    continue
                allowed = {
                    key: value for key, value in event.payload.items()
                    if key in {"lifecycle", "outcome", "verification", "user_disposition"}
                }
                decision_records = list(task.decisions)
                for dimension, value in allowed.items():
                    decision_id = _digest("dec", event.event_id, dimension)
                    decision_records = [
                        decision for decision in decision_records
                        if decision.decision_id != decision_id
                    ]
                    decision_records.append(DecisionRecord(
                        decision_id=decision_id,
                        dimension=dimension,
                        value=str(value),
                        confidence=1.0,
                        decision="confirmed",
                        decided_by=f"human:{event.actor}",
                        algorithm_version="manual-override-v1",
                        evidence_refs=event.evidence_refs,
                        observed_at=event.observed_at,
                    ))
                tasks[event.target_id] = replace(
                    task, decisions=tuple(decision_records), **allowed,
                )
            elif event.operation == "set_attempt_state":
                attempts = [
                    self._set_attempt_override(attempt, event)
                    if attempt.attempt_id == event.target_id else attempt
                    for attempt in attempts
                ]
            elif event.operation == "upsert_attempt_relation":
                relation_type = str(event.payload.get("relation_type") or "")
                from_attempt_id = str(
                    event.payload.get("from_attempt_id") or event.target_id
                )
                to_attempt_id = str(event.payload.get("to_attempt_id") or "")
                attempt_by_id = {
                    attempt.attempt_id: attempt for attempt in attempts
                }
                if (
                    from_attempt_id in attempt_by_id
                    and to_attempt_id in attempt_by_id
                    and attempt_by_id[from_attempt_id].task_id
                    == attempt_by_id[to_attempt_id].task_id
                ):
                    relation_id = _digest(
                        "arel", from_attempt_id, to_attempt_id, relation_type,
                    )
                    attempt_relations = [
                        item for item in attempt_relations
                        if item.relation_id != relation_id
                    ]
                    attempt_relations.append(AttemptRelation(
                        relation_id=relation_id,
                        from_attempt_id=from_attempt_id,
                        to_attempt_id=to_attempt_id,
                        relation_type=relation_type,
                        confidence=1.0,
                        decision="confirmed",
                        decided_by=f"human:{event.actor}",
                        algorithm_version="manual-override-v1",
                        evidence_refs=event.evidence_refs,
                        observed_at=event.observed_at,
                    ))
            elif event.operation == "reject_attempt_relation":
                attempt_relations = [
                    replace(
                        relation,
                        decision="rejected",
                        confidence=1.0,
                        decided_by=f"human:{event.actor}",
                        algorithm_version="manual-override-v1",
                        observed_at=event.observed_at,
                    ) if relation.relation_id == event.target_id else relation
                    for relation in attempt_relations
                ]
            elif event.operation == "upsert_task_relation":
                relation_type = str(event.payload.get("relation_type") or "")
                from_task_id = str(event.payload.get("from_task_id") or event.target_id)
                to_task_id = str(event.payload.get("to_task_id") or "")
                if from_task_id in tasks and to_task_id in tasks:
                    relation_id = _digest("rel", from_task_id, to_task_id, relation_type)
                    relations = [item for item in relations if item.relation_id != relation_id]
                    relations.append(TaskRelation(
                        relation_id=relation_id,
                        from_task_id=from_task_id,
                        to_task_id=to_task_id,
                        relation_type=relation_type,
                        confidence=1.0,
                        decision="confirmed",
                        decided_by=f"human:{event.actor}",
                        algorithm_version="manual-override-v1",
                        evidence_refs=event.evidence_refs,
                        observed_at=event.observed_at,
                    ))
            elif event.operation == "reject_task_relation":
                relations = [
                    replace(
                        relation,
                        decision="rejected",
                        confidence=1.0,
                        decided_by=f"human:{event.actor}",
                        algorithm_version="manual-override-v1",
                        observed_at=event.observed_at,
                    ) if relation.relation_id == event.target_id else relation
                    for relation in relations
                ]
            elif event.operation == "merge_tasks":
                canonical_id = event.target_id
                merge_ids = {
                    str(value) for value in event.payload.get("task_ids") or ()
                    if str(value) in tasks and str(value) != canonical_id
                }
                if canonical_id in tasks:
                    memberships, relations, attempts, attempt_relations = self._merge_tasks(
                        canonical_id, merge_ids, tasks, memberships, relations,
                        attempts, attempt_relations,
                    )
            elif event.operation == "move_atoms":
                target_id = str(event.payload.get("target_task_id") or event.target_id)
                atom_keys = {str(value) for value in event.payload.get("atom_keys") or ()}
                if target_id in tasks:
                    memberships = self._move_atoms(
                        target_id, atom_keys, memberships, atoms_by_key, event,
                        event.payload.get("atom_refs") or {},
                    )
            elif event.operation == "split_task":
                new_task_id = str(event.payload.get("new_task_id") or "")
                atom_keys = {
                    str(value) for value in event.payload.get("atom_keys") or ()
                }
                if event.target_id in tasks and new_task_id:
                    existing = tasks.get(new_task_id)
                    if existing is None:
                        tasks[new_task_id] = LogicalTask(
                            task_id=new_task_id,
                            title=str(event.payload.get("title") or "")[:240],
                            summary=str(event.payload.get("summary") or "")[:1000],
                            created_at=event.observed_at,
                        )
                    else:
                        tasks[new_task_id] = replace(
                            existing,
                            title=str(event.payload.get("title") or existing.title)[:240],
                            summary=str(
                                event.payload.get("summary") or existing.summary
                            )[:1000],
                            tombstoned=False,
                        )
                    memberships = self._move_atoms(
                        new_task_id, atom_keys, memberships, atoms_by_key,
                        event, event.payload.get("atom_refs") or {},
                    )
        return tasks, memberships, relations, attempts, attempt_relations

    @staticmethod
    def _override_membership(
        memberships: list[TaskAtomMembership],
        tasks: dict[str, LogicalTask],
        event: OverrideEvent,
        atoms_by_key: dict[str, ScopedAtomEvidence],
    ) -> list[TaskAtomMembership]:
        result = list(memberships)
        target_index = next((
            index for index, item in enumerate(result)
            if item.membership_id == event.target_id
        ), None)
        if target_index is None:
            atom_key = str(event.payload.get("atom_key") or "")
            task_id = str(event.payload.get("task_id") or "")
            atom = atoms_by_key.get(atom_key)
            atom_ref_payload = event.payload.get("atom_ref")
            if task_id not in tasks:
                return result
            if atom is not None:
                atom_ref = atom.atom_ref
                stale = False
            elif isinstance(atom_ref_payload, dict):
                try:
                    atom_ref = AtomRef.from_dict(atom_ref_payload)
                except (KeyError, TypeError, ValueError):
                    return result
                stale = True
            else:
                return result
            role = str(event.payload.get("role") or "primary")
            effective_membership_id = _digest("mem", task_id, atom_key, role)
            target_index = next((
                index for index, item in enumerate(result)
                if item.membership_id == effective_membership_id
            ), None)
            if target_index is None:
                result.append(TaskAtomMembership(
                    membership_id=effective_membership_id,
                    task_id=task_id,
                    atom_ref=atom_ref,
                    role=role,
                    confidence=1.0,
                    decision=(
                        "confirmed"
                        if event.operation == "confirm_membership"
                        else "rejected"
                    ),
                    decided_by=f"human:{event.actor}",
                    algorithm_version="manual-override-v1",
                    evidence_refs=event.evidence_refs,
                    observed_at=event.observed_at,
                    stale=stale,
                ))
                target_index = len(result) - 1
        target = result[target_index]
        if event.operation == "confirm_membership" and target.role == "primary":
            result = [
                replace(
                    item,
                    decision="rejected",
                    confidence=1.0,
                    decided_by=f"human:{event.actor}",
                    algorithm_version="manual-override-v1",
                    evidence_refs=event.evidence_refs,
                    observed_at=event.observed_at,
                ) if (
                    item.atom_ref.key == target.atom_ref.key
                    and item.role == "primary"
                    and item.membership_id != target.membership_id
                    and item.decision != "rejected"
                ) else item
                for item in result
            ]
            target_index = next(
                index for index, item in enumerate(result)
                if item.membership_id == target.membership_id
            )
        result[target_index] = replace(
            result[target_index],
            decision="confirmed" if event.operation == "confirm_membership" else "rejected",
            confidence=1.0,
            decided_by=f"human:{event.actor}",
            algorithm_version="manual-override-v1",
            evidence_refs=event.evidence_refs,
            observed_at=event.observed_at,
        )
        return result

    @staticmethod
    def _set_attempt_override(attempt: TaskAttempt, event: OverrideEvent) -> TaskAttempt:
        allowed = {
            key: value for key, value in event.payload.items()
            if key in {"lifecycle", "outcome", "verification", "user_disposition"}
        }
        decisions = list(attempt.decisions)
        for dimension, value in allowed.items():
            decision_id = _digest("dec", event.event_id, dimension)
            decisions = [
                decision for decision in decisions
                if decision.decision_id != decision_id
            ]
            decisions.append(DecisionRecord(
                decision_id=decision_id,
                dimension=dimension,
                value=str(value),
                confidence=1.0,
                decision="confirmed",
                decided_by=f"human:{event.actor}",
                algorithm_version="manual-override-v1",
                evidence_refs=event.evidence_refs,
                observed_at=event.observed_at,
            ))
        lifecycle = allowed.get("lifecycle", attempt.lifecycle)
        ended_at = attempt.ended_at
        if lifecycle == "running":
            ended_at = None
        elif lifecycle == "finished" and ended_at is None:
            ended_at = event.observed_at
        return replace(
            attempt, decisions=tuple(decisions), ended_at=ended_at, **allowed,
        )

    @staticmethod
    def _merge_tasks(
        canonical_id: str,
        merge_ids: set[str],
        tasks: dict[str, LogicalTask],
        memberships: list[TaskAtomMembership],
        relations: list[TaskRelation],
        attempts: list[TaskAttempt],
        attempt_relations: list[AttemptRelation],
    ) -> tuple[
        list[TaskAtomMembership], list[TaskRelation],
        list[TaskAttempt], list[AttemptRelation],
    ]:
        canonical = tasks[canonical_id]
        aliases = set(canonical.aliases)
        aliases.update(merge_ids)
        tasks[canonical_id] = replace(canonical, aliases=tuple(sorted(aliases)), tombstoned=False)
        for task_id in merge_ids:
            task = tasks[task_id]
            tasks[task_id] = replace(task, tombstoned=True)
        rewritten_memberships = []
        membership_by_id: dict[str, TaskAtomMembership] = {}
        decision_rank = {
            "confirmed": 3, "needs_review": 2, "proposed": 1, "rejected": 0,
        }

        def override_rank(record) -> tuple:
            human = record.decided_by.startswith("human:")
            return (
                human,
                not getattr(record, "stale", False),
                record.observed_at if human else "",
                decision_rank[record.decision],
            )

        for membership in memberships:
            rewritten = replace(
                membership,
                task_id=canonical_id,
                membership_id=_digest(
                    "mem", canonical_id, stable_ref_key(membership.atom_ref), membership.role,
                ),
            ) if membership.task_id in merge_ids else membership
            previous = membership_by_id.get(rewritten.membership_id)
            rank = override_rank(rewritten)
            previous_rank = override_rank(previous) if previous is not None else None
            if previous_rank is None or rank > previous_rank:
                membership_by_id[rewritten.membership_id] = rewritten
        rewritten_memberships.extend(membership_by_id.values())
        relation_by_id: dict[str, TaskRelation] = {}
        for relation in relations:
            from_task_id = (
                canonical_id if relation.from_task_id in merge_ids
                else relation.from_task_id
            )
            to_task_id = (
                canonical_id if relation.to_task_id in merge_ids
                else relation.to_task_id
            )
            if from_task_id == to_task_id:
                continue
            rewritten = replace(
                relation,
                relation_id=_digest(
                    "rel", from_task_id, to_task_id, relation.relation_type,
                ),
                from_task_id=from_task_id,
                to_task_id=to_task_id,
            )
            previous = relation_by_id.get(rewritten.relation_id)
            if previous is None or override_rank(rewritten) > override_rank(previous):
                relation_by_id[rewritten.relation_id] = rewritten
        rewritten_attempts = [
            replace(attempt, task_id=canonical_id)
            if attempt.task_id in merge_ids else attempt
            for attempt in attempts
        ]
        return (
            rewritten_memberships, list(relation_by_id.values()),
            rewritten_attempts, attempt_relations,
        )

    @staticmethod
    def _move_atoms(
        target_id: str,
        atom_keys: set[str],
        memberships: list[TaskAtomMembership],
        atoms_by_key: dict[str, ScopedAtomEvidence],
        event: OverrideEvent,
        atom_ref_payloads: dict,
    ) -> list[TaskAtomMembership]:
        result = []
        existing_targets: set[str] = set()
        for membership in memberships:
            atom_key = stable_ref_key(membership.atom_ref)
            if atom_key not in atom_keys or membership.role != "primary":
                result.append(membership)
                continue
            if membership.task_id == target_id:
                result.append(replace(
                    membership,
                    decision="confirmed", confidence=1.0,
                    decided_by=f"human:{event.actor}",
                    algorithm_version="manual-override-v1",
                    evidence_refs=event.evidence_refs,
                    observed_at=event.observed_at,
                ))
                existing_targets.add(atom_key)
            else:
                result.append(replace(
                    membership,
                    decision="rejected", confidence=1.0,
                    decided_by=f"human:{event.actor}",
                    algorithm_version="manual-override-v1",
                    evidence_refs=event.evidence_refs,
                    observed_at=event.observed_at,
                ))
        for atom_key in sorted(atom_keys - existing_targets):
            atom = atoms_by_key.get(atom_key)
            atom_ref = atom.atom_ref if atom is not None else None
            if atom_ref is None:
                payload = atom_ref_payloads.get(atom_key)
                if isinstance(payload, dict):
                    try:
                        atom_ref = AtomRef.from_dict(payload)
                    except (KeyError, TypeError, ValueError):
                        atom_ref = None
            if atom_ref is None:
                continue
            result.append(TaskAtomMembership(
                membership_id=_digest("mem", target_id, atom_key, "primary"),
                task_id=target_id,
                atom_ref=atom_ref,
                role="primary",
                confidence=1.0,
                decision="confirmed",
                decided_by=f"human:{event.actor}",
                algorithm_version="manual-override-v1",
                evidence_refs=event.evidence_refs,
                observed_at=event.observed_at,
                stale=atom is None,
            ))
        return result
