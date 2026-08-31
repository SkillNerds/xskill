"""Evaluate Formation methods from immutable paired outcomes.

The evaluator is runtime-agnostic and never invokes a model, harness, workspace,
or production xskill state.
"""

from __future__ import annotations

import argparse
import json
import math
import random
from collections import Counter, defaultdict
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Callable

SCHEMA_VERSION = 1
METHODS = ("no_skill", "session", "atom", "task_grounded", "gold_task")
FORMATION_METHODS = METHODS[1:]
MODE_IDS = ("natural_output", "matched_budget")
COHORTS = ("novel", "known", "negative")
RESOURCE_FIELDS = ("input_tokens", "output_tokens")
FINGERPRINT_PREFIX = "sha256:"
MAX_BOOTSTRAP_OPERATIONS = 10_000_000


class FormationEffectValidationError(ValueError):
    """Raised when a Formation effect suite violates its data contract."""


def _require(mapping: dict[str, Any], key: str, expected: type, context: str) -> Any:
    if key not in mapping:
        raise FormationEffectValidationError(
            f"{context}: missing required field {key!r}"
        )
    value = mapping[key]
    if expected is int and isinstance(value, bool):
        raise FormationEffectValidationError(f"{context}.{key}: expected int")
    if not isinstance(value, expected):
        raise FormationEffectValidationError(
            f"{context}.{key}: expected {expected.__name__}, "
            f"got {type(value).__name__}"
        )
    return value


def _require_keys(
    mapping: dict[str, Any], expected: set[str], context: str
) -> None:
    actual = set(mapping)
    if actual != expected:
        missing = sorted(expected - actual)
        unknown = sorted(actual - expected)
        raise FormationEffectValidationError(
            f"{context}: field mismatch; missing={missing}, unknown={unknown}"
        )


def _non_empty_string(mapping: dict[str, Any], key: str, context: str) -> str:
    value = _require(mapping, key, str, context)
    if not value:
        raise FormationEffectValidationError(f"{context}.{key}: must not be empty")
    return value


