"""Evaluate recorded Atom splitting and routing outputs without model calls.

The replay input is deliberately independent from xskill runtime objects.  A model
or harness may produce a recorded prediction out of band; regular CI only validates
the fixture and scores that immutable prediction against human-authored gold data.
"""

from __future__ import annotations

import argparse
import bisect
import hashlib
import json
import math
import re
from collections import Counter
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from scripts.bench.evaluate import pk, prf, score_case, window_diff

LATEST_SCHEMA_VERSION = 3
SUPPORTED_SCHEMA_VERSIONS = {1, 2, LATEST_SCHEMA_VERSION}
SUPPORTED_LANGUAGES = {"en", "zh"}
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_CODE_SPAN_RE = re.compile(r"`[^`]*`")
_TECHNICAL_TOKEN_RE = re.compile(r"\b\S*[/_.:-]\S*\b")


class ReplayValidationError(ValueError):
    """Raised when a replay fixture violates the versioned input contract."""


def load_suite(path: Path | str) -> dict[str, Any]:
    """Load and validate a replay suite from JSON."""
    suite_path = Path(path)
    try:
        payload = json.loads(suite_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ReplayValidationError(f"invalid JSON in {suite_path}: {error}") from error
    validate_suite(payload)
    return payload


def _require(mapping: dict[str, Any], key: str, expected_type: type, context: str) -> Any:
    if key not in mapping:
        raise ReplayValidationError(f"{context}: missing required field {key!r}")
    value = mapping[key]
    if expected_type is int and isinstance(value, bool):
        raise ReplayValidationError(f"{context}.{key}: expected int, got bool")
    if not isinstance(value, expected_type):
        raise ReplayValidationError(
            f"{context}.{key}: expected {expected_type.__name__}, "
            f"got {type(value).__name__}"
        )
    return value


def _validate_range(value: Any, *, line_count: int, context: str) -> tuple[int, int]:
    if not isinstance(value, list) or len(value) != 2:
        raise ReplayValidationError(f"{context}: expected [start, end]")
    start, end = value
    if any(isinstance(item, bool) or not isinstance(item, int) for item in value):
        raise ReplayValidationError(f"{context}: boundaries must be integers")
    if not 1 <= start < end <= line_count + 1:
        raise ReplayValidationError(
            f"{context}: expected 1 <= start < end <= {line_count + 1}, "
            f"got [{start}, {end}]"
        )
    return start, end


def _validate_atoms(
    atoms: Any,
    *,
    line_count: int,
    skill_catalog: set[str],
    context: str,
    require_non_overlapping: bool,
    require_weight_scores: bool = False,
) -> None:
    if not isinstance(atoms, list):
        raise ReplayValidationError(f"{context}: expected a list")
    seen_ids: set[str] = set()
    ranges: list[tuple[int, int]] = []
    for index, atom in enumerate(atoms):
        atom_context = f"{context}[{index}]"
        if not isinstance(atom, dict):
            raise ReplayValidationError(f"{atom_context}: expected an object")
        atom_id = _require(atom, "id", str, atom_context)
        if not atom_id or atom_id in seen_ids:
            raise ReplayValidationError(f"{atom_context}.id: empty or duplicate id")
        seen_ids.add(atom_id)
        start = _require(atom, "start_line", int, atom_context)
        end = _require(atom, "end_line", int, atom_context)
        ranges.append(
            _validate_range(
                [start, end], line_count=line_count, context=f"{atom_context}.range"
            )
        )
        _require(atom, "intent", str, atom_context)
        _require(atom, "summary", str, atom_context)
        skills = _require(atom, "skills", list, atom_context)
        candidates = _require(atom, "candidates", list, atom_context)
        for field, labels in (("skills", skills), ("candidates", candidates)):
            if any(not isinstance(label, str) or not label for label in labels):
                raise ReplayValidationError(
                    f"{atom_context}.{field}: labels must be non-empty strings"
                )
            unknown = set(labels) - skill_catalog
            if unknown:
                raise ReplayValidationError(
                    f"{atom_context}.{field}: unknown skill labels {sorted(unknown)}"
                )
            if len(set(labels)) != len(labels):
                raise ReplayValidationError(
                    f"{atom_context}.{field}: duplicate labels are not allowed"
                )
        if require_weight_scores:
            weight_scores = _require(atom, "weight_scores", list, atom_context)
            if not weight_scores:
                raise ReplayValidationError(
                    f"{atom_context}.weight_scores must not be empty"
                )
            weighted_skills: list[str] = []
            for weight_index, item in enumerate(weight_scores):
                weight_context = f"{atom_context}.weight_scores[{weight_index}]"
                if not isinstance(item, dict):
                    raise ReplayValidationError(
                        f"{weight_context}: expected an object"
                    )
                skill = _require(item, "skill", str, weight_context)
                if not skill or skill not in skill_catalog:
                    raise ReplayValidationError(
                        f"{weight_context}.skill: unknown skill label {skill!r}"
                    )
                weightscore = _require(item, "weightscore", int, weight_context)
                if not 1 <= weightscore <= 10:
                    raise ReplayValidationError(
                        f"{weight_context}.weightscore must satisfy 1 <= value <= 10"
                    )
                weighted_skills.append(skill)
            if len(set(weighted_skills)) != len(weighted_skills):
                raise ReplayValidationError(
                    f"{atom_context}.weight_scores contains duplicate skills"
                )
            if set(weighted_skills) != set(skills):
                raise ReplayValidationError(
                    f"{atom_context}.weight_scores must match the final skills set; "
                    f"skills={sorted(skills)}, weighted={sorted(weighted_skills)}"
                )
            if not set(skills).issubset(candidates):
                raise ReplayValidationError(
                    f"{atom_context}.skills must be present in ordered candidates"
                )
    if require_non_overlapping:
        ordered = sorted(ranges)
        for previous, current in zip(ordered, ordered[1:]):
            if current[0] < previous[1]:
                raise ReplayValidationError(
                    f"{context}: gold atom ranges overlap: {previous} and {current}"
                )


def _validate_boundary_candidates(
    candidates: Any,
    *,
    predicted_atoms: list[dict[str, Any]],
    scorable_ranges: list[tuple[int, int]],
    context: str,
) -> None:
    if not isinstance(candidates, list):
        raise ReplayValidationError(f"{context}: expected a list")
    predicted_by_id = {atom["id"]: atom for atom in predicted_atoms}
    expected_predicted_ids = {
        atom["id"]
        for atom in predicted_atoms
        if any(start < atom["start_line"] < end for start, end in scorable_ranges)
    }
    seen_lines: set[int] = set()
    selected_predicted_ids: set[str] = set()
    for index, candidate in enumerate(candidates):
        candidate_context = f"{context}[{index}]"
        if not isinstance(candidate, dict):
            raise ReplayValidationError(f"{candidate_context}: expected an object")
        line = _require(candidate, "line", int, candidate_context)
        if not any(start < line < end for start, end in scorable_ranges):
            raise ReplayValidationError(
                f"{candidate_context}.line: expected an internal scorable line"
            )
        if line in seen_lines:
            raise ReplayValidationError(
                f"{candidate_context}.line: duplicate candidate line {line}"
            )
        seen_lines.add(line)
        score = candidate.get("boundary_score")
        if (
            isinstance(score, bool)
            or not isinstance(score, (int, float))
            or not 0 <= score <= 1
        ):
            raise ReplayValidationError(
                f"{candidate_context}.boundary_score must satisfy 0 <= value <= 1"
            )
        algorithm_version = _require(
            candidate, "algorithm_version", str, candidate_context
        )
        if not algorithm_version:
            raise ReplayValidationError(
                f"{candidate_context}.algorithm_version must not be empty"
            )
        selected = _require(candidate, "selected", bool, candidate_context)
        predicted_atom_id = candidate.get("predicted_atom_id")
        if not selected:
            if predicted_atom_id is not None:
                raise ReplayValidationError(
                    f"{candidate_context}.predicted_atom_id must be null when rejected"
                )
            continue
        if not isinstance(predicted_atom_id, str) or not predicted_atom_id:
            raise ReplayValidationError(
                f"{candidate_context}.predicted_atom_id must identify a selected Atom"
            )
        if predicted_atom_id not in predicted_by_id:
            raise ReplayValidationError(
                f"{candidate_context}.predicted_atom_id: unknown Atom {predicted_atom_id!r}"
            )
        if predicted_by_id[predicted_atom_id]["start_line"] != line:
            raise ReplayValidationError(
                f"{candidate_context}: selected line must equal the Atom start line"
            )
        if predicted_atom_id in selected_predicted_ids:
            raise ReplayValidationError(
                f"{candidate_context}.predicted_atom_id: duplicate selected Atom"
            )
        selected_predicted_ids.add(predicted_atom_id)
    if selected_predicted_ids != expected_predicted_ids:
        missing = sorted(expected_predicted_ids - selected_predicted_ids)
        unexpected = sorted(selected_predicted_ids - expected_predicted_ids)
        raise ReplayValidationError(
            f"{context}: selected candidate mappings do not match internal predicted "
            f"Atoms; missing={missing}, unexpected={unexpected}"
        )


def _validate_manifest(
    manifest: Any, *, context: str, require_stage_fields: bool = False
) -> None:
    if not isinstance(manifest, dict):
        raise ReplayValidationError(f"{context}: expected an object")
    for key in (
        "repository_revision",
        "model",
        "harness",
        "prompt_fingerprint",
        "generated_at",
    ):
        value = _require(manifest, key, str, context)
        if not value:
            raise ReplayValidationError(f"{context}.{key} must not be empty")
    if not _SHA256_RE.fullmatch(manifest["prompt_fingerprint"]):
        raise ReplayValidationError(
            f"{context}.prompt_fingerprint must be sha256:<64 lowercase hex>"
        )
    for key in ("seed", "input_tokens", "output_tokens"):
        value = _require(manifest, key, int, context)
        if value < 0:
            raise ReplayValidationError(f"{context}.{key} must be >= 0")
    cost = manifest.get("cost_usd")
    if (
        isinstance(cost, bool)
        or not isinstance(cost, (int, float))
        or not math.isfinite(float(cost))
        or cost < 0
    ):
        raise ReplayValidationError(f"{context}.cost_usd must be a number >= 0")
    generation_config = _require(manifest, "generation_config", dict, context)
    temperature = generation_config.get("temperature")
    if (
        isinstance(temperature, bool)
        or not isinstance(temperature, (int, float))
        or not math.isfinite(float(temperature))
        or temperature < 0
    ):
        raise ReplayValidationError(
            f"{context}.generation_config.temperature must be a number >= 0"
        )
    top_p = generation_config.get("top_p")
    if (
        isinstance(top_p, bool)
        or not isinstance(top_p, (int, float))
        or not math.isfinite(float(top_p))
        or not 0 < top_p <= 1
    ):
        raise ReplayValidationError(
            f"{context}.generation_config.top_p must satisfy 0 < top_p <= 1"
        )
    max_output_tokens = generation_config.get("max_output_tokens")
    if (
        isinstance(max_output_tokens, bool)
        or not isinstance(max_output_tokens, int)
        or max_output_tokens <= 0
    ):
        raise ReplayValidationError(
            f"{context}.generation_config.max_output_tokens must be an int > 0"
        )
    if not require_stage_fields:
        return
    algorithm_version = _require(manifest, "algorithm_version", str, context)
    if not algorithm_version:
        raise ReplayValidationError(
            f"{context}.algorithm_version must not be empty"
        )
    calls = _require(manifest, "calls", int, context)
    if calls <= 0:
        raise ReplayValidationError(f"{context}.calls must be > 0")
    cache_read_tokens = _require(manifest, "cache_read_tokens", int, context)
    if cache_read_tokens < 0:
        raise ReplayValidationError(f"{context}.cache_read_tokens must be >= 0")
    if cache_read_tokens > manifest["input_tokens"]:
        raise ReplayValidationError(
            f"{context}.cache_read_tokens must not exceed input_tokens"
        )
    price_source = _require(manifest, "price_source", str, context)
    if not price_source:
        raise ReplayValidationError(f"{context}.price_source must not be empty")
    generation_seconds = manifest.get("generation_seconds")
    if (
        isinstance(generation_seconds, bool)
        or not isinstance(generation_seconds, (int, float))
        or not math.isfinite(float(generation_seconds))
        or generation_seconds < 0
    ):
        raise ReplayValidationError(
            f"{context}.generation_seconds must be a number >= 0"
        )


def validate_suite(suite: Any) -> None:
    """Fail loudly on malformed or unsupported replay data."""
    if not isinstance(suite, dict):
        raise ReplayValidationError("suite: expected an object")
    version = _require(suite, "schema_version", int, "suite")
    if version not in SUPPORTED_SCHEMA_VERSIONS:
        raise ReplayValidationError(
            "suite.schema_version: "
            f"supported={sorted(SUPPORTED_SCHEMA_VERSIONS)}, got={version}"
        )
    _require(suite, "suite_id", str, "suite")
    config = _require(suite, "metric_config", dict, "suite")
    recall_k = _require(config, "routing_recall_k", int, "suite.metric_config")
    if recall_k <= 0:
        raise ReplayValidationError("suite.metric_config.routing_recall_k must be > 0")
    alignment_min_iou = config.get("atom_alignment_min_iou")
    if (
        isinstance(alignment_min_iou, bool)
        or not isinstance(alignment_min_iou, (int, float))
        or not 0 < alignment_min_iou <= 1
    ):
        raise ReplayValidationError(
            "suite.metric_config.atom_alignment_min_iou must satisfy 0 < value <= 1"
        )
    if version >= 2:
        thresholds = _require(
            config, "boundary_score_thresholds", list, "suite.metric_config"
        )
        if not thresholds:
            raise ReplayValidationError(
                "suite.metric_config.boundary_score_thresholds must not be empty"
            )
        if any(
            isinstance(threshold, bool)
            or not isinstance(threshold, (int, float))
            or not 0 <= threshold <= 1
            for threshold in thresholds
        ):
            raise ReplayValidationError(
                "suite.metric_config.boundary_score_thresholds must contain numbers "
                "satisfying 0 <= value <= 1"
            )
        if any(left >= right for left, right in zip(thresholds, thresholds[1:])):
            raise ReplayValidationError(
                "suite.metric_config.boundary_score_thresholds must be strictly increasing"
            )

    if version < 3:
        _validate_manifest(
            _require(suite, "run_manifest", dict, "suite"),
            context="suite.run_manifest",
        )
    else:
        if "run_manifest" in suite:
            raise ReplayValidationError(
                "suite.run_manifest is not allowed in schema v3; use stage_manifests"
            )
        stage_manifests = _require(suite, "stage_manifests", dict, "suite")
        if set(stage_manifests) != {"split", "route"}:
            raise ReplayValidationError(
                "suite.stage_manifests must contain exactly split and route"
            )
        for stage in ("split", "route"):
            _validate_manifest(
                stage_manifests[stage],
                context=f"suite.stage_manifests.{stage}",
                require_stage_fields=True,
            )
        revisions = {
            stage_manifests[stage]["repository_revision"]
            for stage in ("split", "route")
        }
        if len(revisions) != 1:
            raise ReplayValidationError(
                "suite.stage_manifests must use one repository_revision"
            )

    catalog_raw = _require(suite, "skill_catalog", list, "suite")
    if any(not isinstance(label, str) or not label for label in catalog_raw):
        raise ReplayValidationError("suite.skill_catalog must contain non-empty strings")
    skill_catalog = set(catalog_raw)
    if len(skill_catalog) != len(catalog_raw):
        raise ReplayValidationError("suite.skill_catalog contains duplicate labels")

    cases = _require(suite, "cases", list, "suite")
    if not cases:
        raise ReplayValidationError("suite.cases must not be empty")
    seen_case_ids: set[str] = set()
    boundary_algorithm_versions: set[str] = set()
    for index, case in enumerate(cases):
        context = f"suite.cases[{index}]"
        if not isinstance(case, dict):
            raise ReplayValidationError(f"{context}: expected an object")
        case_id = _require(case, "case_id", str, context)
        if not case_id or case_id in seen_case_ids:
            raise ReplayValidationError(f"{context}.case_id: empty or duplicate id")
        seen_case_ids.add(case_id)
        language = _require(case, "expected_language", str, context)
        if language not in SUPPORTED_LANGUAGES:
            raise ReplayValidationError(
                f"{context}.expected_language: supported={sorted(SUPPORTED_LANGUAGES)}"
            )
        line_count = _require(case, "line_count", int, context)
        if line_count <= 0:
            raise ReplayValidationError(f"{context}.line_count must be > 0")
        source_lines = _require(case, "source_lines", list, context)
        if any(not isinstance(line, str) for line in source_lines):
            raise ReplayValidationError(
                f"{context}.source_lines must contain strings"
            )
        if len(source_lines) != line_count:
            raise ReplayValidationError(
                f"{context}.source_lines must contain exactly {line_count} lines"
            )
        scorable = _require(case, "scorable_ranges", list, context)
        if not scorable:
            raise ReplayValidationError(f"{context}.scorable_ranges must not be empty")
        parsed_ranges = [
            _validate_range(
                value,
                line_count=line_count,
                context=f"{context}.scorable_ranges[{range_index}]",
            )
            for range_index, value in enumerate(scorable)
        ]
        if _range_units(parsed_ranges) != sum(end - start for start, end in parsed_ranges):
            raise ReplayValidationError(f"{context}.scorable_ranges must not overlap")
        _validate_atoms(
            _require(case, "gold_atoms", list, context),
            line_count=line_count,
            skill_catalog=skill_catalog,
            context=f"{context}.gold_atoms",
            require_non_overlapping=True,
        )
        predicted_atoms = _require(case, "predicted_atoms", list, context)
        _validate_atoms(
            predicted_atoms,
            line_count=line_count,
            skill_catalog=skill_catalog,
            context=f"{context}.predicted_atoms",
            require_non_overlapping=False,
            require_weight_scores=version >= 3,
        )
        if version >= 2:
            boundary_candidates = _require(
                case, "boundary_candidates", list, context
            )
            _validate_boundary_candidates(
                boundary_candidates,
                predicted_atoms=predicted_atoms,
                scorable_ranges=parsed_ranges,
                context=f"{context}.boundary_candidates",
            )
            boundary_algorithm_versions.update(
                candidate["algorithm_version"] for candidate in boundary_candidates
            )
    if version >= 2 and len(boundary_algorithm_versions) != 1:
        raise ReplayValidationError(
            "suite.boundary_candidates must contain exactly one algorithm_version"
        )
    if version >= 3:
        boundary_version = next(iter(boundary_algorithm_versions))
        split_version = suite["stage_manifests"]["split"]["algorithm_version"]
        if boundary_version != split_version:
            raise ReplayValidationError(
                "suite boundary algorithm_version must match the split manifest"
            )


def _range_set(ranges: Iterable[tuple[int, int]]) -> set[int]:
    units: set[int] = set()
    for start, end in ranges:
        units.update(range(start, end))
    return units


def _range_units(ranges: Iterable[tuple[int, int]]) -> int:
    return len(_range_set(ranges))


def _atom_range(atom: dict[str, Any]) -> tuple[int, int]:
    return int(atom["start_line"]), int(atom["end_line"])


def _overlap_size(left: tuple[int, int], right: tuple[int, int]) -> int:
    return max(0, min(left[1], right[1]) - max(left[0], right[0]))


def _interval_iou(left: tuple[int, int], right: tuple[int, int]) -> float:
    intersection = _overlap_size(left, right)
    union = (left[1] - left[0]) + (right[1] - right[0]) - intersection
    return intersection / union if union else 0.0


def _align_predictions(
    gold_atoms: list[dict[str, Any]],
    predicted_atoms: list[dict[str, Any]],
    alignment_min_iou: float,
) -> dict[str, str | None]:
    alignment: dict[str, str | None] = {}
    for predicted in predicted_atoms:
        ranked = sorted(
            (
                (_interval_iou(_atom_range(predicted), _atom_range(gold)), gold["id"])
                for gold in gold_atoms
            ),
            key=lambda item: (-item[0], item[1]),
        )
        alignment[predicted["id"]] = (
            ranked[0][1]
            if ranked and ranked[0][0] >= alignment_min_iou
            else None
        )
    return alignment


def _prf(gold: set[Any], predicted: set[Any]) -> dict[str, float | int]:
    true_positive = len(gold & predicted)
    false_positive = len(predicted - gold)
    false_negative = len(gold - predicted)
    precision, recall, f1 = prf(true_positive, false_positive, false_negative)
    return {
        "true_positive": true_positive,
        "false_positive": false_positive,
        "false_negative": false_negative,
        "precision": round(precision, 6),
        "recall": round(recall, 6),
        "f1": round(f1, 6),
    }


def _binary_auroc(examples: list[tuple[float, bool]]) -> float | None:
    positive_count = sum(label for _score, label in examples)
    negative_count = len(examples) - positive_count
    if not positive_count or not negative_count:
        return None
    ranked = sorted(examples, key=lambda item: item[0])
    positive_rank_sum = 0.0
    index = 0
    while index < len(ranked):
        group_end = index + 1
        while group_end < len(ranked) and ranked[group_end][0] == ranked[index][0]:
            group_end += 1
        average_rank = ((index + 1) + group_end) / 2
        positive_rank_sum += average_rank * sum(
            label for _score, label in ranked[index:group_end]
        )
        index = group_end
    area = positive_rank_sum - positive_count * (positive_count + 1) / 2
    return round(area / (positive_count * negative_count), 6)


def _routing_error_thresholds(
    examples: list[tuple[float, bool]], thresholds: list[float]
) -> list[dict[str, Any]]:
    ordered = sorted(examples, key=lambda item: item[0])
    scores = [score for score, _error in ordered]
    prefix_errors = [0]
    for _score, error in ordered:
        prefix_errors.append(prefix_errors[-1] + int(error))
    total = len(ordered)
    total_errors = prefix_errors[-1]
    results = []
    for threshold in thresholds:
        retained_start = bisect.bisect_left(scores, threshold)
        retained = total - retained_start
        errors = total_errors - prefix_errors[retained_start]
        results.append(
            {
                "minimum_score": threshold,
                "eligible": total,
                "retained": retained,
                "coverage": round(retained / total, 6) if total else None,
                "routing_errors": errors,
                "routing_error_rate": round(errors / retained, 6)
                if retained
                else None,
            }
        )
    return results


def detect_language(text: str) -> str:
    """Detect the dominant natural-language script for English/Chinese fixtures.

    Inline code and path-like tokens are removed first so identifiers do not turn a
    Chinese explanation into an English classification.  Chinese characters carry
    more lexical information than individual Latin letters, so a 20% CJK share is
    considered Chinese for this deliberately small two-language baseline.
    """
    natural = _CODE_SPAN_RE.sub(" ", text)
    natural = _TECHNICAL_TOKEN_RE.sub(" ", natural)
    cjk = sum("\u4e00" <= char <= "\u9fff" for char in natural)
    latin = sum(char.isascii() and char.isalpha() for char in natural)
    total = cjk + latin
    if total == 0:
        return "unknown"
    return "zh" if cjk / total >= 0.2 else "en"


def _case_counts(
    case: dict[str, Any],
    recall_k: int,
    alignment_min_iou: float,
    *,
    include_boundary_scores: bool,
) -> dict[str, Any]:
    gold_atoms = case["gold_atoms"]
    predicted_atoms = case["predicted_atoms"]
    alignment = _align_predictions(gold_atoms, predicted_atoms, alignment_min_iou)
    scorable_ranges = [tuple(value) for value in case["scorable_ranges"]]
    scorable_units = _range_set(scorable_ranges)
    predicted_ranges = [_atom_range(atom) for atom in predicted_atoms]
    predicted_units = _range_set(predicted_ranges) & scorable_units
    predicted_total = sum(
        len(set(range(start, end)) & scorable_units)
        for start, end in predicted_ranges
    )

    excluded_starts = {start for start, _end in scorable_ranges}
    gold_boundaries = {
        atom["start_line"] for atom in gold_atoms if atom["start_line"] not in excluded_starts
    }
    predicted_boundaries = {
        atom["start_line"]
        for atom in predicted_atoms
        if atom["start_line"] not in excluded_starts
    }
    boundary = score_case(
        sorted(predicted_boundaries), sorted(gold_boundaries), tol=0
    )

    assignments = Counter(
        gold_id for gold_id in alignment.values() if gold_id is not None
    )
    duplicate_extras = sum(max(0, count - 1) for count in assignments.values())

    language_total = len(predicted_atoms)
    language_matches = 0
    for atom in predicted_atoms:
        detected = detect_language(f"{atom['intent']}\n{atom['summary']}")
        language_matches += detected == case["expected_language"]

    gold_relations = {
        (atom["id"], skill) for atom in gold_atoms for skill in atom["skills"]
    }
    predicted_relations: set[tuple[str, str]] = set()
    candidate_relations: set[tuple[str, str]] = set()
    for atom in predicted_atoms:
        target = alignment[atom["id"]] or f"__unmatched__:{atom['id']}"
        predicted_relations.update((target, skill) for skill in atom["skills"])
        candidate_relations.update(
            (target, skill) for skill in atom["candidates"][:recall_k]
        )
    multi_skill_gold = {
        (atom["id"], skill)
        for atom in gold_atoms
        if len(atom["skills"]) > 1
        for skill in atom["skills"]
    }

    counts = {
        "boundary_true_positive": boundary["tp"],
        "boundary_false_positive": boundary["fp"],
        "boundary_false_negative": boundary["fn"],
        "boundary_exact": int(boundary["exact"]),
        "segmentation_pk_sum": pk(
            case["line_count"],
            sorted(gold_boundaries),
            sorted(predicted_boundaries),
        ),
        "segmentation_window_diff_sum": window_diff(
            case["line_count"],
            sorted(gold_boundaries),
            sorted(predicted_boundaries),
        ),
        "case_count": 1,
        "scorable_units": len(scorable_units),
        "covered_units": len(predicted_units),
        "overlap_units": max(0, predicted_total - len(predicted_units)),
        "predicted_atoms": len(predicted_atoms),
        "duplicate_extras": duplicate_extras,
        "language_total": language_total,
        "language_matches": language_matches,
        "gold_relations": gold_relations,
        "predicted_relations": predicted_relations,
        "candidate_relations": candidate_relations,
        "multi_skill_gold": multi_skill_gold,
    }
    if include_boundary_scores:
        gold_by_id = {atom["id"]: atom for atom in gold_atoms}
        predicted_by_id = {atom["id"]: atom for atom in predicted_atoms}
        boundary_score_examples = []
        routing_error_examples = []
        for candidate in case["boundary_candidates"]:
            score = float(candidate["boundary_score"])
            boundary_score_examples.append(
                (score, candidate["line"] in gold_boundaries)
            )
            if not candidate["selected"]:
                continue
            predicted = predicted_by_id[candidate["predicted_atom_id"]]
            gold_id = alignment[predicted["id"]]
            routing_error = gold_id is None or set(predicted["skills"]) != set(
                gold_by_id[gold_id]["skills"]
            )
            routing_error_examples.append((score, routing_error))
        counts["boundary_score_examples"] = boundary_score_examples
        counts["routing_error_examples"] = routing_error_examples
    return counts


def _safe_ratio(numerator: int, denominator: int, *, empty: float = 1.0) -> float:
    return round(numerator / denominator, 6) if denominator else empty


def _macro_prf(case_counts: list[dict[str, Any]]) -> dict[str, float]:
    scores = [
        _prf(counts["gold_relations"], counts["predicted_relations"])
        for counts in case_counts
    ]
    return {
        metric: round(sum(float(score[metric]) for score in scores) / len(scores), 6)
        for metric in ("precision", "recall", "f1")
    }


def _public_metrics(
    counts: dict[str, Any], boundary_score_thresholds: list[float] | None = None
) -> dict[str, Any]:
    precision, recall, f1 = prf(
        counts["boundary_true_positive"],
        counts["boundary_false_positive"],
        counts["boundary_false_negative"],
    )
    boundary = {
        "true_positive": counts["boundary_true_positive"],
        "false_positive": counts["boundary_false_positive"],
        "false_negative": counts["boundary_false_negative"],
        "precision": round(precision, 6),
        "recall": round(recall, 6),
        "f1": round(f1, 6),
        "exact_match": _safe_ratio(
            counts["boundary_exact"], counts["case_count"]
        ),
    }
    routing = _prf(counts["gold_relations"], counts["predicted_relations"])
    metrics = {
        "boundary": boundary,
        "segmentation": {
            "pk": round(
                counts["segmentation_pk_sum"] / counts["case_count"], 6
            ),
            "window_diff": round(
                counts["segmentation_window_diff_sum"] / counts["case_count"], 6
            ),
        },
        "coverage": _safe_ratio(counts["covered_units"], counts["scorable_units"]),
        "overlap_rate": _safe_ratio(
            counts["overlap_units"], counts["scorable_units"], empty=0.0
        ),
        "duplicate_rate": _safe_ratio(
            counts["duplicate_extras"], counts["predicted_atoms"], empty=0.0
        ),
        "language_consistency": _safe_ratio(
            counts["language_matches"], counts["language_total"]
        ),
        "routing_micro": routing,
        "routing_recall_at_k": _safe_ratio(
            len(counts["gold_relations"] & counts["candidate_relations"]),
            len(counts["gold_relations"]),
        ),
        "multi_skill_relation_retention": _safe_ratio(
            len(counts["multi_skill_gold"] & counts["predicted_relations"]),
            len(counts["multi_skill_gold"]),
        ),
    }
    if boundary_score_thresholds is not None:
        boundary_examples = counts["boundary_score_examples"]
        routing_examples = counts["routing_error_examples"]
        positive_count = sum(label for _score, label in boundary_examples)
        routing_error_count = sum(error for _score, error in routing_examples)
        metrics["boundary_score"] = {
            "candidates": len(boundary_examples),
            "positive": positive_count,
            "negative": len(boundary_examples) - positive_count,
            "auroc": _binary_auroc(boundary_examples),
        }
        metrics["routing_error_association"] = {
            "selected_candidates": len(routing_examples),
            "routing_errors": routing_error_count,
            "low_score_error_auroc": _binary_auroc(
                [(1 - score, error) for score, error in routing_examples]
            ),
            "thresholds": _routing_error_thresholds(
                routing_examples, boundary_score_thresholds
            ),
        }
    return metrics


def _merge_counts(
    all_counts: list[dict[str, Any]], *, include_boundary_scores: bool
) -> dict[str, Any]:
    merged: dict[str, Any] = {
        "boundary_true_positive": 0,
        "boundary_false_positive": 0,
        "boundary_false_negative": 0,
        "boundary_exact": 0,
        "segmentation_pk_sum": 0.0,
        "segmentation_window_diff_sum": 0.0,
        "case_count": 0,
        "scorable_units": 0,
        "covered_units": 0,
        "overlap_units": 0,
        "predicted_atoms": 0,
        "duplicate_extras": 0,
        "language_total": 0,
        "language_matches": 0,
        "gold_relations": set(),
        "predicted_relations": set(),
        "candidate_relations": set(),
        "multi_skill_gold": set(),
    }
    set_keys = {
        "gold_relations",
        "predicted_relations",
        "candidate_relations",
        "multi_skill_gold",
    }
    if include_boundary_scores:
        merged["boundary_score_examples"] = []
        merged["routing_error_examples"] = []
    list_keys = {"boundary_score_examples", "routing_error_examples"}
    for index, counts in enumerate(all_counts):
        prefix = f"case-{index}:"
        for key, value in counts.items():
            if key in set_keys:
                merged[key].update((prefix, item) for item in value)
            elif key in list_keys:
                merged[key].extend(value)
            else:
                merged[key] += value
    return merged


def evaluate_suite(suite: dict[str, Any]) -> dict[str, Any]:
    """Return a stable JSON-serializable report for a validated replay suite."""
    validate_suite(suite)
    recall_k = suite["metric_config"]["routing_recall_k"]
    alignment_min_iou = suite["metric_config"]["atom_alignment_min_iou"]
    include_boundary_scores = suite["schema_version"] >= 2
    boundary_score_thresholds = (
        suite["metric_config"]["boundary_score_thresholds"]
        if include_boundary_scores
        else None
    )
    case_counts = [
        _case_counts(
            case,
            recall_k,
            alignment_min_iou,
            include_boundary_scores=include_boundary_scores,
        )
        for case in suite["cases"]
    ]
    cases = [
        {
            "case_id": case["case_id"],
            "expected_language": case["expected_language"],
            "metrics": _public_metrics(counts, boundary_score_thresholds),
        }
        for case, counts in zip(suite["cases"], case_counts)
    ]
    report = {
        "schema_version": suite["schema_version"],
        "suite_id": suite["suite_id"],
    }
    if suite["schema_version"] >= 3:
        report["stage_manifests"] = suite["stage_manifests"]
        report["route_algorithm_version"] = suite["stage_manifests"]["route"][
            "algorithm_version"
        ]
    else:
        report["run_manifest"] = suite["run_manifest"]
    report.update(
        {
            "metric_config": suite["metric_config"],
            "metrics": {
                **_public_metrics(
                    _merge_counts(
                        case_counts, include_boundary_scores=include_boundary_scores
                    ),
                    boundary_score_thresholds,
                ),
                "routing_macro": _macro_prf(case_counts),
            },
            "cases": cases,
        }
    )
    if include_boundary_scores:
        report["boundary_algorithm_version"] = next(
            iter(
                {
                    candidate["algorithm_version"]
                    for case in suite["cases"]
                    for candidate in case["boundary_candidates"]
                }
            )
        )
    canonical = json.dumps(report, ensure_ascii=False, sort_keys=True).encode("utf-8")
    report["report_sha256"] = hashlib.sha256(canonical).hexdigest()
    return report


def render_text(report: dict[str, Any]) -> str:
    """Render a compact reviewer-facing summary."""
    metrics = report["metrics"]
    boundary = metrics["boundary"]
    routing = metrics["routing_micro"]
    revision = (
        report["stage_manifests"]["split"]["repository_revision"]
        if "stage_manifests" in report
        else report["run_manifest"]["repository_revision"]
    )
    lines = [
        f"suite: {report['suite_id']}",
        f"revision: {revision}",
        (
            "boundary: "
            f"P={boundary['precision']:.3f} "
            f"R={boundary['recall']:.3f} F1={boundary['f1']:.3f}"
        ),
        (
            f"coverage={metrics['coverage']:.3f} "
            f"overlap={metrics['overlap_rate']:.3f} "
            f"duplicates={metrics['duplicate_rate']:.3f}"
        ),
        f"language_consistency={metrics['language_consistency']:.3f}",
        (
            "routing: "
            f"P={routing['precision']:.3f} "
            f"R={routing['recall']:.3f} F1={routing['f1']:.3f}"
        ),
        (
            f"routing_recall@{report['metric_config']['routing_recall_k']}="
            f"{metrics['routing_recall_at_k']:.3f} "
            f"multi_skill_retention="
            f"{metrics['multi_skill_relation_retention']:.3f}"
        ),
    ]
    if "boundary_score" in metrics:
        score = metrics["boundary_score"]
        association = metrics["routing_error_association"]
        lines.extend(
            (
                (
                    f"boundary_score: candidates={score['candidates']} "
                    f"AUROC={score['auroc']}"
                ),
                (
                    "routing_error_association: "
                    f"selected={association['selected_candidates']} "
                    f"errors={association['routing_errors']} "
                    f"low_score_AUROC={association['low_score_error_auroc']}"
                ),
            )
        )
    lines.append(f"report_sha256={report['report_sha256']}")
    return "\n".join(lines)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Evaluate a recorded xskill Atom splitting/routing replay suite."
    )
    parser.add_argument("suite", type=Path, help="Path to the versioned replay JSON")
    parser.add_argument(
        "--format", choices=("text", "json"), default="text", help="Output format"
    )
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
