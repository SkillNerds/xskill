"""`xskill search` 客户端安装结果与人读输出测试。"""
from __future__ import annotations

import io
import json
import os
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from xskill import cli
from xskill.team.client.search_slots import SearchSlots


class _Response:
    def __init__(self, status_code: int, *, json_data: dict | None = None,
                 content: bytes = b"", text: str = "",
                 headers: dict | None = None):
        self.status_code = status_code
        self._json_data = json_data
        self.content = content
        self.text = text
        self.headers = headers or {}

    def json(self) -> dict:
        return self._json_data or {}


class _SearchHttp:
    def __init__(self, results: list[dict]):
        self.results = results

    def get(self, path: str, **_kwargs) -> _Response:
        if "search" in path:
            return _Response(200, json_data={"results": self.results})
        skill_id = path.split("/")[-2]
        name = next(
            result["display_name"]
            for result in self.results
            if result["skill_id"] == skill_id
        )
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as zip_file:
            zip_file.writestr(
                "SKILL.md",
                f"---\nname: {name}\ndescription: test\n---\nbody\n",
            )
        return _Response(200, content=archive.getvalue())


def _result(name: str, *, source: str = "skillhub",
            source_path: str | None = None) -> dict:
    return {
        "skill_id": f"{name}@abcdef",
        "display_name": name,
        "description": "  描述中有\n连续的   空白  ",
        "content_sha": "ab",
        "source_path": source_path or f"agentcenter_hub/skills/{name}",
        "source": source,
        "ux_avg": 8.6,
        "match": {"bm25_rank": 1, "semantic_rank": 2},
    }


def _install_home(monkeypatch, tmp_path: Path, home: Path) -> None:
    monkeypatch.setattr(
        "xskill.team.client.search_slots.SearchSlots",
        lambda **_kwargs: SearchSlots(
            xskill_home=tmp_path / "xskill-home", home_root=home,
        ),
    )


def test_only_detected_ngagent_and_nga3_are_printed(
    tmp_path, monkeypatch, capsys,
):
    home = tmp_path / "home"
    (home / ".cac" / "projects").mkdir(parents=True)
    ngagent_db = home / ".local" / "share" / "opencode" / "db" / "ngagent.db"
    ngagent_db.parent.mkdir(parents=True)
    ngagent_db.touch()
    _install_home(monkeypatch, tmp_path, home)
    results = [
        _result("repo-skill", source="repo", source_path="repo/repo-skill"),
        _result(
            "upload-skill", source="上传者:alice",
            source_path="user_skill_hub/alice/upload-skill",
        ),
    ]

    return_code = cli.cmd_search_hub(
        SimpleNamespace(terms=["openqa"], top_k=5, json=False),
        http=_SearchHttp(results), headers={},
    )

    output = capsys.readouterr().out
    assert return_code == 0
    assert "CodeAgent3 / NGA3" in output
    assert "NGAgent" in output
    assert str(home / ".cac" / "skills" / "repo-skill@abcdef") in output
    assert str(
        home / ".config" / "opencode" / "skills" / "repo-skill@abcdef"
    ) in output
    assert "Claude Code" not in output
    assert "Codex" not in output
    assert "OpenCode" not in output
    assert "OpenClaw" not in output
    assert not (home / ".agents").exists()
    assert not (home / ".claude").exists()
    assert "XSkill 自蒸馏生成" in output
    assert "上传者:alice（用户上传）" in output
    assert "描述中有 连续的 空白" in output
    assert "\n" + "-" * 64 + "\n" in output


def test_shared_target_keeps_each_harness_record(tmp_path):
    home = tmp_path / "home"
    (home / ".codex" / "sessions").mkdir(parents=True)
    opencode_db = home / ".local" / "share" / "opencode" / "opencode.db"
    opencode_db.parent.mkdir(parents=True)
    opencode_db.touch()
    (home / ".openclaw" / "agents").mkdir(parents=True)
    slots = SearchSlots(
        xskill_home=tmp_path / "xskill-home", home_root=home,
    )
    result = _result("shared")
    archive = _SearchHttp([result]).get(
        f"/api/v1/team/skill/{result['skill_id']}/bundle"
    ).content

    details = slots.install(
        result, archive, query="shared", return_details=True,
    )

    records = list(details["installations"])
    shared_target = str(home / ".agents" / "skills" / result["skill_id"])
    shared_records = [
        record for record in records if record["target"] == shared_target
    ]
    assert {record["ecosystem"] for record in shared_records} == {
        "codex", "opencode", "openclaw",
    }
    assert all(record["status"] == "installed" for record in shared_records)
    assert all(record["mode"] == "copy" for record in shared_records)


def test_detected_install_failure_is_visible(
    tmp_path, monkeypatch, capsys,
):
    home = tmp_path / "home"
    (home / ".cac" / "projects").mkdir(parents=True)
    ngagent_db = home / ".local" / "share" / "opencode" / "db" / "ngagent.db"
    ngagent_db.parent.mkdir(parents=True)
    ngagent_db.touch()
    _install_home(monkeypatch, tmp_path, home)

    def fail_ngagent(*_args, **_kwargs):
        raise PermissionError("target is read-only")

    monkeypatch.setattr(
        "xskill.ecosystems.install_to_ngagent", fail_ngagent,
    )
    result = _result("partial")

    return_code = cli.cmd_search_hub(
        SimpleNamespace(terms=["partial"], top_k=5, json=False),
        http=_SearchHttp([result]), headers={},
    )

    output = capsys.readouterr().out
    assert return_code == 0
    assert "[失败] NGAgent 安装失败" in output
    assert "目标目录不可写，请检查目录权限" in output
    assert "target is read-only" not in output
    assert "[成功] CodeAgent3 / NGA3" in output


def test_json_keeps_path_and_adds_cache_path_and_installations(
    tmp_path, monkeypatch, capsys,
):
    home = tmp_path / "home"
    _install_home(monkeypatch, tmp_path, home)
    result = _result("json-skill")

    return_code = cli.cmd_search_hub(
        SimpleNamespace(terms=["json"], top_k=5, json=True),
        http=_SearchHttp([result]), headers={},
    )

    rows = json.loads(capsys.readouterr().out)
    assert return_code == 0
    assert rows[0]["path"] == rows[0]["cache_path"]
    assert rows[0]["installations"] == []
    assert rows[0]["match"] == result["match"]
    assert rows[0]["source"] == result["source"]


def test_human_output_is_encodable_by_real_cp936_stream(monkeypatch):
    output_bytes = io.BytesIO()
    cp936_stdout = io.TextIOWrapper(
        output_bytes, encoding="cp936", errors="strict",
    )
    monkeypatch.setattr(sys, "stdout", cp936_stdout)
    row = _result("windows-\N{GRINNING FACE}")
    row["name"] = row["display_name"]
    row["description"] = "包含 emoji \N{ROCKET} 的描述"
    row["source_path"] = "user_skill_hub/\N{CAT FACE}/windows-output"
    row["installations"] = [{
        "ecosystem": "ngagent",
        "target": (
            "C:\\Users\\\N{GRINNING FACE}\\.config\\opencode\\skills"
            "\\windows-output"
        ),
        "status": "installed",
        "mode": "copy",
    }, {
        "ecosystem": "nga3",
        "target": r"C:\Users\tester\.cac\skills\windows-output",
        "status": "failed",
        "error_code": "TARGET_PERMISSION_DENIED",
        "error": "目标目录不可写，请检查目录权限",
    }]

    cli._render_search_results([row], "openqa \N{FIRE}")
    cp936_stdout.flush()

    rendered = output_bytes.getvalue().decode("cp936")
    assert "[成功] NGAgent [copy]" in rendered
    assert "[失败] CodeAgent3 / NGA3 安装失败" in rendered
    assert "\\U0001f600" in rendered
    assert "\\U0001f680" in rendered
    assert "\\U0001f431" in rendered
    assert "\\U0001f525" in rendered


