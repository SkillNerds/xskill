from __future__ import annotations

import argparse
import json

import xskill.cli as cli
from xskill import config as xconfig


# ── 客户端模式：registry list 现算视图（不读 watch_dirs/trajectories 表）──

def test_cmd_registry_list_client(tmp_path, monkeypatch, capsys):
    xskill_home = tmp_path / ".xskill"
    xskill_home.mkdir()
    monkeypatch.setattr("xskill.config.XSKILL_HOME", xskill_home)

    # bridge 目录：3 条已采集轨迹（每条一个 .json + 一个裸内容文件 + index.pkl 噪声）
    bridge = xskill_home / "cc_sessions"
    bridge.mkdir()
    for tid in ("traj_cc_a", "traj_cc_b", "traj_cc_c"):
        (bridge / f"{tid}.json").write_text("{}", encoding="utf-8")
        (bridge / tid).write_text("x", encoding="utf-8")        # 必须被忽略
    (bridge / "index.pkl").write_text("x", encoding="utf-8")     # 必须被忽略

    # 客户端连接状态 + 按 server 分目录的游标（方案 A）：cmd_registry_list_client
    # 先读 team_client.json 拿 server_url，再定位该 server 的 cursor.json。
    server_url = "http://7.220.144.233:9961"
    (xskill_home / "team_client.json").write_text(
        json.dumps({"server_url": server_url, "client_id": "cid", "join_token": "t"}),
        encoding="utf-8",
    )
    # cursor：3 条里 2 条已上传，外加一条不在 bridge 里的（不应计入）
    xconfig.get_team_client_cursor_path(server_url).write_text(
        json.dumps({"traj_cc_a": "s1", "traj_cc_b": "s2", "traj_other": "sX"}),
        encoding="utf-8",
    )

    src = tmp_path / ".claude" / "projects"
    monkeypatch.setattr(
        "xskill.ecosystems.detect_known_ecosystems",
        lambda home_root=None: [
            {"ecosystem": "claude_code", "source": src, "bridge": bridge},
        ],
    )

    rc = cli.cmd_registry_list_client()
    assert rc == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "ECOSYSTEM\tCOLLECTED\tUPLOADED\tSOURCE"
    row = next(l for l in lines if l.startswith("claude_code")).split("\t")
    assert row == ["claude_code", "3", "2", str(src)]   # 采集3 / 上传2 / 真实源目录


def test_cmd_registry_list_client_no_ecosystems(tmp_path, monkeypatch, capsys):
    xskill_home = tmp_path / ".xskill"
    xskill_home.mkdir()
    monkeypatch.setattr("xskill.config.XSKILL_HOME", xskill_home)
    monkeypatch.setattr(
        "xskill.ecosystems.detect_known_ecosystems", lambda home_root=None: [])
    rc = cli.cmd_registry_list_client()
    assert rc == 0
    assert "(no agent ecosystems detected)" in capsys.readouterr().out


def test_main_routes_client_registry_list(tmp_path, monkeypatch):
    # 有 team_client.json = 客户端 → registry list 走现算分支，且不经过 config 闸
    state = tmp_path / "team_client.json"
    state.write_text("{}", encoding="utf-8")
    monkeypatch.setattr("xskill.config.get_team_client_state_path", lambda: state)
    # 无 standalone 数据（registry.db 指向不存在的临时路径 → watch_dirs 计为 0）
    monkeypatch.setattr("xskill.config.get_registry_db_path",
                        lambda: tmp_path / "registry.db")
    called = {}
    monkeypatch.setattr("xskill.cli.cmd_registry_list_client",
                        lambda: (called.__setitem__("hit", True), 0)[1])

    def _boom():
        raise AssertionError("client registry list 不应走到 config 闸")
    monkeypatch.setattr("xskill.config.ensure_config_exists", _boom)
    monkeypatch.setattr("sys.argv", ["xskill", "registry", "list"])

    rc = cli.main()
    assert rc == 0
    assert called.get("hit") is True


def _make_registry_db(path, rows: int) -> None:
    import sqlite3
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE watch_dirs (id INTEGER PRIMARY KEY, path TEXT)")
    for i in range(rows):
        conn.execute("INSERT INTO watch_dirs (path) VALUES (?)", (f"/d/{i}",))
    conn.commit()
    conn.close()


def test_standalone_watch_dir_count(tmp_path, monkeypatch):
    db = tmp_path / "registry.db"
    monkeypatch.setattr("xskill.config.get_registry_db_path", lambda: db)
    assert cli._standalone_watch_dir_count() == 0          # 库不存在 → 0
    _make_registry_db(db, 3)
    assert cli._standalone_watch_dir_count() == 3


def test_main_prefers_standalone_when_watch_dirs_present(tmp_path, monkeypatch):
    # 本机同时有 team_client.json 和 standalone 数据 → 不走客户端视图
    state = tmp_path / "team_client.json"
    state.write_text("{}", encoding="utf-8")
    db = tmp_path / "registry.db"
    _make_registry_db(db, 2)
    monkeypatch.setattr("xskill.config.get_team_client_state_path", lambda: state)
    monkeypatch.setattr("xskill.config.get_registry_db_path", lambda: db)

    def _boom():
        raise AssertionError("有 standalone 数据时不应走客户端视图")
    monkeypatch.setattr("xskill.cli.cmd_registry_list_client", _boom)
    # 在 config 闸早退，避免构造 facade
    monkeypatch.setattr("xskill.config.ensure_config_exists", lambda: False)
    monkeypatch.setattr("sys.argv", ["xskill", "registry", "list"])

    rc = cli.main()
    assert rc == 0   # 走到 config 闸正常早退，未触发客户端视图


# ── standalone 模式：list 输出带表头，数据行仍为 \t 分隔（e2e 依赖）──

def test_standalone_registry_list_has_header(capsys):
    class _W:
        id = 1
        ecosystem = "claude_code"
        traj_count = 5
        indexed_count = 3
        label = "cc"
        path = "/home/x/.xskill/cc_sessions"

    class _Reg:
        def list(self):
            return [_W()]

    class _X:
        registry = _Reg()

    args = argparse.Namespace(registry_action="list", path=None, label="")
    rc = cli.cmd_registry(args, _X())
    assert rc == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "ID\tECOSYSTEM\tTRAJ\tINDEXED\tLABEL\tPATH"
    assert out[1].split("\t") == [
        "1", "claude_code", "5", "3", "cc", "/home/x/.xskill/cc_sessions"]
