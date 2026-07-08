"""
test_nga3_adapter.py -- nga3 / CodeAgent3 生态测试
=================================================
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from xskill.ecosystems import JsonlIngester, adapt_trajectory, detect_known_ecosystems
from xskill.ecosystems.nga3 import (
    NGA3_SPEC,
    _nga3_projects_path,
    _nga3_session_id_from_path,
    _nga3_skills_path,
    _read_cwd_from_nga3_jsonl_content,
    ingest_nga3_sessions,
    install_to_nga3,
)


def _nga3_jsonl_sample() -> str:
    return "\n".join([
        json.dumps({
            "type": "file-history-snapshot",
            "messageId": "snap-1",
            "snapshot": {"messageId": "snap-1", "trackedFileBackups": {}},
            "isSnapshotUpdate": False,
        }),
        json.dumps({
            "parentUuid": None,
            "isSidechain": False,
            "type": "user",
            "message": {
                "role": "user",
                "content": "<local-command-caveat>hello world message</local-command-caveat>",
            },
            "isMeta": True,
            "uuid": "meta-user",
            "timestamp": "2026-07-07T07:36:48.532Z",
            "userType": "external",
            "entrypoint": "cli",
            "cwd": r"D:\02-code\nga-xx",
            "sessionId": "sess-nga3",
            "version": "1.2605.02-IN.1",
            "gitBranch": "HEAD",
        }),
        json.dumps({
            "parentUuid": "meta-user",
            "isSidechain": False,
            "promptId": "prompt-uuid",
            "type": "user",
            "message": {"role": "user", "content": "列出当前目录下所有 txt 文件"},
            "uuid": "user-1",
            "timestamp": "2026-07-07T07:40:36.191Z",
            "permissionMode": "default",
            "userType": "external",
            "entrypoint": "cli",
            "cwd": r"D:\02-code\nga-xx",
            "sessionId": "sess-nga3",
            "version": "1.2605.02-IN.1",
            "gitBranch": "HEAD",
        }),
        json.dumps({
            "parentUuid": "user-1",
            "isSidechain": False,
            "message": {
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "I will glob files", "signature": "sig"},
                    {
                        "type": "tool_use",
                        "id": "call_glob",
                        "name": "Glob",
                        "input": {"pattern": "**/*.txt"},
                        "caller": {"type": "direct"},
                    },
                ],
                "model": "Glm-5.1",
            },
            "type": "assistant",
            "uuid": "assistant-1",
            "timestamp": "2026-07-07T07:40:42.171Z",
            "_cac_providerId": "openai",
            "_cac_modelId": "Glm-5.1",
            "_cac_agentType": "main",
            "_cac_promptId": "prompt_internal",
            "userType": "external",
            "entrypoint": "cli",
            "cwd": r"D:\02-code\nga-xx",
            "sessionId": "sess-nga3",
            "version": "1.2605.02-IN.1",
            "gitBranch": "HEAD",
        }),
        json.dumps({
            "parentUuid": "assistant-1",
            "isSidechain": False,
            "promptId": "prompt-uuid",
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {
                        "tool_use_id": "call_glob",
                        "type": "tool_result",
                        "content": "hello.txt",
                    }
                ],
            },
            "uuid": "tool-result-1",
            "timestamp": "2026-07-07T07:40:42.212Z",
            "toolUseResult": {
                "filenames": ["hello.txt"],
                "durationMs": 12,
                "numFiles": 1,
                "truncated": False,
            },
            "sourceToolAssistantUUID": "assistant-1",
            "userType": "external",
            "entrypoint": "cli",
            "cwd": r"D:\02-code\nga-xx",
            "sessionId": "sess-nga3",
            "version": "1.2605.02-IN.1",
            "gitBranch": "HEAD",
            "slug": "indexed-twirling-reddy",
        }),
        json.dumps({
            "parentUuid": "tool-result-1",
            "isSidechain": False,
            "message": {
                "id": "msg_2",
                "type": "message",
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "call_read",
                        "name": "Read",
                        "input": {"file_path": r"D:\02-code\nga-xx\hello.txt"},
                        "caller": {"type": "direct"},
                    }
                ],
                "model": "Glm-5.1",
            },
            "type": "assistant",
            "uuid": "assistant-2",
            "timestamp": "2026-07-07T07:40:45.220Z",
            "_cac_providerId": "openai",
            "_cac_modelId": "Glm-5.1",
            "_cac_agentType": "main",
            "_cac_promptId": "prompt_internal_2",
            "cwd": r"D:\02-code\nga-xx",
            "sessionId": "sess-nga3",
        }),
        json.dumps({
            "parentUuid": "assistant-2",
            "isSidechain": False,
            "promptId": "prompt-uuid",
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "content": "File content exceeds maximum allowed tokens",
                        "is_error": True,
                        "tool_use_id": "call_read",
                    }
                ],
            },
            "uuid": "tool-result-2",
            "timestamp": "2026-07-07T07:40:45.562Z",
            "toolUseResult": {
                "type": "Error",
                "message": "File content exceeds maximum allowed tokens",
            },
            "sourceToolAssistantUUID": "assistant-2",
            "cwd": r"D:\02-code\nga-xx",
            "sessionId": "sess-nga3",
            "slug": "indexed-twirling-reddy",
        }),
    ])


def _build_skill(root: Path, name: str = "demo-skill") -> Path:
    skill_path = root / name
    skill_path.mkdir(parents=True)
    (skill_path / "SKILL.md").write_text("# demo skill\n", encoding="utf-8")
    return skill_path


class TestNga3Adapter:
    def test_adapter_accepts_nga3_jsonl_format(self):
        md, meta = adapt_trajectory(_nga3_jsonl_sample(), "nga3_jsonl")

        assert "# NGA3 Session Trajectory" in md
        assert "## Initial Query" in md
        assert "列出当前目录下所有 txt 文件" in md
        assert "Tool Call: Glob" in md
        assert "Tool Output: Glob" in md
        assert "Tool Output: Read (error)" in md
        assert "local-command-caveat" not in md

        assert meta["category"] == "nga3_session"
        assert meta["source"] == "nga3_session_jsonl"
        assert meta["session_id"] == "sess-nga3"
        assert meta["cwd"] == r"D:\02-code\nga-xx"
        assert meta["git_branch"] == "HEAD"
        assert meta["model"] == "Glm-5.1"
        assert meta["provider"] == "openai"
        assert meta["agent_type"] == "main"
        assert meta["internal_prompt_id"] == "prompt_internal"
        assert meta["prompt_id"] == "prompt-uuid"
        assert meta["permission_mode"] == "default"
        assert meta["client_version"] == "1.2605.02-IN.1"
        assert meta["entrypoint"] == "cli"
        assert meta["user_type"] == "external"
        assert meta["slug"] == "indexed-twirling-reddy"
        assert meta["thinking_blocks"] == 1
        assert meta["tool_names"] == ["Glob", "Read"]
        assert meta["total_tool_calls"] == 2
        assert meta["tool_calls"][0]["caller"] == {"type": "direct"}
        assert meta["tool_calls"][1]["is_error"] is True
        assert any(e["type"] == "local-command-caveat" for e in meta["meta_events"])

    def test_zcode_alias_uses_same_adapter(self):
        md, meta = adapt_trajectory(_nga3_jsonl_sample(), "zcode_jsonl")
        assert "# NGA3 Session Trajectory" in md
        assert meta["category"] == "nga3_session"

    def test_read_cwd_normalizes_windows_path_for_traj_id(self):
        assert _read_cwd_from_nga3_jsonl_content(_nga3_jsonl_sample()) == "D:/02-code/nga-xx"


class TestNga3DetectAndIngest:
    def test_detect_reports_nga3_when_cac_projects_exists(self, tmp_path):
        (tmp_path / ".cac" / "projects").mkdir(parents=True)

        found = detect_known_ecosystems(home_root=tmp_path)
        ecos = {r["ecosystem"] for r in found}
        assert "nga3" in ecos

        rec = next(r for r in found if r["ecosystem"] == "nga3")
        assert rec["source"] == (tmp_path / ".cac" / "projects").resolve()
        assert rec["bridge"] == (tmp_path / ".xskill" / "nga3_sessions").resolve()

    def test_ingest_nga3_sessions_bridges_cac_project_jsonl(self, tmp_path):
        src_dir = tmp_path / ".cac" / "projects" / "D--02-code-nga-xx"
        src_dir.mkdir(parents=True)
        (src_dir / "sess-nga3.jsonl").write_text(
            _nga3_jsonl_sample(), encoding="utf-8",
        )

        bridge = tmp_path / "bridge"
        ingester = JsonlIngester(NGA3_SPEC, settle_seconds=0)
        submitted = ingester.scan_and_bridge(bridge, home_root=tmp_path)

        assert len(submitted) == 1
        assert submitted[0]["session_id"] == "sess-nga3"
        md_path = Path(submitted[0]["path"])
        assert md_path.name.startswith("traj_nga3_nga-xx_sess-nga")
        assert md_path.is_file()

        meta = json.loads(md_path.with_suffix(".json").read_text(encoding="utf-8"))
        assert meta["category"] == "nga3_session"
        assert meta["model"] == "Glm-5.1"

    def test_ingest_wrapper_uses_nga3_spec(self, tmp_path):
        src_dir = tmp_path / ".cac" / "projects" / "D--02-code-nga-xx"
        src_dir.mkdir(parents=True)
        (src_dir / "sess-nga3.jsonl").write_text(
            _nga3_jsonl_sample(), encoding="utf-8",
        )

        submitted = ingest_nga3_sessions(
            tmp_path / "bridge", home_root=tmp_path, seen_sessions=set(),
        )

        assert len(submitted) == 1
        assert submitted[0]["ecosystem"] == "nga3"


class TestNga3Installer:
    def test_path_resolvers(self):
        home = Path("/fake/home")
        assert _nga3_projects_path(home) == home / ".cac" / "projects"
        assert _nga3_skills_path(home) == home / ".cac" / "skills"
        assert _nga3_session_id_from_path(Path("abc123.jsonl")) == "abc123"

    def test_spec_fields(self):
        assert NGA3_SPEC.name == "nga3"
        assert NGA3_SPEC.source_kind == "jsonl"
        assert NGA3_SPEC.sessions_glob == "*/*.jsonl"
        assert NGA3_SPEC.adapter_format == "nga3_jsonl"
        assert NGA3_SPEC.traj_id_prefix == "traj_nga3_"
        assert NGA3_SPEC.skills_install_path(Path("/fake/home")) == (
            Path("/fake/home") / ".cac" / "skills"
        )

    def test_install_creates_skill_md_at_cac_skills_path(self, tmp_path):
        skill_path = _build_skill(tmp_path / "src")
        fake_home = tmp_path / "home"

        dest = install_to_nga3(skill_path, target_root=fake_home)

        assert dest == fake_home / ".cac" / "skills" / "demo-skill" / "SKILL.md"
        assert dest.is_file()
        assert dest.read_text(encoding="utf-8") == "# demo skill\n"
        assert not (fake_home / ".agents" / "skills").exists()

    def test_install_missing_skill_md_raises(self, tmp_path):
        empty = tmp_path / "empty-skill"
        empty.mkdir()
        with pytest.raises(FileNotFoundError):
            install_to_nga3(empty, target_root=tmp_path / "home")

    def test_watcher_installer_dict_includes_nga3(self):
        import inspect
        from xskill.pipeline.runner import DirectoryWatcher

        src = inspect.getsource(DirectoryWatcher._install_skill_to_all_detected)
        assert '"nga3": install_to_nga3' in src
