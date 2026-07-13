from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import subprocess
import sys
import threading
import site

import pytest

from xskill import __version__


class _LocalTeamAndPypiHandler(BaseHTTPRequestHandler):
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
            self._json({"client_id": "e2e-client"})
            return
        if self.path == "/api/v1/team/upload":
            self._json({"accepted": []})
            return
        self._json({"detail": "not found"}, status=404)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/pypi/xskill/json":
            # 额外给一个非 dev 的 0.0.0：CI 浅克隆下 setuptools-scm 的本地
            # 版本是 dev 版（0.0.1.dev1+unknown...），updater 会过滤 dev
            # 版——若 releases 里只有它，查询结果为空，「已是最新版本」断言
            # 就变成「查询失败」。0.0.0 恒不高于当前版本，语义不变。
            self._json({"releases": {"0.0.0": [{}], __version__: [{}]}})
            return
        self._json({"detail": "not found"}, status=404)

    def log_message(self, format: str, *args) -> None:
        return


def _run_cli(repo: Path, env: dict[str, str], *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "xskill", *args], cwd=repo, env=env,
        capture_output=True, text=True, timeout=20,
    )


def _json_output(result: subprocess.CompletedProcess) -> dict:
    assert result.returncode == 0, result.stderr or result.stdout
    return json.loads(result.stdout)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux service E2E")
def test_linux_connect_update_status_start_stop_lifecycle(tmp_path):
    """
    AC: 普通 Linux 用户关闭终端后 connect 常驻，且 start/stop/status/update 可用。
    Behavior: connect → status → update → stop/start → observable daemon states.
    @category: service-integration-e2e
    @lane: service-integration-e2e
    @dependency: local HTTP stub, subprocess CLI, Linux process table
    @complexity: medium
    ROI: 88
    """
    repo = Path(__file__).resolve().parents[2]
    server = ThreadingHTTPServer(("127.0.0.1", 0), _LocalTeamAndPypiHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    address = f"127.0.0.1:{server.server_port}"
    env = os.environ.copy()
    python_paths = [str(repo / "src"), site.getusersitepackages()]
    if env.get("PYTHONPATH"):
        python_paths.append(env["PYTHONPATH"])
    env.update({
        "HOME": str(tmp_path),
        "PYTHONPATH": os.pathsep.join(python_paths),
        "XDG_CONFIG_HOME": str(tmp_path / ".config"),
        "XSKILL_CONNECT_BACKEND": "detached",
        "XSKILL_PYPI_JSON_URL": (
            f"http://127.0.0.1:{server.server_port}/pypi/{{package}}/json"
        ),
    })

    try:
        connected = _run_cli(
            repo, env, "connect", address, "--token", "test-token",
            "--name", "m00000001",
        )
        assert connected.returncode == 0, connected.stderr or connected.stdout

        running = _json_output(_run_cli(repo, env, "status", "--json"))
        assert running["running"] is True
        assert running["platform"] == "linux"
        assert running["method"] == "detached"

        update = _run_cli(repo, env, "update")
        assert update.returncode == 0, update.stderr or update.stdout
        assert "已是最新版本" in update.stdout
        assert _json_output(_run_cli(repo, env, "status", "--json"))["running"] is True

        stopped = _json_output(_run_cli(repo, env, "stop", "--json"))
        assert stopped["running"] is False
        assert _json_output(_run_cli(repo, env, "status", "--json"))["running"] is False

        started = _json_output(_run_cli(repo, env, "start", "--json"))
        assert started["running"] is True
        assert _json_output(_run_cli(repo, env, "status", "--json"))["running"] is True
    finally:
        _run_cli(repo, env, "stop", "--json")
        server.shutdown()
        server.server_close()
