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
import contextlib
import contextvars
import threading
import time
import unicodedata
from collections import defaultdict, deque
from functools import partial
from typing import Any, Callable, Dict, Optional

from xskill.utils.shutdown import SHUTTING_DOWN


_REQUEST_SOURCE: contextvars.ContextVar[str] = contextvars.ContextVar(
    "xskill_request_source", default="other",
)

GENERATE_SOURCE = "generate"
_LIVE_LLM_POOL_WEIGHTS: Optional[dict[str, int]] = None


@contextlib.contextmanager
def request_source(source: str):
    """Identify the agent-worker pool issuing requests in this context."""
    token = _REQUEST_SOURCE.set(source)
    try:
        yield
    finally:
        _REQUEST_SOURCE.reset(token)


def current_request_source() -> str:
    return _REQUEST_SOURCE.get()


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
        request_burst: Optional[int] = None,
        token_burst: Optional[int] = None,
        clock: Optional[Callable[[], float]] = None,
    ):
        self.rpm = rpm
        self.tpm = tpm
        # ``burst`` is retained for the public Python API.  Config validation
        # rejects it and requires the unambiguous request/token names.
        if request_burst is None:
            request_burst = burst
        if token_burst is None:
            token_burst = burst
        self._rpm_burst = (
            request_burst
            if request_burst is not None
            else (max(1, rpm // 6) if rpm else 0)
        )
        self._tpm_burst = (
            token_burst
            if token_burst is not None
            else (max(1, tpm // 6) if tpm else 0)
        )
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


class _WeightedInflightGate:
    """Weighted, work-conserving semaphore for LLM HTTP calls."""

    def __init__(self, limit: int, weights: Optional[dict[str, int]] = None):
        self.limit = int(limit)
        self.weights = {
            name: max(1, int(weight))
            for name, weight in (weights or {}).items()
        }
        self._condition = threading.Condition()
        self._waiting: dict[str, deque[threading.Event]] = defaultdict(deque)
        self._scores: dict[str, int] = defaultdict(int)
        self._active = 0

    def set_weights(self, weights: Optional[dict[str, int]] = None) -> None:
        """Replace split/cluster/edit weights. generate 不参与配额比。"""
        with self._condition:
            self.weights = {
                name: max(1, int(weight))
                for name, weight in (weights or {}).items()
                if name != GENERATE_SOURCE
            }
            self._grant_locked()

    def _select_locked(self) -> str:
        if self._waiting.get(GENERATE_SOURCE):
            return GENERATE_SOURCE
        active_sources = [
            name for name, queue in self._waiting.items()
            if queue and name != GENERATE_SOURCE
        ]
        total_weight = sum(self.weights.get(name, 1) for name in active_sources)
        for name in active_sources:
            self._scores[name] += self.weights.get(name, 1)
        selected = max(active_sources, key=self._scores.__getitem__)
        self._scores[selected] -= total_weight
        return selected

    def _grant_locked(self) -> None:
        while self._active < self.limit and any(self._waiting.values()):
            source = self._select_locked()
            ticket = self._waiting[source].popleft()
            self._active += 1
            ticket.set()

    def acquire(self, source: str) -> None:
        ticket = threading.Event()
        with self._condition:
            self._waiting[source].append(ticket)
            self._grant_locked()
        # Wait on a ticket rather than polling the watcher; no HTTP slot is
        # counted as in flight until this admission has also passed RPM/TPM.
        ticket.wait()

    def release(self) -> None:
        with self._condition:
            self._active -= 1
            self._grant_locked()

    @property
    def active(self) -> int:
        with self._condition:
            return self._active

    @property
    def waiting(self) -> int:
        with self._condition:
            return sum(len(queue) for queue in self._waiting.values())


class SharedRequestLimiter:
    """Shared RPM/TPM and weighted in-flight limit for one endpoint."""

    def __init__(
        self,
        *,
        bucket: TokenBucket | None = None,
        rpm: Optional[int] = None,
        tpm: Optional[int] = None,
        request_burst: Optional[int] = None,
        token_burst: Optional[int] = None,
        max_inflight: Optional[int] = None,
        weights: Optional[dict[str, int]] = None,
    ):
        self.bucket = bucket or TokenBucket(
            rpm=rpm, tpm=tpm,
            request_burst=request_burst, token_burst=token_burst,
        )
        self._gate = (
            _WeightedInflightGate(int(max_inflight), weights)
            if max_inflight
            else None
        )
        self._lock = threading.Lock()
        self._inflight = 0
        self._rate_limit_waiting = 0
        self._retry_waiting = 0

    def call(
        self,
        *,
        prompt: str,
        inner_call: Callable[[], Any],
        timeout: float = 60.0,
    ) -> Any:
        estimated = estimate_tokens(prompt)
        gate_acquired = False
        if self._gate is not None:
            # Admission happens before RPM/TPM acquisition so the configured
            # pool weights govern request opportunities, not only calls that
            # happened to obtain a token first.
            self._gate.acquire(current_request_source())
            gate_acquired = True
        with self._lock:
            self._rate_limit_waiting += 1
        try:
            wait = self.bucket.acquire_rpm(timeout=timeout)
            if wait > 0:
                raise RateLimitExhausted(
                    f"RPM bucket exhausted, need wait {wait:.1f}s"
                )
            wait = self.bucket.acquire_tpm(estimated, timeout=timeout)
            if wait > 0:
                raise RateLimitExhausted(
                    f"TPM bucket exhausted, need wait {wait:.1f}s"
                )
        except BaseException:
            if gate_acquired:
                self._gate.release()
            raise
        finally:
            with self._lock:
                self._rate_limit_waiting -= 1
        with self._lock:
            self._inflight += 1
        try:
            response = inner_call()
        finally:
            with self._lock:
                self._inflight -= 1
            if gate_acquired:
                self._gate.release()
        actual = extract_total_tokens(response)
        if actual is not None:
            self.bucket.reconcile_tpm(estimated=estimated, actual=actual)
        return response

    def begin_retry_wait(self) -> None:
        with self._lock:
            self._retry_waiting += 1

    def end_retry_wait(self) -> None:
        with self._lock:
            self._retry_waiting -= 1

    @property
    def status(self) -> dict[str, int]:
        with self._lock:
            inflight = self._inflight
            rate_waiting = self._rate_limit_waiting
            retry_waiting = self._retry_waiting
        return {
            "inflight": inflight,
            "waiting": self._gate.waiting if self._gate is not None else 0,
            "rate_limit_waiting": rate_waiting,
            "retry_waiting": retry_waiting,
        }


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

    def __init__(
        self,
        *,
        bucket: TokenBucket | None = None,
        limiter: SharedRequestLimiter | None = None,
        inner_call: Callable[..., Any],
    ):
        if bucket is None and limiter is None:
            raise ValueError("bucket 或 limiter 至少提供一个")
        self.bucket = bucket
        self.limiter = limiter
        self.inner_call = inner_call

    def call(self, *, prompt: str, timeout: float = 30.0, **kwargs) -> Any:
        """执行受限流的 LLM 调用。流程: acquire_rpm → estimate → acquire_tpm
        → inner_call(**kw) → reconcile_tpm by response.usage(缺失则保留估算)。
        """
        if self.limiter is not None:
            return self.limiter.call(
                prompt=prompt,
                inner_call=partial(self.inner_call, prompt=prompt, **kwargs),
                timeout=timeout,
            )

        # Backward-compatible direct bucket path.
        assert self.bucket is not None
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
_LIMITERS: Dict[tuple[str, str], SharedRequestLimiter] = {}
_LIMITERS_LOCK = threading.Lock()


def get_or_create_bucket(
    base_url: str,
    *,
    rpm: Optional[int] = None,
    tpm: Optional[int] = None,
    burst: Optional[int] = None,
    request_burst: Optional[int] = None,
    token_burst: Optional[int] = None,
) -> TokenBucket:
    """按 base_url 取桶,不存在则新建。线程安全。"""
    with _BUCKETS_LOCK:
        if base_url not in _BUCKETS:
            _BUCKETS[base_url] = TokenBucket(
                rpm=rpm,
                tpm=tpm,
                burst=burst,
                request_burst=request_burst,
                token_burst=token_burst,
            )
        return _BUCKETS[base_url]


def get_or_create_request_limiter(
    kind: str,
    base_url: str,
    *,
    rpm: Optional[int] = None,
    tpm: Optional[int] = None,
    request_burst: Optional[int] = None,
    token_burst: Optional[int] = None,
    max_inflight: Optional[int] = None,
    weights: Optional[dict[str, int]] = None,
) -> SharedRequestLimiter:
    """Return the process-wide limiter shared by one request endpoint."""
    key = (kind, base_url)
    effective_weights = dict(weights or {})
    if kind == "llm" and _LIVE_LLM_POOL_WEIGHTS is not None:
        effective_weights.update(_LIVE_LLM_POOL_WEIGHTS)
    with _LIMITERS_LOCK:
        if key not in _LIMITERS:
            bucket = None
            if kind == "llm":
                bucket = get_or_create_bucket(
                    base_url,
                    rpm=rpm,
                    tpm=tpm,
                    request_burst=request_burst,
                    token_burst=token_burst,
                )
            _LIMITERS[key] = SharedRequestLimiter(
                bucket=bucket,
                rpm=rpm,
                tpm=tpm,
                request_burst=request_burst,
                token_burst=token_burst,
                max_inflight=max_inflight,
                weights=effective_weights,
            )
        limiter = _LIMITERS[key]
        if (
            kind == "llm"
            and limiter._gate is not None
            and effective_weights
        ):
            limiter._gate.set_weights(effective_weights)
        return limiter


def request_limiter_status(kind: str) -> dict[str, int]:
    """Aggregate live counters for status endpoints."""
    with _LIMITERS_LOCK:
        limiters = [
            limiter
            for (limiter_kind, _), limiter in _LIMITERS.items()
            if limiter_kind == kind
        ]
    statuses = [limiter.status for limiter in limiters]
    keys = ("inflight", "waiting", "rate_limit_waiting", "retry_waiting")
    return {key: sum(status.get(key, 0) for status in statuses) for key in keys}


def begin_retry_wait(kind: str, base_url: str) -> None:
    with _LIMITERS_LOCK:
        limiter = _LIMITERS.get((kind, base_url))
    if limiter is not None:
        limiter.begin_retry_wait()


def end_retry_wait(kind: str, base_url: str) -> None:
    with _LIMITERS_LOCK:
        limiter = _LIMITERS.get((kind, base_url))
    if limiter is not None:
        limiter.end_retry_wait()


def set_llm_pool_weights(weights: dict[str, int]) -> None:
    """Hot-update split/cluster/edit LLM weights on every live llm limiter."""
    global _LIVE_LLM_POOL_WEIGHTS
    cleaned = {
        name: max(1, int(weight))
        for name, weight in weights.items()
        if name != GENERATE_SOURCE
    }
    _LIVE_LLM_POOL_WEIGHTS = cleaned
    with _LIMITERS_LOCK:
        limiters = [
            limiter
            for (kind, _), limiter in _LIMITERS.items()
            if kind == "llm"
        ]
    for limiter in limiters:
        if limiter._gate is not None:
            limiter._gate.set_weights(cleaned)


def reset_buckets_for_testing() -> None:
    """测试用 —— 清空注册表,各测试间隔离。"""
    global _LIVE_LLM_POOL_WEIGHTS
    _LIVE_LLM_POOL_WEIGHTS = None
    with _LIMITERS_LOCK:
        _LIMITERS.clear()
    with _BUCKETS_LOCK:
        _BUCKETS.clear()


# pylint: disable=R0902  # 10 instance attrs 是 RPM+TPM 双桶 + 时钟 + 锁的自然结果