def test_install_exception_secret_never_enters_output_or_ledger(
    tmp_path, monkeypatch, capsys, caplog,
):
    home = tmp_path / "home"
    ngagent_db = home / ".local" / "share" / "opencode" / "db" / "ngagent.db"
    ngagent_db.parent.mkdir(parents=True)
    ngagent_db.touch()
    _install_home(monkeypatch, tmp_path, home)

    def fail_with_secret(*_args, **_kwargs):
        raise RuntimeError(
            "Authorization: Bearer very-secret-token /root/private/path"
        )

    monkeypatch.setattr(
        "xskill.ecosystems.install_to_ngagent", fail_with_secret,
    )
    caplog.set_level("WARNING", logger="xskill.team.client")
    result = _result("safe-error")

    return_code = cli.cmd_search_hub(
        SimpleNamespace(terms=["safe"], top_k=5, json=False),
        http=_SearchHttp([result]), headers={},
    )

    output = capsys.readouterr().out
    ledger_text = (
        tmp_path / "xskill-home" / "search_slots.json"
    ).read_text(encoding="utf-8")
    assert return_code == 0
    assert "Authorization" not in output
    assert "very-secret-token" not in output
    assert "/root/private/path" not in output
    assert "Authorization" not in ledger_text
    assert "very-secret-token" not in ledger_text
    assert "/root/private/path" not in ledger_text
    assert "Authorization" not in caplog.text
    assert "very-secret-token" not in caplog.text
    assert "/root/private/path" not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)
    record = json.loads(ledger_text)[0]["installations"][0]
    assert record["error_code"] == "INSTALLER_ERROR"
    assert record["error"] == "安装器执行失败，请查看本机 xskill 日志"


def test_stale_copy_is_not_current_when_new_version_install_fails(
    tmp_path, monkeypatch,
):
    home = tmp_path / "home"
    ngagent_db = home / ".local" / "share" / "opencode" / "db" / "ngagent.db"
    ngagent_db.parent.mkdir(parents=True)
    ngagent_db.touch()
    slots = SearchSlots(
        xskill_home=tmp_path / "xskill-home", home_root=home,
    )
    first_result = _result("versioned")
    first_archive = io.BytesIO()
    with zipfile.ZipFile(first_archive, "w") as zip_file:
        zip_file.writestr(
            "SKILL.md",
            "---\nname: versioned\ndescription: test\n---\nversion one\n",
        )
    slots.install(
        first_result, first_archive.getvalue(), query="versioned",
    )
    target = (
        home / ".config" / "opencode" / "skills"
        / first_result["skill_id"]
    )

    def fail_new_copy(*_args, **_kwargs):
        raise PermissionError("Authorization: Bearer update-secret")

    monkeypatch.setattr(
        "xskill.ecosystems.install_to_ngagent", fail_new_copy,
    )
    second_result = dict(first_result)
    second_result["content_sha"] = "cd"
    second_archive = io.BytesIO()
    with zipfile.ZipFile(second_archive, "w") as zip_file:
        zip_file.writestr(
            "SKILL.md",
            "---\nname: versioned\ndescription: test\n---\nversion two\n",
        )

    details = slots.install(
        second_result, second_archive.getvalue(),
        query="versioned", return_details=True,
    )

    record = details["installations"][0]
    assert "version one" in (
        target / "SKILL.md"
    ).read_text(encoding="utf-8")
    assert "version two" in (
        Path(details["cache_path"]) / "SKILL.md"
    ).read_text(encoding="utf-8")
    assert record["status"] == "failed"
    assert record["error_code"] == "TARGET_PERMISSION_DENIED"
    serialized = json.dumps(record, ensure_ascii=False)
    assert "update-secret" not in serialized
    assert "Authorization" not in serialized


def test_stale_copy_auxiliary_file_is_not_current_when_reinstall_fails(
    tmp_path, monkeypatch,
):
    home = tmp_path / "home"
    ngagent_db = home / ".local" / "share" / "opencode" / "db" / "ngagent.db"
    ngagent_db.parent.mkdir(parents=True)
    ngagent_db.touch()
    slots = SearchSlots(
        xskill_home=tmp_path / "xskill-home", home_root=home,
    )
    result = _result("aux-versioned")
    first_archive = io.BytesIO()
    with zipfile.ZipFile(first_archive, "w") as zip_file:
        zip_file.writestr(
            "SKILL.md",
            "---\nname: aux-versioned\ndescription: test\n---\nsame body\n",
        )
        zip_file.writestr("references/note.md", "version one\n")
    slots.install(result, first_archive.getvalue(), query="aux")
    target = (
        home / ".config" / "opencode" / "skills"
        / result["skill_id"]
    )

    def fail_new_copy(*_args, **_kwargs):
        raise PermissionError("Authorization: Bearer auxiliary-secret")

    monkeypatch.setattr(
        "xskill.ecosystems.install_to_ngagent", fail_new_copy,
    )
    second_archive = io.BytesIO()
    with zipfile.ZipFile(second_archive, "w") as zip_file:
        zip_file.writestr(
            "SKILL.md",
            "---\nname: aux-versioned\ndescription: test\n---\nsame body\n",
        )
        zip_file.writestr("references/note.md", "version two\n")

    details = slots.install(
        result, second_archive.getvalue(),
        query="aux", return_details=True,
    )

    record = details["installations"][0]
    assert (target / "references" / "note.md").read_text(
        encoding="utf-8",
    ) == "version one\n"
    assert (
        Path(details["cache_path"]) / "references" / "note.md"
    ).read_text(encoding="utf-8") == "version two\n"
    assert record["status"] == "failed"
    assert record["error_code"] == "TARGET_PERMISSION_DENIED"
    assert "auxiliary-secret" not in json.dumps(
        record, ensure_ascii=False,
    )


def test_packed_ref_openclaw_copy_is_reported_installed(tmp_path):
    from dulwich.repo import Repo

    from xskill.ecosystems._fallback import _install_meta_path
    from xskill.skill.git import init_skill_repo_on_baby
    from xskill.team.client.daemon import install_skill_to_ecosystems

    repo_dir = tmp_path / "packed-skill"
    init_skill_repo_on_baby(
        str(repo_dir), "packed-skill", "packed ref test",
    )
    with Repo(str(repo_dir)) as repo:
        expected_sha = repo.refs[b"HEAD"].decode(
            "ascii", errors="strict",
        )
        # Dulwich 等价于 `git pack-refs --all --prune`：写 packed-refs 并删 loose。
        repo.refs.pack_refs(all=True)
    assert not (repo_dir / ".git" / "refs" / "heads" / "baby").exists()
    assert (repo_dir / ".git" / "packed-refs").is_file()

    home = tmp_path / "home"
    (home / ".openclaw" / "agents").mkdir(parents=True)
    records = install_skill_to_ecosystems(repo_dir, home_root=home)

    assert len(records) == 1
    assert records[0]["ecosystem"] == "openclaw"
    assert records[0]["status"] == "installed"
    assert records[0]["mode"] == "copy"
    target = home / ".agents" / "skills" / "packed-skill"
    install_meta = json.loads(
        _install_meta_path(target).read_text(encoding="utf-8")
    )
    assert install_meta["source_sha"] == expected_sha


def test_invalid_git_head_is_reported_as_safe_install_failure(
    tmp_path, caplog,
):
    from xskill.skill.git import init_skill_repo_on_baby
    from xskill.team.client.daemon import install_skill_to_ecosystems

    repo_dir = tmp_path / "private" / "damaged-skill"
    init_skill_repo_on_baby(
        str(repo_dir), "damaged-skill", "damaged ref test",
    )
    (repo_dir / ".git" / "HEAD").write_text(
        "c" * 40, encoding="ascii",
    )
    home = tmp_path / "home"
    (home / ".openclaw" / "agents").mkdir(parents=True)

    with caplog.at_level("WARNING"):
        records = install_skill_to_ecosystems(repo_dir, home_root=home)

    assert len(records) == 1
    assert records[0]["status"] == "failed"
    assert records[0]["error_code"] == "GIT_HEAD_INVALID"
    assert records[0]["error"] == (
        "Git HEAD 校验失败，请检查 skill 仓库完整性"
    )
    serialized = json.dumps(records[0], ensure_ascii=False)
    assert "cccccccc" not in serialized
    assert all(record.exc_info is None for record in caplog.records)


