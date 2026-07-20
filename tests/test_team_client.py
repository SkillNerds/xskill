from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from xskill.team.server import api as server_api
from xskill.team.client.daemon import TeamClient, register_with_server
from xskill.team.server.client_registry import ClientRegistry
from xskill.team.client.state import ClientState


def _git(args, cwd):
    subprocess.run(["git"] + args, cwd=str(cwd), capture_output=True, text=True, check=True)


def _make_skill(skill_dir: Path, name: str):
    d = skill_dir / name
    d.mkdir(parents=True)
    _git(["init", "-q"], d); _git(["checkout", "-q", "-b", "main"], d)
    _git(["config", "user.email", "t@t"], d); _git(["config", "user.name", "t"], d)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: d\n---\n# {name}\n", encoding="utf-8")
    _git(["add", "."], d); _git(["commit", "-q", "-m", "v1"], d)


@pytest.fixture
def server_app(tmp_path):
    skill_dir = tmp_path / "server_skill"
    skill_dir.mkdir()
    _make_skill(skill_dir, "fix-foo")
    reg = ClientRegistry(tmp_path / "clients.db")
    server_api.init_team_context(
        join_token="tok", client_registry=reg, skill_dir=skill_dir,
        traj_root=tmp_path / "team_traj", register_dir=lambda p, l: None,
    )
    app = FastAPI()
    app.include_router(server_api.router)
    return app, skill_dir


def test_register_with_server_returns_client_id(server_app):
    app, _ = server_app
    tc = TestClient(app)
    cid = register_with_server(tc, token="tok", label="alice", hostname="a")
    assert isinstance(cid, str) and cid


def _client(server_app, tmp_path) -> TeamClient:
    app, _ = server_app
    http = TestClient(app)
    cid = register_with_server(http, token="tok", label="alice", hostname="a")
    state = ClientState(server_url="http://testserver", client_id=cid, join_token="tok")
    return TeamClient(
        state=state, http=http,
        skill_dir=tmp_path / "client_home" / ".xskill" / "skill",
        cursor_path=tmp_path / "cursor.json",
        history_path=tmp_path / "history.jsonl",
        home_root=tmp_path / "client_home",
        # 这些用例不测上传频率拦截 → 关掉去抖,保持单轮即上传的旧契约
        min_change_interval=0,
    )


def test_sync_and_reconcile_materializes_skill(server_app, tmp_path):
    tc = _client(server_app, tmp_path)
    manifest = tc.sync()
    assert any(s.skill_name == "fix-foo" for s in manifest.slots)
    tc.reconcile_skill_sides(manifest)
    repo = tmp_path / "client_home" / ".xskill" / "skill" / "fix-foo"
    assert (repo / ".git").is_dir()
    assert (repo / "SKILL.md").read_text(encoding="utf-8").startswith("---")


def test_upload_sends_pending_trajectory(server_app, tmp_path):
    tc = _client(server_app, tmp_path)
    # 造一个静默够久的 traj，落在标准 bridge 目录 <home>/.xskill/cc_sessions/
    bridge = tmp_path / "client_home" / ".xskill" / "cc_sessions"
    bridge.mkdir(parents=True)
    md = bridge / "traj_cc_x_001.md"
    md.write_text("# body", encoding="utf-8")
    import os, time
    old = time.time() - 600
    os.utime(md, (old, old))
    n = tc.collect_and_upload()
    assert n == 1
    # server 端落盘检查
    expected = (tmp_path / "team_traj" / "clients" / tc.state.client_id
                / "sessions" / "traj_cc_x_001.md")
    assert expected.is_file()
    # 再跑一次不重传（游标生效）
    assert tc.collect_and_upload() == 0


