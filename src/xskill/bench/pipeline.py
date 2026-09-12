from __future__ import annotations

import json
import shutil
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, TextIO

from xskill.bench.artifacts import (
    build_run_config,
    build_summary,
    load_validate,
    now_iso,
    tools_for,
    validate_output_dir,
    write_json,
    write_jsonl,
)
from xskill.bench import proxy
from xskill.bench.card import eval_card, init_lines, train_card
from xskill.bench.constants import HARNESS_ALIASES
from xskill.bench.executor import make_executor
from xskill.bench.hparams import (
    effective_workers,
    eval_notes,
    shared_eval,
    skillopt_official,
    train_hparams,
    train_notes,
    xskill_official,
)
from xskill.bench.mode import resolve_executor
from xskill.bench.skillopt_train import run_official_train
from xskill.bench.xskill_image import run_xskill_image_train
from xskill.bench.usage import UsageCollector, usage_card_label
from xskill.bench.scorer import assert_no_reward_imported
from xskill.bench.skill_io import hash_skill, sha256_file, sha256_sorted_uids
from xskill.bench.split import resolve_eval_uids, resolve_train_uids
from xskill.bench.stdio import safe_write


class BenchError(ValueError):
    pass


def resolve_harness(algo: str, harness: str) -> str:
    harness_id = HARNESS_ALIASES.get(harness)
    if harness_id is None:
        raise BenchError(f"unknown harness {harness!r}")
    if algo == "xskill" and harness_id != "claude_code_native_skills":
        raise BenchError("xskill must use --harness native_cc")
    if algo == "skillopt" and harness_id != "skillopt_claude_code_exec":
        raise BenchError("skillopt must use --harness claude_code_exec")
    if algo == "noskill" and harness_id not in {
        "claude_code_native_skills",
        "skillopt_claude_code_exec",
    }:
        raise BenchError("noskill harness must be native_cc or claude_code_exec")
    return harness_id


def check_skill_args(algo: str, phase: str, skill_dir: Path | None, skill_file: Path | None) -> None:
    if phase == "eval" and algo == "xskill" and skill_dir is None:
        raise BenchError("xskill eval requires --skill-dir")
    if phase == "eval" and algo == "skillopt" and skill_file is None:
        raise BenchError("skillopt eval requires --skill-file")
    if skill_dir and skill_file:
        raise BenchError("use only one of --skill-dir or --skill-file")
    if algo == "xskill" and skill_file is not None:
        raise BenchError("xskill expects --skill-dir, not --skill-file")
    if algo == "skillopt" and skill_dir is not None:
        raise BenchError("skillopt expects --skill-file, not --skill-dir")


def _rel_manifest(path: Path) -> str:
    try:
        return path.resolve().relative_to(Path.cwd().resolve()).as_posix()
    except ValueError:
        return str(path)


