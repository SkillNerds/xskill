"""Evaluate matched Skill body/description experiments without model calls.

The evaluator consumes recorded native-runtime outcomes.
It never invokes a model, harness, workspace command, or production xskill state.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
from collections import Counter
from collections.abc import Iterable
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
BODY_VERSIONS = ("old_body", "new_body")
CELL_KEYS = (
    "old_body__old_description",
    "old_body__new_description",
    "new_body__old_description",
    "new_body__new_description",
)
RESOURCE_FIELDS = (
    "activated_skill_count",
    "catalog_tokens",
    "loaded_skill_tokens",
    "input_tokens",
    "output_tokens",
    "cost_usd",
    "latency_ms",
)
_SHA256_RE = "sha256:"


class LibraryReplayValidationError(ValueError):
    """Raised when a library-aware replay violates its data contract."""


def _require(mapping: dict[str, Any], key: str, expected: type, context: str) -> Any:
    if key not in mapping:
        raise LibraryReplayValidationError(f"{context}: missing required field {key!r}")
    value = mapping[key]
    if expected is int and isinstance(value, bool):
        raise LibraryReplayValidationError(f"{context}.{key}: expected int, got bool")
    if not isinstance(value, expected):
        raise LibraryReplayValidationError(
            f"{context}.{key}: expected {expected.__name__}, got {type(value).__name__}"
        )
    return value


def _require_non_empty_string(mapping: dict[str, Any], key: str, context: str) -> str:
    value = _require(mapping, key, str, context)
    if not value:
        raise LibraryReplayValidationError(f"{context}.{key}: must not be empty")
    return value


def _require_number(
    mapping: dict[str, Any], key: str, context: str, *, minimum: float = 0.0
) -> float:
    if key not in mapping:
        raise LibraryReplayValidationError(f"{context}: missing required field {key!r}")
    value = mapping[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise LibraryReplayValidationError(f"{context}.{key}: expected a number")
    number = float(value)
    if not math.isfinite(number) or number < minimum:
        raise LibraryReplayValidationError(
            f"{context}.{key}: expected a finite number >= {minimum:g}"
        )
    return number


def _validate_fingerprint(value: Any, context: str) -> None:
    if (
        not isinstance(value, str)
        or not value.startswith(_SHA256_RE)
        or len(value) != len(_SHA256_RE) + 64
        or any(char not in "0123456789abcdef" for char in value[len(_SHA256_RE) :])
    ):
        raise LibraryReplayValidationError(
            f"{context}: expected sha256:<64 lowercase hex>"
        )


def _validate_observation(
    observation: Any,
    *,
    context: str,
    target_skill: str,
    run_ids: set[str],
    isolated: bool,
    allowed_skills: set[str],
) -> None:
    if not isinstance(observation, dict):
        raise LibraryReplayValidationError(f"{context}: expected an object")
    run_id = _require_non_empty_string(observation, "run_id", context)
    if run_id in run_ids:
        raise LibraryReplayValidationError(f"{context}.run_id: duplicate {run_id!r}")
    run_ids.add(run_id)
    score = _require_number(observation, "score", context)
    if score > 1:
        raise LibraryReplayValidationError(f"{context}.score: expected 0 <= score <= 1")
    target_activated = _require(observation, "target_activated", bool, context)
    activated_skills = _require(observation, "activated_skills", list, context)
    if any(not isinstance(skill, str) or not skill for skill in activated_skills):
        raise LibraryReplayValidationError(
            f"{context}.activated_skills: expected non-empty strings"
        )
    if len(activated_skills) != len(set(activated_skills)):
        raise LibraryReplayValidationError(
            f"{context}.activated_skills: duplicate skill names are not allowed"
        )
    unknown_skills = set(activated_skills) - allowed_skills
    if unknown_skills:
        raise LibraryReplayValidationError(
            f"{context}.activated_skills: unknown skills {sorted(unknown_skills)}"
        )
    if target_activated != (target_skill in activated_skills):
        raise LibraryReplayValidationError(
            f"{context}: target_activated disagrees with activated_skills"
        )
    if isolated and activated_skills != [target_skill]:
        raise LibraryReplayValidationError(
            f"{context}.activated_skills: isolated runs must force only target_skill"
        )
    for key in (
        "catalog_tokens",
        "loaded_skill_tokens",
        "input_tokens",
        "output_tokens",
    ):
        value = _require(observation, key, int, context)
        if value < 0:
            raise LibraryReplayValidationError(f"{context}.{key}: must be >= 0")
    _require_number(observation, "cost_usd", context)
    _require_number(observation, "latency_ms", context)
    error_type = observation.get("error_type")
    if error_type is not None and (not isinstance(error_type, str) or not error_type):
        raise LibraryReplayValidationError(
            f"{context}.error_type: expected null or a non-empty string"
        )


def validate_suite(suite: Any) -> None:
    """Validate the complete matched-replay contract before computing metrics."""
    if not isinstance(suite, dict):
        raise LibraryReplayValidationError("suite: expected an object")
    version = _require(suite, "schema_version", int, "suite")
    if version != SCHEMA_VERSION:
        raise LibraryReplayValidationError(
            f"suite.schema_version: supported={SCHEMA_VERSION}, got={version}"
        )
    _require_non_empty_string(suite, "suite_id", "suite")
    target_skill = _require_non_empty_string(suite, "target_skill", "suite")

    metric_config = _require(suite, "metric_config", dict, "suite")
    bootstrap_samples = _require(
        metric_config, "bootstrap_samples", int, "suite.metric_config"
    )
    if not 100 <= bootstrap_samples <= 50_000:
        raise LibraryReplayValidationError(
            "suite.metric_config.bootstrap_samples must be within [100, 50000]"
        )
    bootstrap_seed = _require(
        metric_config, "bootstrap_seed", int, "suite.metric_config"
    )
    if bootstrap_seed < 0:
        raise LibraryReplayValidationError(
            "suite.metric_config.bootstrap_seed must be >= 0"
        )
    confidence_level = _require_number(
        metric_config, "confidence_level", "suite.metric_config"
    )
    if not 0 < confidence_level < 1:
        raise LibraryReplayValidationError(
            "suite.metric_config.confidence_level must satisfy 0 < value < 1"
        )

    manifest = _require(suite, "run_manifest", dict, "suite")
    for key in (
        "repository_revision",
        "model",
        "harness",
        "generated_at",
    ):
        _require_non_empty_string(manifest, key, "suite.run_manifest")
    for key in (
        "task_set_fingerprint",
        "evaluation_protocol_fingerprint",
        "old_body_fingerprint",
        "new_body_fingerprint",
        "old_description_fingerprint",
        "new_description_fingerprint",
    ):
        _validate_fingerprint(manifest.get(key), f"suite.run_manifest.{key}")
    if manifest["old_body_fingerprint"] == manifest["new_body_fingerprint"]:
        raise LibraryReplayValidationError(
            "suite.run_manifest: old and new body fingerprints must differ"
        )
    if (
        manifest["old_description_fingerprint"]
        == manifest["new_description_fingerprint"]
    ):
        raise LibraryReplayValidationError(
            "suite.run_manifest: old and new description fingerprints must differ"
        )
    generation_config = _require(
        manifest, "generation_config", dict, "suite.run_manifest"
    )
    _require_number(generation_config, "temperature", "generation_config")
    top_p = _require_number(generation_config, "top_p", "generation_config")
    if not 0 < top_p <= 1:
        raise LibraryReplayValidationError(
            "generation_config.top_p must satisfy 0 < value <= 1"
        )
    max_output_tokens = _require(
        generation_config, "max_output_tokens", int, "generation_config"
    )
    if max_output_tokens <= 0:
        raise LibraryReplayValidationError(
            "generation_config.max_output_tokens must be > 0"
        )

    ladder = _require(suite, "library_ladder", list, "suite")
    if not ladder:
        raise LibraryReplayValidationError("suite.library_ladder must not be empty")
    distractor_counts: list[int] = []
    previous_skills: list[str] = []
    for index, level in enumerate(ladder):
        context = f"suite.library_ladder[{index}]"
        if not isinstance(level, dict):
            raise LibraryReplayValidationError(f"{context}: expected an object")
        count = _require(level, "distractor_count", int, context)
        if count < 0:
            raise LibraryReplayValidationError(
                f"{context}.distractor_count: must be >= 0"
            )
        skills = _require(level, "distractor_skills", list, context)
        _validate_fingerprint(
            level.get("distractor_catalog_fingerprint"),
            f"{context}.distractor_catalog_fingerprint",
        )
        if len(skills) != count:
            raise LibraryReplayValidationError(
                f"{context}.distractor_skills: expected exactly {count} entries"
            )
        if any(not isinstance(skill, str) or not skill for skill in skills):
            raise LibraryReplayValidationError(
                f"{context}.distractor_skills: expected non-empty strings"
            )
        skill_set = set(skills)
        if len(skill_set) != len(skills) or target_skill in skill_set:
            raise LibraryReplayValidationError(
                f"{context}.distractor_skills: duplicates and target_skill are forbidden"
            )
        if skills[: len(previous_skills)] != previous_skills:
            raise LibraryReplayValidationError(
                f"{context}.distractor_skills: library growth must retain prior order"
            )
        previous_skills = skills
        distractor_counts.append(count)
    if any(
        left >= right for left, right in zip(distractor_counts, distractor_counts[1:])
    ):
        raise LibraryReplayValidationError(
            "suite.library_ladder.distractor_count values must be strictly increasing"
        )
    if distractor_counts[0] != 0:
        raise LibraryReplayValidationError(
            "suite.library_ladder must start with distractor_count=0"
        )

    cases = _require(suite, "cases", list, "suite")
    if not cases:
        raise LibraryReplayValidationError("suite.cases must not be empty")
    case_ids: set[str] = set()
    task_seed_pairs: set[tuple[str, int]] = set()
    run_ids: set[str] = set()
    activation_labels: set[bool] = set()
    for case_index, case in enumerate(cases):
        context = f"suite.cases[{case_index}]"
        if not isinstance(case, dict):
            raise LibraryReplayValidationError(f"{context}: expected an object")
        case_id = _require_non_empty_string(case, "case_id", context)
        if case_id in case_ids:
            raise LibraryReplayValidationError(
                f"{context}.case_id: duplicate {case_id!r}"
            )
        case_ids.add(case_id)
        task_fingerprint = case.get("task_fingerprint")
        _validate_fingerprint(task_fingerprint, f"{context}.task_fingerprint")
        seed = _require(case, "seed", int, context)
        if seed < 0:
            raise LibraryReplayValidationError(f"{context}.seed: must be >= 0")
        pair = (task_fingerprint, seed)
        if pair in task_seed_pairs:
            raise LibraryReplayValidationError(
                f"{context}: duplicate task_fingerprint and seed pair"
            )
        task_seed_pairs.add(pair)
        should_activate = _require(case, "should_activate", bool, context)
        activation_labels.add(should_activate)

        isolated_runs = _require(case, "isolated", dict, context)
        if set(isolated_runs) != set(BODY_VERSIONS):
            raise LibraryReplayValidationError(
                f"{context}.isolated: expected exactly {list(BODY_VERSIONS)}"
            )
        for body in BODY_VERSIONS:
            _validate_observation(
                isolated_runs[body],
                context=f"{context}.isolated.{body}",
                target_skill=target_skill,
                run_ids=run_ids,
                isolated=True,
                allowed_skills={target_skill},
            )

        libraries = _require(case, "libraries", list, context)
        if len(libraries) != len(distractor_counts):
            raise LibraryReplayValidationError(
                f"{context}.libraries: expected {len(distractor_counts)} levels"
            )
        observed_counts: list[int] = []
        for level_index, level in enumerate(libraries):
            level_context = f"{context}.libraries[{level_index}]"
            if not isinstance(level, dict):
                raise LibraryReplayValidationError(
                    f"{level_context}: expected an object"
                )
            observed_counts.append(
                _require(level, "distractor_count", int, level_context)
            )
            deployed = _require(level, "deployed", dict, level_context)
            if set(deployed) != set(CELL_KEYS):
                raise LibraryReplayValidationError(
                    f"{level_context}.deployed: expected exactly {list(CELL_KEYS)}"
                )
            for cell in CELL_KEYS:
                _validate_observation(
                    deployed[cell],
                    context=f"{level_context}.deployed.{cell}",
                    target_skill=target_skill,
                    run_ids=run_ids,
                    isolated=False,
                    allowed_skills={target_skill}
                    | set(ladder[level_index]["distractor_skills"]),
                )
        if observed_counts != distractor_counts:
            raise LibraryReplayValidationError(
                f"{context}.libraries: expected distractor counts {distractor_counts}"
            )
    if activation_labels != {False, True}:
        raise LibraryReplayValidationError(
            "suite.cases must include both positive and negative activation cases"
        )
    if len(cases) * bootstrap_samples > 5_000_000:
        raise LibraryReplayValidationError(
            "suite: cases * bootstrap_samples exceeds the 5000000 work bound"
        )


def load_suite(path: Path | str) -> dict[str, Any]:
    """Load and validate a recorded replay suite."""
    suite_path = Path(path)
    try:
        suite = json.loads(suite_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise LibraryReplayValidationError(
            f"invalid JSON in {suite_path}: {error}"
        ) from error
    validate_suite(suite)
    return suite


def _round(value: float) -> float:
    return round(float(value), 6)


def _mean(values: Iterable[float]) -> float:
    materialized = list(values)
    return sum(materialized) / len(materialized)


def _percentile(sorted_values: list[float], quantile: float) -> float:
    if len(sorted_values) == 1:
        return sorted_values[0]
    position = (len(sorted_values) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return sorted_values[lower]
    weight = position - lower
    return sorted_values[lower] * (1 - weight) + sorted_values[upper] * weight


def _stable_seed(base_seed: int, label: str) -> int:
    digest = hashlib.sha256(label.encode("utf-8")).digest()
    return base_seed ^ int.from_bytes(digest[:8], "big")


def _paired_effect(
    deltas: list[float], metric_config: dict[str, Any], label: str
) -> dict[str, Any]:
    point = _mean(deltas)
    confidence_interval: list[float] | None
    if len(deltas) == 1:
        confidence_interval = None
    else:
        rng = random.Random(_stable_seed(metric_config["bootstrap_seed"], label))
        sample_count = metric_config["bootstrap_samples"]
        size = len(deltas)
        bootstrap_means = [
            sum(deltas[rng.randrange(size)] for _ in range(size)) / size
            for _ in range(sample_count)
        ]
        bootstrap_means.sort()
        alpha = (1 - metric_config["confidence_level"]) / 2
        low = _percentile(bootstrap_means, alpha)
        high = _percentile(bootstrap_means, 1 - alpha)
        confidence_interval = [_round(low), _round(high)]
    return {
        "n": len(deltas),
        "mean": _round(point),
        "confidence_interval": confidence_interval,
        "wins": sum(delta > 0 for delta in deltas),
        "ties": sum(delta == 0 for delta in deltas),
        "losses": sum(delta < 0 for delta in deltas),
    }


def _observation_value(observation: dict[str, Any], field: str) -> float:
    if field == "activated_skill_count":
        return float(len(observation["activated_skills"]))
    return float(observation[field])


def _observation_summary(rows: list[tuple[dict[str, Any], bool]]) -> dict[str, Any]:
    positives = [observation for observation, label in rows if label]
    negatives = [observation for observation, label in rows if not label]
    observations = [observation for observation, _ in rows]
    activated_skill_counts = Counter(
        skill
        for observation in observations
        for skill in observation["activated_skills"]
    )
    error_types = Counter(
        observation["error_type"]
        for observation in observations
        if observation.get("error_type") is not None
    )
    return {
        "score_mean": _round(_mean(row["score"] for row in observations)),
        "target_activation_rate": _round(
            _mean(float(row["target_activated"]) for row in observations)
        ),
        "positive_recall": _round(
            _mean(float(row["target_activated"]) for row in positives)
        ),
        "negative_false_positive_rate": _round(
            _mean(float(row["target_activated"]) for row in negatives)
        ),
        "activated_skill_counts": dict(sorted(activated_skill_counts.items())),
        "error_types": dict(sorted(error_types.items())),
        **{
            f"{field}_mean": _round(
                _mean(_observation_value(row, field) for row in observations)
            )
            for field in RESOURCE_FIELDS
        },
    }


def _isolated_summary(observations: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "score_mean": _round(_mean(row["score"] for row in observations)),
        **{
            f"{field}_mean": _round(
                _mean(_observation_value(row, field) for row in observations)
            )
            for field in RESOURCE_FIELDS
        },
    }


def _effect(
    cases: list[dict[str, Any]],
    metric_config: dict[str, Any],
    label: str,
    left,
    right,
) -> dict[str, Any]:
    return _paired_effect(
        [float(left(case)) - float(right(case)) for case in cases],
        metric_config,
        label,
    )


def evaluate_suite(suite: dict[str, Any]) -> dict[str, Any]:
    """Compute paired isolated, factorial, activation, and resource effects."""
    validate_suite(suite)
    cases = suite["cases"]
    metric_config = suite["metric_config"]
    isolated_rows = {
        body: [case["isolated"][body] for case in cases] for body in BODY_VERSIONS
    }
    isolated_effect = _effect(
        cases,
        metric_config,
        "isolated_body_effect",
        lambda case: case["isolated"]["new_body"]["score"],
        lambda case: case["isolated"]["old_body"]["score"],
    )
    isolated_positive_cases = [case for case in cases if case["should_activate"]]
    isolated_negative_cases = [case for case in cases if not case["should_activate"]]
    isolated_positive_effect = _effect(
        isolated_positive_cases,
        metric_config,
        "isolated_positive_body_effect",
        lambda case: case["isolated"]["new_body"]["score"],
        lambda case: case["isolated"]["old_body"]["score"],
    )
    isolated_negative_effect = _effect(
        isolated_negative_cases,
        metric_config,
        "isolated_negative_body_effect",
        lambda case: case["isolated"]["new_body"]["score"],
        lambda case: case["isolated"]["old_body"]["score"],
    )
    levels_by_case = {
        case["case_id"]: {
            level["distractor_count"]: level for level in case["libraries"]
        }
        for case in cases
    }

    library_curve: list[dict[str, Any]] = []
    for ladder_level in suite["library_ladder"]:
        distractor_count = ladder_level["distractor_count"]

        def deployed(
            item: dict[str, Any], cell: str, count: int = distractor_count
        ) -> dict[str, Any]:
            level = levels_by_case[item["case_id"]][count]
            return level["deployed"][cell]

        cell_rows = {
            cell: [(deployed(case, cell), case["should_activate"]) for case in cases]
            for cell in CELL_KEYS
        }
        old_old = "old_body__old_description"
        old_new = "old_body__new_description"
        new_old = "new_body__old_description"
        new_new = "new_body__new_description"

        def score(cell: str):
            return lambda item, key=cell: deployed(item, key)["score"]

        factorial_effects = {
            "body_at_old_description": _effect(
                cases,
                metric_config,
                f"{distractor_count}:body_at_old_description",
                score(new_old),
                score(old_old),
            ),
            "body_at_new_description": _effect(
                cases,
                metric_config,
                f"{distractor_count}:body_at_new_description",
                score(new_new),
                score(old_new),
            ),
            "description_at_old_body": _effect(
                cases,
                metric_config,
                f"{distractor_count}:description_at_old_body",
                score(old_new),
                score(old_old),
            ),
            "description_at_new_body": _effect(
                cases,
                metric_config,
                f"{distractor_count}:description_at_new_body",
                score(new_new),
                score(new_old),
            ),
            "joint_deployed_effect": _effect(
                cases,
                metric_config,
                f"{distractor_count}:joint_deployed_effect",
                score(new_new),
                score(old_old),
            ),
        }
        interaction_deltas = [
            deployed(case, new_new)["score"]
            - deployed(case, new_old)["score"]
            - deployed(case, old_new)["score"]
            + deployed(case, old_old)["score"]
            for case in cases
        ]
        factorial_effects["interaction"] = _paired_effect(
            interaction_deltas,
            metric_config,
            f"{distractor_count}:interaction",
        )
        interference_deltas = [
            case["isolated"]["new_body"]["score"]
            - case["isolated"]["old_body"]["score"]
            - deployed(case, new_new)["score"]
            + deployed(case, old_old)["score"]
            for case in cases
        ]
        activation_cases = {
            "all": cases,
            "positive": [case for case in cases if case["should_activate"]],
            "negative": [case for case in cases if not case["should_activate"]],
        }
        activation_effects = {
            "target_activation_delta": _effect(
                activation_cases["all"],
                metric_config,
                f"{distractor_count}:target_activation_delta",
                lambda item, key=new_new: deployed(item, key)["target_activated"],
                lambda item, key=old_old: deployed(item, key)["target_activated"],
            ),
            "positive_recall_delta": _effect(
                activation_cases["positive"],
                metric_config,
                f"{distractor_count}:positive_recall_delta",
                lambda item, key=new_new: deployed(item, key)["target_activated"],
                lambda item, key=old_old: deployed(item, key)["target_activated"],
            ),
            "negative_false_positive_rate_delta": _effect(
                activation_cases["negative"],
                metric_config,
                f"{distractor_count}:negative_false_positive_rate_delta",
                lambda item, key=new_new: deployed(item, key)["target_activated"],
                lambda item, key=old_old: deployed(item, key)["target_activated"],
            ),
        }
        resource_effects = {
            field: _effect(
                cases,
                metric_config,
                f"{distractor_count}:{field}",
                lambda item, key=field, cell=new_new: _observation_value(
                    deployed(item, cell), key
                ),
                lambda item, key=field, cell=old_old: _observation_value(
                    deployed(item, cell), key
                ),
            )
            for field in RESOURCE_FIELDS
        }
        library_curve.append(
            {
                "distractor_count": distractor_count,
                "distractor_catalog_fingerprint": ladder_level[
                    "distractor_catalog_fingerprint"
                ],
                "distractor_skills": ladder_level["distractor_skills"],
                "cells": {
                    cell: _observation_summary(cell_rows[cell]) for cell in CELL_KEYS
                },
                "factorial_effects": factorial_effects,
                "interference": _paired_effect(
                    interference_deltas,
                    metric_config,
                    f"{distractor_count}:interference",
                ),
                "activation_effects": activation_effects,
                "resource_effects": resource_effects,
            }
        )

    return {
        "schema_version": SCHEMA_VERSION,
        "suite_id": suite["suite_id"],
        "target_skill": suite["target_skill"],
        "run_manifest": suite["run_manifest"],
        "cases": len(cases),
        "positive_cases": sum(case["should_activate"] for case in cases),
        "negative_cases": sum(not case["should_activate"] for case in cases),
        "isolated": {
            "old_body": _isolated_summary(isolated_rows["old_body"]),
            "new_body": _isolated_summary(isolated_rows["new_body"]),
            "body_effect": isolated_effect,
            "positive_body_effect": isolated_positive_effect,
            "negative_body_effect": isolated_negative_effect,
        },
        "library_curve": library_curve,
    }


def render_text(report: dict[str, Any]) -> str:
    """Render a compact human-readable summary."""
    isolated = report["isolated"]["body_effect"]
    lines = [
        f"suite: {report['suite_id']}",
        f"target_skill: {report['target_skill']}",
        (
            f"cases: {report['cases']} "
            f"(positive={report['positive_cases']}, negative={report['negative_cases']})"
        ),
        (
            "isolated_body_effect: "
            f"{isolated['mean']} CI={isolated['confidence_interval']} n={isolated['n']}"
        ),
    ]
    for level in report["library_curve"]:
        joint = level["factorial_effects"]["joint_deployed_effect"]
        interaction = level["factorial_effects"]["interaction"]
        interference = level["interference"]
        activation = level["activation_effects"]
        resources = level["resource_effects"]
        lines.extend(
            [
                f"library[distractors={level['distractor_count']}]:",
                (
                    "  joint_deployed_effect: "
                    f"{joint['mean']} CI={joint['confidence_interval']}"
                ),
                (
                    "  factorial_interaction: "
                    f"{interaction['mean']} CI={interaction['confidence_interval']}"
                ),
                (
                    "  interference: "
                    f"{interference['mean']} CI={interference['confidence_interval']}"
                ),
                (
                    "  activation_delta: "
                    f"all={activation['target_activation_delta']['mean']} "
                    f"positive={activation['positive_recall_delta']['mean']} "
                    "negative_fpr="
                    f"{activation['negative_false_positive_rate_delta']['mean']}"
                ),
                (
                    "  resource_delta: "
                    f"catalog_tokens={resources['catalog_tokens']['mean']} "
                    f"loaded_skill_tokens={resources['loaded_skill_tokens']['mean']} "
                    f"cost_usd={resources['cost_usd']['mean']}"
                ),
            ]
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Evaluate a recorded library-aware Skill 2x2 replay."
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
