#!/usr/bin/env python3
"""Inspect XSkill kernel discovery without executing the algorithm."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--kernel", required=True, dest="kernel_id")
    parser.add_argument("--plugin-dir", default="~/.xskill/kernels")
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
