"""usage.py — LLM/embedding token & 成本统计(单一收口)
═══════════════════════════════════════════════════════════════════
Issue #43。设计要点(详见会话定稿):

- **唯一新存的数据** = 每次调用的 token/cost(否则算完即丢)。其余 stats
  内容(技能事件/流水线/贡献)都是对已有状态单元的只读视图,不在这里存。
- **成本 = 估算**:token × 单价。没有余额、没有第二套真值来源。
- **定价不引任何包**(litellm/tokencost/tiktoken 全 ban,见 ADR-0001):
  build 时 vendor 一份 ``data/model_prices.json``(USD / 1M token)做离线 seed;
  运行时 ``prices.maybe_refresh`` 后台 best-effort 刷新用户缓存(无网/报错绝不
  阻塞、绝不抛错)。解析优先级 ``config.pricing[model]`` > 用户缓存 > vendored
  seed > ``pricing.default`` 可配兜底单价 —— ``cost`` 永不为 None,只标
  ``price_source``。
- **旁路 best-effort**:``record_*`` 永不抛错、永不阻断流水线。
- token 数沿用 rate_limit 的字符粗估 + ``response.usage`` 自校准。

全局单例 ``LEDGER``,调用点一行 ``LEDGER.record_llm(step, model, resp)``。
"""

from __future__ import annotations

import contextlib
import json
import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger("xskill.usage")

# 当前流水线步骤(atom_split / skill_route / skill_edit / ux_score …)。
# 收口点记账时用它归因;pipeline 用 `with use_step("atom_split"):` 包住即可。
_STEP = threading.local()


def current_step() -> str:
    return getattr(_STEP, "name", None) or "llm"


@contextlib.contextmanager
def use_step(name: str):
    prev = getattr(_STEP, "name", None)
    _STEP.name = name
    try:
        yield
    finally:
        _STEP.name = prev

# 出厂兜底单价(USD / 1M token)。config.pricing.default 可覆盖。
_FALLBACK_DEFAULT = {"input_per_1m": 1.0, "output_per_1m": 3.0, "embed_per_1m": 0.05}


@dataclass(frozen=True)
class Usage:
    """一次调用的 token 拆分。embedding 调用只有 prompt(=total)。"""
    prompt: int = 0
    completion: int = 0
    total: int = 0
    cache_hit: int = 0      # DeepSeek prompt_cache_hit_tokens
    cache_miss: int = 0     # DeepSeek prompt_cache_miss_tokens


@dataclass(frozen=True)
class ModelPrice:
    """USD / 1M token。cache_* 为 None 时退回 input 价;embed_per_1m 仅 embedding。"""
    input_per_1m: float = 0.0
    output_per_1m: float = 0.0
    cache_hit_per_1m: Optional[float] = None
    cache_miss_per_1m: Optional[float] = None
    embed_per_1m: Optional[float] = None


def extract_usage(resp: Any) -> Usage:
    """从 OpenAI 兼容 response 提取 token(dict 或 SDK 对象,缺失补 0)。

    覆盖 prompt/completion/total + DeepSeek 的 prompt_cache_hit/miss_tokens。
    """
    def g(obj: Any, key: str) -> Optional[int]:
        if obj is None:
            return None
        v = obj.get(key) if isinstance(obj, dict) else getattr(obj, key, None)
        return int(v) if isinstance(v, (int, float)) else None

    usage = resp.get("usage") if isinstance(resp, dict) else getattr(resp, "usage", None)
    if usage is None:
        return Usage()
    prompt = g(usage, "prompt_tokens") or 0
    completion = g(usage, "completion_tokens") or 0
    total = g(usage, "total_tokens") or (prompt + completion)
    hit = g(usage, "prompt_cache_hit_tokens") or 0
    miss = g(usage, "prompt_cache_miss_tokens") or 0
    return Usage(prompt=prompt, completion=completion, total=total,
                 cache_hit=hit, cache_miss=miss)


class PriceTable:
    """定价解析:config 覆盖 > vendored 静态表 > default 兜底。永远给出 (price, source)。"""

    def __init__(self, vendored: Dict[str, dict], config_pricing: Optional[dict] = None):
        cp = config_pricing or {}
        self._default = ModelPrice(**{**_FALLBACK_DEFAULT, **(cp.get("default") or {})})
        self._config = {k: _mk_price(v) for k, v in cp.items() if k != "default"}
        self._static = {k: _mk_price(v) for k, v in vendored.items()
                        if not k.startswith("_")}

    def resolve(self, model: str) -> Tuple[ModelPrice, str]:
        if model in self._config:
            return self._config[model], "config"
        if model in self._static:
            return self._static[model], "static"
        return self._default, "default"


def _mk_price(d: dict) -> ModelPrice:
    return ModelPrice(
        input_per_1m=float(d.get("input_per_1m", 0.0)),
        output_per_1m=float(d.get("output_per_1m", 0.0)),
        cache_hit_per_1m=_opt_float(d.get("cache_hit_per_1m")),
        cache_miss_per_1m=_opt_float(d.get("cache_miss_per_1m")),
        embed_per_1m=_opt_float(d.get("embed_per_1m")),
    )


