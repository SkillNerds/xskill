#!/usr/bin/env python3.11
"""build 时拉取最新模型价格,vendor 成 src/xskill/data/model_prices.json。

数据源 = LiteLLM 社区维护的价格表(只取数据,**不引 litellm 包**,见 ADR-0001)。
拉取/解析逻辑收口在 ``xskill.prices``(运行时后台刷新复用同一份,避免熵增)。
任何失败(网络错 / 源不存在 / 解析失败 / 条目过少)→ **退出码 1,构建报错**。

用法(发版前):
    python3.11 scripts/fetch_model_prices.py
    # 然后 python3.11 -m build / twine upload
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from xskill import prices  # noqa: E402  pylint: disable=wrong-import-position

OUT = Path(__file__).resolve().parents[1] / "src" / "xskill" / "data" / "model_prices.json"


def main() -> int:
    try:
        payload = prices.fetch_and_build()
    except Exception as e:  # pylint: disable=broad-exception-caught
        print(f"FATAL: 拉取/解析价格表失败: {e}\n  源: {prices.PRICES_URL}", file=sys.stderr)
        return 1

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=0), encoding="utf-8")
    n = len(payload) - 1  # 减去 _meta
    print(f"OK: wrote {OUT}  ({n} models)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
