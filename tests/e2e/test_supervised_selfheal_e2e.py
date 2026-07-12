"""supervised 链路真实进程 e2e：常驻 → 杀子进程 → watchdog 自愈 → stop 全清理。

无 systemd 的环境（精简容器 / 未启 systemd 的 WSL / 鸿蒙）里这条链就是常驻的
唯一保障，必须用真进程验证：
- connect 后 watchdog 与 connect 子进程都在；
- SIGKILL 子进程后 watchdog 在退避窗口内拉起新子进程（pid 变化）；
- cron @reboot 自启条目装上（经 PATH shim 的假 crontab，不动真 crontab）；
- stop 后 watchdog、子进程全部退出，cron 条目移除，二次 status 不误报。

docker 平台矩阵（tests/docker_e2e/platform_matrix/）在 ubuntu/debian/openEuler
（含鸿蒙模拟）容器里跑的正是本文件。
"""
from __future__ import annotations

import json
import os
import signal
import stat
import subprocess
import sys
import site
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from xskill import __version__


class _StubHandler(BaseHTTPRequestHandler):
    def _json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        if length:
            self.rfile.read(length)
        if self.path == "/api/v1/team/register":
            self._json({"client_id": "e2e-selfheal"})
            return
        if self.path == "/api/v1/team/upload":
            self._json({"accepted": []})
            return
        self._json({"detail": "not found"}, status=404)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/pypi/xskill/json":
            self._json({"releases": {__version__: [{}]}})
            return
        self._json({"detail": "not found"}, status=404)

    def log_message(self, format: str, *args) -> None:
        return


def _run_cli(repo: Path, env: dict[str, str], *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "xskill", *args], cwd=repo, env=env,
        capture_output=True, text=True, timeout=30,
    )


def _json_out(result: subprocess.CompletedProcess) -> dict:
    assert result.returncode == 0, result.stderr or result.stdout
    return json.loads(result.stdout)


def _pid_alive(pid: int) -> bool:
    """与 service._pid_alive 同口径：容器里孤儿僵尸（Z 态）视为死。"""
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, TypeError):
        return False
    except PermissionError:
        return True
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
        if stat.rsplit(")", 1)[1].split()[0] == "Z":
            return False
    except (OSError, IndexError):
        pass
    return True


def _make_fake_crontab(shim_dir: Path, store: Path) -> None:
    """PATH shim：假 crontab 读写文件，e2e 绝不碰真用户 crontab。"""
    script = shim_dir / "crontab"
    script.write_text(
        "#!/bin/sh\n"
        f'STORE="{store}"\n'
        'if [ "$1" = "-l" ]; then\n'
        '  [ -f "$STORE" ] || exit 1\n'
        '  cat "$STORE"; exit 0\n'
        'fi\n'
        'if [ "$1" = "-" ]; then cat > "$STORE"; exit 0; fi\n'
        'exit 0\n',
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)


def _wait_until(predicate, timeout: float, interval: float = 0.3):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(interval)
    return last


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux service E2E")
def test_supervised_selfheal_lifecycle(tmp_path):
    """
    AC: 无 systemd 平台 connect 常驻具备崩溃自愈与开机自启挂载，stop 全清理。
    Behavior: connect → status → kill child → auto respawn → stop → clean.
    @category: service-integration-e2e
    @lane: service-integration-e2e
    @dependency: local HTTP stub, subprocess CLI, fake crontab shim
    @complexity: medium
    ROI: 90
    """
    repo = Path(__file__).resolve().parents[2]
    server = ThreadingHTTPServer(("127.0.0.1", 0), _StubHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    address = f"127.0.0.1:{server.server_port}"

    shim_dir = tmp_path / "bin"
    shim_dir.mkdir()
    cron_store = tmp_path / "crontab.store"
    _make_fake_crontab(shim_dir, cron_store)

    env = os.environ.copy()
    python_paths = [str(repo / "src"), site.getusersitepackages()]
    if env.get("PYTHONPATH"):
        python_paths.append(env["PYTHONPATH"])
    env.update({
        "HOME": str(tmp_path),
        "PATH": f"{shim_dir}{os.pathsep}{env.get('PATH', '')}",
        "PYTHONPATH": os.pathsep.join(python_paths),
        "XDG_CONFIG_HOME": str(tmp_path / ".config"),
        "XSKILL_CONNECT_BACKEND": "supervised",
        "XSKILL_PYPI_JSON_URL": (
            f"http://127.0.0.1:{server.server_port}/pypi/{{package}}/json"
        ),
    })
    state_file = tmp_path / ".xskill" / "connect_daemon.json"

    def read_state() -> dict:
        try:
            return json.loads(state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    try:
        connected = _run_cli(repo, env, "connect", address,
                             "--token", "t", "--name", "m00000002")
        assert connected.returncode == 0, connected.stderr or connected.stdout

        st = _json_out(_run_cli(repo, env, "status", "--json"))
        assert st["running"] is True
        assert st["method"] == "supervised"
        assert st["crash_recovery"] == "watchdog"
        watchdog_pid = st["watchdog_pid"]
        assert _pid_alive(watchdog_pid)

        # 首个 connect 子进程就位
        child1 = _wait_until(
            lambda: (read_state().get("child_pid")
                     if _pid_alive(read_state().get("child_pid") or -1) else None),
            timeout=15)
        assert child1, f"首个子进程未出现: state={read_state()}"

        # cron @reboot 自启已挂（假 crontab）
        assert "@reboot" in cron_store.read_text(encoding="utf-8")
        assert "xskill-connect-boot" in cron_store.read_text(encoding="utf-8")

        # ── 自愈：SIGKILL 子进程，watchdog 应拉起新的 ──
        os.kill(child1, signal.SIGKILL)
        child2 = _wait_until(
            lambda: (read_state().get("child_pid")
                     if (read_state().get("child_pid") not in (None, child1)
                         and _pid_alive(read_state().get("child_pid")))
                     else None),
            timeout=30)
        assert child2, f"watchdog 未拉起新子进程: state={read_state()}"
        assert child2 != child1
        assert _pid_alive(watchdog_pid)

        st = _json_out(_run_cli(repo, env, "status", "--json"))
        assert st["running"] is True

        # ── stop：watchdog + 子进程全退，cron 条目移除 ──
        stopped = _json_out(_run_cli(repo, env, "stop", "--json"))
        assert stopped["running"] is False
        assert _wait_until(lambda: not _pid_alive(watchdog_pid), timeout=10)
        assert _wait_until(lambda: not _pid_alive(child2), timeout=10)
        assert "xskill-connect-boot" not in cron_store.read_text(encoding="utf-8")
        assert _json_out(_run_cli(repo, env, "status", "--json"))["running"] is False

        # ── start --quiet 幂等：再拉起后重复调用不重复起 watchdog ──
        started = _json_out(_run_cli(repo, env, "start", "--json"))
        assert started["running"] is True
        wpid = started["watchdog_pid"]
        quiet = _run_cli(repo, env, "start", "--quiet")
        assert quiet.returncode == 0 and quiet.stdout.strip() == ""
        assert _json_out(_run_cli(repo, env, "status", "--json"))["watchdog_pid"] == wpid
    finally:
        _run_cli(repo, env, "stop", "--json")
        server.shutdown()
        server.server_close()
