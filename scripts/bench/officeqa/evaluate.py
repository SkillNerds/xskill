#!/usr/bin/env python3.11
"""校验并回放一个 OfficeQA 评测目录的合同文件。不调模型。"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from xskill.bench.artifacts import load_validate
from xskill.bench.card import eval_card
from xskill.bench.scorer import assert_no_reward_imported
from xskill.bench.usage import usage_card_label


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate an OfficeQA bench run directory")
    parser.add_argument("--run-dir", required=True)
    args = parser.parse_args(argv)
    run_dir = Path(args.run_dir)
    errors = load_validate().validate_example_dir(run_dir)
    if errors:
        for err in errors:
            print(err)
        return 1
    assert_no_reward_imported()
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    rows = [
        json.loads(line)
        for line in (run_dir / "results.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    print(
        eval_card(
            benchmark=summary["benchmark"],
            split_name=summary["split_name"],
            n_total=summary["n_total"],
            counts=summary["status_counts"],
            usage=summary["usage_totals"],
            output_dir=str(run_dir),
            elapsed_s=0.0,
            usage_label=usage_card_label(rows),
        )
    )
    print("officeqa_evaluate=ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
