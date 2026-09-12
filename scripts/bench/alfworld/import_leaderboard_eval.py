#!/usr/bin/env python3.11
"""ALFWorld 导入入口。默认 sub81；SkillOpt 用 sub80，no-skill 用 sub86。"""
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
        argv = ["--preset", "alfworld-sub81", *argv]
    return _impl.main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
