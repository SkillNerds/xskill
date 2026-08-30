"""Raw-only Formation inputs and scorer-only truth contracts.

The Formation-facing payload is built from the raw suite alone.  Gold boundaries,
evidence annotations, outcomes, and learning eligibility live in a separate truth
suite that is loaded only by the offline scorer.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any

from scripts.bench.algorithm_replay.evaluate import ReplayValidationError

RAW_SCHEMA_VERSION = 1
TRUTH_SCHEMA_VERSION = 1
ANNOTATION_SCHEMA_VERSION = 1

_RAW_ROOT_KEYS = {
    "raw_schema_version",
    "suite_id",
    "dataset_manifest",
    "cases",
}
_RAW_MANIFEST_KEYS = {"dataset_id", "source_uri", "revision", "license"}
_RAW_CASE_KEYS = {"case_id", "raw_content"}
_TRUTH_ROOT_KEYS = {
    "truth_schema_version",
    "annotation_schema_version",
    "suite_id",
    "cases",
}
_TRUTH_CASE_KEYS = {
    "case_id",
    "raw_sha256",
    "provenance",
    "boundaries",
    "gold_atoms",
}
_PROVENANCE_KEYS = {"source_ids", "composition_scenario"}
_BOUNDARY_KEYS = {"line", "decision", "type", "ambiguous"}
_ATOM_KEYS = {
    "id",
    "start_line",
    "end_line",
    "outcome_state",
    "evidence",
    "evidence_completeness",
    "learning_eligibility",
    "eligibility_reasons",
}
_EVIDENCE_TYPES = {
    "goal",
    "action",
    "tool_feedback",
    "artifact_change",
    "verification",
    "outcome",
    "user_acceptance",
    "user_rejection",
}
_BOUNDARY_DECISIONS = {"split", "keep"}
_BOUNDARY_TYPES = {
    "new_goal",
    "continue",
    "clarify",
    "correct",
    "retry",
    "abandon_or_return",
    "uncertain",
}
_OUTCOME_STATES = {"completed", "failed", "abandoned", "unknown"}
_EVIDENCE_COMPLETENESS = {"complete", "partial", "minimal"}
_LEARNING_ELIGIBILITY = {"eligible", "ineligible", "uncertain"}
_COMPOSITION_SCENARIOS = {
    "organic",
    "a_to_b",
    "a_to_b_to_a",
    "continuation",
    "clarification",
    "correction",
    "retry",
    "abandon_or_return",
    "similar_goal_hard_negative",
    "missing_terminal",
}
_USER_HEADER_RE = re.compile(
    r"^##\s+(?:User|Initial\s+Query)\b",
    re.IGNORECASE,
)
_SECTION_HEADER_RE = re.compile(r"^##\s+\S+")


def _error(path: str, message: str) -> ReplayValidationError:
    return ReplayValidationError(f"{path}: {message}")


def _expect_exact_keys(value: Any, expected: set[str], path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _error(path, "expected an object")
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise _error(path, f"keys mismatch; missing={missing}, extra={extra}")
    return value


def _expect_non_empty_string(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _error(path, "expected a non-empty string")
    return value


def _expect_strict_int(value: Any, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _error(path, "expected an integer")
    return value


def _expect_bool(value: Any, path: str) -> bool:
    if not isinstance(value, bool):
        raise _error(path, "expected a boolean")
    return value


def _expect_enum(value: Any, choices: set[str], path: str) -> str:
    if not isinstance(value, str) or value not in choices:
        raise _error(path, f"expected one of {sorted(choices)}, got {value!r}")
    return value


def _read_json(path: str | Path) -> dict[str, Any]:
    source_path = Path(path)
    try:
        value = json.loads(source_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ReplayValidationError(
            f"invalid JSON in {source_path}: {error}"
        ) from error
    if not isinstance(value, dict):
        raise ReplayValidationError(f"{source_path}: expected a JSON object")
    return value


def raw_content_sha256(raw_content: str) -> str:
    """Return the stable hash used to bind hidden truth to one raw session."""
    digest = hashlib.sha256(raw_content.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def _raw_lines(raw_content: str) -> list[str]:
    return raw_content.splitlines()


def _user_header_lines(raw_content: str) -> list[int]:
    return [
        line_number
        for line_number, line in enumerate(_raw_lines(raw_content), start=1)
        if _USER_HEADER_RE.match(line.strip())
    ]


def validate_raw_suite(source: Any) -> None:
    """Validate data that may be exposed to the Formation algorithm."""
    root = _expect_exact_keys(source, _RAW_ROOT_KEYS, "raw")
    if root["raw_schema_version"] != RAW_SCHEMA_VERSION:
        raise _error(
            "raw.raw_schema_version",
            f"supported={RAW_SCHEMA_VERSION}, got={root['raw_schema_version']!r}",
        )
    _expect_non_empty_string(root["suite_id"], "raw.suite_id")
    manifest = _expect_exact_keys(
        root["dataset_manifest"],
        _RAW_MANIFEST_KEYS,
        "raw.dataset_manifest",
    )
    for key in sorted(_RAW_MANIFEST_KEYS):
        _expect_non_empty_string(manifest[key], f"raw.dataset_manifest.{key}")

    cases = root["cases"]
    if not isinstance(cases, list) or not cases:
        raise _error("raw.cases", "expected a non-empty list")
    case_ids: set[str] = set()
    for case_index, value in enumerate(cases):
        path = f"raw.cases[{case_index}]"
        case = _expect_exact_keys(value, _RAW_CASE_KEYS, path)
        case_id = _expect_non_empty_string(case["case_id"], f"{path}.case_id")
        if case_id in case_ids:
            raise _error(f"{path}.case_id", f"duplicate case id {case_id!r}")
        case_ids.add(case_id)
        raw_content = _expect_non_empty_string(
            case["raw_content"],
            f"{path}.raw_content",
        )
        if not _user_header_lines(raw_content):
            raise _error(
                f"{path}.raw_content",
                "expected at least one ## User or ## Initial Query header",
            )


def build_formation_payload(source: Any) -> dict[str, Any]:
    """Build the complete Formation input without accepting truth data."""
    validate_raw_suite(source)
    return {
        "cases": [
            {
                "case_id": f"case-{case_index:06d}",
                "raw_content": case["raw_content"],
            }
            for case_index, case in enumerate(source["cases"], start=1)
        ]
    }


def formation_case_id_map(source: Any) -> dict[str, str]:
    """Return the scorer-side map from opaque payload ids to raw case ids."""
    validate_raw_suite(source)
    return {
        f"case-{case_index:06d}": case["case_id"]
        for case_index, case in enumerate(source["cases"], start=1)
    }


def _validate_boundary_annotations(
    boundaries: Any,
    *,
    raw_content: str,
    path: str,
) -> dict[int, dict[str, Any]]:
    if not isinstance(boundaries, list):
        raise _error(path, "expected a list")
    user_lines = _user_header_lines(raw_content)
    candidate_lines = user_lines[1:]
    by_line: dict[int, dict[str, Any]] = {}
    for index, value in enumerate(boundaries):
        boundary_path = f"{path}[{index}]"
        boundary = _expect_exact_keys(value, _BOUNDARY_KEYS, boundary_path)
        line = _expect_strict_int(boundary["line"], f"{boundary_path}.line")
        if line not in candidate_lines:
            raise _error(
                f"{boundary_path}.line",
                f"{line} is not an internal user-message header",
            )
        if line in by_line:
            raise _error(f"{boundary_path}.line", f"duplicate line {line}")
        _expect_enum(
            boundary["decision"],
            _BOUNDARY_DECISIONS,
            f"{boundary_path}.decision",
        )
        _expect_enum(
            boundary["type"],
            _BOUNDARY_TYPES,
            f"{boundary_path}.type",
        )
        decision = boundary["decision"]
        boundary_type = boundary["type"]
        if boundary_type == "new_goal" and decision != "split":
            raise _error(
                boundary_path,
                "new_goal must use decision='split'",
            )
        if (
            boundary_type in {"continue", "clarify", "correct", "retry"}
            and decision != "keep"
        ):
            raise _error(
                boundary_path,
                f"{boundary_type} must use decision='keep'",
            )
        _expect_bool(boundary["ambiguous"], f"{boundary_path}.ambiguous")
        if boundary_type == "uncertain" and not boundary["ambiguous"]:
            raise _error(
                boundary_path,
                "uncertain boundary type requires ambiguous=true",
            )
        by_line[line] = boundary
    if set(by_line) != set(candidate_lines):
        missing = sorted(set(candidate_lines) - set(by_line))
        extra = sorted(set(by_line) - set(candidate_lines))
        raise _error(path, f"must label every candidate; missing={missing}, extra={extra}")
    return by_line


def _validate_evidence(
    evidence: Any,
    *,
    raw_lines: list[str],
    start_line: int,
    end_line: int,
    path: str,
) -> dict[str, list[int]]:
    evidence_map = _expect_exact_keys(evidence, _EVIDENCE_TYPES, path)
    for evidence_type in sorted(_EVIDENCE_TYPES):
        lines = evidence_map[evidence_type]
        evidence_path = f"{path}.{evidence_type}"
        if not isinstance(lines, list):
            raise _error(evidence_path, "expected a list")
        normalized: list[int] = []
        for index, value in enumerate(lines):
            line = _expect_strict_int(value, f"{evidence_path}[{index}]")
            if not start_line <= line < end_line:
                raise _error(
                    f"{evidence_path}[{index}]",
                    f"line {line} falls outside Atom range [{start_line}, {end_line})",
                )
            source_line = raw_lines[line - 1].strip()
            if not source_line or _SECTION_HEADER_RE.match(source_line):
                raise _error(
                    f"{evidence_path}[{index}]",
                    f"line {line} does not contain evidence content",
                )
            normalized.append(line)
        if len(set(normalized)) != len(normalized):
            raise _error(evidence_path, "contains duplicate line references")
    if not evidence_map["goal"]:
        raise _error(f"{path}.goal", "must contain at least one goal reference")
    return evidence_map


def _validate_gold_atoms(
    atoms: Any,
    *,
    raw_content: str,
    boundaries_by_line: dict[int, dict[str, Any]],
    path: str,
) -> None:
    if not isinstance(atoms, list) or not atoms:
        raise _error(path, "expected a non-empty list")
    eof_line = len(_raw_lines(raw_content)) + 1
    raw_lines = _raw_lines(raw_content)
    user_lines = set(_user_header_lines(raw_content))
    atom_ids: set[str] = set()
    cursor = 1
    internal_starts: set[int] = set()
    for index, value in enumerate(atoms):
        atom_path = f"{path}[{index}]"
        atom = _expect_exact_keys(value, _ATOM_KEYS, atom_path)
        atom_id = _expect_non_empty_string(atom["id"], f"{atom_path}.id")
        if atom_id in atom_ids:
            raise _error(f"{atom_path}.id", f"duplicate Atom id {atom_id!r}")
        atom_ids.add(atom_id)
        start_line = _expect_strict_int(
            atom["start_line"],
            f"{atom_path}.start_line",
        )
        end_line = _expect_strict_int(atom["end_line"], f"{atom_path}.end_line")
        if start_line != cursor:
            raise _error(
                f"{atom_path}.start_line",
                f"expected contiguous start {cursor}, got {start_line}",
            )
        if end_line <= start_line or end_line > eof_line:
            raise _error(
                f"{atom_path}.end_line",
                f"expected {start_line} < end <= {eof_line}, got {end_line}",
            )
        if index and start_line not in user_lines:
            raise _error(
                f"{atom_path}.start_line",
                "internal Atom must start at a user-message header",
            )
        if index:
            internal_starts.add(start_line)
        _expect_enum(
            atom["outcome_state"],
            _OUTCOME_STATES,
            f"{atom_path}.outcome_state",
        )
        evidence = _validate_evidence(
            atom["evidence"],
            raw_lines=raw_lines,
            start_line=start_line,
            end_line=end_line,
            path=f"{atom_path}.evidence",
        )
        completeness = _expect_enum(
            atom["evidence_completeness"],
            _EVIDENCE_COMPLETENESS,
            f"{atom_path}.evidence_completeness",
        )
        if completeness == "complete":
            terminal = (
                evidence["verification"]
                or evidence["user_acceptance"]
                or evidence["user_rejection"]
            )
            if not evidence["action"] or not terminal:
                raise _error(
                    f"{atom_path}.evidence_completeness",
                    "complete evidence requires action and terminal references",
                )
        outcome_state = atom["outcome_state"]
        observable_outcome = (
            evidence["outcome"]
            or evidence["verification"]
            or evidence["user_acceptance"]
            or evidence["user_rejection"]
        )
        if outcome_state != "unknown" and not observable_outcome:
            raise _error(
                f"{atom_path}.outcome_state",
                "non-unknown outcome requires an outcome or terminal reference",
            )
        _expect_enum(
            atom["learning_eligibility"],
            _LEARNING_ELIGIBILITY,
            f"{atom_path}.learning_eligibility",
        )
        reasons = atom["eligibility_reasons"]
        if not isinstance(reasons, list) or not reasons:
            raise _error(
                f"{atom_path}.eligibility_reasons",
                "expected a non-empty list",
            )
        for reason_index, reason in enumerate(reasons):
            _expect_non_empty_string(
                reason,
                f"{atom_path}.eligibility_reasons[{reason_index}]",
            )
        cursor = end_line
    if cursor != eof_line:
        raise _error(path, f"Atoms end at {cursor}, expected EOF {eof_line}")
    split_lines = {
        line
        for line, annotation in boundaries_by_line.items()
        if annotation["decision"] == "split"
    }
    if internal_starts != split_lines:
        raise _error(
            path,
            "internal Atom starts must equal split decisions; "
            f"starts={sorted(internal_starts)}, splits={sorted(split_lines)}",
        )


def validate_truth_suite(truth: Any, raw_source: Any) -> None:
    """Validate scorer-only annotations against an already validated raw suite."""
    validate_raw_suite(raw_source)
    root = _expect_exact_keys(truth, _TRUTH_ROOT_KEYS, "truth")
    if root["truth_schema_version"] != TRUTH_SCHEMA_VERSION:
        raise _error(
            "truth.truth_schema_version",
            f"supported={TRUTH_SCHEMA_VERSION}, got={root['truth_schema_version']!r}",
        )
    if root["annotation_schema_version"] != ANNOTATION_SCHEMA_VERSION:
        raise _error(
            "truth.annotation_schema_version",
            "supported="
            f"{ANNOTATION_SCHEMA_VERSION}, got={root['annotation_schema_version']!r}",
        )
    if root["suite_id"] != raw_source["suite_id"]:
        raise _error("truth.suite_id", "does not match raw.suite_id")
    cases = root["cases"]
    if not isinstance(cases, list) or not cases:
        raise _error("truth.cases", "expected a non-empty list")
    raw_cases = {case["case_id"]: case for case in raw_source["cases"]}
    truth_ids: set[str] = set()
    for case_index, value in enumerate(cases):
        path = f"truth.cases[{case_index}]"
        case = _expect_exact_keys(value, _TRUTH_CASE_KEYS, path)
        case_id = _expect_non_empty_string(case["case_id"], f"{path}.case_id")
        if case_id in truth_ids:
            raise _error(f"{path}.case_id", f"duplicate case id {case_id!r}")
        truth_ids.add(case_id)
        if case_id not in raw_cases:
            raise _error(f"{path}.case_id", f"unknown raw case {case_id!r}")
        raw_content = raw_cases[case_id]["raw_content"]
        expected_hash = raw_content_sha256(raw_content)
        if case["raw_sha256"] != expected_hash:
            raise _error(
                f"{path}.raw_sha256",
                f"expected {expected_hash}, got {case['raw_sha256']!r}",
            )
        provenance = _expect_exact_keys(
            case["provenance"],
            _PROVENANCE_KEYS,
            f"{path}.provenance",
        )
        source_ids = provenance["source_ids"]
        if not isinstance(source_ids, list) or not source_ids:
            raise _error(
                f"{path}.provenance.source_ids",
                "expected a non-empty list",
            )
        for source_index, source_id in enumerate(source_ids):
            _expect_non_empty_string(
                source_id,
                f"{path}.provenance.source_ids[{source_index}]",
            )
        if len(set(source_ids)) != len(source_ids):
            raise _error(
                f"{path}.provenance.source_ids",
                "contains duplicate source ids",
            )
        _expect_enum(
            provenance["composition_scenario"],
            _COMPOSITION_SCENARIOS,
            f"{path}.provenance.composition_scenario",
        )
        boundaries = _validate_boundary_annotations(
            case["boundaries"],
            raw_content=raw_content,
            path=f"{path}.boundaries",
        )
        _validate_gold_atoms(
            case["gold_atoms"],
            raw_content=raw_content,
            boundaries_by_line=boundaries,
            path=f"{path}.gold_atoms",
        )
    if truth_ids != set(raw_cases):
        missing = sorted(set(raw_cases) - truth_ids)
        extra = sorted(truth_ids - set(raw_cases))
        raise _error(
            "truth.cases",
            f"case ids must match raw suite; missing={missing}, extra={extra}",
        )


def load_raw_suite(path: str | Path) -> dict[str, Any]:
    """Read and validate a raw-only suite."""
    source = _read_json(path)
    validate_raw_suite(source)
    return source


def load_truth_suite(
    path: str | Path,
    *,
    raw_source: Any,
) -> dict[str, Any]:
    """Read scorer-only truth and bind it to the exact raw suite."""
    truth = _read_json(path)
    validate_truth_suite(truth, raw_source)
    return truth


def _argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate raw-only Formation data contracts.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    payload = commands.add_parser(
        "payload",
        help="print the Formation input payload from a raw suite",
    )
    payload.add_argument("raw", type=Path)
    validate = commands.add_parser(
        "validate",
        help="validate scorer truth against an exact raw suite",
    )
    validate.add_argument("raw", type=Path)
    validate.add_argument("truth", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run raw payload generation or scorer-side pair validation."""
    args = _argument_parser().parse_args(argv)
    raw_source = load_raw_suite(args.raw)
    if args.command == "payload":
        print(json.dumps(build_formation_payload(raw_source), ensure_ascii=False))
        return 0
    truth = load_truth_suite(args.truth, raw_source=raw_source)
    print(
        json.dumps(
            {
                "status": "ok",
                "suite_id": raw_source["suite_id"],
                "case_count": len(truth["cases"]),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
