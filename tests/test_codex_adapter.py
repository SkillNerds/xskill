"""
test_codex_adapter.py -- Codex CLI 适配器（P2）端到端
=======================================================

覆盖：

* **T1 codex_rollout_jsonl adapter** —— 从入仓 fixture 解析 cwd / session_id /
  user_message，断言进 timeline 与 meta 字段。
* **T2 ingest_codex_sessions** —— 在 tmp_path 模拟 ``<home>/.codex/sessions/YYYY/MM/DD/
  rollout-*.jsonl`` 布局，扫盘 + 桥接，断言 ``traj_codex_*.md`` 落到 watch dir
  且元数据包含 cwd / session_id。
* **T3 JsonlIngester spec dispatching** —— 同一个 ``JsonlIngester`` 基类按 spec
  分派到 CC vs Codex 走完全独立的扫盘路径。
* **T4 install_to_codex** —— skill 落到 ``<home>/.agents/skills/<name>``（不是
  ``.codex/skills``）。
* **T5 install_to_codex copy fallback** —— monkeypatch symlink/junction 都失败，
  仍能装出 SKILL.md 且打 warning。
* **T6 路径解析跨平台** —— 用 ``Path('/fake/home')`` 等纯字符串验证三平台
  都是 POSIX-style ``Path`` 操作（不依赖 ``os.path.expanduser``）。

fixture 见 ``tests/fixtures/codex/sample_rollout.jsonl``——由 codex-rs 单测
schema 1:1 移植生成（``tests/fixtures/codex/README.md``）。
"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import pytest

from xskill.ecosystems import _fallback as install_fallback
from xskill.ecosystems import (
    adapt_trajectory,
    CC_SPEC,
    CODEX_SPEC,
    EcosystemSpec,
    JsonlIngester,
    _agents_skills_path,
    _cc_projects_path,
    _cc_skills_path,
    _codex_sessions_path,
    _codex_session_id_from_path,
    _read_cwd_from_codex_jsonl,
    ingest_codex_sessions,
    install_to_codex,
)
from xskill.skill.frontmatter import serialize as fm_serialize


# ──────────────────────────────────────────────────────────────────
# Fixture loading
# ──────────────────────────────────────────────────────────────────

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "codex" / "sample_rollout.jsonl"
EXPECTED_SESSION_ID = "11111111-2222-3333-4444-555555555555"
EXPECTED_CWD = "/tmp/codex-test-workspace"
EXPECTED_USER_MESSAGE = "Hello from user"


@pytest.fixture
def fixture_content() -> str:
    """返回入仓 codex rollout fixture 的原始 JSONL 文本。"""
    assert FIXTURE_PATH.is_file(), (
        f"fixture missing: {FIXTURE_PATH} — "
        "run `cd tests/fixtures/codex && python3.11 generate.py .` to regenerate"
    )
    return FIXTURE_PATH.read_text(encoding="utf-8")


# ──────────────────────────────────────────────────────────────────
# Helpers — 在 tmp_path 下伪造 codex sessions 目录结构
# ──────────────────────────────────────────────────────────────────


def _place_fixture_in_codex_home(home: Path, fixture_text: str) -> Path:
    """把 fixture 内容写到 ``<home>/.codex/sessions/2026/01/15/rollout-...jsonl``，
    返回写入的文件路径。模拟 codex CLI 实际写盘的目录布局。
    """
    sessions = home / ".codex" / "sessions" / "2026" / "01" / "15"
    sessions.mkdir(parents=True, exist_ok=True)
    p = sessions / f"rollout-2026-01-15T10-00-00-{EXPECTED_SESSION_ID}.jsonl"
    p.write_text(fixture_text, encoding="utf-8")
    return p


def _build_codex_skill(skill_root: Path, name: str = "demo-skill") -> Path:
    """造一个最小合规 skill 目录（``SKILL.md`` 带必需 frontmatter）。"""
    skill_path = skill_root / name
    skill_path.mkdir(parents=True, exist_ok=True)
    fm = {
        "name": name,
        "description": "A demo codex skill.",
        "version": 1,
        "source_trajs": ["traj_codex_0001"],
        "metadata": {"tags": ["demo"], "frozen": False},
    }
    (skill_path / "SKILL.md").write_text(
        fm_serialize(fm, f"# {name}\n\nBody.\n"), encoding="utf-8"
    )
    return skill_path


# ──────────────────────────────────────────────────────────────────
# T1. codex_rollout_jsonl adapter
# ──────────────────────────────────────────────────────────────────


class TestCodexRolloutJsonlAdapter:
    """adapter 能从 codex 真实 schema 抽 cwd / session_id / user_message。"""

    def test_adapter_returns_markdown_with_session_meta(self, fixture_content):
        md, meta = adapt_trajectory(fixture_content, "codex_rollout_jsonl")

        assert md.startswith("# Codex Rollout Trajectory")
        assert f"**session_id**: {EXPECTED_SESSION_ID}" in md
        assert f"**cwd**: {EXPECTED_CWD}" in md
        assert "**originator**: codex-cli" in md
        assert "**cli_version**: 0.130.0" in md
        assert "**model_provider**: openai" in md
        assert EXPECTED_USER_MESSAGE in md

    def test_adapter_meta_has_session_id_and_cwd(self, fixture_content):
        _md, meta = adapt_trajectory(fixture_content, "codex_rollout_jsonl")

        assert meta["session_id"] == EXPECTED_SESSION_ID
        assert meta["cwd"] == EXPECTED_CWD
        assert meta["originator"] == "codex-cli"
        assert meta["cli_version"] == "0.130.0"
        assert meta["model_provider"] == "openai"
        assert meta["source"] == "codex_rollout_jsonl"
        assert meta["category"] == "codex_session"
        assert meta["query"] == EXPECTED_USER_MESSAGE
        # fixture 写了 3 条 response_item
        assert meta["response_items"] == 3

    def test_adapter_extracts_user_message_into_timeline(self, fixture_content):
        _md, meta = adapt_trajectory(fixture_content, "codex_rollout_jsonl")

        timeline = meta["timeline"]
        user_entries = [e for e in timeline if e["role"] == "user"]
        assert len(user_entries) == 1
        assert user_entries[0]["content"] == EXPECTED_USER_MESSAGE

        assistant_entries = [e for e in timeline if e["role"] == "assistant"]
        # 3 条 response_item 占位
        assert len(assistant_entries) == 3

    def test_adapter_handles_empty_input(self):
        md, meta = adapt_trajectory("", "codex_rollout_jsonl")

        assert "# Codex Rollout Trajectory" in md
        assert meta["total_turns"] == 0
        assert meta["response_items"] == 0
        assert meta["category"] == "codex_session"

    def test_adapter_skips_malformed_lines(self):
        # 第 2 行故意 broken JSON，验证 ingester 不挂
        bad = "\n".join([
            json.dumps({
                "timestamp": "2026-01-15T10-00-00",
                "type": "session_meta",
                "payload": {"id": "sid", "cwd": "/x", "originator": "codex-cli",
                            "cli_version": "0.130.0"},
            }),
            "{not a valid json line",
            json.dumps({
                "timestamp": "2026-01-15T10-00-00",
                "type": "event_msg",
                "payload": {"type": "user_message", "message": "hi", "kind": "plain"},
            }),
        ])
        _md, meta = adapt_trajectory(bad, "codex_rollout_jsonl")
        assert meta["session_id"] == "sid"
        assert meta["query"] == "hi"


# ──────────────────────────────────────────────────────────────────
# T2. ingest_codex_sessions —— 扫盘 + 桥接
# ──────────────────────────────────────────────────────────────────


class TestIngestCodexSessions:
    """``ingest_codex_sessions(<home>=tmp_path)`` 能找到 ``~/.codex/sessions/...``
    布局下的 rollout 并桥成 ``traj_codex_*.md``。
    """

    def test_ingest_writes_traj_with_codex_prefix(self, tmp_path, fixture_content):
        _place_fixture_in_codex_home(tmp_path, fixture_content)
        traj_dir = tmp_path / "traj"

        results = ingest_codex_sessions(traj_dir, home_root=tmp_path)

        assert len(results) == 1
        rec = results[0]
        assert rec["status"] == "stored"
        assert rec["session_id"] == EXPECTED_SESSION_ID
        assert rec["ecosystem"] == "codex"
        # traj_id 形如 traj_codex_codex-test-workspace_11111111
        assert rec["traj_id"].startswith("traj_codex_")

        md_path = Path(rec["path"])
        assert md_path.is_file()
        assert md_path.name.startswith("traj_codex_")

        meta_path = md_path.with_suffix(".json")
        assert meta_path.is_file()
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        assert meta["session_id"] == EXPECTED_SESSION_ID
        assert meta["cwd"] == EXPECTED_CWD
        assert meta["category"] == "codex_session"
        assert meta["query"] == EXPECTED_USER_MESSAGE

    def test_ingest_is_idempotent_via_seen_sessions(self, tmp_path, fixture_content):
        _place_fixture_in_codex_home(tmp_path, fixture_content)
        traj_dir = tmp_path / "traj"
        seen: set[str] = set()

        first = ingest_codex_sessions(traj_dir, home_root=tmp_path, seen_sessions=seen)
        assert len(first) == 1
        assert EXPECTED_SESSION_ID in seen

        # 再扫一次 —— 同 sid 应被跳过
        second = ingest_codex_sessions(traj_dir, home_root=tmp_path, seen_sessions=seen)
        assert second == []

    def test_ingest_skips_when_sessions_dir_absent(self, tmp_path):
        # ``<tmp>/.codex/sessions`` 不存在 → 返回空 list，不报错
        results = ingest_codex_sessions(tmp_path / "traj", home_root=tmp_path)
        assert results == []

    def test_ingest_skips_empty_files(self, tmp_path):
        # 触摸空 file（mkdir 父目录）
        sessions = tmp_path / ".codex" / "sessions" / "2026" / "01" / "15"
        sessions.mkdir(parents=True)
        (sessions / "rollout-empty.jsonl").write_text("", encoding="utf-8")

        results = ingest_codex_sessions(tmp_path / "traj", home_root=tmp_path)
        assert results == []

    def test_ingest_finds_files_recursively_under_date_dirs(self, tmp_path, fixture_content):
        # 在多个日期子目录下放 fixture，验证 ``YYYY/MM/DD`` 递归匹配
        d1 = tmp_path / ".codex" / "sessions" / "2026" / "01" / "15"
        d2 = tmp_path / ".codex" / "sessions" / "2026" / "02" / "20"
        d1.mkdir(parents=True)
        d2.mkdir(parents=True)
        (d1 / f"rollout-2026-01-15T10-00-00-aaaaaaaa-bbbb-cccc-dddd-{'1' * 12}.jsonl").write_text(
            fixture_content.replace(EXPECTED_SESSION_ID, "aaaaaaaa-bbbb-cccc-dddd-111111111111"),
            encoding="utf-8",
        )
        (d2 / f"rollout-2026-02-20T10-00-00-bbbbbbbb-cccc-dddd-eeee-{'2' * 12}.jsonl").write_text(
            fixture_content.replace(EXPECTED_SESSION_ID, "bbbbbbbb-cccc-dddd-eeee-222222222222"),
            encoding="utf-8",
        )

        results = ingest_codex_sessions(tmp_path / "traj", home_root=tmp_path)
        sids = sorted(r["session_id"] for r in results)
        assert sids == [
            "aaaaaaaa-bbbb-cccc-dddd-111111111111",
            "bbbbbbbb-cccc-dddd-eeee-222222222222",
        ]


# ──────────────────────────────────────────────────────────────────
# T3. JsonlIngester spec dispatching
# ──────────────────────────────────────────────────────────────────


class TestJsonlIngesterSpecDispatch:
    """同一个 ``JsonlIngester`` 基类按 spec 分派——CC spec 只扫 CC 路径，
    Codex spec 只扫 codex 路径，两者完全独立。
    """

    def test_cc_spec_ignores_codex_rollouts(self, tmp_path, fixture_content):
        # 只放 codex fixture，不放 CC 文件
        _place_fixture_in_codex_home(tmp_path, fixture_content)

        cc = JsonlIngester(CC_SPEC)
        results = cc.scan_and_bridge(tmp_path / "traj", home_root=tmp_path)
        # CC spec 扫 ~/.claude/projects → 没东西
        assert results == []

    def test_codex_spec_ignores_cc_sessions(self, tmp_path):
        # 放一份伪 CC session（满足 CC glob）
        cc_dir = tmp_path / ".claude" / "projects" / "abcd1234"
        cc_dir.mkdir(parents=True)
        (cc_dir / "sess-xxx.jsonl").write_text(
            json.dumps({
                "type": "user",
                "message": {"role": "user", "content": "cc hello"},
                "sessionId": "sess-xxx", "cwd": "/cc/proj",
            }) + "\n",
            encoding="utf-8",
        )

        codex = JsonlIngester(CODEX_SPEC)
        results = codex.scan_and_bridge(tmp_path / "traj", home_root=tmp_path)
        # Codex spec 扫 ~/.codex/sessions → 没东西
        assert results == []

    def test_both_specs_share_same_base_class(self):
        cc = JsonlIngester(CC_SPEC)
        codex = JsonlIngester(CODEX_SPEC)
        # 同一类型，不同实例
        assert type(cc) is type(codex)
        assert cc.spec is not codex.spec
        assert cc.spec.name == "claude_code"
        assert codex.spec.name == "codex"

    def test_sqlite_spec_rejected_by_jsonl_ingester(self):
        """JsonlIngester 拒绝 sqlite 形态 spec——P3 OpenCode 自己实现 SqliteIngester。"""
        # 临时造一个 sqlite spec 模拟 P3 即将引入的形态
        sqlite_spec = EcosystemSpec(
            name="fake_sqlite",
            source_kind="jsonl",  # 占位先这么写，下面覆盖
            sessions_path=lambda h: h / "fake",
            sessions_glob="*.db",
            session_id_from_path=lambda p: p.stem,
            cwd_from_content=lambda c: "",
            adapter_format="raw",
            traj_id_prefix="traj_x_",
            skills_install_path=lambda h: h / "fake_skills",
            label="fake",
        )
        # frozen dataclass — 用 dataclasses.replace 改 source_kind
        from dataclasses import replace
        bad = replace(sqlite_spec, source_kind="sqlite")
        with pytest.raises(ValueError, match="jsonl"):
            JsonlIngester(bad)


# ──────────────────────────────────────────────────────────────────
# T4. install_to_codex —— skill 落到 ~/.agents/skills/<name>
# ──────────────────────────────────────────────────────────────────


class TestInstallToCodex:
    """skill 装到 ``<home>/.agents/skills/<name>``——是 .agents 不是 .codex。"""

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="Windows symlink happy path 需要 Dev Mode；该 case 在 P4 Windows runner 覆盖",
    )
    def test_install_creates_skill_md_at_agents_skills_path(self, tmp_path):
        skill_path = _build_codex_skill(tmp_path / "src")
        fake_home = tmp_path / "home"

        dest = install_to_codex(skill_path, target_root=fake_home)

        # 关键路径断言：是 .agents，不是 .codex
        assert dest == fake_home / ".agents" / "skills" / "demo-skill" / "SKILL.md"
        assert dest.is_file()

        # 不要装到 .codex/skills（deprecated 路径）
        deprecated = fake_home / ".codex" / "skills"
        assert not deprecated.exists()

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="Windows symlink happy path 需要 Dev Mode",
    )
    def test_install_uses_symlink_on_posix(self, tmp_path):
        skill_path = _build_codex_skill(tmp_path / "src")
        fake_home = tmp_path / "home"

        install_to_codex(skill_path, target_root=fake_home)

        dest_dir = fake_home / ".agents" / "skills" / "demo-skill"
        assert dest_dir.is_symlink()
        assert dest_dir.resolve() == skill_path.resolve()

    def test_install_missing_skill_md_raises(self, tmp_path):
        empty = tmp_path / "empty-skill"
        empty.mkdir()
        with pytest.raises(FileNotFoundError):
            install_to_codex(empty, target_root=tmp_path / "home")


# ──────────────────────────────────────────────────────────────────
# T5. install_to_codex fallback —— symlink 失败走 copy
# ──────────────────────────────────────────────────────────────────


class TestInstallToCodexFallback:
    """模拟 Windows 非 Dev Mode + 跨盘场景：symlink 失败、junction 失败
    → copy 模式；仍要装出 SKILL.md，并通过 ``xskill.ecosystems`` logger 打 warning。
    """

    def test_install_falls_back_to_copy_when_symlink_and_junction_fail(
        self, tmp_path, monkeypatch, caplog
    ):
        skill_path = _build_codex_skill(tmp_path / "src")
        fake_home = tmp_path / "home"

        monkeypatch.setattr(install_fallback, "_try_symlink", lambda s, d: False)
        monkeypatch.setattr(install_fallback, "_try_junction", lambda s, d: False)

        with caplog.at_level(logging.WARNING, logger="xskill.ecosystems"):
            dest = install_to_codex(skill_path, target_root=fake_home)

        assert dest == fake_home / ".agents" / "skills" / "demo-skill" / "SKILL.md"
        assert dest.is_file()
        # 不是 symlink（copy fallback）
        assert not dest.parent.is_symlink()
        # 该 warning 标注是 codex 生态、copy 模式
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert any("install_to_codex" in r.message and "copy-mode" in r.message
                   for r in warnings)


# ──────────────────────────────────────────────────────────────────
# T6. 路径解析跨平台 —— 不依赖 os.path.expanduser
# ──────────────────────────────────────────────────────────────────


class TestPathResolversCrossPlatform:
    """所有路径 helper 都接受 ``Path`` 实例并返回 ``Path`` 实例，纯字符串可重现。

    P1 落地的硬要求：不用 ``os.path.expanduser``，全走 ``Path.home() / ...``。
    本测用 ``Path('/fake/home')`` 类型断言，三平台都跑得过——Windows runner
    会自动转 ``WindowsPath('/fake/home')``，但断言用 ``__fspath__`` / 子路径
    比较仍稳。
    """

    def test_codex_sessions_path_under_dot_codex_sessions(self):
        home = Path("/fake/home")
        assert _codex_sessions_path(home) == home / ".codex" / "sessions"

    def test_agents_skills_path_under_dot_agents_skills(self):
        """安装根是 ``.agents/skills``——跨 codex / opencode 共享。"""
        home = Path("/fake/home")
        assert _agents_skills_path(home) == home / ".agents" / "skills"

    def test_cc_paths_unchanged(self):
        """CC 路径不应被 P2 改动。"""
        home = Path("/fake/home")
        assert _cc_projects_path(home) == home / ".claude" / "projects"
        assert _cc_skills_path(home) == home / ".claude" / "skills"

    def test_cc_and_codex_install_paths_diverge(self):
        """CC → ``.claude/skills`` ；Codex → ``.agents/skills``。两者必须不同
        目录，不能装错。
        """
        home = Path("/fake/home")
        cc = _cc_skills_path(home)
        codex_dest = _agents_skills_path(home)
        assert cc != codex_dest
        assert cc.name == "skills"
        assert codex_dest.name == "skills"
        assert cc.parent.name == ".claude"
        assert codex_dest.parent.name == ".agents"

    def test_spec_path_resolvers_use_pathlib_only(self):
        """``EcosystemSpec.sessions_path`` 调用是纯 ``Path`` 操作，不触发
        ``os.path.expanduser`` 等环境敏感函数（这是设计约束）。
        """
        home = Path("/another/home")
        assert CC_SPEC.sessions_path(home) == home / ".claude" / "projects"
        assert CODEX_SPEC.sessions_path(home) == home / ".codex" / "sessions"
        assert CC_SPEC.skills_install_path(home) == home / ".claude" / "skills"
        assert CODEX_SPEC.skills_install_path(home) == home / ".agents" / "skills"


# ──────────────────────────────────────────────────────────────────
# T7. codex 私有 helpers —— session_id_from_path / cwd_from_content
# ──────────────────────────────────────────────────────────────────


class TestCodexHelpers:
    def test_session_id_from_path_extracts_uuid(self, tmp_path):
        # 标准 rollout 文件名
        p = tmp_path / f"rollout-2026-01-15T10-00-00-{EXPECTED_SESSION_ID}.jsonl"
        assert _codex_session_id_from_path(p) == EXPECTED_SESSION_ID

    def test_session_id_from_path_handles_short_name(self, tmp_path):
        # 文件名不符合预期 —— 退化为整个 stem（不挂）
        p = tmp_path / "rollout-short.jsonl"
        # short 只能拆出 ['rollout', 'short'] = 2 段，少于 5 → 走退化分支
        assert _codex_session_id_from_path(p) == "rollout-short"

    def test_read_cwd_from_codex_jsonl_extracts_session_meta_cwd(self, fixture_content):
        cwd = _read_cwd_from_codex_jsonl(fixture_content)
        assert cwd == EXPECTED_CWD

    def test_read_cwd_returns_empty_when_no_session_meta(self):
        only_response = json.dumps({
            "timestamp": "x", "type": "response_item",
            "payload": {"record_type": "response", "index": 0},
        })
        assert _read_cwd_from_codex_jsonl(only_response) == ""


# ──────────────────────────────────────────────────────────────────
# codex-cli ≥0.148 rollout format (user input moved into response_item)
# ──────────────────────────────────────────────────────────────────

FIXTURE_V0148 = Path(__file__).parent / "fixtures" / "codex" / "sample_rollout_v0148.jsonl"


class TestCodexRollout0148:
    """0.148 把用户输入从 ``event_msg::user_message`` 搬到
    ``response_item/message/role=user``，并把注入上下文混在同一形态里。
    fixture 是真实 codex-cli 0.148.0 写出的 rollout（脱敏）。"""

    def _adapt(self):
        assert FIXTURE_V0148.is_file(), f"fixture missing: {FIXTURE_V0148}"
        return adapt_trajectory(
            FIXTURE_V0148.read_text(encoding="utf-8"), "codex_rollout_jsonl",
        )

    def test_user_prompt_is_recovered(self):
        md, meta = self._adapt()
        assert meta.get("query") == "hello"
        assert "## User\n\nhello" in md

    def test_assistant_reply_is_recovered(self):
        md, _meta = self._adapt()
        assert "## Assistant\n\nHello from mock LLM" in md

    def test_injected_context_is_filtered(self):
        """role=developer 的 skills 说明与 role=user 的 <environment_context>
        是运行时注入，不是用户说的话，不得进入轨迹正文。"""
        md, _meta = self._adapt()
        assert "<environment_context>" not in md
        assert "skills_instructions" not in md
        assert "<cwd>" not in md

    def test_session_meta_still_parsed(self):
        md, _meta = self._adapt()
        assert "**cli_version**: 0.148.0" in md
        assert "**originator**: codex_exec" in md

    def test_old_format_fixture_unchanged(self):
        """两种格式并存：旧 fixture（event_msg::user_message）行为不变。"""
        md, meta = adapt_trajectory(
            FIXTURE_PATH.read_text(encoding="utf-8"), "codex_rollout_jsonl",
        )
        assert meta.get("query")
        assert "## User" in md
