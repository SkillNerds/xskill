"""从 LiteLLM 代理的用量日志中提取 token 消耗与费用统计。

本模块仅负责用量回填，不参与题目判定与评分。
"""

from __future__ import annotations

import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


def aggregate_spend_logs_by_uid(
    logs: list[dict[str, Any]],
    *,
    run_id: str | None = None,
) -> dict[str, dict[str, Any]]:
    """按 metadata.uid 聚合 LiteLLM 的费用与 token 消耗日志。

    日志中支持识别的字段包括：
    - metadata.run_id 与 metadata.uid（或 spend_logs_metadata）
    - prompt_tokens / completion_tokens / total_tokens
    - spend（美元费用）
    - request_id
    """
    by_uid: dict[str, dict[str, Any]] = {}
    for row in logs:
        meta = _metadata(row)
        if run_id is not None and str(meta.get("run_id") or "") != run_id:
            continue
        uid = str(meta.get("uid") or "").strip()
        if not uid:
            continue
        bucket = by_uid.setdefault(
            uid,
            {
                "source": "litellm",
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_read_tokens": 0,
                "total_tokens": 0,
                "cost_usd": 0.0,
                "request_ids": [],
            },
        )
        prompt = int(row.get("prompt_tokens") or row.get("input_tokens") or 0)
        completion = int(row.get("completion_tokens") or row.get("output_tokens") or 0)
        cache_read = int(row.get("cache_read_input_tokens") or row.get("cache_read_tokens") or 0)
        total = int(row.get("total_tokens") or (prompt + completion))
        spend = float(row.get("spend") or row.get("cost") or 0.0)
        bucket["input_tokens"] += prompt
        bucket["output_tokens"] += completion
        bucket["cache_read_tokens"] += cache_read
        bucket["total_tokens"] += total
        bucket["cost_usd"] = float(bucket["cost_usd"]) + spend
        req_id = row.get("request_id") or row.get("id")
        if req_id:
            bucket["request_ids"].append(str(req_id))
    return by_uid


def merge_usage_into_results(
    results: list[dict[str, Any]],
    usage_by_uid: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """将按 uid 汇总的用量信息回填到逐题结果列表中（浅拷贝每一行）。"""
    out: list[dict[str, Any]] = []
    for row in results:
        item = dict(row)
        uid = str(item.get("uid") or "")
        usage = usage_by_uid.get(uid)
        if usage is None:
            current = dict(item.get("usage") or {})
            current.setdefault("source", "missing")
            item["usage"] = current
        else:
            item["usage"] = dict(usage)
        out.append(item)
    return out


def fetch_spend_logs(
    *,
    base_url: str,
    api_key: str,
    start_date: str,
    end_date: str,
    timeout_s: float = 60.0,
) -> list[dict[str, Any]]:
    """从 LiteLLM 代理接口获取用量日志（/spend/logs?summarize=false）。

    api_key 从环境变量传入，请勿写进代码或提交到仓库。
    """
    query = urlencode(
        {
            "start_date": start_date,
            "end_date": end_date,
            "summarize": "false",
        }
    )
    url = base_url.rstrip("/") + "/spend/logs?" + query
    req = Request(url, headers={"Authorization": f"Bearer {api_key}"})
    try:
        with urlopen(req, timeout=timeout_s) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"LiteLLM spend log fetch failed: {exc}") from exc
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("data", "logs", "results"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
    raise RuntimeError("LiteLLM spend log response was not a list of rows")


def _metadata(row: dict[str, Any]) -> dict[str, Any]:
    meta = row.get("metadata")
    if not isinstance(meta, dict):
        meta = {}
    nested = meta.get("spend_logs_metadata")
    if isinstance(nested, dict):
        merged = dict(nested)
        merged.update({k: v for k, v in meta.items() if k != "spend_logs_metadata"})
        return merged
    return meta


def load_jsonl(path: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    return rows


def write_jsonl(path: str, rows: list[dict[str, Any]]) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
