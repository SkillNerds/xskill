"""
test_deepseek_harness_adapter.py -- DeepSeek Harness (dsh) 接入端到端单测
=========================================================================

覆盖：

* **T1 deepseek_harness_session_jsonl adapter** —— 从 fixture 解析
  timeline + tool_names + first_user_query；跳过 ``assistant/chunk`` 与
  打包行；session header 元数据（session_id / cwd / agent_preset）。
* **T2 ingest_deepseek_harness_sessions** —— tmp_path 模拟
  ``<home>/.dsh/sessions/--<cwd>--/<encoded-id>/session.jsonl`` 布局，
  扫盘 + 桥接，断言 traj_dsh_*.md 落地；zstd 文件不被拾取。
* **T3 detect_known_ecosystems** —— ``<home>/.dsh/`` 存在即注册 dsh bridge
  （无 sessions 子目录也要能探到，skill 安装不依赖已有会话）。
* **T4 helpers** —— path helpers / sid 抽取 / header cwd 抽取 / spec 字段。
* **T5 install** —— ``install_to_deepseek_harness`` 装到
  ``~/.dsh/skills/<name>/``，POSIX 上 symlink；不写共享的
  ``~/.agents/skills``；watcher / daemon 安装表含 deepseek_harness。

fixture 来源：``fixtures/deepseek_harness/session.jsonl`` 由 DeepSeek Harness
自身的序列化函数（``@deepseek-ai/dsh-session-persistence-jsonl`` 0.1.0-rc.7）
生成，非手写；见同目录 README.md。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from xskill.ecosystems import (
    DSH_SPEC,
    JsonlIngester,
    _dsh_session_id_from_path,
    _dsh_sessions_path,
    _dsh_skills_path,
    _read_cwd_from_dsh_jsonl,
    adapt_trajectory,
    detect_known_ecosystems,
    ingest_deepseek_harness_sessions,
    install_to_deepseek_harness,
)
from xskill.skill.frontmatter import serialize as fm_serialize

FIXTURE_PATH = (
    Path(__file__).parent / "fixtures" / "deepseek_harness" / "session.jsonl"
)
ZSTD_FIXTURE_PATH = (
    Path(__file__).parent / "fixtures" / "deepseek_harness" / "session.jsonl.zstd"
)


@pytest.fixture
def fixture_content() -> str:
    assert FIXTURE_PATH.is_file(), f"fixture missing: {FIXTURE_PATH}"
    return FIXTURE_PATH.read_text(encoding="utf-8")


def _place_fixture_in_dsh_home(
    home: Path, fixture_text: str,
    project_dir: str = "--home-u-proj--", encoded_id: str = "session-c69faa5c-9536-443a-b839-a640f199e663",
) -> Path:
    session_dir = home / ".dsh" / "sessions" / project_dir / encoded_id
    session_dir.mkdir(parents=True, exist_ok=True)
    p = session_dir / "session.jsonl"
    p.write_text(fixture_text, encoding="utf-8")
    return p


def _build_skill(skill_root: Path, name: str = "demo-skill") -> Path:
    skill_path = skill_root / name
    skill_path.mkdir(parents=True, exist_ok=True)
    fm = {
        "name": name,
        "description": "A demo skill for dsh install tests.",
        "version": 1,
        "metadata": {"tags": ["demo"], "frozen": False},
    }
    (skill_path / "SKILL.md").write_text(
        fm_serialize(fm, f"# {name}\n\nBody.\n"), encoding="utf-8"
    )
    return skill_path


# ──────────────────────────────────────────────────────────────────
# T1. adapter
# ──────────────────────────────────────────────────────────────────


class TestAdapter:
    def test_adapter_returns_markdown_with_user_assistant(self, fixture_content):
        md, _meta = adapt_trajectory(
            fixture_content, "deepseek_harness_session_jsonl",
        )
        assert md.startswith("# DeepSeek Harness Trajectory")
        assert "## User" in md
        assert "## Assistant" in md
        assert "HELLO XSKILL" in md

    def test_adapter_extracts_first_user_query(self, fixture_content):
        _md, meta = adapt_trajectory(
            fixture_content, "deepseek_harness_session_jsonl",
        )
        assert meta["query"].startswith("Reply with exactly the two words")

    def test_adapter_collects_tool_names(self, fixture_content):
        _md, meta = adapt_trajectory(
            fixture_content, "deepseek_harness_session_jsonl",
        )
        assert meta["tool_names"] == []  # the real run used no tools

    def test_adapter_skips_chunks_and_packed_rows(self, fixture_content):
        """assistant/chunk 与 text-chunks 打包行是重放数据，不进 timeline。"""
        md, meta = adapt_trajectory(
            fixture_content, "deepseek_harness_session_jsonl",
        )
        # 4 assistant/chunk + 1 text-chunks packed row exist in the file;
        # only the assembled assistant/message may reach the timeline, so
        # there is exactly one Assistant section (chunks would add more).
        assert md.count("## Assistant") == 1
        assistant_turns = [e for e in meta["timeline"] if e["role"] == "assistant"]
        assert [e["content"] for e in assistant_turns] == ["HELLO XSKILL"]
        # timeline: 1 real user message + 1 assistant/message = 2 条
        assert meta["total_turns"] == 2

    def test_adapter_session_header_metadata(self, fixture_content):
        __, meta = adapt_trajectory(
            fixture_content, "deepseek_harness_session_jsonl",
        )
        assert meta["session_id"] == "session-c69faa5c-9536-443a-b839-a640f199e663"
        assert meta["cwd"] == "/home/u/proj"  # sanitized in fixture
        assert "agent_preset" not in meta  # headless run wrote no agentPreset
        assert meta["source"] == "deepseek_harness_session_jsonl"
        assert meta["category"] == "deepseek_harness_session"
        assert meta["execution_usage_events"]
        assert meta["execution_usage_events"][0]["source_event_id"]

    def test_adapter_handles_empty(self):
        md, meta = adapt_trajectory("", "deepseek_harness_session_jsonl")
        assert meta["total_turns"] == 0
        assert md.startswith("# DeepSeek Harness Trajectory")

    def test_adapter_skips_malformed_lines(self):
        content = "not-json\n{\"type\": \"user/message\", \"seq\": 1}\n"
        _md, meta = adapt_trajectory(content, "deepseek_harness_session_jsonl")
        # 第二行缺 data，也应安全跳过
        assert meta["total_turns"] == 0

    def test_adapter_filters_injected_user_context(self, fixture_content):
        """dsh 以 user 角色注入运行时上下文（source.kind = plugin /
        skill-catalog）；只有 source.kind == "user" 才是用户说的话。真机一次
        任务写出 3 条 user/message，其中 2 条是注入——必须过滤，否则整份
        skill 目录清单与沙箱策略会被当作用户消息写进轨迹。"""
        md, meta = adapt_trajectory(
            fixture_content, "deepseek_harness_session_jsonl",
        )
        assert "example-skill-a" not in md        # skill-catalog injection
        assert "Approval policy" not in md        # system-prompt snapshot
        assert "Current runtime context" not in md
        user_turns = [e for e in meta["timeline"] if e["role"] == "user"]
        assert len(user_turns) == 1
        assert user_turns[0]["content"].startswith("Reply with exactly")

    def test_adapter_legacy_user_message_without_source_is_kept(self):
        """无 source 字段的旧格式 user/message 视为用户消息（前向兼容）。"""
        content = "\n".join([
            json.dumps({"type": "session", "id": "s1", "cwd": "/w"}),
            json.dumps({
                "type": "user/message", "seq": 1, "time": 1,
                "data": {"role": "user", "content": "legacy question"},
            }),
        ])
        md, meta = adapt_trajectory(content, "deepseek_harness_session_jsonl")
        assert "legacy question" in md
        assert meta["total_turns"] == 1

    def test_adapter_string_content_form(self):
        """Message.content 的 string 形态也要能取到正文。"""
        content = "\n".join([
            json.dumps({"type": "session", "id": "s1", "cwd": "/w"}),
            json.dumps({
                "type": "user/message", "seq": 1, "time": 1,
                "data": {"role": "user", "content": "plain string question"},
            }),
        ])
        md, meta = adapt_trajectory(content, "deepseek_harness_session_jsonl")
        assert "plain string question" in md
        assert meta["total_turns"] == 1


# ──────────────────────────────────────────────────────────────────
# T2. ingest
# ──────────────────────────────────────────────────────────────────


class TestIngest:
    def test_ingest_writes_traj_with_dsh_prefix(self, tmp_path, fixture_content):
        _place_fixture_in_dsh_home(tmp_path, fixture_content)
        traj_dir = tmp_path / "traj"

        results = ingest_deepseek_harness_sessions(
            traj_dir, home_root=tmp_path,
        )

        assert len(results) == 1
        written = list(traj_dir.glob("traj_dsh_*.md"))
        assert len(written) == 1
        text = written[0].read_text(encoding="utf-8")
        assert "DeepSeek Harness Trajectory" in text

    def test_two_sessions_same_project_get_distinct_traj_files(
        self, tmp_path, fixture_content,
    ):
        """评审实测发现的覆盖缺陷（PR #243）：dsh 会话目录名带固定前缀
        ``session-``，恰好 8 个字符——若整个目录名进文件名截断，所有会话
        尾段都变成 ``session-``，同一项目多条会话写进同一个轨迹文件、
        后写覆盖先写。修复后会话标识取 uuid 段，各会话各落各的文件。"""
        _place_fixture_in_dsh_home(
            tmp_path, fixture_content,
            encoded_id="session-11111111-aaaa-bbbb-cccc-dddddddddddd",
        )
        _place_fixture_in_dsh_home(
            tmp_path, fixture_content,
            encoded_id="session-22222222-aaaa-bbbb-cccc-dddddddddddd",
        )
        traj_dir = tmp_path / "traj"

        results = ingest_deepseek_harness_sessions(
            traj_dir, home_root=tmp_path,
        )

        assert len(results) == 2
        written = sorted(p.name for p in traj_dir.glob("traj_dsh_*.md"))
        assert len(written) == 2, written
        # 尾段来自 uuid，不再是清一色的 ``session-``
        assert written[0] != written[1]
        assert not any(name.endswith("_session-.md") for name in written)

    def test_session_id_strips_fixed_prefix(self, tmp_path):
        from xskill.ecosystems.deepseek_harness import (
            _dsh_session_id_from_path,
        )
        path = (tmp_path / "session-851be686-dc2e-4271-af4b-5f35299af69e"
                / "session.jsonl")
        assert _dsh_session_id_from_path(path) == (
            "851be686-dc2e-4271-af4b-5f35299af69e"
        )
        # 不带固定前缀的目录名原样返回（未来格式变化不做猜测）
        other = tmp_path / "run_0001" / "session.jsonl"
        assert _dsh_session_id_from_path(other) == "run_0001"

    def test_ingest_idempotent(self, tmp_path, fixture_content):
        _place_fixture_in_dsh_home(tmp_path, fixture_content)
        traj_dir = tmp_path / "traj"
        seen: set[str] = set()

        first = ingest_deepseek_harness_sessions(
            traj_dir, home_root=tmp_path, seen_sessions=seen,
        )
        second = ingest_deepseek_harness_sessions(
            traj_dir, home_root=tmp_path, seen_sessions=seen,
        )
        assert len(first) == 1
        assert second == []

    def test_ingest_skips_when_dir_absent(self, tmp_path):
        results = ingest_deepseek_harness_sessions(
            tmp_path / "traj", home_root=tmp_path,
        )
        assert results == []

    def test_ingest_bridges_zstd_sessions(self, tmp_path):
        """dsh 默认写 session.jsonl.zstd（多帧拼接）；有 zstandard 时须桥接，
        结果与明文完全一致。fixture 是 dsh 真跑写出的压缩文件（脱敏后按
        dsh 布局重压为 2 帧：header 一帧 + 其余一帧）。"""
        pytest.importorskip("zstandard")
        session_dir = tmp_path / ".dsh" / "sessions" / "--home-u-proj--" / "zsess"
        session_dir.mkdir(parents=True)
        (session_dir / "session.jsonl.zstd").write_bytes(ZSTD_FIXTURE_PATH.read_bytes())

        results = ingest_deepseek_harness_sessions(
            tmp_path / "traj", home_root=tmp_path,
        )
        assert len(results) == 1
        md = next((tmp_path / "traj").glob("traj_dsh_*.md")).read_text(encoding="utf-8")
        assert "ZSTD CHECK" in md
        assert "example-skill-a" not in md   # injected catalog filtered on zstd path too

    def test_ingest_zstd_without_zstandard_skips_and_warns_once(
            self, tmp_path, monkeypatch, caplog):
        """环境缺 zstandard（安装不完整）：跳过压缩文件、只警告一次；明文不受影响。"""
        import builtins
        import logging

        from xskill.ecosystems import deepseek_harness as dsh_mod

        real_import = builtins.__import__

        def _no_zstd(name, *a, **kw):
            if name == "zstandard":
                raise ImportError("simulated missing zstandard")
            return real_import(name, *a, **kw)

        monkeypatch.setattr(builtins, "__import__", _no_zstd)
        monkeypatch.setattr(dsh_mod, "_ZSTD_MISSING_WARNED", False)
        monkeypatch.setattr(dsh_mod, "ensure_zstandard_for_dsh", lambda *_a, **_k: False)
        monkeypatch.setattr(dsh_mod, "zstandard_available", lambda: False)

        z1 = tmp_path / ".dsh" / "sessions" / "--p--" / "z1"
        z2 = tmp_path / ".dsh" / "sessions" / "--p--" / "z2"
        z1.mkdir(parents=True)
        z2.mkdir(parents=True)
        (z1 / "session.jsonl.zstd").write_bytes(b"\x28\xb5\x2f\xfd")
        (z2 / "session.jsonl.zstd").write_bytes(b"\x28\xb5\x2f\xfd")
        plain = tmp_path / ".dsh" / "sessions" / "--p--" / "plain"
        plain.mkdir(parents=True)
        (plain / "session.jsonl").write_text(
            FIXTURE_PATH.read_text(encoding="utf-8"), encoding="utf-8",
        )

        with caplog.at_level(logging.WARNING, logger="xskill.ecosystems"):
            results = ingest_deepseek_harness_sessions(
                tmp_path / "traj", home_root=tmp_path,
            )
        assert len(results) == 1                      # only the plaintext one
        warns = [r for r in caplog.records if "zstandard" in r.getMessage()]
        assert len(warns) == 1                        # warned once, not per file
        assert "pip install zstandard" in warns[0].getMessage()

    def test_ingest_finds_across_multiple_projects(self, tmp_path, fixture_content):
        _place_fixture_in_dsh_home(
            tmp_path, fixture_content, project_dir="--p-one--", encoded_id="s1",
        )
        _place_fixture_in_dsh_home(
            tmp_path, fixture_content, project_dir="--p-two--", encoded_id="s2",
        )
        results = ingest_deepseek_harness_sessions(
            tmp_path / "traj", home_root=tmp_path,
        )
        assert len(results) == 2


# ──────────────────────────────────────────────────────────────────
# T3. detection
# ──────────────────────────────────────────────────────────────────


class TestDetection:
    def test_detect_reports_dsh_when_home_dir_exists(self, tmp_path):
        """``~/.dsh`` 存在即探到——不要求 sessions 子目录（安装不依赖会话）。"""
        (tmp_path / ".dsh").mkdir()
        detections = {
            d["ecosystem"]: d for d in detect_known_ecosystems(home_root=tmp_path)
        }
        assert "deepseek_harness" in detections
        record = detections["deepseek_harness"]
        assert record["source"] == (tmp_path / ".dsh").resolve()
        assert record["bridge"] == (
            tmp_path / ".xskill" / "dsh_sessions"
        ).resolve()

    def test_detect_skips_when_absent(self, tmp_path):
        detections = [
            d["ecosystem"] for d in detect_known_ecosystems(home_root=tmp_path)
        ]
        assert "deepseek_harness" not in detections


# ──────────────────────────────────────────────────────────────────
# T4. helpers / spec
# ──────────────────────────────────────────────────────────────────


class TestHelpers:
    def test_session_id_from_path(self, tmp_path):
        p = tmp_path / "--proj--" / "enc-abc" / "session.jsonl"
        assert _dsh_session_id_from_path(p) == "enc-abc"

    def test_read_cwd_from_header(self):
        content = json.dumps({"type": "session", "id": "x", "cwd": "/w/s"}) + "\n"
        assert _read_cwd_from_dsh_jsonl(content) == "/w/s"

    def test_read_cwd_missing_returns_empty(self):
        assert _read_cwd_from_dsh_jsonl("") == ""
        assert _read_cwd_from_dsh_jsonl("not-json\n") == ""
        no_cwd = json.dumps({"type": "session", "id": "x"}) + "\n"
        assert _read_cwd_from_dsh_jsonl(no_cwd) == ""

    def test_path_resolvers(self):
        home = Path("/fake/home")
        assert _dsh_sessions_path(home) == home / ".dsh" / "sessions"
        assert _dsh_skills_path(home) == home / ".dsh" / "skills"

    def test_spec_fields(self):
        assert DSH_SPEC.name == "deepseek_harness"
        assert DSH_SPEC.adapter_format == "deepseek_harness_session_jsonl"
        assert DSH_SPEC.traj_id_prefix == "traj_dsh_"
        assert DSH_SPEC.sessions_glob == "*/*/session.jsonl*"  # plaintext and .zstd


# ──────────────────────────────────────────────────────────────────
# T5. install
# ──────────────────────────────────────────────────────────────────


class TestInstall:
    def test_install_creates_skill_md_at_dsh_skills_path(self, tmp_path):
        skill_path = _build_skill(tmp_path / "src")
        fake_home = tmp_path / "home"

        dest = install_to_deepseek_harness(skill_path, target_root=fake_home)

        assert dest == fake_home / ".dsh" / "skills" / "demo-skill" / "SKILL.md"
        assert dest.is_file()
        # 不装到共享的 .agents/skills（Codex/OpenCode/OpenClaw 战场，
        # 与 dsh 的 user-agents 扫描重叠，见 issue #214）
        assert not (fake_home / ".agents" / "skills").exists()

    def test_install_uses_symlink_on_posix(self, tmp_path):
        import sys
        if sys.platform == "win32":
            pytest.skip("Windows symlink happy path 需要 Dev Mode")
        skill_path = _build_skill(tmp_path / "src")
        fake_home = tmp_path / "home"
        install_to_deepseek_harness(skill_path, target_root=fake_home)

        dest_dir = fake_home / ".dsh" / "skills" / "demo-skill"
        assert dest_dir.is_symlink()
        assert dest_dir.resolve() == skill_path.resolve()

    def test_install_missing_skill_md_raises(self, tmp_path):
        empty = tmp_path / "empty-skill"
        empty.mkdir()
        with pytest.raises(FileNotFoundError):
            install_to_deepseek_harness(empty, target_root=tmp_path / "home")

    def test_watcher_installer_dict_includes_dsh(self):
        import inspect

        from xskill.pipeline.runner import DirectoryWatcher
        src = inspect.getsource(DirectoryWatcher._install_skill_to_all_detected)
        assert '"deepseek_harness": install_to_deepseek_harness' in src

    def test_team_daemon_installer_dict_includes_dsh(self):
        import inspect

        from xskill.team.client import daemon
        src = inspect.getsource(daemon.install_skill_to_ecosystems)
        assert '"deepseek_harness": install_to_deepseek_harness' in src

    def test_team_collector_starts_dsh_ingester(self, tmp_path, monkeypatch):
        """team 客户端的采集器（TeamCollector）必须接 dsh：connect 后自动把
        dsh 会话镜像进 ~/.xskill/dsh_sessions/（PR #243 评审在云机实测发现
        此前漏接）。"""
        import xskill.ecosystems as eco
        from xskill.team.client import collector as coll_mod

        (tmp_path / ".dsh").mkdir()
        started = []

        class _FakeIngester:
            def __init__(self, spec=None, *_a, **_kw):
                self.spec = spec

            def start(self):
                started.append(getattr(self.spec, "name", "?"))

        monkeypatch.setattr(eco, "JsonlIngester", _FakeIngester)
        c = coll_mod.TeamCollector.__new__(coll_mod.TeamCollector)
        c.home_root = tmp_path
        c.poll_interval = 5
        c._ingesters = []
        c.start_ingesters()
        assert "deepseek_harness" in started

    def test_harness_name_for_dsh_bridge_dir(self, tmp_path):
        """上传元数据里的 harness 必须是 deepseek_harness，不能退化为 dsh。"""
        from xskill.team.client.collector import _harness_for
        md = tmp_path / "dsh_sessions" / "traj_dsh_x.md"
        assert _harness_for(md) == "deepseek_harness"


# ──────────────────────────────────────────────────────────────────
# T6. JsonlIngester 不混淆 dsh 跟其他生态
# ──────────────────────────────────────────────────────────────────


class TestJsonlIngesterIsolation:
    def test_dsh_ingester_ignores_cursor_files(self, tmp_path):
        d = tmp_path / ".cursor" / "projects" / "c-x" / "agent-transcripts"
        d.mkdir(parents=True)
        (d / "sid.jsonl").write_text(
            '{"role": "user", "message": {"content": []}}\n', encoding="utf-8",
        )
        results = JsonlIngester(DSH_SPEC).scan_and_bridge(
            tmp_path / "traj", home_root=tmp_path,
        )
        assert results == []
