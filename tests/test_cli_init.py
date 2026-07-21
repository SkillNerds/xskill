"""`xskill init` 一站式引导命令的单元测试。

装 skill 与 connect 都靠 monkeypatch 打桩——不真跑生态安装、不真握手 server，
只验证 cmd_init 的分支（skills-only / no-skill / 已有常驻 / force / 缺必填项）。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import xskill.cli as cli  # noqa: E402


def _args(**overrides) -> argparse.Namespace:
    base = dict(
        address=None, token=None, name=None, label="", use_proxy=False,
        foreground=False, no_auto_update=False, skills_only=False,
        no_skill=False, force=False, yes=False, target_root=None,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


@pytest.fixture
def bundled_skill(tmp_path, monkeypatch):
    """让 ``files('xskill')`` 指向两个带 SKILL.md 的临时 Skill。"""
    root = tmp_path / "pkgroot"
    skill_root = root / "data" / "skill"
    for name in ("xskill", "xskill-kernel"):
        skill = skill_root / name
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(f"# {name}\n", encoding="utf-8")
    monkeypatch.setattr("importlib.resources.files", lambda pkg: root)
    return skill_root


@pytest.fixture
def install_recorder(monkeypatch):
    """把所有 install_to_* 换成记录 Skill 名和生态的桩。"""
    installed = []
    for eco in ("claude_code", "codex", "nga3", "opencode", "ngagent",
                "openclaw", "cursor", "trae"):
        def _make(eco_name):
            def _fake(skill_path, target_root=None, side="main"):
                installed.append((Path(skill_path).name, eco_name))
                return Path(skill_path) / "SKILL.md"
            return _fake
        monkeypatch.setattr(f"xskill.ecosystems.install_to_{eco}", _make(eco))
    return installed


def test_skills_only_installs_without_connecting(
        bundled_skill, install_recorder, monkeypatch):
    monkeypatch.setattr(
        "xskill.ecosystems.detect_known_ecosystems",
        lambda home_root=None: [{"ecosystem": "claude_code",
                                 "source": "/x", "bridge": "/y"}],
    )
    connect_called = []
    monkeypatch.setattr(cli, "cmd_connect",
                        lambda a: connect_called.append(a) or 0)

    code = cli.cmd_init(_args(skills_only=True))

    assert code == 0
    assert install_recorder == [
        ("xskill", "claude_code"),
        ("xskill-kernel", "claude_code"),
    ]
    assert connect_called == []


def test_no_skill_connects_without_installing(
        bundled_skill, install_recorder, monkeypatch):
    monkeypatch.setattr("xskill.team.client.service.read_daemon_state",
                        lambda: {"running": False})
    captured = {}
    monkeypatch.setattr(cli, "cmd_connect",
                        lambda a: captured.update(vars(a)) or 0)

    code = cli.cmd_init(_args(no_skill=True, yes=True,
                              address="1.2.3.4:8000", token="TOK", name="007"))

    assert code == 0
    assert install_recorder == []          # --no-skill：一个生态都没装
    assert captured["address"] == "1.2.3.4:8000"
    assert captured["token"] == "TOK"
    assert captured["name"] == "007"


def test_existing_daemon_kept_when_no_force_noninteractive(monkeypatch):
    monkeypatch.setattr("xskill.team.client.service.read_daemon_state",
                        lambda: {"running": True, "pid": 4321, "backend": "schtasks"})
    monkeypatch.setattr(
        "xskill.config.get_team_client_state_path",
        lambda: Path("/nonexistent/team_client.json"),
    )
    connect_called = []
    monkeypatch.setattr(cli, "cmd_connect",
                        lambda a: connect_called.append(a) or 0)

    code = cli.cmd_init(_args(no_skill=True, yes=True, force=False,
                              address="h:1", token="T"))

    assert code == 0
    assert connect_called == []            # 保留现有，不重连


def test_existing_daemon_force_stops_then_connects(monkeypatch):
    monkeypatch.setattr("xskill.team.client.service.read_daemon_state",
                        lambda: {"running": True, "pid": 4321, "backend": "systemd"})
    monkeypatch.setattr(
        "xskill.config.get_team_client_state_path",
        lambda: Path("/nonexistent/team_client.json"),
    )
    stopped = []
    cleared = []

    class _Backend:
        def stop(self):
            stopped.append(True)
            return {}
    monkeypatch.setattr("xskill.team.client.service.get_backend",
                        lambda: _Backend())
    monkeypatch.setattr("xskill.team.client.service.clear_daemon_state",
                        lambda: cleared.append(True))
    connect_called = []
    monkeypatch.setattr(cli, "cmd_connect",
                        lambda a: connect_called.append(a) or 0)

    code = cli.cmd_init(_args(no_skill=True, yes=True, force=True,
                              address="h:1", token="T"))

    assert code == 0
    assert stopped == [True]
    assert cleared == [True]
    assert len(connect_called) == 1        # 停掉旧的后照常重连


def test_noninteractive_missing_token_errors(monkeypatch):
    monkeypatch.setattr("xskill.team.client.service.read_daemon_state",
                        lambda: {"running": False})
    connect_called = []
    monkeypatch.setattr(cli, "cmd_connect",
                        lambda a: connect_called.append(a) or 0)

    code = cli.cmd_init(_args(no_skill=True, yes=True, address="h:1", token=None))

    assert code == 2                       # 缺 token，非交互直接报错
    assert connect_called == []