def test_metadata_write_failure_is_not_corrected_by_valid_symlink(
    tmp_path, monkeypatch, caplog,
):
    from xskill.ecosystems import installation
    from xskill.team.client.daemon import install_skill_to_ecosystems

    repo_dir = tmp_path / "metadata-write-failure"
    repo_dir.mkdir()
    (repo_dir / "SKILL.md").write_text(
        "---\nname: metadata-write-failure\n"
        "description: test\n---\nbody\n",
        encoding="utf-8",
    )
    home = tmp_path / "home"
    (home / ".codex" / "sessions").mkdir(parents=True)

    def fail_metadata_write(*_args, **_kwargs):
        raise PermissionError(
            "Authorization: Bearer write-secret /root/private/meta"
        )

    monkeypatch.setattr(
        installation,
        "_atomic_write_json",
        fail_metadata_write,
    )
    with caplog.at_level("WARNING"):
        records = install_skill_to_ecosystems(repo_dir, home_root=home)

    assert len(records) == 1
    assert records[0]["status"] == "failed"
    assert records[0]["error_code"] == "INSTALL_METADATA_WRITE_FAILED"
    assert "write-secret" not in caplog.text
    assert "/root/private/meta" not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)


def test_retry_repairs_missing_metadata_on_correct_link(
    tmp_path, monkeypatch,
):
    from xskill.ecosystems import _fallback as install_fallback
    from xskill.ecosystems.installation import (
        InstallationMetadataError,
        read_install_metadata,
    )
    from xskill.team.client.daemon import install_skill_to_ecosystems

    repo_dir = tmp_path / "metadata-retry"
    repo_dir.mkdir()
    (repo_dir / "SKILL.md").write_text(
        "---\nname: metadata-retry\ndescription: test\n---\nbody\n",
        encoding="utf-8",
    )
    home = tmp_path / "home"
    (home / ".codex" / "sessions").mkdir(parents=True)
    original_write_metadata = install_fallback._write_install_meta
    write_attempts = 0

    def fail_first_metadata_write(dest, source, mode):
        nonlocal write_attempts
        write_attempts += 1
        if write_attempts == 1:
            raise InstallationMetadataError(
                "INSTALL_METADATA_WRITE_FAILED",
            )
        return original_write_metadata(dest, source, mode)

    monkeypatch.setattr(
        install_fallback,
        "_write_install_meta",
        fail_first_metadata_write,
    )

    first_records = install_skill_to_ecosystems(
        repo_dir, home_root=home,
    )
    target = home / ".agents" / "skills" / repo_dir.name
    assert first_records[0]["status"] == "failed"
    assert target.is_symlink()
    assert read_install_metadata(target) is None

    second_records = install_skill_to_ecosystems(
        repo_dir, home_root=home,
    )

    assert second_records[0]["status"] == "installed"
    metadata = read_install_metadata(target)
    assert metadata is not None
    assert metadata["mode"] == "symlink"
    assert metadata["source"] == str(repo_dir.resolve())


def test_retry_repairs_partial_metadata_on_correct_link(
    tmp_path, monkeypatch,
):
    from xskill.ecosystems import _fallback as install_fallback
    from xskill.ecosystems.installation import (
        InstallationMetadataError,
        install_metadata_path,
        read_install_metadata,
    )
    from xskill.team.client.daemon import install_skill_to_ecosystems

    repo_dir = tmp_path / "partial-metadata-retry"
    repo_dir.mkdir()
    (repo_dir / "SKILL.md").write_text(
        "---\nname: partial-metadata-retry\ndescription: test\n---\nbody\n",
        encoding="utf-8",
    )
    home = tmp_path / "home"
    (home / ".codex" / "sessions").mkdir(parents=True)
    original_write_metadata = install_fallback._write_install_meta
    write_attempts = 0

    def leave_partial_metadata(dest, source, mode):
        nonlocal write_attempts
        write_attempts += 1
        if write_attempts == 1:
            install_metadata_path(dest).write_text(
                '{"mode":"symlink", broken',
                encoding="utf-8",
            )
            raise InstallationMetadataError(
                "INSTALL_METADATA_WRITE_FAILED",
            )
        return original_write_metadata(dest, source, mode)

    monkeypatch.setattr(
        install_fallback,
        "_write_install_meta",
        leave_partial_metadata,
    )
    first_records = install_skill_to_ecosystems(
        repo_dir, home_root=home,
    )
    assert first_records[0]["status"] == "failed"

    second_records = install_skill_to_ecosystems(
        repo_dir, home_root=home,
    )

    assert second_records[0]["status"] == "installed"
    target = home / ".agents" / "skills" / repo_dir.name
    metadata = read_install_metadata(target)
    assert metadata is not None
    assert metadata["source"] == str(repo_dir.resolve())


def test_shared_correct_link_repairs_metadata_for_all_harnesses(
    tmp_path, monkeypatch,
):
    from xskill.ecosystems import _fallback as install_fallback
    from xskill.ecosystems.installation import (
        InstallationMetadataError,
        read_install_metadata,
    )
    from xskill.team.client.daemon import install_skill_to_ecosystems

    repo_dir = tmp_path / "shared-metadata-repair"
    repo_dir.mkdir()
    (repo_dir / "SKILL.md").write_text(
        "---\nname: shared-metadata-repair\ndescription: test\n---\nbody\n",
        encoding="utf-8",
    )
    home = tmp_path / "home"
    (home / ".codex" / "sessions").mkdir(parents=True)
    opencode_db = (
        home / ".local" / "share" / "opencode" / "opencode.db"
    )
    opencode_db.parent.mkdir(parents=True)
    opencode_db.touch()
    original_write_metadata = install_fallback._write_install_meta
    write_attempts = 0

    def fail_first_metadata_write(dest, source, mode):
        nonlocal write_attempts
        write_attempts += 1
        if write_attempts == 1:
            raise InstallationMetadataError(
                "INSTALL_METADATA_WRITE_FAILED",
            )
        return original_write_metadata(dest, source, mode)

    monkeypatch.setattr(
        install_fallback,
        "_write_install_meta",
        fail_first_metadata_write,
    )

    records = install_skill_to_ecosystems(repo_dir, home_root=home)

    assert {record["ecosystem"] for record in records} == {
        "codex", "opencode",
    }
    assert {record["status"] for record in records} == {"installed"}
    target = home / ".agents" / "skills" / repo_dir.name
    metadata = read_install_metadata(target)
    assert metadata is not None
    assert metadata["source"] == str(repo_dir.resolve())


def test_recent_auxiliary_edit_remains_safe_install_failure(
    tmp_path,
):
    from xskill.ecosystems.installation import read_install_metadata
    from xskill.skill.git import init_skill_repo_on_baby
    from xskill.team.client.daemon import install_skill_to_ecosystems

    repo_dir = tmp_path / "recent-user-edit"
    init_skill_repo_on_baby(
        str(repo_dir), "recent-user-edit", "recent edit test",
    )
    home = tmp_path / "home"
    (home / ".openclaw" / "agents").mkdir(parents=True)
    first_records = install_skill_to_ecosystems(
        repo_dir, home_root=home,
    )
    assert first_records[0]["status"] == "installed"
    target = home / ".agents" / "skills" / repo_dir.name
    user_file = target / "references" / "user-note.md"
    user_file.parent.mkdir(exist_ok=True)
    user_file.write_text("USER EDIT IN PROGRESS\n", encoding="utf-8")
    metadata = read_install_metadata(target)
    assert metadata is not None
    recent_mtime = metadata["installed_at"] + 2.0
    user_file.touch()
    import os
    os.utime(user_file, (recent_mtime, recent_mtime))

    second_records = install_skill_to_ecosystems(
        repo_dir, home_root=home,
    )

    assert second_records[0]["status"] == "failed"
    assert second_records[0]["error_code"] == "USER_EDIT_IN_PROGRESS"
    assert user_file.read_text(
        encoding="utf-8",
    ) == "USER EDIT IN PROGRESS\n"


