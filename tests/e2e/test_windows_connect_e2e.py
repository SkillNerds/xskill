"""Windows 真机 connect 常驻 e2e（schtasks / startup_folder 降级均可过）。

用真实用户 Profile（schtasks 任务进程不继承测试进程的 env 重定向，HOME 假
不了），所以默认跳过，只有显式 ``XSKILL_WIN_E2E=1`` 才跑——CI windows-latest
的一次性 runner 上开启；本地 Windows 开发机自担 ~/.xskill 状态被覆盖。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from xskill import __version__

pytestmark = pytest.mark.skipif(
    sys.platform != "win32" or os.environ.get("XSKILL_WIN_E2E") != "1",
    reason="Windows 真机 e2e：仅 win32 且 XSKILL_WIN_E2E=1（写真实用户 Profile）",
)


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
            self._json({"client_id": "e2e-win"})
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


def _run_cli(repo: Path, env: dict, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "xskill", *args], cwd=repo, env=env,
        capture_output=True, text=True, timeout=60,
    )


def _json_out(result: subprocess.CompletedProcess) -> dict:
    assert result.returncode == 0, result.stderr or result.stdout
    return json.loads(result.stdout)


def test_windows_connect_start_stop_lifecycle():
    """
    AC: Windows 上 connect 默认后台常驻（schtasks 或启动文件夹降级），
        status 可见、stop 全清理。
    Behavior: connect → status(running) → stop → status(not running)。
    @category: service-integration-e2e
    @lane: service-integration-e2e
    @dependency: local HTTP stub, real schtasks/Startup folder
    @complexity: medium
    ROI: 86
    """
    repo = Path(__file__).resolve().parents[2]
    server = ThreadingHTTPServer(("127.0.0.1", 0), _StubHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    address = f"127.0.0.1:{server.server_port}"

    env = os.environ.copy()   # 真实 Profile：任务进程读同一份 ~/.xskill
    env["XSKILL_PYPI_JSON_URL"] = (
        f"http://127.0.0.1:{server.server_port}/pypi/{{package}}/json")

    try:
        connected = _run_cli(repo, env, "connect", address,
                             "--token", "t", "--name", "m00000003")
        assert connected.returncode == 0, connected.stderr or connected.stdout

        # 计划任务是异步拉起的：轮询直到 running（最多 90s，容忍慢 runner）
        deadline = time.time() + 90
        st: dict = {}
        while time.time() < deadline:
            st = _json_out(_run_cli(repo, env, "status", "--json"))
            if st.get("running"):
                break
            time.sleep(3)
        assert st.get("running") is True, f"常驻未进入 running: {st}"
        assert st.get("method") in ("schtasks", "startup_folder")
        assert st.get("crash_recovery") in ("schtasks", "watchdog")

        stopped = _json_out(_run_cli(repo, env, "stop", "--json"))
        assert stopped["running"] is False
        final = _json_out(_run_cli(repo, env, "status", "--json"))
        assert final["running"] is False
    finally:
        _run_cli(repo, env, "stop", "--json")
        server.shutdown()
        server.server_close()
