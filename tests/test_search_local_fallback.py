"""`xskill search` 部署模式自适应 + 本地 BM25 回退测试（#46 前向修复 / #201）。"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from xskill import cli


def _args(**overrides) -> SimpleNamespace:
    base = {
        "terms": ["文案", "对齐"], "top_k": 5, "json": False,
        "download": False, "team": False, "local": False,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


class _Response:
    def __init__(self, status_code: int, json_data):
        self.status_code = status_code
        self._json_data = json_data

    def json(self):
        return self._json_data


def _write_skill(skill_dir: Path, name: str, description: str) -> None:
    d = skill_dir / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n"
        "metadata:\n  version: 1\n---\n\n# body\n",
        encoding="utf-8",
    )


@pytest.fixture()
def local_skill_dir(tmp_path, monkeypatch) -> Path:
    skill_dir = tmp_path / "skill"
    skill_dir.mkdir()
    import xskill.config as config_module
    monkeypatch.setattr(
        config_module, "resolve_local_skill_dir", lambda **_kw: skill_dir,
    )
    return skill_dir


# ── 模式分发 ─────────────────────────────────────────────────────

def test_standalone_role_dispatches_local(monkeypatch):
    from xskill import runtime
    monkeypatch.setattr(runtime, "role", lambda: "standalone")
    called = {}
    monkeypatch.setattr(
        cli, "_cmd_search_local", lambda args: called.setdefault("local", 0) or 0,
    )
    assert cli.cmd_search(_args()) == 0
    assert "local" in called


def test_client_role_dispatches_team(monkeypatch):
    from xskill import runtime
    monkeypatch.setattr(runtime, "role", lambda: "client")
    called = {}
    monkeypatch.setattr(
        cli, "cmd_search_hub", lambda args: called.setdefault("team", 0) or 0,
    )
    assert cli.cmd_search(_args()) == 0
    assert "team" in called


def test_explicit_flags_override_role(monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "cmd_search_hub", lambda args: calls.append("team") or 0)
    monkeypatch.setattr(cli, "_cmd_search_local", lambda args: calls.append("local") or 0)
    # --team 在 standalone 角色下仍走 team；--local 在 client 角色下仍走本地
    cli.cmd_search(_args(team=True))
    cli.cmd_search(_args(local=True))
    assert calls == ["team", "local"]


# ── 本地路径：daemon 语义检索 ────────────────────────────────────

def test_local_semantic_hits_render_without_warning(monkeypatch, capsys):
    from xskill import runtime
    monkeypatch.setattr(
        runtime, "read_status", lambda: {"running": True, "port": 8000},
    )
    hits = [{
        "skill_name": "worktree-copy-alignment",
        "similarity": 0.7755,
        "description": "处理基于 git worktree 的文案对齐任务",
        "tags": [], "version": 1,
    }]
    rc = cli._cmd_search_local(
        _args(), post=lambda url, **kw: _Response(200, hits),
    )
    captured = capsys.readouterr()
    assert rc == 0
    assert "worktree-copy-alignment" in captured.out
    assert "语义相似度" in captured.out
    assert "回退" not in captured.err


def test_local_daemon_down_falls_back_to_bm25(
        monkeypatch, capsys, local_skill_dir):
    from xskill import runtime
    monkeypatch.setattr(runtime, "read_status", lambda: {"running": False})
    _write_skill(local_skill_dir, "copy-align", "处理前端文案对齐任务")
    _write_skill(local_skill_dir, "unrelated", "语音服务链路加固")
    rc = cli._cmd_search_local(_args())
    captured = capsys.readouterr()
    assert rc == 0
    assert "回退 BM25" in captured.err
    assert "copy-align" in captured.out
    assert "unrelated" not in captured.out


def test_local_dict_envelope_does_not_crash(
        monkeypatch, capsys, local_skill_dir):
    """#46 Bug A 回归：空索引的 dict 包络绝不能当 list 遍历崩掉。"""
    from xskill import runtime
    monkeypatch.setattr(
        runtime, "read_status", lambda: {"running": True, "port": 8000},
    )
    _write_skill(local_skill_dir, "copy-align", "处理前端文案对齐任务")
    envelope = {"results": [], "message": "skill index empty"}
    rc = cli._cmd_search_local(
        _args(), post=lambda url, **kw: _Response(200, envelope),
    )
    captured = capsys.readouterr()
    assert rc == 0
    assert "BM25" in captured.err        # 降级对用户可见
    assert "copy-align" in captured.out  # BM25 兜住了结果


def test_local_json_output_is_machine_readable(
        monkeypatch, capsys, local_skill_dir):
    from xskill import runtime
    monkeypatch.setattr(runtime, "read_status", lambda: {"running": False})
    _write_skill(local_skill_dir, "copy-align", "处理前端文案对齐任务")
    rc = cli._cmd_search_local(_args(json=True))
    captured = capsys.readouterr()
    assert rc == 0
    parsed = json.loads(captured.out)
    assert isinstance(parsed, list)
    assert parsed[0]["skill_name"] == "copy-align"
    assert parsed[0]["bm25_score"] > 0


def test_local_empty_repo_reports_no_match(
        monkeypatch, capsys, local_skill_dir):
    from xskill import runtime
    monkeypatch.setattr(runtime, "read_status", lambda: {"running": False})
    rc = cli._cmd_search_local(_args())
    captured = capsys.readouterr()
    assert rc == 0
    assert "无匹配" in captured.out


def test_local_bm25_survives_missing_config(monkeypatch, capsys, tmp_path):
    """首装无 config.yaml：BM25 回退不依赖配置，用默认 skill 目录兜底。"""
    import xskill.config as config_module
    from xskill import runtime

    def _raise_missing_config(*_a, **_k):
        raise FileNotFoundError("xskill config not found")

    monkeypatch.setattr(runtime, "read_status", lambda: {"running": False})
    # 完整 get_config 会因缺 llm 等字段失败；search 只窥视 skill_dir。
    monkeypatch.setattr(config_module, "get_config", _raise_missing_config)
    monkeypatch.setattr(config_module, "XSKILL_HOME", tmp_path)
    _write_skill(tmp_path / "skill", "copy-align", "处理前端文案对齐任务")
    rc = cli._cmd_search_local(_args())
    captured = capsys.readouterr()
    assert rc == 0
    assert "copy-align" in captured.out