def _opt_float(v: Any) -> Optional[float]:
    return float(v) if isinstance(v, (int, float)) else None


def cost_usd(usage: Usage, price: ModelPrice, *, is_embed: bool = False) -> float:
    """token × 单价 → USD。embedding 走 embed 价;否则 prompt(含 cache 分档)+ completion。"""
    M = 1_000_000
    if is_embed:
        per = price.embed_per_1m if price.embed_per_1m is not None else price.input_per_1m
        return usage.total / M * per
    # prompt 段:有 cache 拆分且配了 cache 价 → 分档计;否则整段 input 价
    if (usage.cache_hit or usage.cache_miss) and price.cache_hit_per_1m is not None:
        hit_p = price.cache_hit_per_1m
        miss_p = price.cache_miss_per_1m if price.cache_miss_per_1m is not None else price.input_per_1m
        prompt_cost = (usage.cache_hit * hit_p + usage.cache_miss * miss_p) / M
    else:
        prompt_cost = usage.prompt / M * price.input_per_1m
    return prompt_cost + usage.completion / M * price.output_per_1m


# ─────────────────────────────────────────────────────────────────
# Ledger —— 唯一有状态的部分:进程内热计数 + 落 registry 持久化
# ─────────────────────────────────────────────────────────────────

@dataclass
class _Bucket:
    calls: int = 0
    tokens: int = 0
    cost: float = 0.0


class UsageLedger:
    """线程安全的 token/cost 汇聚点。``record_*`` 永不抛错。"""

    def __init__(self, prices: PriceTable):
        self._prices = prices
        self._lock = threading.Lock()
        self._by_step: Dict[str, _Bucket] = {}
        self._by_model: Dict[str, _Bucket] = {}
        self._total = _Bucket()
        self._estimated = False   # 只要命中过 default/static 即标估算

    # -- 记账 --------------------------------------------------------
    def record_llm(self, step: str, model: str, resp: Any) -> None:
        self._record(step, model, extract_usage(resp), is_embed=False)

    def record_embed(self, model: str, resp: Any, *, step: str = "embedding") -> None:
        self._record(step, model, extract_usage(resp), is_embed=True)

    def _record(self, step: str, model: str, usage: Usage, *, is_embed: bool) -> None:
        try:
            price, source = self._prices.resolve(model)
            usd = cost_usd(usage, price, is_embed=is_embed)
            with self._lock:
                if source != "config":
                    self._estimated = True
                for d, key in ((self._by_step, step), (self._by_model, model)):
                    b = d.setdefault(key, _Bucket())
                    b.calls += 1; b.tokens += usage.total; b.cost += usd
                self._total.calls += 1
                self._total.tokens += usage.total
                self._total.cost += usd
            logger.debug("[LLM] model=%s step=%s tokens=%d cost=$%.5f src=%s",
                         model, step, usage.total, usd, source)
            _persist(step, model, usage, usd, source)
        except Exception:  # pylint: disable=broad-exception-caught
            logger.warning("usage ledger record failed (non-fatal)", exc_info=True)

    # -- 快照 --------------------------------------------------------
    def snapshot(self) -> dict:
        with self._lock:
            return {
                "total_usd": round(self._total.cost, 6),
                "total_tokens": self._total.tokens,
                "total_calls": self._total.calls,
                "estimated": self._estimated,
                "by_step": {k: _b(v) for k, v in self._by_step.items()},
                "by_model": {k: _b(v) for k, v in self._by_model.items()},
            }


def _b(b: _Bucket) -> dict:
    return {"calls": b.calls, "tokens": b.tokens, "cost_usd": round(b.cost, 6)}


# ─────────────────────────────────────────────────────────────────
# 加载 / 全局单例
# ─────────────────────────────────────────────────────────────────

def _vendored_path() -> Path:
    return Path(__file__).with_name("data") / "model_prices.json"


def load_price_table(config_pricing: Optional[dict] = None,
                     vendored_path: Optional[Path] = None,
                     cache_path: Optional[Path] = None) -> PriceTable:
    """vendored seed 打底,运行时刷新得来的用户缓存**覆盖**其上(较新)。

    用合并而非替换:缓存里有的(上游 litellm)取新价,seed 独有的(自维护的
    deepseek-v4-flash 等 litellm 缺失条目)保留,不会因刷新而退化成 default。
    """
    from xskill import prices
    p = vendored_path or _vendored_path()
    table = _read_prices(p) or {}
    cp = cache_path if cache_path is not None else prices.user_cache_path()
    if cp and cp.exists():
        cache = _read_prices(cp)
        if cache:
            table = {**table, **cache}
    if not table:
        logger.warning("price table unreadable (cache=%s vendored=%s); default-only", cp, p)
    return PriceTable(table, config_pricing)


