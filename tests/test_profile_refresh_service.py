"""ProfileRefreshService 的并发、失败和停机行为。"""
from __future__ import annotations

from dataclasses import dataclass
import threading
import time

import pytest

from xskill.team.server.profile_refresh import ProfileRefreshService

# Windows CI runners are often slower to schedule/join worker threads than
# local laptops; keep the contract but use a wider idle/stop budget.
_IDLE_TIMEOUT = 10
_EVENT_TIMEOUT = 5


@dataclass
class _Result:
    changed: bool = True
    embed_items: int = 1
    cancelled: bool = False


class _ImmediateEngine:
    def __init__(self):
        self.calls: list[str] = []

    def update_user_interest(self, interest, *, should_commit=None):
        self.calls.append(interest.user_id)
        return _Result()


@pytest.mark.flaky(reruns=2, reruns_delay=1)
def test_queued_requests_coalesce_and_queue_full_is_nonblocking():
    engine = _ImmediateEngine()
    service = ProfileRefreshService(
        engine, workers=1, queue_size=1, autostart=False,
    )
    assert service.request("u1") is True
    assert service.request("u1") is True
    assert service.request("u2") is False
    assert service.metrics["queued"] == 1
    assert service.metrics["coalesced"] == 1
    assert service.metrics["queue_full"] == 1
    service.start()
    assert service.wait_idle(timeout=_IDLE_TIMEOUT)
    assert engine.calls == ["u1"]
    assert service.stop(timeout=_IDLE_TIMEOUT)


@pytest.mark.flaky(reruns=2, reruns_delay=1)
def test_running_request_causes_at_most_one_rerun():
    first_started = threading.Event()
    release_first = threading.Event()
    second_started = threading.Event()

    class Engine:
        def __init__(self):
            self.calls = 0

        def update_user_interest(self, _interest, *, should_commit=None):
            self.calls += 1
            if self.calls == 1:
                first_started.set()
                assert release_first.wait(_EVENT_TIMEOUT)
            else:
                second_started.set()
            return _Result(changed=self.calls == 1, embed_items=self.calls == 1)

    engine = Engine()
    service = ProfileRefreshService(engine, workers=1, queue_size=1)
    assert service.request("u1")
    assert first_started.wait(_EVENT_TIMEOUT)
    assert service.request("u1")
    assert service.request("u1")
    release_first.set()
    assert second_started.wait(_EVENT_TIMEOUT)
    assert service.wait_idle(timeout=_IDLE_TIMEOUT)
    assert engine.calls == 2
    metrics = service.metrics
    assert metrics["rerun"] == 1
    assert metrics["coalesced"] == 2
    assert metrics["completed"] == 1
    assert metrics["unchanged"] == 1
    assert metrics["embed_items"] == 1
    assert service.stop(timeout=_IDLE_TIMEOUT)


@pytest.mark.flaky(reruns=2, reruns_delay=1)
def test_failure_clears_state_and_later_request_retries():
    class Engine:
        def __init__(self):
            self.calls = 0

        def update_user_interest(self, _interest, *, should_commit=None):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("backend down")
            return _Result(embed_items=3)

    engine = Engine()
    service = ProfileRefreshService(engine, workers=1, queue_size=1)
    assert service.request("u1")
    assert service.wait_idle(timeout=_IDLE_TIMEOUT)
    assert service.metrics["failed"] == 1
    assert service.request("u1")
    assert service.wait_idle(timeout=_IDLE_TIMEOUT)
    assert engine.calls == 2
    assert service.metrics["completed"] == 1
    assert service.metrics["embed_items"] == 3
    assert service.stop(timeout=_IDLE_TIMEOUT)


def test_completion_callback_only_reports_successful_commits():
    outcomes = []

    class Engine:
        def __init__(self):
            self.calls = 0

        def update_user_interest(self, _interest, *, should_commit=None):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("temporary failure")
            return _Result(changed=False)

    service = ProfileRefreshService(
        Engine(),
        workers=1,
        queue_size=1,
        on_processed=lambda client_id, succeeded: outcomes.append(
            (client_id, succeeded),
        ),
    )
    assert service.request("u1")
    assert service.wait_idle(timeout=_IDLE_TIMEOUT)
    assert service.request("u1")
    assert service.wait_idle(timeout=_IDLE_TIMEOUT)
    assert outcomes == [("u1", False), ("u1", True)]
    assert service.stop(timeout=_IDLE_TIMEOUT)


