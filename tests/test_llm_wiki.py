from __future__ import annotations

from pathlib import Path

import pytest

from xskill.agents import agent_tools
from xskill.agents.llm_wiki import (
    AFTER_COMPACT_EMPTY_HINT,
    AFTER_COMPACT_HINT,
    apply_after_compact_hint,
    seed_generate_wiki,
    wiki_edit,
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


@pytest.mark.performance_contract
def test_wiki_log_appends_without_rewriting_history(tmp_path: Path, monkeypatch):
    root = seed_generate_wiki(tmp_path / "wiki")
    log = root / "log.md"
    log.write_text("existing history", encoding="utf-8")
    ctx = agent_tools.create_agent_tool_context(
        skill_dir=tmp_path / "skill",
        wiki_root=root,
    )
    (tmp_path / "skill").mkdir()
    original_read_text = Path.read_text
    original_write_text = Path.write_text

    def reject_full_read(path, *args, **kwargs):
        if path == log:
            raise AssertionError("wiki_log must not read the complete history")
        return original_read_text(path, *args, **kwargs)

    def reject_rewrite(path, *args, **kwargs):
        if path == log:
            raise AssertionError("wiki_log must append instead of rewriting history")
        return original_write_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", reject_full_read)
    monkeypatch.setattr(Path, "write_text", reject_rewrite)
    with agent_tools.use_agent_tool_context(ctx):
        result = wiki_log.entrypoint(entry="continue from here")

    content = original_read_text(log, encoding="utf-8")
    assert result.startswith("ok appended")
    assert content.startswith("existing history\n## [")
    assert content.endswith("] continue from here\n")


def test_wiki_log_recreates_missing_log(tmp_path: Path):
    root = seed_generate_wiki(tmp_path / "wiki")
    log = root / "log.md"
    log.unlink()
    ctx = agent_tools.create_agent_tool_context(
        skill_dir=tmp_path / "skill",
        wiki_root=root,
    )
    (tmp_path / "skill").mkdir()

    with agent_tools.use_agent_tool_context(ctx):
        result = wiki_log.entrypoint(entry="start again")

    assert result.startswith("ok appended")
    content = log.read_text(encoding="utf-8")
    assert content.startswith("# log\n\n## [")
    assert content.endswith("] start again\n")


def test_wiki_edit_append_and_replace(tmp_path: Path):
    root = seed_generate_wiki(tmp_path / "wiki")
    ctx = agent_tools.create_agent_tool_context(
        skill_dir=tmp_path / "skill",
        wiki_root=root,
    )
    (tmp_path / "skill").mkdir()
    row = "| traj_cc_admin_aaa11111 | 修好了轮询 | 先看锁文件 |"
    with agent_tools.use_agent_tool_context(ctx):
        appended = wiki_edit.entrypoint(
            path="pages/survey.md", old_string="", new_string=row,
        )
        assert appended.startswith("ok appended")
        replaced = wiki_edit.entrypoint(
            path="pages/survey.md",
            old_string="修好了轮询",
            new_string="修好了 watcher 轮询",
        )
        assert replaced.startswith("ok edited")
        missing = wiki_edit.entrypoint(
            path="pages/survey.md", old_string="不存在的原文", new_string="x",
        )
        assert missing.startswith("error:")
        empty_append = wiki_edit.entrypoint(
            path="pages/survey.md", old_string="", new_string="  ",
        )
        assert empty_append.startswith("error:")
        no_page = wiki_edit.entrypoint(
            path="pages/nothing.md", old_string="", new_string=row,
        )
        assert no_page.startswith("error:")
    text = (root / "pages" / "survey.md").read_text(encoding="utf-8")
    assert "修好了 watcher 轮询" in text
    # 追加行落在表尾，种子表头仍在
    assert text.index("| traj_id |") < text.index("traj_cc_admin_aaa11111")


def test_wiki_edit_rejects_ambiguous(tmp_path: Path):
    root = seed_generate_wiki(tmp_path / "wiki")
    ctx = agent_tools.create_agent_tool_context(
        skill_dir=tmp_path / "skill",
        wiki_root=root,
    )
    (tmp_path / "skill").mkdir()
    with agent_tools.use_agent_tool_context(ctx):
        wiki_edit.entrypoint(path="log.md", old_string="", new_string="重复 重复")
        ambiguous = wiki_edit.entrypoint(
            path="log.md", old_string="重复", new_string="x",
        )
    assert ambiguous.startswith("error:")
    assert "2" in ambiguous


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


def test_wiki_tools_noop_without_wiki_root(tmp_path: Path):
    ctx = agent_tools.create_agent_tool_context(skill_dir=tmp_path / "skill")
    (tmp_path / "skill").mkdir()
    with agent_tools.use_agent_tool_context(ctx):
        status = wiki_status.entrypoint()
    assert status.startswith("error:")
    assert "wiki_root" in status


def test_after_compact_hint_empty_survey_asks_to_read_sessions():
    messages = [{"role": "assistant", "content": "done"}]
    apply_after_compact_hint(messages)
    apply_after_compact_hint(messages)
    hints = [
        m for m in messages
        if "上下文刚被压缩" in str(
            m.get("content") if isinstance(m, dict) else getattr(m, "content", "")
        )
    ]
    assert len(hints) == 1
    text = hints[0]["content"] if isinstance(hints[0], dict) else hints[0].content
    assert "traj_search" in text
    assert "traj_cards" in text
    assert "read_traj" in text
    assert text == AFTER_COMPACT_EMPTY_HINT


def test_after_compact_hint_recovers_when_survey_has_rows(tmp_path: Path):
    root = seed_generate_wiki(tmp_path / "wiki")
    ctx = agent_tools.create_agent_tool_context(
        skill_dir=tmp_path / "skill",
        wiki_root=root,
    )
    (tmp_path / "skill").mkdir()
    with agent_tools.use_agent_tool_context(ctx):
        wiki_write.entrypoint(
            path="pages/survey.md",
            content="# survey\n\n| traj_id | 要点 | 可写进 skill 的做法 |\n|---|---|---|\n| traj_cc_admin_aaa11111 | 先读报错 | 换解释器 |\n",
        )
        messages = [{"role": "assistant", "content": "done"}]
        apply_after_compact_hint(messages)
    text = messages[-1]["content"] if isinstance(messages[-1], dict) else messages[-1].content
    assert text == AFTER_COMPACT_HINT
    assert "wiki_read pages/survey.md" in text
