"""WSL / 鸿蒙 / 无 systemd Linux 的常驻策略（cross-platform-persistence）。

旧策略「WSL 无 systemd 直接硬失败」已废除：崩溃自愈由 supervised watchdog
兜底，开机自启按能力（WSL interop / crontab / linger）尽力挂载，挂不上只记
degraded——按能力探测降级，不按平台名一刀切。
"""
from __future__ import annotations

import types

import pytest

import xskill.team.client.service as svc


@pytest.fixture
def state_path(tmp_path, monkeypatch):
    p = tmp_path / "connect_daemon.json"
    monkeypatch.setattr(svc, "get_connect_daemon_state_path", lambda: p)
    return p


def _stub_supervised(monkeypatch):
    """把 SupervisedProcessBackend 打成不起真进程的替身。"""
    monkeypatch.setattr(
        svc.SupervisedProcessBackend, "install_and_start",
        lambda self: svc.write_daemon_state(method=self.method)
        or {"running": True, "method": self.method,
            "crash_recovery": "watchdog"},
    )
    monkeypatch.setattr(
        svc.SupervisedProcessBackend, "status",
        lambda self: {"running": True, "method": self.method,
                      "crash_recovery": "watchdog"},
    )


# ─────────────── WSL：无 systemd 不再拒绝，落 supervised ───────────────

def test_wsl_without_systemd_falls_back_to_supervised(monkeypatch, state_path):
    monkeypatch.setattr(svc, "_is_wsl", lambda: True)
    monkeypatch.setattr(svc, "_systemd_user_available", lambda: False)
    monkeypatch.setattr(svc, "_install_wsl_boot_task", lambda: True)
    _stub_supervised(monkeypatch)

    st = svc.LinuxServiceBackend().install_and_start()   # 不抛 ServiceError
    assert st["running"] is True
    assert st["method"] == "supervised"
    assert st["crash_recovery"] == "watchdog"
    assert st["boot_autostart"] == "windows-task"


def test_wsl_without_systemd_nor_interop_degrades_visibly(monkeypatch, state_path):
    """interop 也不可用：仍常驻（自愈），但 degraded 必须明示自启缺失。"""
    monkeypatch.setattr(svc, "_is_wsl", lambda: True)
    monkeypatch.setattr(svc, "_systemd_user_available", lambda: False)
    monkeypatch.setattr(svc, "_install_wsl_boot_task", lambda: False)
    _stub_supervised(monkeypatch)

    st = svc.LinuxServiceBackend().install_and_start()
    assert st["running"] is True
    assert st["boot_autostart"] == "none"
    assert any("开机" in w or "start" in w for w in st["degraded"])


def test_plain_linux_without_systemd_uses_supervised(monkeypatch, state_path):
    monkeypatch.setattr(svc, "_is_wsl", lambda: False)
    monkeypatch.setattr(svc, "_is_harmony", lambda: False)
    monkeypatch.setattr(svc, "_systemd_user_available", lambda: False)
    monkeypatch.setattr(svc, "_install_cron_boot", lambda: True)
    _stub_supervised(monkeypatch)

    st = svc.LinuxServiceBackend().install_and_start()
    assert st["method"] == "supervised"
    assert st["boot_autostart"] == "cron"


# ─────────────── WSL + systemd：linger 失败降级而非硬失败 ───────────────

