from __future__ import annotations

from xskill.cli import build_parser, cmd_update


def _args():
    return build_parser().parse_args(["update"])


def test_update_no_current_version_errors(monkeypatch, tmp_path):
    monkeypatch.setattr("xskill.config.get_team_client_state_path",
                        lambda: tmp_path / "absent.json")
    monkeypatch.setattr(
        "xskill.team.client.updater._current_version", lambda package: None)
    rc = cmd_update(_args())
    assert rc == 1


def test_update_success_delegates_to_autoupdater_run_once(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr("xskill.config.get_team_client_state_path",
                        lambda: tmp_path / "absent.json")
    monkeypatch.setattr(
        "xskill.team.client.updater._current_version", lambda package: "1.0.0")

    calls: list[bool] = []

    class _FakeUpdater:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def run_once(self):
            calls.append(True)
            return True

    monkeypatch.setattr("xskill.team.client.updater.AutoUpdater", _FakeUpdater)
    rc = cmd_update(_args())
    assert rc == 0
    assert calls == [True]
    assert "已是最新版本" in capsys.readouterr().out


def test_update_all_sources_fail_returns_error(monkeypatch, tmp_path):
    monkeypatch.setattr("xskill.config.get_team_client_state_path",
                        lambda: tmp_path / "absent.json")
    monkeypatch.setattr(
        "xskill.team.client.updater._current_version", lambda package: "1.0.0")

    class _FakeUpdater:
        def __init__(self, **kwargs):
            pass

        def run_once(self):
            return False

    monkeypatch.setattr("xskill.team.client.updater.AutoUpdater", _FakeUpdater)
    rc = cmd_update(_args())
    assert rc == 1


def test_update_reuses_persisted_server_and_pypi_url(monkeypatch, tmp_path):
    """已 connect 过的机器：team_client.json 里的 server/token/pypi_url 应
    自动喂给 AutoUpdater，不需要用户在 `xskill update` 时重新指定。"""
    from xskill.team.client.state import ClientState, save_client_state

    state_path = tmp_path / "team_client.json"
    save_client_state(
        ClientState(server_url="http://server", client_id="cid-1",
                    join_token="tok", pypi_url="http://mirror/simple"),
        state_path,
    )
    monkeypatch.setattr("xskill.config.get_team_client_state_path", lambda: state_path)
    monkeypatch.setattr(
        "xskill.team.client.updater._current_version", lambda package: "1.0.0")

    captured: dict = {}

    class _FakeUpdater:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def run_once(self):
            return True

    monkeypatch.setattr("xskill.team.client.updater.AutoUpdater", _FakeUpdater)
    rc = cmd_update(_args())
    assert rc == 0
    assert captured == {
        "server_url": "http://server",
        "client_id": "cid-1",
        "join_token": "tok",
        "pypi_url": "http://mirror/simple",
    }
