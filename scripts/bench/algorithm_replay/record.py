"""Record real model outputs for the deterministic Atom replay evaluator.

The recorder is deliberately opt-in: it reads only the source JSON and config path
provided on the command line.  It never discovers local trajectories or writes to
xskill's runtime stores.  Normal CI evaluates the committed output without calling
the model again.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import subprocess
import time
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

import yaml

from scripts.bench.algorithm_replay.evaluate import (
    ReplayValidationError,
    SOURCE_SCHEMA_VERSION,
    evaluate_suite,
    render_text,
    validate_suite,
)
from xskill.agents.agno_factory import resolve_agent_llm_config
from xskill.usage import cost_usd, extract_usage, load_price_table
from xskill.utils.llm import LLMClient

DEFAULT_SPLIT_ALGORITHM_VERSION = "offline-boundary-ranker-v1"
DEFAULT_ROUTE_ALGORITHM_VERSION = "offline-skill-router-v1"

SPLIT_SYSTEM_PROMPT = """You are an offline evaluator for xskill Atom boundaries.
Return one JSON object and no prose.
Score every supplied candidate line from 0 to 1 as an uncalibrated boundary ranking signal.
Select boundaries that begin a new reusable user goal, not continuations, corrections, or retries of the same goal.
Line numbers are the exact 1-based positions in source_lines.
For every scorable range, emit non-overlapping predicted atoms that cover the complete range in order.
Emit one Atom start at every scorable range start and every selected candidate line, with no other Atom starts.
Use the expected source language for intent and summary.
The recorder derives half-open end_line values and assigns stable Atom ids and candidate mappings; do not emit those fields.
Do not emit skills, hidden reasoning, markdown fences, or fields not requested."""

ROUTE_SYSTEM_PROMPT = """You are an offline evaluator for xskill Atom-to-Skills routing.
Return one JSON object and no prose.
For every supplied predicted atom, rank unique candidate skills from the supplied catalog and select one or more final skills.
Every final skill must occur in the ordered candidates and have one strict integer weightscore from 1 to 10.
The weight_scores skill set must equal the final skills set exactly; do not score unselected candidates.
Preserve valid one-Atom-to-many-Skills relationships.
Do not change atom ids, ranges, intent, or summary.
Do not emit hidden reasoning, markdown fences, or fields not requested."""

SPLIT_OUTPUT_INSTRUCTION = "Return {cases:[{case_id,boundary_candidates:[{line,boundary_score,selected}],predicted_atoms:[{start_line,intent,summary}]}]}. Include every candidate line exactly once."
ROUTE_OUTPUT_INSTRUCTION = "Return {cases:[{case_id,atoms:[{id,skills,candidates,weight_scores:[{skill,weightscore}]}]}]}. Include every atom exactly once."


@dataclass(frozen=True)
class ModelCallResult:
    """One measured OpenAI-compatible model response."""

    content: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cost_usd: float
    price_source: str
    generation_seconds: float
    calls: int = 1


ModelCaller = Callable[
    [str, dict[str, Any], str, str, int, float, Any], ModelCallResult
]


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ReplayValidationError(f"invalid JSON in {path}: {error}") from error
    if not isinstance(payload, dict):
        raise ReplayValidationError(f"{path}: expected a JSON object")
    return payload


def _dummy_stage_manifest(algorithm_version: str) -> dict[str, Any]:
    return {
        "algorithm_version": algorithm_version,
        "repository_revision": "source-validation",
        "model": "source-validation",
        "harness": "offline-source-validator",
        "prompt_fingerprint": "sha256:" + "0" * 64,
        "generated_at": "1970-01-01T00:00:00Z",
        "seed": 0,
        "calls": 1,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cost_usd": 0.0,
        "price_source": "not-applicable",
        "generation_seconds": 0.0,
        "generation_config": {
            "temperature": 0.0,
            "top_p": 1.0,
            "max_output_tokens": 1,
        },
    }


def validate_source_suite(source: Any) -> None:
    """Validate gold data and explicit candidate lines without model calls."""
    if not isinstance(source, dict):
        raise ReplayValidationError("source: expected an object")
    source_version = source.get("source_schema_version")
    if (
        isinstance(source_version, bool)
        or not isinstance(source_version, int)
        or source_version != SOURCE_SCHEMA_VERSION
    ):
        raise ReplayValidationError(
            "source.source_schema_version: supported=1, "
            f"got={source.get('source_schema_version')!r}"
        )
    catalog = source.get("skill_catalog")
    if not isinstance(catalog, list) or not catalog:
        raise ReplayValidationError("source.skill_catalog must be a non-empty list")
    cases = source.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ReplayValidationError("source.cases must be a non-empty list")

    placeholder_cases: list[dict[str, Any]] = []
    for case_index, case in enumerate(cases):
        context = f"source.cases[{case_index}]"
        if not isinstance(case, dict):
            raise ReplayValidationError(f"{context}: expected an object")
        candidate_lines = case.get("candidate_lines")
        if not isinstance(candidate_lines, list):
            raise ReplayValidationError(f"{context}.candidate_lines: expected a list")
        if any(
            isinstance(line, bool) or not isinstance(line, int)
            for line in candidate_lines
        ):
            raise ReplayValidationError(
                f"{context}.candidate_lines must contain integers"
            )
        if len(set(candidate_lines)) != len(candidate_lines):
            raise ReplayValidationError(
                f"{context}.candidate_lines contains duplicate lines"
            )

        gold_atoms = deepcopy(case.get("gold_atoms"))
        if not isinstance(gold_atoms, list):
            raise ReplayValidationError(f"{context}.gold_atoms: expected a list")
        scorable_starts = {
            value[0]
            for value in case.get("scorable_ranges", [])
            if isinstance(value, list) and len(value) == 2
        }
        gold_by_start = {
            atom.get("start_line"): atom
            for atom in gold_atoms
            if isinstance(atom, dict) and atom.get("start_line") not in scorable_starts
        }
        missing_candidate_lines = set(gold_by_start) - set(candidate_lines)
        if missing_candidate_lines:
            raise ReplayValidationError(
                f"{context}.candidate_lines misses gold boundaries "
                f"{sorted(missing_candidate_lines)}"
            )

        predicted_atoms = deepcopy(gold_atoms)
        for atom in predicted_atoms:
            atom["weight_scores"] = [
                {"skill": skill, "weightscore": 10} for skill in atom.get("skills", [])
            ]
        boundary_candidates = []
        for line in candidate_lines:
            selected_atom = gold_by_start.get(line)
            boundary_candidates.append(
                {
                    "line": line,
                    "boundary_score": 1.0 if selected_atom else 0.0,
                    "algorithm_version": "source-validation",
                    "selected": selected_atom is not None,
                    "predicted_atom_id": selected_atom.get("id")
                    if selected_atom
                    else None,
                }
            )
        placeholder = deepcopy(case)
        placeholder.pop("candidate_lines", None)
        placeholder["predicted_atoms"] = predicted_atoms
        placeholder["boundary_candidates"] = boundary_candidates
        placeholder_cases.append(placeholder)

    validate_suite(
        {
            "schema_version": 3,
            "source_schema_version": source["source_schema_version"],
            "suite_id": source.get("suite_id"),
            "metric_config": source.get("metric_config"),
            "stage_manifests": {
                "split": _dummy_stage_manifest("source-validation"),
                "route": _dummy_stage_manifest("source-route-validation"),
            },
            "skill_catalog": source.get("skill_catalog"),
            "cases": placeholder_cases,
        }
    )


def _prompt_fingerprint(system_prompt: str, output_instruction: str) -> str:
    prompt_contract = f"{system_prompt}\n{output_instruction}"
    digest = hashlib.sha256(prompt_contract.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def _json_prompt(instruction: str, payload: dict[str, Any]) -> str:
    return (
        instruction
        + "\nINPUT_JSON:\n"
        + json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    )


def _split_prompt(source: dict[str, Any]) -> str:
    cases = [
        {
            "case_id": case["case_id"],
            "expected_language": case["expected_language"],
            "source_lines": case["source_lines"],
            "scorable_ranges": case["scorable_ranges"],
            "candidate_lines": case["candidate_lines"],
        }
        for case in source["cases"]
    ]
    return _json_prompt(
        SPLIT_OUTPUT_INSTRUCTION,
        {"cases": cases},
    )


def _route_prompt(source: dict[str, Any], split_cases: list[dict[str, Any]]) -> str:
    return _json_prompt(
        ROUTE_OUTPUT_INSTRUCTION,
        {
            "skill_catalog": source["skill_catalog"],
            "cases": [
                {
                    "case_id": case["case_id"],
                    "source_lines": source_case["source_lines"],
                    "predicted_atoms": case["predicted_atoms"],
                }
                for case, source_case in zip(split_cases, source["cases"])
            ],
        },
    )


def _parse_json_response(content: str, *, stage: str) -> dict[str, Any]:
    if not isinstance(content, str) or not content.strip():
        raise ReplayValidationError(f"{stage} model returned empty content")
    text = content.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if len(lines) >= 3 and lines[-1].strip() == "```":
            text = "\n".join(lines[1:-1])
            if text.lstrip().startswith("json"):
                text = text.lstrip()[4:].lstrip("\r\n")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as error:
        raise ReplayValidationError(
            f"{stage} model returned invalid JSON: {error}"
        ) from error
    if not isinstance(payload, dict):
        raise ReplayValidationError(f"{stage} model response must be an object")
    return payload


def _default_model_caller(
    stage: str,
    llm_config: dict[str, Any],
    system_prompt: str,
    prompt: str,
    seed: int,
    top_p: float,
    price_table: Any,
) -> ModelCallResult:
    model = llm_config.get("model")
    if not isinstance(model, str) or not model:
        raise ValueError(f"llm_agents.{stage}.model is required")
    if not llm_config.get("base_url"):
        raise ValueError(f"llm_agents.{stage}.base_url is required")
    if not llm_config.get("api_key"):
        raise ValueError(f"llm_agents.{stage}.api_key is required")
    client = LLMClient.from_config(llm_config)._get_client()
    request = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt},
        ],
        "max_tokens": int(llm_config.get("max_tokens", 10000)),
        "temperature": float(llm_config.get("temperature", 0.0)),
        "top_p": top_p,
        "seed": seed,
        "response_format": {"type": "json_object"},
    }
    if llm_config.get("extra_body"):
        request["extra_body"] = llm_config["extra_body"]
    started = time.perf_counter()
    response = client.chat.completions.create(**request)
    elapsed = time.perf_counter() - started
    if not response.choices:
        raise ReplayValidationError(f"{stage} model returned no choices")
    content = response.choices[0].message.content
    usage = extract_usage(response)
    if usage.prompt is None or usage.completion is None:
        raise ReplayValidationError(
            f"{stage} model response did not report input/output tokens"
        )
    price, price_source = price_table.resolve(model)
    estimated_cost = cost_usd(usage, price)
    if estimated_cost is None:
        raise ReplayValidationError(f"{stage} model cost could not be calculated")
    return ModelCallResult(
        content=content or "",
        input_tokens=usage.prompt,
        output_tokens=usage.completion,
        cache_read_tokens=usage.cache_hit or 0,
        cost_usd=estimated_cost,
        price_source=price_source,
        generation_seconds=elapsed,
    )


def _merge_call_results(results: list[ModelCallResult]) -> ModelCallResult:
    if not results:
        raise ValueError("at least one model call result is required")
    price_sources = {result.price_source for result in results}
    return ModelCallResult(
        content="",
        input_tokens=sum(result.input_tokens for result in results),
        output_tokens=sum(result.output_tokens for result in results),
        cache_read_tokens=sum(result.cache_read_tokens for result in results),
        cost_usd=sum(result.cost_usd for result in results),
        price_source=(
            next(iter(price_sources)) if len(price_sources) == 1 else "mixed"
        ),
        generation_seconds=sum(result.generation_seconds for result in results),
        calls=sum(result.calls for result in results),
    )


def _ordered_cases(
    response: dict[str, Any], source: dict[str, Any], *, stage: str
) -> list[dict[str, Any]]:
    cases = response.get("cases")
    if not isinstance(cases, list):
        raise ReplayValidationError(f"{stage}.cases must be a list")
    by_id: dict[str, dict[str, Any]] = {}
    for index, case in enumerate(cases):
        if not isinstance(case, dict) or not isinstance(case.get("case_id"), str):
            raise ReplayValidationError(f"{stage}.cases[{index}] has invalid case_id")
        case_id = case["case_id"]
        if case_id in by_id:
            raise ReplayValidationError(f"{stage}.cases has duplicate {case_id!r}")
        by_id[case_id] = case
    expected = [case["case_id"] for case in source["cases"]]
    if set(by_id) != set(expected):
        raise ReplayValidationError(
            f"{stage}.cases ids differ: expected={expected}, got={sorted(by_id)}"
        )
    return [by_id[case_id] for case_id in expected]


def _prepare_split_cases(
    response: dict[str, Any],
    source: dict[str, Any],
    *,
    algorithm_version: str,
) -> list[dict[str, Any]]:
    ordered = _ordered_cases(response, source, stage="split")
    prepared: list[dict[str, Any]] = []
    for source_case, case in zip(source["cases"], ordered):
        candidates = case.get("boundary_candidates")
        atoms = case.get("predicted_atoms")
        if not isinstance(candidates, list) or not isinstance(atoms, list):
            raise ReplayValidationError(
                f"split case {case['case_id']!r} must contain candidate and Atom lists"
            )
        candidates_by_line: dict[int, dict[str, Any]] = {}
        for candidate_index, candidate in enumerate(candidates):
            if not isinstance(candidate, dict):
                raise ReplayValidationError(
                    f"split case {case['case_id']!r} candidate {candidate_index} "
                    "must be an object"
                )
            line = candidate.get("line")
            if isinstance(line, bool) or not isinstance(line, int):
                raise ReplayValidationError(
                    f"split case {case['case_id']!r} candidate line must be an integer"
                )
            if line in candidates_by_line:
                raise ReplayValidationError(
                    f"split case {case['case_id']!r} contains duplicate candidate lines"
                )
            candidates_by_line[line] = candidate
        if set(candidates_by_line) != set(source_case["candidate_lines"]):
            raise ReplayValidationError(
                f"split case {case['case_id']!r} must score every candidate line once"
            )
        normalized_candidates = []
        for line in source_case["candidate_lines"]:
            candidate = candidates_by_line[line]
            normalized = {
                "line": line,
                "boundary_score": candidate.get("boundary_score"),
                "algorithm_version": algorithm_version,
                "selected": candidate.get("selected"),
                "predicted_atom_id": None,
            }
            normalized_candidates.append(normalized)
        atoms_by_start: dict[int, dict[str, Any]] = {}
        for atom in atoms:
            if not isinstance(atom, dict):
                raise ReplayValidationError(
                    f"split case {case['case_id']!r} contains a non-object Atom"
                )
            start_line = atom.get("start_line")
            if isinstance(start_line, bool) or not isinstance(start_line, int):
                raise ReplayValidationError(
                    f"split case {case['case_id']!r} Atom start_line must be an integer"
                )
            if start_line in atoms_by_start:
                raise ReplayValidationError(
                    f"split case {case['case_id']!r} duplicates Atom start {start_line}"
                )
            atoms_by_start[start_line] = atom
        selected_starts = {
            candidate["line"]
            for candidate in normalized_candidates
            if candidate["selected"] is True
        }
        forced_starts = {item[0] for item in source_case["scorable_ranges"]}
        expected_starts = forced_starts | selected_starts
        if set(atoms_by_start) != expected_starts:
            raise ReplayValidationError(
                f"split case {case['case_id']!r} Atom starts differ: "
                f"expected={sorted(expected_starts)}, got={sorted(atoms_by_start)}"
            )
        normalized_atoms = []
        atom_index = 1
        for range_start, range_end in source_case["scorable_ranges"]:
            starts = sorted(
                start for start in expected_starts if range_start <= start < range_end
            )
            for start_index, start_line in enumerate(starts):
                atom = atoms_by_start[start_line]
                end_line = (
                    starts[start_index + 1]
                    if start_index + 1 < len(starts)
                    else range_end
                )
                normalized_atoms.append(
                    {
                        "id": f"pred-{case['case_id']}-{atom_index:04d}",
                        "start_line": start_line,
                        "end_line": end_line,
                        "intent": atom.get("intent"),
                        "summary": atom.get("summary"),
                    }
                )
                atom_index += 1
        normalized_by_start = {atom["start_line"]: atom for atom in normalized_atoms}
        for candidate in normalized_candidates:
            if candidate["selected"] is not True:
                continue
            candidate["predicted_atom_id"] = normalized_by_start[candidate["line"]][
                "id"
            ]
        prepared.append(
            {
                "case_id": case["case_id"],
                "boundary_candidates": normalized_candidates,
                "predicted_atoms": normalized_atoms,
            }
        )
    return prepared


def _apply_route_cases(
    response: dict[str, Any],
    source: dict[str, Any],
    split_cases: list[dict[str, Any]],
) -> None:
    ordered = _ordered_cases(response, source, stage="route")
    for route_case, split_case in zip(ordered, split_cases):
        route_atoms = route_case.get("atoms")
        if not isinstance(route_atoms, list):
            raise ReplayValidationError(
                f"route case {route_case['case_id']!r}.atoms must be a list"
            )
        route_by_id: dict[str, dict[str, Any]] = {}
        for atom in route_atoms:
            if not isinstance(atom, dict) or not isinstance(atom.get("id"), str):
                raise ReplayValidationError(
                    f"route case {route_case['case_id']!r} contains an invalid Atom"
                )
            if atom["id"] in route_by_id:
                raise ReplayValidationError(
                    f"route case {route_case['case_id']!r} duplicates Atom {atom['id']!r}"
                )
            route_by_id[atom["id"]] = atom
        expected_ids = [atom["id"] for atom in split_case["predicted_atoms"]]
        if set(route_by_id) != set(expected_ids):
            raise ReplayValidationError(
                f"route case {route_case['case_id']!r} Atom ids differ from split output"
            )
        for atom in split_case["predicted_atoms"]:
            routed = route_by_id[atom["id"]]
            atom["skills"] = routed.get("skills")
            atom["candidates"] = routed.get("candidates")
            atom["weight_scores"] = routed.get("weight_scores")


def _route_batches(
    source: dict[str, Any],
    split_cases: list[dict[str, Any]],
    *,
    batch_size: int,
) -> list[tuple[dict[str, Any], list[dict[str, Any]]]]:
    """Group complete cases without exceeding the production Atom batch size."""
    if (
        isinstance(batch_size, bool)
        or not isinstance(batch_size, int)
        or batch_size <= 0
    ):
        raise ValueError("cluster batch_size must be a positive integer")
    source_by_id = {case["case_id"]: case for case in source["cases"]}
    batches = []
    current_cases: list[dict[str, Any]] = []
    current_sources: list[dict[str, Any]] = []
    current_atoms = 0
    for split_case in split_cases:
        atom_count = len(split_case["predicted_atoms"])
        if atom_count > batch_size:
            raise ReplayValidationError(
                f"route case {split_case['case_id']!r} has {atom_count} Atoms, "
                f"exceeding cluster batch_size={batch_size}"
            )
        if current_cases and current_atoms + atom_count > batch_size:
            batches.append(({**source, "cases": current_sources}, current_cases))
            current_cases = []
            current_sources = []
            current_atoms = 0
        current_cases.append(split_case)
        current_sources.append(source_by_id[split_case["case_id"]])
        current_atoms += atom_count
    if current_cases:
        batches.append(({**source, "cases": current_sources}, current_cases))
    return batches


def _route_batch_size(config: dict[str, Any]) -> int:
    """Validate the production routing batch size before spending model tokens."""
    agent_worker = config.get("agent_worker") or {}
    if not isinstance(agent_worker, dict):
        raise TypeError("agent_worker must be a mapping")
    worker_pools = agent_worker.get("pools") or {}
    if not isinstance(worker_pools, dict):
        raise TypeError("agent_worker.pools must be a mapping")
    cluster_pool = worker_pools.get("cluster") or {}
    if not isinstance(cluster_pool, dict):
        raise TypeError("agent_worker.pools.cluster must be a mapping")
    batch_size = cluster_pool.get("batch_size", 8)
    if (
        isinstance(batch_size, bool)
        or not isinstance(batch_size, int)
        or batch_size <= 0
    ):
        raise ValueError("cluster batch_size must be a positive integer")
    return batch_size


def _validate_split_before_route(
    source: dict[str, Any],
    split_cases: list[dict[str, Any]],
    *,
    algorithm_version: str,
) -> None:
    """Reject malformed split output before spending a routing model call."""
    placeholder_skill = source["skill_catalog"][0]
    source_by_id = {case["case_id"]: case for case in source["cases"]}
    cases = []
    for split_case in split_cases:
        source_case = source_by_id[split_case["case_id"]]
        predicted_atoms = deepcopy(split_case["predicted_atoms"])
        for atom in predicted_atoms:
            atom["skills"] = [placeholder_skill]
            atom["candidates"] = [placeholder_skill]
            atom["weight_scores"] = [{"skill": placeholder_skill, "weightscore": 1}]
        cases.append(
            {
                "case_id": source_case["case_id"],
                "expected_language": source_case["expected_language"],
                "line_count": source_case["line_count"],
                "source_lines": source_case["source_lines"],
                "scorable_ranges": source_case["scorable_ranges"],
                "gold_atoms": source_case["gold_atoms"],
                "predicted_atoms": predicted_atoms,
                "boundary_candidates": split_case["boundary_candidates"],
            }
        )
    validate_suite(
        {
            "schema_version": 3,
            "source_schema_version": source["source_schema_version"],
            "suite_id": source["suite_id"],
            "metric_config": source["metric_config"],
            "stage_manifests": {
                "split": _dummy_stage_manifest(algorithm_version),
                "route": _dummy_stage_manifest("pre-route-validation"),
            },
            "skill_catalog": source["skill_catalog"],
            "cases": cases,
        }
    )


def _recorded_cases(
    source: dict[str, Any], predicted_cases: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    source_by_id = {case["case_id"]: case for case in source["cases"]}
    cases = []
    for predicted in predicted_cases:
        source_case = source_by_id[predicted["case_id"]]
        cases.append(
            {
                "case_id": source_case["case_id"],
                "scenario": source_case.get("scenario"),
                "expected_language": source_case["expected_language"],
                "line_count": source_case["line_count"],
                "source_lines": source_case["source_lines"],
                "scorable_ranges": source_case["scorable_ranges"],
                "candidate_lines": source_case["candidate_lines"],
                "gold_atoms": source_case["gold_atoms"],
                "predicted_atoms": predicted["predicted_atoms"],
                "boundary_candidates": predicted["boundary_candidates"],
            }
        )
    return cases


def _validate_route_batch(
    source: dict[str, Any],
    predicted_cases: list[dict[str, Any]],
    *,
    split_algorithm_version: str,
    route_algorithm_version: str,
) -> None:
    """Reject one malformed route batch before spending the next model call."""
    validate_suite(
        {
            "schema_version": 3,
            "source_schema_version": source["source_schema_version"],
            "suite_id": source["suite_id"],
            "metric_config": source["metric_config"],
            "stage_manifests": {
                "split": _dummy_stage_manifest(split_algorithm_version),
                "route": _dummy_stage_manifest(route_algorithm_version),
            },
            "skill_catalog": source["skill_catalog"],
            "cases": _recorded_cases(source, predicted_cases),
        }
    )


def _stage_manifest(
    *,
    algorithm_version: str,
    repository_revision: str,
    harness: str,
    llm_config: dict[str, Any],
    prompt_fingerprint: str,
    generated_at: str,
    seed: int,
    top_p: float,
    result: ModelCallResult,
) -> dict[str, Any]:
    return {
        "algorithm_version": algorithm_version,
        "repository_revision": repository_revision,
        "model": llm_config["model"],
        "harness": harness,
        "prompt_fingerprint": prompt_fingerprint,
        "generated_at": generated_at,
        "seed": seed,
        "calls": result.calls,
        "input_tokens": result.input_tokens,
        "output_tokens": result.output_tokens,
        "cache_read_tokens": result.cache_read_tokens,
        "cost_usd": round(result.cost_usd, 9),
        "price_source": result.price_source,
        "generation_seconds": round(result.generation_seconds, 6),
        "generation_config": {
            "temperature": float(llm_config.get("temperature", 0.0)),
            "top_p": top_p,
            "max_output_tokens": int(llm_config.get("max_tokens", 10000)),
        },
    }


def record_suite(
    source: dict[str, Any],
    config: dict[str, Any],
    *,
    repository_revision: str,
    harness: str = "xskill-openai-compatible",
    split_algorithm_version: str = DEFAULT_SPLIT_ALGORITHM_VERSION,
    route_algorithm_version: str = DEFAULT_ROUTE_ALGORITHM_VERSION,
    seed: int = 0,
    top_p: float = 1.0,
    generated_at: str | None = None,
    model_caller: ModelCaller = _default_model_caller,
) -> dict[str, Any]:
    """Call split and route models once each and return a validated v3 suite."""
    validate_source_suite(source)
    if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
        raise ValueError("seed must be a non-negative integer")
    if (
        isinstance(top_p, bool)
        or not isinstance(top_p, (int, float))
        or not 0 < top_p <= 1
    ):
        raise ValueError("top_p must satisfy 0 < top_p <= 1")
    for field, value in (
        ("repository_revision", repository_revision),
        ("harness", harness),
        ("split_algorithm_version", split_algorithm_version),
        ("route_algorithm_version", route_algorithm_version),
    ):
        if not isinstance(value, str) or not value:
            raise ValueError(f"{field} must be a non-empty string")
    if generated_at is not None and (
        not isinstance(generated_at, str) or not generated_at
    ):
        raise ValueError("generated_at must be a non-empty string when supplied")

    split_config = resolve_agent_llm_config(config, "split")
    route_config = resolve_agent_llm_config(config, "cluster")
    for stage, stage_config in (("split", split_config), ("route", route_config)):
        for field in ("base_url", "model", "api_key"):
            if not isinstance(stage_config.get(field), str) or not stage_config[field]:
                raise ValueError(f"{stage} LLM configuration requires {field}")
        temperature = stage_config.get("temperature", 0.0)
        if (
            isinstance(temperature, bool)
            or not isinstance(temperature, (int, float))
            or not math.isfinite(float(temperature))
            or temperature < 0
        ):
            raise ValueError(f"{stage} LLM temperature must be a number >= 0")
        max_tokens = stage_config.get("max_tokens", 10000)
        if (
            isinstance(max_tokens, bool)
            or not isinstance(max_tokens, int)
            or max_tokens <= 0
        ):
            raise ValueError(f"{stage} LLM max_tokens must be a positive integer")
        extra_body = stage_config.get("extra_body")
        if extra_body is not None and not isinstance(extra_body, dict):
            raise TypeError(f"{stage} LLM extra_body must be a mapping")
    route_batch_size = _route_batch_size(config)
    price_table = load_price_table(config.get("pricing"))
    timestamp = generated_at or datetime.now(timezone.utc).isoformat().replace(
        "+00:00", "Z"
    )

    split_cases = []
    split_results = []
    for source_case in source["cases"]:
        case_source = {**source, "cases": [source_case]}
        split_result = model_caller(
            "split",
            split_config,
            SPLIT_SYSTEM_PROMPT,
            _split_prompt(case_source),
            seed,
            float(top_p),
            price_table,
        )
        split_payload = _parse_json_response(split_result.content, stage="split")
        prepared = _prepare_split_cases(
            split_payload,
            case_source,
            algorithm_version=split_algorithm_version,
        )
        _validate_split_before_route(
            case_source,
            prepared,
            algorithm_version=split_algorithm_version,
        )
        split_cases.extend(prepared)
        split_results.append(split_result)
    split_result = _merge_call_results(split_results)
    _validate_split_before_route(
        source, split_cases, algorithm_version=split_algorithm_version
    )

    route_results = []
    for source_batch, split_batch in _route_batches(
        source, split_cases, batch_size=route_batch_size
    ):
        route_result = model_caller(
            "route",
            route_config,
            ROUTE_SYSTEM_PROMPT,
            _route_prompt(source_batch, split_batch),
            seed,
            float(top_p),
            price_table,
        )
        route_payload = _parse_json_response(route_result.content, stage="route")
        _apply_route_cases(route_payload, source_batch, split_batch)
        _validate_route_batch(
            source_batch,
            split_batch,
            split_algorithm_version=split_algorithm_version,
            route_algorithm_version=route_algorithm_version,
        )
        route_results.append(route_result)
    route_result = _merge_call_results(route_results)

    suite = {
        "schema_version": 3,
        "source_schema_version": source["source_schema_version"],
        "suite_id": source["suite_id"],
        "metric_config": source["metric_config"],
        "stage_manifests": {
            "split": _stage_manifest(
                algorithm_version=split_algorithm_version,
                repository_revision=repository_revision,
                harness=harness,
                llm_config=split_config,
                prompt_fingerprint=_prompt_fingerprint(
                    SPLIT_SYSTEM_PROMPT, SPLIT_OUTPUT_INSTRUCTION
                ),
                generated_at=timestamp,
                seed=seed,
                top_p=float(top_p),
                result=split_result,
            ),
            "route": _stage_manifest(
                algorithm_version=route_algorithm_version,
                repository_revision=repository_revision,
                harness=harness,
                llm_config=route_config,
                prompt_fingerprint=_prompt_fingerprint(
                    ROUTE_SYSTEM_PROMPT, ROUTE_OUTPUT_INSTRUCTION
                ),
                generated_at=timestamp,
                seed=seed,
                top_p=float(top_p),
                result=route_result,
            ),
        },
        "skill_catalog": source["skill_catalog"],
        "cases": _recorded_cases(source, split_cases),
    }
    validate_suite(suite)
    return suite


def _write_json(path: Path, payload: dict[str, Any], *, overwrite: bool) -> None:
    path = Path(path)
    if path.exists() and not overwrite:
        raise FileExistsError(f"output already exists: {path}; pass --overwrite")
    path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    with NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False
    ) as handle:
        temporary = Path(handle.name)
        handle.write(serialized)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _git_revision() -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Record real model output for xskill Atom replay."
    )
    parser.add_argument("source", type=Path, help="Explicit privacy-safe source JSON")
    parser.add_argument("output", type=Path, help="New recorded replay JSON")
    parser.add_argument("--config", required=True, type=Path, help="xskill YAML config")
    parser.add_argument(
        "--api-key-env",
        help="Read the API key from this environment variable without persisting it",
    )
    parser.add_argument("--harness", default="xskill-openai-compatible")
    parser.add_argument(
        "--split-algorithm-version", default=DEFAULT_SPLIT_ALGORITHM_VERSION
    )
    parser.add_argument(
        "--route-algorithm-version", default=DEFAULT_ROUTE_ALGORITHM_VERSION
    )
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--top-p", type=float, default=1.0)
    parser.add_argument("--overwrite", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.output.exists() and not args.overwrite:
        raise FileExistsError(f"output already exists: {args.output}; pass --overwrite")
    source = _read_json(args.source)
    config_payload = yaml.safe_load(args.config.read_text(encoding="utf-8")) or {}
    if not isinstance(config_payload, dict):
        raise TypeError(f"{args.config}: expected a YAML mapping")
    if args.api_key_env:
        api_key = os.environ.get(args.api_key_env)
        if not api_key:
            raise ValueError(
                f"API key environment variable is empty: {args.api_key_env}"
            )
        llm_config = config_payload.setdefault("llm", {})
        if not isinstance(llm_config, dict):
            raise ValueError("llm must be a YAML mapping")
        llm_config["api_key"] = api_key
        stage_configs = config_payload.get("llm_agents") or {}
        if not isinstance(stage_configs, dict):
            raise ValueError("llm_agents must be a YAML mapping")
        for stage in ("split", "cluster"):
            stage_config = stage_configs.get(stage)
            if stage_config is not None:
                if not isinstance(stage_config, dict):
                    raise ValueError(f"llm_agents.{stage} must be a YAML mapping")
                stage_config["api_key"] = api_key
    suite = record_suite(
        source,
        config_payload,
        repository_revision=_git_revision(),
        harness=args.harness,
        split_algorithm_version=args.split_algorithm_version,
        route_algorithm_version=args.route_algorithm_version,
        seed=args.seed,
        top_p=args.top_p,
    )
    _write_json(args.output, suite, overwrite=args.overwrite)
    print(render_text(evaluate_suite(suite)))
    print(f"recorded_suite={args.output}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
