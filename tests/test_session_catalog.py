"""会话预览卡：目录不含正文；卡片有 query、path、L、截断 toolcall，没有 tool result。"""
from __future__ import annotations

from pathlib import Path

from xskill.agents import agent_tools, session_catalog


def _call(tool, *args, **kwargs):
    entrypoint = tool if callable(tool) else getattr(tool, "entrypoint")
    return entrypoint(*args, **kwargs)


def _bind(tmp_path: Path):
    sessions = tmp_path / "team_trajectories" / "clients" / "alice" / "sessions"
    sessions.mkdir(parents=True)
    long_cmd = "echo start " + ("y" * 40) + " UNIQUE_CMD_TAIL_SHOULD_TRUNCATE"
    (sessions / "traj_demo_one.md").write_text(
        "# Cursor Agent Trajectory\n"
        "\n"
        "## Initial Query\n"
        "\n"
        "<user_query>\n"
        "please check the invoice workflow\n"
        "</user_query>\n"
        "\n"
        "## Assistant\n"
        "I will look around.\n"
        f"[tool_use: Shell command={long_cmd}]\n"
        "\n"
        "## Tool Result\n"
        "UNIQUE_TOOL_RESULT_SHOULD_NOT_APPEAR\n"
        "FULL_BODY_PARAGRAPH_ONLY_IN_TRAJ\n",
        encoding="utf-8",
    )
    (sessions / "traj_demo_two.md").write_text(
        "## Initial Query\n\nfix the login form\n\n"
        "## Assistant\n[tool_use: Read path=/tmp/login.py]\n",
        encoding="utf-8",
    )
    skill_dir = tmp_path / "skill"
    skill_dir.mkdir()
    ctx = agent_tools.create_agent_tool_context(
        skill_dir=skill_dir,
        default_traj_root=tmp_path / "team_trajectories",
        extra_read_roots=(tmp_path / "team_trajectories",),
        generate_user_id="alice",
    )
    return ctx


def test_list_sessions_has_no_traj_body(tmp_path: Path):
    ctx = _bind(tmp_path)
    with agent_tools.use_agent_tool_context(ctx):
        listing = _call(session_catalog.list_sessions)
    assert "traj_demo_one" in listing
    assert "invoice workflow" in listing
    assert "FULL_BODY_PARAGRAPH_ONLY_IN_TRAJ" not in listing
    assert "UNIQUE_TOOL_RESULT_SHOULD_NOT_APPEAR" not in listing


def test_session_card_has_query_path_line_and_truncated_toolcall(tmp_path: Path):
    ctx = _bind(tmp_path)
    with agent_tools.use_agent_tool_context(ctx):
        card = _call(session_catalog.session_card, traj_id="traj_demo_one")
    assert "invoice workflow" in card
    assert "traj_demo_one.md" in card
    assert "path:" in card
    assert "L" in card
    assert "Shell" in card
    assert "echo start" in card
    assert "UNIQUE_CMD_TAIL_SHOULD_TRUNCATE" not in card
    assert "UNIQUE_TOOL_RESULT_SHOULD_NOT_APPEAR" not in card


def test_session_cards_max_ten(tmp_path: Path):
    ctx = _bind(tmp_path)
    ids = ",".join(f"traj_{i:02d}" for i in range(11))
    with agent_tools.use_agent_tool_context(ctx):
        err = _call(session_catalog.session_cards, traj_ids=ids)
        ok = _call(
            session_catalog.session_cards,
            traj_ids="traj_demo_one traj_demo_two",
        )
    assert err.startswith("error:")
    assert "10" in err
    assert "batch=2" in ok
    assert "traj_demo_one" in ok
    assert "traj_demo_two" in ok
