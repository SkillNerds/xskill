"""updater.py — xskill client 自动更新

TeamClient 跑起来后每隔一段时间（默认 1 小时）查 PyPI，发现新版就升级并重启。
如果 PyPI 查询或安装失败，且 client 已连接 team server，则读取 server 版本；
server 版本高于本地版本时，下载 server 暴露的 wheel 并安装。

重启机制
────────
- supervisor 托管（XSKILL_SUPERVISED=1：无 systemd 的 Linux/WSL/鸿蒙，以及
  Windows startup_folder 降级）：以非零退出码退出，watchdog 用新版本拉起
- Linux / macOS（systemd 直管）：``os.execv`` 原地替换进程（同 PID）
- Windows schtasks：非零退出，RestartOnFailure 在 1 分钟内重启

版本策略与健壮性
────────
- 包含预发版（a/b/rc），因为内部用 alpha 版本
- 严格大于当前版本才升级，不降级
- 网络/PyPI/server 故障不会打断主循环
- **升级后健康检查**：pip 装完先用子进程验证 ``<python> -m xskill --version``
  可跑，失败则回滚到升级前版本——坏 wheel（半残依赖/二进制不兼容，鸿蒙与
  老 glibc 上很现实）不会把常驻进程带进「重启即崩」的死循环
- **坏版本拉黑**：健康检查失败的版本记入 ``~/.xskill/update_journal.json``，
  之后的检查跳过该版本，杜绝「升级→崩→回滚→再升级」空转
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
    url_template = os.environ.get("XSKILL_PYPI_JSON_URL", _PYPI_JSON_URL)
    url = url_template.format(package=package)
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

    - supervisor 托管（XSKILL_SUPERVISED=1）：全平台统一以非零退出码退出，
      watchdog 用新版本拉起；不自行 spawn，watchdog 始终是唯一管理者
    - Linux/macOS：``os.execv`` 原地替换，PID 不变，对 systemd 透明
    - Windows schtasks：以非零退出码退出，schtasks RestartOnFailure 在
      1 分钟内用新版本重启进程；不另起 detach 进程，避免孤立进程脱管
    - Windows startup_folder（旧版无 supervisor 的存量安装）：spawn detach
      新进程 + 以 0 退出
    """
    import time
    logger.info("updater: 升级完成，即将重启...")
    if os.environ.get("XSKILL_SUPERVISED") == "1":
        logger.info("updater: supervisor 托管 — 以退出码 1 退出，等 watchdog 重启")
        time.sleep(1)
        os._exit(1)
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


def _journal_path() -> Path:
    from xskill.config import get_connect_daemon_state_path
    return get_connect_daemon_state_path().parent / "update_journal.json"


