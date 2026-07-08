"""service.py — ``xskill connect`` 常驻进程的“装/起/停/看”后端（Problem 2）

``xskill connect`` 本身是阻塞轮询循环（见 daemon.TeamClient.run_forever）。要让
它在用户不开终端的情况下持久后台运行、开机自启、崩溃自拉，就得把它托管给操作系统
的原生守护设施。本模块把这层抽象成可插拔后端：

    ConnectServiceBackend           抽象基类（含共享 pid/state 读写 + 存活校验）
      └─ WindowsTaskSchedulerBackend  Windows「计划任务」(schtasks)  —— 本 MR 完整实现
      └─ SystemdUserBackend           Linux systemd --user            —— TODO(占位)
      └─ LaunchdBackend               macOS launchd LaunchAgent       —— TODO(占位)

CLI (``xskill start/stop/status``) 只跟 ``get_backend()`` 打交道，不关心平台。

设计约定
────────
- 常驻任务实际执行的是 ``<python> -m xskill connect --foreground``：``--foreground``
  是真正的阻塞轮询；不带它的 ``xskill connect`` 走“握手 + 交给本模块拉起后台”的路径
  （见 cli.cmd_connect）。用 ``-m xskill`` 而非 console-script，避免 pythonw 下
  PATH 不含 Scripts 目录找不到 xskill.exe。
- 运行态落 ``~/.xskill/connect_daemon.json``（get_connect_daemon_state_path）。pid
  会因重启/被杀而失效，所有读取都过 ``_pid_alive`` 校验，陈旧文件不误报 running。
- 启动前必须已有 ``team_client.json``（即先 connect 过一次带 token 的握手），否则
  后台进程起来也没有连接信息可复用——CLI 层据此给出“未曾 connect”的提示。
"""
from __future__ import annotations

import abc
import csv
import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from xskill.config import get_connect_daemon_state_path

logger = logging.getLogger("xskill.team.client.service")

# Windows 计划任务名（TN）。带前缀避免和用户其他任务重名；schtasks 大小写不敏感。
WINDOWS_TASK_NAME = "Xskill_Connect"


class ServiceError(RuntimeError):
    """后端操作失败（含平台不支持）。CLI 捕获后打印 message 即可。"""


# ═══════════════════════════════════════════════════════════════
# pid / 运行态：跨后端共享
# ═══════════════════════════════════════════════════════════════

def _pid_alive(pid: Optional[int]) -> bool:
    """pid 是否存活。与 runtime._alive 同款：signal 0 探测，权限错也算活。"""
    if not isinstance(pid, int) or pid <= 0:
        return False
    if sys.platform == "win32":
        return _pid_alive_windows(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _pid_alive_windows(pid: int) -> bool:
    """Windows 没有 signal 0；用 tasklist 过滤 PID 判断存活。

    ``tasklist /FI "PID eq <pid>"`` 命中会在输出里带上该 pid；无进程时打印
    “INFO: No tasks...”。用 subprocess 而非 ctypes.OpenProcess，避免拿句柄的
    权限细节，也更好在非 Windows 上 mock。
    """
    try:
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"],
            capture_output=True, text=True, check=False, timeout=2,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    for row in csv.reader(out.splitlines()):
        if len(row) >= 2 and row[1] == str(pid):
            return True
    return False


def read_daemon_state() -> dict:
    """读常驻运行态并补上校验过的 ``running``。文件缺失/损坏都视作未运行。"""
    path = get_connect_daemon_state_path()
    if not path.is_file():
        return {"running": False}
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"running": False}
    if not isinstance(d, dict):
        return {"running": False}
    d["running"] = _pid_alive(d.get("pid"))
    return d


def write_daemon_state(**fields) -> None:
    """写常驻运行态（started_at 自动补当前时间戳）。失败仅记 debug，不抛。"""
    path = get_connect_daemon_state_path()
    payload = {"started_at": int(time.time()), **fields}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    except OSError:
        logger.debug("write connect daemon state failed", exc_info=True)


def clear_daemon_state() -> None:
    try:
        get_connect_daemon_state_path().unlink(missing_ok=True)
    except OSError:
        pass


