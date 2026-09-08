"""
test_cursor_adapter.py -- Cursor 接入端到端单测
=================================================

覆盖：

* **T1 cursor_transcripts_jsonl adapter** —— 从 fixture 解析 timeline +
  tool_names + first_user_query。
* **T2 ingest_cursor_sessions** —— tmp_path 模拟
  ``<home>/.cursor/projects/<encoded>/agent-transcripts/<sid>.jsonl`` 布局，
  扫盘 + 桥接，断言 traj_cursor_*.md 落地。
* **T3 detect_known_ecosystems** —— ``<home>/.cursor/projects/`` 存在即注册
  cursor bridge。
* **T4 helpers** —— path helpers / sid 抽取。
* **T5 Cursor 是 IDE 单向采集** —— 没有 install_to_cursor 函数；watcher 的
  installer 字典里也没 cursor 入口。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from xskill.ecosystems import (
    adapt_trajectory,
    CURSOR_SPEC,
    JsonlIngester,
    _cursor_projects_path,
    _cursor_session_id_from_path,
    _cursor_skills_path,
    _cwd_from_cursor_path,
    _read_cwd_from_cursor_jsonl,
    detect_known_ecosystems,
    ingest_cursor_sessions,
    install_to_cursor,
)
from xskill.skill.frontmatter import serialize as fm_serialize


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "cursor" / "sample_transcript.jsonl"


@pytest.fixture
def fixture_content() -> str:
    assert FIXTURE_PATH.is_file(), f"fixture missing: {FIXTURE_PATH}"
    return FIXTURE_PATH.read_text(encoding="utf-8")


def _place_fixture_in_cursor_home(
    home: Path, fixture_text: str,
    encoded_cwd: str = "c-proj-foo", sid: str = "abc12345",
) -> Path:
    transcripts = home / ".cursor" / "projects" / encoded_cwd / "agent-transcripts"
    transcripts.mkdir(parents=True, exist_ok=True)
    p = transcripts / f"{sid}.jsonl"
    p.write_text(fixture_text, encoding="utf-8")
    return p


def _build_cursor_skill(skill_root: Path, name: str = "demo-skill") -> Path:
    skill_path = skill_root / name
    skill_path.mkdir(parents=True, exist_ok=True)
    fm = {
        "name": name,
        "description": "A demo cursor skill.",
        "version": 1,
        "source_trajs": ["traj_cursor_0001"],
        "metadata": {"tags": ["demo"], "frozen": False},
    }
    (skill_path / "SKILL.md").write_text(
        fm_serialize(fm, f"# {name}\n\nBody.\n"), encoding="utf-8"
    )
    return skill_path


# ──────────────────────────────────────────────────────────────────
# T1. adapter
# ──────────────────────────────────────────────────────────────────


class TestCursorAdapter:
    def test_adapter_returns_markdown_with_user_assistant(self, fixture_content):
        md, meta = adapt_trajectory(fixture_content, "cursor_transcripts_jsonl")
        assert md.startswith("# Cursor Agent Trajectory")
        assert "## User" in md
        assert "## Assistant" in md
        assert "Fix the bug in foo.py" in md

    def test_adapter_extracts_first_user_query(self, fixture_content):
        _md, meta = adapt_trajectory(fixture_content, "cursor_transcripts_jsonl")
        assert meta["query"].startswith("Fix the bug in foo.py")

    def test_adapter_collects_tool_names(self, fixture_content):
        _md, meta = adapt_trajectory(fixture_content, "cursor_transcripts_jsonl")
        assert "read_file" in meta["tool_names"]
        assert "edit_file" in meta["tool_names"]

    def test_adapter_keeps_tool_input_on_timeline(self):
        raw = json.dumps({
            "role": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": "look"},
                    {
                        "type": "tool_use",
                        "name": "Shell",
                        "input": {"command": "ps aux", "working_directory": "/tmp"},
                    },
                ]
            },
        })
        md, meta = adapt_trajectory(raw + "\n", "cursor_transcripts_jsonl")
        assert "[tool_use: Shell command=ps aux]" in md
        tools = (meta["timeline"][0].get("tools") or [])
        assert tools and tools[0]["tool"] == "Shell"
        assert tools[0]["input"]["command"] == "ps aux"

    def test_adapter_counts_turns(self, fixture_content):
        _md, meta = adapt_trajectory(fixture_content, "cursor_transcripts_jsonl")
        # fixture 有 4 行：user / assistant+tool / assistant+tool / user
        assert meta["total_turns"] == 4

    def test_adapter_meta_fields(self, fixture_content):
        _md, meta = adapt_trajectory(fixture_content, "cursor_transcripts_jsonl")
        assert meta["source"] == "cursor_transcripts_jsonl"
        assert meta["category"] == "cursor_session"

    def test_adapter_handles_empty(self):
        md, meta = adapt_trajectory("", "cursor_transcripts_jsonl")
        assert "# Cursor Agent Trajectory" in md
        assert meta["total_turns"] == 0
        assert meta["tool_names"] == []

    def test_adapter_keeps_user_query_after_inlined_skill(self):
        dump = "SKILL " + ("x" * 3000)
        text = (
            f"<manually_attached_skills>\n{dump}\n</manually_attached_skills>\n"
            "<timestamp>Friday, Aug 28, 2026</timestamp>\n"
            "<user_query>\n问题第215号给出这样一个问题单\n</user_query>"
        )
        raw = json.dumps({
            "role": "user",
            "message": {"content": [{"type": "text", "text": text}]},
        })
        md, meta = adapt_trajectory(raw + "\n", "cursor_transcripts_jsonl")
        assert "问题第215号给出这样一个问题单" in md
        assert meta["query"] == "问题第215号给出这样一个问题单"
        assert "xxxxx" not in md

    def test_adapter_skips_malformed_lines(self):
        bad = "{not json\n" + json.dumps({
            "role": "user", "message": {"content": [{"type": "text", "text": "ok"}]},
        }) + "\n"
        _md, meta = adapt_trajectory(bad, "cursor_transcripts_jsonl")
        assert meta["total_turns"] == 1


# ──────────────────────────────────────────────────────────────────
# T2. ingest
# ──────────────────────────────────────────────────────────────────


class TestIngestCursorSessions:
    def test_ingest_writes_traj_with_cursor_prefix(self, tmp_path, fixture_content):
        _place_fixture_in_cursor_home(tmp_path, fixture_content)
        traj_dir = tmp_path / "traj"

        results = ingest_cursor_sessions(traj_dir, home_root=tmp_path)

        assert len(results) == 1
        rec = results[0]
        assert rec["status"] == "stored"
        assert rec["session_id"] == "abc12345"
        assert rec["ecosystem"] == "cursor"
        assert rec["traj_id"] == "traj_cursor_c-proj-foo_abc12345"

        md_path = Path(rec["path"])
        assert md_path.is_file()

        meta_path = md_path.with_suffix(".json")
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        assert meta["category"] == "cursor_session"
        assert "Fix the bug" in meta["query"]

    def test_ingest_idempotent(self, tmp_path, fixture_content):
        _place_fixture_in_cursor_home(tmp_path, fixture_content)
        traj_dir = tmp_path / "traj"
        seen: set[str] = set()

        first = ingest_cursor_sessions(traj_dir, home_root=tmp_path, seen_sessions=seen)
        assert len(first) == 1
        assert "abc12345" in seen

        second = ingest_cursor_sessions(traj_dir, home_root=tmp_path, seen_sessions=seen)
        assert second == []

    def test_ingest_skips_when_dir_absent(self, tmp_path):
        results = ingest_cursor_sessions(tmp_path / "traj", home_root=tmp_path)
        assert results == []

    def test_ingest_finds_across_multiple_projects(self, tmp_path, fixture_content):
        # 两个不同 encoded-cwd 项目各放一个 session，都应被扫到
        _place_fixture_in_cursor_home(tmp_path, fixture_content,
                                       encoded_cwd="c-proj-A", sid="aaaa1111")
        _place_fixture_in_cursor_home(tmp_path, fixture_content,
                                       encoded_cwd="c-proj-B", sid="bbbb2222")

        results = ingest_cursor_sessions(tmp_path / "traj", home_root=tmp_path)
        sids = sorted(r["session_id"] for r in results)
        assert sids == ["aaaa1111", "bbbb2222"]
        assert {r["traj_id"] for r in results} == {
            "traj_cursor_c-proj-A_aaaa1111",
            "traj_cursor_c-proj-B_bbbb2222",
        }

    def test_run_once_restores_seen_and_is_idempotent(self, tmp_path, fixture_content):
        _place_fixture_in_cursor_home(tmp_path, fixture_content)
        target = tmp_path / "traj"
        first = JsonlIngester(
            CURSOR_SPEC, target_traj_dir=target, home_root=tmp_path,
            settle_seconds=0.0,
        ).run_once()
        assert len(first) == 1
        second = JsonlIngester(
            CURSOR_SPEC, target_traj_dir=target, home_root=tmp_path,
            settle_seconds=0.0,
        ).run_once()
        assert second == []

    def test_ingest_finds_nested_transcript_layout(self, tmp_path, fixture_content):
        transcripts = (
            tmp_path / ".cursor" / "projects" / "home-admin-xskill"
            / "agent-transcripts" / "sid-nested"
        )
        transcripts.mkdir(parents=True)
        (transcripts / "sid-nested.jsonl").write_text(fixture_content, encoding="utf-8")
        results = ingest_cursor_sessions(tmp_path / "traj", home_root=tmp_path)
        assert len(results) == 1
        assert results[0]["traj_id"] == "traj_cursor_home-admin-xskill_sid-nest"


# ──────────────────────────────────────────────────────────────────
# T3. detect
# ──────────────────────────────────────────────────────────────────


class TestDetectCursor:
    def test_detect_reports_cursor_when_projects_dir_exists(self, tmp_path):
        (tmp_path / ".cursor" / "projects").mkdir(parents=True)

        found = detect_known_ecosystems(home_root=tmp_path)
        ecos = {r["ecosystem"] for r in found}
        assert "cursor" in ecos

        rec = next(r for r in found if r["ecosystem"] == "cursor")
        assert rec["source"] == (tmp_path / ".cursor" / "projects").resolve()
        assert rec["bridge"] == (tmp_path / ".xskill" / "cursor_sessions").resolve()

    def test_detect_skips_when_absent(self, tmp_path):
        found = detect_known_ecosystems(home_root=tmp_path)
        ecos = {r["ecosystem"] for r in found}
        assert "cursor" not in ecos


# ──────────────────────────────────────────────────────────────────
# T4. helpers
# ──────────────────────────────────────────────────────────────────


class TestCursorHelpers:
    def test_session_id_from_path(self, tmp_path):
        p = tmp_path / "abc12345.jsonl"
        assert _cursor_session_id_from_path(p) == "abc12345"

    def test_read_cwd_returns_empty(self):
        """Cursor JSONL 内容里没有 cwd——cwd_from_content 永远返回空串。"""
        line = json.dumps({"role": "user", "message": {"content": []}})
        assert _read_cwd_from_cursor_jsonl(line) == ""

    def test_cwd_from_path_reads_encoded_project(self, tmp_path):
        nested = (
            tmp_path / "projects" / "home-admin-xskill" / "agent-transcripts"
            / "sid" / "sid.jsonl"
        )
        nested.parent.mkdir(parents=True)
        nested.write_text("{}\n", encoding="utf-8")
        assert _cwd_from_cursor_path(nested) == "home-admin-xskill"
        flat = tmp_path / "projects" / "c-proj-foo" / "agent-transcripts" / "abc.jsonl"
        flat.parent.mkdir(parents=True)
        flat.write_text("{}\n", encoding="utf-8")
        assert _cwd_from_cursor_path(flat) == "c-proj-foo"

    def test_path_resolvers(self):
        home = Path("/fake/home")
        assert _cursor_projects_path(home) == home / ".cursor" / "projects"
        assert _cursor_skills_path(home) == home / ".cursor" / "skills"

    def test_spec_fields(self):
        assert CURSOR_SPEC.name == "cursor"
        assert CURSOR_SPEC.source_kind == "jsonl"
        assert CURSOR_SPEC.adapter_format == "cursor_transcripts_jsonl"
        assert CURSOR_SPEC.traj_id_prefix == "traj_cursor_"
        assert CURSOR_SPEC.sessions_glob == "*/agent-transcripts/**/*.jsonl"
        assert CURSOR_SPEC.cwd_from_path is _cwd_from_cursor_path
        # skills 装到 ~/.cursor/skills/，不是 ~/.agents/skills/
        home = Path("/fake/home")
        assert CURSOR_SPEC.skills_install_path(home) == home / ".cursor" / "skills"


# ──────────────────────────────────────────────────────────────────
# T5. install_to_cursor —— skill 装到 ~/.cursor/skills/<name>
# ──────────────────────────────────────────────────────────────────


class TestInstallToCursor:
    """Cursor 有 skill 目录 ~/.cursor/skills/，cursor_setup.ps1 用 junction
    把整个目录链到源仓；本函数走 per-skill symlink 跟 CC 同形。
    """

    def test_install_creates_skill_md_at_cursor_skills_path(self, tmp_path):
        skill_path = _build_cursor_skill(tmp_path / "src")
        fake_home = tmp_path / "home"

        dest = install_to_cursor(skill_path, target_root=fake_home)

        assert dest == fake_home / ".cursor" / "skills" / "demo-skill" / "SKILL.md"
        assert dest.is_file()

        # 不应装到 .agents/skills（那是 codex/opencode/openclaw 共用目录）
        assert not (fake_home / ".agents" / "skills").exists()

    def test_install_uses_symlink_on_posix(self, tmp_path):
        import sys
        if sys.platform == "win32":
            pytest.skip("Windows symlink happy path 需要 Dev Mode")
        skill_path = _build_cursor_skill(tmp_path / "src")
        fake_home = tmp_path / "home"
        install_to_cursor(skill_path, target_root=fake_home)

        dest_dir = fake_home / ".cursor" / "skills" / "demo-skill"
        assert dest_dir.is_symlink()
        assert dest_dir.resolve() == skill_path.resolve()

    def test_install_missing_skill_md_raises(self, tmp_path):
        empty = tmp_path / "empty-skill"
        empty.mkdir()
        with pytest.raises(FileNotFoundError):
            install_to_cursor(empty, target_root=tmp_path / "home")

    def test_watcher_installer_dict_includes_cursor(self):
        """watcher._install_skill_to_all_detected 的 installer 字典含 cursor。"""
        import inspect
        from xskill.pipeline.runner import DirectoryWatcher
        src = inspect.getsource(DirectoryWatcher._install_skill_to_all_detected)
        assert '"cursor": install_to_cursor' in src


# ──────────────────────────────────────────────────────────────────
# T6. JsonlIngester 不混淆 cursor 跟其他生态
# ──────────────────────────────────────────────────────────────────


class TestJsonlIngesterIsolation:
    def test_cursor_ingester_ignores_openclaw_files(self, tmp_path):
        d = tmp_path / ".openclaw" / "agents" / "main" / "sessions"
        d.mkdir(parents=True)
        (d / "xxx.trajectory.jsonl").write_text(
            '{"traceSchema":"openclaw-trajectory","type":"session.started"}\n',
            encoding="utf-8",
        )
        results = JsonlIngester(CURSOR_SPEC).scan_and_bridge(
            tmp_path / "traj", home_root=tmp_path,
        )
        assert results == []


# ──────────────────────────────────────────────────────────────────
# T4. slug → 真实项目目录（sidecar cwd，供 xskill privacy 按项目放行）
# ──────────────────────────────────────────────────────────────────

def _slug_for(path: Path) -> str:
    return str(path.resolve()).strip("/").replace("/", "-").lower()


class TestCursorProjectDir:
    def test_decodes_hyphenated_dir_names_by_checking_disk(self, tmp_path):
        from xskill.ecosystems.cursor import _decode_cursor_slug
        project = tmp_path / "my-service" / "backend-api"
        project.mkdir(parents=True)
        assert _decode_cursor_slug(_slug_for(project)) == str(project.resolve())

    def test_prefers_existing_layout_when_slug_is_ambiguous(self, tmp_path):
        from xskill.ecosystems.cursor import _decode_cursor_slug
        (tmp_path / "gq" / "code").mkdir(parents=True)
        (tmp_path / "gq-code").mkdir()
        assert _decode_cursor_slug(_slug_for(tmp_path / "gq-code")) == str((tmp_path / "gq-code").resolve())
        (tmp_path / "gq-code").rmdir()
        assert _decode_cursor_slug(_slug_for(tmp_path / "gq" / "code")) == str((tmp_path / "gq" / "code").resolve())

    def test_matches_case_insensitively(self, tmp_path):
        from xskill.ecosystems.cursor import _decode_cursor_slug
        project = tmp_path / "MyProj"
        project.mkdir()
        assert _decode_cursor_slug(_slug_for(project)) == str(project.resolve())

    def test_returns_empty_when_project_no_longer_exists(self, tmp_path):
        from xskill.ecosystems.cursor import _decode_cursor_slug
        assert _decode_cursor_slug(_slug_for(tmp_path / "gone-project")) == ""
        assert _decode_cursor_slug("") == ""

    def test_ingest_writes_real_cwd_into_sidecar(self, tmp_path, fixture_content):
        project = tmp_path / "code" / "my-service"
        project.mkdir(parents=True)
        _place_fixture_in_cursor_home(tmp_path, fixture_content, encoded_cwd=_slug_for(project))
        results = ingest_cursor_sessions(tmp_path / "traj", home_root=tmp_path)
        assert len(results) == 1
        meta = json.loads(Path(results[0]["path"]).with_suffix(".json").read_text(encoding="utf-8"))
        assert meta["cwd"] == str(project.resolve())
        from xskill.ecosystems._shared import _sanitize_for_filename
        expected_project = _sanitize_for_filename(_slug_for(project), maxlen=32)
        assert results[0]["traj_id"] == f"traj_cursor_{expected_project}_abc12345"

    def test_ingest_leaves_cwd_absent_when_project_missing(self, tmp_path, fixture_content):
        _place_fixture_in_cursor_home(tmp_path, fixture_content, encoded_cwd="c-proj-foo")
        results = ingest_cursor_sessions(tmp_path / "traj", home_root=tmp_path)
        meta = json.loads(Path(results[0]["path"]).with_suffix(".json").read_text(encoding="utf-8"))
        assert "cwd" not in meta
        assert results[0]["traj_id"] == "traj_cursor_c-proj-foo_abc12345"
