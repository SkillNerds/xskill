#!/usr/bin/env python3
"""检查 XSkill 能否发现并导入算法内核，不执行算法。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(
        description="检查算法内核的导入状态、版本、触发方式和目录",
    )
    parser.add_argument(
        "--kernel", required=True, dest="kernel_id", help="算法内核 ID",
    )
    parser.add_argument(
        "--plugin-dir",
        default="~/.xskill/kernels",
        help="算法内核根目录（默认：~/.xskill/kernels）",
    )
    args = parser.parse_args()

    from xskill.kernels.catalog import KernelCatalog

    plugin_dir = Path(args.plugin_dir).expanduser().resolve()
    xskill_home = Path.home() / ".xskill"
    catalog = KernelCatalog(plugin_dir=plugin_dir, xskill_home=xskill_home)
    descriptor = catalog.get(args.kernel_id)
    print(json.dumps(descriptor.as_dict(), ensure_ascii=False, indent=2))
    return 0 if descriptor.available else 2


if __name__ == "__main__":
    raise SystemExit(main())
