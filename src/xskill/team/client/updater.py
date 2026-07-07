"""updater.py — xskill client 自动更新

TeamClient 跑起来后每隔一段时间（默认 1 小时）查 PyPI，发现新版就升级并重启。

重启机制
────────
- Linux / macOS：``os.execv`` 原地替换进程（同 PID，守护进程/systemd 不感知）
- Windows：spawn 新 detach 进程 + 退出当前进程（schtasks/Startup 文件夹会保持常驻）

版本策略
────────
- 包含预发版（a/b/rc），因为内部用 alpha 版本
- 严格大于当前版本才升级，不降级
- 网络/PyPI 故障直接跳过，不打断主循环
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
from typing import Optional

logger = logging.getLogger("xskill.team.client.updater")

_PYPI_JSON_URL = "https://pypi.org/pypi/{package}/json"


def _current_version(package: str) -> Optional[str]:
    try:
        from importlib.metadata import version
        return version(package)
    except Exception:
        return None


def _latest_pypi_version(package: str) -> Optional[str]:
    """查 PyPI JSON API 取最新版本（含预发版）。超时/网络错误返回 None。"""
    import json
    import urllib.request
    url = _PYPI_JSON_URL.format(package=package)
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            data = json.loads(r.read().decode("utf-8"))
        # info.version 是 PyPI 判定的「最新稳定版」；
        # 要包含预发版，需扫 releases 键取最大版本。
        from packaging.version import Version
        all_versions = [
            Version(v) for v in data.get("releases", {})
            if not Version(v).is_devrelease  # 排除 dev 版，保留 a/b/rc
        ]
        if not all_versions:
            return None
        return str(max(all_versions))
    except Exception:
        logger.debug("updater: 查 PyPI 失败", exc_info=True)
        return None


def _restart() -> None:
    """升级成功后重启进程，加载新版本代码。

    - Linux/macOS：``os.execv`` 原地替换，PID 不变，对 systemd 透明
    - Windows：spawn detach 新进程 + 退出；schtasks/Startup 文件夹保持常驻不受影响
    """
    logger.info("updater: 升级完成，即将重启...")
    if sys.platform == "win32":
        CREATE_NO_WINDOW = 0x08000000
        DETACHED_PROCESS = 0x00000008
        subprocess.Popen(
            sys.argv,
            creationflags=CREATE_NO_WINDOW | DETACHED_PROCESS,
            close_fds=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        # 让新进程有时间启动，再退出旧进程
        import time
        time.sleep(2)
        os._exit(0)
    else:
        # os.execv 替换当前进程镜像，不产生新 PID
        os.execv(sys.executable, [sys.executable] + sys.argv)


class AutoUpdater:
    """后台线程：每隔 ``interval`` 秒检查 PyPI，有新版则升级并重启。

    用法::

        updater = AutoUpdater()
        updater.start()
        # ... 主循环 ...
        updater.stop()
    """

    def __init__(
        self,
        package: str = "xskill",
        interval: float = 3600,       # 默认 1 小时
        pypi_url: str = "https://pypi.org/simple/",
    ):
        self.package = package
        self.interval = interval
        self.pypi_url = pypi_url
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        """启动后台检查线程（daemon=True，主进程退出时自动终止）。"""
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="xskill-auto-updater",
        )
        self._thread.start()
        logger.info("updater: 自动更新已启用（每 %.0f 分钟检查一次）",
                    self.interval / 60)

    def stop(self) -> None:
        self._stop.set()

    # ─────────────────────────────────────────────────────────────

    def _loop(self) -> None:
        # 启动后先等一个完整周期再做第一次检查，避免刚起来就占 pip
        self._stop.wait(self.interval)
        while not self._stop.is_set():
            self._check_and_update()
            self._stop.wait(self.interval)

    def _check_and_update(self) -> None:
        current_str = _current_version(self.package)
        if not current_str:
            logger.debug("updater: 无法读取当前版本，跳过本次检查")
            return

        latest_str = _latest_pypi_version(self.package)
        if not latest_str:
            return   # 网络问题，静默跳过

        try:
            from packaging.version import Version
            current = Version(current_str)
            latest = Version(latest_str)
        except Exception:
            return

        if latest <= current:
            logger.debug("updater: 当前版本 %s 已是最新", current_str)
            return

        logger.info("updater: 发现新版本 %s（当前 %s），开始升级...",
                    latest_str, current_str)
        if self._install(latest_str):
            _restart()   # 升级成功后重启，不会走到这行之后的代码
            # （_restart 在 Windows 上 os._exit；Linux 上 execv）

    def _install(self, target_version: str) -> bool:
        """用 pip 升级到指定版本。返回是否成功。"""
        cmd = [
            sys.executable, "-m", "pip", "install", "--upgrade",
            f"{self.package}=={target_version}",
            "-i", self.pypi_url,
            "-q",     # quiet：只打印错误
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode == 0:
                logger.info("updater: 升级到 %s 成功", target_version)
                return True
            logger.warning("updater: pip 升级失败:\n%s",
                           result.stderr.strip() or result.stdout.strip())
            return False
        except Exception:
            logger.warning("updater: 执行 pip 失败", exc_info=True)
            return False
