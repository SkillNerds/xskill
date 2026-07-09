from __future__ import annotations

from pathlib import Path

from xskill.team.client import updater as updater_mod
from xskill.team.client.updater import AutoUpdater, _latest_mirror_version


def test_latest_mirror_version_parses_pip_index_output(monkeypatch):
    class FakeResult:
        stdout = (
            "xskill (1.3.0)\n"
            "Available versions: 1.3.0, 1.2.0a3, 1.2.0, 1.0.0\n"
            "  INSTALLED: 1.0.0\n"
        )
        stderr = ""
        returncode = 0

    captured_cmds: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        captured_cmds.append(cmd)
        return FakeResult()

    monkeypatch.setattr(updater_mod.subprocess, "run", fake_run)

    version = _latest_mirror_version("xskill", "http://mirror/simple")

    assert version == "1.3.0"
    cmd = captured_cmds[0]
    assert cmd[:5] == [updater_mod.sys.executable, "-m", "pip", "index", "versions"]
    assert "-i" in cmd and cmd[cmd.index("-i") + 1] == "http://mirror/simple"
    assert "--pre" in cmd


def test_latest_mirror_version_returns_none_on_failure(monkeypatch):
    def fake_run(cmd, **kwargs):
        raise OSError("no route to host")

    monkeypatch.setattr(updater_mod.subprocess, "run", fake_run)

    assert _latest_mirror_version("xskill", "http://mirror/simple") is None


def test_pypi_newer_version_uses_pypi_and_skips_server(monkeypatch):
    installed: list[str] = []
    restarted: list[bool] = []

    monkeypatch.setattr(updater_mod, "_current_version", lambda package: "1.0.0")
    monkeypatch.setattr(updater_mod, "_latest_pypi_version", lambda package: "1.1.0")
    monkeypatch.setattr(
        updater_mod,
        "_server_version",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("server fallback used")),
    )
    monkeypatch.setattr(updater_mod, "_restart", lambda: restarted.append(True))

    updater = AutoUpdater(server_url="http://server", client_id="cid", join_token="tok")
    monkeypatch.setattr(updater, "_install", lambda version: installed.append(version) or True)

    updater._check_and_update()

    assert installed == ["1.1.0"]
    assert restarted == [True]


def test_current_pypi_version_does_not_fallback_to_server(monkeypatch):
    monkeypatch.setattr(updater_mod, "_current_version", lambda package: "1.0.0")
    monkeypatch.setattr(updater_mod, "_latest_pypi_version", lambda package: "1.0.0")
    monkeypatch.setattr(
        updater_mod,
        "_server_version",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("server fallback used")),
    )

    updater = AutoUpdater(server_url="http://server", client_id="cid", join_token="tok")
    monkeypatch.setattr(
        updater,
        "_install",
        lambda version: (_ for _ in ()).throw(AssertionError("pypi install used")),
    )

    updater._check_and_update()


def test_pypi_query_failure_falls_back_to_server_wheel(monkeypatch):
    installed_wheels: list[str] = []
    restarted: list[bool] = []

    monkeypatch.setattr(updater_mod, "_current_version", lambda package: "1.0.0")
    monkeypatch.setattr(updater_mod, "_latest_pypi_version", lambda package: None)
    monkeypatch.setattr(
        updater_mod,
        "_server_version",
        lambda server_url, join_token, client_id: {
            "version": "1.2.0",
            "wheel_available": True,
            "wheel_filename": "xskill-1.2.0-py3-none-any.whl",
        },
    )

    def fake_download(
        server_url: str,
        join_token: str,
        client_id: str,
        dest_dir: Path,
        filename: str | None,
    ) -> Path:
        wheel = dest_dir / (filename or "xskill-1.2.0-py3-none-any.whl")
        wheel.write_bytes(b"wheel")
        return wheel

    monkeypatch.setattr(updater_mod, "_download_server_wheel", fake_download)
    monkeypatch.setattr(updater_mod, "_restart", lambda: restarted.append(True))

    updater = AutoUpdater(server_url="http://server", client_id="cid", join_token="tok")
    monkeypatch.setattr(
        updater,
        "_install_wheel",
        lambda wheel: installed_wheels.append(wheel.name) or True,
    )

    updater._check_and_update()

    assert installed_wheels == ["xskill-1.2.0-py3-none-any.whl"]
    assert restarted == [True]


