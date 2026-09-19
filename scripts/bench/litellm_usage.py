#!/usr/bin/env python3.11
"""从 LiteLLM 花费日志或本机旁路文件填回评测用量。不 import litellm。"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from xskill.bench.usage import (  # noqa: E402
    claude_custom_headers_value,
    collect_item_usage,
    contract_usage,
    fetch_spend_logs,
    spend_row_matches,
    tagging_headers,
    usage_from_spend_rows,
    write_usage_sidecar,
)


def _load_dotenv(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip().strip("'").strip('"')
    return out


def _apply_proxy_env(env_file: str | None) -> None:
    path = Path(env_file) if env_file else Path("/home/admin/litellm-proxy/.env")
    loaded = _load_dotenv(path)
    if loaded.get("LITELLM_MASTER_KEY") and not os.environ.get("LITELLM_MASTER_KEY"):
        os.environ["LITELLM_MASTER_KEY"] = loaded["LITELLM_MASTER_KEY"]
    if loaded.get("DEEPSEEK_API_KEY") and not os.environ.get("DEEPSEEK_API_KEY"):
        os.environ["DEEPSEEK_API_KEY"] = loaded["DEEPSEEK_API_KEY"]


def cmd_collect(args: argparse.Namespace) -> int:
    _apply_proxy_env(args.env_file)
    usage = collect_item_usage(
        run_id=args.run_id,
        uid=args.item_id,
        model=args.model,
        logs_dir=Path(args.logs_dir) if args.logs_dir else None,
        proxy_url=args.proxy_url,
        spend_logs_path=Path(args.spend_logs) if args.spend_logs else None,
        mode=args.mode,
    )
    print(json.dumps(usage, ensure_ascii=False, indent=2))
    return 0 if usage.get("source") != "missing" else 1


def cmd_headers(args: argparse.Namespace) -> int:
    headers = tagging_headers(args.run_id, args.item_id)
    print(json.dumps(headers, ensure_ascii=False, indent=2))
    print("--- ANTHROPIC_CUSTOM_HEADERS ---")
    print(claude_custom_headers_value(args.run_id, args.item_id))
    return 0


def _chat(url: str, key: str, payload: dict, headers: dict[str, str], timeout: float = 60.0) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json", **headers},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def cmd_probe(args: argparse.Namespace) -> int:
    _apply_proxy_env(args.env_file)
    run_id = args.run_id
    item_id = args.item_id
    model = args.model
    headers = tagging_headers(run_id, item_id)
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "Reply with exactly the two letters ok."}],
        "max_tokens": 8,
    }
    if args.via == "proxy":
        base = args.proxy_url.rstrip("/")
        key = os.environ.get("LITELLM_MASTER_KEY") or ""
        if not key:
            print("probe: missing LITELLM_MASTER_KEY", file=sys.stderr)
            return 2
        url = base + "/v1/chat/completions"
    else:
        key = os.environ.get("DEEPSEEK_API_KEY") or ""
        if not key:
            print("probe: missing DEEPSEEK_API_KEY", file=sys.stderr)
            return 2
        url = "https://api.deepseek.com/v1/chat/completions"
        headers = {}
    try:
        doc = _chat(url, key, payload, headers)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        print(f"probe: chat failed ({type(exc).__name__})", file=sys.stderr)
        return 2
    usage_raw = doc.get("usage") or {}
    print(
        json.dumps(
            {
                "via": args.via,
                "http": 200,
                "has_usage": bool(usage_raw),
                "prompt_tokens": usage_raw.get("prompt_tokens"),
                "completion_tokens": usage_raw.get("completion_tokens"),
                "total_tokens": usage_raw.get("total_tokens"),
                "request_id_present": bool(doc.get("id")),
                "headers_sent": list(tagging_headers(run_id, item_id)) if args.via == "proxy" else [],
            },
            ensure_ascii=False,
        )
    )
    if args.logs_dir:
        write_usage_sidecar(Path(args.logs_dir), item_id, doc, run_id=run_id, model=model)
    if args.via == "proxy":
        req_id = str(doc.get("id") or "")
        rows: list = []
        for _ in range(8):
            try:
                rows = fetch_spend_logs(
                    base_url=args.proxy_url, request_id=req_id or None
                )
            except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
                rows = []
            if rows:
                break
            time.sleep(1)
        tagged = [row for row in rows if spend_row_matches(row, run_id, item_id)]
        filled = usage_from_spend_rows(tagged or rows, model=model)
        print(
            json.dumps(
                {
                    "collected": filled,
                    "spend_rows": len(rows),
                    "tagged_rows": len(tagged),
                },
                ensure_ascii=False,
            )
        )
        return 0 if filled.get("source") == "litellm" and tagged else 1
    filled = contract_usage(source="local_fallback", resp=doc, model=model)
    print(json.dumps({"collected": filled}, ensure_ascii=False))
    return 0 if filled.get("source") == "local_fallback" else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Bench usage from LiteLLM spend logs or local sidecar")
    sub = parser.add_subparsers(dest="action", required=True)

    collect = sub.add_parser("collect", help="fill one item from spend logs or sidecar")
    collect.add_argument("--run-id", required=True)
    collect.add_argument("--item-id", required=True)
    collect.add_argument("--model", default="deepseek-v4-flash")
    collect.add_argument("--logs-dir")
    collect.add_argument("--spend-logs")
    collect.add_argument("--proxy-url", default="http://127.0.0.1:4000")
    collect.add_argument("--mode", default="auto", choices=["auto", "litellm", "local_fallback", "missing"])
    collect.add_argument("--env-file")
    collect.set_defaults(func=cmd_collect)

    headers = sub.add_parser("headers", help="print X-Run-Id / X-Item-Id / Claude env value")
    headers.add_argument("--run-id", required=True)
    headers.add_argument("--item-id", required=True)
    headers.set_defaults(func=cmd_headers)

    probe = sub.add_parser("probe", help="live smoke: proxy chat or direct DeepSeek")
    probe.add_argument("--via", choices=["proxy", "direct"], default="proxy")
    probe.add_argument("--run-id", default="probe-step8")
    probe.add_argument("--item-id", default="probe-item")
    probe.add_argument("--model", default="deepseek-v4-flash")
    probe.add_argument("--proxy-url", default="http://127.0.0.1:4000")
    probe.add_argument("--logs-dir")
    probe.add_argument("--env-file")
    probe.set_defaults(func=cmd_probe)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
