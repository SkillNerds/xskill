#!/usr/bin/env python3
"""Trae 兼容全流程自检（不依赖 pytest）。在项目根目录执行：
  set PYTHONPATH=src
  python scripts/run_trae_full_test.py
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

# 避免 import xskill 时加载 __init__.py（依赖 dulwich 等全量依赖）
import types

if "xskill" not in sys.modules:
    _xskill_pkg = types.ModuleType("xskill")
    _xskill_pkg.__path__ = [str(SRC / "xskill")]  # type: ignore[attr-defined]
    sys.modules["xskill"] = _xskill_pkg

FIXTURE_IDE = ROOT / "tests" / "fixtures" / "trae" / "sample_ide_session.json"
FIXTURE_CLI = ROOT / "tests" / "fixtures" / "trae" / "sample_agent_trajectory.json"

PASS = 0
FAIL = 0
WARN = 0


def ok(msg: str) -> None:
    global PASS
    PASS += 1
    print(f"  [PASS] {msg}")


def fail(msg: str, exc: BaseException | None = None) -> None:
    global FAIL
    FAIL += 1
    print(f"  [FAIL] {msg}")
    if exc:
        traceback.print_exception(type(exc), exc, exc.__traceback__, limit=2)


def warn(msg: str) -> None:
    global WARN
    WARN += 1
    print(f"  [WARN] {msg}")


def section(title: str) -> None:
    print(f"\n=== {title} ===")


def main() -> int:
    section("0. 环境路径")
    home = Path.home()
    trae_cn = home / ".trae-cn"
    ws = Path(os.environ.get("APPDATA", "")) / "TRAE SOLO CN" / "User" / "workspaceStorage"
    xskill_cfg = home / ".xskill" / "config.yaml"
    print(f"  .trae-cn: {trae_cn.is_dir()}")
    print(f"  workspaceStorage: {ws.is_dir()}")
    print(f"  config.yaml: {xskill_cfg.is_file()}")
    if trae_cn.is_dir():
        ok(".trae-cn 存在")
    else:
        warn(".trae-cn 不存在（部分测试用临时目录）")
    if ws.is_dir():
        ok("TRAE SOLO CN workspaceStorage 存在")
    else:
        warn("workspaceStorage 不存在（跳过实机 IDE 摄取检查）")

    section("1. 导入 xskill.ecosystems（需 dulwich）")
    try:
        from xskill.ecosystems import (
            TraeIngester,
            adapt_trajectory,
            detect_known_ecosystems,
            detect_trae_record,
            install_to_trae,
            ingest_trae_sessions,
        )
        from xskill.ecosystems.trae import (
            _sessions_from_chat_blob,
            _trae_workspace_storage_roots,
        )
        ok("ecosystems 导入成功")
    except Exception as e:
        fail("ecosystems 导入失败（请先 pip install dulwich pyyaml）", e)
        print(f"\n合计: PASS={PASS} FAIL={FAIL} WARN={WARN}")
        return 1

    section("2. detect_trae_record / detect_known_ecosystems")
    try:
        rec = detect_trae_record(home)
        if rec and rec.get("ecosystem") == "trae":
            ok(f"detect_trae_record: source={rec['source']}")
        else:
            fail(f"detect_trae_record 未返回 trae: {rec}")
        ids = {d["ecosystem"] for d in detect_known_ecosystems(home_root=home)}
        if "trae" in ids:
            ok("detect_known_ecosystems 含 trae")
        else:
            fail(f"detect_known_ecosystems 无 trae: {ids}")
    except Exception as e:
        fail("探测异常", e)

    section("3. adapter（fixture）")
    try:
        ide = FIXTURE_IDE.read_text(encoding="utf-8")
        md, meta = adapt_trajectory(ide, "trae_ide_session_json")
        assert "authentication timeout" in md
        assert meta["total_turns"] == 3
        assert meta["source"] == "trae_ide_session_json"
        ok("trae_ide_session_json adapter")

        agent = FIXTURE_CLI.read_text(encoding="utf-8")
        md2, meta2 = adapt_trajectory(agent, "trae_agent_trajectory_json")
        assert "hello world" in md2.lower()
        assert "str_replace_based_edit_tool" in meta2.get("tool_names", [])
        ok("trae_agent_trajectory_json adapter")
    except Exception as e:
        fail("adapter", e)

    section("4. chat blob 解析")
    try:
        blob = {"version": 1, "entries": {"s1": {"sessionId": "s1", "messages": [{"role": "user", "content": "hi"}]}}}
        assert len(_sessions_from_chat_blob(blob, "chat.ChatSessionStore.index")) == 1
        ok("_sessions_from_chat_blob")
    except Exception as e:
        fail("chat blob", e)

    section("5. install_to_trae（临时目录）")
    try:
        with tempfile.TemporaryDirectory() as td:
            h = Path(td)
            (h / ".trae-cn").mkdir()
            skill = h / "skill" / "demo"
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text("---\nname: demo\ndescription: d\nversion: 1\n---\n# demo\n", encoding="utf-8")
            dest = install_to_trae(skill, target_root=h)
            assert (h / ".trae-cn" / "skills" / "demo" / "SKILL.md").is_file()
            ok(f"install_to_trae -> {dest.name}")
    except Exception as e:
        fail("install_to_trae", e)

    section("6. ingest CLI 轨迹（临时目录）")
    try:
        with tempfile.TemporaryDirectory() as td:
            h = Path(td)
            (h / ".trae-cn" / "trajectories").mkdir(parents=True)
            agent = FIXTURE_CLI.read_text(encoding="utf-8")
            (h / ".trae-cn" / "trajectories" / "trajectory_test.json").write_text(agent, encoding="utf-8")
            out = h / "bridge"
            recs = TraeIngester(target_traj_dir=out, home_root=h).scan_and_bridge()
            assert len(recs) == 1
            mds = list(out.glob("traj_trae_cli_*.md"))
            assert mds and "hello world" in mds[0].read_text(encoding="utf-8").lower()
            ok(f"CLI ingest: {mds[0].name}")
    except Exception as e:
        fail("CLI ingest", e)

    section("7. ingest IDE workspaceStorage（模拟 vscdb）")
    try:
        with tempfile.TemporaryDirectory() as td:
            h = Path(td)
            appdata = h / "AppData" / "Roaming"
            ws_dir = appdata / "TRAE SOLO CN" / "User" / "workspaceStorage" / "ws1"
            ws_dir.mkdir(parents=True)
            session = json.loads(FIXTURE_IDE.read_text(encoding="utf-8"))
            db = ws_dir / "state.vscdb"
            conn = sqlite3.connect(db)
            conn.execute("CREATE TABLE ItemTable (key TEXT UNIQUE, value BLOB)")
            conn.execute(
                "INSERT INTO ItemTable VALUES (?, ?)",
                ("chat.ChatSessionStore.index", json.dumps({"version": 1, "entries": {"sess-demo-001": session}})),
            )
            conn.commit()
            conn.close()
            old = os.environ.get("APPDATA")
            os.environ["APPDATA"] = str(appdata)
            try:
                out = h / "bridge_ide"
                recs = TraeIngester(target_traj_dir=out, home_root=h).scan_and_bridge()
                assert len(recs) == 1
                md = next(out.glob("traj_trae_*.md"))
                assert "authentication timeout" in md.read_text(encoding="utf-8")
                ok(f"IDE vscdb ingest: {md.name}")
            finally:
                if old is None:
                    os.environ.pop("APPDATA", None)
                else:
                    os.environ["APPDATA"] = old
    except Exception as e:
        fail("IDE vscdb ingest", e)

    section("8. 本机实机 workspaceStorage（只读探测）")
    if ws.is_dir():
        db_count = 0
        chat_entries = 0
        for d in ws.iterdir():
            if not d.is_dir():
                continue
            db = d / "state.vscdb"
            if not db.is_file():
                continue
            db_count += 1
            try:
                c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
                row = c.execute(
                    "SELECT value FROM ItemTable WHERE key = ?",
                    ("chat.ChatSessionStore.index",),
                ).fetchone()
                c.close()
                if row:
                    data = json.loads(row[0])
                    chat_entries += len(data.get("entries") or {})
            except Exception:
                pass
        print(f"  workspace 数: {db_count}, chat.ChatSessionStore.index entries 合计: {chat_entries}")
        if db_count:
            ok(f"实机 {db_count} 个 state.vscdb")
        else:
            warn("workspaceStorage 下无 state.vscdb")
        if chat_entries == 0:
            warn("实机 chat entries=0，serve 后 IDE 轨迹可能不会桥接（需先在 Trae Builder 对话）")
        else:
            try:
                out = home / ".xskill" / "trae_sessions_test_run"
                out.mkdir(parents=True, exist_ok=True)
                before = set(out.glob("traj_trae_*.md"))
                recs = ingest_trae_sessions(out, home_root=home)
                after = set(out.glob("traj_trae_*.md")) - before
                if recs or after:
                    ok(f"实机 ingest 桥接 {len(recs)} 条, 新 md {len(after)}")
                else:
                    warn("实机 ingest 未桥接新会话（可能均已处理过或 entries 结构不匹配）")
            except Exception as e:
                fail("实机 ingest_trae_sessions", e)
    else:
        warn("跳过实机 workspaceStorage")

    section("9. xskill CLI 可用性")
    try:
        import subprocess
        r = subprocess.run(
            [sys.executable, "-m", "xskill.cli", "--help"],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=30,
            env={**os.environ, "PYTHONPATH": str(SRC)},
        )
        if r.returncode == 0 and "serve" in (r.stdout + r.stderr):
            ok("xskill.cli --help")
        else:
            fail(f"xskill.cli exit={r.returncode}")
    except Exception as e:
        fail("xskill CLI", e)

    print(f"\n{'='*50}")
    print(f"合计: PASS={PASS}  FAIL={FAIL}  WARN={WARN}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