def _foreground_argv() -> list[str]:
    """常驻任务真正执行的命令：``<python> -m xskill connect --foreground``。

    用 ``sys.executable``（Windows 下若存在同目录 pythonw.exe 则优先，免弹窗）。
    """
    exe = sys.executable or "python"
    if sys.platform == "win32":
        pythonw = Path(exe).with_name("pythonw.exe")
        if pythonw.is_file():
            exe = str(pythonw)
    return [exe, "-m", "xskill", "connect", "--foreground"]


# ═══════════════════════════════════════════════════════════════
# 后端抽象
# ═══════════════════════════════════════════════════════════════

class ConnectServiceBackend(abc.ABC):
    """把 ``xskill connect --foreground`` 托管给操作系统守护设施的后端。

    子类实现 install_and_start / stop / status；pid 文件读写用基类的共享助手。
    ``supported`` 为 False 表示该平台的原生常驻尚未实现——``xskill connect`` 默认
    会退化成前台阻塞（保持历史行为），而显式的 start/stop/status 才报“未实现”。
    """

    name = "base"
    supported = True

    @abc.abstractmethod
    def install_and_start(self) -> dict:
        """安装（若需要）并立即启动常驻任务。返回一份 status dict。"""

    @abc.abstractmethod
    def stop(self) -> dict:
        """停止常驻任务（尽量也移除自启注册）。返回一份 status dict。"""

    @abc.abstractmethod
    def status(self) -> dict:
        """汇报常驻任务状态。至少含 ``running`` 布尔。"""


class _UnsupportedBackend(ConnectServiceBackend):
    """尚未实现的平台占位：三个操作都抛带指引的 ServiceError。

    Linux/macOS 的原生持久化（systemd --user / launchd）是后续 MR 的活；在此之前
    这些平台的用户可以自行用 init 系统托管 ``xskill connect --foreground``。
    """

    supported = False

    def __init__(self, name: str, hint: str):
        self.name = name
        self._hint = hint

    def _fail(self) -> dict:
        raise ServiceError(
            f"{self.name} 平台的原生常驻尚未实现。\n"
            f"  暂用你的 init 系统托管 `xskill connect --foreground` 即可"
            f"（{self._hint}）。\n"
            f"  Windows 平台已支持 `xskill start/stop/status`。"
        )

    def install_and_start(self) -> dict:
        return self._fail()

    def stop(self) -> dict:
        return self._fail()

    def status(self) -> dict:
        return self._fail()


# ═══════════════════════════════════════════════════════════════
# Windows：计划任务（schtasks）+ 开机启动文件夹降级
# ═══════════════════════════════════════════════════════════════

# 开机启动文件夹里的 .vbs 脚本名（用 wscript 隐藏窗口运行 pythonw）
_STARTUP_VBS_NAME = "xskill_connect.vbs"


def _startup_vbs_path() -> Optional[Path]:
    """返回 %APPDATA%\\...\\Startup\\xskill_connect.vbs 路径。

    仅在 Windows 上有效；环境变量 APPDATA 不存在时返回 None。
    """
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return None
    return (Path(appdata) / "Microsoft" / "Windows"
            / "Start Menu" / "Programs" / "Startup" / _STARTUP_VBS_NAME)


def _build_startup_vbs(argv: list[str]) -> str:
    """生成隐藏窗口运行 `xskill connect --foreground` 的 VBS 脚本。

    用 WScript.Shell.Run(..., 0, False)：
    - 第二参数 0 = 隐藏窗口（无 CMD 弹窗）
    - 第三参数 False = 不等待进程退出，立即返回
    """
    # list2cmdline 保证路径含空格时正确加引号
    cmd = subprocess.list2cmdline(argv)
    return (
        'Set oShell = CreateObject("WScript.Shell")\r\n'
        f'oShell.Run "{cmd.replace(chr(34), chr(34)+chr(34))}", 0, False\r\n'
    )


def _is_access_denied(cp: "subprocess.CompletedProcess") -> bool:
    """判断 schtasks 输出是否包含"拒绝访问 / Access is denied"。"""
    combined = ((cp.stderr or "") + (cp.stdout or "")).lower()
    return ("access" in combined and "denied" in combined) or "拒绝访问" in combined


