from __future__ import annotations

from xskill.cli import build_parser, cmd_update


def test_manual_update_respects_system_pip_index(monkeypatch):
    """
    手动 update 与自动 updater 一样，不得绕过企业 pip 配置。
    @category: integration
    @lane: integration
    @dependency: updater pip adapter
    @complexity: low
    ROI: 64
    """
    captured: list[list[str]] = []

    class _Result:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr(
        "xskill.team.client.updater._current_version", lambda package: "1.0.0",
    )
    monkeypatch.setattr(
        "xskill.team.client.updater._latest_pypi_version",
        lambda package: "1.1.0",
    )
    monkeypatch.setattr(
        "xskill.team.client.updater._restart", lambda: None,
    )
    monkeypatch.setattr(
        "subprocess.run",
        lambda command, **kwargs: captured.append(command) or _Result(),
    )

    args = build_parser().parse_args(["update"])
    assert cmd_update(args) == 0
    assert captured
    assert "-i" not in captured[0]
