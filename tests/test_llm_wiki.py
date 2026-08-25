from __future__ import annotations

from pathlib import Path

from xskill.agents import agent_tools
from xskill.agents.llm_wiki import (
    seed_generate_wiki,
    wiki_log,
    wiki_read,
    wiki_search,
    wiki_status,
    wiki_write,
)


def test_wiki_roundtrip_and_search(tmp_path: Path):
    root = seed_generate_wiki(tmp_path / "wiki")
    assert (root / "SCHEMA.md").is_file()
    assert (root / "pages" / "survey.md").is_file()
    ctx = agent_tools.create_agent_tool_context(
        skill_dir=tmp_path / "skill",
        wiki_root=root,
    )
    (tmp_path / "skill").mkdir()
    with agent_tools.use_agent_tool_context(ctx):
        written = wiki_write.entrypoint(
            path="pages/survey.md",
            content="# survey\n\n| traj_cc_admin_aaa11111 | 先读报错原文 | 换解释器 |\n",
        )
        status = wiki_status.entrypoint()
        read = wiki_read.entrypoint(path="pages/survey.md")
        hits = wiki_search.entrypoint(pattern="traj_cc_admin_aaa11111")
        logged = wiki_log.entrypoint(entry="看完一批会话")
    assert written.startswith("ok")
    assert "pages/survey.md" in status
    assert "traj_cc_admin_aaa11111" in read
    assert "pages/survey.md" in hits
    assert logged.startswith("ok appended")
    assert "看完一批会话" in (root / "log.md").read_text(encoding="utf-8")


def test_wiki_rejects_escape(tmp_path: Path):
    root = seed_generate_wiki(tmp_path / "wiki")
    ctx = agent_tools.create_agent_tool_context(
        skill_dir=tmp_path / "skill",
        wiki_root=root,
    )
    (tmp_path / "skill").mkdir()
    with agent_tools.use_agent_tool_context(ctx):
        denied = wiki_read.entrypoint(path="../secret.md")
    assert denied.startswith("error:")
