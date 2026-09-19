#!/usr/bin/env python3.11
"""把榜上已有评测记录收成精度合同的四个文件。不调模型、不重跑。"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent.parent
BENCH_ROOT = REPO_ROOT / "benchmarks"
if str(BENCH_ROOT) not in sys.path:
    sys.path.insert(0, str(BENCH_ROOT))
import validate  # noqa: E402

FAILURE_POLICY = validate.FAILURE_POLICY
OFFICEQA_SCORER = {
    "id": validate.OFFICEQA_SCORER_ID,
    "module": "skillopt.envs.officeqa.evaluator",
    "metric": "em",
    "soft_metric": "f1",
    "commit": "02cdead11f635546f11e7669b08a96ce823d6377",
    "sha256": "49dad2a2bdede9665541102fe47ca74ab2a0cc05ad84d2f8ca047f223c1b4aef",
    "notes": "SkillOpt 官方归一化整串匹配为 hard，token F1 为 soft。不用 Databricks reward.py。",
}
SPREADSHEET_SCORER = {
    "id": "spreadsheetbench.cell_compare",
    "metric": "hard",
    "notes": "逐单元格比对作者标准答案表。",
}
ALFWORLD_SCORER = {
    "id": "alfworld.env_won",
    "metric": "hard",
    "notes": "规定步数内环境返回 won 算通过。",
}


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def load_jsonl(path: Path) -> list[dict]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def write_json(path: Path, doc: Any) -> None:
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def classify_status(row: dict) -> str:
    """timeout 只看 fail_reason 和 error 的短标签，不扫长 traceback。"""
    hard = row.get("hard")
    if hard in (1, 1.0, True) or row.get("success") is True:
        return "pass"
    reason = str(row.get("fail_reason") or "").strip().lower()
    error = str(row.get("error") or "").strip().lower()
    if "rc=124" in reason:
        return "timeout"
    if "task-timeout" in reason or "timeout after" in reason:
        return "timeout"
    if reason in {"timeout", "timed out"} or reason.endswith(" timeout"):
        return "timeout"
    if "timed out" in reason or "timeoutexpired" in reason:
        return "timeout"
    if error in {"timeout", "timed out"}:
        return "timeout"
    if reason.startswith("no result json"):
        return "infra_error"
    return "fail"


def row_uid(row: dict) -> str:
    uid = row.get("id") or row.get("item_id") or row.get("uid")
    if not uid:
        raise ValueError("leaderboard row missing id")
    return str(uid)


def row_pred(row: dict) -> Any:
    if "predicted_answer" in row:
        pred = row.get("predicted_answer")
        if pred is None:
            return None
        text = str(pred).strip()
        return text if text else None
    status = classify_status(row)
    if status == "pass":
        if row.get("gamefile") or str(row.get("id") or "").startswith("test:"):
            return "won"
        return "ok"
    reason = str(row.get("fail_reason") or "").strip()
    return reason or None


def row_gold(row: dict) -> Any:
    if row.get("ground_truth") is not None:
        return str(row.get("ground_truth"))
    if row.get("gamefile") or str(row.get("id") or "").startswith("test:"):
        return "won"
    return None


def row_summary(row: dict) -> str:
    text = row.get("question") or row.get("task_description") or row.get("fail_reason") or ""
    text = " ".join(str(text).split())
    return text[:240]


def git_head(repo: Path) -> str:
    try:
        return subprocess.check_output(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            text=True,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def skill_tree_sha256(root: Path) -> str:
    digest = hashlib.sha256()
    files = sorted(p for p in root.rglob("*") if p.is_file() and ".git" not in p.parts)
    if not files:
        raise FileNotFoundError(f"no skill files under {root}")
    for path in files:
        rel = path.relative_to(root).as_posix()
        digest.update(rel.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
    return digest.hexdigest()


def resolve_skill_sha256(source_dir: Path, meta: dict) -> str | None:
    if meta.get("no_skill"):
        return None
    if meta.get("skill_sha256"):
        return str(meta["skill_sha256"])
    shipped = source_dir / "skills_shipped"
    if shipped.is_dir():
        children = [p for p in shipped.iterdir() if p.is_dir()]
        if len(children) == 1:
            return skill_tree_sha256(children[0])
        named = shipped / str(meta.get("skill_name") or "")
        if named.is_dir():
            return skill_tree_sha256(named)
    for name in ("best_skill.md", "SKILL.md", "skill.md"):
        path = source_dir / name
        if path.is_file() and path.stat().st_size > 0:
            return sha256_file(path)
    raise FileNotFoundError(
        f"{source_dir} has no skill_sha256 in meta.json and no shipped skill file"
    )


def official_harness(algo: str) -> tuple[str, list[str]]:
    if algo == "skillopt":
        return "skillopt_claude_code_exec", list(validate.SKILLOPT_TOOLS)
    if algo == "noskill":
        return "skillopt_claude_code_exec", list(validate.SKILLOPT_TOOLS)
    return "claude_code_native_skills", list(validate.XSKILL_TOOLS)


def convert_rows(raw_rows: list[dict], run_id: str, skill_sha256: str | None) -> list[dict]:
    out = []
    for row in raw_rows:
        status = classify_status(row)
        uid = row_uid(row)
        rec = {
            "schema_version": 1,
            "run_id": run_id,
            "uid": uid,
            "summary_text": row_summary(row),
            "gold_answer": row_gold(row),
            "pred_answer": row_pred(row),
            "status": status,
            "is_correct": status == "pass",
            "attempts": 1,
            "usage": {"source": "missing"},
            "skill_sha256": skill_sha256,
        }
        rec["log_file"] = str(row.get("log_file") or f"eval/logs/{uid}.log")
        out.append(rec)
    out.sort(key=lambda r: r["uid"])
    return out


def build_run_config(
    *,
    run_id: str,
    meta: dict,
    manifest_rel: str,
    skill_sha256: str | None,
    xskill_commit: str,
) -> dict:
    algo = meta["algo"]
    harness_id, tools = official_harness(algo)
    notes = (
        f"historical_import from leaderboard sub{meta.get('eval_submission_id')} "
        f"({meta.get('eval_job')}); no model rerun. "
        f"{meta.get('historical_notes') or ''} "
        f"Source eval tools were {meta.get('historical_tools')}. "
        "tools field keeps the official contract list. Not an official paper score."
    )
    return {
        "schema_version": 1,
        "run_id": run_id,
        "phase": "eval",
        "algo": algo,
        "benchmark": meta["benchmark"],
        "split_manifest": manifest_rel,
        "split_name": "test",
        "model": meta["model"],
        "harness": {
            "id": harness_id,
            "tools": tools,
            "notes": " ".join(notes.split()),
        },
        "skill_sha256": skill_sha256,
        "scorer": meta["scorer"],
        "code": {"xskill_commit": xskill_commit},
        "workers": int(meta["workers"]),
        "timeout_s": int(meta["timeout_s"]),
        "max_tool_turns": int(meta.get("max_tool_turns") or 24),
        "failure_policy": FAILURE_POLICY,
    }


def build_provenance(
    *,
    run_id: str,
    meta: dict,
    skill_sha256: str,
    train_uids_sha256: str,
) -> dict:
    algo = meta["algo"]
    target = (
        "skillopt_claude_code_exec"
        if algo == "skillopt"
        else "claude_code_native_skills"
    )
    doc: dict[str, Any] = {
        "schema_version": 1,
        "run_id": run_id,
        "algo": algo,
        "benchmark": meta["benchmark"],
        "train_uids_sha256": train_uids_sha256,
        "merge_val_into_train": bool(meta["merge_val_into_train"]),
        "selection_split": "val" if algo == "skillopt" else None,
        "target_harness": target,
        "algo_components": {
            "historical_import": True,
            "eval_submission_id": meta.get("eval_submission_id"),
            "train_submission_id": meta.get("train_submission_id"),
            "eval_job": meta.get("eval_job"),
            "train_job": meta.get("train_job"),
            "algorithm_image": meta.get("algorithm_image"),
            "train_algorithm_image": meta.get("train_algorithm_image"),
            "note": meta.get("provenance_note") or "imported from leaderboard train+eval artifacts",
        },
        "intermediate_checkpoints": [
            {
                "step": 1,
                "skill_sha256": skill_sha256,
                "action": "imported_trained_skill",
                "log_file": f"leaderboard:{meta.get('train_job') or meta.get('eval_job')}",
            }
        ],
        "skill_sha256": skill_sha256,
    }
    if algo == "skillopt":
        doc["optimizer_backend"] = "openai_chat"
    return doc


def build_summary(
    *,
    run_id: str,
    meta: dict,
    rows: list[dict],
    refs: dict,
) -> dict:
    counts = {"pass": 0, "fail": 0, "timeout": 0, "invalid": 0, "infra_error": 0}
    for rec in rows:
        counts[rec["status"]] = counts.get(rec["status"], 0) + 1
    n_total = len(rows)
    n_pass = counts["pass"]
    return {
        "schema_version": 1,
        "run_id": run_id,
        "benchmark": meta["benchmark"],
        "split_name": "test",
        "model": meta["model"],
        "n_total": n_total,
        "n_pass": n_pass,
        "accuracy": (n_pass / n_total) if n_total else 0.0,
        "status_counts": counts,
        "usage_totals": {
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "cost_usd": 0.0,
            "usage_missing_n": n_total,
        },
        "refs": refs,
        "started_at": meta.get("started_at") or "unknown",
        "finished_at": meta.get("finished_at") or "unknown",
        "resumed": False,
    }


def check_split(rows: list[dict], manifest: dict, *, allow_partial: bool) -> list[str]:
    errors = []
    got = {rec["uid"] for rec in rows}
    expected = set(manifest["test"])
    missing = sorted(expected - got)
    extra = sorted(got - expected)
    if extra:
        errors.append(f"uids not in official test split: {extra[:8]}")
    if missing and not allow_partial:
        errors.append(f"official test uids missing: {missing[:8]} (n={len(missing)})")
    if allow_partial and not got.issubset(expected):
        errors.append(f"partial import still has non-test uids: {extra[:8]}")
    return errors


def write_run(
    *,
    source_dir: Path,
    output_dir: Path,
    run_id: str,
    manifest_path: Path,
    repo_root: Path,
    allow_partial: bool,
    meta: dict,
) -> dict:
    source_dir = source_dir.resolve()
    output_dir = output_dir.resolve()
    results_src = source_dir / "results.jsonl"
    if not results_src.is_file():
        raise FileNotFoundError(f"missing {results_src}")
    if (source_dir / "meta.json").is_file():
        loaded = load_json(source_dir / "meta.json")
        merged = dict(meta)
        merged.update(loaded)
        meta = merged
    manifest = load_json(manifest_path)
    skill_sha256 = resolve_skill_sha256(source_dir, meta)
    xskill_commit = str(meta.get("xskill_commit") or git_head(repo_root))
    rows = convert_rows(load_jsonl(results_src), run_id, skill_sha256)
    split_errors = check_split(rows, manifest, allow_partial=allow_partial)
    if split_errors:
        raise ValueError("; ".join(split_errors))

    if meta.get("merge_val_into_train"):
        train_uids = list(manifest["train"]) + list(manifest["val"])
    else:
        train_uids = list(manifest["train"])
    train_uids_sha256 = validate.sha256_sorted_uids(train_uids)
    try:
        manifest_rel = str(manifest_path.resolve().relative_to(repo_root.resolve()))
    except ValueError:
        manifest_rel = str(manifest_path)

    output_dir.mkdir(parents=True, exist_ok=True)
    run_config = build_run_config(
        run_id=run_id,
        meta=meta,
        manifest_rel=manifest_rel,
        skill_sha256=skill_sha256,
        xskill_commit=xskill_commit,
    )
    results_path = output_dir / "results.jsonl"
    results_path.write_text(
        "".join(json.dumps(rec, ensure_ascii=False) + "\n" for rec in rows),
        encoding="utf-8",
    )
    run_config_path = output_dir / "run_config.json"
    write_json(run_config_path, run_config)
    refs: dict[str, Any] = {
        "run_config_sha256": sha256_file(run_config_path),
        "results_sha256": sha256_file(results_path),
        "skill_sha256": skill_sha256,
        "split_manifest_sha256": sha256_file(manifest_path),
    }
    write_prov = (not meta.get("no_skill")) and skill_sha256
    if write_prov:
        provenance = build_provenance(
            run_id=run_id,
            meta=meta,
            skill_sha256=skill_sha256,
            train_uids_sha256=train_uids_sha256,
        )
        provenance_path = output_dir / "train_provenance.json"
        write_json(provenance_path, provenance)
        refs["train_provenance_sha256"] = sha256_file(provenance_path)
    summary = build_summary(run_id=run_id, meta=meta, rows=rows, refs=refs)
    write_json(output_dir / "summary.json", summary)
    return {
        "output_dir": str(output_dir),
        "n_total": summary["n_total"],
        "n_pass": summary["n_pass"],
        "accuracy": summary["accuracy"],
        "status_counts": summary["status_counts"],
        "skill_sha256": skill_sha256,
    }


def validate_imported_run(output_dir: Path) -> list[str]:
    errors = []
    run_config = load_json(output_dir / "run_config.json")
    errors.extend(validate.validate_run_config(run_config, official=True))
    errors.extend(validate.validate_results_jsonl(output_dir / "results.jsonl"))
    rows = load_jsonl(output_dir / "results.jsonl")
    errors.extend(validate.validate_summary(load_json(output_dir / "summary.json"), results=rows))
    prov = output_dir / "train_provenance.json"
    if prov.is_file():
        errors.extend(validate.validate_train_provenance(load_json(prov), official=False))
    return errors


def _docker_cat(path: str) -> bytes:
    proc = subprocess.run(
        ["docker", "exec", "lb-control-plane", "cat", path],
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout).decode("utf-8", "replace")
        raise RuntimeError(f"docker cat {path} failed: {err}")
    return proc.stdout


def _docker_exists(path: str) -> bool:
    proc = subprocess.run(
        ["docker", "exec", "lb-control-plane", "test", "-f", path],
        capture_output=True,
        check=False,
    )
    return proc.returncode == 0


def _docker_isdir(path: str) -> bool:
    proc = subprocess.run(
        ["docker", "exec", "lb-control-plane", "test", "-d", path],
        capture_output=True,
        check=False,
    )
    return proc.returncode == 0


def export_kind_source(dest: Path, *, board: str, job: str, meta: dict) -> Path:
    root = f"/models/xarena/{board}/{job}"
    results_remote = f"{root}/eval/results.jsonl"
    if not _docker_exists(results_remote):
        raise FileNotFoundError(f"kind missing {results_remote}")
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "results.jsonl").write_bytes(_docker_cat(results_remote))
    summary_remote = f"{root}/eval/eval_summary.json"
    if _docker_exists(summary_remote):
        (dest / "eval_summary.json").write_bytes(_docker_cat(summary_remote))
    shipped_remote = f"{root}/algo/skills_shipped"
    if _docker_isdir(shipped_remote):
        subprocess.run(
            ["docker", "cp", f"lb-control-plane:{shipped_remote}", str(dest / "skills_shipped")],
            capture_output=True,
            check=False,
        )
    for name in ("best_skill.md", "SKILL.md", "skill.md"):
        remote = f"{root}/algo/{name}"
        if _docker_exists(remote):
            (dest / name).write_bytes(_docker_cat(remote))
    local_meta = dict(meta)
    local_meta["eval_job"] = job
    if not local_meta.get("no_skill"):
        try:
            local_meta["skill_sha256"] = resolve_skill_sha256(dest, {**local_meta, "skill_sha256": ""})
        except FileNotFoundError:
            pass
    write_json(dest / "meta.json", local_meta)
    return dest


def preset_catalog(repo_root: Path) -> dict[str, dict]:
    manifests = {
        "officeqa": repo_root / "benchmarks/officeqa/manifests/officeqa_skillopt_id_split.json",
        "spreadsheet": repo_root / "benchmarks/spreadsheet/manifests/spreadsheet_skillopt_id_split.json",
        "alfworld": repo_root / "benchmarks/alfworld/manifests/alfworld_skillopt_id_split.json",
    }
    return {
        "officeqa-sub200": {
            "run_id": "eval-officeqa-xskill-sub200-import",
            "kind_board": "officeqa-full-official-test172",
            "kind_job": "eval-sub-200-5aa5bd",
            "manifest": manifests["officeqa"],
            "meta": {
                "eval_submission_id": 200,
                "eval_job": "eval-sub-200-5aa5bd",
                "train_submission_id": 197,
                "train_job": "eval-sub-197-b61688",
                "algo": "xskill",
                "benchmark": "officeqa",
                "model": "deepseek-v4-flash",
                "workers": 4,
                "timeout_s": 600,
                "max_tool_turns": 24,
                "historical_tools": ["Read", "Write", "Edit", "Bash", "Skill"],
                "historical_notes": "Frozen retest of sub197 shipped main skill.",
                "merge_val_into_train": False,
                "scorer": OFFICEQA_SCORER,
                "algorithm_image": (
                    "localhost:5000/p_user1/algo-xskill-officeqa-frozen-sub197main:"
                    "sub197-main-v1-fail55-20260830"
                ),
                "train_algorithm_image": (
                    "localhost:5000/p_user1/algo-xskill-officeqa:hard-canary-pure-q1-20260829b"
                ),
                "started_at": "2026-08-29T18:55:19+00:00",
                "finished_at": "2026-08-29T20:45:03+00:00",
                "skill_name": "officeqa-document-qa",
            },
        },
        "spreadsheet-sub181": {
            "run_id": "eval-spreadsheet-xskill-sub181-import",
            "kind_board": "spreadsheet-full-official-test280",
            "kind_job": "eval-sub-181-fc3615",
            "manifest": manifests["spreadsheet"],
            "meta": {
                "eval_submission_id": 181,
                "eval_job": "eval-sub-181-fc3615",
                "train_submission_id": 181,
                "train_job": "eval-sub-181-fc3615",
                "algo": "xskill",
                "benchmark": "spreadsheet",
                "model": "deepseek-v4-flash",
                "workers": 3,
                "timeout_s": 600,
                "max_tool_turns": 5,
                "historical_tools": ["Read", "Bash", "Skill"],
                "historical_notes": (
                    "Train+eval on Spreadsheet Full test280. "
                    "Eval used the board single image (claude_code_exec)."
                ),
                "merge_val_into_train": False,
                "scorer": SPREADSHEET_SCORER,
                "algorithm_image": (
                    "localhost:5000/p_user1/algo-xskill-spreadsheet-full:"
                    "sub178base-v7prompt2-20260806"
                ),
                "train_algorithm_image": (
                    "localhost:5000/p_user1/algo-xskill-spreadsheet-full:"
                    "sub178base-v7prompt2-20260806"
                ),
                "started_at": "2026-08-06T10:03:54+00:00",
                "finished_at": "2026-08-07T00:22:00+00:00",
                "skill_name": "openpyxl-excel-automation",
                "provenance_note": "same job trained then evaluated the shipped skill",
            },
        },
        "spreadsheet-sub182": {
            "run_id": "eval-spreadsheet-skillopt-sub182-import",
            "kind_board": "spreadsheet-full-official-test280",
            "kind_job": "eval-sub-182-7fedec",
            "manifest": manifests["spreadsheet"],
            "meta": {
                "eval_submission_id": 182,
                "eval_job": "eval-sub-182-7fedec",
                "train_submission_id": 182,
                "train_job": "eval-sub-182-7fedec",
                "algo": "skillopt",
                "benchmark": "spreadsheet",
                "model": "deepseek-v4-flash",
                "workers": 4,
                "timeout_s": 600,
                "max_tool_turns": 24,
                "historical_tools": ["Read", "Bash"],
                "historical_notes": (
                    "Train target and rewrite were openai_chat. "
                    "Eval used the board single Claude Code exec image on the trained skill."
                ),
                "merge_val_into_train": False,
                "scorer": SPREADSHEET_SCORER,
                "algorithm_image": (
                    "localhost:5000/p_user1/algo-skillopt-spreadsheet-official-full:v1"
                ),
                "train_algorithm_image": (
                    "localhost:5000/p_user1/algo-skillopt-spreadsheet-official-full:v1"
                ),
                "started_at": "2026-08-07T01:55:06+00:00",
                "finished_at": "2026-08-07T10:55:00+00:00",
                "provenance_note": "same job trained then evaluated best_skill.md",
            },
        },
        "alfworld-sub81": {
            "run_id": "eval-alfworld-xskill-sub81-import",
            "kind_board": "alfworld-full-official-test134",
            "kind_job": "eval-sub-81-9390c3",
            "manifest": manifests["alfworld"],
            "meta": {
                "eval_submission_id": 81,
                "eval_job": "eval-sub-81-9390c3",
                "train_submission_id": 81,
                "train_job": "eval-sub-81-9390c3",
                "algo": "xskill",
                "benchmark": "alfworld",
                "model": "deepseek-v4-flash",
                "workers": 3,
                "timeout_s": 600,
                "max_tool_turns": 50,
                "historical_tools": ["Read", "Bash", "Skill"],
                "historical_notes": (
                    "Train was Native Claude Code. Eval on this board was still Chat ReAct. "
                    "Native Claude Code eval is step 5."
                ),
                "merge_val_into_train": False,
                "scorer": ALFWORLD_SCORER,
                "algorithm_image": (
                    "localhost:5000/p_user1/algo-xskill-alf-full:"
                    "workers3-projectionfix-20260715-v1"
                ),
                "train_algorithm_image": (
                    "localhost:5000/p_user1/algo-xskill-alf-full:"
                    "workers3-projectionfix-20260715-v1"
                ),
                "started_at": "2026-07-16T03:15:40+00:00",
                "finished_at": "2026-07-16T07:21:00+00:00",
                "skill_name": "alfworld-household-tasks",
                "provenance_note": "same job trained then evaluated the shipped skill",
            },
        },
        "alfworld-sub80": {
            "run_id": "eval-alfworld-skillopt-sub80-import",
            "kind_board": "alfworld-full-official-test134",
            "kind_job": "eval-sub-80-e1ab2b",
            "manifest": manifests["alfworld"],
            "meta": {
                "eval_submission_id": 80,
                "eval_job": "eval-sub-80-e1ab2b",
                "train_submission_id": 80,
                "train_job": "eval-sub-80-e1ab2b",
                "algo": "skillopt",
                "benchmark": "alfworld",
                "model": "deepseek-v4-flash",
                "workers": 4,
                "timeout_s": 600,
                "max_tool_turns": 50,
                "historical_tools": ["Read", "Bash"],
                "historical_notes": (
                    "Official SkillOpt full run. Eval on this board was Chat ReAct. "
                    "claude_code_exec eval is step 4."
                ),
                "merge_val_into_train": False,
                "scorer": ALFWORLD_SCORER,
                "algorithm_image": "localhost:5000/p_user1/algo-skillopt-alf-official-full:v1",
                "train_algorithm_image": "localhost:5000/p_user1/algo-skillopt-alf-official-full:v1",
                "started_at": "2026-07-15T04:47:18+00:00",
                "finished_at": "2026-07-15T18:06:00+00:00",
                "provenance_note": "same job trained then evaluated best_skill.md",
            },
        },
        "alfworld-sub86": {
            "run_id": "eval-alfworld-noskill-sub86-import",
            "kind_board": "alfworld-full-official-test134",
            "kind_job": "eval-sub-86-388c4e",
            "manifest": manifests["alfworld"],
            "meta": {
                "eval_submission_id": 86,
                "eval_job": "eval-sub-86-388c4e",
                "train_submission_id": None,
                "algo": "noskill",
                "benchmark": "alfworld",
                "model": "deepseek-v4-flash",
                "workers": 4,
                "timeout_s": 600,
                "max_tool_turns": 50,
                "historical_tools": ["Read", "Bash"],
                "historical_notes": "No skill injected. Eval was Chat ReAct on the official test134 board.",
                "merge_val_into_train": False,
                "no_skill": True,
                "scorer": ALFWORLD_SCORER,
                "algorithm_image": "localhost:5000/p_user1/alfworld-noskill:v1",
                "started_at": "2026-07-17T07:37:27+00:00",
                "finished_at": "2026-07-17T12:22:00+00:00",
            },
        },
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Import a leaderboard eval into four contract files")
    parser.add_argument("--preset", default="", help="named import, e.g. spreadsheet-sub181")
    parser.add_argument("--source-dir", type=Path)
    parser.add_argument("--from-kind-job", default="")
    parser.add_argument("--kind-board", default="")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--run-id", default="")
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--repo-root", type=Path, default=REPO_ROOT)
    parser.add_argument("--allow-partial-split", action="store_true")
    parser.add_argument("--skip-validate", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    catalog = preset_catalog(args.repo_root)
    preset = catalog.get(args.preset) if args.preset else None
    if args.preset and preset is None:
        print(f"unknown preset {args.preset!r}; choose from {sorted(catalog)}", file=sys.stderr)
        return 2
    meta = dict(preset["meta"]) if preset else {}
    manifest = args.manifest or (preset["manifest"] if preset else None)
    if manifest is None:
        print("need --manifest or --preset", file=sys.stderr)
        return 2
    run_id = args.run_id or (preset["run_id"] if preset else args.output_dir.name)
    kind_job = args.from_kind_job or (preset["kind_job"] if preset and not args.source_dir else "")
    kind_board = args.kind_board or (preset["kind_board"] if preset else "")
    source_dir = args.source_dir
    if kind_job:
        export_root = args.output_dir.parent / f".import-src-{kind_job}"
        source_dir = export_kind_source(export_root, board=kind_board, job=kind_job, meta=meta)
    if source_dir is None:
        print("need --source-dir, --from-kind-job, or --preset", file=sys.stderr)
        return 2
    info = write_run(
        source_dir=source_dir,
        output_dir=args.output_dir,
        run_id=run_id,
        manifest_path=Path(manifest),
        repo_root=args.repo_root,
        allow_partial=args.allow_partial_split,
        meta=meta,
    )
    if not args.skip_validate:
        errors = validate_imported_run(args.output_dir)
        if errors:
            for err in errors:
                print(err, file=sys.stderr)
            return 1
    print(json.dumps(info, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