def test_push_user_edits_skips_when_no_real_diff(server_app, tmp_path):
    """reconcile 后工作树跟 HEAD 一致就不该推：mtime 启发式会因为 git
    checkout 把 SKILL.md mtime 抬到 now 误判，但 git status --porcelain
    看的是内容差异，准。回归"reconcile 后每个 skill 每轮都被刷一次 commit
    尝试和警告日志"的 bug。"""
    import subprocess

    tc = _client(server_app, tmp_path)
    manifest = tc.sync()
    tc.reconcile_skill_sides(manifest)
    # 工作树跟 HEAD 一致（reconcile 刚 checkout），不该推任何东西
    pushed = tc.push_user_edits()
    assert pushed == 0
    # 也不该产生 _useredit 分支（说明早早被 git status --porcelain 门挡掉了）
    repo = tmp_path / "client_home" / ".xskill" / "skill" / "fix-foo"
    branches = subprocess.run(
        ["git", "-C", str(repo), "branch", "--list", "_useredit"],
        capture_output=True, text=True).stdout
    assert "_useredit" not in branches, (
        "push_user_edits 创建了 _useredit 分支——门没挡住，会刷屏")


def test_push_user_edits_stops_after_reverse_sync_failure(
    server_app, tmp_path, monkeypatch,
):
    from xskill.agents import user_edit_absorb_agent as user_absorb
    from xskill.team.client import daemon

    tc = _client(server_app, tmp_path)
    manifest = tc.sync()
    tc.reconcile_skill_sides(manifest)
    monkeypatch.setattr(
        user_absorb,
        "reverse_sync_openclaw_dest",
        lambda *_args, **_kwargs: user_absorb.ReverseSyncStatus.FAILED,
    )

    def fail_if_git_status_runs(*_args, **_kwargs):
        raise AssertionError("FAILED 后不得继续 git status/commit/upload")

    monkeypatch.setattr(daemon, "run_git", fail_if_git_status_runs)

    assert tc.push_user_edits() == 0


def test_cleanup_removes_skill_not_in_manifest(server_app, tmp_path):
    tc = _client(server_app, tmp_path)
    # 本地有个 manifest 里没有的 stale skill
    stale = tmp_path / "client_home" / ".xskill" / "skill" / "stale-skill"
    stale.mkdir(parents=True)
    (stale / "SKILL.md").write_text("# stale", encoding="utf-8")
    internal = stale.parent / ".repo_locks"
    internal.mkdir()
    manifest = tc.sync()
    tc.reconcile_skill_sides(manifest)
    tc.cleanup(manifest)
    assert not stale.exists()
    assert internal.is_dir()
    assert (tmp_path / "client_home" / ".xskill" / "skill" / "fix-foo").is_dir()   # manifest 里的保留


def test_cleanup_reaps_orphaned_ecosystem_links(server_app, tmp_path):
    """cleanup 按生态目录反向收孤儿 link:工作副本被 out-of-band 删除后残留、
    指向 xskill 工作副本根、且不在 manifest 的 dangling link 要收掉;第三方 link /
    手动真目录 / manifest 仍需要的 link 一律不碰。回归"~/.claude/skills 里 xskill
    link 只增不减、断链越积越多"。"""
    tc = _client(server_app, tmp_path)
    manifest = tc.sync()
    keep = {s.skill_name for s in manifest.slots}
    skill_root = tmp_path / "client_home" / ".xskill" / "skill"
    skill_root.mkdir(parents=True, exist_ok=True)
    cc = tmp_path / "client_home" / ".claude" / "skills"
    cc.mkdir(parents=True)

    # ① 孤儿:xskill link 指向已不存在的工作副本、名字不在 manifest → 该收
    orphan = cc / "gone-skill"
    orphan.symlink_to(skill_root / "gone-skill")           # target 不存在 = dangling
    # ② 第三方 link 指向 xskill 根之外 → 不该碰
    elsewhere = tmp_path / "third_party_src"
    elsewhere.mkdir()
    foreign = cc / "foreign-skill"
    foreign.symlink_to(elsewhere)
    # ③ 手动建的真目录 → 不该碰
    manual = cc / "manual-skill"
    manual.mkdir()
    (manual / "SKILL.md").write_text("# manual", encoding="utf-8")
    # ④ manifest 仍需要的 skill 的 dangling link → 留给 reconcile 重装,不该收
    wanted = cc / next(iter(keep))
    wanted.symlink_to(skill_root / next(iter(keep)))

    tc.cleanup(manifest)

    assert not orphan.is_symlink()   # 孤儿被收
    assert foreign.is_symlink()      # 第三方 link 保留
    assert manual.is_dir()           # 手动真目录保留
    assert wanted.is_symlink()       # keep 内的即便 dangling 也保留