def test_pypi_install_failure_can_fallback_to_server_wheel(monkeypatch):
    installed_wheels: list[str] = []

    monkeypatch.setattr(updater_mod, "_current_version", lambda package: "1.0.0")
    monkeypatch.setattr(updater_mod, "_latest_pypi_version", lambda package: "1.2.0")
    monkeypatch.setattr(
        updater_mod,
        "_server_version",
        lambda server_url, join_token, client_id: {
            "version": "1.2.0",
            "wheel_available": True,
            "wheel_filename": "xskill-1.2.0-py3-none-any.whl",
        },
    )

    def fake_download(
        server_url: str,
        join_token: str,
        client_id: str,
        dest_dir: Path,
        filename: str | None,
    ) -> Path:
        wheel = dest_dir / (filename or "xskill-1.2.0-py3-none-any.whl")
        wheel.write_bytes(b"wheel")
        return wheel

    monkeypatch.setattr(updater_mod, "_download_server_wheel", fake_download)
    monkeypatch.setattr(updater_mod, "_restart", lambda: None)

    updater = AutoUpdater(server_url="http://server", client_id="cid", join_token="tok")
    monkeypatch.setattr(updater, "_install", lambda version: False)
    monkeypatch.setattr(
        updater,
        "_install_wheel",
        lambda wheel: installed_wheels.append(wheel.name) or True,
    )

    updater._check_and_update()

    assert installed_wheels == ["xskill-1.2.0-py3-none-any.whl"]


def test_server_fallback_skips_non_newer_server_version(monkeypatch):
    downloaded: list[bool] = []

    monkeypatch.setattr(updater_mod, "_current_version", lambda package: "1.2.0")
    monkeypatch.setattr(updater_mod, "_latest_pypi_version", lambda package: None)
    monkeypatch.setattr(
        updater_mod,
        "_server_version",
        lambda server_url, join_token, client_id: {
            "version": "1.2.0",
            "wheel_available": True,
            "wheel_filename": "xskill-1.2.0-py3-none-any.whl",
        },
    )
    monkeypatch.setattr(
        updater_mod,
        "_download_server_wheel",
        lambda *args, **kwargs: downloaded.append(True),
    )

    updater = AutoUpdater(server_url="http://server", client_id="cid", join_token="tok")
    updater._check_and_update()

    assert downloaded == []


def test_mirror_tried_before_server_when_pypi_unreachable(monkeypatch):
    """公网 PyPI 查询失败但配了内网镜像时，应该先试镜像，不直接跳到 server。"""
    installed: list[str] = []
    restarted: list[bool] = []

    monkeypatch.setattr(updater_mod, "_current_version", lambda package: "1.0.0")
    monkeypatch.setattr(updater_mod, "_latest_pypi_version", lambda package: None)
    monkeypatch.setattr(
        updater_mod, "_latest_mirror_version",
        lambda package, pypi_url: "1.3.0" if pypi_url == "http://mirror/simple" else None,
    )
    monkeypatch.setattr(
        updater_mod, "_server_version",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("server fallback used")),
    )
    monkeypatch.setattr(updater_mod, "_restart", lambda: restarted.append(True))

    updater = AutoUpdater(pypi_url="http://mirror/simple")
    monkeypatch.setattr(updater, "_install", lambda version: installed.append(version) or True)

    ok = updater._check_and_update()

    assert installed == ["1.3.0"]
    assert restarted == [True]
    assert ok is True


def test_mirror_not_queried_when_not_configured(monkeypatch):
    """没配镜像（pypi_url 仍是公网默认值）时，PyPI 查询失败应直接走 server 回退，
    不应该尝试查一个根本没配置的"镜像"。"""
    monkeypatch.setattr(updater_mod, "_current_version", lambda package: "1.0.0")
    monkeypatch.setattr(updater_mod, "_latest_pypi_version", lambda package: None)
    monkeypatch.setattr(
        updater_mod, "_latest_mirror_version",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("mirror query used")),
    )

    updater = AutoUpdater()  # 无 server 配置 -> fallback 也是空操作
    ok = updater._check_and_update()

    assert ok is False


def test_install_wheel_passes_pypi_url_as_index(monkeypatch):
    """server wheel 的依赖必须走一个明确可达的索引（镜像优先），不能吃 pip 默认索引。"""
    captured_cmds: list[list[str]] = []

    class FakeResult:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(cmd, **kwargs):
        captured_cmds.append(cmd)
        return FakeResult()

    monkeypatch.setattr(updater_mod.subprocess, "run", fake_run)

    updater = AutoUpdater(pypi_url="http://mirror/simple")
    ok = updater._install_wheel(Path("/tmp/xskill-1.3.0-py3-none-any.whl"))

    assert ok is True
    assert captured_cmds
    cmd = captured_cmds[0]
    assert "-i" in cmd
    assert cmd[cmd.index("-i") + 1] == "http://mirror/simple"


def test_up_to_date_returns_true(monkeypatch):
    monkeypatch.setattr(updater_mod, "_current_version", lambda package: "1.0.0")
    monkeypatch.setattr(updater_mod, "_latest_pypi_version", lambda package: "1.0.0")

    updater = AutoUpdater()
    assert updater._check_and_update() is True
