"""Kernel-host stdio capture and dashboard SSE tail."""
from __future__ import annotations

import json
import subprocess
import sys
import threading
import time

from xskill.kernels.console_log import iter_kernel_console_sse
from xskill.pipeline.scheduler import IntervalSubprocessScheduler


def _payloads(chunks: list[str]) -> list[dict]:
    out = []
    for chunk in chunks:
        for raw_line in chunk.split("\n"):
            if raw_line.startswith("data: "):
                out.append(json.loads(raw_line[6:]))
    return out


def test_iter_kernel_console_sse_emits_backlog_then_new_lines(tmp_path):
    path = tmp_path / "xskill.kernel.log"
    path.write_text("old-line\nkeep-line\n", encoding="utf-8")
    events = list(iter_kernel_console_sse(path, max_events=3, poll_seconds=0.01))
    payloads = _payloads(events)
    assert payloads[0]["t"] == "meta"
    assert payloads[0]["path"] == str(path)
    assert payloads[1] == {"t": "log", "line": "old-line"}
    assert payloads[2] == {"t": "log", "line": "keep-line"}

    with path.open("a", encoding="utf-8") as handle:
        handle.write("new-line\n")
    more = list(iter_kernel_console_sse(path, max_events=4, poll_seconds=0.01))
    assert {"t": "log", "line": "new-line"} in _payloads(more)


def test_iter_kernel_console_sse_status_when_file_missing(tmp_path):
    path = tmp_path / "missing.log"
    events = list(iter_kernel_console_sse(path, max_events=2, poll_seconds=0.01))
    payloads = _payloads(events)
    assert payloads[0]["t"] == "meta"
    assert payloads[1]["t"] == "status"
    assert "尚未生成" in payloads[1]["line"]


def test_persistent_scheduler_captures_child_print(tmp_path):
    script = tmp_path / "talk.py"
    script.write_text(
        "print('kernel-hello', flush=True)\n"
        "import time\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    log_path = tmp_path / "xskill.kernel.log"
    scheduler = IntervalSubprocessScheduler(
        "kernel-host",
        [sys.executable, "-u", str(script)],
        interval=0.05,
        timeout=2.0,
        persistent=True,
        log_path=log_path,
    )
    scheduler.start()
    deadline = time.time() + 3
    text = ""
    try:
        while time.time() < deadline:
            if log_path.is_file():
                text = log_path.read_text(encoding="utf-8", errors="replace")
                if "kernel-hello" in text:
                    break
            time.sleep(0.05)
        else:
            raise AssertionError(text or "no kernel log")
    finally:
        scheduler.stop(timeout=1.0)
    assert "kernel-hello" in text


def test_persistent_scheduler_without_log_path_keeps_devnull(monkeypatch):
    seen: dict = {}
    started = threading.Event()

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

    def fake_popen(_command, **kwargs):
        seen.update(kwargs)
        started.set()
        return FakeProcess()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    scheduler = IntervalSubprocessScheduler(
        "kernel-host",
        ["true"],
        interval=0.01,
        timeout=1.0,
        persistent=True,
    )
    scheduler.start()
    assert started.wait(1.0)
    scheduler.stop(timeout=1.0)
    assert seen["stdout"] is subprocess.DEVNULL
    assert seen["stderr"] is subprocess.DEVNULL
    assert "env" not in seen


def test_persistent_scheduler_log_path_merges_stdio(tmp_path, monkeypatch):
    seen: dict = {}
    started = threading.Event()

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

    def fake_popen(_command, **kwargs):
        seen.update(kwargs)
        started.set()
        return FakeProcess()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    log_path = tmp_path / "nested" / "xskill.kernel.log"
    scheduler = IntervalSubprocessScheduler(
        "kernel-host",
        ["true"],
        interval=0.01,
        timeout=1.0,
        persistent=True,
        log_path=log_path,
    )
    scheduler.start()
    assert started.wait(1.0)
    scheduler.stop(timeout=1.0)
    assert seen["stderr"] is subprocess.STDOUT
    assert getattr(seen["stdout"], "name", None) == str(log_path)
    assert seen["env"]["PYTHONUNBUFFERED"] == "1"
    assert log_path.parent.is_dir()
