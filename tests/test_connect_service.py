"""test_connect_service.py — connect 常驻后端（Problem 2）

平台无关：用 monkeypatch 把 sys.platform 与 subprocess 打桩，在 Linux CI 上也能
验证 Windows 计划任务后端的 argv/XML；非 Windows 平台的占位后端验证会报“未实现”。
"""
from __future__ import annotations

import pytest

import xskill.team.client.service as svc


# ─────────────────── 平台选择 & 占位后端 ───────────────────

def test_get_backend_windows(monkeypatch):
    monkeypatch.setattr(svc.sys, "platform", "win32")
    b = svc.get_backend()
    assert isinstance(b, svc.WindowsTaskSchedulerBackend)
    assert b.name == "windows"
    assert b.supported is True


@pytest.mark.parametrize("platform,label", [("linux", "Linux"),
                                            ("darwin", "macOS")])
def test_get_backend_unsupported_raises(monkeypatch, platform, label):
    monkeypatch.setattr(svc.sys, "platform", platform)
    b = svc.get_backend()
    assert b.supported is False  # connect 据此退化成前台阻塞
    for op in (b.install_and_start, b.stop, b.status):
        with pytest.raises(svc.ServiceError) as ei:
            op()
        assert label in str(ei.value)


# ─────────────────── pid / 运行态读写 ───────────────────

def test_daemon_state_roundtrip(tmp_path, monkeypatch):
    p = tmp_path / "connect_daemon.json"
    monkeypatch.setattr(svc, "get_connect_daemon_state_path", lambda: p)
    # 缺文件 → 未运行
    assert svc.read_daemon_state() == {"running": False}
    # 写入后能读回；running 由 pid 存活决定
    monkeypatch.setattr(svc, "_pid_alive", lambda pid: pid == 4242)
    svc.write_daemon_state(pid=4242, server_url="http://h:8000",
                           client_id="cid-1", task_name="Xskill_Connect")
    st = svc.read_daemon_state()
    assert st["running"] is True
    assert st["pid"] == 4242
    assert st["server_url"] == "http://h:8000"
    # 死 pid → running False
    monkeypatch.setattr(svc, "_pid_alive", lambda pid: False)
    assert svc.read_daemon_state()["running"] is False
    # clear
    svc.clear_daemon_state()
    assert svc.read_daemon_state() == {"running": False}


def test_daemon_state_corrupt_file(tmp_path, monkeypatch):
    p = tmp_path / "connect_daemon.json"
    p.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(svc, "get_connect_daemon_state_path", lambda: p)
    assert svc.read_daemon_state() == {"running": False}


def test_foreground_argv_uses_dash_m(monkeypatch):
    monkeypatch.setattr(svc.sys, "platform", "linux")
    monkeypatch.setattr(svc.sys, "executable", "/usr/bin/python3")
    argv = svc._foreground_argv()
    assert argv == ["/usr/bin/python3", "-m", "xskill", "connect", "--foreground"]


def test_pid_alive_windows_uses_exact_tasklist_pid(monkeypatch):
    class _Result:
        stdout = '"python.exe","4242","Console","1","10,000 K"\n'

    monkeypatch.setattr(svc.subprocess, "run", lambda *args, **kwargs: _Result())

    assert svc._pid_alive_windows(4242) is True
    assert svc._pid_alive_windows(42) is False


# ─────────────────── task XML ───────────────────

def test_task_xml_has_persistence_fields():
    xml = svc._build_task_xml("C:\\py\\pythonw.exe",
                              "-m xskill connect --foreground", "C:\\Users\\me")
    # 不限时长 + 崩溃自愈 + 登录触发 + 单实例
    assert "<ExecutionTimeLimit>PT0S</ExecutionTimeLimit>" in xml
    assert "<RestartOnFailure>" in xml
    assert "<LogonTrigger>" in xml
    assert "IgnoreNew" in xml
    assert "pythonw.exe" in xml


def test_task_xml_escapes_arguments():
    xml = svc._build_task_xml("py", 'a & b <x> "q"', "wd")
    assert "&amp;" in xml and "&lt;" in xml and "&quot;" in xml


# ─────────────────── Windows 后端行为（打桩 subprocess）───────────────────

class _FakeSchtasks:
    """记录 schtasks 调用；/Query 时吐出带 PID 的假输出。"""

    def __init__(self, *, create_rc=0, run_rc=0, query_rc=0, pid=4242):
        self.calls: list[list[str]] = []
        self.create_rc = create_rc
        self.run_rc = run_rc
        self.query_rc = query_rc
        self.pid = pid

    def __call__(self, args, **kw):
        self.calls.append(list(args))
        import types
        cp = types.SimpleNamespace(returncode=0, stdout="", stderr="")
        if args[0] == "schtasks":
            if "/Create" in args:
                cp.returncode = self.create_rc
            elif "/Run" in args:
                cp.returncode = self.run_rc
            elif "/Query" in args:
                cp.returncode = self.query_rc
                cp.stdout = (f"TaskName: \\Xskill_Connect\n"
                             f"PID:                 {self.pid}\n"
                             f"Status:              Running\n")
            elif "/End" in args or "/Delete" in args:
                cp.returncode = 0
        return cp


@pytest.fixture
def win_backend(monkeypatch, tmp_path):
    monkeypatch.setattr(svc.sys, "platform", "win32")
    monkeypatch.setattr(svc, "get_connect_daemon_state_path",
                        lambda: tmp_path / "connect_daemon.json")
    monkeypatch.setattr(svc, "_pid_alive", lambda pid: pid == 4242)
    return svc.WindowsTaskSchedulerBackend()


