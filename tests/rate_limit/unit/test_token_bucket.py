"""TokenBucket 纯算法测试 —— 注入 clock callable 保证确定性,不 sleep。"""
from __future__ import annotations

import threading


from xskill.utils.rate_limit import TokenBucket


class FakeClock:
    """单调递增的假时钟,测试可控制 advance。"""
    def __init__(self, start: float = 1000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


# ─── RPM bucket ──────────────────────────────────────────────

def test_rpm_bucket_starts_full_with_burst():
    clock = FakeClock()
    bucket = TokenBucket(rpm=60, burst=10, clock=clock)
    # 桶初始满 = burst 容量,前 10 个 acquire 不阻塞
    for _ in range(10):
        wait = bucket.acquire_rpm(timeout=0)
        assert wait == 0


def test_rpm_bucket_blocks_when_empty():
    clock = FakeClock()
    bucket = TokenBucket(rpm=60, burst=2, clock=clock)
    bucket.acquire_rpm(timeout=0)
    bucket.acquire_rpm(timeout=0)
    # 桶空,timeout=0 立刻报需要等多久
    wait = bucket.acquire_rpm(timeout=0)
    # rpm=60 → 1 token/sec → 等 1s 才有下一个 token
    assert 0.5 <= wait <= 1.5


def test_rpm_bucket_refills_over_time():
    clock = FakeClock()
    bucket = TokenBucket(rpm=60, burst=1, clock=clock)
    bucket.acquire_rpm(timeout=0)
    # 推进 2 秒,应该补回 2 个 token(但桶容量 1,封顶)
    clock.advance(2.0)
    wait = bucket.acquire_rpm(timeout=0)
    assert wait == 0


def test_zero_rpm_means_unlimited():
    bucket = TokenBucket(rpm=None)
    # 不阻塞任何调用
    for _ in range(1000):
        assert bucket.acquire_rpm(timeout=0) == 0


# ─── TPM bucket ──────────────────────────────────────────────

def test_tpm_bucket_consumes_n_tokens_per_call():
    clock = FakeClock()
    bucket = TokenBucket(tpm=1000, burst=200, clock=clock)
    # 一次扣 150 token,然后再扣 50 应该不阻塞(200 burst)
    wait = bucket.acquire_tpm(150, timeout=0)
    assert wait == 0
    wait = bucket.acquire_tpm(50, timeout=0)
    assert wait == 0
    # 再扣 100 应该需要等待 —— burst 已用完
    wait = bucket.acquire_tpm(100, timeout=0)
    assert wait > 0


def test_tpm_reconcile_returns_overcharge():
    clock = FakeClock()
    bucket = TokenBucket(tpm=1000, burst=200, clock=clock)
    bucket.acquire_tpm(150, timeout=0)  # 桶 = 50
    # 实际只用了 100,退还 50
    bucket.reconcile_tpm(estimated=150, actual=100)
    # 现在桶应能继续扣 100(50 退回 + 50 剩余)
    wait = bucket.acquire_tpm(100, timeout=0)
    assert wait == 0


def test_request_and_token_burst_are_independent():
    bucket = TokenBucket(
        rpm=60,
        tpm=600,
        request_burst=2,
        token_burst=30,
    )

    assert bucket._rpm_burst == 2
    assert bucket._tpm_burst == 30
    assert bucket.acquire_rpm(timeout=0) == 0
    assert bucket.acquire_rpm(timeout=0) == 0
    assert bucket.acquire_rpm(timeout=0) > 0
    assert bucket.acquire_tpm(30, timeout=0) == 0
    assert bucket.acquire_tpm(1, timeout=0) > 0


def test_explicit_bursts_override_legacy_burst_independently():
    bucket = TokenBucket(
        rpm=60,
        tpm=600,
        burst=99,
        request_burst=3,
        token_burst=40,
    )

    assert bucket._rpm_burst == 3
    assert bucket._tpm_burst == 40


# ─── Concurrency ─────────────────────────────────────────────

def test_concurrent_acquire_no_double_spend():
    """50 个线程同时 acquire 1 RPM,总扣量必须 = 50。"""
    clock = FakeClock()
    bucket = TokenBucket(rpm=600, burst=100, clock=clock)
    threads = []
    for _ in range(50):
        t = threading.Thread(target=lambda: bucket.acquire_rpm(timeout=0))
        threads.append(t)
        t.start()
    for t in threads:
        t.join()
    # burst=100,扣完 50 应该还剩 50
    assert 49 <= bucket._rpm_tokens <= 51  # ±1 浮点误差容忍