def _read_prices(path: Path) -> Optional[dict]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _persist(step: str, model: str, usage: Usage, usd: float, source: str) -> None:
    """best-effort 落 registry llm_usage 表(函数内 import 防环;失败仅 warn)。"""
    try:
        from xskill.pipeline.registry import record_usage
        record_usage(step=step, model=model, prompt=usage.prompt,
                     completion=usage.completion, total=usage.total,
                     cost_usd=usd, price_source=source)
    except Exception:  # pylint: disable=broad-exception-caught
        logger.debug("usage persist skipped", exc_info=True)


def _fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n/1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n/1_000:.1f}K"
    return str(n)


_PRICE_WARN_REASON = {
    "schema_changed": "上游格式变更", "source_moved": "上游地址失效",
    "unreachable": "上游不可达", "unknown": "刷新异常",
}


def _price_warn_line(health: Optional[dict]) -> Optional[str]:
    """价格表刷新失效时的看板告警行;健康/无记录 → None(不显示)。"""
    if not health or health.get("ok"):
        return None
    reason = _PRICE_WARN_REASON.get(health.get("kind"), "刷新异常")
    sd = health.get("stale_days")
    aged = f"{sd:g}d 未刷新" if sd is not None else "从未成功刷新"
    return f"  ⚠ 价格表 {aged} · {reason},沿用旧价"


def render_stats(summary: dict, *, status: Optional[dict] = None,
                 models: Optional[list] = None,
                 price_health: Optional[dict] = None) -> str:
    """把 status(进程/角色/处理模型) + usage_summary + 用户模型占比 渲成文本仪表盘。"""
    status = status or {}
    role = status.get("role", "?")
    bar_line = " " + "─" * 56
    lines = [f"  xskill stats · {role}", bar_line]
    if price_health is None:
        try:
            from xskill import prices
            price_health = prices.refresh_health()
        except Exception:  # pylint: disable=broad-exception-caught
            price_health = None
    warn = _price_warn_line(price_health)

    # ── 进程 + 处理模型 ──
    if status.get("running"):
        lines.append(f"  ● serve 运行中   pid {status.get('pid')} · :{status.get('port')}")
    else:
        lines.append("  ○ serve 未运行")
    if status.get("llm_model"):
        prov = (status.get("llm_base_url") or "").split("://")[-1].split("/")[0]
        em = f"  · embed {status.get('embed_model')}" if status.get("embed_model") else ""
        lines.append(f"  处理模型  {status['llm_model']} @ {prov}{em}")
    lines.append(bar_line)

    # ── 成本/用量 ──
    est = " · 估算" if summary.get("estimated") else ""
    lines.append(f"  💰 成本{est}      今日 ${summary.get('today_usd', 0):.4f}"
                 f"  ·  累计 ${summary.get('total_usd', 0):.4f}")
    lines.append(f"     {_fmt_tokens(summary.get('total_tokens', 0))} tokens"
                 f"  ·  {summary.get('total_calls', 0)} calls")
    if warn:
        lines.append(warn)
    steps = summary.get("by_step") or []
    if steps:
        mx = max((s["cost"] or 0) for s in steps) or 1e-9
        for s in steps:
            bar = "█" * int(round((s["cost"] or 0) / mx * 14))
            lines.append(f"       {s['step']:<13}{bar:<14}  "
                         f"{_fmt_tokens(s['tokens'] or 0):>7}  ${s['cost'] or 0:.4f}")
    lines.append(bar_line)

    # ── 用户 agent 模型占比(轨迹来源)──
    models = models or []
    if models:
        lines.append("  🧩 用户模型 (轨迹占比)")
        for m in models[:8]:
            bar = "█" * int(round((m.get("pct") or 0) / 100 * 14))
            lines.append(f"     {m['model']:<22}{bar:<14} {m.get('pct', 0):>5.1f}%"
                         f"  ({m.get('trajs', 0)})")
        lines.append(bar_line)
    return "\n".join(lines)


# 进程级单例。首次 access 时用 config.pricing 构建;config 不可用则纯默认表。
_LEDGER: Optional[UsageLedger] = None
_LEDGER_LOCK = threading.Lock()


def get_ledger() -> UsageLedger:
    global _LEDGER  # pylint: disable=global-statement
    if _LEDGER is None:
        with _LEDGER_LOCK:
            if _LEDGER is None:
                # 后台 best-effort 刷新价格缓存:无网/报错都不阻塞、不抛错。
                try:
                    from xskill import prices
                    prices.maybe_refresh()
                except Exception:  # pylint: disable=broad-exception-caught
                    logger.debug("price refresh trigger skipped", exc_info=True)
                pricing = None
                try:
                    from xskill.config import get_config
                    pricing = get_config().get("pricing")
                except Exception:  # pylint: disable=broad-exception-caught
                    pricing = None
                _LEDGER = UsageLedger(load_price_table(pricing))
    return _LEDGER
