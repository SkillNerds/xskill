"""utils/rate_limit.py —— LLM 请求限流(RPM + TPM 双桶)
═══════════════════════════════════════════════════════════════
DIY 实现,零额外依赖。设计基线:
- 永不 import tiktoken / tokenizers / litellm(详见 docs/adr/0001)
- 字符粗估 token 数,response.usage 存在则自校准,缺失则保留估算
- 线程安全(threading.Lock),用 time.monotonic 防系统时钟回拨
- 配置缺省 = 不限流(快路径)
"""
from __future__ import annotations

import math
import threading
import time
import unicodedata
from typing import Any, Callable, Dict, Optional

from xskill.utils.shutdown import SHUTTING_DOWN


def estimate_tokens(text: str) -> int:
    """粗估字符串 token 数,英文 4 字符/token,中文 1.5 字符/token,× 1.2 余量。

    设计取舍:
    - 不引 tiktoken(中国用户 Azure blob 下载灾难,见 ADR-0001)
    - 误差容忍 ±30%,真实 token 数靠 response.usage 在 reconcile 中校准
    - × 1.2 余量是"宁多算"策略,避免低估导致瞬时超额 429
    """
    if not text:
        return 0
    ascii_chars = sum(1 for c in text if ord(c) < 128)
    cjk_chars = sum(
        1 for c in text
        if 'CJK' in unicodedata.name(c, '') or '　' <= c <= '鿿'
    )
    other_chars = len(text) - ascii_chars - cjk_chars
    raw = ascii_chars / 4 + cjk_chars / 1.5 + other_chars / 2.5
    return max(1, math.ceil(raw * 1.2))


