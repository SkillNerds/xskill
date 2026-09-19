#!/usr/bin/env python3.11
"""SpreadsheetBench 训练或评测入口。"""
from __future__ import annotations

import sys

from xskill.bench.cli import main


if __name__ == "__main__":
    raise SystemExit(main(["bench", *sys.argv[1:]]))
