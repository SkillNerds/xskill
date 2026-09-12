from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from xskill.bench import proxy
from xskill.usage import cost_usd, extract_usage, load_price_table

# 只走 HTTP 代理和本机旁路文件，不 import litellm。

RUN_ID_HEADER = "X-Run-Id"
ITEM_ID_HEADER = "X-Item-Id"
DEFAULT_PROXY_URL = proxy.DEFAULT_PROXY_URL
SIDECAR_SUFFIX = ".usage.json"
# spend 日志是异步落库的，实测滞后几秒。逐题对账时给它一点时间，别直接判缺失。
SPEND_SETTLE_TRIES = 4
SPEND_SETTLE_SLEEP_S = 3.0


# 整趟合计里留几条 request_id 做抽样对账，其余只记笔数。
_RUN_ID_SAMPLE = 20


def missing_usage() -> dict[str, Any]:
    return {"source": "missing"}


def empty_usage_totals(*, missing_n: int) -> dict[str, Any]:
    return {
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "cost_usd": 0.0,
        "usage_missing_n": missing_n,
    }


def sum_usage(rows: list[dict[str, Any]]) -> dict[str, Any]:
    totals = empty_usage_totals(missing_n=0)
    for row in rows:
        usage = row.get("usage") or {}
        if usage.get("source") == "missing" or not usage.get("source"):
            totals["usage_missing_n"] += 1
            continue
        totals["input_tokens"] += int(usage.get("input_tokens") or 0)
        totals["output_tokens"] += int(usage.get("output_tokens") or 0)
        totals["total_tokens"] += int(usage.get("total_tokens") or 0)
        totals["cost_usd"] += float(usage.get("cost_usd") or 0.0)
    return totals


def usage_card_label(rows: list[dict[str, Any]]) -> str:
    sources = {((row.get("usage") or {}).get("source") or "missing") for row in rows}
    if sources == {"litellm"}:
        return "via LiteLLM proxy"
    if sources == {"local_fallback"}:
        return "usage.source=local_fallback"
    if not sources or sources == {"missing"}:
        return "usage.source=missing"
    return "mixed usage sources"


def tagging_headers(run_id: str, item_id: str) -> dict[str, str]:
    """Chat 与 Claude 共用的拆账头。x-litellm-tags 不依赖代理额外配置也能进 request_tags。"""
    # 训练阶段拿不到题号，这时不发空的题号头：账本里留一条 item: 只会让人以为漏了题。
    if not item_id:
        return {RUN_ID_HEADER: run_id, "x-litellm-tags": f"run:{run_id}"}
    return {
        RUN_ID_HEADER: run_id,
        ITEM_ID_HEADER: item_id,
        "x-litellm-tags": f"run:{run_id},item:{item_id}",
    }


def claude_custom_headers_value(run_id: str, item_id: str) -> str:
    """ANTHROPIC_CUSTOM_HEADERS 的一行一条格式。"""
    headers = tagging_headers(run_id, item_id)
    return "\n".join(f"{name}: {value}" for name, value in headers.items())


def sidecar_path(logs_dir: Path, uid: str) -> Path:
    return Path(logs_dir) / f"{uid}{SIDECAR_SUFFIX}"


def normalize_response(resp: Any) -> dict[str, Any]:
    """把 Chat、Anthropic、流式最后一块收成 extract_usage 认的形态。"""
    if resp is None:
        return {}
    if not isinstance(resp, dict):
        usage_obj = getattr(resp, "usage", None)
        resp_id = getattr(resp, "id", None)
        if hasattr(usage_obj, "model_dump"):
            usage_obj = usage_obj.model_dump()
        elif hasattr(usage_obj, "__dict__") and not isinstance(usage_obj, dict):
            usage_obj = dict(usage_obj.__dict__)
        resp = {"id": resp_id, "usage": usage_obj}
    if isinstance(resp.get("choices"), list) and resp.get("usage") is None:
        for chunk in reversed(resp["choices"]):
            if isinstance(chunk, dict) and chunk.get("usage"):
                resp = {**resp, "usage": chunk["usage"]}
                break
    usage = dict(resp.get("usage") or {})
    if "prompt_tokens" not in usage and "input_tokens" in usage:
        usage["prompt_tokens"] = usage["input_tokens"]
    if "completion_tokens" not in usage and "output_tokens" in usage:
        usage["completion_tokens"] = usage["output_tokens"]
    if "prompt_cache_hit_tokens" not in usage:
        for key in ("cache_read_tokens", "cache_read_input_tokens", "cache_read"):
            if key in usage:
                usage["prompt_cache_hit_tokens"] = usage[key]
                break
    out = dict(resp)
    out["usage"] = usage
    return out