def _xml_escape(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def _build_task_xml(command: str, arguments: str, working_dir: str) -> str:
    """生成计划任务定义 XML。

    关键字段：
    - LogonTrigger：AtLogOn，用户一登录就起（无需管理员/开机服务级权限）。
    - ExecutionTimeLimit=PT0S：不限运行时长（默认 3 天会被杀）。
    - RestartOnFailure：崩了每 1 分钟重启，最多 999 次——约等于“永远自愈”。
    - StartWhenAvailable：错过触发（比如登录时机）也尽快补起。
    - MultipleInstancesPolicy=IgnoreNew：已在跑就不重复起，防双 daemon。
    """
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>xskill connect thin client (team skill sync)</Description>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
    </LogonTrigger>
  </Triggers>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RestartOnFailure>
      <Interval>PT1M</Interval>
      <Count>999</Count>
    </RestartOnFailure>
  </Settings>
  <Actions>
    <Exec>
      <Command>{_xml_escape(command)}</Command>
      <Arguments>{_xml_escape(arguments)}</Arguments>
      <WorkingDirectory>{_xml_escape(working_dir)}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


class WindowsTaskSchedulerBackend(ConnectServiceBackend):
    """用 schtasks 把 connect 托管为 AtLogOn、不限时、崩溃自愈的计划任务。"""

    name = "windows"

    def __init__(self, task_name: str = WINDOWS_TASK_NAME):
        self.task_name = task_name

    def _run(self, args: list[str]) -> subprocess.CompletedProcess:
        """跑一条 schtasks 子命令。text 模式拿输出，不 check（自行判 returncode）。"""
        return subprocess.run(
            ["schtasks", *args], capture_output=True, text=True, check=False,
        )

    def install_and_start(self) -> dict:
        argv = _foreground_argv()
        command, arguments = argv[0], subprocess.list2cmdline(argv[1:])
        xml = _build_task_xml(command, arguments, working_dir=str(Path.home()))

        import tempfile
        fd, xml_path = tempfile.mkstemp(suffix=".xml", prefix="xskill_task_")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(xml.encode("utf-16"))
            create = self._run(
                ["/Create", "/TN", self.task_name, "/XML", xml_path, "/F"]
            )
        finally:
            try:
                os.unlink(xml_path)
            except OSError:
                pass

        if create.returncode != 0:
            if _is_access_denied(create):
                # 公司 Group Policy 禁止普通用户通过 schtasks 创建任务。
                # 降级：写开机启动文件夹，立即 detach 启动进程。
                logger.info(
                    "schtasks /Create 被拒（Group Policy 限制），"
                    "降级到开机启动文件夹方案"
                )
                return self._install_startup_folder_and_spawn(argv)
            raise ServiceError(
                "创建计划任务失败：\n"
                f"  {create.stderr.strip() or create.stdout.strip()}"
            )

        run = self._run(["/Run", "/TN", self.task_name])
        if run.returncode != 0:
            raise ServiceError(
                "计划任务已创建但启动失败：\n"
                f"  {run.stderr.strip() or run.stdout.strip()}\n"
                "  可手动在「任务计划程序」里运行 " + self.task_name + " 排查。"
            )

        write_daemon_state(task_name=self.task_name, backend=self.name,
                           method="schtasks", argv=argv, pid=self._query_pid())
        return self.status()

    def _install_startup_folder_and_spawn(self, argv: list[str]) -> dict:
        """降级方案：写 Startup 文件夹 .vbs 脚本 + 立即 detach 启动进程。

        适用于公司 Group Policy 禁止 schtasks 的场景。
        - 持久化：.vbs 在 %APPDATA%\\...\\Startup\\，用户登录即自动执行
        - 无窗口：WScript.Shell.Run(..., 0, False) 隐藏 CMD 窗口
        - 立即启动：用 subprocess.Popen CREATE_NO_WINDOW|DETACHED_PROCESS

        缺点（相比计划任务）：无崩溃自愈重启，但日常运行足够稳定。
        """
        vbs_path = _startup_vbs_path()
        if vbs_path is None:
            raise ServiceError(
                "无法定位开机启动文件夹（APPDATA 未设置）。\n"
                "  请手动将 `xskill connect --foreground` 加入开机自启，\n"
                "  或以管理员身份运行后重试。"
            )
        try:
            vbs_path.parent.mkdir(parents=True, exist_ok=True)
            vbs_path.write_text(_build_startup_vbs(argv), encoding="utf-8")
        except OSError as e:
            raise ServiceError(f"写开机启动脚本失败：{e}") from e

        # 立即 detach 启动（CREATE_NO_WINDOW=0x08000000, DETACHED_PROCESS=0x00000008）
        CREATE_NO_WINDOW = 0x08000000
        DETACHED_PROCESS = 0x00000008
        try:
            proc = subprocess.Popen(
                argv,
                creationflags=CREATE_NO_WINDOW | DETACHED_PROCESS,
                close_fds=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            pid = proc.pid
        except OSError as e:
            raise ServiceError(f"启动进程失败：{e}") from e

        write_daemon_state(method="startup_folder", backend=self.name,
                           vbs_path=str(vbs_path), argv=argv, pid=pid)
        logger.info("startup folder 方案安装成功：vbs=%s  pid=%s", vbs_path, pid)
        return self.status()

    def stop(self) -> dict:
        """停止并清理常驻任务（计划任务 或 启动文件夹，按 state 判断）。"""
        state = read_daemon_state()
        method = state.get("method", "schtasks")

        if method == "startup_folder":
            # 1. 删 .vbs 防下次登录自启
            vbs = state.get("vbs_path") or str(_startup_vbs_path() or "")
            if vbs:
                try:
                    Path(vbs).unlink(missing_ok=True)
                except OSError:
                    pass
            # 2. 按 pid 杀进程
            pid = state.get("pid")
            if pid and _pid_alive(pid):
                try:
                    subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                                   capture_output=True, check=False)
                except OSError:
                    pass
            clear_daemon_state()
            return {"running": False, "backend": self.name, "method": method}

        # 计划任务路径
        self._run(["/End", "/TN", self.task_name])
        delete = self._run(["/Delete", "/TN", self.task_name, "/F"])
        clear_daemon_state()
        st = {"running": False, "backend": self.name,
              "task_name": self.task_name, "method": method}
        err = (delete.stderr or "").strip()
        if delete.returncode != 0 and "cannot find" not in err.lower():
            st["warning"] = err or (delete.stdout or "").strip()
        return st

    def status(self) -> dict:
        state = read_daemon_state()
        method = state.get("method", "schtasks")

        if method == "startup_folder":
            pid = state.get("pid")
            vbs = state.get("vbs_path") or str(_startup_vbs_path() or "")
            installed = bool(vbs and Path(vbs).is_file())
            return {
                "installed": installed,
                "backend": self.name,
                "method": method,
                "vbs_path": vbs,
                "pid": pid,
                "running": _pid_alive(pid),
                "server_url": state.get("server_url"),
                "client_id": state.get("client_id"),
                "started_at": state.get("started_at"),
            }

        # 计划任务路径
        q = self._run(["/Query", "/TN", self.task_name, "/FO", "LIST", "/V"])
        if q.returncode != 0:
            return {"running": False, "installed": False,
                    "backend": self.name, "task_name": self.task_name,
                    "method": method}
        pid = self._query_pid()
        return {
            "installed": True,
            "backend": self.name,
            "task_name": self.task_name,
            "method": method,
            "pid": pid,
            "running": _pid_alive(pid),
            "server_url": state.get("server_url"),
            "client_id": state.get("client_id"),
            "started_at": state.get("started_at"),
            "schtasks_query": q.stdout.strip(),
        }

    def _query_pid(self) -> Optional[int]:
        """从 ``schtasks /Query /V`` 里取任务当前进程 PID（拿不到返回 None）。"""
        q = self._run(["/Query", "/TN", self.task_name, "/FO", "LIST", "/V"])
        if q.returncode != 0:
            return None
        for line in q.stdout.splitlines():
            # 本地化：英文 "PID:"、中文 "PID:" 都是这个键；只认冒号后数字。
            if line.strip().upper().startswith("PID:"):
                val = line.split(":", 1)[1].strip()
                if val.isdigit() and int(val) > 0:
                    return int(val)
        return None


# ═══════════════════════════════════════════════════════════════
# 平台选择
# ═══════════════════════════════════════════════════════════════

def get_backend() -> ConnectServiceBackend:
    """按当前平台返回后端。Linux/macOS 暂返回占位后端（操作即报“未实现”）。"""
    if sys.platform == "win32":
        return WindowsTaskSchedulerBackend()
    if sys.platform == "darwin":
        return _UnsupportedBackend("macOS", "launchd LaunchAgent，KeepAlive=true")
    return _UnsupportedBackend("Linux", "systemd --user，Restart=always")
