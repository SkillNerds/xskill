"""prices.py — 模型价格表的拉取 / 解析 / 运行时后台刷新(单一收口)。

═══════════════════════════════════════════════════════════════════
背景:成本 = token × 单价,单价来自 LiteLLM 社区维护的价格表(只取数据,
**不引 litellm 包**,见 ADR-0001)。

两条使用路径,共用同一份 fetch+parse 逻辑(避免熵增):

1. **build 时**(``scripts/fetch_model_prices.py``):strict 拉取 → vendor 成
   ``data/model_prices.json``。任何失败 → 构建报错(exit 1)。
2. **运行时**(``maybe_refresh``):**best-effort 后台守护线程**刷新用户缓存
   ``~/.xskill/cache/model_prices.json``。**无网不阻塞启动、网络报错不阻塞
   主流程**——静默吞错,下次运行生效即可。

运行时解析优先级(见 ``usage.load_price_table``):
``config.pricing`` > 用户缓存(刷新得来,较新) > vendored seed(离线保底) > default。
缓存的「新鲜度 TTL」只决定**是否触发**一次后台刷新,不决定是否**使用**缓存——
哪怕缓存过期,它仍比 build 时冻结的 seed 新。
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, Optional, Tuple

logger = logging.getLogger("xskill.prices")

PRICES_URL = ("https://raw.githubusercontent.com/BerriAI/litellm/main/"
              "model_prices_and_context_window.json")
SCHEMA_DESC = ("model -> {input_per_1m, output_per_1m, "
               "cache_hit_per_1m?, cache_miss_per_1m?, embed_per_1m?}")
MIN_ENTRIES = 50          # 解析出的条目少于此值视为源格式变更
DEFAULT_TTL_DAYS = 1      # 缓存超过此天数才触发后台刷新


def _per_1m(per_token) -> Optional[float]:
    return round(float(per_token) * 1_000_000, 6) if isinstance(per_token, (int, float)) else None


def parse_litellm(raw: Dict[str, dict]) -> Dict[str, dict]:
    """把 LiteLLM 原始价格表解析成本项目 schema:``model -> entry``(不含 _meta)。"""
    out: Dict[str, dict] = {}
    for model, d in raw.items():
        if not isinstance(d, dict):
            continue
        mode = d.get("mode")
        if mode == "embedding":
            ep = _per_1m(d.get("input_cost_per_token"))
            if ep is not None:
                out[model] = {"embed_per_1m": ep}
        elif mode in (None, "chat", "completion", "responses"):
            ip = _per_1m(d.get("input_cost_per_token"))
            op = _per_1m(d.get("output_cost_per_token"))
            if ip is None and op is None:
                continue
            entry = {"input_per_1m": ip or 0.0, "output_per_1m": op or 0.0}
            ch = _per_1m(d.get("cache_read_input_token_cost"))
            if ch is not None:
                entry["cache_hit_per_1m"] = ch
                entry["cache_miss_per_1m"] = entry["input_per_1m"]
            out[model] = entry
    return out


def fetch_and_build(timeout: int = 30) -> Dict[str, dict]:
    """拉取并解析价格表,返回带 ``_meta`` 的完整 payload。失败/条目过少 → 抛错。"""
    with urllib.request.urlopen(PRICES_URL, timeout=timeout) as r:  # noqa: S310
        raw = json.loads(r.read().decode("utf-8"))
    models = parse_litellm(raw)
    if len(models) < MIN_ENTRIES:
        raise ValueError(f"解析到的条目过少({len(models)}),疑似源格式变更")
    payload: Dict[str, dict] = {"_meta": {
        "source": PRICES_URL,
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "schema": SCHEMA_DESC,
    }}
    payload.update(models)
    return payload


# ─────────────────────────────────────────────────────────────────
# 运行时后台刷新(best-effort,永不阻塞、永不抛错)
# ─────────────────────────────────────────────────────────────────

def user_cache_path() -> Path:
    from xskill.config import XSKILL_HOME
    return XSKILL_HOME / "cache" / "model_prices.json"


def _is_fresh(path: Path, ttl_days: float) -> bool:
    try:
        return (time.time() - path.stat().st_mtime) < ttl_days * 86400
    except OSError:
        return False


def _atomic_write(path: Path, payload: Dict[str, dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


# ── watchdog:上游失效/地址变更/schema 变更要能被看见 ───────────────
#   high(warn 级日志,可操作):地址失效 / schema 变更 —— 源真的变了,得改 URL/parser。
#   low(debug 级,预期噪音):网络不可达 —— 多半是临时断网。
def _classify(exc: Exception) -> Tuple[str, str]:
    if isinstance(exc, urllib.error.HTTPError):
        return ("source_moved", "high") if exc.code in (404, 410, 451) else ("unreachable", "low")
    if isinstance(exc, urllib.error.URLError):
        return "unreachable", "low"
    if isinstance(exc, (TimeoutError, OSError)):
        return "unreachable", "low"
    if isinstance(exc, ValueError):       # 含 json.JSONDecodeError 与 MIN_ENTRIES 校验
        return "schema_changed", "high"
    return "unknown", "high"


def _status_path(cache_path: Path) -> Path:
    return cache_path.with_suffix(".status.json")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _write_status(cache_path: Path, *, ok: bool, kind: Optional[str] = None,
                  error: Optional[str] = None, models: Optional[int] = None) -> None:
    """记录最近一次刷新结局(成功保留 last_success_at)。best-effort,自身永不抛错。"""
    sp = _status_path(cache_path)
    try:
        prev = json.loads(sp.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        prev = {}
    now = _now_iso()
    st = {
        "last_attempt_at": now,
        "last_attempt_ok": ok,
        "last_success_at": now if ok else prev.get("last_success_at"),
        "last_error_kind": None if ok else kind,
        "last_error": None if ok else (error or "")[:200],
        "models": models if ok else prev.get("models"),
    }
    try:
        sp.parent.mkdir(parents=True, exist_ok=True)
        sp.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
    except OSError:
        logger.debug("price status write failed", exc_info=True)


def _do_refresh(fetcher: Callable[[], Dict[str, dict]], cache_path: Path) -> None:
    """后台线程体:拉取 → 原子写缓存 + 记状态。异常永不外抛,但按类型分级告警。"""
    try:
        payload = fetcher()
    except Exception as exc:  # pylint: disable=broad-exception-caught
        kind, sev = _classify(exc)
        _write_status(cache_path, ok=False, kind=kind, error=str(exc))
        (logger.warning if sev == "high" else logger.debug)(
            "model price refresh failed (%s): %s", kind, exc)
        return
    try:
        _atomic_write(cache_path, payload)
        _write_status(cache_path, ok=True, models=max(len(payload) - 1, 0))
        logger.debug("model prices refreshed -> %s", cache_path)
    except OSError:
        logger.debug("model price cache write failed", exc_info=True)


def refresh_health(cache_path: Optional[Path] = None) -> Optional[dict]:
    """给仪表盘用:返回最近刷新健康度。无状态文件(从没刷过)→ None,不告警。

    返回 ``{ok, kind, error, stale_days}``;``stale_days`` = 距上次**成功**刷新的天数。
    """
    cp = cache_path or user_cache_path()
    try:
        st = json.loads(_status_path(cp).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    stale_days = None
    succ = st.get("last_success_at")
    if succ:
        try:
            age = datetime.now(timezone.utc) - datetime.fromisoformat(succ)
            stale_days = round(age.total_seconds() / 86400, 1)
        except ValueError:
            stale_days = None
    return {"ok": bool(st.get("last_attempt_ok")), "kind": st.get("last_error_kind"),
            "error": st.get("last_error"), "stale_days": stale_days}


def maybe_refresh(ttl_days: float = DEFAULT_TTL_DAYS, *,
                  fetcher: Optional[Callable[[], Dict[str, dict]]] = None,
                  cache_path: Optional[Path] = None) -> Optional[threading.Thread]:
    """缓存陈旧(或缺失)则起一个 daemon 线程后台刷新,**立刻返回不阻塞**。

    无网/报错都不会传播到调用方;返回已启动的线程(测试用)或 None(无需刷新)。
    """
    try:
        cp = cache_path or user_cache_path()
        if _is_fresh(cp, ttl_days):
            return None
        t = threading.Thread(target=_do_refresh, args=(fetcher or fetch_and_build, cp),
                             daemon=True, name="xskill-price-refresh")
        t.start()
        return t
    except Exception:  # pylint: disable=broad-exception-caught
        logger.debug("maybe_refresh noop", exc_info=True)
        return None