def load_update_journal() -> dict:
    """读更新日志（坏版本黑名单 + last_good）。缺失/损坏容忍为空。"""
    import json
    try:
        d = json.loads(_journal_path().read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def save_update_journal(journal: dict) -> None:
    import json
    try:
        path = _journal_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(journal, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    except OSError:
        logger.debug("updater: 写 update journal 失败", exc_info=True)


def _blacklist_version(version: str, reason: str) -> None:
    import time
    journal = load_update_journal()
    bad = journal.setdefault("bad_versions", {})
    bad[version] = {"ts": int(time.time()), "reason": reason}
    save_update_journal(journal)
    logger.warning("updater: 版本 %s 已拉黑（%s），后续检查将跳过", version, reason)


def _is_blacklisted(version: str) -> bool:
    return version in (load_update_journal().get("bad_versions") or {})


def _record_last_good(version: str) -> None:
    journal = load_update_journal()
    journal["last_good"] = version
    save_update_journal(journal)


# 健康检查的子进程超时。--version 只 import 包 + 打印，正常几秒内返回；
# 超时视为坏版本（import 挂死同样是坏）。
_HEALTH_CHECK_TIMEOUT = 60


def _health_check() -> bool:
    """新版本装完后、重启前，用干净子进程验证包可导入可执行。

    当前进程内存里跑的还是旧代码，import 状态不能证明新安装是好的；
    必须起新解释器让它真正加载磁盘上的新版本。
    """
    try:
        cp = subprocess.run(
            [sys.executable, "-m", "xskill", "--version"],
            capture_output=True, text=True, timeout=_HEALTH_CHECK_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        logger.warning("updater: 健康检查超时（%ds）", _HEALTH_CHECK_TIMEOUT)
        return False
    except Exception:
        logger.warning("updater: 健康检查执行失败", exc_info=True)
        return False
    if cp.returncode != 0:
        logger.warning("updater: 健康检查失败 (rc=%s):\n%s", cp.returncode,
                       (cp.stderr or cp.stdout or "").strip()[:2000])
    return cp.returncode == 0


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
        pypi_url: str | None = None,
        server_url: str | None = None,
        client_id: str | None = None,
        join_token: str | None = None,
    ):
        # pypi_url 缺省 None = 不传 -i，尊重用户机器的 pip 配置（pip.ini /
        # pip.conf 的 index-url，内网通常配了企业镜像）。曾写死
        # https://pypi.org/simple/ ——强行绕过企业镜像直连公网，代理环境下
        # 必超时，用户配好的镜像形同虚设。显式传入时才覆盖。
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

        if _is_blacklisted(latest_str):
            # 该版本此前健康检查失败被回滚过——跳过，等再新的版本。
            logger.info("updater: PyPI 最新 %s 在坏版本黑名单中，跳过", latest_str)
            self._check_server_fallback(current_str, current,
                                        reason="pypi_blacklisted")
            return

        if latest <= current:
            logger.debug("updater: 当前版本 %s 不低于 PyPI 最新 %s",
                         current_str, latest_str)
            # 内网场景 server 预置的 wheel 常常领先公网 PyPI（先内部分发、
            # 后补发 PyPI）。这里曾直接 return——只要 pypi.org 的 JSON API
            # 可达且没有新版，server 渠道就永远不会被查询，内网更新静默
            # 失效。PyPI 无新版 ≠ 没有更新。
            self._check_server_fallback(current_str, current,
                                        reason="pypi_not_ahead")
            return

        logger.info("updater: 发现新版本 %s（当前 %s），开始升级...",
                    latest_str, current_str)
        if self.install_and_verify(latest_str, current_str):
            _restart()   # 升级成功后重启，不会走到这行之后的代码
            # （_restart 在 supervisor/Windows 下 os._exit；Linux 上 execv）
            return
        self._check_server_fallback(current_str, current, reason="pypi_install_failed")

    # pip 卡死的硬上限。pip 自带的 socket timeout 只覆盖"完全无数据"，
    # 代理黑洞式的涓涓细流永远不触发；而 updater 是单线程循环，一次
    # subprocess.run 挂死 = 之后每小时的检查全部消失，自动更新静默死亡。
    _PIP_TIMEOUT = 600

    def install_and_verify(self, target_version: str,
                           current_version: str) -> bool:
        """升级到 target 并做健康检查；失败回滚到 current 并拉黑 target。

        返回 True = 新版本已装好且健康，可以重启。
        返回 False = 未升级成功；若发生过回滚，当前磁盘上仍是（或已回到）
        current_version，进程可安全继续跑内存里的旧代码。
        """
        if not self._install(target_version):
            return False
        if _health_check():
            _record_last_good(target_version)
            return True
        _blacklist_version(target_version, "health_check_failed")
        logger.warning("updater: 新版本 %s 健康检查失败，回滚到 %s...",
                       target_version, current_version)
        if self._install(current_version):
            if _health_check():
                logger.info("updater: 已回滚到 %s", current_version)
            else:
                logger.critical(
                    "updater: 回滚到 %s 后健康检查仍失败——环境可能已损坏，"
                    "请人工介入（pip install xskill==%s）",
                    current_version, current_version)
        else:
            logger.critical(
                "updater: 回滚安装失败！磁盘上可能是坏版本 %s；本进程继续以"
                "内存中的旧代码运行，且不会重启。请人工执行 "
                "pip install xskill==%s", target_version, current_version)
        return False

    def _install(self, target_version: str) -> bool:
        """用 pip 升级到指定版本。返回是否成功。"""
        cmd = [
            sys.executable, "-m", "pip", "install", "--upgrade",
            f"{self.package}=={target_version}",
            "--timeout", "15", "--retries", "2",
            "-q",     # quiet：只打印错误
        ]
        if self.pypi_url:
            cmd += ["-i", self.pypi_url]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True,
                                    timeout=self._PIP_TIMEOUT)
            if result.returncode == 0:
                logger.info("updater: 升级到 %s 成功", target_version)
                return True
            logger.warning("updater: pip 升级失败:\n%s",
                           result.stderr.strip() or result.stdout.strip())
            return False
        except subprocess.TimeoutExpired:
            logger.warning("updater: pip 超过 %ds 未退出，放弃本次升级",
                           self._PIP_TIMEOUT)
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
        if _is_blacklisted(server_version_str):
            logger.info("updater: server 版本 %s 在坏版本黑名单中，跳过",
                        server_version_str)
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
            if not self._install_wheel(wheel):
                return
            if _health_check():
                _record_last_good(server_version_str)
                _restart()
                return
            # server wheel 健康检查失败：拉黑 + 尽力回滚（回滚走 pip 索引，
            # 纯内网机若无镜像可能失败——critical 留痕，进程不重启保命）。
            _blacklist_version(server_version_str, "health_check_failed")
            logger.warning("updater: server wheel %s 健康检查失败，回滚到 %s...",
                           server_version_str, current_str)
            if not (self._install(current_str) and _health_check()):
                logger.critical(
                    "updater: 回滚失败或仍不健康——请人工执行 "
                    "pip install xskill==%s；本进程继续跑内存旧代码，不重启",
                    current_str)

    def _install_wheel(self, wheel_path: Path) -> bool:
        """用 pip 安装 server 下载的 wheel。返回是否成功。"""
        cmd = [
            sys.executable, "-m", "pip", "install", "--upgrade",
            str(wheel_path),
            "-q",
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True,
                                    timeout=self._PIP_TIMEOUT)
            if result.returncode == 0:
                logger.info("updater: 安装 server wheel 成功: %s", wheel_path.name)
                return True
            logger.warning("updater: server wheel 安装失败:\n%s",
                           result.stderr.strip() or result.stdout.strip())
            return False
        except subprocess.TimeoutExpired:
            logger.warning("updater: pip 装 server wheel 超过 %ds 未退出，放弃",
                           self._PIP_TIMEOUT)
            return False
        except Exception:
            logger.warning("updater: 执行 pip 安装 server wheel 失败", exc_info=True)
            return False
