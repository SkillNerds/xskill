"""zstandard 不进主依赖；探测到 ~/.dsh 时现场补装一次（#334）。"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from xskill.ecosystems import deepseek_harness as dsh_mod
from xskill.ecosystems.deepseek_harness import (
    _ZSTD_REQUIREMENT,
    ensure_zstandard_for_dsh,
)


def test_pyproject_keeps_zstandard_optional_only():
    text = Path("pyproject.toml").read_text(encoding="utf-8")
    deps_start = text.index("dependencies = [")
    extras_start = text.index("[project.optional-dependencies]")
    deps_block = text[deps_start:extras_start]
    assert "zstandard" not in deps_block
    extras = text[extras_start:]
    assert "dsh = [" in extras or "dsh=[" in extras
    assert "zstandard>=0.21" in extras


def test_provision_skips_without_dsh_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(dsh_mod, "zstandard_available", lambda: False)
    called = []
    monkeypatch.setattr(
        dsh_mod, "_install_zstandard_with_pip",
        lambda: called.append(True) or (True, ""),
    )
    assert ensure_zstandard_for_dsh(tmp_path) is False
    assert called == []
    assert not (tmp_path / ".xskill" / "dsh-zstandard-provision.json").exists()


def test_provision_skips_when_already_importable(monkeypatch, tmp_path):
    (tmp_path / ".dsh").mkdir()
    monkeypatch.setattr(dsh_mod, "zstandard_available", lambda: True)
    called = []
    monkeypatch.setattr(
        dsh_mod, "_install_zstandard_with_pip",
        lambda: called.append(True) or (True, ""),
    )
    assert ensure_zstandard_for_dsh(tmp_path) is True
    assert called == []


def test_provision_pip_omits_proxy_and_index(monkeypatch, tmp_path):
    (tmp_path / ".dsh").mkdir()
    calls = {"n": 0}

    def _available() -> bool:
        calls["n"] += 1
        return calls["n"] > 1

    captured: dict = {}

    def _fake_run(cmd, **kwargs):
        captured["cmd"] = list(cmd)
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(dsh_mod, "zstandard_available", _available)
    monkeypatch.setattr(dsh_mod.subprocess, "run", _fake_run)

    assert ensure_zstandard_for_dsh(tmp_path) is True
    assert "--proxy" not in captured["cmd"]
    assert "-i" not in captured["cmd"]
    assert _ZSTD_REQUIREMENT in captured["cmd"]
    state = json.loads(
        (tmp_path / ".xskill" / "dsh-zstandard-provision.json").read_text(
            encoding="utf-8",
        )
    )
    assert state["status"] == "ok"


def test_provision_does_not_retry_after_failure(monkeypatch, tmp_path):
    (tmp_path / ".dsh").mkdir()
    sentinel = tmp_path / ".xskill" / "dsh-zstandard-provision.json"
    sentinel.parent.mkdir(parents=True)
    sentinel.write_text(
        json.dumps({"package": _ZSTD_REQUIREMENT, "status": "failed", "detail": "once"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(dsh_mod, "zstandard_available", lambda: False)
    called = []
    monkeypatch.setattr(
        dsh_mod, "_install_zstandard_with_pip",
        lambda: called.append(True) or (True, ""),
    )
    assert ensure_zstandard_for_dsh(tmp_path) is False
    assert called == []


def test_provision_writes_failed_sentinel(monkeypatch, tmp_path):
    (tmp_path / ".dsh").mkdir()
    monkeypatch.setattr(dsh_mod, "zstandard_available", lambda: False)
    monkeypatch.setattr(
        dsh_mod, "_install_zstandard_with_pip",
        lambda: (False, "ProxyError handshake timed out"),
    )
    assert ensure_zstandard_for_dsh(tmp_path) is False
    state = json.loads(
        (tmp_path / ".xskill" / "dsh-zstandard-provision.json").read_text(
            encoding="utf-8",
        )
    )
    assert state["status"] == "failed"
    assert "ProxyError" in state["detail"]
