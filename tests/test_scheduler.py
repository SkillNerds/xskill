"""块 6 调度器:命令/超时正确、失败落日志、串行不重叠、stop 及时。"""
from __future__ import annotations

import subprocess
import threading

import pytest

from xskill.pipeline.scheduler import IntervalSubprocessScheduler


def _sched(command=None, interval=0.01, timeout=5.0):
    return IntervalSubprocessScheduler(
        "test", command or ["true"], interval=interval, timeout=timeout,
    )


def test_rejects_nonpositive_interval_and_timeout():
    with pytest.raises(ValueError):
        _sched(interval=0)
    with pytest.raises(ValueError):
        _sched(timeout=-1)


def test_run_once_invokes_subprocess_with_command_and_timeout(monkeypatch):
    seen = {}

    def fake_run(command, **kwargs):
        seen["command"] = command
        seen["timeout"] = kwargs.get("timeout")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    _sched(command=["xskill", "sweep", "--once"], timeout=42.0)._run_once()
    assert seen["command"] == ["xskill", "sweep", "--once"]
    assert seen["timeout"] == 42.0


def test_nonzero_returncode_logs_warning(monkeypatch, caplog):
    def fake_run(command, **_kwargs):
        return subprocess.CompletedProcess(command, 1, "", "boom")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with caplog.at_level("WARNING"):
        _sched()._run_once()
    assert any("退出码" in record.getMessage() for record in caplog.records)


def test_timeout_logs_warning(monkeypatch, caplog):
    def fake_run(command, **kwargs):
        raise subprocess.TimeoutExpired(command, kwargs.get("timeout"))

    monkeypatch.setattr(subprocess, "run", fake_run)
    with caplog.at_level("WARNING"):
        _sched()._run_once()
    assert any("上限被杀" in record.getMessage() for record in caplog.records)


def test_spawn_failure_logs_warning(monkeypatch, caplog):
    def fake_run(_command, **_kwargs):
        raise OSError("no such executable")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with caplog.at_level("WARNING"):
        _sched()._run_once()
    assert any("启动子进程失败" in record.getMessage() for record in caplog.records)


def test_runs_are_serial_no_overlap(monkeypatch):
    """subprocess.run 阻塞 → 同一任务不可能自重叠:任一时刻并发数最多 1。"""
    concurrent = {"now": 0, "max": 0}
    lock = threading.Lock()
    gate = threading.Event()

    def fake_run(command, **_kwargs):
        with lock:
            concurrent["now"] += 1
            concurrent["max"] = max(concurrent["max"], concurrent["now"])
        gate.wait(0.05)
        with lock:
            concurrent["now"] -= 1
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    scheduler = _sched(interval=0.001)
    scheduler.start()
    gate.wait(0.2)
    scheduler.stop(timeout=2.0)
    assert concurrent["max"] == 1


def test_stop_joins_thread_promptly():
    scheduler = _sched(command=["true"], interval=100.0)  # 长周期,靠 stop 中断 wait
    scheduler.start()
    scheduler.stop(timeout=2.0)
    assert scheduler._thread is not None and not scheduler._thread.is_alive()


def test_persistent_mode_keeps_one_child_and_terminates_on_stop(monkeypatch):
    """轻量 ingest 常驻子进程不应每个 poll 反复启动或退出后泄漏。"""
    started = threading.Event()
    processes = []

    class FakeProcess:
        def __init__(self):
            self.terminated = False
            self.killed = False

        def poll(self):
            return 0 if self.terminated or self.killed else None

        def terminate(self):
            self.terminated = True

        def wait(self, timeout=None):
            del timeout
            return 0

        def kill(self):
            self.killed = True

    def fake_popen(_command, **_kwargs):
        process = FakeProcess()
        processes.append(process)
        started.set()
        return process

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    scheduler = IntervalSubprocessScheduler(
        "persistent-test",
        ["xskill", "ecosystem-ingest", "--loop"],
        interval=0.01,
        timeout=1.0,
        persistent=True,
    )

    scheduler.start()
    assert started.wait(1.0)
    scheduler.stop(timeout=1.0)

    assert len(processes) == 1
    assert processes[0].terminated
    assert scheduler._thread is not None
    assert not scheduler._thread.is_alive()


def test_subprocess_is_windowless_and_gbk_safe(monkeypatch):
    """回归:调度器每 poll_interval spawn 一次子进程。

    ① 不带无窗 flag → Windows 上 `xskill serve` 每 30s 弹一次 cmd 黑窗给用户;
    ② text=True 不带 errors → 中文 Windows 上子进程输出按 cp936 strict 解码,
       非法字节抛 UnicodeDecodeError,而 _run_once 只接 Timeout/OSError,
       异常会穿出去打死调度线程 → sweep/画像从此永不再跑。
    """
    captured: dict = {}

    def fake_run(cmd, **kwargs):
        captured.update(kwargs)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    _sched()._run_once()

    from xskill.utils.proc import windowless_subprocess_kwargs
    for key, value in windowless_subprocess_kwargs().items():
        assert captured.get(key) == value, f"子进程必须静默无窗:缺 {key}"
    assert captured.get("errors") == "replace", "解码必须带 errors,否则 GBK 打死调度线程"
    assert captured.get("encoding") == "utf-8"
    assert "text" not in captured, "显式 encoding 与 text=True 不可并存"


def test_loop_survives_run_once_exception(monkeypatch, caplog):
    """回归:_loop 无兜底时,_run_once 漏网异常会让 daemon 调度线程静默猝死,
    进程还活着但 sweep/画像再也不跑。异常必须被吞掉+落日志,下轮继续。"""
    calls: list[int] = []
    scheduler = _sched()

    def boom():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("首轮炸")
        scheduler.stop()

    monkeypatch.setattr(scheduler, "_run_once", boom)
    monkeypatch.setattr(scheduler._stop, "wait", lambda _t: scheduler._stop.is_set())
    with caplog.at_level("WARNING"):
        scheduler._loop()

    assert len(calls) >= 2, "首轮异常不该终结循环"
    assert any("异常" in r.message for r in caplog.records), "异常必须落日志,不能静默"
