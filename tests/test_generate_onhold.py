"""on hold 轨迹：generate 提示词单列一行；list/grep/read 拦截；list 超长 spill。"""
from __future__ import annotations

from pathlib import Path

from xskill.agents import agent_tools
from xskill.agents.generate_agent import ONHOLD_PROMPT_LINE, SYSTEM_PROMPT
from xskill.team.server.client_registry import (
    ClientRegistry,
    list_paused_client_dir_names,
    paused_trajectory_roots,
)
from xskill.team.server.generate_jobs import exclude_blocked_read_roots


def test_onhold_prompt_is_its_own_line():
    lines = SYSTEM_PROMPT.splitlines()
    assert ONHOLD_PROMPT_LINE in lines
    assert lines.count(ONHOLD_PROMPT_LINE) == 1
    assert ONHOLD_PROMPT_LINE.strip() == ONHOLD_PROMPT_LINE


def _traj_ctx(tmp_path: Path, blocked: tuple[Path, ...], spill: bool = True):
    skill_dir = tmp_path / "skill"
    skill_dir.mkdir()
    traj_root = tmp_path / "team_trajectories"
    live = traj_root / "clients" / "alice" / "sessions"
    held = traj_root / "clients" / "bob" / "sessions"
    live.mkdir(parents=True)
    held.mkdir(parents=True)
    (live / "traj_ok.md").write_text("live evidence\n", encoding="utf-8")
    (held / "traj_secret.md").write_text("secret evidence\n", encoding="utf-8")
    spill_root = tmp_path / "spill" if spill else None
    if spill_root is not None:
        spill_root.mkdir()
    ctx = agent_tools.create_agent_tool_context(
        skill_dir=skill_dir,
        extra_read_roots=(traj_root,),
        blocked_read_roots=blocked or (traj_root / "clients" / "bob",),
        spill_root=spill_root,
    )
    return traj_root, live, held, ctx


def test_list_files_omits_onhold_client_dir(tmp_path):
    traj_root, live, held, ctx = _traj_ctx(tmp_path, ())
    with agent_tools.use_agent_tool_context(ctx):
        listing = agent_tools.list_files.entrypoint(str(traj_root / "clients"))
    assert "alice" in listing
    assert "bob" not in listing
    with agent_tools.use_agent_tool_context(ctx):
        blocked = agent_tools.list_files.entrypoint(str(held.parent))
    assert blocked.startswith("error: on hold 轨迹，不要参考")


def test_read_and_grep_block_onhold_trajs(tmp_path):
    traj_root, live, held, ctx = _traj_ctx(tmp_path, ())
    secret = held / "traj_secret.md"
    live_file = live / "traj_ok.md"
    with agent_tools.use_agent_tool_context(ctx):
        denied = agent_tools.read_file.entrypoint(str(secret))
        allowed = agent_tools.read_file.entrypoint(str(live_file))
        grep_all = agent_tools.grep_files.entrypoint("evidence", path=str(traj_root))
        grep_held = agent_tools.grep_files.entrypoint(
            "secret", path=str(held),
        )
    assert denied.startswith("error: on hold 轨迹，不要参考")
    assert "secret evidence" not in denied
    assert "live evidence" in allowed
    assert "traj_ok.md" in grep_all
    assert "traj_secret.md" not in grep_all
    assert grep_held.startswith("error: on hold 轨迹，不要参考")


def test_list_files_spills_long_listing(tmp_path):
    skill_dir = tmp_path / "skill"
    skill_dir.mkdir()
    fat = tmp_path / "fat"
    fat.mkdir()
    for index in range(220):
        (fat / f"file_{index:03d}.txt").write_text("x\n", encoding="utf-8")
    spill_root = tmp_path / "spill"
    spill_root.mkdir()
    ctx = agent_tools.create_agent_tool_context(
        skill_dir=skill_dir,
        extra_read_roots=(fat,),
        spill_root=spill_root,
    )
    with agent_tools.use_agent_tool_context(ctx):
        out = agent_tools.list_files.entrypoint(str(fat))
    assert "[list_files_spilled]" in out
    assert "read_file(spill_path, offset=1, limit=200)" in out
    assert "file_000.txt" not in out
    spill_line = [
        line for line in out.splitlines() if line.startswith("spill_path:")
    ][0]
    spill_path = Path(spill_line.split(":", 1)[1].strip())
    spilled = spill_path.read_text(encoding="utf-8")
    assert "file_000.txt" in spilled
    assert spilled.count("\n") + 1 >= 220
    with agent_tools.use_agent_tool_context(ctx):
        page = agent_tools.read_file.entrypoint(str(spill_path), offset=1, limit=20)
    assert "file_000.txt" in page


def test_paused_client_dirs_become_blocked_roots(tmp_path):
    db_path = tmp_path / "team_clients.db"
    registry = ClientRegistry(db_path)
    try:
        live_id = registry.register(user_name="alice")
        held_id = registry.register(user_name="bob")
        registry.set_ingest_paused(held_id, True, actor="boss")
        assert registry.is_ingest_paused(live_id) is False
    finally:
        registry.close()
    traj_root = tmp_path / "team_trajectories"
    (traj_root / "clients" / "alice").mkdir(parents=True)
    (traj_root / "clients" / "bob").mkdir(parents=True)
    names = list_paused_client_dir_names(db_path)
    assert names == ["bob"]
    blocked = paused_trajectory_roots(traj_root, db_path)
    assert any(path.name == "bob" for path in blocked)
    assert all(path.name != "alice" for path in blocked)
    roots = [
        traj_root,
        traj_root / "clients",
        traj_root / "clients" / "alice" / "sessions",
        traj_root / "clients" / "bob" / "sessions",
    ]
    kept = exclude_blocked_read_roots(roots, blocked)
    kept_names = {path.name for path in kept}
    assert "alice" in {path.parent.name for path in kept if path.name == "sessions"}
    assert "bob" not in {path.parent.name for path in kept if path.name == "sessions"}
    assert traj_root in kept
    assert (traj_root / "clients") in kept
    assert "sessions" in kept_names


def test_missing_clients_db_blocks_nothing(tmp_path):
    assert list_paused_client_dir_names(tmp_path / "nope.db") == []
    assert paused_trajectory_roots(tmp_path / "traj", tmp_path / "nope.db") == ()
