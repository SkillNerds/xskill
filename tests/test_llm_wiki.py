"""Generate wiki：seed 出 read-plan 与 knowledge；越出 wiki 根要拦住。"""
from __future__ import annotations

from pathlib import Path

from xskill.agents import agent_tools, llm_wiki
from xskill.team.server.generate_jobs import prepare_generate_wiki


def _call(tool, *args, **kwargs):
    entrypoint = tool if callable(tool) else getattr(tool, "entrypoint")
    return entrypoint(*args, **kwargs)


def _ctx(tmp_path: Path, wiki: Path):
    skill_dir = tmp_path / "skill"
    skill_dir.mkdir(exist_ok=True)
    return agent_tools.create_agent_tool_context(
        skill_dir=skill_dir,
        wiki_root=wiki,
        generate_user_id="alice",
        extra_read_roots=(wiki,),
    )


def test_seed_writes_read_plan_and_knowledge(tmp_path: Path):
    root = llm_wiki.seed_generate_wiki(tmp_path / "wiki")
    plan = (root / "pages" / "read-plan.md").read_text(encoding="utf-8")
    knowledge = (root / "pages" / "knowledge.md").read_text(encoding="utf-8")
    survey = (root / "pages" / "survey.md").read_text(encoding="utf-8")
    assert "必要信息" in plan
    assert "未读完" in plan
    assert "待读" in plan or "状态" in plan
    assert "已入 skill" in knowledge
    assert "来源 traj_id" in knowledge
    assert "read_file" in survey
    old = plan
    (root / "pages" / "read-plan.md").write_text("KEEP_ME\n", encoding="utf-8")
    llm_wiki.seed_generate_wiki(root)
    assert (root / "pages" / "read-plan.md").read_text(encoding="utf-8") == "KEEP_ME\n"
    assert old != "KEEP_ME\n"


def test_wiki_path_escape_blocked(tmp_path: Path):
    wiki = llm_wiki.seed_generate_wiki(tmp_path / "wiki")
    secret = tmp_path / "secret.md"
    secret.write_text("do not leak\n", encoding="utf-8")
    ctx = _ctx(tmp_path, wiki)
    with agent_tools.use_agent_tool_context(ctx):
        escaped = _call(llm_wiki.wiki_read, path="../secret.md")
        dotted = _call(llm_wiki.wiki_write, path="pages/../../secret.md", content="x")
        ok = _call(llm_wiki.wiki_read, path="pages/read-plan.md")
    assert escaped.startswith("error:")
    assert "do not leak" not in escaped
    assert dotted.startswith("error:")
    assert "必要信息" in ok


def test_wiki_symlink_escape_blocked(tmp_path: Path):
    wiki = llm_wiki.seed_generate_wiki(tmp_path / "wiki")
    outside = tmp_path / "outside.md"
    outside.write_text("outside-secret\n", encoding="utf-8")
    link = wiki / "pages" / "leak.md"
    link.symlink_to(outside)
    ctx = _ctx(tmp_path, wiki)
    with agent_tools.use_agent_tool_context(ctx):
        denied = _call(llm_wiki.wiki_read, path="pages/leak.md")
    assert denied.startswith("error:")
    assert "outside-secret" not in denied


def test_prepare_generate_wiki_does_not_overwrite(tmp_path: Path):
    logs = tmp_path / "logs"
    root = prepare_generate_wiki(logs, "alice", "job1")
    plan = root / "pages" / "read-plan.md"
    plan.write_text("ALREADY_WRITTEN\n", encoding="utf-8")
    again = prepare_generate_wiki(logs, "alice", "job1")
    assert again == root
    assert plan.read_text(encoding="utf-8") == "ALREADY_WRITTEN\n"


def test_after_compact_hint_mentions_plan(tmp_path: Path):
    messages = [{"role": "user", "content": "start"}]
    llm_wiki.apply_after_compact_hint(messages)
    assert any("上下文刚被压缩" in str(m.get("content")) for m in messages)
    assert any("read-plan.md" in str(m.get("content")) for m in messages)
    before = len(messages)
    llm_wiki.apply_after_compact_hint(messages)
    assert len(messages) == before
