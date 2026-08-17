"""定时短命子进程调度器:daemon 线程周期性 spawn 一个短命子进程跑重活(sweep /
画像 batch),算完即退。子进程在独立解释器进程里跑,GIL 与 web 事件循环彻底隔离。

沿用 ``team/client/updater.py`` AutoUpdater 的"daemon 线程 + Event.wait + 短命子进程
(带 timeout 硬上限)"范式:

- ``Popen.communicate`` **阻塞**本调度线程直到子进程退出,故同一任务天然串行、不可能
  自重叠；同时保留进程句柄，停服时可终止并回收正在运行的短命子进程;
- 调度线程只等子进程(I/O 阻塞、释放 GIL),不做任何重计算,不占 web 事件循环;
- 用 ``Event.wait(interval)`` 定时(禁 time.sleep),``stop()`` 竖旗即时中断等待。
"""
from __future__ import annotations

import logging
import os
import subprocess
import threading
from pathlib import Path

from xskill.utils.proc import windowless_subprocess_kwargs

logger = logging.getLogger("xskill.pipeline.scheduler")


class IntervalSubprocessScheduler:
    """每隔 ``interval`` 秒 spawn 一次 ``command`` 短命子进程,算完即退。"""

    def __init__(
        self,
        name: str,
        command: list[str],
        *,
        interval: float,
        timeout: float,
        persistent: bool = False,
        log_path: str | Path | None = None,
    ):
        if interval <= 0:
            raise ValueError("interval 必须 > 0")
        if timeout <= 0:
            raise ValueError("timeout 必须 > 0")
        self._name = name
        self._command = list(command)
        self._interval = float(interval)
        self._timeout = float(timeout)
        self._persistent = bool(persistent)
        self._log_path = Path(log_path) if log_path is not None else None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._process: subprocess.Popen | None = None
        self._process_lock = threading.Lock()

    def start(self) -> None:
        """幂等启动调度 daemon 线程。"""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name=f"xskill-sched-{self._name}", daemon=True,
        )
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        """竖停机旗、中断 wait，并终止、回收正在运行的子进程。"""
        self._stop.set()
        with self._process_lock:
            process = self._process
        if process is not None and process.poll() is None:
            try:
                process.terminate()
            except OSError:
                logger.warning(
                    "调度任务 %s 停止时 terminate 失败",
                    self._name,
                    exc_info=True,
                )
        if self._thread is not None:
            self._thread.join(timeout)

        # Popen 与 stop 存在竞态：第一次读取后，调度线程可能刚完成 spawn。
        # join 后必须重读当前句柄，不能只操作旧的 process。
        with self._process_lock:
            process = self._process
        if process is not None and process.poll() is None:
            try:
                process.kill()
            except OSError:
                logger.warning(
                    "调度任务 %s 停止时 kill 失败",
                    self._name,
                    exc_info=True,
                )
            if self._thread is not None:
                self._thread.join(timeout)

        # 正常路径由 communicate()/wait() 完成回收并清空句柄。若调度线程已退但
        # 句柄仍在，最后再 wait 一次，避免已退出的子进程残留为 zombie。
        with self._process_lock:
            process = self._process
        if (
            process is not None
            and (self._thread is None or not self._thread.is_alive())
        ):
            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                logger.warning("调度任务 %s 停止后子进程未退出", self._name)
            except OSError:
                logger.warning(
                    "调度任务 %s 停止时 wait 失败",
                    self._name,
                    exc_info=True,
                )

    def _loop(self) -> None:
        if self._persistent:
            self._persistent_loop()
            return
        # 先等一个周期再首跑:避免 startup 瞬间与其它初始化抢资源(照 AutoUpdater)。
        # Event.wait 返回 True 表示被 stop 竖旗中断 → 退出循环。
        while not self._stop.wait(self._interval):
            # 本线程是 daemon:任何漏网异常都会让它静默猝死,此后 sweep / 画像
            # 永不再跑(进程还活着,只是不干活了)。照 daemon._tick 兜住并落日志。
            try:
                self._run_once()
            except Exception:  # noqa: BLE001 — 顶层任务边界,吞掉但必须落日志
                logger.warning("调度任务 %s 本轮异常,下轮继续", self._name,
                               exc_info=True)

    def _persistent_loop(self) -> None:
        """守护一个常驻轻量子进程；退出后有界退避重启。"""
        while not self._stop.is_set():
            log_file = None
            try:
                popen_kwargs: dict = {
                    "stdout": subprocess.DEVNULL,
                    "stderr": subprocess.DEVNULL,
                    **windowless_subprocess_kwargs(),
                }
                if self._log_path is not None:
                    self._log_path.parent.mkdir(parents=True, exist_ok=True)
                    log_file = open(self._log_path, "ab")
                    env = os.environ.copy()
                    env["PYTHONUNBUFFERED"] = "1"
                    popen_kwargs["stdout"] = log_file
                    popen_kwargs["stderr"] = subprocess.STDOUT
                    popen_kwargs["env"] = env
                process = subprocess.Popen(self._command, **popen_kwargs)
            except OSError:
                if log_file is not None:
                    log_file.close()
                logger.warning(
                    "常驻调度任务 %s 启动子进程失败",
                    self._name,
                    exc_info=True,
                )
                if self._stop.wait(self._interval):
                    return
                continue
            if log_file is not None:
                log_file.close()
            with self._process_lock:
                self._process = process
            while process.poll() is None:
                if self._stop.wait(min(self._interval, 0.5)):
                    try:
                        process.terminate()
                    except OSError:
                        logger.warning(
                            "常驻调度任务 %s terminate 失败",
                            self._name,
                            exc_info=True,
                        )
                    try:
                        process.wait(timeout=self._timeout)
                    except subprocess.TimeoutExpired:
                        try:
                            process.kill()
                            process.wait()
                        except OSError:
                            logger.warning(
                                "常驻调度任务 %s kill 失败",
                                self._name,
                                exc_info=True,
                            )
                    break
            return_code = process.poll()
            with self._process_lock:
                if self._process is process:
                    self._process = None
            if self._stop.is_set():
                return
            logger.warning(
                "常驻调度任务 %s 退出码=%s，%.1fs 后重启",
                self._name,
                return_code,
                self._interval,
            )
            if self._stop.wait(self._interval):
                return

    def _run_once(self) -> None:
        try:
            process = subprocess.Popen(
                self._command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                # Windows:不带无窗 flag 会每个周期弹一次 cmd 黑窗给用户。
                # 编码:子进程输出在中文 Windows 上默认按 cp936
                # strict 解码,非法字节会抛 UnicodeDecodeError——那会穿过下面的
                # except(只接 Timeout/OSError)打死调度线程。显式 utf-8+replace:
                # 子进程是我们自己的 python -m xskill._workers,统一按 utf-8 出。
                encoding="utf-8", errors="replace",
                **windowless_subprocess_kwargs(),
            )
        except OSError:
            logger.warning("调度任务 %s 启动子进程失败", self._name, exc_info=True)
            return

        with self._process_lock:
            self._process = process
        stdout = ""
        stderr = ""
        timed_out = False
        try:
            # 覆盖 stop() 恰好发生在 Popen 返回、句柄登记之前的竞态。
            if self._stop.is_set() and process.poll() is None:
                process.terminate()
            try:
                stdout, stderr = process.communicate(timeout=self._timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                try:
                    process.kill()
                except OSError:
                    logger.warning(
                        "调度任务 %s 超时后 kill 失败",
                        self._name,
                        exc_info=True,
                    )
                # kill 后必须 communicate/wait，不能把已退出子进程留成 zombie。
                stdout, stderr = process.communicate()
        finally:
            with self._process_lock:
                if self._process is process:
                    self._process = None

        if timed_out:
            logger.warning("调度任务 %s 超过 %.0fs 上限被杀", self._name, self._timeout)
            return
        if process.returncode != 0 and not self._stop.is_set():
            logger.warning(
                "调度任务 %s 退出码=%d stderr=%s", self._name, process.returncode,
                (stderr or stdout or "")[:500],
            )
