"""
test_openclaw_adapter.py -- OpenClaw 接入端到端单测
======================================================

覆盖：

* **T1 openclaw_trajectory_jsonl adapter** —— 从真实 trajectory.jsonl 抽
  sessionId / workspaceDir / agentId / provider / modelId / final_status /
  messagesSnapshot transcript。
* **T2 ingest_openclaw_sessions** —— tmp_path 模拟
  ``<home>/.openclaw/agents/<agent>/sessions/<sid>.trajectory.jsonl`` 布局，扫盘
  + 桥接，断言 traj_oc_*.md 落地、glob 排除 .jsonl.bak-* / .jsonl.reset.* /
  runtime .jsonl。
* **T3 install_to_openclaw** —— skill 落 ``<home>/.agents/skills/<name>``
  （与 codex / opencode 共享）。
* **T4 detect_known_ecosystems** —— ``<home>/.openclaw/agents/`` 存在即注册
  openclaw bridge。
* **T5 tool_use / tool_result content blocks** —— synthetic 数据验证 timeline
  + tool_calls 配对（本机 fixture 没有 tool 调用，用合成数据兜底）。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from dulwich import porcelain

from xskill.ecosystems import (
    adapt_trajectory,
    OPENCLAW_SPEC,
    JsonlIngester,
    _agents_skills_path,
    _openclaw_agents_path,
    _openclaw_session_id_from_path,
    _read_workspace_dir_from_openclaw_jsonl,
    detect_known_ecosystems,
    ingest_openclaw_sessions,
    install_to_openclaw,
)
from xskill.skill.frontmatter import serialize as fm_serialize


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "openclaw" / "sample_session.trajectory.jsonl"
EXPECTED_SESSION_ID = "11111111-2222-3333-4444-555555555555"
EXPECTED_WORKSPACE_DIR = "/tmp/openclaw-test-workspace"


@pytest.fixture
def fixture_content() -> str:
    assert FIXTURE_PATH.is_file(), f"fixture missing: {FIXTURE_PATH}"
    return FIXTURE_PATH.read_text(encoding="utf-8")


def _place_fixture_in_openclaw_home(home: Path, fixture_text: str, sid: str = EXPECTED_SESSION_ID) -> Path:
    sessions = home / ".openclaw" / "agents" / "main" / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    p = sessions / f"{sid}.trajectory.jsonl"
    p.write_text(fixture_text, encoding="utf-8")
    return p


def _build_skill(skill_root: Path, name: str = "demo-skill") -> Path:
    skill_path = skill_root / name
    skill_path.mkdir(parents=True, exist_ok=True)
    fm = {
        "name": name,
        "description": "A demo openclaw skill.",
        "version": 1,
        "source_trajs": ["traj_oc_0001"],
        "metadata": {"tags": ["demo"], "frozen": False},
    }
    (skill_path / "SKILL.md").write_text(
        fm_serialize(fm, f"# {name}\n\nBody.\n"), encoding="utf-8"
    )
    return skill_path


# ──────────────────────────────────────────────────────────────────
# T1. adapter
# ──────────────────────────────────────────────────────────────────


class TestOpenClawAdapter:
    def test_adapter_extracts_session_meta(self, fixture_content):
        md, meta = adapt_trajectory(fixture_content, "openclaw_trajectory_jsonl")
        assert md.startswith("# OpenClaw Session Trajectory")
        assert f"**session_id**: {EXPECTED_SESSION_ID}" in md
        assert f"**workspace_dir**: {EXPECTED_WORKSPACE_DIR}" in md
        assert "**provider**: deepseek" in md
        assert "**model_id**: deepseek-v4-pro" in md
        assert "**agent_id**: main" in md

    def test_adapter_meta_fields(self, fixture_content):
        _md, meta = adapt_trajectory(fixture_content, "openclaw_trajectory_jsonl")
        assert meta["session_id"] == EXPECTED_SESSION_ID
        assert meta["cwd"] == EXPECTED_WORKSPACE_DIR
        assert meta["workspace_dir"] == EXPECTED_WORKSPACE_DIR
        assert meta["agent_id"] == "main"
        assert meta["provider"] == "deepseek"
        assert meta["model_id"] == "deepseek-v4-pro"
        assert meta["source"] == "openclaw_trajectory_jsonl"
        assert meta["category"] == "openclaw_session"
        assert meta["final_status"] == "success"
        assert meta["total_turns"] > 0

    def test_adapter_handles_empty_input(self):
        md, meta = adapt_trajectory("", "openclaw_trajectory_jsonl")
        assert "# OpenClaw Session Trajectory" in md
        assert meta["total_turns"] == 0
        assert meta["total_tool_calls"] == 0
        assert meta["category"] == "openclaw_session"

    def test_adapter_skips_non_openclaw_lines(self):
        # 混入一行非 openclaw schema 的 json，不应被解析进 timeline
        bad = json.dumps({"traceSchema": "other-trajectory", "type": "noise"}) + "\n"
        bad += json.dumps({
            "traceSchema": "openclaw-trajectory",
            "type": "session.started",
            "sessionId": "sid-x",
            "workspaceDir": "/x",
            "provider": "deepseek",
            "modelId": "m",
            "data": {"agentId": "main"},
        }) + "\n"
        _md, meta = adapt_trajectory(bad, "openclaw_trajectory_jsonl")
        assert meta["session_id"] == "sid-x"

    def test_adapter_skips_malformed_lines(self):
        bad = "{not json\n" + json.dumps({
            "traceSchema": "openclaw-trajectory",
            "type": "session.started",
            "sessionId": "sid-y",
            "workspaceDir": "/y",
            "provider": "p",
            "modelId": "m",
            "data": {"agentId": "main"},
        }) + "\n"
        _md, meta = adapt_trajectory(bad, "openclaw_trajectory_jsonl")
        assert meta["session_id"] == "sid-y"


# ──────────────────────────────────────────────────────────────────
# T2. ingest
# ──────────────────────────────────────────────────────────────────


class TestIngestOpenClawSessions:
    def test_ingest_writes_traj_with_oc_prefix(self, tmp_path, fixture_content):
        _place_fixture_in_openclaw_home(tmp_path, fixture_content)
        traj_dir = tmp_path / "traj"

        results = ingest_openclaw_sessions(traj_dir, home_root=tmp_path)

        assert len(results) == 1
        rec = results[0]
        assert rec["status"] == "stored"
        assert rec["session_id"] == EXPECTED_SESSION_ID
        assert rec["ecosystem"] == "openclaw"
        assert rec["traj_id"].startswith("traj_oc_")

        md_path = Path(rec["path"])
        assert md_path.is_file()
        assert md_path.name.startswith("traj_oc_")

        meta_path = md_path.with_suffix(".json")
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        assert meta["session_id"] == EXPECTED_SESSION_ID
        assert meta["workspace_dir"] == EXPECTED_WORKSPACE_DIR
        assert meta["category"] == "openclaw_session"

    def test_ingest_idempotent_via_seen_sessions(self, tmp_path, fixture_content):
        _place_fixture_in_openclaw_home(tmp_path, fixture_content)
        traj_dir = tmp_path / "traj"
        seen: set[str] = set()

        first = ingest_openclaw_sessions(traj_dir, home_root=tmp_path, seen_sessions=seen)
        assert len(first) == 1
        assert EXPECTED_SESSION_ID in seen

        second = ingest_openclaw_sessions(traj_dir, home_root=tmp_path, seen_sessions=seen)
        assert second == []

    def test_ingest_skips_when_agents_dir_absent(self, tmp_path):
        results = ingest_openclaw_sessions(tmp_path / "traj", home_root=tmp_path)
        assert results == []

    def test_ingest_glob_excludes_runtime_and_backups(self, tmp_path, fixture_content):
        """同目录下还有 runtime <sid>.jsonl、<sid>.jsonl.bak-* 备份、
        <sid>.jsonl.reset.* reset 文件、<sid>.trajectory-path.json 指针；
        ingester 只能扫到 .trajectory.jsonl。
        """
        _place_fixture_in_openclaw_home(tmp_path, fixture_content)
        sessions = tmp_path / ".openclaw" / "agents" / "main" / "sessions"
        # 干扰文件
        (sessions / f"{EXPECTED_SESSION_ID}.jsonl").write_text('{"type":"noise"}\n', encoding="utf-8")
        (sessions / f"{EXPECTED_SESSION_ID}.jsonl.bak-1234-1700000000000").write_text(
            '{"type":"noise"}\n', encoding="utf-8")
        (sessions / f"{EXPECTED_SESSION_ID}.jsonl.reset.2026-05-09T09-16-47.019Z").write_text(
            '{"type":"noise"}\n', encoding="utf-8")
        (sessions / f"{EXPECTED_SESSION_ID}.trajectory-path.json").write_text(
            '{"pointer": true}\n', encoding="utf-8")

        results = ingest_openclaw_sessions(tmp_path / "traj", home_root=tmp_path)
        # 只有一份 .trajectory.jsonl 被扫到（其他 4 个干扰文件都被 glob 跳过）
        assert len(results) == 1
        assert results[0]["session_id"] == EXPECTED_SESSION_ID

    def test_ingest_finds_files_across_agent_dirs(self, tmp_path, fixture_content):
        """多个 agent（main / ops）下各放一个 session，都应被扫到。"""
        for agent, sid in [("main", "aaaaaaaa-bbbb-cccc-dddd-111111111111"),
                           ("ops", "bbbbbbbb-cccc-dddd-eeee-222222222222")]:
            sessions = tmp_path / ".openclaw" / "agents" / agent / "sessions"
            sessions.mkdir(parents=True)
            (sessions / f"{sid}.trajectory.jsonl").write_text(
                fixture_content.replace(EXPECTED_SESSION_ID, sid),
                encoding="utf-8",
            )

        results = ingest_openclaw_sessions(tmp_path / "traj", home_root=tmp_path)
        sids = sorted(r["session_id"] for r in results)
        assert sids == [
            "aaaaaaaa-bbbb-cccc-dddd-111111111111",
            "bbbbbbbb-cccc-dddd-eeee-222222222222",
        ]


# ──────────────────────────────────────────────────────────────────
# T3. install
# ──────────────────────────────────────────────────────────────────


class TestInstallToOpenClaw:
    def test_install_creates_skill_md_at_agents_skills_path(self, tmp_path):
        skill_path = _build_skill(tmp_path / "src")
        fake_home = tmp_path / "home"

        dest = install_to_openclaw(skill_path, target_root=fake_home)

        assert dest == fake_home / ".agents" / "skills" / "demo-skill" / "SKILL.md"
        assert dest.is_file()

        # 不能装到 .openclaw/skills（那是 managed tier，xskill 走 personal-agent）
        deprecated = fake_home / ".openclaw" / "skills"
        assert not deprecated.exists()

    def test_install_missing_skill_md_raises(self, tmp_path):
        empty = tmp_path / "empty-skill"
        empty.mkdir()
        with pytest.raises(FileNotFoundError):
            install_to_openclaw(empty, target_root=tmp_path / "home")


# ──────────────────────────────────────────────────────────────────
# 新增：copy 模式 / 回流 / canary flip 保护
# ──────────────────────────────────────────────────────────────────


class TestInstallToOpenClawCopyMode:
    """install_to_openclaw 必须用 copy 而不是 symlink（openclaw 拒收 escape-root
    的 symlink，详见 docs/ecosystem/openclaw-install-fix.md）。
    """

    def test_dest_is_real_dir_not_symlink(self, tmp_path):
        skill_path = _build_skill(tmp_path / "src")
        fake_home = tmp_path / "home"
        dest_md = install_to_openclaw(skill_path, target_root=fake_home)
        dest_dir = dest_md.parent
        assert dest_dir.is_dir()
        assert not dest_dir.is_symlink()

    def test_install_meta_file_landed(self, tmp_path):
        skill_path = _build_skill(tmp_path / "src")
        fake_home = tmp_path / "home"
        dest_md = install_to_openclaw(skill_path, target_root=fake_home)
        meta_path = dest_md.parent / ".xskill-install-meta.json"
        assert meta_path.is_file()
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        assert meta["side"] == "main"
        assert meta["ecosystem"] == "openclaw"
        assert "installed_at" in meta

    def test_dest_does_not_track_source_edits(self, tmp_path):
        """编辑源仓 SKILL.md 后 dest 不会自动变（确认是 copy 不是 symlink）。"""
        skill_path = _build_skill(tmp_path / "src")
        fake_home = tmp_path / "home"
        dest_md = install_to_openclaw(skill_path, target_root=fake_home)

        # 改源
        (skill_path / "SKILL.md").write_text("source changed\n")

        # dest 应不变
        assert "source changed" not in dest_md.read_text(encoding="utf-8")

    def test_install_ignores_dot_git_in_source(self, tmp_path):
        """源仓有 .git 时 install 不应把 .git 拷到 dest（dest 不是 git 仓）。"""
        skill_path = _build_skill(tmp_path / "src")
        repo = porcelain.init(str(skill_path), bare=False)
        try:
            porcelain.add(repo)
            porcelain.commit(
                repo,
                message=b"test source",
                author=b"test <test@example.com>",
                committer=b"test <test@example.com>",
            )
        finally:
            repo.close()
        fake_home = tmp_path / "home"
        dest_md = install_to_openclaw(skill_path, target_root=fake_home)
        assert not (dest_md.parent / ".git").exists()


class TestReverseSyncOpenClawDest:
    """dest 改了 → reverse_sync 灌回 source；没改 → no-op。"""

    def _build_real_skill_repo(self, root: Path) -> Path:
        """造一个有 .git 的 skill 源仓（reverse_sync 要 skill_repo_lock）。"""
        import subprocess
        sk = root / "src" / "demo-skill"
        sk.mkdir(parents=True)
        (sk / "SKILL.md").write_text(
            "---\nname: demo-skill\ndescription: A demo openclaw skill.\nversion: 1\n"
            "source_trajs:\n  - traj_oc_0001\nmetadata:\n  tags:\n    - demo\n  "
            "frozen: false\n---\n# demo-skill\n\nBody.\n",
        )
        subprocess.run(["git", "init", "-q", str(sk)], check=True)
        subprocess.run(["git", "-C", str(sk), "config", "user.email", "x@y.z"], check=True)
        subprocess.run(["git", "-C", str(sk), "config", "user.name", "x"], check=True)
        subprocess.run(["git", "-C", str(sk), "add", "."], check=True)
        subprocess.run(["git", "-C", str(sk), "commit", "-q", "-m", "init"], check=True)
        return sk

    def test_no_user_edit_returns_false(self, tmp_path):
        from xskill.agents.user_edit_absorb_agent import (
            ReverseSyncStatus,
            reverse_sync_openclaw_dest,
        )
        sk = self._build_real_skill_repo(tmp_path)
        home = tmp_path / "home"
        dest_md = install_to_openclaw(sk, target_root=home)
        # dest 刚装出来，没用户改 → reverse_sync False
        assert reverse_sync_openclaw_dest(
            dest_md.parent, sk, quiet_seconds=0,
        ) == ReverseSyncStatus.NO_EDIT

    def test_user_edit_in_dest_synced_back_to_source(self, tmp_path):
        import time as _t
        from xskill.agents.user_edit_absorb_agent import (
            ReverseSyncStatus,
            reverse_sync_openclaw_dest,
        )
        sk = self._build_real_skill_repo(tmp_path)
        home = tmp_path / "home"
        dest_md = install_to_openclaw(sk, target_root=home)
        dest_dir = dest_md.parent

        # 用户改 dest 的 SKILL.md + 加新文件
        _t.sleep(1.1)  # 让 mtime 比 installed_at 至少大 1 秒
        dest_md.write_text("USER MODIFIED IN DEST\n")
        (dest_dir / "scripts").mkdir()
        (dest_dir / "scripts" / "go.sh").write_text("#!/bin/sh\necho run\n")

        synced = reverse_sync_openclaw_dest(dest_dir, sk, quiet_seconds=0)
        assert synced == ReverseSyncStatus.SYNCED
        assert "USER MODIFIED IN DEST" in (sk / "SKILL.md").read_text()
        assert (sk / "scripts" / "go.sh").is_file()
        # install-meta 不应被灌回源仓
        assert not (sk / ".xskill-install-meta.json").is_file()

    def test_install_meta_skipped_during_sync(self, tmp_path):
        """install-meta 不算用户改，本身的 mtime 不应触发回流。"""
        from xskill.agents.user_edit_absorb_agent import (
            ReverseSyncStatus,
            has_pending_dest_edit,
            reverse_sync_openclaw_dest,
        )
        sk = self._build_real_skill_repo(tmp_path)
        home = tmp_path / "home"
        dest_md = install_to_openclaw(sk, target_root=home)
        # 改 install-meta（模拟下一次 install 重写它）
        import time as _t
        _t.sleep(1.1)
        meta = dest_md.parent / ".xskill-install-meta.json"
        meta.write_text(meta.read_text() + "\n")  # bump mtime
        # has_pending_dest_edit 只看用户文件，不看 install-meta → False
        assert has_pending_dest_edit(dest_md.parent, quiet_seconds=0) is False
        assert reverse_sync_openclaw_dest(
            dest_md.parent, sk, quiet_seconds=0,
        ) == ReverseSyncStatus.NO_EDIT


class TestInstallToOpenClawProtectsPendingDestEdits:
    """install_to_openclaw 在 copytree 前自动跑 reverse_sync 保护——canary
    flip 时如果 dest 有用户改没回流，先回流再覆盖，用户改不丢。
    """

    def _build_real_skill_repo(self, root: Path) -> Path:
        import subprocess
        sk = root / "src" / "demo-skill"
        sk.mkdir(parents=True)
        (sk / "SKILL.md").write_text(
            "---\nname: demo-skill\ndescription: A demo openclaw skill.\nversion: 1\n"
            "source_trajs:\n  - traj_oc_0001\nmetadata:\n  tags:\n    - demo\n  "
            "frozen: false\n---\n# demo-skill\n\nBody v1.\n",
        )
        subprocess.run(["git", "init", "-q", str(sk)], check=True)
        subprocess.run(["git", "-C", str(sk), "config", "user.email", "x@y.z"], check=True)
        subprocess.run(["git", "-C", str(sk), "config", "user.name", "x"], check=True)
        subprocess.run(["git", "-C", str(sk), "add", "."], check=True)
        subprocess.run(["git", "-C", str(sk), "commit", "-q", "-m", "init"], check=True)
        return sk

    def test_pending_dest_edit_synced_back_before_reinstall(self, tmp_path, monkeypatch):
        """user 改 dest 后 → install_to_openclaw 再跑（模拟 canary flip） → dest
        被覆盖但 source 已经吸收了用户改。
        """
        import time as _t
        sk = self._build_real_skill_repo(tmp_path)
        home = tmp_path / "home"
        dest_md = install_to_openclaw(sk, target_root=home)

        # 让 reverse_sync 跳过静默检查（测试用，免等 3 分钟）
        from xskill.agents.user_edit_absorb_agent import reverse_sync_openclaw_dest as _orig
        monkeypatch.setattr(
            "xskill.agents.user_edit_absorb_agent.reverse_sync_openclaw_dest",
            lambda d, s, **k: _orig(d, s, quiet_seconds=0),
        )

        # user 改 dest
        _t.sleep(1.1)
        dest_md.write_text("DEST USER EDIT\n")

        # 重 install（模拟 canary 切版本）
        install_to_openclaw(sk, target_root=home)

        # source 已经吸收了 user 改
        assert "DEST USER EDIT" in (sk / "SKILL.md").read_text()
        # dest 被覆盖回 source 的当前内容（含 user 改，因为 source 已经被同步）
        new_dest = home / ".agents" / "skills" / "demo-skill" / "SKILL.md"
        assert "DEST USER EDIT" in new_dest.read_text()


class TestInstallToOpenClawJunctionRegression:
    """issue #35 回归锚点：dest 是 symlink/junction 时 install_to_openclaw
    不应抛 ``OSError: Cannot call rmtree on a symbolic link``，而是 unlink
    旧 link 后 copytree 新内容。

    Linux 上 ``Path.is_symlink()`` 对 symlink 正确返回 True，rmtree 也会
    自己保护；问题完整复现在 Windows non-DevMode 上（``Path.is_symlink()``
    对 junction 返回 False）。本类只测 POSIX 行为，``_is_link_or_junction``
    的 Windows 分支由 ``test_install_fallback_revsync`` 的纯 mock 测覆盖。
    """

    def _build_real_skill_repo(self, root: Path) -> Path:
        import subprocess
        sk = root / "src" / "demo-skill"
        sk.mkdir(parents=True)
        (sk / "SKILL.md").write_text(
            "---\nname: demo-skill\ndescription: A demo openclaw skill.\nversion: 1\n"
            "source_trajs:\n  - traj_oc_0001\nmetadata:\n  tags:\n    - demo\n  "
            "frozen: false\n---\n# demo-skill\n\nBody v1.\n",
        )
        subprocess.run(["git", "init", "-q", str(sk)], check=True)
        subprocess.run(["git", "-C", str(sk), "config", "user.email", "x@y.z"], check=True)
        subprocess.run(["git", "-C", str(sk), "config", "user.name", "x"], check=True)
        subprocess.run(["git", "-C", str(sk), "add", "."], check=True)
        subprocess.run(["git", "-C", str(sk), "commit", "-q", "-m", "init"], check=True)
        return sk

    def test_install_to_openclaw_replaces_existing_symlink_safely(self, tmp_path):
        """模拟 codex/opencode 先在 ``~/.agents/skills/<name>`` 装了 symlink
        （Linux 上 ``install_dir`` 默认走 symlink），再让 openclaw 跑：
        旧 symlink 应被 unlink，新 copy 顺利落地，不抛 OSError。
        """
        sk = self._build_real_skill_repo(tmp_path)
        home = tmp_path / "home"
        skills_root = home / ".agents" / "skills"
        skills_root.mkdir(parents=True)
        # 模拟 codex 先装了一个指向 sk 的 symlink
        existing_link = skills_root / "demo-skill"
        existing_link.symlink_to(sk, target_is_directory=True)
        assert existing_link.is_symlink()

        # openclaw 现在跑——不应抛 OSError
        from xskill.ecosystems import install_to_openclaw
        dest_md = install_to_openclaw(sk, target_root=home)

        # 旧 link 已被替换为真目录
        assert dest_md.is_file()
        dest_dir = home / ".agents" / "skills" / "demo-skill"
        assert dest_dir.is_dir()
        assert not dest_dir.is_symlink()


# ──────────────────────────────────────────────────────────────────
# T4. detect
# ──────────────────────────────────────────────────────────────────


class TestDetectOpenClaw:
    def test_detect_reports_openclaw_when_agents_dir_exists(self, tmp_path):
        (tmp_path / ".openclaw" / "agents").mkdir(parents=True)

        found = detect_known_ecosystems(home_root=tmp_path)
        ecos = {r["ecosystem"] for r in found}
        assert "openclaw" in ecos

        rec = next(r for r in found if r["ecosystem"] == "openclaw")
        assert rec["source"] == (tmp_path / ".openclaw" / "agents").resolve()
        assert rec["bridge"] == (tmp_path / ".xskill" / "openclaw_sessions").resolve()

    def test_detect_skips_openclaw_when_dir_absent(self, tmp_path):
        found = detect_known_ecosystems(home_root=tmp_path)
        ecos = {r["ecosystem"] for r in found}
        assert "openclaw" not in ecos


# ──────────────────────────────────────────────────────────────────
# T5. tool_use / tool_result —— synthetic content blocks
# ──────────────────────────────────────────────────────────────────


def _synthetic_trajectory_with_tool_call() -> str:
    """合成最小 trajectory：session.started + 一条带 tool_use+tool_result 的
    model.completed + trace.artifacts + session.ended。

    本机真 fixture 没有 tool 调用，这条用来覆盖 tool_use/tool_result 分支
    （schema 假设按 Anthropic 风格）。
    """
    sid = "tool-test-aaaaaaaa-bbbb-cccc"
    events = [
        {
            "traceSchema": "openclaw-trajectory",
            "type": "session.started",
            "sessionId": sid,
            "workspaceDir": "/tmp/oc-tools",
            "provider": "deepseek",
            "modelId": "deepseek-v4-pro",
            "data": {"agentId": "main", "messageProvider": "cli"},
        },
        {
            "traceSchema": "openclaw-trajectory",
            "type": "model.completed",
            "sessionId": sid,
            "data": {
                "messagesSnapshot": [
                    {"role": "user", "content": [{"type": "text", "text": "list /etc"}]},
                    {
                        "role": "assistant",
                        "content": [
                            {"type": "text", "text": "Running exec…"},
                            {"type": "tool_use", "id": "tu-1", "name": "exec",
                             "input": {"command": "ls /etc | head -5"}},
                        ],
                    },
                    {
                        "role": "user",
                        "content": [
                            {"type": "tool_result", "tool_use_id": "tu-1",
                             "content": [{"type": "text", "text": "adduser.conf\naliases\n"}]},
                        ],
                    },
                    {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "Found these files."}],
                    },
                ],
            },
        },
        {
            "traceSchema": "openclaw-trajectory",
            "type": "trace.artifacts",
            "data": {"finalStatus": "success"},
        },
        {"traceSchema": "openclaw-trajectory", "type": "session.ended", "data": {"status": "ok"}},
    ]
    return "\n".join(json.dumps(e) for e in events) + "\n"


class TestToolBlockParsing:
    def test_tool_use_and_result_pair_into_timeline(self):
        content = _synthetic_trajectory_with_tool_call()
        md, meta = adapt_trajectory(content, "openclaw_trajectory_jsonl")

        assert meta["total_tool_calls"] == 1
        assert "exec" in meta["tool_names"]

        tcs = meta["tool_calls"]
        assert len(tcs) == 1
        assert tcs[0]["tool"] == "exec"
        assert tcs[0]["input"] == {"command": "ls /etc | head -5"}
        assert tcs[0]["output_available"] is True
        assert "adduser.conf" in tcs[0]["output"]

        # markdown 里出现 tool call + output 章节
        assert "## Tool Call: exec" in md
        assert "## Tool Output: exec" in md
        assert "adduser.conf" in md

    def test_assistant_text_around_tool_call_preserved(self):
        content = _synthetic_trajectory_with_tool_call()
        md, meta = adapt_trajectory(content, "openclaw_trajectory_jsonl")
        # 第一条 assistant text + 工具调用后那条 assistant text 都在
        assert "Running exec" in md
        assert "Found these files." in md


# ──────────────────────────────────────────────────────────────────
# T6. helpers
# ──────────────────────────────────────────────────────────────────


class TestOpenClawHelpers:
    def test_session_id_from_path(self, tmp_path):
        p = tmp_path / f"{EXPECTED_SESSION_ID}.trajectory.jsonl"
        assert _openclaw_session_id_from_path(p) == EXPECTED_SESSION_ID

    def test_read_workspace_dir_top_level(self):
        line = json.dumps({
            "traceSchema": "openclaw-trajectory",
            "type": "session.started",
            "workspaceDir": "/tmp/ws-top",
            "data": {},
        })
        assert _read_workspace_dir_from_openclaw_jsonl(line) == "/tmp/ws-top"

    def test_read_workspace_dir_falls_back_to_data(self):
        line = json.dumps({
            "traceSchema": "openclaw-trajectory",
            "type": "session.started",
            "data": {"workspaceDir": "/tmp/ws-data"},
        })
        assert _read_workspace_dir_from_openclaw_jsonl(line) == "/tmp/ws-data"

    def test_read_workspace_dir_returns_empty_when_missing(self):
        line = json.dumps({"traceSchema": "openclaw-trajectory", "type": "x", "data": {}})
        assert _read_workspace_dir_from_openclaw_jsonl(line) == ""

    def test_path_resolvers(self):
        home = Path("/fake/home")
        assert _openclaw_agents_path(home) == home / ".openclaw" / "agents"
        # OpenClaw 跟 codex / opencode 共用 .agents/skills 安装目录
        assert OPENCLAW_SPEC.skills_install_path(home) == home / ".agents" / "skills"
        assert OPENCLAW_SPEC.skills_install_path(home) == _agents_skills_path(home)

    def test_spec_fields(self):
        assert OPENCLAW_SPEC.name == "openclaw"
        assert OPENCLAW_SPEC.source_kind == "jsonl"
        assert OPENCLAW_SPEC.adapter_format == "openclaw_trajectory_jsonl"
        assert OPENCLAW_SPEC.traj_id_prefix == "traj_oc_"
        assert OPENCLAW_SPEC.sessions_glob == "*/sessions/*.trajectory.jsonl"


class TestJsonlIngesterDoesNotConfuseOpenClawWithCodex:
    """OPENCLAW_SPEC ingester 不应误扫 codex 路径，反之亦然。"""

    def test_openclaw_ingester_ignores_codex_files(self, tmp_path):
        d = tmp_path / ".codex" / "sessions" / "2026" / "01" / "15"
        d.mkdir(parents=True)
        (d / "rollout-2026-01-15T10-00-00-aaaa-bbbb-cccc-dddd-eeeeeeeeeeee.jsonl").write_text(
            '{"type":"session_meta"}\n', encoding="utf-8")
        results = JsonlIngester(OPENCLAW_SPEC).scan_and_bridge(tmp_path / "traj", home_root=tmp_path)
        assert results == []
