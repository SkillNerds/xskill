#!/usr/bin/env python3
"""校验精度评测合同：四份产物 schema、划分名单、官方口径。

只依赖标准库。不调用模型、不改其它包。供第一步合同测试和后续脚本复用。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any, Iterator

ROOT = Path(__file__).resolve().parent
SCHEMA_DIR = ROOT / "schemas"
OFFICIAL_COUNTS = {
    "officeqa": {"train": 50, "val": 24, "test": 172},
    "spreadsheet": {"train": 80, "val": 40, "test": 280},
    "alfworld": {"train": 39, "val": 18, "test": 134},
}
FAILURE_POLICY = (
    "timeout 标记为超时状态，计算准确率时与 fail 同样计为未通过（计入分母，不计入分子）"
)
XSKILL_TOOLS = ["Read", "Bash", "Skill"]
SKILLOPT_TOOLS = ["Read", "Bash"]
OFFICEQA_SCORER_ID = "skillopt.envs.officeqa.evaluator.evaluate"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class SchemaError(ValueError):
    pass


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def sha256_sorted_uids(uids: list[str]) -> str:
    text = "\n".join(sorted(uids)) + "\n"
    return sha256_bytes(text.encode("utf-8"))


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def load_schema(name: str) -> dict:
    return load_json(SCHEMA_DIR / name)


def _join(path: str, key: Any) -> str:
    if path == "$":
        return f"$.{key}" if not isinstance(key, int) else f"$[{key}]"
    return f"{path}.{key}" if not isinstance(key, int) else f"{path}[{key}]"


def _type_ok(value: Any, declared: Any) -> bool:
    mapping = {
        "object": dict,
        "array": list,
        "string": str,
        "integer": int,
        "number": (int, float),
        "boolean": bool,
        "null": type(None),
    }
    types = declared if isinstance(declared, list) else [declared]
    for item in types:
        py = mapping[item]
        if item == "integer" and isinstance(value, bool):
            continue
        if item == "number" and isinstance(value, bool):
            continue
        if isinstance(value, py):
            return True
    return False


def iter_schema_errors(instance: Any, schema: dict, path: str = "$") -> Iterator[str]:
    if "const" in schema and instance != schema["const"]:
        yield f"{path}: expected const {schema['const']!r}, got {instance!r}"
        return
    if "enum" in schema and instance not in schema["enum"]:
        yield f"{path}: {instance!r} not in {schema['enum']!r}"
        return
    if "type" in schema and not _type_ok(instance, schema["type"]):
        yield f"{path}: type {type(instance).__name__} not {schema['type']}"
        return
    if isinstance(instance, str):
        if "minLength" in schema and len(instance) < schema["minLength"]:
            yield f"{path}: string shorter than {schema['minLength']}"
        if "pattern" in schema and not re.search(schema["pattern"], instance):
            yield f"{path}: {instance!r} does not match {schema['pattern']}"
    if isinstance(instance, (int, float)) and not isinstance(instance, bool):
        if "minimum" in schema and instance < schema["minimum"]:
            yield f"{path}: {instance} < minimum {schema['minimum']}"
        if "maximum" in schema and instance > schema["maximum"]:
            yield f"{path}: {instance} > maximum {schema['maximum']}"
    if isinstance(instance, list) and "items" in schema:
        if "minItems" in schema and len(instance) < schema["minItems"]:
            yield f"{path}: array shorter than {schema['minItems']}"
        if "maxItems" in schema and len(instance) > schema["maxItems"]:
            yield f"{path}: array longer than {schema['maxItems']}"
        for i, item in enumerate(instance):
            yield from iter_schema_errors(item, schema["items"], _join(path, i))
    if isinstance(instance, dict):
        required = schema.get("required") or []
        for key in required:
            if key not in instance:
                yield f"{path}: missing required field {key!r}"
        props = schema.get("properties") or {}
        additional = schema.get("additionalProperties", True)
        for key, value in instance.items():
            if key in props:
                yield from iter_schema_errors(value, props[key], _join(path, key))
            elif additional is False:
                yield f"{path}: unexpected field {key!r}"
            elif isinstance(additional, dict):
                yield from iter_schema_errors(value, additional, _join(path, key))


def validate_schema(instance: Any, schema: dict, label: str = "document") -> list[str]:
    return [f"{label}{msg[1:]}" if msg.startswith("$") else f"{label}: {msg}" for msg in iter_schema_errors(instance, schema)]


def validate_run_config(doc: dict, *, official: bool = True) -> list[str]:
    errors = validate_schema(doc, load_schema("run_config.schema.json"), "run_config")
    if errors:
        return errors
    harness = doc["harness"]
    algo = doc["algo"]
    if algo == "xskill":
        if harness["id"] != "claude_code_native_skills":
            errors.append("run_config: xskill harness.id must be claude_code_native_skills")
        if official and harness["tools"] != XSKILL_TOOLS:
            errors.append(f"run_config: xskill tools must be {XSKILL_TOOLS}, got {harness['tools']}")
    if algo == "skillopt":
        if harness["id"] != "skillopt_claude_code_exec":
            errors.append("run_config: skillopt harness.id must be skillopt_claude_code_exec")
        if official and harness["tools"] != SKILLOPT_TOOLS:
            errors.append(f"run_config: skillopt tools must be {SKILLOPT_TOOLS}, got {harness['tools']}")
    if "Write" in harness["tools"] or "Edit" in harness["tools"]:
        errors.append("run_config: Write/Edit are not allowed")
    if official and doc.get("failure_policy") != FAILURE_POLICY:
        errors.append("run_config: failure_policy does not match the official wording")
    if official and doc["benchmark"] == "officeqa":
        scorer_id = doc["scorer"]["id"]
        if scorer_id != OFFICEQA_SCORER_ID:
            errors.append(
                f"run_config: OfficeQA scorer.id must be {OFFICEQA_SCORER_ID}, got {scorer_id!r}"
            )
        if "reward.py" in scorer_id.lower() or "databricks" in scorer_id.lower():
            errors.append("run_config: OfficeQA must not use databricks reward.py")
    return errors


def validate_train_provenance(doc: dict, *, official: bool = True) -> list[str]:
    errors = validate_schema(doc, load_schema("train_provenance.schema.json"), "train_provenance")
    if errors:
        return errors
    algo = doc["algo"]
    if official and algo == "xskill":
        if doc["merge_val_into_train"] is not True:
            errors.append("train_provenance: official xskill must merge val into train")
        if doc["selection_split"] is not None:
            errors.append("train_provenance: official xskill selection_split must be null")
        if doc["target_harness"] != "claude_code_native_skills":
            errors.append("train_provenance: xskill target_harness must be claude_code_native_skills")
    if official and algo == "skillopt":
        if doc["merge_val_into_train"] is not False:
            errors.append("train_provenance: official skillopt must keep val as a gate")
        if doc["selection_split"] != "val":
            errors.append("train_provenance: official skillopt selection_split must be val")
        if doc["target_harness"] != "skillopt_claude_code_exec":
            errors.append("train_provenance: skillopt target_harness must be skillopt_claude_code_exec")
        if doc.get("optimizer_backend") != "openai_chat":
            errors.append("train_provenance: skillopt optimizer_backend must be openai_chat")
    return errors


def validate_result_record(doc: dict) -> list[str]:
    errors = validate_schema(doc, load_schema("result_record.schema.json"), "result_record")
    if errors:
        return errors
    if doc["status"] == "pass" and doc["is_correct"] is not True:
        errors.append("result_record: pass requires is_correct=true")
    if doc["status"] != "pass" and doc["is_correct"] is not False:
        errors.append("result_record: non-pass status requires is_correct=false")
    usage = doc.get("usage") or {}
    if usage.get("source") == "missing":
        return errors
    if usage.get("source") == "local_fallback" and usage.get("request_ids"):
        # allowed but unusual; no error
        pass
    return errors


def validate_results_jsonl(path: Path) -> list[str]:
    errors: list[str] = []
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError as exc:
            errors.append(f"results.jsonl:{i}: invalid json ({exc})")
            continue
        errors.extend(f"results.jsonl:{i}: {e.split(': ', 1)[-1]}" for e in validate_result_record(rec))
    return errors


def validate_summary(doc: dict, *, results: list[dict] | None = None) -> list[str]:
    errors = validate_schema(doc, load_schema("summary.schema.json"), "summary")
    if errors:
        return errors
    counts = doc["status_counts"]
    counted = sum(counts.values())
    if counted != doc["n_total"]:
        errors.append(f"summary: status_counts sum {counted} != n_total {doc['n_total']}")
    if counts["pass"] != doc["n_pass"]:
        errors.append("summary: n_pass must equal status_counts.pass")
    expected_acc = (doc["n_pass"] / doc["n_total"]) if doc["n_total"] else 0.0
    if abs(doc["accuracy"] - expected_acc) > 1e-6:
        errors.append(
            f"summary: accuracy {doc['accuracy']} != n_pass/n_total {expected_acc}"
        )
    if results is not None:
        if len(results) != doc["n_total"]:
            errors.append(f"summary: results rows {len(results)} != n_total {doc['n_total']}")
        actual = {key: 0 for key in counts}
        for rec in results:
            actual[rec["status"]] = actual.get(rec["status"], 0) + 1
        if actual != counts:
            errors.append(f"summary: status_counts {counts} != results {actual}")
    return errors


def validate_split_manifest(doc: dict) -> list[str]:
    errors = validate_schema(doc, load_schema("split_manifest.schema.json"), "split_manifest")
    if errors:
        return errors
    bench = doc["benchmark"]
    expected = OFFICIAL_COUNTS[bench]
    if doc["counts"] != expected:
        errors.append(f"split_manifest: counts {doc['counts']} != official {expected}")
    for split in ("train", "val", "test"):
        uids = doc[split]
        if len(uids) != doc["counts"][split]:
            errors.append(f"split_manifest: len({split}) {len(uids)} != counts.{split}")
        if len(uids) != len(set(uids)):
            errors.append(f"split_manifest: {split} has duplicate ids")
    overlap = (
        set(doc["train"]) & set(doc["val"])
        | set(doc["train"]) & set(doc["test"])
        | set(doc["val"]) & set(doc["test"])
    )
    if overlap:
        errors.append(f"split_manifest: split overlap {sorted(overlap)[:8]}")
    forbidden = {"question", "instruction", "gold_answer", "ground_truth", "gamefile", "text"}
    if forbidden & set(doc):
        errors.append("split_manifest: must be id-only; found corpus-like fields")
    return errors


def _load_results(path: Path) -> list[dict]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def validate_example_dir(example_dir: Path, *, official: bool = True) -> list[str]:
    errors: list[str] = []
    run_config = example_dir / "run_config.json"
    results = example_dir / "results.jsonl"
    summary = example_dir / "summary.json"
    provenance = example_dir / "train_provenance.json"
    if run_config.exists():
        errors.extend(validate_run_config(load_json(run_config), official=official))
    if results.exists():
        errors.extend(validate_results_jsonl(results))
    if summary.exists():
        rows = _load_results(results) if results.exists() else None
        errors.extend(validate_summary(load_json(summary), results=rows))
    if provenance.exists():
        errors.extend(validate_train_provenance(load_json(provenance), official=official))
    return [f"{example_dir.name}: {e}" for e in errors]


def iter_official_paths() -> Iterator[Path]:
    for bench in ("officeqa", "spreadsheet", "alfworld"):
        yield ROOT / bench / "manifests" / f"{bench}_skillopt_id_split.json"
        examples = ROOT / bench / "examples"
        if examples.is_dir():
            for child in sorted(examples.iterdir()):
                if child.is_dir():
                    yield child


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate accuracy-bench contract files")
    parser.add_argument("paths", nargs="*", help="files or example directories; default: official tree")
    parser.add_argument("--no-official-policy", action="store_true")
    args = parser.parse_args(argv)
    official = not args.no_official_policy
    paths = [Path(p) for p in args.paths] if args.paths else list(iter_official_paths())
    errors: list[str] = []
    for path in paths:
        if path.is_dir():
            errors.extend(validate_example_dir(path, official=official))
            continue
        name = path.name
        try:
            if name.endswith(".jsonl"):
                errors.extend(f"{path}: {e}" for e in validate_results_jsonl(path))
            else:
                doc = load_json(path)
                if "train" in doc and "test" in doc and "counts" in doc:
                    errors.extend(f"{path}: {e}" for e in validate_split_manifest(doc))
                elif "status_counts" in doc:
                    errors.extend(f"{path}: {e}" for e in validate_summary(doc))
                elif "merge_val_into_train" in doc:
                    errors.extend(
                        f"{path}: {e}" for e in validate_train_provenance(doc, official=official)
                    )
                elif "harness" in doc:
                    errors.extend(f"{path}: {e}" for e in validate_run_config(doc, official=official))
                elif "uid" in doc:
                    errors.extend(f"{path}: {e}" for e in validate_result_record(doc))
                else:
                    errors.append(f"{path}: unrecognized contract document")
        except (OSError, json.JSONDecodeError) as exc:
            errors.append(f"{path}: {exc}")
    if errors:
        for err in errors:
            print(err, file=sys.stderr)
        return 1
    print(f"ok {len(paths)} path(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