def test_damaged_metadata_is_not_reported_installed(
    tmp_path, monkeypatch, caplog,
):
    from xskill.ecosystems.installation import install_metadata_path
    from xskill.team.client.daemon import install_skill_to_ecosystems

    repo_dir = tmp_path / "damaged-metadata"
    repo_dir.mkdir()
    (repo_dir / "SKILL.md").write_text(
        "---\nname: damaged-metadata\ndescription: test\n---\nbody\n",
        encoding="utf-8",
    )
    home = tmp_path / "home"
    ngagent_db = home / ".local" / "share" / "opencode" / "db" / "ngagent.db"
    ngagent_db.parent.mkdir(parents=True)
    ngagent_db.touch()
    target = home / ".config" / "opencode" / "skills" / repo_dir.name
    target.mkdir(parents=True)
    (target / "SKILL.md").write_text("stale\n", encoding="utf-8")
    install_metadata_path(target).write_text(
        '{"secret":"metadata-secret", broken',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "xskill.ecosystems.install_to_ngagent",
        lambda *_args, **_kwargs: target / "SKILL.md",
    )

    with caplog.at_level("WARNING"):
        records = install_skill_to_ecosystems(repo_dir, home_root=home)

    assert len(records) == 1
    assert records[0]["status"] == "failed"
    assert records[0]["error_code"] == "INSTALL_METADATA_INVALID"
    assert "metadata-secret" not in caplog.text
    assert str(target) not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)


def test_uninstall_skips_damaged_metadata_and_continues_other_targets(
    tmp_path, caplog,
):
    from xskill.ecosystems.installation import (
        install_metadata_path,
        write_install_metadata,
    )
    from xskill.team.client.daemon import uninstall_skill_from_ecosystems

    home = tmp_path / "home"
    source = tmp_path / "source" / "cleanup-skill"
    source.mkdir(parents=True)
    (source / "SKILL.md").write_text("body\n", encoding="utf-8")
    bad_target = home / ".claude" / "skills" / source.name
    good_target = home / ".config" / "opencode" / "skills" / source.name
    for target in (bad_target, good_target):
        target.mkdir(parents=True)
        (target / "SKILL.md").write_text("body\n", encoding="utf-8")
    install_metadata_path(bad_target).write_text(
        '{"secret":"cleanup-secret", broken',
        encoding="utf-8",
    )
    write_install_metadata(good_target, source, "copy")

    with caplog.at_level("WARNING"):
        removed = uninstall_skill_from_ecosystems(
            source.name,
            home_root=home,
            source_dir=source,
        )

    assert good_target in removed
    assert not good_target.exists()
    assert bad_target.exists()
    assert "cleanup-secret" not in caplog.text
    assert str(bad_target) not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)


def test_remove_target_failure_keeps_transaction_and_retry_succeeds(
    tmp_path, monkeypatch,
):
    from xskill.ecosystems.installation import (
        install_metadata_path,
        write_install_metadata,
    )
    from xskill.team.client import daemon

    target = tmp_path / "skills" / "retry-remove"
    target.mkdir(parents=True)
    (target / "SKILL.md").write_text("body\n", encoding="utf-8")
    source = tmp_path / "source"
    source.mkdir()
    (source / "SKILL.md").write_text("body\n", encoding="utf-8")
    metadata_path = install_metadata_path(target)
    write_install_metadata(target, source, "copy")
    original_remove = daemon._rmtree_anchored
    original_rmtree = daemon.shutil.rmtree
    removal_attempts = 0

    def fail_first_remove(*args, **kwargs):
        nonlocal removal_attempts
        removal_attempts += 1
        if removal_attempts == 1:
            raise PermissionError(
                "Authorization: Bearer remove-secret /root/private/target"
            )
        return original_remove(*args, **kwargs)

    if (
        getattr(original_rmtree, "avoids_symlink_attacks", False)
        and os.open in os.supports_dir_fd
    ):
        monkeypatch.setattr(daemon, "_rmtree_anchored", fail_first_remove)
    else:
        def fail_first_rmtree(path, *args, **kwargs):
            nonlocal removal_attempts
            removal_attempts += 1
            if removal_attempts == 1:
                raise PermissionError(
                    "Authorization: Bearer remove-secret /root/private/target"
                )
            return original_rmtree(path, *args, **kwargs)

        monkeypatch.setattr(daemon.shutil, "rmtree", fail_first_rmtree)

    assert daemon._remove_owned_install_target(target, source) is False
    assert not target.exists()
    assert not metadata_path.exists()
    active_transaction = daemon._active_removal_transaction(target)
    assert active_transaction is not None
    transaction_target, isolated_metadata, transaction_record = (
        active_transaction
    )
    assert isolated_metadata.exists()
    assert transaction_target.exists()
    assert transaction_record.exists()

    assert daemon._remove_owned_install_target(target, source) is True
    assert not target.exists()
    assert not metadata_path.exists()
    assert not transaction_target.exists()
    assert not transaction_record.exists()


@pytest.mark.skipif(
    os.open not in os.supports_dir_fd,
    reason="anchored open unavailable",
)
def test_copy_uninstall_does_not_require_rmtree_dir_fd(
    tmp_path, monkeypatch,
):
    """Python 3.9/3.10 没有 shutil.rmtree(dir_fd=)，安全删除仍应可用。"""
    from xskill.ecosystems.installation import write_install_metadata
    from xskill.team.client import daemon

    target = tmp_path / "skills" / "old-python-remove"
    target.mkdir(parents=True)
    (target / "SKILL.md").write_text("skill\n", encoding="utf-8")
    (target / "nested").mkdir()
    (target / "nested" / "data.txt").write_text(
        "body\n", encoding="utf-8",
    )
    source = tmp_path / "source"
    source.mkdir()
    (source / "SKILL.md").write_text("skill\n", encoding="utf-8")
    (source / "nested").mkdir()
    (source / "nested" / "data.txt").write_text(
        "body\n", encoding="utf-8",
    )
    write_install_metadata(target, source, "copy")

    def reject_dir_fd(_path, *_args, **kwargs):
        assert "dir_fd" not in kwargs
        raise AssertionError("anchored removal must not call shutil.rmtree")

    reject_dir_fd.avoids_symlink_attacks = True
    monkeypatch.setattr(daemon.shutil, "rmtree", reject_dir_fd)

    assert daemon._remove_owned_install_target(target, source) is True
    assert not target.exists()


def test_sidecar_isolation_failure_keeps_canonical_target(
    tmp_path, monkeypatch, caplog,
):
    from xskill.ecosystems.installation import (
        install_metadata_path,
        write_install_metadata,
    )
    from xskill.team.client import daemon

    target = tmp_path / "skills" / "isolation-failure"
    target.mkdir(parents=True)
    (target / "SKILL.md").write_text("user data\n", encoding="utf-8")
    source = tmp_path / "source"
    source.mkdir()
    (source / "SKILL.md").write_text("user data\n", encoding="utf-8")
    metadata_path = install_metadata_path(target)
    write_install_metadata(target, source, "copy")
    original_replace = Path.replace

    def fail_metadata_replace(path, destination):
        if path == metadata_path:
            raise PermissionError(
                "Authorization: Bearer isolate-secret /root/private/meta"
            )
        return original_replace(path, destination)

    monkeypatch.setattr(Path, "replace", fail_metadata_replace)
    with caplog.at_level("WARNING"):
        removed = daemon._remove_owned_install_target(target, source)

    assert removed is False
    assert (target / "SKILL.md").read_text(
        encoding="utf-8",
    ) == "user data\n"
    active_transaction = daemon._active_removal_transaction(target)
    assert active_transaction is not None
    transaction_target, _, _ = active_transaction
    assert not transaction_target.exists()
    assert metadata_path.exists()
    assert "isolate-secret" not in caplog.text
    assert "/root/private/meta" not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)


