#!/usr/bin/env python3
"""检查共享 LiteLLM 代理是否满足 bench 的两个算法都走代理的要求。

需要的路由：

- ``deepseek-v4-flash``：两边做题的 Claude Code（走 /v1/messages）、SkillOpt 官方
  反思 LLM、xskill 镜像内部的蒸馏与 SkillEdit（走 /v1/chat/completions）。
- ``text-embedding-v4``：xskill 的 atom 入库与向量检索（走 /v1/embeddings）。
  DeepSeek 没有 embeddings 接口，官方镜像用的是 DashScope 这个模型。

只读检查默认执行；带 ``--ensure`` 时缺的路由会通过 ``/model/new`` 补上（代理开了
store_model_in_db，不用重启）。补路由需要 DashScope 的 key，从 ~/.aikey 读，不回显。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from xskill.bench import proxy  # noqa: E402

REQUIRED_CHAT = "deepseek-v4-flash"
REQUIRED_EMBEDDING = "text-embedding-v4"
DASHSCOPE_BASE = "https://dashscope.aliyuncs.com/compatible-mode/v1"
AIKEY = Path("/home/admin/.aikey")


def _aikey(name: str) -> str:
    if not AIKEY.is_file():
        return ""
    for line in AIKEY.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith(f"{name}="):
            return line.split("=", 1)[1].strip().strip("'").strip('"')
    return ""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url")
    parser.add_argument("--ensure", action="store_true", help="缺的路由直接补上")
    args = parser.parse_args()

    base = proxy.proxy_base_url(args.base_url)
    if not proxy.alive(base):
        print(f"FAIL 代理不在: {base}")
        return 1
    if not proxy.master_key():
        print(f"FAIL 读不到 LITELLM_MASTER_KEY（环境变量或 {proxy.env_file()}）")
        return 1

    names = set(proxy.model_names(base_url=base))
    print(f"proxy={base} models={sorted(names)}")
    missing = [name for name in (REQUIRED_CHAT, REQUIRED_EMBEDDING) if name not in names]
    if not missing:
        print("OK 两个算法需要的路由都在")
        return 0
    if not args.ensure:
        print(f"FAIL 缺路由 {missing}；加 --ensure 补上")
        return 1

    if REQUIRED_CHAT in missing:
        print(f"FAIL {REQUIRED_CHAT} 要在 config.yaml 里配，不在这里补")
        return 1
    key = _aikey("DASHSCOPE_API_KEY")
    if not key:
        print("FAIL ~/.aikey 里没有 DASHSCOPE_API_KEY")
        return 1
    doc = proxy._request(
        "/model/new",
        base_url=base,
        payload={
            "model_name": REQUIRED_EMBEDDING,
            "litellm_params": {
                "model": f"openai/{REQUIRED_EMBEDDING}",
                "api_base": DASHSCOPE_BASE,
                "api_key": key,
            },
            "model_info": {"mode": "embedding"},
        },
    )
    print(f"added {REQUIRED_EMBEDDING}: {json.dumps(doc, ensure_ascii=False)[:120]}")
    names = set(proxy.model_names(base_url=base))
    if REQUIRED_EMBEDDING not in names:
        print("FAIL 补完还是查不到")
        return 1
    print("OK 路由已补齐")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
