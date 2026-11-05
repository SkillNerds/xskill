"""块 6 调度器:命令/超时正确、失败落日志、串行不重叠、stop 及时。"""
from __future__ import annotations

import subprocess
import sys
import threading
import time

import pytest

from xskill.pipeline.scheduler import IntervalSubprocessScheduler


def _sched(command=None, interval=0.01, timeout=5.0):
    return IntervalSubprocessScheduler(
        "test", command or ["true"], interval=interval, timeout=timeout,
    )


class _FakeProcess:
    def __init__(self, *, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.communicate_timeout = None
        self.terminated = False
        self.killed = False

    def poll(self):
        return self.returncode

    def communicate(self, timeout=None):
        self.communicate_timeout = timeout
        return self.stdout, self.stderr

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def kill(self):
        self.killed = True
        self.returncode = -9

    def wait(self, timeout=None):
        del timeout
        return self.returncode


def test_rejects_nonpositive_interval_and_timeout():
    with pytest.raises(ValueError):
        _sched(interval=0)
    with pytest.raises(ValueError):
        _sched(timeout=-1)


def test_run_once_invokes_subprocess_with_command_and_timeout(monkeypatch):
    seen = {}
    process = _FakeProcess()

    def fake_popen(command, **kwargs):
        seen["command"] = command
        seen["kwargs"] = kwargs
        return process

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    _sched(command=["xskill", "profile-refresh"], timeout=42.0)._run_once()
    assert seen["command"] == ["xskill", "profile-refresh"]
    assert process.communicate_timeout == 42.0
    assert seen["kwargs"]["stdout"] is subprocess.PIPE
    assert seen["kwargs"]["stderr"] is subprocess.PIPE


def test_nonzero_returncode_logs_warning(monkeypatch, caplog):
    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda _command, **_kwargs: _FakeProcess(returncode=1, stderr="boom"),
    )
    with caplog.at_level("WARNING"):
        _sched()._run_once()
    assert any("退出码" in record.getMessage() for record in caplog.records)


def test_timeout_logs_warning(monkeypatch, caplog):
    class TimeoutProcess(_FakeProcess):
        def __init__(self):
            super().__init__(returncode=None)
            self.communicate_calls = 0

        def communicate(self, timeout=None):
            self.communicate_calls += 1
            self.communicate_timeout = timeout
            if self.communicate_calls == 1:
                raise subprocess.TimeoutExpired(["true"], timeout)
            return "", ""

    process = TimeoutProcess()
    monkeypatch.setattr(subprocess, "Popen", lambda *_args, **_kwargs: process)
    with caplog.at_level("WARNING"):
        _sched()._run_once()
    assert process.killed
    assert process.communicate_calls == 2
    assert any("上限被杀" in record.getMessage() for record in caplog.records)


def test_spawn_failure_logs_warning(monkeypatch, caplog):
    def fake_popen(_command, **_kwargs):
        raise OSError("no such executable")

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    with caplog.at_level("WARNING"):
        _sched()._run_once()
    assert any("启动子进程失败" in record.getMessage() for record in caplog.records)


def test_runs_are_serial_no_overlap(monkeypatch):
    """调度循环同步等待 _run_once → 同一任务不可能自重叠。"""
    concurrent = {"now": 0, "max": 0}
    lock = threading.Lock()
    gate = threading.Event()
    scheduler = _sched(interval=0.001)

    def fake_run_once():
        with lock:
            concurrent["now"] += 1
            concurrent["max"] = max(concurrent["max"], concurrent["now"])
        gate.wait(0.05)
        with lock:
            concurrent["now"] -= 1

    monkeypatch.setattr(scheduler, "_run_once", fake_run_once)
    scheduler.start()
    gate.wait(0.2)
    scheduler.stop(timeout=2.0)
    assert concurrent["max"] == 1


def test_stop_joins_thread_promptly():
    scheduler = _sched(command=["true"], interval=100.0)  # 长周期,靠 stop 中断 wait
    scheduler.start()
    scheduler.stop(timeout=2.0)
    assert scheduler._thread is not None and not scheduler._thread.is_alive()


def test_nonpersistent_child_is_terminated_and_reaped_on_stop(monkeypatch):
    """停服不能遗留仍在运行或已退出未回收的短命子进程。"""
    started = threading.Event()
    finished = threading.Event()

    class BlockingProcess(_FakeProcess):
        def __init__(self):
            super().__init__(returncode=None)
            self.reaped = False

        def communicate(self, timeout=None):
            del timeout
            started.set()
            assert finished.wait(2.0)
            self.reaped = True
            return "", ""

        def terminate(self):
            super().terminate()
            finished.set()

        def kill(self):
            super().kill()
            finished.set()

    process = BlockingProcess()
    monkeypatch.setattr(subprocess, "Popen", lambda *_args, **_kwargs: process)
    scheduler = _sched(interval=0.001, timeout=60.0)

    scheduler.start()
    assert started.wait(1.0)
    scheduler.stop(timeout=1.0)

    assert process.terminated
    assert process.reaped
    assert process.poll() is not None
    assert scheduler._process is None
    assert scheduler._thread is not None
    assert not scheduler._thread.is_alive()


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
       异常会穿出去打死调度线程 → 画像从此永不再跑。
    """
    captured: dict = {}

    def fake_popen(_cmd, **kwargs):
        captured.update(kwargs)
        return _FakeProcess()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    _sched()._run_once()

    from xskill.utils.proc import windowless_subprocess_kwargs
    for key, value in windowless_subprocess_kwargs().items():
        assert captured.get(key) == value, f"子进程必须静默无窗:缺 {key}"
    assert captured.get("errors") == "replace", "解码必须带 errors,否则 GBK 打死调度线程"
    assert captured.get("encoding") == "utf-8"
    assert "text" not in captured, "显式 encoding 与 text=True 不可并存"


def test_loop_survives_run_once_exception(monkeypatch, caplog):
    """回归:_loop 无兜底时,_run_once 漏网异常会让 daemon 调度线程静默猝死,
    进程还活着但画像再也不跑。异常必须被吞掉+落日志,下轮继续。"""
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


def test_replacement_child_inherits_pending_drain_before_starting_work(tmp_path):
    command = [
        sys.executable, "-c",
        """import os, sys, time
from pathlib import Path
root = Path(sys.argv[1])
first = root / "first"
if not first.exists():
    first.touch()
    while not (root / "exit-first").exists():
        time.sleep(0.02)
else:
    output = root / "replacement.tmp"
    output.write_text(os.environ.get("XSKILL_DISTILLATION_DRAIN_TOKEN", ""))
    output.replace(root / "replacement")
    os.read(0, 1)
""", str(tmp_path),
    ]
    scheduler = IntervalSubprocessScheduler(
        "restarting-writer", command, interval=0.05, timeout=1,
        persistent=True, stop_on_stdin_close=True,
    )

    def wait_for_file(name):
        path = tmp_path / name
        deadline = time.monotonic() + 5
        while not path.is_file() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert path.is_file()
        return path

    scheduler.start()
    try:
        wait_for_file("first")
        scheduler.set_drain("pending-switch")
        (tmp_path / "exit-first").touch()
        assert wait_for_file("replacement").read_text() == "pending-switch"
    finally:
        scheduler.stop()
