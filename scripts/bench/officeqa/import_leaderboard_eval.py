#!/usr/bin/env python3.11
"""OfficeQA 导入入口。实现在 scripts/bench/contract_import.py。"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
BENCH_SCRIPTS = HERE.parent
if str(BENCH_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(BENCH_SCRIPTS))

import contract_import as _impl

classify_status = _impl.classify_status
validate_imported_run = _impl.validate_imported_run
write_run = _impl.write_run
convert_rows = _impl.convert_rows
sha256_file = _impl.sha256_file
FAILURE_POLICY = _impl.FAILURE_POLICY
OFFICEQA_SCORER = _impl.OFFICEQA_SCORER
HISTORICAL_TOOLS = ["Read", "Write", "Edit", "Bash", "Skill"]


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--preset" not in argv:
        argv = ["--preset", "officeqa-sub200", *argv]
    return _impl.main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