def test_stale_isolated_sidecar_never_deletes_recreated_user_target(tmp_path):
    from xskill.ecosystems.installation import install_metadata_path
    from xskill.team.client import daemon

    target = tmp_path / "skills" / "recreated-target"
    target.mkdir(parents=True)
    (target / "SKILL.md").write_text("old install\n", encoding="utf-8")
    metadata_path = install_metadata_path(target)
    metadata_path.write_text(
        '{"mode":"copy","source":"/source","source_sha":""}',
        encoding="utf-8",
    )
    isolated_path = metadata_path.with_name(
        f"{metadata_path.name}.removing",
    )
    metadata_path.replace(isolated_path)

    assert daemon._remove_owned_install_target(target) is False
    assert target.exists()
    assert isolated_path.exists()

    (target / "SKILL.md").write_text(
        "new user directory\n", encoding="utf-8",
    )

    assert daemon._remove_owned_install_target(target) is False
    assert (target / "SKILL.md").read_text(
        encoding="utf-8",
    ) == "new user directory\n"
    assert isolated_path.exists()

    daemon.shutil.rmtree(target)
    assert daemon._remove_owned_install_target(target) is True
    assert not isolated_path.exists()


def test_missing_target_legacy_sidecar_is_not_treated_as_identity(tmp_path):
    from xskill.ecosystems.installation import install_metadata_path
    from xskill.team.client import daemon

    target = tmp_path / "skills" / "missing-target"
    target.parent.mkdir(parents=True)
    metadata_path = install_metadata_path(target)
    metadata_path.write_text(
        '{"mode":"copy","source":"/source","source_sha":""}',
        encoding="utf-8",
    )

    assert daemon._remove_owned_install_target(target) is False
    assert metadata_path.exists()


def test_uninstall_does_not_delete_out_of_band_replacement(tmp_path):
    from xskill.ecosystems.installation import write_install_metadata
    from xskill.team.client.daemon import uninstall_skill_from_ecosystems

    home = tmp_path / "home"
    source = tmp_path / "source" / "replaced-copy"
    source.mkdir(parents=True)
    (source / "SKILL.md").write_text("old\n", encoding="utf-8")
    target = home / ".claude" / "skills" / source.name
    target.mkdir(parents=True)
    (target / "SKILL.md").write_text("old\n", encoding="utf-8")
    write_install_metadata(target, source, "copy")

    old_target = target.with_name("old-target-kept-for-test")
    target.replace(old_target)
    target.mkdir()
    (target / "SKILL.md").write_text("new user data\n", encoding="utf-8")

    removed = uninstall_skill_from_ecosystems(
        source.name, home_root=home, source_dir=source,
    )

    assert removed == []
    assert (target / "SKILL.md").read_text(
        encoding="utf-8",
    ) == "new user data\n"
    assert old_target.is_dir()


def test_uninstall_rename_race_only_removes_captured_old_target(
    tmp_path, monkeypatch,
):
    from xskill.ecosystems.installation import write_install_metadata
    from xskill.team.client import daemon

    source = tmp_path / "source"
    source.mkdir()
    (source / "SKILL.md").write_text("old\n", encoding="utf-8")
    target = tmp_path / "skills" / "rename-race"
    target.mkdir(parents=True)
    (target / "SKILL.md").write_text("old\n", encoding="utf-8")
    write_install_metadata(target, source, "copy")
    original_replace = Path.replace

    def recreate_after_target_rename(path, destination):
        result = original_replace(path, destination)
        if (
            path == target
            and destination.name.startswith(
                ".xskill-removing-target-rename-race-",
            )
        ):
            target.mkdir()
            (target / "SKILL.md").write_text(
                "new user data\n", encoding="utf-8",
            )
        return result

    monkeypatch.setattr(Path, "replace", recreate_after_target_rename)

    assert daemon._remove_owned_install_target(target, source) is True
    assert (target / "SKILL.md").read_text(
        encoding="utf-8",
    ) == "new user data\n"
    assert daemon._active_removal_transaction(target) is None


def test_uninstall_never_isolates_concurrent_reinstall_sidecar(
    tmp_path, monkeypatch,
):
    from xskill.ecosystems.installation import (
        read_install_metadata,
        write_install_metadata,
    )
    from xskill.team.client import daemon

    source = tmp_path / "source"
    source.mkdir()
    (source / "SKILL.md").write_text("old\n", encoding="utf-8")
    target = tmp_path / "skills" / "concurrent-reinstall"
    target.mkdir(parents=True)
    (target / "SKILL.md").write_text("old\n", encoding="utf-8")
    write_install_metadata(target, source, "copy")
    original_replace = Path.replace
    reinstalled = False

    def reinstall_after_target_isolation(path, destination):
        nonlocal reinstalled
        result = original_replace(path, destination)
        if (
            path == target
            and destination.name.startswith(
                ".xskill-removing-target-concurrent-reinstall-",
            )
            and not reinstalled
        ):
            reinstalled = True
            target.mkdir()
            (target / "SKILL.md").write_text(
                "new install\n", encoding="utf-8",
            )
            write_install_metadata(target, source, "copy")
        return result

    monkeypatch.setattr(
        Path, "replace", reinstall_after_target_isolation,
    )

    assert daemon._remove_owned_install_target(target, source) is True
    assert (target / "SKILL.md").read_text(
        encoding="utf-8",
    ) == "new install\n"
    assert read_install_metadata(target) is not None
    assert daemon._active_removal_transaction(target) is None


def test_prepared_recovery_cleans_old_sidecar_after_target_replacement(
    tmp_path, monkeypatch,
):
    from xskill.ecosystems.installation import (
        install_metadata_path,
        read_install_metadata,
        write_install_metadata,
    )
    from xskill.team.client import daemon

    source = tmp_path / "source"
    source.mkdir()
    (source / "SKILL.md").write_text("old\n", encoding="utf-8")
    target = tmp_path / "skills" / "prepared-replaced"
    target.mkdir(parents=True)
    (target / "SKILL.md").write_text("old\n", encoding="utf-8")
    write_install_metadata(target, source, "copy")
    metadata_path = install_metadata_path(target)
    original_replace = Path.replace
    crashed = False

    def crash_after_metadata_isolation(path, destination):
        nonlocal crashed
        result = original_replace(path, destination)
        if (
            path == metadata_path
            and ".removing-" in destination.name
            and not crashed
        ):
            crashed = True
            raise SystemExit("simulated metadata isolation crash")
        return result

    monkeypatch.setattr(Path, "replace", crash_after_metadata_isolation)
    with pytest.raises(SystemExit):
        daemon._remove_owned_install_target(target, source)
    assert daemon._active_removal_transaction(target) is not None

    monkeypatch.setattr(Path, "replace", original_replace)
    captured_old = tmp_path / "captured-old-prepared"
    target.replace(captured_old)
    target.mkdir()
    (target / "SKILL.md").write_text(
        "new install\n", encoding="utf-8",
    )
    write_install_metadata(target, source, "copy")

    assert daemon._remove_owned_install_target(target, source) is False
    assert daemon._active_removal_transaction(target) is None
    assert (target / "SKILL.md").read_text(
        encoding="utf-8",
    ) == "new install\n"
    assert read_install_metadata(target) is not None
    assert (captured_old / "SKILL.md").read_text(
        encoding="utf-8",
    ) == "old\n"


