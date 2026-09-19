from __future__ import annotations

import argparse
from pathlib import Path

from xskill.bench.constants import ALGOS, BENCHMARKS
from xskill.bench.mode import resolve_executor
from xskill.bench.pipeline import BenchError, run_eval, run_pipeline, run_train
from xskill.bench.stdio import ignore_sigpipe, open_run_console, quiet_broken_stdout

HARNESS_CHOICES = (
    "native_cc",
    "claude_code_exec",
    "claude_code_native_skills",
    "skillopt_claude_code_exec",
)


def _add_common(parser: argparse.ArgumentParser, *, require_output: bool = True) -> None:
    parser.add_argument("--benchmark", required=True, choices=BENCHMARKS)
    parser.add_argument("--model", default="deepseek-v4-flash")
    parser.add_argument("--algo", required=True, choices=ALGOS)
    parser.add_argument("--harness", required=True, choices=HARNESS_CHOICES)
    parser.add_argument("--output-dir", required=require_output)
    parser.add_argument("--skill-dir")
    parser.add_argument("--skill-file")
    # 不传就按数据集取官方值（评测两算法同值，训练各走各家官方）。
    # 传了就是手动覆盖，会写进 run_config。
    parser.add_argument("--workers", type=int, default=None)
    parser.add_argument("--timeout-s", type=float, default=None)
    parser.add_argument("--max-tool-turns", type=int, default=None)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--limit", type=int, default=0, help="only the first N items (wiring / smoke)")
    parser.add_argument("--run-id")
    parser.add_argument("--split-manifest")
    parser.add_argument(
        "--usage-mode",
        choices=["auto", "litellm", "local_fallback", "missing"],
        default="auto",
    )
    parser.add_argument("--spend-logs")
    parser.add_argument("--litellm-url")


def add_bench_parser(sub: argparse._SubParsersAction) -> argparse.ArgumentParser:
    parser = sub.add_parser("bench", help="精度评测：train、eval 或一键 run")
    bench = parser.add_subparsers(dest="bench_action", required=True)

    p_eval = bench.add_parser("eval", help="用冻结 skill 评测一个 split")
    _add_common(p_eval)
    p_eval.add_argument("--split", default="test", choices=["train", "val", "test"])

    p_train = bench.add_parser("train", help="只训练并写出 train_provenance.json")
    _add_common(p_train)
    p_train.add_argument("--split", default="train", choices=["train", "val", "train_plus_val"])

    p_run = bench.add_parser("run", help="先训练再评测测试集")
    _add_common(p_run)
    return parser


def _kwargs(args: argparse.Namespace) -> dict:
    return {
        "benchmark": args.benchmark,
        "algo": args.algo,
        "harness": args.harness,
        "model": args.model,
        "output_dir": Path(args.output_dir),
        "skill_dir": Path(args.skill_dir) if args.skill_dir else None,
        "skill_file": Path(args.skill_file) if args.skill_file else None,
        "workers": args.workers,
        "timeout_s": args.timeout_s,
        "max_tool_turns": args.max_tool_turns,
        "seed": args.seed,
        "limit": args.limit,
        "executor": resolve_executor(),
        "fake_script": None,
        "predictions": None,
        "gold_json": None,
        "run_id": args.run_id,
        "manifest_path": Path(args.split_manifest) if getattr(args, "split_manifest", None) else None,
        "usage_mode": getattr(args, "usage_mode", "auto"),
        "spend_logs": Path(args.spend_logs) if getattr(args, "spend_logs", None) else None,
        "litellm_url": getattr(args, "litellm_url", None),
    }


def dispatch_bench(args: argparse.Namespace) -> int:
    ignore_sigpipe()
    log_fp = None
    try:
        action = getattr(args, "bench_action", None)
        kw = _kwargs(args)
        tee, log_fp = open_run_console(kw["output_dir"])
        kw["stream"] = tee
        if action == "eval":
            run_eval(split_name=args.split, **kw)
            return 0
        if action == "train":
            for key in ("fake_script", "predictions", "gold_json", "usage_mode", "spend_logs"):
                kw.pop(key, None)
            run_train(**kw)
            return 0
        if action == "run":
            run_pipeline(**kw)
            return 0
        raise BenchError("bench 需要 train、eval 或 run")
    except BenchError as exc:
        try:
            print(f"error: {exc}", flush=True)
        except BrokenPipeError:
            pass
        if log_fp is not None:
            log_fp.write(f"error: {exc}\n")
            log_fp.flush()
        return 2
    finally:
        if log_fp is not None:
            log_fp.close()
        quiet_broken_stdout()


def build_standalone_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="xskill bench")
    sub = parser.add_subparsers(dest="command")
    add_bench_parser(sub)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="xskill-bench")
    sub = parser.add_subparsers(dest="command")
    add_bench_parser(sub)
    args = parser.parse_args(argv)
    if args.command != "bench":
        parser.print_help()
        return 1
    return dispatch_bench(args)
