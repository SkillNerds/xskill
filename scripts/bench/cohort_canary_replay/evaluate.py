"""Evaluate immutable old/new Skill outcomes across deployment cohorts.

The evaluator is deliberately offline: it validates a recorded matrix, computes
Task-clustered paired effects, and compares global with cohort-scoped publication
without reading production Canary state or invoking a model, Harness, Git, or the
network.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import re
from collections import Counter
from pathlib import Path
from typing import Any, Callable

SCHEMA_VERSION = 1
_FINGERPRINT_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_ACTIVATION_CONTROLS = {"forced", "matched"}
_DECISION_METRICS = {"score", "pass_rate"}
_OBSERVATION_ROLES = {"decision", "evaluation"}
_FAMILYWISE_METHOD = "bonferroni"
_MAX_WORK_UNITS = 5_000_000


class CohortReplayValidationError(ValueError):
    """Raised when an immutable replay suite violates its data contract."""


def _require(mapping: dict[str, Any], key: str, expected: type, context: str) -> Any:
    if key not in mapping:
        raise CohortReplayValidationError(
            f"{context}: missing required field {key!r}"
        )
    value = mapping[key]
    if expected is int and isinstance(value, bool):
        raise CohortReplayValidationError(f"{context}.{key}: expected int")
    if not isinstance(value, expected):
        raise CohortReplayValidationError(
            f"{context}.{key}: expected {expected.__name__}"
        )
    return value


def _require_string(mapping: dict[str, Any], key: str, context: str) -> str:
    value = _require(mapping, key, str, context)
    if not value.strip():
        raise CohortReplayValidationError(
            f"{context}.{key}: expected a non-empty string"
        )
    return value


def _require_number(
    mapping: dict[str, Any],
    key: str,
    context: str,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    if key not in mapping:
        raise CohortReplayValidationError(
            f"{context}: missing required field {key!r}"
        )
    value = mapping[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CohortReplayValidationError(f"{context}.{key}: expected a number")
    number = float(value)
    if not math.isfinite(number):
        raise CohortReplayValidationError(
            f"{context}.{key}: expected a finite number"
        )
    if minimum is not None and number < minimum:
        raise CohortReplayValidationError(
            f"{context}.{key}: expected a number >= {minimum:g}"
        )
    if maximum is not None and number > maximum:
        raise CohortReplayValidationError(
            f"{context}.{key}: expected a number <= {maximum:g}"
        )
    return number


def _require_exact_keys(
    mapping: dict[str, Any], expected: set[str], context: str
) -> None:
    if set(mapping) == expected:
        return
    missing = sorted(expected - set(mapping))
    unknown = sorted(set(mapping) - expected)
    raise CohortReplayValidationError(
        f"{context}: keys do not match schema; missing={missing}, unknown={unknown}"
    )


def _validate_fingerprint(value: Any, context: str) -> None:
    if not isinstance(value, str) or _FINGERPRINT_RE.fullmatch(value) is None:
        raise CohortReplayValidationError(
            f"{context}: expected sha256 followed by 64 lowercase hex characters"
        )


def _validate_outcome(
    outcome: Any,
    *,
    context: str,
    run_ids: set[str],
) -> None:
    if not isinstance(outcome, dict):
        raise CohortReplayValidationError(f"{context}: expected an object")
    _require_exact_keys(
        outcome,
        {"run_id", "score", "passed", "cost_usd", "latency_ms", "error_type"},
        context,
    )
    run_id = _require_string(outcome, "run_id", context)
    if run_id in run_ids:
        raise CohortReplayValidationError(f"{context}.run_id: duplicate {run_id!r}")
    run_ids.add(run_id)
    _require_number(outcome, "score", context, minimum=0.0, maximum=1.0)
    _require(outcome, "passed", bool, context)
    _require_number(outcome, "cost_usd", context, minimum=0.0)
    _require_number(outcome, "latency_ms", context, minimum=0.0)
    error_type = outcome["error_type"]
    if error_type is not None and (
        not isinstance(error_type, str) or not error_type.strip()
    ):
        raise CohortReplayValidationError(
            f"{context}.error_type: expected null or a non-empty string"
        )


def validate_suite(suite: Any) -> None:
    """Validate one cohort replay suite without changing it."""
    if not isinstance(suite, dict):
        raise CohortReplayValidationError("suite: expected an object")
    _require_exact_keys(
        suite,
        {
            "schema_version",
            "suite_id",
            "metric_config",
            "run_manifest",
            "cohorts",
            "updates",
        },
        "suite",
    )
    version = _require(suite, "schema_version", int, "suite")
    if version != SCHEMA_VERSION:
        raise CohortReplayValidationError(
            f"suite.schema_version: supported={SCHEMA_VERSION}, got={version}"
        )
    _require_string(suite, "suite_id", "suite")

    metric = _require(suite, "metric_config", dict, "suite")
    _require_exact_keys(
        metric,
        {
            "bootstrap_samples",
            "bootstrap_seed",
            "confidence_level",
            "familywise_method",
            "practical_margin",
            "decision_metric",
            "non_evaluable_error_types",
        },
        "suite.metric_config",
    )
    bootstrap_samples = _require(
        metric, "bootstrap_samples", int, "suite.metric_config"
    )
    if bootstrap_samples < 100:
        raise CohortReplayValidationError(
            "suite.metric_config.bootstrap_samples: must be >= 100"
        )
    bootstrap_seed = _require(metric, "bootstrap_seed", int, "suite.metric_config")
    if bootstrap_seed < 0:
        raise CohortReplayValidationError(
            "suite.metric_config.bootstrap_seed: must be >= 0"
        )
    confidence = _require_number(
        metric,
        "confidence_level",
        "suite.metric_config",
        minimum=0.5,
        maximum=0.999,
    )
    if confidence <= 0.5:
        raise CohortReplayValidationError(
            "suite.metric_config.confidence_level: must be > 0.5"
        )
    familywise_method = _require_string(
        metric, "familywise_method", "suite.metric_config"
    )
    if familywise_method != _FAMILYWISE_METHOD:
        raise CohortReplayValidationError(
            "suite.metric_config.familywise_method: only 'bonferroni' is supported"
        )
    margin = _require_number(
        metric,
        "practical_margin",
        "suite.metric_config",
        minimum=0.0,
        maximum=1.0,
    )
    if margin <= 0:
        raise CohortReplayValidationError(
            "suite.metric_config.practical_margin: must be > 0"
        )
    decision_metric = _require_string(
        metric, "decision_metric", "suite.metric_config"
    )
    if decision_metric not in _DECISION_METRICS:
        raise CohortReplayValidationError(
            "suite.metric_config.decision_metric: expected 'score' or 'pass_rate'"
        )
    error_types = _require(
        metric, "non_evaluable_error_types", list, "suite.metric_config"
    )
    if (
        not error_types
        or any(not isinstance(item, str) or not item.strip() for item in error_types)
        or len(error_types) != len(set(error_types))
    ):
        raise CohortReplayValidationError(
            "suite.metric_config.non_evaluable_error_types: "
            "expected unique non-empty strings"
        )

    manifest = _require(suite, "run_manifest", dict, "suite")
    _require_exact_keys(
        manifest,
        {
            "repository_revision",
            "generated_at",
            "candidate_stream_fingerprint",
            "task_set_fingerprint",
            "evaluation_protocol_fingerprint",
            "cohort_definition_fingerprint",
        },
        "suite.run_manifest",
    )
    _require_string(manifest, "repository_revision", "suite.run_manifest")
    _require_string(manifest, "generated_at", "suite.run_manifest")
    for key in (
        "candidate_stream_fingerprint",
        "task_set_fingerprint",
        "evaluation_protocol_fingerprint",
        "cohort_definition_fingerprint",
    ):
        _validate_fingerprint(manifest[key], f"suite.run_manifest.{key}")

    cohorts = _require(suite, "cohorts", list, "suite")
    if len(cohorts) < 2:
        raise CohortReplayValidationError("suite.cohorts: expected at least two cohorts")
    cohort_ids: set[str] = set()
    cohort_tuples: set[tuple[str, str, str]] = set()
    weight_total = 0.0
    for index, cohort in enumerate(cohorts):
        context = f"suite.cohorts[{index}]"
        if not isinstance(cohort, dict):
            raise CohortReplayValidationError(f"{context}: expected an object")
        _require_exact_keys(
            cohort,
            {"cohort_id", "scenario", "model", "runtime", "traffic_weight"},
            context,
        )
        cohort_id = _require_string(cohort, "cohort_id", context)
        if cohort_id in cohort_ids:
            raise CohortReplayValidationError(
                f"{context}.cohort_id: duplicate {cohort_id!r}"
            )
        cohort_ids.add(cohort_id)
        identity = tuple(
            _require_string(cohort, key, context)
            for key in ("scenario", "model", "runtime")
        )
        if identity in cohort_tuples:
            raise CohortReplayValidationError(
                f"{context}: duplicate scenario/model/runtime identity {identity!r}"
            )
        cohort_tuples.add(identity)
        weight_total += _require_number(
            cohort, "traffic_weight", context, minimum=0.0, maximum=1.0
        )
        if cohort["traffic_weight"] <= 0:
            raise CohortReplayValidationError(
                f"{context}.traffic_weight: must be greater than zero"
            )
    if not math.isclose(weight_total, 1.0, rel_tol=0.0, abs_tol=1e-9):
        raise CohortReplayValidationError(
            f"suite.cohorts: traffic weights must sum to 1, got {weight_total}"
        )

    updates = _require(suite, "updates", list, "suite")
    if not updates:
        raise CohortReplayValidationError("suite.updates: must not be empty")
    update_ids: set[str] = set()
    epochs: list[int] = []
    run_ids: set[str] = set()
    total_pairs = 0
    for update_index, update in enumerate(updates):
        context = f"suite.updates[{update_index}]"
        if not isinstance(update, dict):
            raise CohortReplayValidationError(f"{context}: expected an object")
        _require_exact_keys(
            update,
            {
                "update_id",
                "epoch",
                "old_skill_fingerprint",
                "new_skill_fingerprint",
                "activation_control",
                "observations",
            },
            context,
        )
        update_id = _require_string(update, "update_id", context)
        if update_id in update_ids:
            raise CohortReplayValidationError(
                f"{context}.update_id: duplicate {update_id!r}"
            )
        update_ids.add(update_id)
        epoch = _require(update, "epoch", int, context)
        if epoch < 0:
            raise CohortReplayValidationError(f"{context}.epoch: must be >= 0")
        epochs.append(epoch)
        for key in ("old_skill_fingerprint", "new_skill_fingerprint"):
            _validate_fingerprint(update[key], f"{context}.{key}")
        if update["old_skill_fingerprint"] == update["new_skill_fingerprint"]:
            raise CohortReplayValidationError(
                f"{context}: old and new Skill fingerprints must differ"
            )
        activation_control = _require_string(update, "activation_control", context)
        if activation_control not in _ACTIVATION_CONTROLS:
            raise CohortReplayValidationError(
                f"{context}.activation_control: expected one of "
                f"{sorted(_ACTIVATION_CONTROLS)}"
            )
        observations = _require(update, "observations", list, context)
        if not observations:
            raise CohortReplayValidationError(
                f"{context}.observations: must not be empty"
            )
        total_pairs += len(observations)
        case_ids: set[str] = set()
        pairs_by_cohort: dict[str, set[tuple[str, str, int]]] = {
            cohort_id: set() for cohort_id in cohort_ids
        }
        seeds_by_cohort_task: dict[str, dict[str, set[int]]] = {
            cohort_id: {} for cohort_id in cohort_ids
        }
        for obs_index, observation in enumerate(observations):
            obs_context = f"{context}.observations[{obs_index}]"
            if not isinstance(observation, dict):
                raise CohortReplayValidationError(
                    f"{obs_context}: expected an object"
                )
            _require_exact_keys(
                observation,
                {
                    "case_id",
                    "task_fingerprint",
                    "seed",
                    "cohort_id",
                    "role",
                    "old",
                    "new",
                },
                obs_context,
            )
            case_id = _require_string(observation, "case_id", obs_context)
            if case_id in case_ids:
                raise CohortReplayValidationError(
                    f"{obs_context}.case_id: duplicate {case_id!r} within update"
                )
            case_ids.add(case_id)
            task_fingerprint = observation["task_fingerprint"]
            _validate_fingerprint(
                task_fingerprint, f"{obs_context}.task_fingerprint"
            )
            seed = _require(observation, "seed", int, obs_context)
            cohort_id = _require_string(observation, "cohort_id", obs_context)
            if cohort_id not in cohort_ids:
                raise CohortReplayValidationError(
                    f"{obs_context}.cohort_id: undeclared cohort {cohort_id!r}"
                )
            role = _require_string(observation, "role", obs_context)
            if role not in _OBSERVATION_ROLES:
                raise CohortReplayValidationError(
                    f"{obs_context}.role: expected one of "
                    f"{sorted(_OBSERVATION_ROLES)}"
                )
            role_pair = (role, task_fingerprint, seed)
            if role_pair in pairs_by_cohort[cohort_id]:
                raise CohortReplayValidationError(
                    f"{obs_context}: duplicate role/task_fingerprint/seed in cohort"
                )
            pairs_by_cohort[cohort_id].add(role_pair)
            seeds_by_cohort_task[cohort_id].setdefault(
                f"{role}:{task_fingerprint}", set()
            ).add(seed)
            _validate_outcome(
                observation["old"], context=f"{obs_context}.old", run_ids=run_ids
            )
            _validate_outcome(
                observation["new"], context=f"{obs_context}.new", run_ids=run_ids
            )
        for cohort_id in sorted(cohort_ids):
            task_seeds = seeds_by_cohort_task[cohort_id]
            for role in sorted(_OBSERVATION_ROLES):
                role_tasks = {
                    key: seeds
                    for key, seeds in task_seeds.items()
                    if key.startswith(f"{role}:")
                }
                if len(role_tasks) < 2:
                    raise CohortReplayValidationError(
                        f"{context}: cohort {cohort_id!r} role {role!r} "
                        "requires at least two Tasks"
                    )
                expected_seeds = next(iter(role_tasks.values()))
                if any(seeds != expected_seeds for seeds in role_tasks.values()):
                    raise CohortReplayValidationError(
                        f"{context}: every Task in cohort {cohort_id!r} role "
                        f"{role!r} must use the same seed set"
                    )
            decision_tasks = {
                key.removeprefix("decision:")
                for key in task_seeds
                if key.startswith("decision:")
            }
            evaluation_tasks = {
                key.removeprefix("evaluation:")
                for key in task_seeds
                if key.startswith("evaluation:")
            }
            if decision_tasks & evaluation_tasks:
                raise CohortReplayValidationError(
                    f"{context}: cohort {cohort_id!r} decision and evaluation "
                    "Task sets must be disjoint"
                )
        for role in sorted(_OBSERVATION_ROLES):
            matrices = {
                cohort_id: {
                    (task_fingerprint, seed)
                    for item_role, task_fingerprint, seed in pairs_by_cohort[
                        cohort_id
                    ]
                    if item_role == role
                }
                for cohort_id in cohort_ids
            }
            expected_matrix = matrices[next(iter(sorted(cohort_ids)))]
            if any(matrix != expected_matrix for matrix in matrices.values()):
                raise CohortReplayValidationError(
                    f"{context}: role {role!r} must use the same Task/seed "
                    "matrix in every cohort"
                )
    if epochs != sorted(epochs) or len(epochs) != len(set(epochs)):
        raise CohortReplayValidationError(
            "suite.updates: epochs must be unique and strictly increasing"
        )
    if total_pairs * bootstrap_samples * (len(cohorts) + 2) > _MAX_WORK_UNITS:
        raise CohortReplayValidationError(
            "suite: replay work exceeds the deterministic 5000000-unit bound"
        )


def load_suite(path: Path | str) -> dict[str, Any]:
    """Load and validate one JSON replay suite."""
    suite_path = Path(path)
    try:
        suite = json.loads(suite_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise CohortReplayValidationError(
            f"invalid JSON in {suite_path}: {error}"
        ) from error
    validate_suite(suite)
    return suite


def _round(value: float) -> float:
    return round(float(value), 6)


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _stable_seed(base_seed: int, label: str) -> int:
    digest = hashlib.sha256(label.encode("utf-8")).digest()
    return base_seed ^ int.from_bytes(digest[:8], "big")


def _percentile(sorted_values: list[float], quantile: float) -> float:
    if not sorted_values:
        return 0.0
    position = quantile * (len(sorted_values) - 1)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return sorted_values[lower]
    fraction = position - lower
    return sorted_values[lower] * (1 - fraction) + sorted_values[upper] * fraction


def _task_deltas(
    observations: list[dict[str, Any]],
    value: Callable[[dict[str, Any]], float],
) -> dict[str, float]:
    by_task: dict[str, list[float]] = {}
    for observation in observations:
        delta = value(observation["new"]) - value(observation["old"])
        by_task.setdefault(observation["task_fingerprint"], []).append(delta)
    return {key: _mean(by_task[key]) for key in sorted(by_task)}


def _effect_from_task_deltas(
    task_deltas: dict[str, float],
    *,
    pair_count: int,
    metric_config: dict[str, Any],
    label: str,
    family_size: int,
) -> dict[str, Any]:
    deltas = list(task_deltas.values())
    point = _mean(deltas)
    interval: list[float] | None
    if len(deltas) < 2:
        interval = None
    else:
        alpha = 1.0 - metric_config["confidence_level"]
        adjusted_alpha = alpha / max(1, family_size)
        rng = random.Random(_stable_seed(metric_config["bootstrap_seed"], label))
        size = len(deltas)
        means = [
            _mean([deltas[rng.randrange(size)] for _ in range(size)])
            for _ in range(metric_config["bootstrap_samples"])
        ]
        means.sort()
        interval = [
            _round(_percentile(means, adjusted_alpha / 2)),
            _round(_percentile(means, 1 - adjusted_alpha / 2)),
        ]
    return {
        "n": pair_count,
        "tasks": len(deltas),
        "mean": _round(point),
        "confidence_interval": interval,
        "wins": sum(delta > 0 for delta in deltas),
        "ties": sum(delta == 0 for delta in deltas),
        "losses": sum(delta < 0 for delta in deltas),
    }


def _effect(
    observations: list[dict[str, Any]],
    *,
    value: Callable[[dict[str, Any]], float],
    metric_config: dict[str, Any],
    label: str,
    family_size: int,
) -> tuple[dict[str, Any], dict[str, float]]:
    deltas = _task_deltas(observations, value)
    return (
        _effect_from_task_deltas(
            deltas,
            pair_count=len(observations),
            metric_config=metric_config,
            label=label,
            family_size=family_size,
        ),
        deltas,
    )


def _global_effect(
    task_deltas_by_cohort: dict[str, dict[str, float]],
    weights: dict[str, float],
    *,
    pair_count: int,
    metric_config: dict[str, Any],
    label: str,
    family_size: int,
) -> dict[str, Any]:
    task_sets = {frozenset(deltas) for deltas in task_deltas_by_cohort.values()}
    if len(task_sets) != 1:
        raise CohortReplayValidationError(
            "global effect requires the same Task set in every cohort"
        )
    task_ids = sorted(next(iter(task_sets)))
    weighted_task_deltas = [
        sum(
            weights[cohort_id] * task_deltas_by_cohort[cohort_id][task_id]
            for cohort_id in sorted(task_deltas_by_cohort)
        )
        for task_id in task_ids
    ]
    point = _mean(weighted_task_deltas)
    rng = random.Random(_stable_seed(metric_config["bootstrap_seed"], label))
    size = len(weighted_task_deltas)
    means = [
        _mean(
            [weighted_task_deltas[rng.randrange(size)] for _ in range(size)]
        )
        for _ in range(metric_config["bootstrap_samples"])
    ]
    means.sort()
    alpha = (1.0 - metric_config["confidence_level"]) / max(1, family_size)
    interval = [
        _round(_percentile(means, alpha / 2)),
        _round(_percentile(means, 1 - alpha / 2)),
    ]
    return {
        "n": pair_count,
        "tasks": len(task_ids),
        "mean": _round(point),
        "confidence_interval": interval,
    }


def _evidence_status(
    effect: dict[str, Any],
    *,
    margin: float,
    has_non_evaluable_error: bool,
) -> str:
    if has_non_evaluable_error or effect["confidence_interval"] is None:
        return "unresolved"
    lower, upper = effect["confidence_interval"]
    if lower > margin:
        return "supported_positive"
    if upper < -margin:
        return "supported_negative"
    return "unresolved"


def _error_counts(
    observations: list[dict[str, Any]], non_evaluable_types: set[str]
) -> Counter[str]:
    return Counter(
        outcome["error_type"]
        for observation in observations
        for outcome in (observation["old"], observation["new"])
        if outcome["error_type"] in non_evaluable_types
    )


def evaluate_suite(suite: dict[str, Any]) -> dict[str, Any]:
    """Return a deterministic cohort and publication-policy report."""
    validate_suite(suite)
    metric = suite["metric_config"]
    cohorts = {cohort["cohort_id"]: cohort for cohort in suite["cohorts"]}
    cohort_ids = sorted(cohorts)
    weights = {
        cohort_id: float(cohorts[cohort_id]["traffic_weight"])
        for cohort_id in cohort_ids
    }
    decision_metric = metric["decision_metric"]
    score_value = lambda outcome: float(outcome["score"])
    pass_value = lambda outcome: 1.0 if outcome["passed"] else 0.0
    non_evaluable_types = set(metric["non_evaluable_error_types"])
    update_reports: list[dict[str, Any]] = []

    family_size = len(cohort_ids) * len(suite["updates"])
    for update in suite["updates"]:
        by_cohort_role = {
            (cohort_id, role): [
                observation
                for observation in update["observations"]
                if observation["cohort_id"] == cohort_id
                and observation["role"] == role
            ]
            for cohort_id in cohort_ids
            for role in sorted(_OBSERVATION_ROLES)
        }
        decision_task_deltas_by_cohort: dict[str, dict[str, float]] = {}
        evaluation_task_deltas_by_cohort: dict[str, dict[str, float]] = {}
        cohort_reports: list[dict[str, Any]] = []
        for cohort_id in cohort_ids:
            decision_observations = by_cohort_role[(cohort_id, "decision")]
            evaluation_observations = by_cohort_role[(cohort_id, "evaluation")]
            decision_score_effect, decision_score_deltas = _effect(
                decision_observations,
                value=score_value,
                metric_config=metric,
                label=f"{update['update_id']}:{cohort_id}:decision:score",
                family_size=family_size,
            )
            decision_pass_effect, decision_pass_deltas = _effect(
                decision_observations,
                value=pass_value,
                metric_config=metric,
                label=f"{update['update_id']}:{cohort_id}:decision:pass_rate",
                family_size=family_size,
            )
            evaluation_score_effect, evaluation_score_deltas = _effect(
                evaluation_observations,
                value=score_value,
                metric_config=metric,
                label=f"{update['update_id']}:{cohort_id}:evaluation:score",
                family_size=family_size,
            )
            evaluation_pass_effect, evaluation_pass_deltas = _effect(
                evaluation_observations,
                value=pass_value,
                metric_config=metric,
                label=f"{update['update_id']}:{cohort_id}:evaluation:pass_rate",
                family_size=family_size,
            )
            if decision_metric == "score":
                decision_effect = decision_score_effect
                decision_task_deltas = decision_score_deltas
                evaluation_effect = evaluation_score_effect
                evaluation_task_deltas = evaluation_score_deltas
            else:
                decision_effect = decision_pass_effect
                decision_task_deltas = decision_pass_deltas
                evaluation_effect = evaluation_pass_effect
                evaluation_task_deltas = evaluation_pass_deltas
            decision_task_deltas_by_cohort[cohort_id] = decision_task_deltas
            evaluation_task_deltas_by_cohort[cohort_id] = evaluation_task_deltas

            decision_errors = _error_counts(
                decision_observations, non_evaluable_types
            )
            evaluation_errors = _error_counts(
                evaluation_observations, non_evaluable_types
            )
            decision_status = _evidence_status(
                decision_effect,
                margin=metric["practical_margin"],
                has_non_evaluable_error=bool(decision_errors),
            )
            evaluation_status = _evidence_status(
                evaluation_effect,
                margin=metric["practical_margin"],
                has_non_evaluable_error=bool(evaluation_errors),
            )
            cohort_reports.append(
                {
                    "cohort_id": cohort_id,
                    "scenario": cohorts[cohort_id]["scenario"],
                    "model": cohorts[cohort_id]["model"],
                    "runtime": cohorts[cohort_id]["runtime"],
                    "traffic_weight": _round(weights[cohort_id]),
                    "decision_metric": decision_metric,
                    "decision_effect": decision_effect,
                    "decision_score_effect": decision_score_effect,
                    "decision_pass_rate_effect": decision_pass_effect,
                    "decision_evidence_status": decision_status,
                    "decision_non_evaluable_errors": dict(
                        sorted(decision_errors.items())
                    ),
                    "evaluation_effect": evaluation_effect,
                    "evaluation_score_effect": evaluation_score_effect,
                    "evaluation_pass_rate_effect": evaluation_pass_effect,
                    "evaluation_evidence_status": evaluation_status,
                    "evaluation_non_evaluable_errors": dict(
                        sorted(evaluation_errors.items())
                    ),
                }
            )

        positive = [
            report["cohort_id"]
            for report in cohort_reports
            if report["decision_evidence_status"] == "supported_positive"
        ]
        negative = [
            report["cohort_id"]
            for report in cohort_reports
            if report["decision_evidence_status"] == "supported_negative"
        ]
        unresolved = [
            report["cohort_id"]
            for report in cohort_reports
            if report["decision_evidence_status"] == "unresolved"
        ]
        supported_sign_reversal = bool(positive and negative)
        global_decision_effect = _global_effect(
            decision_task_deltas_by_cohort,
            weights,
            pair_count=sum(
                len(by_cohort_role[(cohort_id, "decision")])
                for cohort_id in cohort_ids
            ),
            metric_config=metric,
            label=f"{update['update_id']}:global:decision:{decision_metric}",
            family_size=len(suite["updates"]),
        )
        global_evaluation_effect = _global_effect(
            evaluation_task_deltas_by_cohort,
            weights,
            pair_count=sum(
                len(by_cohort_role[(cohort_id, "evaluation")])
                for cohort_id in cohort_ids
            ),
            metric_config=metric,
            label=f"{update['update_id']}:global:evaluation:{decision_metric}",
            family_size=len(suite["updates"]),
        )
        global_status = _evidence_status(
            global_decision_effect,
            margin=metric["practical_margin"],
            has_non_evaluable_error=any(
                report["decision_non_evaluable_errors"] for report in cohort_reports
            ),
        )
        global_selection = (
            "new" if global_status == "supported_positive" else "old"
        )
        scoped_selection = {
            report["cohort_id"]: (
                "new"
                if report["decision_evidence_status"] == "supported_positive"
                else "old"
            )
            for report in cohort_reports
        }
        evaluation_point_effects = {
            report["cohort_id"]: float(report["evaluation_effect"]["mean"])
            for report in cohort_reports
        }
        global_evaluation_point = sum(
            weights[cohort_id] * evaluation_point_effects[cohort_id]
            for cohort_id in cohort_ids
        )
        global_policy_gain = (
            global_evaluation_point if global_selection == "new" else 0.0
        )
        scoped_policy_gain = sum(
            weights[cohort_id] * evaluation_point_effects[cohort_id]
            for cohort_id in cohort_ids
            if scoped_selection[cohort_id] == "new"
        )
        evaluation_statuses = {
            report["cohort_id"]: report["evaluation_evidence_status"]
            for report in cohort_reports
        }
        global_harmful = [
            cohort_id
            for cohort_id in cohort_ids
            if global_selection == "new"
            and evaluation_statuses[cohort_id] == "supported_negative"
        ]
        scoped_harmful = [
            cohort_id
            for cohort_id in cohort_ids
            if scoped_selection[cohort_id] == "new"
            and evaluation_statuses[cohort_id] == "supported_negative"
        ]
        oracle_global = max(0.0, global_evaluation_point)
        oracle_scoped = sum(
            weights[cohort_id] * max(0.0, evaluation_point_effects[cohort_id])
            for cohort_id in cohort_ids
        )
        total_calls = 2 * len(update["observations"])
        total_cost = sum(
            float(outcome["cost_usd"])
            for observation in update["observations"]
            for outcome in (observation["old"], observation["new"])
        )
        total_latency = sum(
            float(outcome["latency_ms"])
            for observation in update["observations"]
            for outcome in (observation["old"], observation["new"])
        )
        update_reports.append(
            {
                "update_id": update["update_id"],
                "epoch": update["epoch"],
                "old_skill_fingerprint": update["old_skill_fingerprint"],
                "new_skill_fingerprint": update["new_skill_fingerprint"],
                "activation_control": update["activation_control"],
                "cohorts": cohort_reports,
                "supported_positive_cohorts": positive,
                "supported_negative_cohorts": negative,
                "unresolved_cohorts": unresolved,
                "supported_sign_reversal": supported_sign_reversal,
                "global": {
                    "decision_effect": global_decision_effect,
                    "decision_evidence_status": global_status,
                    "evaluation_effect": global_evaluation_effect,
                    "selection": global_selection,
                    "policy_gain": _round(global_policy_gain),
                    "harmful_promoted_cohorts": global_harmful,
                    "harmful_traffic_weight": _round(
                        sum(weights[item] for item in global_harmful)
                    ),
                    "retained_version_count": 1,
                    "oracle_value": _round(oracle_global),
                },
                "scoped": {
                    "selection_by_cohort": scoped_selection,
                    "policy_gain": _round(scoped_policy_gain),
                    "harmful_promoted_cohorts": scoped_harmful,
                    "harmful_traffic_weight": _round(
                        sum(weights[item] for item in scoped_harmful)
                    ),
                    "unresolved_cohort_rate": _round(
                        len(unresolved) / len(cohort_ids)
                    ),
                    "unresolved_traffic_weight": _round(
                        sum(weights[item] for item in unresolved)
                    ),
                    "retained_version_count": len(set(scoped_selection.values())),
                    "oracle_value": _round(oracle_scoped),
                    "oracle_value_gap_over_global": _round(
                        oracle_scoped - oracle_global
                    ),
                },
                "evaluation_resources": {
                    "calls": total_calls,
                    "cost_usd": _round(total_cost),
                    "latency_ms_sum": _round(total_latency),
                },
            }
        )

    update_count = len(update_reports)
    cohort_decisions = update_count * len(cohort_ids)
    return {
        "schema_version": SCHEMA_VERSION,
        "suite_id": suite["suite_id"],
        "run_manifest": suite["run_manifest"],
        "method": {
            "cohort_key": "scenario×model×runtime",
            "decision_metric": decision_metric,
            "selection_role": "decision",
            "held_out_evaluation_role": "evaluation",
            "bootstrap_samples": metric["bootstrap_samples"],
            "bootstrap_seed": metric["bootstrap_seed"],
            "confidence_level": metric["confidence_level"],
            "familywise_method": metric["familywise_method"],
            "practical_margin": metric["practical_margin"],
            "cohort_family_size": family_size,
            "global_family_size": len(suite["updates"]),
        },
        "updates": update_reports,
        "aggregate": {
            "updates": update_count,
            "cohorts": len(cohort_ids),
            "supported_sign_reversals": sum(
                report["supported_sign_reversal"] for report in update_reports
            ),
            "supported_sign_reversal_rate": _round(
                sum(report["supported_sign_reversal"] for report in update_reports)
                / update_count
            ),
            "global_mean_policy_gain": _round(
                _mean([report["global"]["policy_gain"] for report in update_reports])
            ),
            "scoped_mean_policy_gain": _round(
                _mean([report["scoped"]["policy_gain"] for report in update_reports])
            ),
            "global_harmful_promotions": sum(
                len(report["global"]["harmful_promoted_cohorts"])
                for report in update_reports
            ),
            "scoped_harmful_promotions": sum(
                len(report["scoped"]["harmful_promoted_cohorts"])
                for report in update_reports
            ),
            "global_harmful_promotion_rate": _round(
                sum(
                    len(report["global"]["harmful_promoted_cohorts"])
                    for report in update_reports
                )
                / cohort_decisions
            ),
            "scoped_harmful_promotion_rate": _round(
                sum(
                    len(report["scoped"]["harmful_promoted_cohorts"])
                    for report in update_reports
                )
                / cohort_decisions
            ),
            "global_mean_harmful_traffic_weight": _round(
                _mean(
                    [
                        report["global"]["harmful_traffic_weight"]
                        for report in update_reports
                    ]
                )
            ),
            "scoped_mean_harmful_traffic_weight": _round(
                _mean(
                    [
                        report["scoped"]["harmful_traffic_weight"]
                        for report in update_reports
                    ]
                )
            ),
            "scoped_mean_retained_versions": _round(
                _mean(
                    [
                        report["scoped"]["retained_version_count"]
                        for report in update_reports
                    ]
                )
            ),
            "evaluation_calls": sum(
                report["evaluation_resources"]["calls"]
                for report in update_reports
            ),
            "evaluation_cost_usd": _round(
                sum(
                    report["evaluation_resources"]["cost_usd"]
                    for report in update_reports
                )
            ),
            "evaluation_latency_ms_sum": _round(
                sum(
                    report["evaluation_resources"]["latency_ms_sum"]
                    for report in update_reports
                )
            ),
        },
    }


def render_text(report: dict[str, Any]) -> str:
    """Render a concise reviewer-facing report."""
    aggregate = report["aggregate"]
    lines = [
        f"suite_id: {report['suite_id']}",
        f"updates: {aggregate['updates']}",
        f"cohorts: {aggregate['cohorts']}",
        (
            "supported_sign_reversals: "
            f"{aggregate['supported_sign_reversals']} "
            f"rate={aggregate['supported_sign_reversal_rate']}"
        ),
        (
            "mean_policy_gain: "
            f"global={aggregate['global_mean_policy_gain']} "
            f"scoped={aggregate['scoped_mean_policy_gain']}"
        ),
        (
            "harmful_promotions: "
            f"global={aggregate['global_harmful_promotions']} "
            f"scoped={aggregate['scoped_harmful_promotions']}"
        ),
    ]
    for update in report["updates"]:
        lines.append(
            f"{update['update_id']}: reversal={str(update['supported_sign_reversal']).lower()} "
            f"global={update['global']['selection']} "
            f"scoped={json.dumps(update['scoped']['selection_by_cohort'], sort_keys=True)}"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Evaluate a recorded cohort-scoped publication replay."
    )
    parser.add_argument("suite", type=Path)
    parser.add_argument("--format", choices=("text", "json"), default="text")
    args = parser.parse_args(argv)
    report = evaluate_suite(load_suite(args.suite))
    if args.format == "json":
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(render_text(report))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