def test_uninstall_identity_mismatch_restores_transaction_target(
    tmp_path, monkeypatch,
):
    from xskill.ecosystems.installation import write_install_metadata
    from xskill.team.client import daemon

    source = tmp_path / "source"
    source.mkdir()
    (source / "SKILL.md").write_text("old\n", encoding="utf-8")
    target = tmp_path / "skills" / "identity-race"
    target.mkdir(parents=True)
    (target / "SKILL.md").write_text("old\n", encoding="utf-8")
    write_install_metadata(target, source, "copy")
    captured_old = tmp_path / "captured-old"
    original_replace = Path.replace
    swapped = False

    def replace_transaction_before_identity_check(path, destination):
        nonlocal swapped
        result = original_replace(path, destination)
        if (
            path == target
            and destination.name.startswith(
                ".xskill-removing-target-identity-race-",
            )
            and not swapped
        ):
            swapped = True
            original_replace(destination, captured_old)
            destination.mkdir()
            (destination / "SKILL.md").write_text(
                "raced replacement\n", encoding="utf-8",
            )
        return result

    monkeypatch.setattr(
        Path, "replace", replace_transaction_before_identity_check,
    )

    assert daemon._remove_owned_install_target(target, source) is False
    assert (target / "SKILL.md").read_text(
        encoding="utf-8",
    ) == "raced replacement\n"
    assert (captured_old / "SKILL.md").read_text(
        encoding="utf-8",
    ) == "old\n"
    assert daemon._active_removal_transaction(target) is None


def test_uninstall_identity_mismatch_never_overwrites_recreated_target(
    tmp_path, monkeypatch,
):
    from xskill.ecosystems.installation import write_install_metadata
    from xskill.team.client import daemon

    source = tmp_path / "source"
    source.mkdir()
    (source / "SKILL.md").write_text("old\n", encoding="utf-8")
    target = tmp_path / "skills" / "identity-conflict"
    target.mkdir(parents=True)
    (target / "SKILL.md").write_text("old\n", encoding="utf-8")
    write_install_metadata(target, source, "copy")
    captured_old = tmp_path / "captured-old-conflict"
    original_replace = Path.replace
    swapped = False

    def create_both_conflicting_paths(path, destination):
        nonlocal swapped
        result = original_replace(path, destination)
        if (
            path == target
            and destination.name.startswith(
                ".xskill-removing-target-identity-conflict-",
            )
            and not swapped
        ):
            swapped = True
            original_replace(destination, captured_old)
            destination.mkdir()
            (destination / "SKILL.md").write_text(
                "transaction replacement\n", encoding="utf-8",
            )
            target.mkdir()
            (target / "SKILL.md").write_text(
                "new user data\n", encoding="utf-8",
            )
        return result

    monkeypatch.setattr(Path, "replace", create_both_conflicting_paths)

    assert daemon._remove_owned_install_target(target, source) is False
    assert daemon._active_removal_transaction(target) is not None
    assert (target / "SKILL.md").read_text(
        encoding="utf-8",
    ) == "new user data\n"

    monkeypatch.setattr(Path, "replace", original_replace)
    assert daemon._remove_owned_install_target(target, source) is False
    assert (target / "SKILL.md").read_text(
        encoding="utf-8",
    ) == "new user data\n"
    assert daemon._active_removal_transaction(target) is not None


@pytest.mark.parametrize(
    "crash_stage", ["target_to_transaction", "metadata_to_transaction"],
)
def test_uninstall_recovers_each_atomic_rename_crash(
    tmp_path, monkeypatch, crash_stage,
):
    from xskill.ecosystems.installation import (
        install_metadata_path,
        write_install_metadata,
    )
    from xskill.team.client import daemon

    source = tmp_path / "source"
    source.mkdir()
    (source / "SKILL.md").write_text("body\n", encoding="utf-8")
    target = tmp_path / "skills" / f"crash-{crash_stage}"
    target.mkdir(parents=True)
    (target / "SKILL.md").write_text("body\n", encoding="utf-8")
    write_install_metadata(target, source, "copy")
    metadata_path = install_metadata_path(target)
    original_replace = Path.replace
    crashed = False

    def crash_after_selected_rename(path, destination):
        nonlocal crashed
        target_rename = (
            path == target
            and destination.name.startswith(
                f".xskill-removing-target-{target.name}-",
            )
        )
        metadata_rename = (
            path == metadata_path
            and destination.name.startswith(
                f"{metadata_path.name}.removing-",
            )
        )
        should_crash = (
            crash_stage == "target_to_transaction" and target_rename
        ) or (
            crash_stage == "metadata_to_transaction" and metadata_rename
        )
        result = original_replace(path, destination)
        if should_crash and not crashed:
            crashed = True
            raise SystemExit("simulated uninstall crash")
        return result

    monkeypatch.setattr(Path, "replace", crash_after_selected_rename)
    with pytest.raises(SystemExit):
        daemon._remove_owned_install_target(target, source)
    assert daemon._active_removal_transaction(target) is not None

    monkeypatch.setattr(Path, "replace", original_replace)
    assert daemon._remove_owned_install_target(target, source) is True
    assert not target.exists()
    assert daemon._active_removal_transaction(target) is None


def test_partial_copy_removal_retries_without_identity_marker(
    tmp_path, monkeypatch,
):
    from xskill.ecosystems.installation import (
        COPY_INSTALL_MARKER_NAME,
        write_install_metadata,
    )
    from xskill.team.client import daemon

    source = tmp_path / "source"
    source.mkdir()
    (source / "SKILL.md").write_text("body\n", encoding="utf-8")
    target = tmp_path / "skills" / "partial-remove"
    target.mkdir(parents=True)
    (target / "SKILL.md").write_text("body\n", encoding="utf-8")
    write_install_metadata(target, source, "copy")
    original_remove = daemon._rmtree_anchored
    original_rmtree = daemon.shutil.rmtree
    first_attempt = True

    def remove_marker_then_fail(
        parent_descriptor,
        entry_name,
        expected_identity,
        display_path,
    ):
        nonlocal first_attempt
        if (
            first_attempt
            and display_path.name.startswith(
                ".xskill-removing-target-partial-remove-",
            )
        ):
            first_attempt = False
            (display_path / COPY_INSTALL_MARKER_NAME).unlink()
            raise PermissionError("partial recursive delete")
        return original_remove(
            parent_descriptor,
            entry_name,
            expected_identity,
            display_path,
        )

    if (
        getattr(original_rmtree, "avoids_symlink_attacks", False)
        and os.open in os.supports_dir_fd
    ):
        monkeypatch.setattr(
            daemon, "_rmtree_anchored", remove_marker_then_fail,
        )
    else:
        def remove_marker_then_fail_rmtree(path, *args, **kwargs):
            nonlocal first_attempt
            if first_attempt:
                first_attempt = False
                (Path(path) / COPY_INSTALL_MARKER_NAME).unlink()
                raise PermissionError("partial recursive delete")
            return original_rmtree(path, *args, **kwargs)

        monkeypatch.setattr(
            daemon.shutil, "rmtree", remove_marker_then_fail_rmtree,
        )

    assert daemon._remove_owned_install_target(target, source) is False
    active_transaction = daemon._active_removal_transaction(target)
    assert active_transaction is not None
    transaction_target, _, _ = active_transaction
    assert transaction_target.is_dir()
    assert not (transaction_target / COPY_INSTALL_MARKER_NAME).exists()
    assert daemon._remove_owned_install_target(target, source) is True
    assert not transaction_target.exists()


def test_completed_transaction_cleanup_never_touches_recreated_target(
    tmp_path, monkeypatch,
):
    from xskill.ecosystems.installation import (
        install_metadata_path,
        write_install_metadata,
    )
    from xskill.team.client import daemon

    source = tmp_path / "source"
    source.mkdir()
    (source / "SKILL.md").write_text("old\n", encoding="utf-8")
    target = tmp_path / "skills" / "cleanup-crash"
    target.mkdir(parents=True)
    (target / "SKILL.md").write_text("old\n", encoding="utf-8")
    write_install_metadata(target, source, "copy")
    metadata_path = install_metadata_path(target)
    original_unlink = Path.unlink
    fail_cleanup = True

    def fail_first_metadata_cleanup(path, *args, **kwargs):
        if (
            path.name.startswith(
                f"{metadata_path.name}.removing-",
            )
            and fail_cleanup
        ):
            raise PermissionError("simulated cleanup crash")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_first_metadata_cleanup)
    assert daemon._remove_owned_install_target(target, source) is False
    assert not target.exists()
    assert not metadata_path.exists()

    target.mkdir()
    (target / "SKILL.md").write_text("new user data\n", encoding="utf-8")
    fail_cleanup = False

    assert daemon._remove_owned_install_target(target, source) is True
    assert (target / "SKILL.md").read_text(
        encoding="utf-8",
    ) == "new user data\n"