def test_wsl_systemd_linger_failure_no_longer_fatal(monkeypatch, tmp_path,
                                                    state_path):
    monkeypatch.setattr(svc, "_is_wsl", lambda: True)
    monkeypatch.setattr(svc.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setenv("USER", "alice")

    def fake_run(args, **kwargs):
        if args[:2] == ["loginctl", "enable-linger"]:
            return types.SimpleNamespace(returncode=1, stdout="", stderr="denied")
        stdout = ""
        if "show" in args:
            stdout = ("LoadState=loaded\nActiveState=active\n"
                      "SubState=running\nMainPID=2468\n")
        return types.SimpleNamespace(returncode=0, stdout=stdout, stderr="")

    monkeypatch.setattr(svc.subprocess, "run", fake_run)
    unit = tmp_path / "xskill-connect.service"

    st = svc.SystemdUserBackend(unit_path=unit).install_and_start()   # 不抛
    assert unit.exists()
    assert st["running"] is True
    assert st["linger_enabled"] is False


# ─────────────── WSL + systemd：仍要挂 Windows 侧开机任务 ───────────────

def test_wsl_systemd_still_installs_windows_boot_task(monkeypatch, state_path):
    """systemd+linger 只管 VM 内自启；Windows 重启后 VM 要 Windows 侧拉起。"""
    monkeypatch.setattr(svc, "_is_wsl", lambda: True)
    monkeypatch.setattr(svc, "_systemd_user_available", lambda: True)
    monkeypatch.setattr(
        svc.SystemdUserBackend, "install_and_start",
        lambda self: svc.write_daemon_state(method=self.method,
                                            linger_enabled=True)
        or {"running": True, "method": self.method},
    )
    monkeypatch.setattr(
        svc.SystemdUserBackend, "status",
        lambda self: {"running": True, "method": self.method},
    )
    calls = []
    monkeypatch.setattr(svc, "_install_wsl_boot_task",
                        lambda: calls.append(1) or True)

    st = svc.LinuxServiceBackend().install_and_start()
    assert calls == [1]
    assert st["boot_autostart"] == "windows-task"


# ─────────────── 开机自启挂载决策表 ───────────────

@pytest.mark.parametrize(
    "flavor,linger,interop_ok,cron_ok,expect",
    [
        ("wsl", False, True, False, "windows-task"),
        ("wsl", True, False, False, "systemd-linger"),   # interop 挂不上，VM 内自启兜底
        ("wsl", False, False, False, "none"),
        ("linux", True, False, False, "systemd-linger"),
        ("linux", False, False, True, "cron"),
        ("harmony", False, False, True, "cron"),
        ("harmony", False, False, False, "none"),
    ],
)
def test_boot_autostart_decision(monkeypatch, flavor, linger, interop_ok,
                                 cron_ok, expect):
    monkeypatch.setattr(svc, "_install_wsl_boot_task", lambda: interop_ok)
    monkeypatch.setattr(svc, "_install_cron_boot", lambda: cron_ok)
    mode, warnings = svc._install_boot_autostart(flavor, systemd_linger=linger)
    assert mode == expect
    if expect == "none":
        assert warnings   # 降到无自启必须有人类可读的警告


# ─────────────── WSL interop：Windows 侧任务命令拼装 ───────────────

def test_wsl_boot_task_command_assembly(monkeypatch):
    calls = []
    monkeypatch.setenv("WSL_DISTRO_NAME", "Ubuntu-22.04")
    monkeypatch.setattr(svc.shutil, "which", lambda name: f"/mnt/c/win/{name}")

    def fake_run(args, **kw):
        calls.append(list(args))
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(svc.subprocess, "run", fake_run)
    import getpass
    monkeypatch.setattr(getpass, "getuser", lambda: "alice")

    assert svc._install_wsl_boot_task() is True
    (create,) = calls
    assert create[:2] == ["schtasks.exe", "/Create"]
    assert svc.WINDOWS_WSL_BOOT_TASK in create
    tr = create[create.index("/TR") + 1]
    assert "wsl.exe -d Ubuntu-22.04" in tr
    assert "-u alice" in tr
    assert "xskill" in tr and "start" in tr and "--quiet" in tr

    svc._remove_wsl_boot_task()
    assert calls[1][:2] == ["schtasks.exe", "/Delete"]
    assert svc.WINDOWS_WSL_BOOT_TASK in calls[1]


def test_wsl_boot_task_requires_distro_and_interop(monkeypatch):
    monkeypatch.delenv("WSL_DISTRO_NAME", raising=False)
    assert svc._install_wsl_boot_task() is False       # 无发行版名
    monkeypatch.setenv("WSL_DISTRO_NAME", "Ubuntu")
    monkeypatch.setattr(svc.shutil, "which", lambda name: None)
    assert svc._install_wsl_boot_task() is False       # interop 不可用


# ─────────────── 鸿蒙识别 ───────────────

@pytest.mark.parametrize(
    "os_release,expected",
    [
        ('NAME="HarmonyOS"\nID=harmonyos\nVERSION_ID=5.1\n', True),
        ('NAME=OpenHarmony\nID=openharmony\n', True),
        ('ID=euleros\nID_LIKE="openharmony linux"\n', True),
        ('NAME="Ubuntu"\nID=ubuntu\nID_LIKE=debian\n', False),
        ("", False),
    ],
)
def test_is_harmony_from_os_release(tmp_path, os_release, expected):
    p = tmp_path / "os-release"
    p.write_text(os_release, encoding="utf-8")
    assert svc._is_harmony(str(p)) is expected


def test_linux_flavor_priority(monkeypatch):
    monkeypatch.setattr(svc, "_is_wsl", lambda: True)
    monkeypatch.setattr(svc, "_is_harmony", lambda: True)
    assert svc._linux_flavor() == "wsl"     # wsl 判定优先
    monkeypatch.setattr(svc, "_is_wsl", lambda: False)
    assert svc._linux_flavor() == "harmony"
    monkeypatch.setattr(svc, "_is_harmony", lambda: False)
    assert svc._linux_flavor() == "linux"


# ─────────────── cron @reboot marker 幂等 ───────────────

class _FakeCrontab:
    """内存版 crontab：crontab -l 读、crontab - 写。"""

    def __init__(self, initial: str = ""):
        self.content = initial

    def __call__(self, args, **kw):
        if args[:2] == ["crontab", "-l"]:
            rc = 0 if self.content else 1
            return types.SimpleNamespace(returncode=rc, stdout=self.content,
                                         stderr="" if rc == 0 else "no crontab")
        if args[:2] == ["crontab", "-"]:
            self.content = kw.get("input", "")
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")


def test_cron_boot_install_is_idempotent(monkeypatch):
    fake = _FakeCrontab("0 3 * * * /usr/bin/backup.sh\n")
    monkeypatch.setattr(svc.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(svc.subprocess, "run", fake)

    assert svc._install_cron_boot() is True
    assert svc._install_cron_boot() is True   # 再装一次不重复
    lines = [ln for ln in fake.content.splitlines() if svc._CRON_MARKER in ln]
    assert len(lines) == 1
    assert lines[0].startswith("@reboot ")
    assert "xskill" in lines[0] and "--quiet" in lines[0]
    assert "backup.sh" in fake.content     # 用户已有条目不被动

    svc._remove_cron_boot()
    assert svc._CRON_MARKER not in fake.content
    assert "backup.sh" in fake.content


def test_cron_unavailable_probe(monkeypatch):
    monkeypatch.setattr(svc.shutil, "which", lambda name: None)
    assert svc._crontab_available() is False
    assert svc._install_cron_boot() is False
