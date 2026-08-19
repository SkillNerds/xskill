"""tests/test_sanitize.py — 入库前轨迹文本清洗（去 ANSI / 控制字符）"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from xskill.utils.sanitize import sanitize_trajectory_text as S


# ── 纯函数 ─────────────────────────────────────────────────────────

def test_strips_ansi_escape():
    assert S("\x1b[31mred\x1b[0m text") == "red text"


def test_strips_c0_control_chars_keeps_tab_newline():
    out = S("a\x00b\x07c\td\ne")
    assert out == "abc\td\ne"  # \x00 \x07 删, \t \n 留


def test_preserves_spaces_and_unicode_text():
    assert S("中文 空格  test  end") == "中文 空格  test  end"
    assert S("x   y") == "x   y"


def test_line_count_consistent_after_sanitize():
    # \x0b \x0c \x1c-\x1e \x85     是 splitlines 会切但 \n 不切的
    dirty = "L1\x0bL1b\x0cL1c\x1cL1d\x85L1e L1f\nL2\nL3"
    clean = S(dirty)
    assert len(clean.splitlines()) == clean.count("\n") + 1


def test_normalizes_crlf():
    assert S("a\r\nb\rc") == "a\nb\nc"


def test_keeps_replacement_char():
    # U+FFFD 是已丢数据的标记,保留(救不回,但提示哪里丢了)
    assert "�" in S("good�bad")


def test_empty_and_clean_passthrough():
    assert S("") == ""
    assert S("已经很干净的 markdown\n## User\nhi") == "已经很干净的 markdown\n## User\nhi"


# ── 集成：submit_trajectory 落盘前清洗 ──────────────────────────────

def test_submit_trajectory_sanitizes_before_write(tmp_path):
    from xskill.ecosystems._shared import submit_trajectory
    dirty = "## User\n\x1b[32mhello\x1b[0m\x0bworld\x00\n## Assistant\nhi"
    res = submit_trajectory(content=dirty, format="raw",
                            traj_id="traj_test_x", traj_dir=tmp_path)
    written = Path(res["path"]).read_text(encoding="utf-8")
    assert "\x1b" not in written and "\x00" not in written and "\x0b" not in written
    assert "hello" in written and "world" in written
    # 无隐藏换行符残留(splitlines 会切但 \n 不切的那些都清掉了)
    assert not any(c in written for c in "\x0b\x0c\x1c\x1d\x1e\x85  \r")


# ── 集成：team_upload 落盘前清洗 ────────────────────────────────────

def test_team_upload_sanitizes_content(tmp_path):
    import hashlib
    from xskill.team.server import api as server_api
    from xskill.team.server.client_registry import ClientRegistry

    traj_root = tmp_path / "team_traj"
    reg = ClientRegistry(tmp_path / "clients.db")
    server_api.init_team_context(
        join_token="tok", client_registry=reg,
        skill_dir=tmp_path / "skill", traj_root=traj_root,
        register_dir=lambda path, label: None,
    )
    app = FastAPI()
    app.include_router(server_api.router)
    client = TestClient(app)

    r = client.post("/api/v1/team/register",
                    json={"token": "tok", "client_label": "a", "hostname": "a"})
    cid = r.json()["client_id"]
    hdr = {"X-Xskill-Token": "tok", "X-Xskill-Client": cid}

    body = "## User\n\x1b[31mdirty\x1b[0m\x0chidden\x00\nok"
    r = client.post("/api/v1/team/upload", headers=hdr, json={
        "trajectories": [{"traj_id": "traj_cc_x", "content": body,
                          "sha256": hashlib.sha256(body.encode()).hexdigest()}]})
    assert r.status_code == 200 and r.json()["accepted"] == ["traj_cc_x"]

    md = (traj_root / "clients" / cid / "sessions"
          / f"traj_u_{cid[:8]}_cc_x.md").read_text(encoding="utf-8")
    assert "\x1b" not in md and "\x00" not in md and "\x0c" not in md
    assert "dirty" in md and "hidden" in md