def test_public_uninstall_recovers_transaction_before_new_target_gate(
    tmp_path, monkeypatch,
):
    from xskill.ecosystems.installation import (
        install_metadata_path,
        write_install_metadata,
    )
    from xskill.team.client import daemon

    home = tmp_path / "home"
    source = tmp_path / "source" / "cleanup-with-new-target"
    source.mkdir(parents=True)
    (source / "SKILL.md").write_text("old\n", encoding="utf-8")
    target = home / ".claude" / "skills" / source.name
    target.mkdir(parents=True)
    (target / "SKILL.md").write_text("old\n", encoding="utf-8")
    write_install_metadata(target, source, "copy")
    metadata_path = install_metadata_path(target)
    original_unlink = Path.unlink
    fail_cleanup = True

    def fail_isolated_sidecar_cleanup(path, *args, **kwargs):
        if (
            fail_cleanup
            and path.name.startswith(f"{metadata_path.name}.removing-")
        ):
            raise PermissionError("simulated cleanup failure")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_isolated_sidecar_cleanup)
    assert daemon.uninstall_skill_from_ecosystems(
        source.name, home_root=home, source_dir=source,
    ) == []
    assert daemon._active_removal_transaction(target) is not None
    assert not target.exists()

    target.mkdir()
    (target / "SKILL.md").write_text(
        "new user directory\n", encoding="utf-8",
    )
    fail_cleanup = False

    assert daemon.uninstall_skill_from_ecosystems(
        source.name, home_root=home, source_dir=source,
    ) == [target]
    assert (target / "SKILL.md").read_text(
        encoding="utf-8",
    ) == "new user directory\n"
    assert daemon._active_removal_transaction(target) is None


def test_forged_prepared_removal_record_never_moves_target(
    tmp_path, monkeypatch,
):
    from xskill.ecosystems.installation import write_install_metadata
    from xskill.team.client import daemon

    source = tmp_path / "source"
    source.mkdir()
    (source / "SKILL.md").write_text("owned\n", encoding="utf-8")
    target = tmp_path / "skills" / "forged-prepared"
    target.mkdir(parents=True)
    (target / "SKILL.md").write_text("owned\n", encoding="utf-8")
    write_install_metadata(target, source, "copy")
    transaction_id = "1" * 24
    transaction_target, _, transaction_record = (
        daemon._removal_transaction_paths(target, transaction_id)
    )
    target_identity = daemon._path_identity(target)
    assert target_identity is not None
    daemon._atomic_write_removal_record(
        transaction_record,
        {
            "schema_version": 1,
            "transaction_id": transaction_id,
            "target_hash": daemon._target_path_hash(target),
            "target_identity": list(target_identity),
            "mode": "copy",
            "installation_id": "a" * 32,
            "content_identity": "b" * 64,
            "state": "prepared",
            "deletion_started": False,
        },
    )
    original_replace = Path.replace
    target_moves: list[Path] = []

    def record_target_move(path, destination):
        if path == target:
            target_moves.append(destination)
        return original_replace(path, destination)

    monkeypatch.setattr(Path, "replace", record_target_move)

    assert daemon._remove_owned_install_target(target, source) is False
    assert target_moves == []
    assert not transaction_target.exists()
    assert (target / "SKILL.md").read_text(
        encoding="utf-8",
    ) == "owned\n"


def test_windows_removal_directory_persistence_does_not_open_directory(
    tmp_path, monkeypatch,
):
    from xskill.team.client import daemon

    parent = tmp_path / "skills"
    parent.mkdir()

    def fail_directory_open(*_args, **_kwargs):
        raise AssertionError("Windows directory must not use CRT os.open")

    monkeypatch.setattr(daemon.os, "name", "nt")
    monkeypatch.setattr(daemon.os, "open", fail_directory_open)

    daemon._fsync_removal_directory(parent)


def test_uninstall_symlink_unlinks_only_link_target(tmp_path):
    from xskill.team.client import daemon

    source = tmp_path / "source"
    source.mkdir()
    (source / "SKILL.md").write_text("source\n", encoding="utf-8")
    target = tmp_path / "skills" / "linked"
    target.parent.mkdir()
    target.symlink_to(source, target_is_directory=True)

    assert daemon._remove_owned_install_target(target, source) is True
    assert not target.is_symlink()
    assert (source / "SKILL.md").read_text(encoding="utf-8") == "source\n"


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO unsupported")
def test_uninstall_rejects_fifo_target(tmp_path):
    from xskill.team.client import daemon

    target = tmp_path / "skills" / "special"
    target.parent.mkdir()
    os.mkfifo(target)

    assert daemon._remove_owned_install_target(target) is False
    assert target.exists()


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO unsupported")
def test_uninstall_rejects_fifo_identity_marker(tmp_path):
    from xskill.ecosystems.installation import (
        COPY_INSTALL_MARKER_NAME,
        write_install_metadata,
    )
    from xskill.team.client.daemon import uninstall_skill_from_ecosystems

    home = tmp_path / "home"
    source = tmp_path / "source" / "fifo-marker"
    source.mkdir(parents=True)
    (source / "SKILL.md").write_text("body\n", encoding="utf-8")
    target = home / ".claude" / "skills" / source.name
    target.mkdir(parents=True)
    (target / "SKILL.md").write_text("body\n", encoding="utf-8")
    write_install_metadata(target, source, "copy")
    marker = target / COPY_INSTALL_MARKER_NAME
    marker.unlink()
    os.mkfifo(marker)

    assert uninstall_skill_from_ecosystems(
        source.name, home_root=home, source_dir=source,
    ) == []
    assert target.is_dir()


def test_runner_stops_absorb_after_reverse_sync_failure(
    tmp_path, monkeypatch,
):
    from xskill.agents import user_edit_absorb_agent as user_absorb
    from xskill.pipeline.runner import DirectoryWatcher

    skill_dir = tmp_path / "skills"
    skill_path = skill_dir / "failed-reverse"
    skill_path.mkdir(parents=True)
    (skill_path / "SKILL.md").write_text("body\n", encoding="utf-8")
    watcher = DirectoryWatcher(
        skill_dir=skill_dir,
        home_root=tmp_path / "home",
    )
    monkeypatch.setattr(
        user_absorb,
        "reverse_sync_openclaw_dest",
        lambda *_args, **_kwargs: user_absorb.ReverseSyncStatus.FAILED,
    )

    def fail_if_detection_runs(*_args, **_kwargs):
        raise AssertionError("FAILED 后不得继续 detect/absorb/install")

    monkeypatch.setattr(
        user_absorb, "detect_user_edits", fail_if_detection_runs,
    )
    monkeypatch.setattr(watcher, "_factory", lambda: object())

    watcher._check_user_edits()


def test_link_modes_do_not_read_install_metadata(monkeypatch):
    from xskill.ecosystems import installation

    class _LinkTarget:
        def __init__(self, is_symlink):
            self._is_symlink = is_symlink

        def is_symlink(self):
            return self._is_symlink

    def fail_metadata_read(_target):
        raise AssertionError("link mode must not read install metadata")

    monkeypatch.setattr(
        installation, "read_install_metadata", fail_metadata_read,
    )
    symlink_target = _LinkTarget(True)
    assert installation.installed_mode(symlink_target) == "symlink"

    junction_target = _LinkTarget(False)

    def is_test_junction(target):
        return target is junction_target

    monkeypatch.setattr(
        installation, "is_link_or_junction",
        is_test_junction,
    )
    assert installation.installed_mode(junction_target) == "junction"


