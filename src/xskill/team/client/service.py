"""service.py — ``xskill connect`` 常驻进程的“装/起/停/看”后端（Problem 2）

``xskill connect`` 本身是阻塞轮询循环（见 daemon.TeamClient.run_forever）。要让
它在用户不开终端的情况下持久后台运行、开机自启、崩溃自拉，就得把它托管给操作系统
的原生守护设施。本模块把这层抽象成可插拔后端：

    ConnectServiceBackend           抽象基类（含共享 pid/state 读写 + 存活校验）
      └─ WindowsTaskSchedulerBackend  Windows「计划任务」(schtasks)
                                      + Group Policy 拒绝时降级启动文件夹(supervisor)
      └─ LinuxServiceBackend          Linux 族（linux/wsl/harmony）能力探测选择
           ├─ SystemdUserBackend        systemd --user 可用时的首选
           ├─ SupervisedProcessBackend  无 systemd 的降级：watchdog 崩溃自愈
           └─ DetachedProcessBackend    裸 detached，仅显式 override 可达
      └─ LaunchdBackend               macOS launchd LaunchAgent       —— TODO(占位)

CLI (``xskill start/stop/status``) 只跟 ``get_backend()`` 打交道，不关心平台。

选择原则：按「能力探测」（systemd 可用？crontab 可用？WSL interop 可用？）逐级
降级，平台名（wsl/harmony/linux）只影响提示文案与开机自启的挂载方式；每一级降级
都在 status 的 ``crash_recovery`` / ``boot_autostart`` / ``degraded`` 里如实汇报，
不伪装成完整常驻，也不因为不完美而拒绝服务。

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
import shlex
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from xskill.config import get_connect_daemon_state_path

logger = logging.getLogger("xskill.team.client.service")

# Windows 计划任务名（TN）。带前缀避免和用户其他任务重名；schtasks 大小写不敏感。
WINDOWS_TASK_NAME = "Xskill_Connect"
SYSTEMD_UNIT_NAME = "xskill-connect.service"


WINDOWS_WSL_BOOT_TASK = "Xskill_WSL_Boot"


class ServiceError(RuntimeError):
    """后端操作失败（含平台不支持）。CLI 捕获后打印 message 即可。"""


# ═══════════════════════════════════════════════════════════════
# pid / 运行态：跨后端共享
# ═══════════════════════════════════════════════════════════════

def _pid_alive(pid: Optional[int]) -> bool:
    """pid 是否存活。signal 0 探测，权限错也算活；Linux 上僵尸视为死。

    容器/精简环境里 PID 1 常不收割孤儿（bash/应用直接当 init），被停掉的
    watchdog 会长期滞留为僵尸——signal 0 对僵尸返回成功，若不识别 Z 态，
    status 会误报 running、stop 会对尸体空等 + 无谓 SIGKILL。
    """
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
    if sys.platform.startswith("linux"):
        try:
            stat = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
            # comm 可含空格/括号，状态字段取最后一个 ')' 之后的首个 token
            if stat.rsplit(")", 1)[1].split()[0] == "Z":
                return False
        except (OSError, IndexError):
            pass
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


def update_daemon_state(**fields) -> None:
    """合并写运行态：保留已有键，仅覆盖传入键。

    后端与 supervisor watchdog 会先后写同一个 state 文件（后端写 method/
    backend，watchdog 补 watchdog_pid/child_pid）——整文件覆盖会互相抹掉
    对方的键，必须 read-merge-write。文件损坏时退化为全新写入。
    """
    path = get_connect_daemon_state_path()
    current: dict = {}
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                current = loaded
        except (OSError, ValueError):
            current = {}
    current.pop("running", None)   # 派生字段不落盘
    current.update(fields)
    current.setdefault("started_at", int(time.time()))
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(current, ensure_ascii=False),
                        encoding="utf-8")
    except OSError:
        logger.debug("update connect daemon state failed", exc_info=True)


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


def _supervise_argv() -> list[str]:
    """watchdog 进程的命令：``<python> -m xskill connect --supervise``。"""
    argv = _foreground_argv()
    return argv[:-1] + ["--supervise"]


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

        # /Run 返回 0 ≠ 进程真起来了：LogonTrigger 任务（未存凭据）只能在
        # 「用户已登录」的交互会话里启动，服务上下文/CI/断开的 RDP 里
        # schtasks 会报成功但任务永远不进 Running。按观测验证，拿不到
        # 任务进程 PID 就降级：direct-spawn supervisor 保证当下常驻，
        # 计划任务保留作下次登录自启。
        pid = self._wait_task_pid(timeout=self.TASK_START_TIMEOUT)
        if pid is None:
            logger.info(
                "schtasks /Run 成功但 %ss 内未观测到任务进程"
                "（无交互登录会话？），降级 direct-spawn supervisor",
                self.TASK_START_TIMEOUT)
            watchdog_pid = self._spawn_detached(_supervise_argv())
            write_daemon_state(task_name=self.task_name, backend=self.name,
                               method="schtasks", argv=argv,
                               launch="direct-spawn", watchdog_pid=watchdog_pid)
            return self.status()

        write_daemon_state(task_name=self.task_name, backend=self.name,
                           method="schtasks", argv=argv, pid=pid)
        return self.status()

    # /Run 后等任务进程出现的观测窗口（秒）。已登录桌面上任务 1-2s 就起。
    TASK_START_TIMEOUT = 10

    def _wait_task_pid(self, timeout: float) -> Optional[int]:
        deadline = time.time() + timeout
        while True:
            pid = self._query_pid()
            if pid is not None and _pid_alive(pid):
                return pid
            if time.time() >= deadline:
                return None
            time.sleep(1)

    @staticmethod
    def _spawn_detached(argv: list[str]) -> int:
        """CREATE_NO_WINDOW|DETACHED_PROCESS 拉起进程，返回 pid。"""
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
        except OSError as e:
            raise ServiceError(f"启动进程失败：{e}") from e
        return proc.pid

    def _install_startup_folder_and_spawn(self, argv: list[str]) -> dict:
        """降级方案：写 Startup 文件夹 .vbs 脚本 + 立即 detach 启动进程。

        适用于公司 Group Policy 禁止 schtasks 的场景。
        - 持久化：.vbs 在 %APPDATA%\\...\\Startup\\，用户登录即自动执行
        - 无窗口：WScript.Shell.Run(..., 0, False) 隐藏 CMD 窗口
        - 立即启动：用 subprocess.Popen CREATE_NO_WINDOW|DETACHED_PROCESS
        - 崩溃自愈：.vbs 与 detach 拉起的都是 supervisor watchdog
          （connect --supervise），schtasks RestartOnFailure 的用户态等价物。
        """
        del argv  # 调主任务用 foreground argv；本降级路径固定走 supervisor。
        watchdog_argv = _supervise_argv()
        vbs_path = _startup_vbs_path()
        if vbs_path is None:
            raise ServiceError(
                "无法定位开机启动文件夹（APPDATA 未设置）。\n"
                "  请手动将 `xskill connect --foreground` 加入开机自启，\n"
                "  或以管理员身份运行后重试。"
            )
        try:
            vbs_path.parent.mkdir(parents=True, exist_ok=True)
            vbs_path.write_text(_build_startup_vbs(watchdog_argv),
                                encoding="utf-8")
        except OSError as e:
            raise ServiceError(f"写开机启动脚本失败：{e}") from e

        pid = self._spawn_detached(watchdog_argv)

        write_daemon_state(method="startup_folder", backend=self.name,
                           vbs_path=str(vbs_path), argv=watchdog_argv,
                           watchdog_pid=pid, pid=pid)
        logger.info("startup folder 方案安装成功：vbs=%s  watchdog pid=%s",
                    vbs_path, pid)
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
            # 2. 杀 watchdog 进程树（/T 连 connect 子进程一起）
            for pid in {state.get("watchdog_pid"), state.get("pid"),
                        state.get("child_pid")}:
                if pid and _pid_alive(pid):
                    try:
                        subprocess.run(
                            ["taskkill", "/PID", str(pid), "/T", "/F"],
                            capture_output=True, check=False)
                    except OSError:
                        pass
            clear_daemon_state()
            return {"running": False, "backend": self.name, "method": method}

        # 计划任务路径
        self._run(["/End", "/TN", self.task_name])
        delete = self._run(["/Delete", "/TN", self.task_name, "/F"])
        # direct-spawn 降级过的还有 watchdog 进程树要杀（/End 只管任务进程）
        for pid in {state.get("watchdog_pid"), state.get("child_pid")}:
            if pid and _pid_alive(pid):
                try:
                    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                                   capture_output=True, check=False)
                except OSError:
                    pass
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
            wpid = state.get("watchdog_pid") or state.get("pid")
            vbs = state.get("vbs_path") or str(_startup_vbs_path() or "")
            installed = bool(vbs and Path(vbs).is_file())
            return {
                "installed": installed,
                "backend": self.name,
                "method": method,
                "vbs_path": vbs,
                "pid": state.get("child_pid") or wpid,
                "watchdog_pid": wpid,
                "child_alive": _pid_alive(state.get("child_pid")),
                "running": _pid_alive(wpid),
                "crash_recovery": "watchdog",
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
        wpid = state.get("watchdog_pid")
        direct_spawn = state.get("launch") == "direct-spawn"
        return {
            "installed": True,
            "backend": self.name,
            "task_name": self.task_name,
            "method": method,
            "pid": pid or state.get("child_pid") or wpid,
            "watchdog_pid": wpid,
            # 任务进程或 direct-spawn 的 watchdog 任一存活即 running
            "running": _pid_alive(pid) or _pid_alive(wpid),
            "server_url": state.get("server_url"),
            "client_id": state.get("client_id"),
            "started_at": state.get("started_at"),
            "crash_recovery": "watchdog" if direct_spawn else "schtasks",
            "launch": state.get("launch"),
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
# Linux 族（linux / wsl / harmony）：平台与能力探测
# ═══════════════════════════════════════════════════════════════

def _is_wsl() -> bool:
    """当前 Linux 是否运行在 WSL。"""
    if os.environ.get("WSL_DISTRO_NAME") or os.environ.get("WSL_INTEROP"):
        return True
    try:
        release = Path("/proc/sys/kernel/osrelease").read_text(encoding="utf-8")
    except OSError:
        return False
    return "microsoft" in release.lower()


_HARMONY_IDS = {"harmonyos", "openharmony", "ohos"}


def _is_harmony(os_release_path: str = "/etc/os-release") -> bool:
    """当前 Linux 是否鸿蒙用户态（HarmonyOS / OpenHarmony）。

    鸿蒙终端 = Linux 内核 + 自有 init（无 systemd）。识别只影响提示文案与
    开机自启挂载方式，常驻主链路与普通无 systemd Linux 完全一致。
    """
    try:
        text = Path(os_release_path).read_text(encoding="utf-8")
    except OSError:
        text = ""
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep and key.strip() in ("ID", "ID_LIKE"):
            values = value.strip().strip('"').lower().split()
            if _HARMONY_IDS & set(values):
                return True
    return "ohos" in os.uname().release.lower() if hasattr(os, "uname") else False


def _linux_flavor() -> str:
    """``"wsl" | "harmony" | "linux"``。wsl 判定优先（interop 语义更特殊）。"""
    if _is_wsl():
        return "wsl"
    if _is_harmony():
        return "harmony"
    return "linux"


def _linux_platform_name() -> str:
    return _linux_flavor()


def _systemd_user_available() -> bool:
    """用户级 systemd manager 是否可用。

    WSL 只有在 /etc/wsl.conf 启用 systemd 后才满足；普通 Linux 的精简容器、
    没有 user bus 的 SSH 环境、鸿蒙终端都会返回 False，随后落到 supervised
    watchdog 链路。
    """
    if (os.environ.get("XSKILL_CONNECT_BACKEND", "").strip().lower()
            in ("detached", "supervised")):
        return False
    if not shutil.which("systemctl"):
        return False
    try:
        cp = subprocess.run(
            ["systemctl", "--user", "show-environment"],
            capture_output=True, text=True, check=False, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return cp.returncode == 0


# ═══════════════════════════════════════════════════════════════
# 开机自启挂载（与 systemd/supervised 后端正交的能力层）
# ═══════════════════════════════════════════════════════════════
#
# WSL 的关键事实：即使发行版内 systemd + linger 齐备，Windows 重启后 WSL VM
# 也不会自动拉起——「开机自启」只能靠 Windows 侧触发器经 interop 调 wsl.exe。
# 普通 Linux/鸿蒙无 systemd 时则用 crontab @reboot。两者挂的都是幂等的
# ``xskill start --quiet``（已在跑则静默退出），触发器可无脑重复执行。

_CRON_MARKER = "# xskill-connect-boot"


def _boot_start_command() -> str:
    return shlex.join([sys.executable or "python", "-m", "xskill",
                       "start", "--quiet"])


def _crontab_available() -> bool:
    """crontab 可用 = 命令存在且能读（退出码 0 或 1=「no crontab for user」）。"""
    if not shutil.which("crontab"):
        return False
    try:
        cp = subprocess.run(["crontab", "-l"], capture_output=True,
                            text=True, check=False, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return False
    return cp.returncode in (0, 1)


def _read_crontab_lines() -> list[str]:
    try:
        cp = subprocess.run(["crontab", "-l"], capture_output=True,
                            text=True, check=False, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return []
    return cp.stdout.splitlines() if cp.returncode == 0 else []


def _write_crontab_lines(lines: list[str]) -> bool:
    text = "\n".join(lines)
    if text and not text.endswith("\n"):
        text += "\n"
    try:
        cp = subprocess.run(["crontab", "-"], input=text, capture_output=True,
                            text=True, check=False, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return False
    return cp.returncode == 0


def _install_cron_boot() -> bool:
    """幂等挂 ``@reboot … xskill start --quiet`` 行（marker 去重）。"""
    if not _crontab_available():
        return False
    lines = [ln for ln in _read_crontab_lines() if _CRON_MARKER not in ln]
    lines.append(f"@reboot {_boot_start_command()} {_CRON_MARKER}")
    return _write_crontab_lines(lines)


def _remove_cron_boot() -> None:
    if not _crontab_available():
        return
    lines = _read_crontab_lines()
    kept = [ln for ln in lines if _CRON_MARKER not in ln]
    if kept != lines:
        _write_crontab_lines(kept)


def _wsl_interop_available() -> bool:
    """WSL interop 是否可调 Windows 侧工具（wsl.exe + schtasks.exe 在 PATH）。

    WSL 默认把 Windows PATH 追加进来；interop 被 /etc/wsl.conf 关闭或用户
    精简了 PATH 时探测失败——此时开机自启降级为「无」并在 status 里明示。
    """
    return bool(shutil.which("wsl.exe") and shutil.which("schtasks.exe"))


def _install_wsl_boot_task() -> bool:
    """经 interop 在 Windows 侧挂登录触发任务：wsl.exe 里跑 xskill start。

    schtasks.exe /SC ONLOGON 对当前用户无需管理员；被 Group Policy 拒绝时
    返回 False（调用方记 degraded，不阻断常驻本身）。
    """
    distro = os.environ.get("WSL_DISTRO_NAME", "").strip()
    if not distro or not _wsl_interop_available():
        return False
    import getpass
    try:
        user = getpass.getuser()
    except Exception:
        user = ""
    inner = _boot_start_command()
    user_part = f"-u {user} " if user else ""
    tr = f"wsl.exe -d {distro} {user_part}-- {inner}"
    try:
        cp = subprocess.run(
            ["schtasks.exe", "/Create", "/TN", WINDOWS_WSL_BOOT_TASK,
             "/SC", "ONLOGON", "/TR", tr, "/F"],
            capture_output=True, text=True, check=False, timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if cp.returncode != 0:
        logger.info("WSL boot task 创建失败（degraded 继续）：%s",
                    (cp.stderr or cp.stdout or "").strip())
    return cp.returncode == 0


def _remove_wsl_boot_task() -> None:
    if not _wsl_interop_available():
        return
    try:
        subprocess.run(
            ["schtasks.exe", "/Delete", "/TN", WINDOWS_WSL_BOOT_TASK, "/F"],
            capture_output=True, text=True, check=False, timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        pass


def _install_boot_autostart(flavor: str, *,
                            systemd_linger: bool = False
                            ) -> tuple[str, list[str]]:
    """按 flavor 挂开机自启。返回 (boot_autostart 标识, degraded 警告列表)。

    任何失败都只降级不抛错——常驻本身（自愈）已就位，自启缺失是可接受的
    降级，必须让用户看得见（degraded），但不能因此拒绝服务。
    """
    warnings: list[str] = []
    if flavor == "wsl":
        # systemd/linger 只覆盖「VM 内」自启；VM 本身要 Windows 侧拉起。
        if _install_wsl_boot_task():
            return "windows-task", warnings
        warnings.append(
            "未能注册 Windows 侧开机任务（interop 不可用或被策略拒绝）：Windows"
            " 重启后需手动进一次 WSL 或跑 `xskill start`。")
        if systemd_linger:
            return "systemd-linger", warnings   # 至少 VM 内自启还在
        return "none", warnings
    if systemd_linger:
        return "systemd-linger", warnings
    if _install_cron_boot():
        return "cron", warnings
    warnings.append(
        "未能注册开机自启（无 systemd linger，且 crontab 不可用）：重启后需"
        "手动跑 `xskill start`。")
    return "none", warnings


def _remove_boot_autostart(state: dict) -> None:
    """卸载开机自启挂载。按 state 记录的方式卸，兜底两种都试（幂等）。"""
    mode = state.get("boot_autostart")
    if mode == "windows-task" or _is_wsl():
        _remove_wsl_boot_task()
    if mode == "cron" or mode is None:
        _remove_cron_boot()


class SystemdUserBackend(ConnectServiceBackend):
    """用 ``systemd --user`` 托管 connect，支持自启和崩溃自动重启。"""

    name = "linux"
    method = "systemd-user"

    def __init__(self, unit_name: str = SYSTEMD_UNIT_NAME,
                 unit_path: Path | None = None):
        self.unit_name = unit_name
        self.unit_path = unit_path or (
            Path.home() / ".config" / "systemd" / "user" / unit_name
        )

    def _run(self, args: list[str]) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(
                ["systemctl", "--user", *args], capture_output=True,
                text=True, check=False, timeout=15,
            )
        except (OSError, subprocess.SubprocessError) as e:
            raise ServiceError(f"systemd --user 执行失败：{e}") from e

    def _unit_text(self) -> str:
        command = shlex.join(_foreground_argv())
        return (
            "[Unit]\n"
            "Description=xskill team client\n"
            "Wants=network-online.target\n"
            "After=network-online.target\n\n"
            "[Service]\n"
            "Type=simple\n"
            f"ExecStart={command}\n"
            "Restart=always\n"
            "RestartSec=10\n"
            "Environment=PYTHONUNBUFFERED=1\n\n"
            "[Install]\n"
            "WantedBy=default.target\n"
        )

    @staticmethod
    def _enable_linger() -> bool:
        """允许 user manager 随系统启动，而不是依赖当前终端会话。"""
        user = os.environ.get("USER")
        if not user or not shutil.which("loginctl"):
            return False
        try:
            linger = subprocess.run(
                ["loginctl", "enable-linger", user], capture_output=True,
                text=True, check=False, timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return linger.returncode == 0

    def install_and_start(self) -> dict:
        # linger 失败不再硬失败（旧版对 WSL 直接 raise）：常驻与崩溃自愈由
        # unit 本身保证，linger 只影响「重启后无登录也自启」——那属于
        # boot_autostart 层的降级，由 LinuxServiceBackend 补 cron/Windows
        # 任务并记 degraded。
        linger_enabled = self._enable_linger()
        try:
            self.unit_path.parent.mkdir(parents=True, exist_ok=True)
            self.unit_path.write_text(self._unit_text(), encoding="utf-8")
        except OSError as e:
            raise ServiceError(f"写 systemd user unit 失败：{e}") from e

        reload_cp = self._run(["daemon-reload"])
        if reload_cp.returncode != 0:
            raise ServiceError(
                "systemd user daemon-reload 失败：\n  "
                + (reload_cp.stderr.strip() or reload_cp.stdout.strip())
            )
        start_cp = self._run(["enable", "--now", self.unit_name])
        if start_cp.returncode != 0:
            raise ServiceError(
                "启用 systemd user service 失败：\n  "
                + (start_cp.stderr.strip() or start_cp.stdout.strip())
            )

        st = self.status()
        if not st.get("running"):
            raise ServiceError(
                "systemd unit 已安装但未进入 running；"
                f"请运行 `journalctl --user -u {self.unit_name} -n 50` 排查。"
            )
        write_daemon_state(
            backend=self.name, method=self.method, unit_name=self.unit_name,
            unit_path=str(self.unit_path), pid=st.get("pid"),
            platform=_linux_platform_name(), linger_enabled=linger_enabled,
        )
        return self.status()

    def stop(self) -> dict:
        cp = self._run(["disable", "--now", self.unit_name])
        warning = ""
        if cp.returncode != 0:
            warning = cp.stderr.strip() or cp.stdout.strip()
        try:
            self.unit_path.unlink(missing_ok=True)
        except OSError as e:
            warning = warning or str(e)
        self._run(["daemon-reload"])
        clear_daemon_state()
        st = {
            "running": False, "installed": False, "backend": self.name,
            "method": self.method, "platform": _linux_platform_name(),
        }
        if warning and "not loaded" not in warning.lower():
            st["warning"] = warning
        return st

    def status(self) -> dict:
        state = read_daemon_state()
        cp = self._run([
            "show", self.unit_name,
            "--property=LoadState", "--property=ActiveState",
            "--property=SubState", "--property=MainPID",
        ])
        props: dict[str, str] = {}
        if cp.returncode == 0:
            for line in cp.stdout.splitlines():
                key, sep, value = line.partition("=")
                if sep:
                    props[key] = value
        pid_text = props.get("MainPID", "")
        pid = int(pid_text) if pid_text.isdigit() and int(pid_text) > 0 else None
        installed = props.get("LoadState") == "loaded" or self.unit_path.is_file()
        running = (props.get("ActiveState") == "active"
                   and props.get("SubState") == "running")
        return {
            "installed": installed,
            "running": running,
            "backend": self.name,
            "method": self.method,
            "platform": _linux_platform_name(),
            "unit_name": self.unit_name,
            "unit_path": str(self.unit_path),
            "pid": pid,
            "server_url": state.get("server_url"),
            "client_id": state.get("client_id"),
            "started_at": state.get("started_at"),
            "linger_enabled": state.get("linger_enabled"),
            "crash_recovery": "systemd",
        }


def _pid_is_connect_daemon(pid: Optional[int]) -> bool:
    """避免 detached 状态文件里的陈旧 PID 误杀无关进程。"""
    if not _pid_alive(pid):
        return False
    cmdline = Path(f"/proc/{pid}/cmdline")
    try:
        args = cmdline.read_bytes().split(b"\0")
    except OSError:
        return True
    return b"xskill" in args and b"connect" in args and b"--foreground" in args


class DetachedProcessBackend(ConnectServiceBackend):
    """无 systemd 时脱离终端运行；适用于未启用 systemd 的 WSL/精简 Linux。"""

    name = "linux"
    method = "detached"

    def install_and_start(self) -> dict:
        current = self.status()
        if current.get("running"):
            return current
        argv = _foreground_argv()
        state_path = get_connect_daemon_state_path()
        log_path = state_path.parent / "logs" / "connect-daemon.log"
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("ab") as log_file:
                proc = subprocess.Popen(
                    argv, cwd=str(Path.home()), stdin=subprocess.DEVNULL,
                    stdout=log_file, stderr=subprocess.STDOUT,
                    start_new_session=True, close_fds=True,
                )
        except OSError as e:
            raise ServiceError(f"启动 detached connect 进程失败：{e}") from e
        write_daemon_state(
            backend=self.name, method=self.method, pid=proc.pid, argv=argv,
            platform=_linux_platform_name(), log_path=str(log_path),
        )
        return self.status()

    def stop(self) -> dict:
        state = read_daemon_state()
        pid = state.get("pid")
        warning = ""
        if _pid_is_connect_daemon(pid):
            try:
                os.kill(pid, signal.SIGTERM)
                deadline = time.time() + 5
                while _pid_alive(pid) and time.time() < deadline:
                    time.sleep(0.05)
                if _pid_alive(pid):
                    os.kill(pid, signal.SIGKILL)
            except OSError as e:
                warning = str(e)
        clear_daemon_state()
        st = {
            "running": False, "installed": False, "backend": self.name,
            "method": self.method, "platform": _linux_platform_name(),
        }
        if warning:
            st["warning"] = warning
        return st

    def status(self) -> dict:
        state = read_daemon_state()
        pid = state.get("pid") if state.get("method") == self.method else None
        return {
            "installed": bool(pid),
            "running": _pid_is_connect_daemon(pid),
            "backend": self.name,
            "method": self.method,
            "platform": _linux_platform_name(),
            "pid": pid,
            "server_url": state.get("server_url"),
            "client_id": state.get("client_id"),
            "started_at": state.get("started_at"),
            "log_path": state.get("log_path"),
            "restart_policy": "none",
        }


class SupervisedProcessBackend(ConnectServiceBackend):
    """无 systemd 平台的常驻：detach 一个 watchdog，由它自愈 connect 子进程。

    适用于未启 systemd 的 WSL、精简/老 Linux、鸿蒙终端。watchdog 主体见
    supervisor.py（指数退避重启、SIGTERM 级联、child_pid 回写 state）。
    ``running`` 以 watchdog 存活为准——子进程崩溃是 watchdog 的正常工况
    （退避窗口内 child 短暂不在），单侧状态另以 child_alive 汇报。
    """

    name = "linux"
    method = "supervised"

    def install_and_start(self) -> dict:
        current = self.status()
        if current.get("running"):
            return current
        argv = _supervise_argv()
        state_path = get_connect_daemon_state_path()
        log_path = state_path.parent / "logs" / "connect-supervisor.log"
        # 静态字段在 spawn 前整写；spawn 后 watchdog 会合并写 watchdog_pid/
        # child_pid——若 spawn 后再整写会与 watchdog 的合并写竞态互抹。
        write_daemon_state(
            backend=self.name, method=self.method, argv=argv,
            platform=_linux_flavor(), log_path=str(log_path),
        )
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("ab") as log_file:
                proc = subprocess.Popen(
                    argv, cwd=str(Path.home()), stdin=subprocess.DEVNULL,
                    stdout=log_file, stderr=subprocess.STDOUT,
                    start_new_session=True, close_fds=True,
                )
        except OSError as e:
            clear_daemon_state()
            raise ServiceError(f"启动 supervisor watchdog 失败：{e}") from e
        update_daemon_state(watchdog_pid=proc.pid)
        # 等 watchdog 把首个 connect 子进程拉起来（最多 10s）——让 start 的
        # 返回状态里就带上 child_pid，用户不必二次 status 确认。
        deadline = time.time() + 10
        while time.time() < deadline:
            st = read_daemon_state()
            if _pid_alive(st.get("child_pid")):
                break
            if not _pid_alive(proc.pid):
                raise ServiceError(
                    "supervisor watchdog 启动后立即退出；"
                    f"请查看日志 {log_path} 排查。")
            time.sleep(0.2)
        return self.status()

    def stop(self) -> dict:
        state = read_daemon_state()
        warning = ""
        wpid = state.get("watchdog_pid")
        if _pid_alive(wpid):
            try:
                os.kill(wpid, signal.SIGTERM)
                # watchdog 收 SIGTERM 后最多 5s 宽限杀 child，再留余量。
                deadline = time.time() + 8
                while _pid_alive(wpid) and time.time() < deadline:
                    time.sleep(0.05)
                if _pid_alive(wpid):
                    os.kill(wpid, signal.SIGKILL)
            except OSError as e:
                warning = str(e)
        # 兜底：watchdog 已死但 child 还挂着（如 watchdog 被 SIGKILL 过）。
        cpid = state.get("child_pid")
        if _pid_is_connect_daemon(cpid):
            try:
                os.kill(cpid, signal.SIGTERM)
                deadline = time.time() + 5
                while _pid_alive(cpid) and time.time() < deadline:
                    time.sleep(0.05)
                if _pid_alive(cpid):
                    os.kill(cpid, signal.SIGKILL)
            except OSError as e:
                warning = warning or str(e)
        clear_daemon_state()
        st = {
            "running": False, "installed": False, "backend": self.name,
            "method": self.method, "platform": _linux_flavor(),
        }
        if warning:
            st["warning"] = warning
        return st

    def status(self) -> dict:
        state = read_daemon_state()
        if state.get("method") != self.method:
            return {"installed": False, "running": False,
                    "backend": self.name, "method": self.method,
                    "platform": _linux_flavor()}
        wpid = state.get("watchdog_pid")
        cpid = state.get("child_pid")
        watchdog_alive = _pid_alive(wpid)
        child_alive = _pid_is_connect_daemon(cpid)
        return {
            "installed": bool(wpid),
            "running": watchdog_alive,
            "backend": self.name,
            "method": self.method,
            "platform": _linux_flavor(),
            "pid": cpid,
            "watchdog_pid": wpid,
            "watchdog_alive": watchdog_alive,
            "child_alive": child_alive,
            "server_url": state.get("server_url"),
            "client_id": state.get("client_id"),
            "started_at": state.get("started_at"),
            "log_path": state.get("log_path"),
            "crash_recovery": "watchdog",
            "boot_autostart": state.get("boot_autostart"),
        }


class LinuxServiceBackend(ConnectServiceBackend):
    """Linux 族入口：能力探测选择 systemd/supervised，并编排开机自启挂载。

    旧版曾对「WSL 无 systemd」硬失败（WSLSystemdRequiredBackend）——策略过苛
    且没解决真问题（systemd+linger 也管不了 Windows 重启后 VM 不自启）。现在：
    崩溃自愈由 systemd 或 watchdog 保证，开机自启由 _install_boot_autostart
    按能力尽力挂载，挂不上只记 degraded。
    """

    name = "linux"

    @staticmethod
    def _select_for_install() -> ConnectServiceBackend:
        override = os.environ.get("XSKILL_CONNECT_BACKEND", "").strip().lower()
        if override == "detached":
            return DetachedProcessBackend()
        if override == "supervised":
            return SupervisedProcessBackend()
        if override == "systemd" or _systemd_user_available():
            return SystemdUserBackend()
        return SupervisedProcessBackend()

    @staticmethod
    def _from_state() -> ConnectServiceBackend:
        method = read_daemon_state().get("method")
        if method == SystemdUserBackend.method:
            return SystemdUserBackend()
        if method == SupervisedProcessBackend.method:
            return SupervisedProcessBackend()
        if method == DetachedProcessBackend.method:
            return DetachedProcessBackend()
        return LinuxServiceBackend._select_for_install()

    def install_and_start(self) -> dict:
        target = self._select_for_install()

        # 换后端（如旧 detached → systemd/supervised）先停旧进程防双 daemon。
        state = read_daemon_state()
        old_method = state.get("method")
        if old_method and old_method != target.method:
            try:
                self._from_state().stop()
            except ServiceError:
                logger.warning("停止旧 %s 后端失败，继续安装 %s",
                               old_method, target.method, exc_info=True)

        try:
            st = target.install_and_start()
        except ServiceError:
            if isinstance(target, SystemdUserBackend):
                # systemd 探测通过但安装失败（unit 拒载等）→ 降级 supervised，
                # 任何 Linux 族平台一视同仁（旧版 WSL 在此硬 raise）。
                logger.warning("systemd user 安装失败，降级为 supervised",
                               exc_info=True)
                st = SupervisedProcessBackend().install_and_start()
            else:
                raise

        # 开机自启挂载 + 降级如实记录（detached 是显式 override 的裸模式，
        # 保持历史语义：不挂自启）。
        if st.get("method") != DetachedProcessBackend.method:
            flavor = _linux_flavor()
            linger = bool(read_daemon_state().get("linger_enabled"))
            boot, warnings = _install_boot_autostart(
                flavor, systemd_linger=linger)
            update_daemon_state(boot_autostart=boot, flavor=flavor,
                                degraded=warnings)
        return self.status()

    def stop(self) -> dict:
        state = read_daemon_state()
        st = self._from_state().stop()
        _remove_boot_autostart(state)
        return st

    def status(self) -> dict:
        st = self._from_state().status()
        state = read_daemon_state()
        st.setdefault("flavor", state.get("flavor") or _linux_flavor())
        if state.get("boot_autostart") is not None:
            st["boot_autostart"] = state.get("boot_autostart")
        degraded = state.get("degraded") or []
        if degraded:
            st["degraded"] = degraded
        if "crash_recovery" not in st:
            st["crash_recovery"] = (
                "systemd" if st.get("method") == SystemdUserBackend.method
                else "none")
        return st


# ═══════════════════════════════════════════════════════════════
# 平台选择
# ═══════════════════════════════════════════════════════════════

def get_backend() -> ConnectServiceBackend:
    """按当前平台返回常驻后端。"""
    if sys.platform == "win32":
        return WindowsTaskSchedulerBackend()
    if sys.platform.startswith("linux"):
        return LinuxServiceBackend()
    if sys.platform == "darwin":
        return _UnsupportedBackend("macOS", "launchd LaunchAgent，KeepAlive=true")
    return _UnsupportedBackend(sys.platform, "请使用该平台的服务管理器")
