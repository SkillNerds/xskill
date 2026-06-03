"""test_team_client_server_scope.py — 客户端可变状态按 server 身份分目录

方案 A：上传游标 / 去抖 / 安装历史落 ~/.xskill/clients/<server_id>/，按
"连的是哪个 server"隔离。复现并锁死的 bug：换 server 后，上一个 server 的
"已上传"游标不再静默压制对新 server 的上传。
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from xskill import config
from xskill.team.client.collector import TeamCollector


# ── 路径派生：不同 server → 不同目录，同 server → 稳定同目录 ────────────
def test_client_dir_is_under_clients_and_per_server(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "XSKILL_HOME", tmp_path / ".xskill")

    a = config.get_team_client_cursor_path("http://1.1.1.1:8000")
    b = config.get_team_client_cursor_path("http://2.2.2.2:9961")

    assert a != b                              # 两个 server 互不污染
    assert a.parent.parent.name == "clients"   # ~/.xskill/clients/<id>/cursor.json
    assert a.parent.parent.parent == tmp_path / ".xskill"


def test_client_dir_stable_for_same_server(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "XSKILL_HOME", tmp_path / ".xskill")
    url = "http://7.220.144.233:9961"
    assert (config.get_team_client_cursor_path(url)
            == config.get_team_client_cursor_path(url))
    # 末尾斜杠 / 多余空白不改变身份（同一 server 不该裂成两个目录）
    assert (config.get_team_client_cursor_path(url)
            == config.get_team_client_cursor_path(url + "/"))


def test_cursor_and_history_share_one_server_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "XSKILL_HOME", tmp_path / ".xskill")
    url = "http://7.220.144.233:9961"
    cur = config.get_team_client_cursor_path(url)
    hist = config.get_team_client_history_path(url)
    assert cur.parent == hist.parent           # 同一个 <server_id> 目录


# ── 行为：换 server 后历史轨迹不再被上一个 server 的游标压制 ──────────
def _seed_quiet_traj(home: Path) -> Path:
    bridge = home / ".xskill" / "cc_sessions"
    bridge.mkdir(parents=True, exist_ok=True)
    md = bridge / "traj_cc_x_001.md"
    md.write_text("# a finished session body", encoding="utf-8")
    old = time.time() - 600
    os.utime(md, (old, old))
    return md


def test_switching_server_reuploads_history(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "XSKILL_HOME", tmp_path / ".xskill")
    home = tmp_path / "home"
    _seed_quiet_traj(home)

    url_a = "http://1.1.1.1:8000"
    url_b = "http://2.2.2.2:9961"

    # server A：采集并标记已上传 → A 的游标记下这条
    col_a = TeamCollector(
        cursor_path=config.get_team_client_cursor_path(url_a),
        quiet_seconds=180, min_change_interval=0, home_root=home)
    p = col_a.pending()[0]
    col_a.mark_uploaded(p.traj_id, p.sha256)
    assert col_a.pending() == []               # A 不再吐（已上传过）

    # 换 server B：同一条历史轨迹必须仍待上传（不被 A 的游标压制）
    col_b = TeamCollector(
        cursor_path=config.get_team_client_cursor_path(url_b),
        quiet_seconds=180, min_change_interval=0, home_root=home)
    ids = {x.traj_id for x in col_b.pending()}
    assert "traj_cc_x_001" in ids
