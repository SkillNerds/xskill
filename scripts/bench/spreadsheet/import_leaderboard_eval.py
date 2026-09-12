#!/usr/bin/env python3.11
"""Spreadsheet 导入入口。默认 sub181；SkillOpt 用 --preset spreadsheet-sub182。"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE.parent) not in sys.path:
    sys.path.insert(0, str(HERE.parent))
import contract_import as _impl


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--preset" not in argv:
        argv = ["--preset", "spreadsheet-sub181", *argv]
    return _impl.main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