class TokenBucket:
    """RPM + TPM 双桶。

    acquire_rpm / acquire_tpm 分别管两个独立桶,wrapper 调用时两个都要 acquire。
    clock 参数可注入(测试用 FakeClock),生产默认 time.monotonic。
    """

    def __init__(
        self,
        *,
        rpm: Optional[int] = None,
        tpm: Optional[int] = None,
        burst: Optional[int] = None,
        clock: Optional[Callable[[], float]] = None,
    ):
        self.rpm = rpm
        self.tpm = tpm
        # burst 默认 = ceil(rate/6),约 10 秒预算的瞬时突发
        self._rpm_burst = burst if burst is not None else (max(1, rpm // 6) if rpm else 0)
        self._tpm_burst = burst if burst is not None else (max(1, tpm // 6) if tpm else 0)
        self._clock = clock or time.monotonic

        self._rpm_tokens = float(self._rpm_burst)
        self._tpm_tokens = float(self._tpm_burst)
        self._last_rpm_refill = self._clock()
        self._last_tpm_refill = self._clock()
        self._lock = threading.Lock()

    # ─── RPM ─────────────────────────────────────────────────

    def _refill_rpm(self) -> None:
        if not self.rpm:
            return
        now = self._clock()
        elapsed = now - self._last_rpm_refill
        self._rpm_tokens = min(
            float(self._rpm_burst),
            self._rpm_tokens + elapsed * (self.rpm / 60.0),
        )
        self._last_rpm_refill = now

    def acquire_rpm(self, *, timeout: float = 0.0) -> float:
        """尝试取 1 个 RPM token。返回值:
        - 0.0  → 已扣减,可立刻发请求
        - > 0  → 还需等待这么多秒
        timeout=0 表示纯查询不阻塞;> 0 时本方法内自旋等待至 timeout 上限。
        """
        if not self.rpm:
            return 0.0
        deadline = self._clock() + timeout
        while True:
            with self._lock:
                self._refill_rpm()
                if self._rpm_tokens >= 1:
                    self._rpm_tokens -= 1
                    return 0.0
                shortfall = 1 - self._rpm_tokens
                wait = shortfall / (self.rpm / 60.0)
            if timeout <= 0 or self._clock() + wait > deadline:
                return wait
            # Event.wait 代替 time.sleep：进程退出时立即放弃等桶,返回剩余
            # 等待秒数,调用方按"桶耗尽"路径抛错,不再把优雅退出拖到分钟级
            if SHUTTING_DOWN.wait(min(wait, max(0.01, deadline - self._clock()))):
                return wait

    # ─── TPM ─────────────────────────────────────────────────

    def _refill_tpm(self) -> None:
        if not self.tpm:
            return
        now = self._clock()
        elapsed = now - self._last_tpm_refill
        self._tpm_tokens = min(
            float(self._tpm_burst),
            self._tpm_tokens + elapsed * (self.tpm / 60.0),
        )
        self._last_tpm_refill = now

    def acquire_tpm(self, n: int, *, timeout: float = 0.0) -> float:
        """扣 n 个 TPM token。语义同 acquire_rpm。"""
        if not self.tpm or n <= 0:
            return 0.0
        deadline = self._clock() + timeout
        while True:
            with self._lock:
                self._refill_tpm()
                if self._tpm_tokens >= n:
                    self._tpm_tokens -= n
                    return 0.0
                shortfall = n - self._tpm_tokens
                wait = shortfall / (self.tpm / 60.0)
            if timeout <= 0 or self._clock() + wait > deadline:
                return wait
            if SHUTTING_DOWN.wait(min(wait, max(0.01, deadline - self._clock()))):
                return wait  # 进程退出中,同 acquire_rpm

    def reconcile_tpm(self, *, estimated: int, actual: int) -> None:
        """请求完成后,按真实 token 数调整桶。
        actual < estimated → 退还; actual > estimated → 补扣(可能让桶变负)。
        """
        if not self.tpm:
            return
        delta = estimated - actual  # >0 表示多扣了应退还
        with self._lock:
            self._tpm_tokens = min(
                float(self._tpm_burst),
                self._tpm_tokens + delta,
            )


# ─── Wrapper ────────────────────────────────────────────────────


class RateLimitExhausted(RuntimeError):
    """限流桶在 timeout 内仍取不到 token —— 上层应捕获或选择降级。"""


def extract_total_tokens(resp: Any) -> Optional[int]:
    """从 OpenAI 兼容 response 提取 total_tokens,缺失返 None。

    覆盖以下形态:
    - dict 标准: resp['usage']['total_tokens']
    - openai SDK 1.x 对象: resp.usage.total_tokens
    - usage = None / 整个字段缺失 → None
    """
    if resp is None:
        return None
    # dict path
    if isinstance(resp, dict):
        usage = resp.get("usage")
        if isinstance(usage, dict):
            tt = usage.get("total_tokens")
            return int(tt) if isinstance(tt, (int, float)) else None
        return None
    # attr path
    usage = getattr(resp, "usage", None)
    if usage is None:
        return None
    tt = getattr(usage, "total_tokens", None)
    return int(tt) if isinstance(tt, (int, float)) else None


# Backward-compatible alias for callers predating the public helper name.
_extract_total_tokens = extract_total_tokens


class RateLimitedLLM:
    """组合 wrapper —— 把任意 LLM 调用函数包成"先 acquire,后 reconcile"。

    inner_call 必须是 ``(*, prompt: str, **kw) -> Any`` 形态的可调用对象,返回
    OpenAI 兼容的 response(dict 或 SDK 对象,含可选的 usage 字段)。本 wrapper
    不假设 inner 的内部实现,只检查 response.usage.total_tokens 做 reconcile。
    """

    def __init__(self, *, bucket: TokenBucket, inner_call: Callable[..., Any]):
        self.bucket = bucket
        self.inner_call = inner_call

    def call(self, *, prompt: str, timeout: float = 30.0, **kwargs) -> Any:
        """执行受限流的 LLM 调用。流程: acquire_rpm → estimate → acquire_tpm
        → inner_call(**kw) → reconcile_tpm by response.usage(缺失则保留估算)。
        """
        # 1) RPM acquire
        wait = self.bucket.acquire_rpm(timeout=timeout)
        if wait > 0:
            raise RateLimitExhausted(f"RPM bucket exhausted, need wait {wait:.1f}s")

        # 2) TPM 估算扣量
        estimated = estimate_tokens(prompt)
        wait = self.bucket.acquire_tpm(estimated, timeout=timeout)
        if wait > 0:
            raise RateLimitExhausted(f"TPM bucket exhausted, need wait {wait:.1f}s")

        # 3) 调用 inner
        resp = self.inner_call(prompt=prompt, **kwargs)

        # 4) reconcile by response.usage(缺失则保留估算扣量,不抛错)
        actual = extract_total_tokens(resp)
        if actual is not None:
            self.bucket.reconcile_tpm(estimated=estimated, actual=actual)

        return resp


# ─── 全局桶注册表 ────────────────────────────────────────────────
# 同一 base_url 共享同一桶 —— 避免 utils/llm 通路和 agno 通路各自一个桶
# 导致同 API key 的额度被双重消耗。
_BUCKETS: Dict[str, TokenBucket] = {}
_BUCKETS_LOCK = threading.Lock()


def get_or_create_bucket(
    base_url: str,
    *,
    rpm: Optional[int] = None,
    tpm: Optional[int] = None,
    burst: Optional[int] = None,
) -> TokenBucket:
    """按 base_url 取桶,不存在则新建。线程安全。"""
    with _BUCKETS_LOCK:
        if base_url not in _BUCKETS:
            _BUCKETS[base_url] = TokenBucket(rpm=rpm, tpm=tpm, burst=burst)
        return _BUCKETS[base_url]


def reset_buckets_for_testing() -> None:
    """测试用 —— 清空注册表,各测试间隔离。"""
    with _BUCKETS_LOCK:
        _BUCKETS.clear()


# pylint: disable=R0902  # 10 instance attrs 是 RPM+TPM 双桶 + 时钟 + 锁的自然结果