def _load_gold(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(doc, dict):
        raise BenchError("--gold-json must be an object mapping uid to gold")
    return doc


def _print(text: str, out: TextIO | None) -> None:
    safe_write(text, out)


def resolve_eval_hparams(
    benchmark: str,
    *,
    workers: int | None = None,
    timeout_s: float | None = None,
    max_tool_turns: int | None = None,
) -> tuple[int, float, int]:
    """评测超参按数据集取官方值，两个算法同一套。显式传参才覆盖。"""
    spec = shared_eval(benchmark)
    _official, capped = effective_workers(benchmark)
    return (
        int(workers) if workers else capped,
        float(timeout_s) if timeout_s else spec.item_timeout_s,
        int(max_tool_turns) if max_tool_turns else spec.max_tool_turns,
    )


def run_eval(
    *,
    benchmark: str,
    algo: str,
    harness: str,
    model: str,
    output_dir: Path,
    split_name: str = "test",
    skill_dir: Path | None = None,
    skill_file: Path | None = None,
    workers: int | None = None,
    timeout_s: float | None = None,
    max_tool_turns: int | None = None,
    seed: int = 42,
    limit: int = 0,
    executor: str | None = None,
    fake_script: Path | None = None,
    predictions: Path | None = None,
    gold_json: Path | None = None,
    run_id: str | None = None,
    manifest_path: Path | None = None,
    stream: TextIO | None = None,
    usage_mode: str = "auto",
    spend_logs: Path | None = None,
    litellm_url: str | None = None,
) -> Path:
    check_skill_args(algo, "eval", skill_dir, skill_file)
    executor = resolve_executor(executor)
    harness_id = resolve_harness(algo, harness)
    tools = tools_for(algo, harness_id)
    if algo == "skillopt" and "Skill" in tools:
        raise BenchError("skillopt tools must not include Skill")
    workers, timeout_s, max_tool_turns = resolve_eval_hparams(
        benchmark, workers=workers, timeout_s=timeout_s, max_tool_turns=max_tool_turns
    )
    manifest, uids = resolve_eval_uids(
        benchmark, split_name, manifest_path=manifest_path, limit=limit
    )
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    skill_sha = hash_skill(skill_dir=skill_dir, skill_file=skill_file) if (skill_dir or skill_file) else None
    rid = run_id or f"eval-{benchmark}-{algo}-{split_name}"
    started_at = now_iso()
    t0 = time.perf_counter()
    run_config = build_run_config(
        run_id=rid,
        phase="eval",
        algo=algo,
        benchmark=benchmark,
        split_manifest=_rel_manifest(manifest),
        split_name=split_name,
        model=model,
        harness_id=harness_id,
        skill_sha256=skill_sha,
        workers=workers,
        timeout_s=timeout_s,
        max_tool_turns=max_tool_turns,
        seed=seed,
        harness_notes=eval_notes(benchmark, algo, limit=limit),
    )
    write_json(output_dir / "run_config.json", run_config)
    for line in init_lines(
        benchmark=benchmark,
        split_name=split_name,
        n_items=len(uids),
        model=model,
        algo=algo,
        harness_id=harness_id,
        tools=tools,
        skill_sha256=skill_sha,
        workers=workers,
    ):
        _print(line, stream)

    gold = _load_gold(gold_json)
    logs_dir = output_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    run_key = None
    if executor == "live":
        litellm_url = proxy.proxy_base_url(litellm_url)
        run_key = proxy.try_create_run_key(rid)
    runner = make_executor(
        executor,
        benchmark=benchmark,
        fake_script=fake_script,
        predictions=predictions,
        gold=gold,
        algo=algo,
        model=model,
        run_id=rid,
        timeout_s=timeout_s,
        max_tool_turns=max_tool_turns,
        skill_dir=skill_dir,
        skill_file=skill_file,
        logs_dir=logs_dir,
        tools=tools,
        litellm_url=litellm_url,
        litellm_key=None if run_key is None else run_key.key,
        workspace_root=output_dir / "workspaces",
    )
    collector = UsageCollector(
        run_id=rid,
        model=model,
        logs_dir=logs_dir,
        proxy_url=litellm_url,
        spend_logs_path=spend_logs,
        mode=usage_mode,
        run_token=None if run_key is None else run_key.token,
    )

    def _one(index: int, uid: str) -> dict[str, Any]:
        log_file = logs_dir / f"{uid}.log"
        try:
            outcome = runner.run_item(uid, index)
        except Exception as exc:
            # 单题异常不能把整池 134 关的 results.jsonl 一起带走。
            log_file.write_text(
                f"uid={uid} status=fail runner-error={type(exc).__name__}: {exc}\n",
                encoding="utf-8",
            )
            return {
                "schema_version": 1,
                "run_id": rid,
                "uid": uid,
                "summary_text": f"runner-error: {type(exc).__name__}: {exc}",
                "gold_answer": gold.get(uid),
                "pred_answer": "",
                "status": "fail",
                "is_correct": False,
                "latency_ms": 0.0,
                "attempts": 1,
                "log_file": str(log_file),
                "skill_sha256": skill_sha,
            }
        log_file.write_text(
            f"uid={uid} status={outcome.status} pred={outcome.pred_answer!r}\n",
            encoding="utf-8",
        )
        return {
            "schema_version": 1,
            "run_id": rid,
            "uid": uid,
            "summary_text": outcome.summary_text,
            "gold_answer": outcome.gold_answer,
            "pred_answer": outcome.pred_answer,
            "status": outcome.status,
            "is_correct": outcome.is_correct,
            "latency_ms": round(outcome.latency_ms, 3),
            "attempts": 1,
            "log_file": str(log_file),
            "skill_sha256": skill_sha,
        }

    if workers > 1 and len(uids) > 1:
        # 官方评测就是线程池并发，两个算法用同一个 workers。
        with ThreadPoolExecutor(max_workers=workers) as pool:
            rows = list(pool.map(lambda pair: _one(*pair), list(enumerate(uids))))
    else:
        rows = [_one(index, uid) for index, uid in enumerate(uids)]
    # 账本异步落库，等所有题跑完再逐题对账，比边跑边查稳。
    collector.prime([str(row["uid"]) for row in rows])
    for row in rows:
        row["usage"] = collector.for_item(str(row["uid"]))
    write_jsonl(output_dir / "results.jsonl", rows)
    finished_at = now_iso()
    summary = build_summary(
        run_id=rid,
        benchmark=benchmark,
        split_name=split_name,
        model=model,
        rows=rows,
        run_config_path=output_dir / "run_config.json",
        results_path=output_dir / "results.jsonl",
        manifest_path=manifest,
        skill_sha256=skill_sha,
        started_at=started_at,
        finished_at=finished_at,
    )
    write_json(output_dir / "summary.json", summary)
    if benchmark == "officeqa":
        assert_no_reward_imported()
    errors = validate_output_dir(output_dir)
    if errors:
        raise BenchError("eval artifacts failed contract:\n" + "\n".join(errors))
    usage_label = usage_card_label(rows)
    _print(
        eval_card(
            benchmark=benchmark,
            split_name=split_name,
            n_total=summary["n_total"],
            counts=summary["status_counts"],
            usage=summary["usage_totals"],
            output_dir=str(output_dir),
            elapsed_s=time.perf_counter() - t0,
            usage_label=usage_label,
        ),
        stream,
    )
    return output_dir


def _write_stub_skill(skill_root: Path, *, benchmark: str, algo: str) -> Path:
    skill_root.mkdir(parents=True, exist_ok=True)
    if algo == "skillopt":
        path = skill_root / "SKILL.md"
        path.write_text(
            f"# Frozen {benchmark} skill ({algo})\n\nProduced by xskill bench train.\n",
            encoding="utf-8",
        )
        return path
    pkg = skill_root / f"{benchmark}-expert"
    pkg.mkdir(parents=True, exist_ok=True)
    path = pkg / "SKILL.md"
    path.write_text(
        "---\n"
        f"name: {benchmark}-expert\n"
        f"description: Frozen {benchmark} skill produced by xskill bench train.\n"
        "---\n\n"
        "Use Read and Bash. Do not invent Write or Edit tools.\n",
        encoding="utf-8",
    )
    return skill_root


def _train_split_dir(benchmark: str, output_dir: Path, uids: list[str], limit: int) -> Path:
    """官方训练读 split 目录。--limit 时写一份副本，绝不改官方 split。"""
    from xskill.bench.dataset import split_root
    from xskill.bench.xskill_image import write_limit_overlay

    official = split_root(benchmark)
    if limit <= 0:
        return official
    overlay = Path(output_dir) / "_split_overlay"
    write_limit_overlay(official, overlay, max(len(uids), 1))
    return overlay


def run_train(
    *,
    benchmark: str,
    algo: str,
    harness: str,
    model: str,
    output_dir: Path,
    skill_dir: Path | None = None,
    skill_file: Path | None = None,
    workers: int | None = None,
    timeout_s: float | None = None,
    max_tool_turns: int | None = None,
    seed: int = 42,
    limit: int = 0,
    executor: str | None = None,
    run_id: str | None = None,
    manifest_path: Path | None = None,
    stream: TextIO | None = None,
    litellm_url: str | None = None,
) -> Path:
    if algo == "noskill":
        raise BenchError("noskill has no train phase; use eval")
    executor = resolve_executor(executor)
    check_skill_args(algo, "train", skill_dir, skill_file)
    harness_id = resolve_harness(algo, harness)
    manifest, uids, split_name, merge_val = resolve_train_uids(
        benchmark, algo, manifest_path=manifest_path, limit=limit
    )
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    rid = run_id or f"train-{benchmark}-{algo}"
    t0 = time.perf_counter()
    train_workers, train_timeout_s, train_turns = train_hparams(
        benchmark,
        algo,
        workers=workers,
        timeout_s=timeout_s,
        max_tool_turns=max_tool_turns,
        limit=limit if executor == "live" else 0,
    )
    seed_sha = hash_skill(skill_dir=skill_dir, skill_file=skill_file) if (skill_dir or skill_file) else None
    run_config = build_run_config(
        run_id=rid,
        phase="train",
        algo=algo,
        benchmark=benchmark,
        split_manifest=_rel_manifest(manifest),
        split_name=split_name,
        model=model,
        harness_id=harness_id,
        skill_sha256=seed_sha,
        workers=train_workers,
        timeout_s=train_timeout_s,
        max_tool_turns=train_turns,
        seed=seed,
        harness_notes=train_notes(benchmark, algo, limit=limit),
    )
    write_json(output_dir / "run_config.json", run_config)
    tools = tools_for(algo, harness_id)
    for line in init_lines(
        benchmark=benchmark,
        split_name=split_name,
        n_items=len(uids),
        model=model,
        algo=algo,
        harness_id=harness_id,
        tools=list(tools),
        skill_sha256=seed_sha,
        workers=train_workers,
        skill_state="training",
    ):
        _print(line, stream)

    skill_out = output_dir / "skill"
    if executor == "live" and algo == "xskill":
        skill_out.mkdir(parents=True, exist_ok=True)
    elif skill_dir:
        shutil.copytree(skill_dir, skill_out, dirs_exist_ok=True)
    elif skill_file:
        skill_out.mkdir(parents=True, exist_ok=True)
        shutil.copy2(skill_file, skill_out / "SKILL.md")
    else:
        _write_stub_skill(skill_out, benchmark=benchmark, algo=algo)

    live_result = None
    run_key = None
    xskill_hyper = xskill_official(benchmark)
    if executor == "live":
        litellm_url = proxy.proxy_base_url(litellm_url)
        # 训练阶段的客户端多半加不了请求头（SkillOpt 官方 Chat 客户端写死了
        # default_headers；训练镜像里的 Claude Code 是 2.1.178），只能按 run 建虚拟
        # key 归账。
        run_key = proxy.try_create_run_key(rid)
        proxy_key = run_key.key if run_key else proxy.master_key()
        if algo == "xskill":
            image_result = run_xskill_image_train(
                benchmark=benchmark,
                model=model,
                run_id=rid,
                uids=uids,
                skill_out=skill_out,
                output_dir=output_dir,
                limit=limit,
                proxy_url=litellm_url,
                proxy_key=proxy_key,
                stream=stream,
            )
            frozen_sha = image_result.frozen_sha
            log_file = Path(image_result.checkpoints[0]["log_file"])
            ckpt_dir = Path(image_result.checkpoints[0]["checkpoint_dir"])
            xskill_hyper = image_result.hyperparameters
        else:
            live_result = run_official_train(
                benchmark=benchmark,
                model=model,
                run_id=rid,
                skill_out=skill_out,
                output_dir=output_dir,
                proxy_url=litellm_url,
                proxy_key=proxy_key,
                split_dir=_train_split_dir(benchmark, output_dir, uids, limit),
                train_size=len(uids) if limit else None,
                # 给了种子就让官方训练真从它起步；不给就用 vendor 自带的 initial.md。
                skill_init=(skill_out / "SKILL.md") if skill_file else None,
                stream=stream,
            )
            if int(live_result.returncode) != 0:
                raise BenchError(
                    f"SkillOpt official train exited {live_result.returncode}"
                )
            frozen_sha = live_result.frozen_sha
            log_file = Path(live_result.checkpoints[-1]["log_file"])
            ckpt_dir = Path(live_result.checkpoints[-1]["checkpoint_dir"])
    else:
        ckpt_dir = output_dir / "checkpoints" / "step_1"
        log_dir = output_dir / "logs"
        ckpt_dir.mkdir(parents=True, exist_ok=True)
        log_dir.mkdir(parents=True, exist_ok=True)
        if skill_out.is_dir():
            shutil.copytree(skill_out, ckpt_dir / "skill", dirs_exist_ok=True)
        log_file = log_dir / "step_1.log"
        log_file.write_text(
            f"fake train {algo} {benchmark} items={len(uids)} merge_val={merge_val}\n",
            encoding="utf-8",
        )
        if algo == "skillopt":
            frozen_sha = sha256_file(skill_out / "SKILL.md")
        else:
            frozen_sha = hash_skill(skill_dir=skill_out)
            if frozen_sha is None:
                raise BenchError("failed to hash frozen skill")

    if algo == "skillopt":
        full = json.loads(manifest.read_text(encoding="utf-8"))
        uid_hash = sha256_sorted_uids(list(full["train"]) if not limit else uids)
        val_score = 0.0 if live_result is None else live_result.val_score
        if val_score is None:
            val_score = 0.0
        action = "accept" if live_result is None else live_result.action
        if live_result is None:
            spec = skillopt_official(benchmark)
            hyper = {
                "source": spec["config"],
                "runtime": "fake",
                "num_epochs": spec["train"]["num_epochs"],
                "batch_size": spec["train"]["batch_size"],
                "minibatch_size": spec["train"]["minibatch_size"],
                "skill_update_mode": spec["train"]["skill_update_mode"],
                "use_gate": spec["train"]["use_gate"],
                "seed": spec["train"]["seed"],
                "rollout_workers": train_workers,
            }
            checkpoints = [
                {
                    "round": 1,
                    "skill_sha256": frozen_sha,
                    "checkpoint_dir": str(ckpt_dir),
                    "action": action,
                    "log_file": str(log_file),
                    "val_score": float(val_score),
                }
            ]
        else:
            hyper = live_result.hyperparameters
            checkpoints = live_result.checkpoints
        provenance = {
            "schema_version": 1,
            "run_id": rid,
            "algo": algo,
            "benchmark": benchmark,
            "train_uids_sha256": uid_hash,
            "merge_val_into_train": False,
            "selection_split": "val",
            "optimizer_backend": "openai_chat",
            "target_harness": "skillopt_claude_code_exec",
            "hyperparameters": hyper,
            "intermediate_checkpoints": checkpoints,
            "skill_sha256": frozen_sha,
        }
    else:
        if limit:
            uid_hash = sha256_sorted_uids(uids)
        else:
            full = json.loads(manifest.read_text(encoding="utf-8"))
            uid_hash = sha256_sorted_uids(list(full["train"]) + list(full["val"]))
        action = "merged_to_main" if live_result is None else live_result.action
        provenance = {
            "schema_version": 1,
            "run_id": rid,
            "algo": algo,
            "benchmark": benchmark,
            "train_uids_sha256": uid_hash,
            "merge_val_into_train": True,
            "selection_split": None,
            "target_harness": "claude_code_native_skills",
            "algo_components": {
                "splitter": "traj2skill_window_v2",
                "atom_distiller": "compact_atom_compiler",
                "canary_router": "hard_canary_v2",
                "simulated_user_workers": int(xskill_hyper.get("workers") or train_workers),
            },
            "hyperparameters": xskill_hyper,
            "intermediate_checkpoints": [
                {
                    "step": 1,
                    "skill_sha256": frozen_sha,
                    "checkpoint_dir": str(ckpt_dir),
                    "action": action,
                    "log_file": str(log_file),
                }
            ],
            "skill_sha256": frozen_sha,
        }
    write_json(output_dir / "train_provenance.json", provenance)
    validate = load_validate()
    errors = validate.validate_train_provenance(
        json.loads((output_dir / "train_provenance.json").read_text(encoding="utf-8"))
    )
    errors.extend(validate.validate_run_config(run_config))
    if errors:
        raise BenchError("train artifacts failed contract:\n" + "\n".join(errors))
    _print(
        train_card(
            benchmark=benchmark,
            split_name=split_name,
            n_items=len(uids),
            merge_val=merge_val,
            skill_sha256=frozen_sha,
            output_dir=str(output_dir),
            elapsed_s=time.perf_counter() - t0,
            n_checkpoints=len(provenance["intermediate_checkpoints"]),
        ),
        stream,
    )
    if executor == "live":
        for line in _train_usage_lines(
            output_dir=output_dir,
            run_id=rid,
            model=model,
            litellm_url=litellm_url,
            run_key=run_key,
        ):
            _print(line, stream)
    return output_dir


def _train_usage_lines(
    *,
    output_dir: Path,
    run_id: str,
    model: str,
    litellm_url: str | None,
    run_key: proxy.RunKey | None,
) -> list[str]:
    """训练阶段只能按 run 合计，逐题分不开。provenance schema 不许加字段，写旁路文件。"""
    totals = UsageCollector(
        run_id=run_id,
        model=model,
        proxy_url=litellm_url,
        mode="litellm",
        run_token=run_key.token if run_key else "",
    ).run_totals()
    doc = {
        "run_id": run_id,
        "model": model,
        "granularity": "run",
        "key_alias": run_key.alias if run_key else None,
        "usage": totals,
    }
    write_json(output_dir / "train_usage.json", doc)
    if totals.get("source") != "litellm":
        return [f"Train usage: {totals.get('source')} (per-run only; see train_usage.json)"]
    cost = totals.get("cost_usd")
    cost_text = "n/a" if cost is None else f"${float(cost):.2f} USD"
    return [
        f"Train usage: {int(totals.get('total_tokens') or 0):,} tokens, "
        f"{cost_text} (via LiteLLM proxy, per-run)"
    ]


def run_pipeline(
    *,
    benchmark: str,
    algo: str,
    harness: str,
    model: str,
    output_dir: Path,
    skill_dir: Path | None = None,
    skill_file: Path | None = None,
    workers: int | None = None,
    timeout_s: float | None = None,
    max_tool_turns: int | None = None,
    seed: int = 42,
    limit: int = 0,
    executor: str | None = None,
    fake_script: Path | None = None,
    predictions: Path | None = None,
    gold_json: Path | None = None,
    run_id: str | None = None,
    manifest_path: Path | None = None,
    stream: TextIO | None = None,
    usage_mode: str = "auto",
    spend_logs: Path | None = None,
    litellm_url: str | None = None,
) -> Path:
    output_dir = Path(output_dir)
    train_dir = output_dir / "train"
    eval_dir = output_dir / "eval"
    run_train(
        benchmark=benchmark,
        algo=algo,
        harness=harness,
        model=model,
        output_dir=train_dir,
        skill_dir=skill_dir,
        skill_file=skill_file,
        workers=workers,
        timeout_s=timeout_s,
        max_tool_turns=max_tool_turns,
        seed=seed,
        limit=limit,
        executor=executor,
        run_id=None if run_id is None else f"{run_id}-train",
        manifest_path=manifest_path,
        stream=stream,
        litellm_url=litellm_url,
    )
    eval_skill_dir = train_dir / "skill" if algo == "xskill" else None
    eval_skill_file = (train_dir / "skill" / "SKILL.md") if algo == "skillopt" else None
    run_eval(
        benchmark=benchmark,
        algo=algo,
        harness=harness,
        model=model,
        output_dir=eval_dir,
        split_name="test",
        skill_dir=eval_skill_dir,
        skill_file=eval_skill_file,
        workers=workers,
        timeout_s=timeout_s,
        max_tool_turns=max_tool_turns,
        seed=seed,
        limit=limit,
        executor=executor,
        fake_script=fake_script,
        predictions=predictions,
        gold_json=gold_json,
        run_id=None if run_id is None else f"{run_id}-eval",
        manifest_path=manifest_path,
        stream=stream,
        usage_mode=usage_mode,
        spend_logs=spend_logs,
        litellm_url=litellm_url,
    )
    return output_dir