@pytest.mark.flaky(reruns=2, reruns_delay=1)
def test_worker_concurrency_is_fixed_and_threads_are_daemon():
    all_workers_busy = threading.Event()
    release = threading.Event()

    class Engine:
        def __init__(self):
            self.lock = threading.Lock()
            self.active = 0
            self.max_active = 0

        def update_user_interest(self, _interest, *, should_commit=None):
            with self.lock:
                self.active += 1
                self.max_active = max(self.max_active, self.active)
                if self.active == 2:
                    all_workers_busy.set()
            assert release.wait(_EVENT_TIMEOUT)
            with self.lock:
                self.active -= 1
            return _Result()

    engine = Engine()
    service = ProfileRefreshService(engine, workers=2, queue_size=6)
    for index in range(6):
        assert service.request(f"u{index}")
    assert all_workers_busy.wait(_EVENT_TIMEOUT)
    assert engine.max_active == 2
    assert len(service._threads) == 2
    assert all(thread.daemon for thread in service._threads)
    release.set()
    assert service.wait_idle(timeout=_IDLE_TIMEOUT)
    assert engine.max_active == 2
    assert service.stop(timeout=_IDLE_TIMEOUT)


def test_settle_delay_keeps_profile_work_behind_sync_burst():
    entered = threading.Event()
    entered_at: list[float] = []

    class Engine:
        def update_user_interest(self, _interest, *, should_commit=None):
            entered_at.append(time.monotonic())
            entered.set()
            return _Result()

    service = ProfileRefreshService(
        Engine(), workers=2, queue_size=4, settle_delay=0.15,
    )
    started = time.monotonic()
    assert service.request("u1")
    assert service.request("u2")
    assert entered.wait(_EVENT_TIMEOUT)
    assert entered_at[0] - started >= 0.14
    assert service.wait_idle(timeout=_IDLE_TIMEOUT)
    assert service.stop(timeout=_IDLE_TIMEOUT)


@pytest.mark.flaky(reruns=2, reruns_delay=1)
def test_stop_cancels_queued_work_and_has_bounded_join():
    entered = threading.Event()
    release = threading.Event()

    class Engine:
        def update_user_interest(self, _interest, *, should_commit=None):
            entered.set()
            assert release.wait(5)
            return _Result()

    service = ProfileRefreshService(Engine(), workers=1, queue_size=2)
    assert service.request("running")
    assert entered.wait(5)
    assert service.request("queued")
    assert service.stop(timeout=0) is False
    assert service.metrics["queued"] == 0
    assert service.request("rejected") is False
    release.set()
    # Windows CI runners can be slow to join after release; keep the
    # bounded-join contract but allow a wider deadline than local laptops.
    assert service.stop(timeout=10) is True
    assert service.wait_idle(timeout=5)


def test_stop_exits_all_workers_with_queue_size_one():
    service = ProfileRefreshService(
        _ImmediateEngine(), workers=4, queue_size=1,
    )

    assert service.stop(timeout=2) is True
    assert all(not thread.is_alive() for thread in service._threads)


def test_stop_during_backend_call_prevents_profile_commit():
    entered = threading.Event()
    release = threading.Event()
    writes: list[str] = []

    class Engine:
        def update_user_interest(self, interest, *, should_commit=None):
            entered.set()
            assert release.wait(2)
            allowed = should_commit is None or should_commit()
            if allowed:
                writes.append(interest.user_id)
            return _Result(
                changed=allowed,
                embed_items=1,
                cancelled=not allowed,
            )

    service = ProfileRefreshService(Engine(), workers=1, queue_size=1)
    assert service.request("u1")
    assert entered.wait(2)
    assert service.stop(timeout=0) is False
    release.set()
    assert service.stop(timeout=2) is True

    assert writes == []
    assert service.metrics["cancelled"] == 1
    # embedding 已经实际发生，即使停止后没有落库，仍要计入消耗。
    assert service.metrics["embed_items"] == 1
