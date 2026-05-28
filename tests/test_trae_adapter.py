"""
test_trae_adapter.py -- Trae IDE / Trae Agent 接入单测
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from xskill.ecosystems import (
    TraeIngester,
    adapt_trajectory,
    detect_known_ecosystems,
    detect_trae_record,
    install_to_trae,
    ingest_trae_sessions,
    _trae_skills_roots,
    _sessions_from_chat_blob,
)
from xskill.skill.frontmatter import serialize as fm_serialize

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "trae"
IDE_SESSION = FIXTURE_DIR / "sample_ide_session.json"
AGENT_TRAJ = FIXTURE_DIR / "sample_agent_trajectory.json"


@pytest.fixture
def ide_session_text() -> str:
    return IDE_SESSION.read_text(encoding="utf-8")


@pytest.fixture
def agent_traj_text() -> str:
    return AGENT_TRAJ.read_text(encoding="utf-8")


class TestTraeAdapters:
    def test_ide_session_adapter(self, ide_session_text):
        md, meta = adapt_trajectory(
            ide_session_text, "trae_ide_session_json",
            metadata={"workspace_folder": "/proj/foo"},
        )
        assert "# Trae IDE Session" in md
        assert "## User" in md
        assert "authentication timeout" in md
        assert meta["source"] == "trae_ide_session_json"
        assert meta["total_turns"] == 3
        assert "read_file" in meta["tool_names"]

    def test_agent_trajectory_adapter(self, agent_traj_text):
        md, meta = adapt_trajectory(agent_traj_text, "trae_agent_trajectory_json")
        assert "# Trae Agent Trajectory" in md
        assert "hello world" in md.lower()
        assert meta["source"] == "trae_agent_trajectory_json"
        assert "str_replace_based_edit_tool" in meta["tool_names"]


class TestTraeChatBlobParsing:
    def test_chat_session_store_entries(self):
        blob = {
            "version": 1,
            "entries": {
                "s1": {
                    "sessionId": "s1",
                    "messages": [{"role": "user", "content": "hi"}],
                }
            },
        }
        sessions = _sessions_from_chat_blob(blob, "chat.ChatSessionStore.index")
        assert len(sessions) == 1
        assert sessions[0]["sessionId"] == "s1"

    def test_agent_storage_list(self):
        blob = {
            "list": [
                {"sessionId": "a", "messages": [{"role": "user", "content": "x"}]},
            ]
        }
        sessions = _sessions_from_chat_blob(blob, "memento/icube-ai-agent-storage")
        assert len(sessions) == 1


def _write_vscdb(db_path: Path, chat_key: str, chat_value: dict) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS ItemTable (key TEXT UNIQUE ON CONFLICT REPLACE, value BLOB)"
    )
    conn.execute(
        "INSERT INTO ItemTable (key, value) VALUES (?, ?)",
        (chat_key, json.dumps(chat_value)),
    )
    conn.commit()
    conn.close()


class TestTraeIngest:
    def test_ingest_from_workspace_storage(self, tmp_path, ide_session_text):
        home = tmp_path / "home"
        ws_id = "abc123"
        ws_dir = (
            home / "AppData" / "Roaming" / "TRAE SOLO CN"
            / "User" / "workspaceStorage" / ws_id
        )
        session = json.loads(ide_session_text)
        _write_vscdb(
            ws_dir / "state.vscdb",
            "chat.ChatSessionStore.index",
            {"version": 1, "entries": {"sess-demo-001": session}},
        )
        (ws_dir / "workspace.json").write_text(
            json.dumps({"folder": "file:///c:/proj/foo"}), encoding="utf-8",
        )

        traj_dir = tmp_path / "traj"
        import os
        old = os.environ.get("APPDATA")
        os.environ["APPDATA"] = str(home / "AppData" / "Roaming")
        try:
            records = ingest_trae_sessions(traj_dir, home_root=home)
        finally:
            if old is None:
                os.environ.pop("APPDATA", None)
            else:
                os.environ["APPDATA"] = old

        assert len(records) == 1
        md_files = list(traj_dir.glob("traj_trae_*.md"))
        assert len(md_files) == 1
        assert "authentication timeout" in md_files[0].read_text(encoding="utf-8")

    def test_ingest_agent_cli_json(self, tmp_path, agent_traj_text):
        home = tmp_path / "home"
        traj_src = home / ".trae-cn" / "trajectories"
        traj_src.mkdir(parents=True)
        (traj_src / "trajectory_20260512_120000.json").write_text(
            agent_traj_text, encoding="utf-8",
        )
        traj_dir = tmp_path / "traj"
        records = ingest_trae_sessions(traj_dir, home_root=home)
        assert len(records) == 1
        assert list(traj_dir.glob("traj_trae_cli_*.md"))

    def test_detect_trae_record(self, tmp_path):
        (tmp_path / ".trae-cn").mkdir()
        rec = detect_trae_record(tmp_path)
        assert rec is not None
        assert rec["ecosystem"] == "trae"

    def test_detect_known_ecosystems_includes_trae(self, tmp_path):
        (tmp_path / ".trae-cn").mkdir()
        ids = {d["ecosystem"] for d in detect_known_ecosystems(home_root=tmp_path)}
        assert "trae" in ids


class TestTraeInstall:
    def test_install_to_trae_cn_skills(self, tmp_path):
        home = tmp_path
        (home / ".trae-cn").mkdir()
        skill_path = home / "skillsrc" / "demo"
        skill_path.mkdir(parents=True)
        fm = {"name": "demo", "description": "d", "version": 1}
        (skill_path / "SKILL.md").write_text(
            fm_serialize(fm, "# demo\n"), encoding="utf-8",
        )
        dest = install_to_trae(skill_path, target_root=home)
        assert dest == home / ".trae-cn" / "skills" / "demo" / "SKILL.md"
        assert dest.is_file()

    def test_trae_skills_roots_default_cn(self, tmp_path):
        roots = _trae_skills_roots(tmp_path)
        assert roots[0] == tmp_path / ".trae-cn" / "skills"
