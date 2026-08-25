from __future__ import annotations

import json
from pathlib import Path

import pytest

from xskill.agents import agent_tools
from xskill.agents import session_catalog as session_catalog_mod
from xskill.agents.generate_agent import ONHOLD_PROMPT_LINE, SYSTEM_PROMPT
from xskill.agents.session_catalog import list_sessions, session_card, session_cards


def _ctx(tmp_path: Path, *, blocked=()):
    skill = tmp_path / "skill"
    skill.mkdir()
    live = tmp_path / "sessions"
    live.mkdir()
    held = tmp_path / "held"
    held.mkdir()
    payload = {
        "source": "claude_code_session_jsonl",
        "query": "为什么本机仓库热路径卡住了",
        "total_turns": 4,
        "tool_names": ["Bash", "Read"],
        "timeline": [
            {"role": "user", "content": "看一下卡在哪"},
            {"role": "tool_call", "tool": "Bash", "input": {"command": "ps aux"}},
        ],
    }
    (live / "traj_cc_admin_aaa11111.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8",
    )
    (live / "traj_cc_work_bbb22222.md").write_text(
        "---\ntraj_id: traj_cc_work_bbb22222\nsource: markdown\nturns: 2\n"
        "tools: Read\n---\n\n# query\n修一下提交失败\n",
        encoding="utf-8",
    )
    (held / "traj_cc_held_ccc33333.md").write_text("secret\n", encoding="utf-8")
    ctx = agent_tools.create_agent_tool_context(
        skill_dir=skill,
        default_traj_root=live,
        extra_read_roots=(live, held),
        blocked_read_roots=tuple(blocked) or (held,),
    )
    return ctx


def test_list_and_card_skip_onhold(tmp_path: Path):
    ctx = _ctx(tmp_path)
    with agent_tools.use_agent_tool_context(ctx):
        listing = list_sessions.entrypoint()
        card = session_card.entrypoint(traj_id="traj_cc_admin_aaa11111")
        held = session_card.entrypoint(traj_id="traj_cc_held_ccc33333")
        batch = session_cards.entrypoint(
            traj_ids="traj_cc_admin_aaa11111 traj_cc_work_bbb22222",
        )
    assert "traj_cc_admin_aaa11111" in listing
    assert "traj_cc_work_bbb22222" in listing
    assert "traj_cc_held_ccc33333" not in listing
    assert "热路径" in card
    assert "Bash" in card
    assert held.startswith("error:")
    assert "batch=2" in batch
    assert "修一下提交失败" in batch


def test_generate_prompt_keeps_onhold_and_mentions_sessions():
    lines = SYSTEM_PROMPT.splitlines()
    assert ONHOLD_PROMPT_LINE in lines
    assert "list_sessions" in SYSTEM_PROMPT
    assert "session_cards" in SYSTEM_PROMPT
    assert "wiki_write" in SYSTEM_PROMPT
    assert (
        SYSTEM_PROMPT.index("优先阅读范围")
        < SYSTEM_PROMPT.index(ONHOLD_PROMPT_LINE)
        < SYSTEM_PROMPT.index("# 你可以读的目录")
    )


def test_symlink_cannot_escape_allowed_session_root(tmp_path: Path):
    ctx = _ctx(tmp_path)
    live = Path(ctx.default_traj_root)
    outside = tmp_path / "outside"
    outside.mkdir()
    escaped = outside / "traj_escape.json"
    escaped.write_text(
        json.dumps({"query": "outside secret"}), encoding="utf-8",
    )
    try:
        (live / escaped.name).symlink_to(escaped)
    except OSError:
        pytest.skip("当前平台不允许创建测试符号链接")

    with agent_tools.use_agent_tool_context(ctx):
        listing = list_sessions.entrypoint()
        card = session_card.entrypoint(traj_id="traj_escape")

    assert "traj_escape" not in listing
    assert card.startswith("error:")
    assert "outside secret" not in card


