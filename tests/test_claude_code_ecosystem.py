"""
test_claude_code_ecosystem.py -- Claude Code 生态端到端测试
=========================================================
覆盖：
  T1. CC JSONL → adapter：~/.claude/projects/<hash>/<session>.jsonl 格式可被读取
  T2. adapter 输出 → submit_trajectory：写盘 + Trajectory 读回
  T3. install_to_claude_code：SKILL.md 落到 ~/.claude/skills/<name>/，可被 CC 发现
  T4. 灰度加载：staging 分支 + pick_side 确定性路由 + ux 分数 + check_and_decide
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from xskill import canary
from xskill.ecosystems import adapt_trajectory, submit_trajectory
from xskill.ecosystems import install_all_to_claude_code, install_to_claude_code
from xskill.ecosystems.claude_code import _session_content_used_skill
from xskill.pipeline.trajectory import Trajectory
from xskill.skill.frontmatter import parse as fm_parse
from xskill.skill.frontmatter import serialize as fm_serialize
from xskill.skill.git import run_git


# ──────────────────────────────────────────────────────
# Fixture: a minimal-but-realistic Claude Code session JSONL
# ──────────────────────────────────────────────────────

CC_JSONL_SAMPLE = "\n".join([
    # 1. permission-mode header (should be skipped)
    json.dumps({"type": "permission-mode", "permissionMode": "bypassPermissions",
                "sessionId": "sess-abc"}),
    # 2. file-history-snapshot (should be skipped)
    json.dumps({"type": "file-history-snapshot", "messageId": "x", "snapshot": {}}),
    # 3. user text turn
    json.dumps({
        "type": "user",
        "message": {"role": "user", "content": "列出当前目录下所有 python 文件"},
        "sessionId": "sess-abc", "cwd": "/home/me/proj", "gitBranch": "main",
    }),
    # 4. assistant turn with text + tool_use
    json.dumps({
        "type": "assistant",
        "message": {"role": "assistant", "content": [
            {"type": "text", "text": "好的，我用 Bash 工具列一下。"},
            {"type": "tool_use", "id": "tu_01", "name": "Bash",
             "input": {"command": "ls *.py", "description": "list py files"}},
        ]},
        "sessionId": "sess-abc",
    }),
    # 5. user turn containing tool_result
    json.dumps({
        "type": "user",
        "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "tu_01",
             "content": "main.py\nutils.py\n", "is_error": False},
        ]},
        "sessionId": "sess-abc",
    }),
    # 6. final assistant text
    json.dumps({
        "type": "assistant",
        "message": {"role": "assistant", "content": [
            {"type": "text", "text": "找到 2 个文件：main.py 和 utils.py。"},
        ]},
        "sessionId": "sess-abc",
    }),
])


def test_session_used_skill_scans_past_unrelated_assistant_turns():
    """前一条 assistant 的 content 为列表时，后续 Skill 调用仍能被识别。"""
    content = "\n".join([
        json.dumps({
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "先检查环境。"}],
            },
        }),
        json.dumps({
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{
                    "type": "tool_use",
                    "name": "Skill",
                    "input": {"skill": "list-py-files"},
                }],
            },
        }),
    ])

    assert _session_content_used_skill(content, "list-py-files") is True


# CC session where every user turn ships content as a LIST of {"type":"text"}
# parts (real CC format for typed input — not a bare string). Reproduces the
# traj_cc_pyproject_ses-alic case that was wrongly filtered as no_user_intent.
CC_JSONL_LIST_TEXT_USER = "\n".join([
    json.dumps({"type": "permission-mode", "permissionMode": "default",
                "sessionId": "ses-alic"}),
    json.dumps({
        "type": "user",
        "message": {"role": "user", "content": [
            {"type": "text", "text": "解释一下 Python 的列表推导式"},
        ]},
        "sessionId": "ses-alic", "cwd": "/home/alic/pyproject",
        "gitBranch": "main",
    }),
    json.dumps({
        "type": "assistant",
        "message": {"role": "assistant", "content": [
            {"type": "text", "text": "列表推导式形如 [x*2 for x in range(10)]。"},
        ]},
        "sessionId": "ses-alic",
    }),
    json.dumps({
        "type": "user",
        "message": {"role": "user", "content": [
            {"type": "text", "text": "Flask 里 *args 和 **kwargs 怎么用？"},
        ]},
        "sessionId": "ses-alic",
    }),
    json.dumps({
        "type": "assistant",
        "message": {"role": "assistant", "content": [
            {"type": "text", "text": "*args 收位置参数，**kwargs 收关键字参数。"},
        ]},
        "sessionId": "ses-alic",
    }),
    json.dumps({
        "type": "user",
        "message": {"role": "user", "content": [
            {"type": "text", "text": "写个递归求阶乘，再用装饰器计时。"},
        ]},
        "sessionId": "ses-alic",
    }),
    json.dumps({
        "type": "assistant",
        "message": {"role": "assistant", "content": [
            {"type": "text", "text": "def fact(n): return 1 if n<2 else n*fact(n-1)"},
        ]},
        "sessionId": "ses-alic",
    }),
])


# ──────────────────────────────────────────────────────
# T1. CC JSONL → adapter
# ──────────────────────────────────────────────────────

class TestClaudeCodeJsonlAdapter:
    """Claude Code 原生 JSONL 是否能被 xskill 正确读取并转标准格式。"""

    def test_adapter_accepts_claude_code_jsonl_format(self):
        md, _meta = adapt_trajectory(CC_JSONL_SAMPLE, "claude_code_jsonl")
        assert "# Claude Code Session Trajectory" in md
        assert "## User" in md
        assert "## Assistant" in md
        assert "Tool Call: Bash" in md
        assert "Tool Output: Bash" in md
        assert "main.py" in md  # 工具输出原文进入 markdown

    def test_metadata_captures_session_and_timeline(self):
        _md, meta = adapt_trajectory(CC_JSONL_SAMPLE, "claude_code_jsonl")
        assert meta["session_id"] == "sess-abc"
        assert meta["cwd"] == "/home/me/proj"
        assert meta["git_branch"] == "main"
        assert meta["category"] == "claude_code_session"
        assert meta["source"] == "claude_code_session_jsonl"
        assert meta["total_tool_calls"] == 1
        assert meta["tool_names"] == ["Bash"]

        roles = [e["role"] for e in meta["timeline"]]
        assert roles == ["user", "assistant", "tool_call", "tool_output", "assistant"]

        tc = meta["tool_calls"][0]
        assert tc["tool"] == "Bash"
        assert tc["output_available"] is True
        assert "main.py" in tc["output"]

    def test_unknown_event_types_are_skipped(self):
        # permission-mode + file-history-snapshot 不能污染 timeline
        _md, meta = adapt_trajectory(CC_JSONL_SAMPLE, "claude_code_jsonl")
        assert all(
            e["role"] in ("user", "assistant", "tool_call", "tool_output")
            for e in meta["timeline"]
        )

    def test_empty_input_yields_minimal_metadata(self):
        _md, meta = adapt_trajectory("", "claude_code_jsonl")
        assert meta["total_turns"] == 0
        assert meta["total_tool_calls"] == 0
        assert meta["tool_names"] == []
        assert meta["category"] == "claude_code_session"

    def test_unsupported_format_raises(self):
        with pytest.raises(ValueError):
            adapt_trajectory("{}", "claude_code_yaml")

    def test_real_cc_session_file_is_parseable(self, tmp_path):
        """若机器上有真实 CC 会话文件，跑一遍验证 schema 与实现一致 (best-effort)。"""
        candidates = list(Path.home().glob(".claude/projects/*/*.jsonl"))
        if not candidates:
            pytest.skip("no real Claude Code session files on this machine")
        sample = candidates[0]
        content = sample.read_text(encoding="utf-8", errors="ignore")
        md, meta = adapt_trajectory(content, "claude_code_jsonl")
        assert meta["category"] == "claude_code_session"
        # 至少抽到一个 sessionId 或一些 timeline 事件之一
        assert meta.get("session_id") or meta["total_turns"] > 0

    def test_zero_settle_waits_for_last_prompt_before_seen(self, tmp_path):
        """低延迟扫描不应把仍在续写的 session 永久标记为已处理。"""
        from xskill.ecosystems._shared import JsonlIngester
        from xskill.ecosystems.claude_code import CC_SPEC

        home_root = tmp_path / "home"
        session_dir = home_root / ".claude" / "projects" / "project"
        session_dir.mkdir(parents=True)
        session_path = session_dir / "session-live.jsonl"
        session_path.write_text(CC_JSONL_SAMPLE, encoding="utf-8")
        target_dir = tmp_path / "bridged"
        seen_sessions: set[str] = set()
        ingester = JsonlIngester(CC_SPEC, settle_seconds=0)

        first_result = ingester.scan_and_bridge(
            target_dir,
            home_root=home_root,
            seen_sessions=seen_sessions,
        )

        assert first_result == []
        assert seen_sessions == set()

        with session_path.open("a", encoding="utf-8") as session_file:
            session_file.write("\n")
            session_file.write(json.dumps({
                "type": "last-prompt",
                "sessionId": "session-live",
            }))
            session_file.write("\n")
        second_result = ingester.scan_and_bridge(
            target_dir,
            home_root=home_root,
            seen_sessions=seen_sessions,
        )

        assert len(second_result) == 1
        assert seen_sessions == {"session-live"}


# ──────────────────────────────────────────────────────
# T1b. CC JSONL with list-form user text (no_user_intent regression)
# ──────────────────────────────────────────────────────

class TestClaudeCodeListTextUserContent:
    """CC 把用户键入的文本包成 ``content: [{"type":"text","text":...}]``（list
    形式，而非裸字符串）。adapter 必须抽出这些 text 作为用户意图，否则桥接出的
    traj_*.md 没有 ``## User`` 段，会被 ``validate_trajectory_source`` 误判为
    ``no_user_intent`` 在 TaskAgent 拆分前过滤掉（traj_cc_pyproject_ses-alic 案）。
    """

    def test_adapter_extracts_user_text_from_list_content(self):
        md, meta = adapt_trajectory(CC_JSONL_LIST_TEXT_USER, "claude_code_jsonl")
        user_turns = [e for e in meta["timeline"] if e["role"] == "user"]
        assert len(user_turns) == 3
        assert "列表推导式" in user_turns[0]["content"]
        assert "Flask" in user_turns[1]["content"]
        assert "递归" in user_turns[2]["content"]
        # markdown 含 ## User 段 + ## Initial Query 预览
        assert "## User" in md
        assert "## Initial Query" in md

    def test_bridged_list_text_traj_not_filtered_as_no_user_intent(self, tmp_path):
        from xskill.pipeline.trajectory import validate_trajectory_source

        md, _meta = adapt_trajectory(CC_JSONL_LIST_TEXT_USER, "claude_code_jsonl")
        traj_path = tmp_path / "traj_cc_pyproject_ses-alic.md"
        traj_path.write_text(md, encoding="utf-8")

        v = validate_trajectory_source(traj_path)
        assert v.valid, f"filtered: reason={v.reason} detail={v.detail}"
        assert v.reason != "no_user_intent"
        assert v.user_intent_count >= 1

    def test_list_with_mixed_text_and_tool_result(self):
        """同一 user 消息里既有 text 又有 tool_result 时两者都要保留。"""
        sample = "\n".join([
            json.dumps({
                "type": "assistant",
                "message": {"role": "assistant", "content": [
                    {"type": "tool_use", "id": "tu_1", "name": "Bash",
                     "input": {"command": "echo hi"}},
                ]},
                "sessionId": "s",
            }),
            json.dumps({
                "type": "user",
                "message": {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": "tu_1",
                     "content": "hi", "is_error": False},
                    {"type": "text", "text": "现在改成输出 bye"},
                ]},
                "sessionId": "s",
            }),
        ])
        _md, meta = adapt_trajectory(sample, "claude_code_jsonl")
        roles = [e["role"] for e in meta["timeline"]]
        assert "tool_output" in roles
        assert "user" in roles
        user_text = [e["content"] for e in meta["timeline"] if e["role"] == "user"]
        assert any("bye" in c for c in user_text)


# ──────────────────────────────────────────────────────
# T2. adapter 输出 → submit_trajectory → Trajectory.load
# ──────────────────────────────────────────────────────

class TestSubmitClaudeCodeTrajectory:
    """adapter 产物可直接走 submit_trajectory 持久化，并能被 Trajectory 读回。"""

    def test_submit_writes_md_and_json(self, tmp_path):
        result = submit_trajectory(
            content=CC_JSONL_SAMPLE,
            format="claude_code_jsonl",
            traj_dir=tmp_path,
        )
        assert result["status"] == "stored"
        md_path = Path(result["path"])
        assert md_path.exists()
        json_path = md_path.with_suffix(".json")
        assert json_path.exists()

        meta = json.loads(json_path.read_text(encoding="utf-8"))
        assert meta["category"] == "claude_code_session"
        assert meta["session_id"] == "sess-abc"

    def test_submitted_trajectory_loadable_as_entity(self, tmp_path):
        result = submit_trajectory(
            content=CC_JSONL_SAMPLE,
            format="claude_code_jsonl",
            traj_dir=tmp_path,
        )
        t = Trajectory.load(result["path"])
        assert t.md_text.startswith("# Claude Code Session Trajectory")
        raw = t.raw_json
        assert raw["session_id"] == "sess-abc"
        assert raw["total_tool_calls"] == 1


# ──────────────────────────────────────────────────────
# Helpers for T3 / T4
# ──────────────────────────────────────────────────────

VALID_FRONTMATTER = {
    "name": "list-py-files",
    "description": "List Python files in a directory using `ls *.py`.",
    "version": 1,
    "source_trajs": ["traj_0001", "traj_0002"],
    "metadata": {"tags": ["bash", "listing"], "frozen": False},
}

SKILL_BODY = """# list-py-files