def _number(
    mapping: dict[str, Any], key: str, context: str, *, minimum: float = 0.0
) -> float:
    if key not in mapping:
        raise FormationEffectValidationError(
            f"{context}: missing required field {key!r}"
        )
    value = mapping[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise FormationEffectValidationError(f"{context}.{key}: expected a number")
    result = float(value)
    if not math.isfinite(result) or result < minimum:
        raise FormationEffectValidationError(
            f"{context}.{key}: expected a finite number >= {minimum:g}"
        )
    return result


def _fingerprint(value: Any, context: str) -> None:
    if (
        not isinstance(value, str)
        or not value.startswith(FINGERPRINT_PREFIX)
        or len(value) != len(FINGERPRINT_PREFIX) + 64
        or any(
            char not in "0123456789abcdef"
            for char in value[len(FINGERPRINT_PREFIX) :]
        )
    ):
        raise FormationEffectValidationError(
            f"{context}: expected sha256:<64 lowercase hex>"
        )


def _validate_observation(
    value: Any,
    *,
    context: str,
    run_ids: set[str],
    expects_skill: bool,
    is_control: bool,
) -> None:
    if not isinstance(value, dict):
        raise FormationEffectValidationError(f"{context}: expected an object")
    _require_keys(
        value,
        {
            "run_id",
            "score",
            "relevant_skill_activated",
            "activated_skills",
            "input_tokens",
            "output_tokens",
            "error_type",
        },
        context,
    )
    run_id = _non_empty_string(value, "run_id", context)
    if run_id in run_ids:
        raise FormationEffectValidationError(
            f"{context}.run_id: duplicate {run_id!r}"
        )
    run_ids.add(run_id)
    score = _number(value, "score", context)
    if score > 1:
        raise FormationEffectValidationError(
            f"{context}.score: expected 0 <= score <= 1"
        )
    relevant = _require(value, "relevant_skill_activated", bool, context)
    activated = _require(value, "activated_skills", list, context)
    if any(not isinstance(item, str) or not item for item in activated):
        raise FormationEffectValidationError(
            f"{context}.activated_skills: expected non-empty strings"
        )
    if len(activated) != len(set(activated)):
        raise FormationEffectValidationError(
            f"{context}.activated_skills: duplicate entries are not allowed"
        )
    if relevant and not activated:
        raise FormationEffectValidationError(
            f"{context}: relevant_skill_activated requires an activated Skill"
        )
    if not expects_skill and relevant:
        raise FormationEffectValidationError(
            f"{context}: a negative case cannot activate a relevant Skill"
        )
    if is_control and (activated or relevant):
        raise FormationEffectValidationError(
            f"{context}: no_skill control cannot activate Skills"
        )
    for field in RESOURCE_FIELDS:
        amount = _require(value, field, int, context)
        if amount < 0:
            raise FormationEffectValidationError(
                f"{context}.{field}: must be >= 0"
            )
    error_type = value["error_type"]
    if error_type is not None and (
        not isinstance(error_type, str) or not error_type
    ):
        raise FormationEffectValidationError(
            f"{context}.error_type: expected null or a non-empty string"
        )


def validate_suite(suite: Any) -> None:
    """Validate all pairing, provenance, budget, and outcome invariants."""
    if not isinstance(suite, dict):
        raise FormationEffectValidationError("suite: expected an object")
    _require_keys(
        suite,
        {
            "schema_version",
            "suite_id",
            "metric_config",
            "decision_policy",
            "run_manifest",
            "modes",
            "cases",
        },
        "suite",
    )
    version = _require(suite, "schema_version", int, "suite")
    if version != SCHEMA_VERSION:
        raise FormationEffectValidationError(
            f"suite.schema_version: supported={SCHEMA_VERSION}, got={version}"
        )
    _non_empty_string(suite, "suite_id", "suite")

    metric = _require(suite, "metric_config", dict, "suite")
    _require_keys(
        metric,
        {"pass_threshold", "bootstrap_samples", "bootstrap_seed", "confidence_level"},
        "suite.metric_config",
    )
    pass_threshold = _number(metric, "pass_threshold", "suite.metric_config")
    if pass_threshold > 1:
        raise FormationEffectValidationError(
            "suite.metric_config.pass_threshold must be <= 1"
        )
    samples = _require(metric, "bootstrap_samples", int, "suite.metric_config")
    if not 100 <= samples <= 50_000:
        raise FormationEffectValidationError(
            "suite.metric_config.bootstrap_samples must be within [100, 50000]"
        )
    seed = _require(metric, "bootstrap_seed", int, "suite.metric_config")
    if seed < 0:
        raise FormationEffectValidationError(
            "suite.metric_config.bootstrap_seed must be >= 0"
        )
    confidence = _number(metric, "confidence_level", "suite.metric_config")
    if not 0 < confidence < 1:
        raise FormationEffectValidationError(
            "suite.metric_config.confidence_level must satisfy 0 < value < 1"
        )

    policy = _require(suite, "decision_policy", dict, "suite")
    _require_keys(
        policy,
        {
            "primary_mode",
            "minimum_primary_pass_rate_delta",
            "minimum_primary_ci_lower",
            "maximum_false_trigger_rate_delta",
            "maximum_known_pass_rate_drop",
            "minimum_improved_task_families",
            "require_matched_budget_positive",
        },
        "suite.decision_policy",
    )
    primary_mode = _non_empty_string(policy, "primary_mode", "suite.decision_policy")
    if primary_mode not in MODE_IDS:
        raise FormationEffectValidationError(
            f"suite.decision_policy.primary_mode: expected one of {MODE_IDS}"
        )
    for field in (
        "minimum_primary_pass_rate_delta",
        "minimum_primary_ci_lower",
        "maximum_false_trigger_rate_delta",
        "maximum_known_pass_rate_drop",
    ):
        value = policy[field]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise FormationEffectValidationError(
                f"suite.decision_policy.{field}: expected a number"
            )
        if not math.isfinite(float(value)) or not -1 <= float(value) <= 1:
            raise FormationEffectValidationError(
                f"suite.decision_policy.{field}: expected a finite value within [-1, 1]"
            )
    for field in (
        "minimum_primary_pass_rate_delta",
        "maximum_known_pass_rate_drop",
    ):
        if float(policy[field]) < 0:
            raise FormationEffectValidationError(
                f"suite.decision_policy.{field}: expected a value within [0, 1]"
            )
    families = _require(
        policy,
        "minimum_improved_task_families",
        int,
        "suite.decision_policy",
    )
    if families < 1:
        raise FormationEffectValidationError(
            "suite.decision_policy.minimum_improved_task_families must be >= 1"
        )
    _require(
        policy,
        "require_matched_budget_positive",
        bool,
        "suite.decision_policy",
    )

    manifest = _require(suite, "run_manifest", dict, "suite")
    _require_keys(
        manifest,
        {
            "repository_revision",
            "recorded_at",
            "training_set_fingerprint",
            "held_out_task_set_fingerprint",
            "evaluation_protocol_fingerprint",
            "runtime_config_fingerprint",
            "scorer_fingerprint",
            "formation_config_fingerprint",
            "skill_generation_config_fingerprint",
            "activation_config_fingerprint",
        },
        "suite.run_manifest",
    )
    _non_empty_string(manifest, "repository_revision", "suite.run_manifest")
    _non_empty_string(manifest, "recorded_at", "suite.run_manifest")
    for field in set(manifest) - {"repository_revision", "recorded_at"}:
        _fingerprint(manifest[field], f"suite.run_manifest.{field}")
    if manifest["training_set_fingerprint"] == manifest["held_out_task_set_fingerprint"]:
        raise FormationEffectValidationError(
            "suite.run_manifest: training and held-out fingerprints must differ"
        )

    modes = _require(suite, "modes", list, "suite")
    if len(modes) != len(MODE_IDS):
        raise FormationEffectValidationError(
            f"suite.modes: expected exactly {list(MODE_IDS)}"
        )
    modes_by_id: dict[str, dict[str, Any]] = {}
    evidence_by_method: dict[str, str] = {}
    for index, mode in enumerate(modes):
        context = f"suite.modes[{index}]"
        if not isinstance(mode, dict):
            raise FormationEffectValidationError(f"{context}: expected an object")
        mode_id = _non_empty_string(mode, "mode_id", context)
        if mode_id in modes_by_id:
            raise FormationEffectValidationError(f"{context}.mode_id: duplicate")
        if mode_id == "natural_output":
            _require_keys(mode, {"mode_id", "budget_policy", "arms"}, context)
            if mode["budget_policy"] != "natural_output":
                raise FormationEffectValidationError(
                    f"{context}.budget_policy: expected 'natural_output'"
                )
        elif mode_id == "matched_budget":
            _require_keys(
                mode,
                {"mode_id", "budget_policy", "budget_value", "arms"},
                context,
            )
            if mode["budget_policy"] != "matched_serialized_skill_tokens":
                raise FormationEffectValidationError(
                    f"{context}.budget_policy: expected "
                    "'matched_serialized_skill_tokens'"
                )
            budget = _require(mode, "budget_value", int, context)
            if budget <= 0:
                raise FormationEffectValidationError(
                    f"{context}.budget_value: must be > 0"
                )
        else:
            raise FormationEffectValidationError(
                f"{context}.mode_id: expected one of {MODE_IDS}"
            )
        arms = _require(mode, "arms", dict, context)
        if set(arms) != set(FORMATION_METHODS):
            raise FormationEffectValidationError(
                f"{context}.arms: expected exactly {list(FORMATION_METHODS)}"
            )
        library_fingerprints: set[str] = set()
        for method in FORMATION_METHODS:
            arm_context = f"{context}.arms.{method}"
            arm = arms[method]
            if not isinstance(arm, dict):
                raise FormationEffectValidationError(
                    f"{arm_context}: expected an object"
                )
            _require_keys(
                arm,
                {
                    "evidence_fingerprint",
                    "skill_library_fingerprint",
                    "skill_count",
                    "serialized_skill_tokens",
                },
                arm_context,
            )
            for field in ("evidence_fingerprint", "skill_library_fingerprint"):
                _fingerprint(arm[field], f"{arm_context}.{field}")
            if arm["skill_library_fingerprint"] in library_fingerprints:
                raise FormationEffectValidationError(
                    f"{context}.arms: each method requires an independent Skill library"
                )
            library_fingerprints.add(arm["skill_library_fingerprint"])
            skill_count = _require(arm, "skill_count", int, arm_context)
            skill_tokens = _require(
                arm, "serialized_skill_tokens", int, arm_context
            )
            if skill_count <= 0 or skill_tokens <= 0:
                raise FormationEffectValidationError(
                    f"{arm_context}: Skill count and tokens must be > 0"
                )
            if mode_id == "matched_budget" and skill_tokens != mode["budget_value"]:
                raise FormationEffectValidationError(
                    f"{arm_context}.serialized_skill_tokens: must equal matched budget"
                )
            previous = evidence_by_method.setdefault(
                method, arm["evidence_fingerprint"]
            )
            if previous != arm["evidence_fingerprint"]:
                raise FormationEffectValidationError(
                    f"{arm_context}.evidence_fingerprint: evidence must remain fixed "
                    "across budget modes"
                )
        modes_by_id[mode_id] = mode
    if set(modes_by_id) != set(MODE_IDS):
        raise FormationEffectValidationError(
            f"suite.modes: expected exactly {list(MODE_IDS)}"
        )

    cases = _require(suite, "cases", list, "suite")
    if not cases:
        raise FormationEffectValidationError("suite.cases must not be empty")
    case_ids: set[str] = set()
    run_ids: set[str] = set()
    pair_keys: set[tuple[str, int]] = set()
    seeds_by_task: dict[str, set[int]] = defaultdict(set)
    cohorts: set[str] = set()
    task_families: set[str] = set()
    task_metadata: dict[str, tuple[str, str, bool]] = {}
    for index, case in enumerate(cases):
        context = f"suite.cases[{index}]"
        if not isinstance(case, dict):
            raise FormationEffectValidationError(f"{context}: expected an object")
        _require_keys(
            case,
            {
                "case_id",
                "task_fingerprint",
                "task_family",
                "seed",
                "cohort",
                "expects_skill",
                "control",
                "observations",
            },
            context,
        )
        case_id = _non_empty_string(case, "case_id", context)
        if case_id in case_ids:
            raise FormationEffectValidationError(f"{context}.case_id: duplicate")
        case_ids.add(case_id)
        _fingerprint(case["task_fingerprint"], f"{context}.task_fingerprint")
        task_family = _non_empty_string(case, "task_family", context)
        task_families.add(task_family)
        seed_value = _require(case, "seed", int, context)
        if seed_value < 0:
            raise FormationEffectValidationError(f"{context}.seed: must be >= 0")
        pair_key = (case["task_fingerprint"], seed_value)
        if pair_key in pair_keys:
            raise FormationEffectValidationError(
                f"{context}: duplicate task_fingerprint × seed pair"
            )
        pair_keys.add(pair_key)
        seeds_by_task[case["task_fingerprint"]].add(seed_value)
        cohort = _non_empty_string(case, "cohort", context)
        if cohort not in COHORTS:
            raise FormationEffectValidationError(
                f"{context}.cohort: expected one of {COHORTS}"
            )
        cohorts.add(cohort)
        expects_skill = _require(case, "expects_skill", bool, context)
        if expects_skill != (cohort != "negative"):
            raise FormationEffectValidationError(
                f"{context}: expects_skill must be false only for negative cases"
            )
        metadata = (task_family, cohort, expects_skill)
        previous_metadata = task_metadata.setdefault(case["task_fingerprint"], metadata)
        if previous_metadata != metadata:
            raise FormationEffectValidationError(
                f"{context}: repeated task_fingerprint changed task metadata"
            )
        _validate_observation(
            case["control"],
            context=f"{context}.control",
            run_ids=run_ids,
            expects_skill=expects_skill,
            is_control=True,
        )
        observations = _require(case, "observations", dict, context)
        if set(observations) != set(MODE_IDS):
            raise FormationEffectValidationError(
                f"{context}.observations: expected exactly {list(MODE_IDS)}"
            )
        for mode_id in MODE_IDS:
            mode_observations = observations[mode_id]
            mode_context = f"{context}.observations.{mode_id}"
            if not isinstance(mode_observations, dict):
                raise FormationEffectValidationError(
                    f"{mode_context}: expected an object"
                )
            if set(mode_observations) != set(FORMATION_METHODS):
                raise FormationEffectValidationError(
                    f"{mode_context}: expected exactly {list(FORMATION_METHODS)}"
                )
            for method in FORMATION_METHODS:
                _validate_observation(
                    mode_observations[method],
                    context=f"{mode_context}.{method}",
                    run_ids=run_ids,
                    expects_skill=expects_skill,
                    is_control=False,
                )
    if cohorts != set(COHORTS):
        raise FormationEffectValidationError(
            f"suite.cases: expected all cohorts {list(COHORTS)}"
        )
    if len(task_families) < 2:
        raise FormationEffectValidationError(
            "suite.cases: at least two task families are required"
        )
    expected_seeds = next(iter(seeds_by_task.values()))
    if any(seeds != expected_seeds for seeds in seeds_by_task.values()):
        raise FormationEffectValidationError(
            "suite.cases: every task_fingerprint must use the same seed set"
        )
    if families > len(task_families):
        raise FormationEffectValidationError(
            "suite.decision_policy.minimum_improved_task_families exceeds "
            "available task families"
        )
    statistics_per_mode = 12 + len(RESOURCE_FIELDS) + len(task_families)
    estimated_operations = (
        len(cases)
        * samples
        * len(MODE_IDS)
        * statistics_per_mode
    )
    if estimated_operations > MAX_BOOTSTRAP_OPERATIONS:
        raise FormationEffectValidationError(
            "suite: estimated bootstrap work exceeds "
            f"{MAX_BOOTSTRAP_OPERATIONS:,} paired observations"
        )


def _round(value: float) -> float:
    return round(float(value), 6)


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _percentile(values: list[float], quantile: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] * (1 - fraction) + ordered[upper] * fraction


def _observation(case: dict[str, Any], mode_id: str, method: str) -> dict[str, Any]:
    if method == "no_skill":
        return case["control"]
    return case["observations"][mode_id][method]


def _paired_stat(
    cases: list[dict[str, Any]],
    mode_id: str,
    treated: str,
    control: str,
    metric: Callable[[dict[str, Any]], float],
    metric_config: dict[str, Any],
    *,
    include: Callable[[dict[str, Any]], bool] | None = None,
    seed_offset: int = 0,
) -> dict[str, Any]:
    selected = [case for case in cases if include is None or include(case)]
    deltas = [
        metric(_observation(case, mode_id, treated))
        - metric(_observation(case, mode_id, control))
        for case in selected
    ]
    by_task: dict[str, list[float]] = defaultdict(list)
    for case, delta in zip(selected, deltas):
        by_task[case["task_fingerprint"]].append(delta)
    interval = None
    if len(by_task) >= 2:
        rng = random.Random(metric_config["bootstrap_seed"] + seed_offset)
        task_ids = sorted(by_task)
        samples: list[float] = []
        for _ in range(metric_config["bootstrap_samples"]):
            drawn: list[float] = []
            for _ in task_ids:
                drawn.extend(by_task[rng.choice(task_ids)])
            samples.append(_mean(drawn))
        alpha = (1 - metric_config["confidence_level"]) / 2
        interval = [
            _round(_percentile(samples, alpha)),
            _round(_percentile(samples, 1 - alpha)),
        ]
    return {
        "n_pairs": len(deltas),
        "n_tasks": len(by_task),
        "mean": _round(_mean(deltas)),
        "confidence_interval": interval,
        "wins": sum(delta > 0 for delta in deltas),
        "ties": sum(delta == 0 for delta in deltas),
        "losses": sum(delta < 0 for delta in deltas),
    }


def _mcnemar(
    cases: list[dict[str, Any]],
    mode_id: str,
    treated: str,
    control: str,
    pass_threshold: float,
) -> dict[str, Any]:
    paired_passes_by_task: dict[str, list[tuple[bool, bool]]] = defaultdict(list)
    for case in cases:
        paired_passes_by_task[case["task_fingerprint"]].append(
            (
                _observation(case, mode_id, treated)["score"] >= pass_threshold,
                _observation(case, mode_id, control)["score"] >= pass_threshold,
            )
        )
    treated_only = 0
    control_only = 0
    for task_id in sorted(paired_passes_by_task):
        paired_passes = paired_passes_by_task[task_id]
        treated_passed = _mean([float(pair[0]) for pair in paired_passes]) >= 0.5
        control_passed = _mean([float(pair[1]) for pair in paired_passes]) >= 0.5
        treated_only += int(treated_passed and not control_passed)
        control_only += int(control_passed and not treated_passed)
    discordant = treated_only + control_only
    if discordant == 0:
        p_value = 1.0
    else:
        tail = sum(
            math.comb(discordant, index)
            for index in range(min(treated_only, control_only) + 1)
        ) / (2**discordant)
        p_value = min(1.0, 2 * tail)
    return {
        "n_tasks": len(paired_passes_by_task),
        "task_pass_rule": "seed_pass_rate>=0.5",
        "treated_only_pass": treated_only,
        "control_only_pass": control_only,
        "discordant_tasks": discordant,
        "exact_two_sided_p": _round(p_value),
    }


def _summary(
    cases: list[dict[str, Any]],
    mode_id: str,
    method: str,
    pass_threshold: float,
) -> dict[str, Any]:
    observations = [_observation(case, mode_id, method) for case in cases]
    positive = [
        (case, observation)
        for case, observation in zip(cases, observations)
        if case["expects_skill"]
    ]
    negative = [
        (case, observation)
        for case, observation in zip(cases, observations)
        if not case["expects_skill"]
    ]
    errors = Counter(
        observation["error_type"]
        for observation in observations
        if observation["error_type"] is not None
    )
    result = {
        "n": len(cases),
        "mean_score": _round(_mean([item["score"] for item in observations])),
        "pass_rate": _round(
            _mean([float(item["score"] >= pass_threshold) for item in observations])
        ),
        "positive_recall": _round(
            _mean([float(item["relevant_skill_activated"]) for _, item in positive])
        ),
        "negative_false_trigger_rate": _round(
            _mean([float(bool(item["activated_skills"])) for _, item in negative])
        ),
        "novel_pass_rate": _round(
            _mean(
                [
                    float(item["score"] >= pass_threshold)
                    for case, item in zip(cases, observations)
                    if case["cohort"] == "novel"
                ]
            )
        ),
        "known_pass_rate": _round(
            _mean(
                [
                    float(item["score"] >= pass_threshold)
                    for case, item in zip(cases, observations)
                    if case["cohort"] == "known"
                ]
            )
        ),
        "errors": dict(sorted(errors.items())),
        "resources": {
            field: _round(_mean([float(item[field]) for item in observations]))
            for field in RESOURCE_FIELDS
        },
    }
    return result


def _contrast(
    cases: list[dict[str, Any]],
    mode_id: str,
    treated: str,
    control: str,
    metric_config: dict[str, Any],
    *,
    detailed: bool,
) -> dict[str, Any]:
    pass_threshold = metric_config["pass_threshold"]
    metrics: dict[
        str,
        tuple[
            Callable[[dict[str, Any]], float],
            Callable[[dict[str, Any]], bool] | None,
        ],
    ] = {
        "score_delta": (lambda item: item["score"], None),
        "pass_rate_delta": (
            lambda item: float(item["score"] >= pass_threshold),
            None,
        ),
    }
    if detailed:
        metrics.update(
            {
                "positive_recall_delta": (
                    lambda item: float(item["relevant_skill_activated"]),
                    lambda case: case["expects_skill"],
                ),
                "false_trigger_rate_delta": (
                    lambda item: float(bool(item["activated_skills"])),
                    lambda case: not case["expects_skill"],
                ),
                "novel_pass_rate_delta": (
                    lambda item: float(item["score"] >= pass_threshold),
                    lambda case: case["cohort"] == "novel",
                ),
                "known_pass_rate_delta": (
                    lambda item: float(item["score"] >= pass_threshold),
                    lambda case: case["cohort"] == "known",
                ),
            }
        )
    result = {}
    for offset, (name, (metric, include)) in enumerate(metrics.items()):
        result[name] = _paired_stat(
            cases,
            mode_id,
            treated,
            control,
            metric,
            metric_config,
            include=include,
            seed_offset=offset,
        )
    if detailed:
        result["resource_deltas"] = {
            field: _paired_stat(
                cases,
                mode_id,
                treated,
                control,
                lambda item, resource=field: float(item[resource]),
                metric_config,
                seed_offset=100 + offset,
            )
            for offset, field in enumerate(RESOURCE_FIELDS)
        }
        result["score_delta_by_task_family"] = {
            family: _paired_stat(
                cases,
                mode_id,
                treated,
                control,
                lambda item: item["score"],
                metric_config,
                include=lambda case, expected=family: (
                    case["task_family"] == expected
                ),
                seed_offset=200 + index,
            )
            for index, family in enumerate(
                sorted({case["task_family"] for case in cases})
            )
        }
    result["mcnemar_pass"] = _mcnemar(
        cases, mode_id, treated, control, pass_threshold
    )
    return result


def _mode_report(
    suite: dict[str, Any], mode: dict[str, Any]
) -> dict[str, Any]:
    mode_id = mode["mode_id"]
    cases = suite["cases"]
    metric = suite["metric_config"]
    summaries = {
        method: _summary(cases, mode_id, method, metric["pass_threshold"])
        for method in METHODS
    }
    arms = {
        "no_skill": {
            "skill_count": 0,
            "serialized_skill_tokens": 0,
        },
        **mode["arms"],
    }
    control_score = _mean(
        [_observation(case, mode_id, "no_skill")["score"] for case in cases]
    )
    utility_density = {"no_skill": None}
    for method in FORMATION_METHODS:
        method_score = _mean(
            [_observation(case, mode_id, method)["score"] for case in cases]
        )
        delta = method_score - control_score
        denominator = arms[method]["serialized_skill_tokens"] / 1000
        utility_density[method] = _round(delta / denominator)
    contrasts = {
        "task_grounded_minus_atom": _contrast(
            cases,
            mode_id,
            "task_grounded",
            "atom",
            metric,
            detailed=True,
        ),
        "task_grounded_minus_session": _contrast(
            cases,
            mode_id,
            "task_grounded",
            "session",
            metric,
            detailed=False,
        ),
        "task_grounded_minus_no_skill": _contrast(
            cases,
            mode_id,
            "task_grounded",
            "no_skill",
            metric,
            detailed=False,
        ),
        "gold_task_minus_task_grounded": _contrast(
            cases,
            mode_id,
            "gold_task",
            "task_grounded",
            metric,
            detailed=False,
        ),
    }
    result = {
        "mode_id": mode_id,
        "budget_policy": mode["budget_policy"],
        "artifacts": arms,
        "methods": summaries,
        "utility_density_per_1000_skill_tokens": utility_density,
        "contrasts": contrasts,
    }
    if "budget_value" in mode:
        result["budget_value"] = mode["budget_value"]
    return result


def _decision(
    suite: dict[str, Any], reports: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    policy = suite["decision_policy"]
    primary = reports[policy["primary_mode"]]["contrasts"][
        "task_grounded_minus_atom"
    ]
    pass_delta = primary["pass_rate_delta"]
    interval = pass_delta["confidence_interval"]
    checks = {
        "minimum_pass_rate_delta": (
            pass_delta["mean"] >= policy["minimum_primary_pass_rate_delta"]
        ),
        "confidence_interval_lower_bound": (
            interval is not None
            and interval[0] > policy["minimum_primary_ci_lower"]
        ),
        "false_trigger_not_worse": (
            primary["false_trigger_rate_delta"]["mean"]
            <= policy["maximum_false_trigger_rate_delta"]
        ),
        "known_tasks_not_regressed": (
            primary["known_pass_rate_delta"]["mean"]
            >= -policy["maximum_known_pass_rate_drop"]
        ),
        "enough_improved_task_families": (
            sum(
                effect["mean"] > 0
                for effect in primary["score_delta_by_task_family"].values()
            )
            >= policy["minimum_improved_task_families"]
        ),
    }
    matched_delta = reports["matched_budget"]["contrasts"][
        "task_grounded_minus_atom"
    ]["pass_rate_delta"]["mean"]
    checks["matched_budget_same_direction"] = (
        not policy["require_matched_budget_positive"] or matched_delta > 0
    )
    return {
        "primary_mode": policy["primary_mode"],
        "primary_contrast": "task_grounded_minus_atom",
        "checks": checks,
        "passed": all(checks.values()),
    }


def evaluate_suite(suite: dict[str, Any]) -> dict[str, Any]:
    """Return a deterministic report after validating the complete suite."""
    validate_suite(suite)
    modes_by_id = {mode["mode_id"]: mode for mode in suite["modes"]}
    mode_reports = [_mode_report(suite, modes_by_id[mode_id]) for mode_id in MODE_IDS]
    reports_by_id = {report["mode_id"]: report for report in mode_reports}
    return {
        "schema_version": SCHEMA_VERSION,
        "suite_id": suite["suite_id"],
        "run_manifest": suite["run_manifest"],
        "metric_config": suite["metric_config"],
        "decision_policy": suite["decision_policy"],
        "n_cases": len(suite["cases"]),
        "n_tasks": len({case["task_fingerprint"] for case in suite["cases"]}),
        "modes": mode_reports,
        "decision": _decision(suite, reports_by_id),
    }


def load_suite(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise FormationEffectValidationError("suite: expected an object")
    return value


def render_text(report: dict[str, Any]) -> str:
    lines = [
        f"suite: {report['suite_id']}",
        f"paired cases: {report['n_cases']} (tasks={report['n_tasks']})",
    ]
    for mode in report["modes"]:
        primary = mode["contrasts"]["task_grounded_minus_atom"]
        lines.extend(
            [
                f"[{mode['mode_id']}]",
                (
                    "  task_grounded - atom pass-rate: "
                    f"{primary['pass_rate_delta']['mean']:+.6f}"
                ),
                (
                    "  task_grounded - atom score: "
                    f"{primary['score_delta']['mean']:+.6f}"
                ),
                (
                    "  false-trigger delta: "
                    f"{primary['false_trigger_rate_delta']['mean']:+.6f}"
                ),
            ]
        )
    lines.append(f"decision: {'PASS' if report['decision']['passed'] else 'FAIL'}")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Evaluate paired Formation-method outcomes without runtime calls."
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


if __name__ == "__main__":
    raise SystemExit(main())