def test_json_metadata_and_markdown_body_merge_into_one_session(tmp_path: Path):
    ctx = _ctx(tmp_path)
    live = Path(ctx.default_traj_root)
    traj_id = "traj_same_session"
    (live / f"{traj_id}.json").write_text(
        json.dumps({
            "source": "json-sidecar",
            "query": "json query",
            "total_turns": 7,
            "tool_names": ["JsonTool"],
        }),
        encoding="utf-8",
    )
    (live / f"{traj_id}.md").write_text(
        "---\nsource: markdown\nturns: 2\ntools: MdTool\n---\n\n"
        "# query\nmarkdown body\n",
        encoding="utf-8",
    )

    with agent_tools.use_agent_tool_context(ctx):
        listing = list_sessions.entrypoint()
        card = session_card.entrypoint(traj_id=traj_id)

    assert listing.count(traj_id) == 1
    assert "source: json-sidecar" in card
    assert "turns: 7" in card
    assert "tools: JsonTool" in card
    assert "markdown body" in card


def test_markdown_card_is_scrubbed_and_does_not_expose_absolute_path(tmp_path: Path):
    ctx = _ctx(tmp_path)
    live = Path(ctx.default_traj_root)
    traj_id = "traj_secret_card"
    (live / f"{traj_id}.md").write_text(
        "# query\npassword: hunter2\n\n# notes\nsk-abcdefghijk\n",
        encoding="utf-8",
    )

    with agent_tools.use_agent_tool_context(ctx):
        card = session_card.entrypoint(traj_id=traj_id)

    assert "hunter2" not in card
    assert "sk-abcdefghijk" not in card
    assert "[REDACTED]" in card
    assert str(live) not in card


def test_pagination_reads_only_page_and_batch_reuses_one_scan(
    tmp_path: Path, monkeypatch,
):
    ctx = _ctx(tmp_path)
    live = Path(ctx.default_traj_root)
    for index in range(12):
        (live / f"traj_page_{index:02d}.md").write_text(
            f"# query\npage {index}\n", encoding="utf-8",
        )

    summarized = []
    scans = 0
    original_summarize = session_catalog_mod.summarize_session_file
    original_iter = session_catalog_mod._iter_traj_files

    def counted_summarize(path):
        if path.stem.startswith("traj_page_"):
            summarized.append(path.stem)
        return original_summarize(path)

    def counted_iter(roots):
        nonlocal scans
        scans += 1
        return original_iter(roots)

    monkeypatch.setattr(
        session_catalog_mod, "summarize_session_file", counted_summarize,
    )
    monkeypatch.setattr(session_catalog_mod, "_iter_traj_files", counted_iter)

    with agent_tools.use_agent_tool_context(ctx):
        listing = list_sessions.entrypoint(offset=2, limit=2)
        scans_after_listing = scans
        batch = session_cards.entrypoint(
            traj_ids="traj_page_00 traj_page_01",
        )

    assert "showing=2" in listing
    assert len(summarized) == 4
    assert scans_after_listing == 1
    assert scans == 2
    assert "batch=2" in batch


def test_invalid_json_fields_are_isolated_to_one_session(tmp_path: Path):
    ctx = _ctx(tmp_path)
    live = Path(ctx.default_traj_root)
    (live / "traj_bad_fields.json").write_text(
        json.dumps({
            "query": "bad but readable",
            "total_turns": "unknown",
            "tool_names": "Bash",
            "timeline": [{"role": "user", "content": "hello"}],
        }),
        encoding="utf-8",
    )

    with agent_tools.use_agent_tool_context(ctx):
        listing = list_sessions.entrypoint()

    bad_line = next(
        line for line in listing.splitlines() if line.startswith("traj_bad_fields")
    )
    assert "turns=1" in bad_line
    assert "tools=-" in bad_line
    assert "traj_cc_admin_aaa11111" in listing
