from __future__ import annotations

import json
from pathlib import Path

from xskill.agents import agent_tools
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
