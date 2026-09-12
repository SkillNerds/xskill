#!/usr/bin/env python3.11
"""校验一个 ALFWorld 评测目录。"""
from __future__ import annotations

import argparse
from pathlib import Path

from xskill.bench.artifacts import load_validate


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", required=True)
    args = parser.parse_args(argv)
    errors = load_validate().validate_example_dir(Path(args.run_dir))
    if errors:
        for err in errors:
            print(err)
        return 1
    print("alfworld_evaluate=ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
