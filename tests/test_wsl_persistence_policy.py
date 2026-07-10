from __future__ import annotations

import types

import pytest

import xskill.team.client.service as svc


def test_wsl_without_systemd_refuses_detached_success(monkeypatch):
    monkeypatch.setattr(svc, "_is_wsl", lambda: True)
    monkeypatch.setattr(svc, "_systemd_user_available", lambda: False)
    monkeypatch.setattr(svc, "read_daemon_state", lambda: {"running": False})

    backend = svc.LinuxServiceBackend()
    with pytest.raises(svc.ServiceError, match="systemd"):
        backend.install_and_start()
    status = backend.status()
    assert status["running"] is False
    assert status["platform"] == "wsl"
    assert status["method"] == "systemd-required"


def test_plain_linux_without_systemd_keeps_detached_fallback(monkeypatch):
    monkeypatch.setattr(svc, "_is_wsl", lambda: False)
    monkeypatch.setattr(svc, "_systemd_user_available", lambda: False)
    monkeypatch.setattr(
        svc.DetachedProcessBackend,
        "install_and_start",
        lambda self: {"running": True, "method": self.method},
    )

    assert svc.LinuxServiceBackend().install_and_start() == {
        "running": True,
        "method": "detached",
    }


def test_wsl_systemd_install_requires_linger(monkeypatch, tmp_path):
    monkeypatch.setattr(svc, "_is_wsl", lambda: True)
    monkeypatch.setattr(svc.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setenv("USER", "alice")

    def fake_run(args, **kwargs):
        if args[:2] == ["loginctl", "enable-linger"]:
            return types.SimpleNamespace(returncode=1, stdout="", stderr="denied")
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(svc.subprocess, "run", fake_run)
    unit = tmp_path / "xskill-connect.service"

    with pytest.raises(svc.ServiceError, match="linger"):
        svc.SystemdUserBackend(unit_path=unit).install_and_start()
    assert not unit.exists()