def test_windows_install_and_start_argv(win_backend, monkeypatch):
    fake = _FakeSchtasks()
    monkeypatch.setattr(svc.subprocess, "run", fake)
    st = win_backend.install_and_start()
    # 应先 /Create /XML 再 /Run
    kinds = [c for c in fake.calls if c[0] == "schtasks"]
    assert any("/Create" in c and "/XML" in c for c in kinds)
    assert any("/Run" in c for c in kinds)
    assert st["running"] is True
    assert st["pid"] == 4242
    assert st["task_name"] == "Xskill_Connect"


def test_windows_install_create_failure_raises(win_backend, monkeypatch):
    fake = _FakeSchtasks(create_rc=1)
    monkeypatch.setattr(svc.subprocess, "run", fake)
    with pytest.raises(svc.ServiceError):
        win_backend.install_and_start()
    # /Create 失败就不该再 /Run
    assert not any("/Run" in c for c in fake.calls)


def test_windows_run_failure_raises(win_backend, monkeypatch):
    fake = _FakeSchtasks(run_rc=1)
    monkeypatch.setattr(svc.subprocess, "run", fake)
    with pytest.raises(svc.ServiceError):
        win_backend.install_and_start()


def test_windows_status_running(win_backend, monkeypatch):
    fake = _FakeSchtasks()
    monkeypatch.setattr(svc.subprocess, "run", fake)
    st = win_backend.status()
    assert st["installed"] is True
    assert st["running"] is True
    assert st["pid"] == 4242


def test_windows_status_not_installed(win_backend, monkeypatch):
    fake = _FakeSchtasks(query_rc=1)  # 任务不存在 → Query 非 0
    monkeypatch.setattr(svc.subprocess, "run", fake)
    st = win_backend.status()
    assert st["installed"] is False
    assert st["running"] is False


def test_windows_stop_idempotent(win_backend, monkeypatch):
    fake = _FakeSchtasks()
    monkeypatch.setattr(svc.subprocess, "run", fake)
    svc.write_daemon_state(pid=4242, task_name="Xskill_Connect", method="schtasks")
    st = win_backend.stop()
    assert st["running"] is False
    assert any("/End" in c for c in fake.calls)
    assert any("/Delete" in c for c in fake.calls)
    assert svc.read_daemon_state() == {"running": False}


def test_windows_query_pid_parsing(win_backend, monkeypatch):
    fake = _FakeSchtasks(pid=13579)
    monkeypatch.setattr(svc.subprocess, "run", fake)
    monkeypatch.setattr(svc, "_pid_alive", lambda pid: True)
    assert win_backend._query_pid() == 13579


# ─────────────── 开机启动文件夹降级（Group Policy 拦截 schtasks）───────────────

class _AccessDeniedSchtasks(_FakeSchtasks):
    """schtasks /Create 返回 access denied 错误码 + 错误文本。"""

    def __call__(self, args, **kw):
        cp = super().__call__(args, **kw)
        if args[0] == "schtasks" and "/Create" in args:
            cp.returncode = 1
            cp.stderr = "ERROR: Access is denied."
        return cp


def test_access_denied_falls_back_to_startup_folder(
    win_backend, monkeypatch, tmp_path
):
    """schtasks /Create 返回 access denied → 自动降级到开机启动文件夹。"""
    fake_startup_vbs = tmp_path / "xskill_connect.vbs"
    monkeypatch.setattr(svc, "_startup_vbs_path", lambda: fake_startup_vbs)
    monkeypatch.setattr(svc.subprocess, "run", _AccessDeniedSchtasks())

    spawned = []

    class _FakePopen:
        pid = 9999

        def __init__(self, *a, **kw):
            spawned.append(a[0])

    monkeypatch.setattr(svc.subprocess, "Popen", _FakePopen)
    monkeypatch.setattr(svc, "_pid_alive", lambda pid: pid == 9999)

    st = win_backend.install_and_start()

    # .vbs 应被写入
    assert fake_startup_vbs.is_file()
    assert "xskill" in fake_startup_vbs.read_text(encoding="utf-8").lower()
    # 进程应被 detach 启动
    assert len(spawned) == 1
    assert "--foreground" in spawned[0]
    # status 反映 startup_folder 方法
    assert st["method"] == "startup_folder"
    assert st["running"] is True


def test_access_denied_stop_cleans_vbs_and_kills_pid(
    win_backend, monkeypatch, tmp_path
):
    """stop() 在 startup_folder 方法下：删 .vbs + 杀进程。"""
    fake_vbs = tmp_path / "xskill_connect.vbs"
    fake_vbs.write_text("stub", encoding="utf-8")
    svc.write_daemon_state(method="startup_folder", pid=8888,
                           vbs_path=str(fake_vbs))

    killed = []

    def _fake_run(args, **kw):
        import types
        if args[0] == "taskkill":
            killed.append(args)
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(svc.subprocess, "run", _fake_run)
    monkeypatch.setattr(svc, "_pid_alive", lambda pid: True)

    st = win_backend.stop()
    assert not fake_vbs.exists()        # .vbs 已删
    assert any("8888" in str(c) for c in killed)   # pid 被 taskkill
    assert st["running"] is False


def test_is_access_denied_detects_english_and_chinese():
    import types
    en = types.SimpleNamespace(stderr="ERROR: Access is denied.", stdout="")
    zh = types.SimpleNamespace(stderr="", stdout="拒绝访问。")
    ok = types.SimpleNamespace(stderr="other error", stdout="")
    assert svc._is_access_denied(en) is True
    assert svc._is_access_denied(zh) is True
    assert svc._is_access_denied(ok) is False
