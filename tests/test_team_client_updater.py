from __future__ import annotations

from pathlib import Path

from xskill.team.client import updater as updater_mod
from xskill.team.client.updater import AutoUpdater


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