def test_later_shared_target_success_corrects_earlier_attempt_failure(
    tmp_path, monkeypatch,
):
    home = tmp_path / "home"
    (home / ".codex" / "sessions").mkdir(parents=True)
    (home / ".openclaw" / "agents").mkdir(parents=True)
    slots = SearchSlots(
        xskill_home=tmp_path / "xskill-home", home_root=home,
    )
    result = _result("shared-recovery")
    archive = _SearchHttp([result]).get(
        f"/api/v1/team/skill/{result['skill_id']}/bundle"
    ).content

    def fail_codex(*_args, **_kwargs):
        raise PermissionError("Authorization: Bearer secret")

    monkeypatch.setattr("xskill.ecosystems.install_to_codex", fail_codex)

    details = slots.install(
        result, archive, query="shared", return_details=True,
    )

    shared_records = list(details["installations"])
    assert {record["ecosystem"] for record in shared_records} == {
        "codex", "openclaw",
    }
    assert all(record["status"] == "installed" for record in shared_records)
    assert all(record["mode"] == "copy" for record in shared_records)
    assert all("error" not in record for record in shared_records)
    assert all("error_code" not in record for record in shared_records)


def test_trae_partial_install_is_corrected_per_target(
    tmp_path, monkeypatch,
):
    from xskill.ecosystems._fallback import install_dir

    home = tmp_path / "home"
    (home / ".trae-cn").mkdir(parents=True)
    (home / ".trae").mkdir(parents=True)
    slots = SearchSlots(
        xskill_home=tmp_path / "xskill-home", home_root=home,
    )
    result = _result("trae-partial")
    archive = _SearchHttp([result]).get(
        f"/api/v1/team/skill/{result['skill_id']}/bundle"
    ).content

    def install_first_trae_target(skill_path, *, target_root, side):
        assert side == "main"
        first_target = (
            Path(target_root) / ".trae-cn" / "skills" / Path(skill_path).name
        )
        first_target.parent.mkdir(parents=True)
        install_dir(
            Path(skill_path), first_target,
            force_mode="copy", auto_reset=True,
        )
        raise PermissionError("Authorization: Bearer trae-secret")

    monkeypatch.setattr(
        "xskill.ecosystems.install_to_trae", install_first_trae_target,
    )

    details = slots.install(
        result, archive, query="trae", return_details=True,
    )

    records = {
        Path(record["target"]): record
        for record in details["installations"]
    }
    first_target = home / ".trae-cn" / "skills" / result["skill_id"]
    second_target = home / ".trae" / "skills" / result["skill_id"]
    assert records[first_target]["status"] == "installed"
    assert records[first_target]["mode"] == "copy"
    assert "error" not in records[first_target]
    assert records[second_target]["status"] == "failed"
    assert records[second_target]["error_code"] == "TARGET_PERMISSION_DENIED"
    assert records[second_target]["error"] == "目标目录不可写，请检查目录权限"
    serialized = json.dumps(list(records.values()), ensure_ascii=False)
    assert "trae-secret" not in serialized
    assert "Authorization" not in serialized


def test_structured_search_error_is_safe_and_correlated(capsys):
    class _ErrorHttp:
        def get(self, *_args, **_kwargs):
            return _Response(
                500,
                json_data={
                    "code": "SKILL_HUB_SEARCH_FAILED",
                    "message": "Authorization: Bearer response-secret",
                    "request_id": "search-0123456789abcdef",
                    "retryable": False,
                },
                text="Authorization: Bearer raw-secret /root/private",
                headers={"X-Request-ID": "search-0123456789abcdef"},
            )

    return_code = cli.cmd_search_hub(
        SimpleNamespace(terms=["error"], top_k=5, json=False),
        http=_ErrorHttp(), headers={},
    )

    captured = capsys.readouterr()
    assert return_code == 1
    assert "HTTP 500" in captured.err
    assert "服务器执行 SkillHub 搜索时发生异常" in captured.err
    assert "search-0123456789abcdef" in captured.err
    assert "response-secret" not in captured.err
    assert "raw-secret" not in captured.err
    assert "/root/private" not in captured.err


def test_structured_search_error_json_is_machine_readable(capsys):
    class _ErrorHttp:
        def get(self, *_args, **_kwargs):
            return _Response(
                503,
                json_data={
                    "code": "SKILL_HUB_SOURCE_UNAVAILABLE",
                    "message": "do not trust raw server text",
                    "request_id": "search-fedcba9876543210",
                    "retryable": True,
                },
                headers={"X-Request-ID": "search-fedcba9876543210"},
            )

    return_code = cli.cmd_search_hub(
        SimpleNamespace(terms=["error"], top_k=5, json=True),
        http=_ErrorHttp(), headers={},
    )

    payload = json.loads(capsys.readouterr().out)
    assert return_code == 1
    assert payload == {"error": {
        "http_status": 503,
        "code": "SKILL_HUB_SOURCE_UNAVAILABLE",
        "message": "SkillHub 数据源暂时不可用",
        "request_id": "search-fedcba9876543210",
        "retryable": True,
    }}


@pytest.mark.parametrize(
    ("body_request_id", "header_request_id"),
    [
        (
            "search-0123456789abcdef-extra",
            "search-0123456789abcdef",
        ),
        (
            "search-0123456789abcdef",
            "search-fedcba9876543210",
        ),
        (
            "search-Authorization-Bearer-secret",
            "search-/root/private",
        ),
    ],
)
def test_untrusted_or_mismatched_request_ids_are_not_shown(
    body_request_id, header_request_id, capsys,
):
    class _ErrorHttp:
        def get(self, *_args, **_kwargs):
            return _Response(
                500,
                json_data={
                    "code": "SKILL_HUB_SEARCH_FAILED",
                    "message": "safe",
                    "request_id": body_request_id,
                },
                headers={"X-Request-ID": header_request_id},
            )

    return_code = cli.cmd_search_hub(
        SimpleNamespace(terms=["error"], top_k=5, json=False),
        http=_ErrorHttp(), headers={},
    )

    captured = capsys.readouterr()
    assert return_code == 1
    assert "错误编号" not in captured.err
    assert body_request_id not in captured.err
    assert header_request_id not in captured.err


def test_cp936_json_output_with_emoji_is_valid(
    tmp_path, monkeypatch,
):
    home = tmp_path / "home"
    _install_home(monkeypatch, tmp_path, home)
    result = _result(
        "json-\N{GRINNING FACE}",
        source_path="user_skill_hub/\N{CAT FACE}/json-skill",
    )
    result["description"] = "emoji \N{ROCKET}"
    output_bytes = io.BytesIO()
    cp936_stdout = io.TextIOWrapper(
        output_bytes, encoding="cp936", errors="strict",
    )
    monkeypatch.setattr(sys, "stdout", cp936_stdout)

    return_code = cli.cmd_search_hub(
        SimpleNamespace(
            terms=["query-\N{FIRE}"], top_k=5, json=True,
        ),
        http=_SearchHttp([result]), headers={},
    )
    cp936_stdout.flush()

    payload = json.loads(output_bytes.getvalue().decode("cp936"))
    assert return_code == 0
    assert payload[0]["name"] == "json-\N{GRINNING FACE}"
    assert payload[0]["description"] == "emoji \N{ROCKET}"
    assert payload[0]["source_path"] == (
        "user_skill_hub/\N{CAT FACE}/json-skill"
    )


def test_search_error_json_parse_log_is_safe(caplog):
    class _BadJsonResponse:
        status_code = 502
        headers = {}

        @staticmethod
        def json():
            raise ValueError(
                "Authorization: Bearer parse-secret /root/private/body"
            )

    caplog.set_level("WARNING", logger="xskill.cli")

    safe_error = cli._safe_search_http_error(_BadJsonResponse())

    assert safe_error["http_status"] == 502
    assert safe_error["code"] == "HTTP_ERROR"
    assert "http_status=502" in caplog.text
    assert "error_type=ValueError" in caplog.text
    assert "Authorization" not in caplog.text
    assert "parse-secret" not in caplog.text
    assert "/root/private/body" not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)
