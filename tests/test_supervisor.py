"""supervisor watchdog 单测：退避、respawn、state 回写、防双跑。

不起真进程——spawn/monotonic/sleep 全部注入替身；真实进程级验证见
tests/e2e/test_supervised_selfheal_e2e.py。
"""
from __future__ import annotations

import os

import xskill.team.client.service as svc
import xskill.team.client.supervisor as sup


def test_next_backoff_progression_and_cap():
    b = sup.BACKOFF_INITIAL
    seq = []
    for _ in range(12):
        b = sup.next_backoff(b, child_runtime=5.0)
        seq.append(b)
    assert seq[0] == sup.BACKOFF_INITIAL * sup.BACKOFF_FACTOR
    assert seq[-1] == sup.BACKOFF_CAP           # 持续崩溃封顶，不无限翻倍
    assert all(x <= sup.BACKOFF_CAP for x in seq)


def test_next_backoff_resets_after_healthy_run():
    assert sup.next_backoff(sup.BACKOFF_CAP,
                            child_runtime=sup.HEALTHY_RUNTIME) == sup.BACKOFF_INITIAL


class _FakeChild:
    """poll() 立即返回退出码的假子进程。"""

    def __init__(self, pid: int, returncode: int = 1):
        self.pid = pid
        self.returncode = returncode

    def poll(self):
        return self.returncode

    def terminate(self):
        pass

    def kill(self):
        pass

    def wait(self, timeout=None):
        return self.returncode


def test_supervisor_respawns_crashed_child_and_writes_state(tmp_path, monkeypatch):
    state_path = tmp_path / "connect_daemon.json"
    monkeypatch.setattr(svc, "get_connect_daemon_state_path", lambda: state_path)

    spawned: list[_FakeChild] = []

    def fake_spawn():
        child = _FakeChild(pid=1000 + len(spawned))
        spawned.append(child)
        return child

    s = sup.Supervisor(spawn=fake_spawn, sleep=None)

    sleeps: list[float] = []

    def fake_sleep(seconds: float):
        sleeps.append(seconds)
        if len(sleeps) >= 3:          # 三次退避后请求停止
            s._stop_requested = True

    s._sleep = fake_sleep
    assert s.run() == 0

    assert len(spawned) == 3                       # 崩 3 次拉 3 次
    assert sleeps == [2.0, 4.0, 8.0]               # 指数退避（起步 1s 已 ×2）
    state = svc.read_daemon_state()
    assert state["watchdog_pid"] == os.getpid()
    assert state["child_pid"] == spawned[-1].pid   # 每次 spawn 都回写


def test_supervisor_refuses_duplicate_watchdog(tmp_path, monkeypatch):
    state_path = tmp_path / "connect_daemon.json"
    monkeypatch.setattr(svc, "get_connect_daemon_state_path", lambda: state_path)
    svc.write_daemon_state(method="supervised", watchdog_pid=12345)
    monkeypatch.setattr(svc, "_pid_alive", lambda pid: pid == 12345)

    spawned = []
    s = sup.Supervisor(spawn=lambda: spawned.append(1))
    assert s.run() == 0
    assert spawned == []                           # 已有 watchdog，幂等退出
    assert svc.read_daemon_state()["watchdog_pid"] == 12345   # 不抢占


def test_supervisor_spawn_failure_backs_off_instead_of_exiting(tmp_path,
                                                               monkeypatch):
    """spawn 本身 OSError（fd 耗尽等瞬态）也走退避重试，watchdog 不退出。"""
    state_path = tmp_path / "connect_daemon.json"
    monkeypatch.setattr(svc, "get_connect_daemon_state_path", lambda: state_path)

    attempts = []

    def failing_spawn():
        attempts.append(1)
        raise OSError("too many open files")

    s = sup.Supervisor(spawn=failing_spawn)
    sleeps = []

    def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) >= 2:
            s._stop_requested = True

    s._sleep = fake_sleep
    assert s.run() == 0
    assert len(attempts) == 2
