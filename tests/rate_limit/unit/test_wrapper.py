"""RateLimitedLLM wrapper 测试 —— 不发 HTTP,用 stub 验证 acquire/reconcile 调用顺序。"""
from __future__ import annotations

import threading
from unittest.mock import MagicMock

import pytest

from xskill.utils.rate_limit import (
    RateLimitedLLM,
    SharedRequestLimiter,
    TokenBucket,
    get_or_create_bucket,
    get_or_create_request_limiter,
    reset_buckets_for_testing,
)


@pytest.fixture(autouse=True)
def _isolate_buckets():
    reset_buckets_for_testing()
    yield
    reset_buckets_for_testing()


def test_wrapper_calls_acquire_before_inner():
    bucket = TokenBucket(rpm=60, tpm=1000, burst=10)
    inner = MagicMock(return_value={
        "choices": [{"message": {"content": "hi"}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    })
    wrapper = RateLimitedLLM(bucket=bucket, inner_call=inner)

    wrapper.call(prompt="hello world", timeout=5.0)

    assert inner.called
    # 调用后 RPM 桶减 1
    assert bucket._rpm_tokens == 9  # burst 10 - 1


def test_wrapper_reconciles_tpm_from_usage():
    bucket = TokenBucket(tpm=1000, burst=500)
    inner = MagicMock(return_value={
        "choices": [{"message": {"content": "ok"}}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
    })
    wrapper = RateLimitedLLM(bucket=bucket, inner_call=inner)

    # "hello" 估算大概 2,但 actual 是 150 → reconcile 补扣 148
    # 校准后桶应该 = 500 - 150 = 350(±10 估算误差)
    wrapper.call(prompt="hello", timeout=5.0)
    assert 340 <= bucket._tpm_tokens <= 360


def test_wrapper_falls_back_when_usage_missing():
    """response 无 usage 时(vLLM 常见),桶按估算扣量不抛错。"""
    bucket = TokenBucket(tpm=1000, burst=500)
    inner = MagicMock(return_value={
        "choices": [{"message": {"content": "ok"}}],
        # 故意不带 usage
    })
    wrapper = RateLimitedLLM(bucket=bucket, inner_call=inner)
    wrapper.call(prompt="hello", timeout=5.0)
    # 桶减少了估算的量(不为 500)
    assert bucket._tpm_tokens < 500


def test_get_or_create_bucket_is_shared_per_base_url():
    b1 = get_or_create_bucket("https://api.example.com", rpm=60, tpm=1000)
    b2 = get_or_create_bucket("https://api.example.com", rpm=60, tpm=1000)
    assert b1 is b2  # 同 URL 共享同一桶
    b3 = get_or_create_bucket("https://other.example.com", rpm=60, tpm=1000)
    assert b3 is not b1


def test_request_limiter_is_shared_per_kind_and_base_url():
    first = get_or_create_request_limiter(
        "llm", "https://api.example.com", max_inflight=2
    )
    second = get_or_create_request_limiter(
        "llm", "https://api.example.com", max_inflight=2
    )
    embedding = get_or_create_request_limiter(
        "embedding", "https://api.example.com", max_inflight=2
    )

    assert first is second
    assert embedding is not first


def test_shared_request_limiter_caps_inflight_calls():
    limiter = SharedRequestLimiter(max_inflight=2)
    release = threading.Event()
    two_entered = threading.Event()
    lock = threading.Lock()
    active = 0
    max_active = 0

    def inner_call():
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
            if active == 2:
                two_entered.set()
        assert release.wait(timeout=2)
        with lock:
            active -= 1
        return {}

    threads = [
        threading.Thread(
            target=lambda: limiter.call(prompt="hello", inner_call=inner_call)
        )
        for _ in range(5)
    ]
    for thread in threads:
        thread.start()

    assert two_entered.wait(timeout=2)
    assert max_active == 2
    release.set()
    for thread in threads:
        thread.join(timeout=2)
        assert not thread.is_alive()
    assert max_active == 2
