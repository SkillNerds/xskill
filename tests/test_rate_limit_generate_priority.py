"""generate 在并发闸里硬优先于拆分、归类、编辑。"""
from __future__ import annotations

import threading
import time

from xskill.utils.rate_limit import (
    GENERATE_SOURCE,
    _WeightedInflightGate,
    reset_buckets_for_testing,
    set_llm_pool_weights,
)


def setup_function():
    reset_buckets_for_testing()


def teardown_function():
    reset_buckets_for_testing()


def test_generate_is_served_before_heavier_split_weight():
    gate = _WeightedInflightGate(1, {"split": 100, "cluster": 50, "edit": 1})
    occupant_started = threading.Event()
    occupant_release = threading.Event()
    split_started = threading.Event()
    generate_started = threading.Event()
    split_release = threading.Event()
    generate_release = threading.Event()
    order: list[str] = []

    def occupy():
        gate.acquire("split")
        occupant_started.set()
        occupant_release.wait(2)
        gate.release()

    def waiter(source, started, release):
        gate.acquire(source)
        order.append(source)
        started.set()
        release.wait(2)
        gate.release()

    holder = threading.Thread(target=occupy)
    split_t = threading.Thread(
        target=waiter, args=("split", split_started, split_release),
    )
    gen_t = threading.Thread(
        target=waiter, args=(GENERATE_SOURCE, generate_started, generate_release),
    )
    holder.start()
    assert occupant_started.wait(1)
    split_t.start()
    time.sleep(0.05)
    gen_t.start()
    time.sleep(0.05)
    occupant_release.set()
    assert generate_started.wait(1)
    assert not split_started.is_set()
    generate_release.set()
    assert split_started.wait(1)
    split_release.set()
    holder.join(2)
    split_t.join(2)
    gen_t.join(2)
    assert order == [GENERATE_SOURCE, "split"]


def test_set_llm_pool_weights_updates_live_gate():
    gate = _WeightedInflightGate(1, {"split": 1, "edit": 1})
    set_llm_pool_weights({"split": 9, "cluster": 3, "edit": 2})
    # 模块级权重给后续 limiter 用；这个闸要显式 set_weights 才会变。
    gate.set_weights({"split": 9, "edit": 2})
    assert gate.weights["split"] == 9
    assert gate.weights["edit"] == 2
    assert GENERATE_SOURCE not in gate.weights