def last_complete_usage_response(chunks: list[Any]) -> dict[str, Any] | None:
    """流式只认最后一块带完整 usage 的，不按字数估。"""
    last: dict[str, Any] | None = None
    for chunk in chunks:
        normalized = normalize_response(chunk)
        usage = normalized.get("usage") or {}
        if usage.get("prompt_tokens") is not None or usage.get("completion_tokens") is not None:
            last = normalized
    return last


def _price_for(model: str):
    table = load_price_table()
    price, _source = table.resolve(model)
    return price


def contract_usage(
    *,
    source: str,
    resp: Any,
    model: str,
    request_ids: list[str] | None = None,
) -> dict[str, Any]:
    normalized = normalize_response(resp)
    extracted = extract_usage(normalized)
    if extracted.measurement_quality == "unavailable" and extracted.prompt is None:
        return missing_usage()
    ids = list(request_ids or [])
    resp_id = normalized.get("id") or normalized.get("request_id")
    if resp_id and str(resp_id) not in ids:
        ids.append(str(resp_id))
    cost = cost_usd(extracted, _price_for(model))
    prompt = int(extracted.prompt or 0)
    completion = int(extracted.completion or 0)
    total = int(extracted.total if extracted.total is not None else prompt + completion)
    return {
        "source": source,
        "input_tokens": prompt,
        "output_tokens": completion,
        "cache_read_tokens": int(extracted.cache_hit or 0),
        "total_tokens": total,
        "cost_usd": float(cost or 0.0),
        "request_ids": ids,
    }


