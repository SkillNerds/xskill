"""Continuation tests 5-8. PYTHONPATH=src python scripts/run_trae_test_part2.py"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import types
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))
if "xskill" not in sys.modules:
    pkg = types.ModuleType("xskill")
    pkg.__path__ = [str(SRC / "xskill")]  # type: ignore[attr-defined]
    sys.modules["xskill"] = pkg

from xskill.ecosystems import TraeIngester, ingest_trae_sessions, install_to_trae
from xskill.ecosystems._fallback import install_dir

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_CLI = ROOT / "tests/fixtures/trae/sample_agent_trajectory.json"
FIXTURE_IDE = ROOT / "tests/fixtures/trae/sample_ide_session.json"
home = Path.home()

print("--- 5 install (copy mode; skip symlink on win32) ---")
try:
    with tempfile.TemporaryDirectory() as td:
        h = Path(td)
        (h / ".trae-cn").mkdir()
        skill = h / "skill" / "demo"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(
            "---\nname: demo\ndescription: d\nversion: 1\n---\n# d\n", encoding="utf-8"
        )
        dest_dir = h / ".trae-cn" / "skills" / "demo"
        dest_dir.parent.mkdir(parents=True, exist_ok=True)
        mode = install_dir(skill, dest_dir, force_mode="copy")
        print("OK install_dir force copy:", mode, (dest_dir / "SKILL.md").is_file())
        if sys.platform != "win32":
            dest = install_to_trae(skill, target_root=h)
            print("OK install_to_trae ->", dest)
        else:
            print("SKIP install_to_trae symlink path on win32 (see test report)")
except Exception as e:
    print("FAIL install:", e)

print("--- 6 CLI ingest ---")
with tempfile.TemporaryDirectory() as td:
    h = Path(td)
    (h / ".trae-cn" / "trajectories").mkdir(parents=True)
    (h / ".trae-cn" / "trajectories" / "t.json").write_text(
        FIXTURE_CLI.read_text(encoding="utf-8"), encoding="utf-8"
    )
    out = h / "bridge"
    r = TraeIngester(target_traj_dir=out, home_root=h).scan_and_bridge()
    print("OK CLI bridged", len(r), [p.name for p in out.glob("traj_trae_cli_*.md")])

print("--- 7 IDE vscdb ---")
with tempfile.TemporaryDirectory() as td:
    h = Path(td)
    appdata = h / "AppData" / "Roaming"
    ws = appdata / "TRAE SOLO CN" / "User" / "workspaceStorage" / "ws1"
    ws.mkdir(parents=True)
    session = json.loads(FIXTURE_IDE.read_text(encoding="utf-8"))
    db = ws / "state.vscdb"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE ItemTable (key TEXT, value BLOB)")
    conn.execute(
        "INSERT INTO ItemTable VALUES (?, ?)",
        (
            "chat.ChatSessionStore.index",
            json.dumps({"version": 1, "entries": {"sess-demo-001": session}}),
        ),
    )
    conn.commit()
    conn.close()
    old = os.environ.get("APPDATA")
    os.environ["APPDATA"] = str(appdata)
    try:
        out = h / "bridge"
        r = TraeIngester(target_traj_dir=out, home_root=h).scan_and_bridge()
        print("OK IDE bridged", len(r), [p.name for p in out.glob("traj_trae_*.md")])
    finally:
        if old is None:
            os.environ.pop("APPDATA", None)
        else:
            os.environ["APPDATA"] = old

print("--- 8 real machine ---")
ws = Path(os.environ["APPDATA"]) / "TRAE SOLO CN" / "User" / "workspaceStorage"
entries = 0
db_count = 0
for d in ws.iterdir():
    if not d.is_dir():
        continue
    db = d / "state.vscdb"
    if not db.is_file():
        continue
    db_count += 1
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    row = conn.execute(
        "SELECT value FROM ItemTable WHERE key = ?",
        ("chat.ChatSessionStore.index",),
    ).fetchone()
    conn.close()
    if row:
        entries += len(json.loads(row[0]).get("entries") or {})
print(f"workspaces with db: {db_count}, chat entries: {entries}")

out = home / ".xskill" / "trae_sessions_test_run"
out.mkdir(parents=True, exist_ok=True)
r = ingest_trae_sessions(out, home_root=home)
mds = list(out.glob("traj_trae_*.md"))
print(f"real ingest: records={len(r)}, md_files={len(mds)}")

print("--- 9 serve smoke (import app hook only) ---")
try:
    from xskill.ecosystems import detect_known_ecosystems
    dets = [x for x in detect_known_ecosystems(home) if x["ecosystem"] == "trae"]
    print("serve would register:", dets[0]["bridge"] if dets else "none")
except Exception as e:
    print("FAIL detect:", e)

print("PART2 DONE")
