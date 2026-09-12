#!/usr/bin/env python3.11
"""OfficeQA 训练或评测入口。不覆盖旁边的导入脚本。"""
from __future__ import annotations

import sys

from xskill.bench.cli import main


def _main(argv: list[str] | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    return main(["bench", *args])


if __name__ == "__main__":
    raise SystemExit(_main())