def write_usage_sidecar(
    logs_dir: Path,
    uid: str,
    resp: Any,
    *,
    run_id: str,
    model: str,
) -> Path:
    logs_dir = Path(logs_dir)
    logs_dir.mkdir(parents=True, exist_ok=True)
    normalized = normalize_response(resp)
    path = sidecar_path(logs_dir, uid)
    path.write_text(
        json.dumps(
            {
                "run_id": run_id,
                "uid": uid,
                "model": model,
                "captured_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
                "response": {
                    "id": normalized.get("id") or normalized.get("request_id"),
                    "model": normalized.get("model") or model,
                    "usage": normalized.get("usage") or {},
                },
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def read_usage_sidecar(logs_dir: Path, uid: str, *, model: str) -> dict[str, Any] | None:
    path = sidecar_path(logs_dir, uid)
    if not path.is_file():
        return None
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    resp = doc.get("response") or doc
    return contract_usage(
        source="local_fallback",
        resp=resp,
        model=str(doc.get("model") or model),
    )


def _tag_strings(row: dict[str, Any]) -> list[str]:
    tags = row.get("request_tags") or []
    if isinstance(tags, str):
        try:
            tags = json.loads(tags)
        except json.JSONDecodeError:
            tags = [tags]
    if not isinstance(tags, list):
        tags = [tags]
    return [str(item) for item in tags]


def _tag_has_run(row: dict[str, Any], run_id: str) -> bool:
    if not run_id:
        return False
    blob = " ".join(item.lower() for item in _tag_strings(row))
    run_l = run_id.lower()
    return f"run:{run_l}" in blob or f"x-run-id: {run_l}" in blob or f"x-run-id:{run_l}" in blob


def spend_row_matches_key(row: dict[str, Any], token: str) -> bool:
    """按 run 虚拟 key 归账：给加不了请求头的客户端用。"""
    if not token:
        return False
    if str(row.get("api_key") or "") == token:
        return True
    meta = row.get("metadata")
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except json.JSONDecodeError:
            meta = None
    if isinstance(meta, dict) and str(meta.get("user_api_key") or "") == token:
        return True
    return False


def spend_row_matches(row: dict[str, Any], run_id: str, item_id: str) -> bool:
    if not run_id or not item_id:
        return False
    tags = [item.lower() for item in _tag_strings(row)]
    blob = " ".join(tags)
    run_l = run_id.lower()
    item_l = item_id.lower()
    has_run = (
        f"run:{run_l}" in blob
        or f"x-run-id: {run_l}" in blob
        or f"x-run-id:{run_l}" in blob
        or any(item == run_l for item in tags)
    )
    has_item = (
        f"item:{item_l}" in blob
        or f"x-item-id: {item_l}" in blob
        or f"x-item-id:{item_l}" in blob
        or any(item == item_l for item in tags)
    )
    if has_run and has_item:
        return True
    meta = row.get("metadata")
    if meta:
        text = json.dumps(meta, default=str).lower()
        if run_l in text and item_l in text:
            return True
    return False


def usage_from_spend_rows(rows: list[dict[str, Any]], *, model: str) -> dict[str, Any]:
    if not rows:
        return missing_usage()
    merged = {
        "id": None,
        "usage": {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "prompt_cache_hit_tokens": 0,
        },
    }
    ids: list[str] = []
    for row in rows:
        req_id = row.get("request_id") or row.get("id")
        if req_id:
            ids.append(str(req_id))
        merged["usage"]["prompt_tokens"] += int(row.get("prompt_tokens") or 0)
        merged["usage"]["completion_tokens"] += int(row.get("completion_tokens") or 0)
        merged["usage"]["total_tokens"] += int(row.get("total_tokens") or 0)
        cache = row.get("cache_read_input_tokens") or row.get("cache_hit_tokens")
        if cache:
            merged["usage"]["prompt_cache_hit_tokens"] += int(cache)
        if not merged["id"]:
            merged["id"] = req_id
    return contract_usage(source="litellm", resp=merged, model=model, request_ids=ids)


def load_spend_logs(path: Path) -> list[dict[str, Any]]:
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(doc, dict):
        doc = doc.get("data") or doc.get("logs") or doc.get("spend_logs") or []
    if not isinstance(doc, list):
        raise ValueError(f"spend logs in {path} must be a list")
    return [row for row in doc if isinstance(row, dict)]


def proxy_base_url(explicit: str | None = None) -> str:
    return proxy.proxy_base_url(explicit)


def proxy_api_key(explicit: str | None = None) -> str:
    """读 spend 日志要管理钥匙。做题进程环境里常常没有，所以也读代理的 .env。"""
    return proxy.master_key(explicit)


def proxy_alive(base_url: str | None = None, timeout: float = 3.0) -> bool:
    return proxy.alive(base_url, timeout)


def _http_json(url: str, *, api_key: str, timeout: float) -> Any:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {api_key}"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_spend_logs(
    *,
    base_url: str | None = None,
    api_key: str | None = None,
    request_id: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    timeout: float = 20.0,
) -> list[dict[str, Any]]:
    base = proxy_base_url(base_url)
    key = proxy_api_key(api_key)
    if not key:
        return []
    if request_id:
        url = f"{base}/spend/logs?request_id={urllib.parse.quote(request_id)}"
        doc = _http_json(url, api_key=key, timeout=timeout)
        rows = doc if isinstance(doc, list) else []
        return [row for row in rows if isinstance(row, dict)]
    today = datetime.now(timezone.utc).date()
    start = start_date or today.isoformat()
    end = end_date or (today + timedelta(days=1)).isoformat()
    url = (
        f"{base}/spend/logs?start_date={urllib.parse.quote(start)}"
        f"&end_date={urllib.parse.quote(end)}&summarize=false"
    )
    doc = _http_json(url, api_key=key, timeout=timeout)
    rows = doc if isinstance(doc, list) else []
    # summarize=true 的日汇总没有 request_id，不能当逐题账。
    return [
        row
        for row in rows
        if isinstance(row, dict) and (row.get("request_id") or row.get("prompt_tokens") is not None)
    ]


@dataclass
class UsageCollector:
    run_id: str
    model: str
    logs_dir: Path | None = None
    proxy_url: str | None = None
    api_key: str | None = None
    spend_logs: list[dict[str, Any]] | None = None
    spend_logs_path: Path | None = None
    mode: str = "auto"
    run_token: str | None = None
    settle_tries: int = SPEND_SETTLE_TRIES
    settle_sleep_s: float = SPEND_SETTLE_SLEEP_S
    _cached_spend: list[dict[str, Any]] | None = field(default=None, init=False, repr=False)
    _fixed_spend: bool = field(default=False, init=False, repr=False)
    _primed: bool = field(default=False, init=False, repr=False)

    def _live_lookup(self) -> bool:
        return (
            self.mode == "litellm"
            or self.proxy_url is not None
            or os.environ.get("XSKILL_BENCH_LITELLM_LIVE") == "1"
        )

    def _fetch_spend(self) -> list[dict[str, Any]]:
        try:
            return fetch_spend_logs(base_url=self.proxy_url, api_key=self.api_key)
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
            return []

    def _loaded_spend(self, *, refresh: bool = False) -> list[dict[str, Any]]:
        if self.spend_logs is not None:
            self._cached_spend = list(self.spend_logs)
            self._fixed_spend = True
            return self._cached_spend
        if self.spend_logs_path is not None:
            if self._cached_spend is None:
                self._cached_spend = load_spend_logs(self.spend_logs_path)
            self._fixed_spend = True
            return self._cached_spend
        if self._cached_spend is not None and not refresh:
            return self._cached_spend
        if (
            self.mode in {"auto", "litellm"}
            and self._live_lookup()
            and proxy_alive(self.proxy_url)
        ):
            self._cached_spend = self._fetch_spend()
        else:
            self._cached_spend = []
        return self._cached_spend

    def _match(self, rows: list[dict[str, Any]], uid: str) -> list[dict[str, Any]]:
        tagged = [row for row in rows if spend_row_matches(row, self.run_id, uid)]
        if tagged or not self.run_token:
            return tagged
        # 加不了请求头的客户端只能按 run 归账，逐题分不开，所以这里不认。
        return []

    def prime(self, uids: list[str]) -> None:
        """一趟跑完统一对账前调一次：账本异步落库，只在这里等它追上。"""
        self._primed = True
        if self.spend_logs is not None or self.spend_logs_path is not None:
            self._loaded_spend()
            return
        if self.mode not in {"auto", "litellm"} or not self._live_lookup():
            return
        tries = max(int(self.settle_tries), 1)
        for attempt in range(tries):
            rows = self._loaded_spend(refresh=True)
            if all(self._match(rows, uid) for uid in uids):
                return
            if attempt + 1 < tries:
                time.sleep(max(self.settle_sleep_s, 0.0))

    def from_litellm(self, uid: str) -> dict[str, Any] | None:
        rows = self._match(self._loaded_spend(), uid)
        # 单题调用时账本可能还没落库；prime 过了就不再逐题等。
        attempts = 1 if (self._fixed_spend or self._primed) else max(int(self.settle_tries), 1)
        for attempt in range(1, attempts):
            if rows:
                break
            time.sleep(max(self.settle_sleep_s, 0.0))
            rows = self._match(self._loaded_spend(refresh=True), uid)
            del attempt
        if not rows:
            return None
        usage = usage_from_spend_rows(rows, model=self.model)
        if usage.get("source") == "missing":
            return None
        return usage

    def _run_rows(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if self.run_token:
            return [row for row in rows if spend_row_matches_key(row, self.run_token)]
        return [row for row in rows if _tag_has_run(row, self.run_id)]

    def run_totals(self) -> dict[str, Any]:
        """整趟合计：逐题对不上时（例如训练阶段）用 run 虚拟 key 归账。"""
        rows = self._run_rows(self._loaded_spend(refresh=True))
        # 账本异步落库，最后一笔常常还没到，跟 prime 一样等一会儿。
        attempts = 1 if self._fixed_spend else max(int(self.settle_tries), 1)
        for attempt in range(1, attempts):
            if rows:
                break
            time.sleep(max(self.settle_sleep_s, 0.0))
            rows = self._run_rows(self._loaded_spend(refresh=True))
            del attempt
        if not rows:
            return missing_usage()
        totals = usage_from_spend_rows(rows, model=self.model)
        # 一趟官方训练几千笔请求，逐条 id 写进旁路文件没人看，只留笔数和头几条。
        ids = list(totals.get("request_ids") or [])
        if len(ids) > _RUN_ID_SAMPLE:
            totals["request_ids"] = ids[:_RUN_ID_SAMPLE]
        totals["n_requests"] = len(ids)
        return totals

    def from_sidecar(self, uid: str) -> dict[str, Any] | None:
        if self.logs_dir is None:
            return None
        usage = read_usage_sidecar(self.logs_dir, uid, model=self.model)
        if usage is None or usage.get("source") == "missing":
            return None
        return usage

    def for_item(self, uid: str) -> dict[str, Any]:
        mode = self.mode or "auto"
        if mode == "missing":
            return missing_usage()
        if mode in {"auto", "litellm"}:
            litellm_usage = self.from_litellm(uid)
            if litellm_usage is not None:
                return litellm_usage
            if mode == "litellm":
                return missing_usage()
        if mode in {"auto", "local_fallback"}:
            sidecar_usage = self.from_sidecar(uid)
            if sidecar_usage is not None:
                return sidecar_usage
        return missing_usage()


def collect_item_usage(
    *,
    run_id: str,
    uid: str,
    model: str,
    logs_dir: Path | None = None,
    proxy_url: str | None = None,
    api_key: str | None = None,
    spend_logs: list[dict[str, Any]] | None = None,
    spend_logs_path: Path | None = None,
    mode: str = "auto",
    run_token: str | None = None,
) -> dict[str, Any]:
    return UsageCollector(
        run_id=run_id,
        model=model,
        logs_dir=logs_dir,
        proxy_url=proxy_url,
        api_key=api_key,
        spend_logs=spend_logs,
        spend_logs_path=spend_logs_path,
        run_token=run_token,
        mode=mode,
    ).for_item(uid)
