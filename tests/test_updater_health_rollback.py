"""updater 健康检查 + 回滚 + 坏版本拉黑（cross-platform-persistence）。

坏 wheel 装上后若直接重启，常驻进程会进入「重启即崩」死循环且 updater 永远
不会自愈——这里验证三道防线：装后健康检查、失败回滚、坏版本不再重试。
"""
from __future__ import annotations

import json

import pytest

import xskill.team.client.updater as upd


@pytest.fixture
def journal_path(tmp_path, monkeypatch):
    p = tmp_path / "update_journal.json"
    monkeypatch.setattr(upd, "_journal_path", lambda: p)
    return p


def test_install_and_verify_success_records_last_good(journal_path, monkeypatch):
    monkeypatch.setattr(upd.AutoUpdater, "_install", lambda self, v: True)
    monkeypatch.setattr(upd, "_health_check", lambda: True)

    assert upd.AutoUpdater().install_and_verify("2.0.0", "1.0.0") is True
    assert upd.load_update_journal()["last_good"] == "2.0.0"
    assert not upd._is_blacklisted("2.0.0")


def test_install_and_verify_bad_version_rolls_back_and_blacklists(
    journal_path, monkeypatch,
):
    installed: list[str] = []
    monkeypatch.setattr(upd.AutoUpdater, "_install",
                        lambda self, v: installed.append(v) or True)
    # 新版本健康检查失败；回滚后的检查成功
    health = iter([False, True])
    monkeypatch.setattr(upd, "_health_check", lambda: next(health))

    assert upd.AutoUpdater().install_and_verify("2.0.0", "1.0.0") is False
    assert installed == ["2.0.0", "1.0.0"]        # 先装新版，失败后装回旧版
    assert upd._is_blacklisted("2.0.0")
    assert upd.load_update_journal().get("last_good") != "2.0.0"


def test_install_failure_does_not_blacklist(journal_path, monkeypatch):
    """pip 安装失败（网络/镜像抖动）≠ 坏版本，下次还应重试。"""
    monkeypatch.setattr(upd.AutoUpdater, "_install", lambda self, v: False)
    assert upd.AutoUpdater().install_and_verify("2.0.0", "1.0.0") is False
    assert not upd._is_blacklisted("2.0.0")


def test_check_and_update_skips_blacklisted_pypi_version(journal_path, monkeypatch):
    upd._blacklist_version("2.0.0", "health_check_failed")

    monkeypatch.setattr(upd, "_current_version", lambda pkg: "1.0.0")
    monkeypatch.setattr(upd, "_latest_pypi_version", lambda pkg: "2.0.0")
    installs = []
    monkeypatch.setattr(upd.AutoUpdater, "install_and_verify",
                        lambda self, t, c: installs.append(t) or True)
    restarted = []
    monkeypatch.setattr(upd, "_restart", lambda: restarted.append(1))
    server_checked = []
    monkeypatch.setattr(
        upd.AutoUpdater, "_check_server_fallback",
        lambda self, cs, c, *, reason: server_checked.append(reason))

    upd.AutoUpdater()._check_and_update()
    assert installs == []                         # 拉黑版本不再安装
    assert restarted == []
    assert server_checked == ["pypi_blacklisted"]  # 但 server 渠道仍会查


def test_server_fallback_skips_blacklisted_version(journal_path, monkeypatch):
    upd._blacklist_version("3.0.0", "health_check_failed")
    monkeypatch.setattr(
        upd, "_server_version",
        lambda *a: {"version": "3.0.0", "wheel_available": True,
                    "wheel_filename": "x.whl"})
    downloads = []
    monkeypatch.setattr(upd, "_download_server_wheel",
                        lambda *a, **kw: downloads.append(1) or None)

    from packaging.version import Version
    u = upd.AutoUpdater(server_url="http://s", client_id="c", join_token="t")
    u._check_server_fallback("1.0.0", Version("1.0.0"), reason="pypi_query_failed")
    assert downloads == []                        # 连 wheel 都不必下


def test_journal_corruption_tolerated(journal_path):
    journal_path.write_text("{broken json", encoding="utf-8")
    assert upd.load_update_journal() == {}
    upd._blacklist_version("2.0.0", "x")          # 损坏文件被健康内容覆盖
    assert json.loads(journal_path.read_text(encoding="utf-8"))["bad_versions"]


def test_restart_under_supervisor_exits_nonzero(monkeypatch):
    """XSKILL_SUPERVISED=1 时统一走「非零退出，交 watchdog 重启」路径。"""
    monkeypatch.setenv("XSKILL_SUPERVISED", "1")
    exit_codes = []

    def fake_exit(code):
        # 真 os._exit 不返回；替身必须抛异常阻断后续 execv 分支。
        exit_codes.append(code)
        raise SystemExit(code)

    monkeypatch.setattr(upd.os, "_exit", fake_exit)
    monkeypatch.setattr(upd, "_windows_persistence_method",
                        lambda: pytest.fail("supervised 分支不应查 Windows 方法"))
    import time as _time
    monkeypatch.setattr(_time, "sleep", lambda s: None)

    with pytest.raises(SystemExit):
        upd._restart()
    assert exit_codes == [1]
