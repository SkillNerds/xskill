"""updater.py — xskill client 自动更新

TeamClient 跑起来后每隔一段时间（默认 1 小时）查 PyPI，发现新版就升级并重启。
如果 PyPI 查询或安装失败，且 client 已连接 team server，则读取 server 版本；
server 版本高于本地版本时，下载 server 暴露的 wheel 并安装。

重启机制
────────
- Linux / macOS：``os.execv`` 原地替换进程（同 PID，守护进程/systemd 不感知）
- Windows：spawn 新 detach 进程 + 退出当前进程（schtasks/Startup 文件夹会保持常驻）

版本策略
────────
- 包含预发版（a/b/rc），因为内部用 alpha 版本
- 严格大于当前版本才升级，不降级
- 网络/PyPI/server 故障不会打断主循环
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from typing import Any, Optional

logger = logging.getLogger("xskill.team.client.updater")

_PYPI_JSON_URL = "https://pypi.org/pypi/{package}/json"


def _team_api_url(server_url: str, path: str) -> str:
    return f"{server_url.rstrip('/')}/api/v1/team{path}"


def _team_headers(join_token: str, client_id: str) -> dict[str, str]:
    return {
        "X-Xskill-Token": join_token,
        "X-Xskill-Client": client_id,
    }


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


def _server_version(
    server_url: str,
    join_token: str,
    client_id: str,
) -> dict[str, Any] | None:
    """从 team server 读取版本信息。网络/鉴权失败返回 None。"""
    import json
    import urllib.request

    req = urllib.request.Request(
        _team_api_url(server_url, "/version"),
        headers=_team_headers(join_token, client_id),
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        logger.debug("updater: 查询 server 版本失败", exc_info=True)
        return None


def _download_server_wheel(
    server_url: str,
    join_token: str,
    client_id: str,
    dest_dir: Path,
    filename: str | None,
) -> Path | None:
    """从 team server 下载 wheel 到临时目录。失败返回 None。"""
    import urllib.request

    safe_name = Path(filename or "xskill-server.whl").name
    if not safe_name.endswith(".whl"):
        safe_name = "xskill-server.whl"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / safe_name
    req = urllib.request.Request(
        _team_api_url(server_url, "/wheel"),
        headers=_team_headers(join_token, client_id),
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            data = r.read()
        if not data:
            logger.warning("updater: server wheel 为空")
            return None
        dest.write_bytes(data)
        return dest
    except Exception:
        logger.debug("updater: 下载 server wheel 失败", exc_info=True)
        return None


def _restart() -> None:
    """升级成功后重启进程，加载新版本代码。

    - Linux/macOS：``os.execv`` 原地替换，PID 不变，对 systemd 透明
    - Windows schtasks：以非零退出码退出，schtasks RestartOnFailure 在
      1 分钟内用新版本重启进程；不另起 detach 进程，避免孤立进程脱管
    - Windows startup_folder：spawn detach 新进程 + 以 0 退出；.vbs
      无重启能力，必须自己起新进程才能立即用上新版本
    """
    import time
    logger.info("updater: 升级完成，即将重启...")
    if sys.platform == "win32":
        method = _windows_persistence_method()
        if method == "schtasks":
            # schtasks RestartOnFailure 仅在非零退出时触发；
            # 以 1 退出 → 任务调度器在 1 分钟内重启，新版本自动生效。
            # 不另起 detach 进程，schtasks 始终是该进程的唯一管理者。
            logger.info("updater: schtasks 路径 — 以退出码 1 退出，等待调度器重启")
            time.sleep(1)
            os._exit(1)
        else:
            # startup_folder / 未知：.vbs 无重启能力，手动 spawn 新进程
            logger.info("updater: startup_folder 路径 — spawn 新进程后退出")
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
            time.sleep(2)
            os._exit(0)
    else:
        # os.execv 替换当前进程镜像，不产生新 PID
        os.execv(sys.executable, [sys.executable] + sys.argv)


def _windows_persistence_method() -> str:
    """读 daemon state 取 Windows 持久化方式（schtasks / startup_folder）。

    进程由 schtasks 启动时 method='schtasks'；startup_folder 时 method=
    'startup_folder'。读不到则返回 'startup_folder'（保守：自己 spawn）。
    """
    try:
        from xskill.team.client.service import read_daemon_state
        return read_daemon_state().get("method", "startup_folder")
    except Exception:
        return "startup_folder"


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
        server_url: str | None = None,
        client_id: str | None = None,
        join_token: str | None = None,
    ):
        self.package = package
        self.interval = interval
        self.pypi_url = pypi_url
        self.server_url = server_url
        self.client_id = client_id
        self.join_token = join_token
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
        try:
            from packaging.version import Version
            current = Version(current_str)
        except Exception:
            logger.debug("updater: 当前版本不可解析: %s", current_str, exc_info=True)
            return

        latest_str = _latest_pypi_version(self.package)
        if not latest_str:
            self._check_server_fallback(current_str, current, reason="pypi_query_failed")
            return

        try:
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
            return
        self._check_server_fallback(current_str, current, reason="pypi_install_failed")

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

    def _check_server_fallback(self, current_str: str, current, *, reason: str) -> None:
        """PyPI 不可用时，从 team server 下载同版本 wheel 回退升级。"""
        if not (self.server_url and self.client_id and self.join_token):
            logger.debug("updater: 无 server 回退配置，跳过（%s）", reason)
            return

        info = _server_version(self.server_url, self.join_token, self.client_id)
        if not info:
            return

        server_version_str = str(info.get("version") or "")
        try:
            from packaging.version import Version
            server_version = Version(server_version_str)
        except Exception:
            logger.debug("updater: server 版本不可解析: %s",
                         server_version_str, exc_info=True)
            return

        if server_version <= current:
            logger.debug("updater: server 版本 %s 不高于当前版本 %s",
                         server_version_str, current_str)
            return
        if not info.get("wheel_available"):
            logger.warning("updater: server 版本 %s 可用，但未提供 wheel",
                           server_version_str)
            return

        with tempfile.TemporaryDirectory(prefix="xskill-update-") as td:
            wheel = _download_server_wheel(
                self.server_url,
                self.join_token,
                self.client_id,
                Path(td),
                str(info.get("wheel_filename") or ""),
            )
            if wheel is None:
                return
            logger.info("updater: PyPI 不可用（%s），改用 server wheel 升级到 %s",
                        reason, server_version_str)
            if self._install_wheel(wheel):
                _restart()

    def _install_wheel(self, wheel_path: Path) -> bool:
        """用 pip 安装 server 下载的 wheel。返回是否成功。"""
        cmd = [
            sys.executable, "-m", "pip", "install", "--upgrade",
            str(wheel_path),
            "-q",
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode == 0:
                logger.info("updater: 安装 server wheel 成功: %s", wheel_path.name)
                return True
            logger.warning("updater: server wheel 安装失败:\n%s",
                           result.stderr.strip() or result.stdout.strip())
            return False
        except Exception:
            logger.warning("updater: 执行 pip 安装 server wheel 失败", exc_info=True)
            return False
