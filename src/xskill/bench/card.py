from __future__ import annotations

from typing import Any

from xskill.bench.constants import BENCHMARK_TITLES, HARNESS_DISPLAY
from xskill.bench.skill_io import short_sha


def _fmt_int(n: int) -> str:
    return f"{int(n):,}"


def _fmt_hms(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def progress_line(
    done: int,
    total: int,
    elapsed_s: float,
    *,
    label: str = "Evaluating",
) -> str:
    width = 30
    frac = 1.0 if total <= 0 else done / total
    filled = int(width * frac)
    bar = "█" * filled + "░" * (width - filled)
    rate = (elapsed_s / done) if done else 0.0
    remain = rate * (total - done) if done else 0.0
    pct = 100 if total <= 0 else int(100 * frac)
    return (
        f"{label}: {pct:3d}%|{bar}| {done}/{total} "
        f"[{_fmt_hms(elapsed_s)}<{_fmt_hms(remain)}, {rate:.2f}s/it]"
    )


def init_lines(
    *,
    benchmark: str,
    split_name: str,
    n_items: int,
    model: str,
    algo: str,
    harness_id: str,
    tools: list[str],
    skill_sha256: str | None,
    workers: int,
    skill_state: str = "frozen",
) -> list[str]:
    title = BENCHMARK_TITLES.get(benchmark, benchmark)
    harness = HARNESS_DISPLAY.get(harness_id, harness_id)
    tool_text = ", ".join(tools)
    return [
        f"[xskill-bench] Initializing benchmark: {title} (Split: {split_name}, {n_items} items)",
        f"[xskill-bench] Model: {model} | Algo: {algo} | Harness: {harness} (Tools: {tool_text})",
        f"[xskill-bench] Skill SHA-256: {short_sha(skill_sha256)} ({skill_state}) | Workers: {workers}",
    ]


def eval_card(
    *,
    benchmark: str,
    split_name: str,
    n_total: int,
    counts: dict[str, int],
    usage: dict[str, Any],
    output_dir: str,
    elapsed_s: float,
    usage_label: str,
) -> str:
    title = BENCHMARK_TITLES.get(benchmark, benchmark)
    n_pass = counts.get("pass", 0)
    n_fail = counts.get("fail", 0)
    n_timeout = counts.get("timeout", 0)
    accuracy = (n_pass / n_total) if n_total else 0.0
    prompt = int(usage.get("input_tokens") or 0)
    completion = int(usage.get("output_tokens") or 0)
    total_tokens = int(usage.get("total_tokens") or 0)
    spend = float(usage.get("cost_usd") or 0.0)
    lines = [
        progress_line(n_total, n_total, elapsed_s, label="Evaluating"),
        f"  ├── pass: {n_pass}",
        f"  ├── fail: {n_fail}",
        f"  └── timeout: {n_timeout} (counted as unpassed)",
        "",
        "--------------------------------------------------------------------------------",
        f"Benchmark Results: {title} ({split_name})",
        "--------------------------------------------------------------------------------",
        f"Overall Accuracy: {accuracy * 100:.2f}% ({n_pass}/{n_total})",
        f"  ├── Passed Items : {n_pass}",
        f"  ├── Failed Items : {n_fail}",
        f"  └── Timeout Items: {n_timeout} (counted as unpassed)",
        "",
        "Usage & Cost:",
        f"  ├── Total Tokens : {_fmt_int(total_tokens)} (Prompt: {_fmt_int(prompt)} | Completion: {_fmt_int(completion)})",
        f"  └── Total Spend  : ${spend:.2f} USD ({usage_label})",
        "",
        "Artifacts saved to:",
        f"  ├── Config  : {output_dir}/run_config.json",
        f"  ├── Details : {output_dir}/results.jsonl",
        f"  └── Summary : {output_dir}/summary.json",
        "--------------------------------------------------------------------------------",
    ]
    return "\n".join(lines)


def train_card(
    *,
    benchmark: str,
    split_name: str,
    n_items: int,
    merge_val: bool,
    skill_sha256: str | None,
    output_dir: str,
    elapsed_s: float,
    n_checkpoints: int,
) -> str:
    title = BENCHMARK_TITLES.get(benchmark, benchmark)
    merged = "yes" if merge_val else "no"
    lines = [
        progress_line(n_items, n_items, elapsed_s, label="Training"),
        f"  ├── checkpoints: {n_checkpoints}",
        f"  └── frozen skill: {short_sha(skill_sha256)}",
        "",
        "--------------------------------------------------------------------------------",
        f"Train Complete: {title} ({split_name})",
        "--------------------------------------------------------------------------------",
        f"Train items: {n_items}",
        f"Merged val into train: {merged}",
        "",
        "Artifacts saved to:",
        f"  ├── Config     : {output_dir}/run_config.json",
        f"  ├── Provenance : {output_dir}/train_provenance.json",
        f"  └── Skill      : {output_dir}/skill",
        "--------------------------------------------------------------------------------",
    ]
    return "\n".join(lines)
