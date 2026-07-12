"""supervisor.py — 无 init 系统平台上的 connect 崩溃自愈 watchdog

``xskill connect --supervise`` 的进程主体：循环拉起 ``connect --foreground``
子进程，子进程退出后按指数退避重启。适用于没有 systemd 的 Linux（精简容器、
鸿蒙、老发行版）、未启 systemd 的 WSL，以及 Windows 上 schtasks 被 Group
Policy 禁用后的启动文件夹降级——这些环境里操作系统不提供崩溃自愈，watchdog
就是自愈层（systemd Restart= / schtasks RestartOnFailure 的用户态等价物）。

行为约定
────────
- 子进程 env 注入 ``XSKILL_SUPERVISED=1``：updater 升级完成后据此统一以非零
  退出码退出，由本 watchdog 用新版本代码拉起（见 updater._restart）。
- 每次 spawn 都把 child_pid 合并写回 daemon state（update_daemon_state），
  ``xskill status`` / stop 靠它定位子进程。
- 退避：1s 起步 ×2 递增、封顶 300s；子进程存活 ≥600s 视为健康，退避归零。
  这样偶发崩溃秒级恢复，持续崩溃（坏版本/坏配置）不会空转烧 CPU。
- SIGTERM/SIGINT → 先 SIGTERM 子进程（5s 宽限后 SIGKILL）再退出，保证
  ``xskill stop`` 一次杀干净。
- 防双跑：启动时 state 里已有存活的其他 watchdog 则直接退出 0（幂等）。
"""
from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import time
from typing import Optional

logger = logging.getLogger("xskill.team.client.supervisor")

BACKOFF_INITIAL = 1.0
BACKOFF_FACTOR = 2.0
BACKOFF_CAP = 300.0
HEALTHY_RUNTIME = 600.0

SUPERVISED_ENV = "XSKILL_SUPERVISED"


def next_backoff(current: float, child_runtime: float) -> float:
    """给定当前退避值与子进程本次存活时长，算下一次重启前的等待秒数。"""
    if child_runtime >= HEALTHY_RUNTIME:
        return BACKOFF_INITIAL
    return min(max(current, BACKOFF_INITIAL) * BACKOFF_FACTOR, BACKOFF_CAP)


def _foreground_child_argv() -> list[str]:
    return [sys.executable or "python", "-m", "xskill", "connect", "--foreground"]


class Supervisor:
    """watchdog 主体。run() 阻塞直到收到停止信号。"""

    def __init__(self, spawn=None, monotonic=time.monotonic, sleep=None):
        # spawn/monotonic/sleep 可注入，单测不必起真进程、不必真等退避。
        self._spawn = spawn or self._default_spawn
        self._monotonic = monotonic
        self._sleep = sleep or self._interruptible_sleep
        self._stop_requested = False
        self._child: Optional[subprocess.Popen] = None

    # ── 进程操作（可注入替身） ─────────────────────────────────

    @staticmethod
    def _default_spawn() -> subprocess.Popen:
        from xskill.config import get_connect_daemon_state_path
        env = dict(os.environ)
        env[SUPERVISED_ENV] = "1"
        log_path = (get_connect_daemon_state_path().parent
                    / "logs" / "connect-daemon.log")
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_file = open(log_path, "ab")
        try:
            return subprocess.Popen(
                _foreground_child_argv(), env=env,
                stdin=subprocess.DEVNULL, stdout=log_file,
                stderr=subprocess.STDOUT, close_fds=True,
            )
        finally:
            # 子进程已持有 fd，父进程侧句柄立即关闭防泄漏。
            log_file.close()

    def _interruptible_sleep(self, seconds: float) -> None:
        """0.2s 粒度轮询 stop 标志的 sleep——SIGTERM 到达后最多 0.2s 内退出。"""
        deadline = self._monotonic() + seconds
        while not self._stop_requested and self._monotonic() < deadline:
            time.sleep(0.2)

    # ── 信号 ──────────────────────────────────────────────────

    def _request_stop(self, signum, frame) -> None:  # noqa: ARG002
        self._stop_requested = True

    def _install_signal_handlers(self) -> None:
        for sig_name in ("SIGTERM", "SIGINT", "SIGBREAK"):
            sig = getattr(signal, sig_name, None)
            if sig is None:
                continue
            try:
                signal.signal(sig, self._request_stop)
            except (OSError, ValueError):
                pass

    def _terminate_child(self) -> None:
        child = self._child
        if child is None or child.poll() is not None:
            return
        try:
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=5)
        except (OSError, subprocess.SubprocessError):
            logger.warning("supervisor: 终止子进程失败", exc_info=True)

    # ── 主循环 ────────────────────────────────────────────────

    def run(self) -> int:
        from xskill.team.client.service import (
            _pid_alive, read_daemon_state, update_daemon_state,
        )

        state = read_daemon_state()
        existing = state.get("watchdog_pid")
        if (isinstance(existing, int) and existing != os.getpid()
                and _pid_alive(existing)):
            logger.info("supervisor: 已有 watchdog (pid %s) 在跑，本进程退出",
                        existing)
            return 0

        self._install_signal_handlers()
        update_daemon_state(watchdog_pid=os.getpid())
        backoff = BACKOFF_INITIAL
        logger.info("supervisor: watchdog 启动 (pid %s)", os.getpid())

        while not self._stop_requested:
            started = self._monotonic()
            try:
                self._child = self._spawn()
            except OSError:
                # spawn 本身失败（fd 耗尽/内存不足等瞬态）也走退避，不退出。
                logger.error("supervisor: 拉起子进程失败", exc_info=True)
                self._sleep(backoff)
                backoff = next_backoff(backoff, 0.0)
                continue
            update_daemon_state(child_pid=self._child.pid,
                                child_started_at=int(time.time()))
            logger.info("supervisor: connect 子进程已拉起 (pid %s)",
                        self._child.pid)

            while self._child.poll() is None and not self._stop_requested:
                time.sleep(0.2)

            if self._stop_requested:
                break
            runtime = self._monotonic() - started
            backoff = next_backoff(backoff, runtime)
            logger.warning(
                "supervisor: 子进程退出 (code=%s, 存活 %.0fs)，%.0fs 后重启",
                self._child.returncode, runtime, backoff,
            )
            self._sleep(backoff)

        self._terminate_child()
        logger.info("supervisor: watchdog 退出")
        return 0


def run_supervisor() -> int:
    """``xskill connect --supervise`` 的入口。"""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    return Supervisor().run()