## Context
当用户要求列出 Python 文件时使用 Bash 工具的 `ls *.py`。

## Stage: 执行
```bash
ls *.py
```
"""


def _build_skill(skill_root: Path, name: str = "list-py-files") -> Path:
    """在 skill_root 下建一个最小的 skill 目录（含合规 SKILL.md）。"""
    skill_path = skill_root / name
    skill_path.mkdir(parents=True, exist_ok=True)
    fm = dict(VALID_FRONTMATTER)
    fm["name"] = name
    (skill_path / "SKILL.md").write_text(
        fm_serialize(fm, SKILL_BODY), encoding="utf-8"
    )
    return skill_path


# ──────────────────────────────────────────────────────
# T3. install_to_claude_code
# ──────────────────────────────────────────────────────

class TestInstallToClaudeCode:
    """蒸馏出的 skill 落到 ~/.claude/skills/<name>/ 即可被 Claude Code 加载。"""

    def test_install_creates_destination_with_skill_md(self, tmp_path):
        skill_path = _build_skill(tmp_path / "src_skills")
        fake_home = tmp_path / "fake_home"

        dest = install_to_claude_code(skill_path, target_root=fake_home)

        assert dest == fake_home / ".claude" / "skills" / "list-py-files" / "SKILL.md"
        assert dest.exists()
        # frontmatter 合规：name + description 是 CC discovery 的核心字段
        fm, body = fm_parse(dest.read_text(encoding="utf-8"))
        assert fm["name"] == "list-py-files"
        assert fm.get("description")
        assert body.startswith("# list-py-files")

    def test_install_overwrites_existing(self, tmp_path):
        skill_path = _build_skill(tmp_path / "src_skills")
        fake_home = tmp_path / "fake_home"
        first = install_to_claude_code(skill_path, target_root=fake_home)

        # source 更新版本
        new_fm = dict(VALID_FRONTMATTER)
        new_fm["version"] = 2
        (skill_path / "SKILL.md").write_text(
            fm_serialize(new_fm, SKILL_BODY + "\n更新过的内容。\n"),
            encoding="utf-8",
        )
        second = install_to_claude_code(skill_path, target_root=fake_home)

        assert second == first
        text = second.read_text(encoding="utf-8")
        assert "更新过的内容" in text
        fm, _ = fm_parse(text)
        assert fm["version"] == 2

    def test_install_creates_directory_symlink_not_copy(self, tmp_path):
        """install 应创建目录级 symlink → xskill 更新即时可见 + 不冲掉用户手改。"""
        skill_path = _build_skill(tmp_path / "src_skills")
        fake_home = tmp_path / "fake_home"
        install_to_claude_code(skill_path, target_root=fake_home)
        dest_dir = fake_home / ".claude" / "skills" / "list-py-files"
        assert dest_dir.is_symlink(), "dest 必须是 symlink 而非真实目录"
        assert dest_dir.resolve() == skill_path.resolve()
        # 改源 → dest 立刻看到（symlink 语义）
        (skill_path / "scripts").mkdir(exist_ok=True)
        (skill_path / "scripts" / "new_script.sh").write_text("#!/bin/bash\necho hi\n", encoding="utf-8")
        assert (dest_dir / "scripts" / "new_script.sh").is_file()

    def test_install_replaces_old_directory_copy_with_symlink(self, tmp_path):
        """旧 install 留下的真实目录（非 symlink）→ 重装时备份并替换为 symlink。"""
        skill_path = _build_skill(tmp_path / "src_skills")
        fake_home = tmp_path / "fake_home"
        # 模拟旧 copy install 留下的真实目录
        legacy_dest = fake_home / ".claude" / "skills" / "list-py-files"
        legacy_dest.mkdir(parents=True)
        (legacy_dest / "SKILL.md").write_text("legacy copy", encoding="utf-8")
        install_to_claude_code(skill_path, target_root=fake_home)
        assert legacy_dest.is_symlink()
        # 旧目录被搬到隐藏备份
        backup = fake_home / ".claude" / "skills" / ".list-py-files.replaced-by-symlink"
        assert backup.is_dir()
        assert (backup / "SKILL.md").read_text(encoding="utf-8") == "legacy copy"

    def test_install_missing_skill_md_raises(self, tmp_path):
        empty = tmp_path / "empty-skill"
        empty.mkdir()
        with pytest.raises(FileNotFoundError):
            install_to_claude_code(empty, target_root=tmp_path / "fake_home")

    def test_install_all_skills_filters_invalid_dirs(self, tmp_path):
        src_root = tmp_path / "src_skills"
        _build_skill(src_root, "skill-a")
        _build_skill(src_root, "skill-b")
        # 没有 SKILL.md 的目录应被跳过
        (src_root / "no-skill-md").mkdir()
        # 文件不是目录，也应被跳过
        (src_root / "stray.txt").write_text("ignore me", encoding="utf-8")

        fake_home = tmp_path / "fake_home"
        installed = install_all_to_claude_code(src_root, target_root=fake_home)

        names = sorted(p.parent.name for p in installed)
        assert names == ["skill-a", "skill-b"]
        assert not (fake_home / ".claude" / "skills" / "no-skill-md").exists()


# ──────────────────────────────────────────────────────
# T4. 灰度加载（canary） — staging 分支 + 路由 + ux 分 + 决策
# ──────────────────────────────────────────────────────

class TestCanaryGradualLoad:
    """灰度加载：staging 分支承载新版本 → pick_side 确定性分流 →
    收集 ux 分 → check_and_decide 自动 promoted/rejected。
    """

    @staticmethod
    def _init_skill_repo(path: Path, body: str = "main v1") -> str:
        path.mkdir(parents=True, exist_ok=True)
        run_git(["init"], cwd=str(path))
        run_git(["checkout", "-b", "main"], cwd=str(path))
        run_git(["config", "user.email", "t@t"], cwd=str(path))
        run_git(["config", "user.name", "t"], cwd=str(path))
        (path / "SKILL.md").write_text(body, encoding="utf-8")
        run_git(["add", "-A"], cwd=str(path))
        run_git(["commit", "-m", "init"], cwd=str(path))
        return canary.main_sha(path)

    def test_route_main_history_to_staging_creates_staging(self, tmp_path):
        skill_dir = tmp_path / "skill-x"
        initial = self._init_skill_repo(skill_dir, body="main v1")
        # main 上叠一个候选版本
        (skill_dir / "SKILL.md").write_text("main v2 (candidate)", encoding="utf-8")
        run_git(["add", "-A"], cwd=str(skill_dir))
        run_git(["commit", "-m", "candidate v2"], cwd=str(skill_dir))

        moved = canary.route_main_history_to_staging(skill_dir, initial_main_sha=initial)

        assert moved is True
        assert canary.has_staging(skill_dir)
        assert canary.main_sha(skill_dir) == initial  # main 回到 v1
        assert canary.staging_sha(skill_dir) != initial  # staging 保留 v2

    def test_pick_side_deterministic_and_distributed(self, tmp_path):
        # 同 traj 重复调用始终同 side
        a = canary.pick_side("traj_0001", "skill-x", probability=0.5)
        b = canary.pick_side("traj_0001", "skill-x", probability=0.5)
        assert a == b

        # 100 个 traj 在 50% 概率下应两边都有
        sides = [
            canary.pick_side(f"traj_{i:04d}", "skill-x", 0.5)
            for i in range(100)
        ]
        assert "main" in sides
        assert "staging" in sides

        # probability 边界
        assert canary.pick_side("any", "any", 0.0) == "main"
        assert canary.pick_side("any", "any", 1.0) == "staging"

    def test_resolve_skill_for_traj_returns_branch_body(self, tmp_path):
        skill_dir = tmp_path / "skill-x"
        initial = self._init_skill_repo(skill_dir, body="MAIN BODY")
        (skill_dir / "SKILL.md").write_text("STAGING BODY", encoding="utf-8")
        run_git(["add", "-A"], cwd=str(skill_dir))
        run_git(["commit", "-m", "candidate"], cwd=str(skill_dir))
        canary.route_main_history_to_staging(skill_dir, initial_main_sha=initial)

        # 强制 probability=0 → 永远走 main
        resolved_main = canary.resolve_skill_for_traj(
            skill_dir, traj_id="t1", skill_name="skill-x", probability=0.0
        )
        assert resolved_main["side"] == "main"
        assert resolved_main["body"] == "MAIN BODY"

        # 强制 probability=1 → 永远走 staging
        resolved_staging = canary.resolve_skill_for_traj(
            skill_dir, traj_id="t1", skill_name="skill-x", probability=1.0
        )
        assert resolved_staging["side"] == "staging"
        assert resolved_staging["body"] == "STAGING BODY"

    def test_check_and_decide_promotes_when_staging_wins(self, tmp_path):
        skill_dir = tmp_path / "skill-x"
        initial = self._init_skill_repo(skill_dir, body="main version")
        (skill_dir / "SKILL.md").write_text("staging version", encoding="utf-8")
        run_git(["add", "-A"], cwd=str(skill_dir))
        run_git(["commit", "-m", "candidate"], cwd=str(skill_dir))
        canary.route_main_history_to_staging(skill_dir, initial_main_sha=initial)

        m_sha = canary.main_sha(skill_dir)
        s_sha = canary.staging_sha(skill_dir)

        # 灌 ux 分：staging 明显胜
        cfg = canary.CanaryConfig(probability=0.5, min_samples=5, max_days_hold=14)
        for i in range(cfg.min_samples):
            canary.append_ux_score(
                skill_dir, traj_id=f"m_{i}", skill_name="skill-x",
                side="main", commit_sha=m_sha, score=5.0, reasons="ok",
            )
            canary.append_ux_score(
                skill_dir, traj_id=f"s_{i}", skill_name="skill-x",
                side="staging", commit_sha=s_sha, score=9.0, reasons="great",
            )

        result = canary.check_and_decide(skill_dir, cfg)

        assert result["action"] == "promoted"
        assert result["staging_avg"] >= result["main_avg"]
        assert not canary.has_staging(skill_dir)
        # main 现在指向 staging 版本
        assert (skill_dir / "SKILL.md").read_text(encoding="utf-8") == "staging version"

    def test_check_and_decide_rejects_when_staging_loses(self, tmp_path):
        skill_dir = tmp_path / "skill-y"
        initial = self._init_skill_repo(skill_dir, body="MAIN")
        (skill_dir / "SKILL.md").write_text("BAD STAGING", encoding="utf-8")
        run_git(["add", "-A"], cwd=str(skill_dir))
        run_git(["commit", "-m", "bad candidate"], cwd=str(skill_dir))
        canary.route_main_history_to_staging(skill_dir, initial_main_sha=initial)

        m_sha = canary.main_sha(skill_dir)
        s_sha = canary.staging_sha(skill_dir)

        cfg = canary.CanaryConfig(probability=0.5, min_samples=5, max_days_hold=14)
        for i in range(cfg.min_samples):
            canary.append_ux_score(
                skill_dir, traj_id=f"m_{i}", skill_name="skill-y",
                side="main", commit_sha=m_sha, score=8.0, reasons="stable",
            )
            canary.append_ux_score(
                skill_dir, traj_id=f"s_{i}", skill_name="skill-y",
                side="staging", commit_sha=s_sha, score=3.0, reasons="worse",
            )

        result = canary.check_and_decide(skill_dir, cfg)

        assert result["action"] == "rejected"
        assert not canary.has_staging(skill_dir)
        # main 没动
        assert (skill_dir / "SKILL.md").read_text(encoding="utf-8") == "MAIN"

    def test_check_and_decide_waits_when_samples_insufficient(self, tmp_path):
        skill_dir = tmp_path / "skill-z"
        initial = self._init_skill_repo(skill_dir, body="MAIN")
        (skill_dir / "SKILL.md").write_text("STAGING", encoding="utf-8")
        run_git(["add", "-A"], cwd=str(skill_dir))
        run_git(["commit", "-m", "candidate"], cwd=str(skill_dir))
        canary.route_main_history_to_staging(skill_dir, initial_main_sha=initial)

        m_sha = canary.main_sha(skill_dir)
        s_sha = canary.staging_sha(skill_dir)

        cfg = canary.CanaryConfig(probability=0.5, min_samples=5, max_days_hold=14)
        # 只灌 2 条（< min_samples）
        for i in range(2):
            canary.append_ux_score(
                skill_dir, traj_id=f"m_{i}", skill_name="skill-z",
                side="main", commit_sha=m_sha, score=5.0, reasons="",
            )
            canary.append_ux_score(
                skill_dir, traj_id=f"s_{i}", skill_name="skill-z",
                side="staging", commit_sha=s_sha, score=8.0, reasons="",
            )

        result = canary.check_and_decide(skill_dir, cfg)

        assert result["action"] == "waiting"
        assert canary.has_staging(skill_dir)
