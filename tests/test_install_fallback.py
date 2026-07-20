"""
test_install_fallback.py -- 跨平台目录安装的三阶 fallback 行为
=============================================================

P1 (cross-platform baseline) 加的 ``install_fallback.install_dir``：

    symlink → directory junction (Windows-only) → copytree

测试覆盖三条返回路径，并用 ``monkeypatch`` 模拟其他平台的失败模式
（CI 矩阵会真跑 ubuntu/macos/windows，但本机只有 Linux，必须能 stub）。

测试设计：
* T1 — 默认 happy path（Linux/macOS）：symlink 成功
* T2 — symlink 失败、junction 成功：模拟 Windows Dev Mode 关，但 NTFS 同卷
* T3 — symlink 失败、junction 失败：极端 fallback，必然走 copy
* T4 — copy 模式下源更新**不**穿透（snapshot 语义）
* T5 — symlink 模式下源更新**穿透**（live mount 语义）
* T6 — install_to_claude_code 在 copy 模式下打 warning，但仍返回正确 SKILL.md 路径
* T7 — install_dir 不接管"dest 已存在"的情况（上层职责）
"""
from __future__ import annotations

import json
import logging
import os
import sys
import traceback
from pathlib import Path

import pytest
from dulwich import porcelain
from dulwich.objects import Blob
from dulwich.repo import Repo

from xskill.ecosystems import _fallback as install_fallback
from xskill.ecosystems._fallback import install_dir
from xskill.ecosystems.installation import (
    COPY_INSTALL_MARKER_NAME,
    GitHeadError,
    InstallationMetadataError,
    copy_install_is_current,
    install_metadata_path,
    read_copy_install_baseline,
    read_install_metadata,
    read_skill_head_sha,
    write_install_metadata,
)


# ──────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────


def _make_src(root: Path, name: str = "skill-x") -> Path:
    """造一个最小 src 目录：``<root>/<name>/{SKILL.md, scripts/run.sh}``。

    模拟 xskill 自己的 skill 子仓结构，足以让三种安装模式都能验证
    "源目录被装到了 dest"。
    """
    src = root / name
    (src / "scripts").mkdir(parents=True)
    (src / "SKILL.md").write_text("---\nname: skill-x\n---\nbody\n", encoding="utf-8")
    (src / "scripts" / "run.sh").write_text("#!/bin/bash\necho hi\n", encoding="utf-8")
    return src


def _init_real_git_repo(src: Path) -> str:
    """把现有 fixture 初始化为带真实 object/ref 的 Dulwich Git 仓。"""
    repo = porcelain.init(str(src), bare=False)
    try:
        porcelain.add(repo)
        commit_sha = porcelain.commit(
            repo,
            message=b"test source",
            author=b"test <test@example.com>",
            committer=b"test <test@example.com>",
        )
    finally:
        repo.close()
    return commit_sha.decode("ascii", errors="strict")


def test_read_skill_head_sha_supports_detached_head(tmp_path):
    src = _make_src(tmp_path / "srcs")
    commit_sha = _init_real_git_repo(src)
    (src / ".git" / "HEAD").write_text(commit_sha, encoding="ascii")

    assert install_fallback._read_skill_head_sha(src) == commit_sha


def test_read_skill_head_sha_supports_linked_worktree(tmp_path):
    src = _make_src(tmp_path / "srcs")
    commit_sha = _init_real_git_repo(src)
    worktree = tmp_path / "linked-worktree"
    porcelain.worktree_add(
        str(src), str(worktree), commit=commit_sha, detach=True,
    )

    assert (worktree / ".git").is_file()
    assert install_fallback._read_skill_head_sha(worktree) == commit_sha


def test_read_skill_head_sha_returns_none_only_for_non_git_or_empty_repo(
    tmp_path,
):
    non_git = _make_src(tmp_path / "non-git")
    empty_repo = _make_src(tmp_path / "empty")
    repo = porcelain.init(str(empty_repo), bare=False)
    repo.close()

    assert read_skill_head_sha(non_git) is None
    assert read_skill_head_sha(empty_repo) is None


@pytest.mark.parametrize(
    ("head_bytes", "expected_error_type"),
    [
        (b"not-an-object-id\n", "GIT_HEAD_DAMAGED"),
        (b"a" * 40 + b"\n", "GIT_HEAD_OBJECT_MISSING"),
        (b"\xff\xfe\xfd\n", "GIT_HEAD_DAMAGED"),
        (b"ref: refs/heads/missing\n", "GIT_HEAD_MISSING"),
    ],
)
def test_read_skill_head_sha_fails_safely_for_damaged_head(
    tmp_path, caplog, head_bytes, expected_error_type,
):
    src = _make_src(tmp_path / "private" / "srcs")
    _init_real_git_repo(src)
    (src / ".git" / "HEAD").write_bytes(head_bytes)

    with caplog.at_level(logging.ERROR, logger="xskill.installation"):
        with pytest.raises(GitHeadError) as raised:
            read_skill_head_sha(src)

    assert raised.value.error_type == expected_error_type
    assert str(src) not in caplog.text
    assert head_bytes.hex() not in caplog.text
    assert "path_hash=" in caplog.text
    assert f"error_type={expected_error_type}" in caplog.text
    assert all(record.exc_info is None for record in caplog.records)


def test_read_skill_head_sha_rejects_non_commit_head(tmp_path, caplog):
    src = _make_src(tmp_path / "private" / "srcs")
    _init_real_git_repo(src)
    with Repo(str(src)) as repo:
        blob = Blob.from_string(b"not a commit")
        repo.object_store.add_object(blob)
        blob_id = blob.id
    (src / ".git" / "HEAD").write_bytes(blob_id + b"\n")

    with caplog.at_level(logging.ERROR, logger="xskill.installation"):
        with pytest.raises(GitHeadError) as raised:
            read_skill_head_sha(src)

    assert raised.value.error_type == "GIT_HEAD_NOT_COMMIT"
    assert str(src) not in caplog.text
    assert blob_id.decode("ascii") not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)


def test_read_skill_head_sha_wraps_repository_io_without_secret(
    tmp_path, monkeypatch, caplog,
):
    from xskill.ecosystems import installation

    src = _make_src(tmp_path / "private" / "srcs")
    (src / ".git").mkdir()

    def fail_repo_open(_path):
        raise OSError(
            "Authorization: Bearer repository-secret /root/private/repo"
        )

    monkeypatch.setattr(installation, "Repo", fail_repo_open)
    raised_error = None
    with caplog.at_level(logging.ERROR, logger="xskill.installation"):
        try:
            read_skill_head_sha(src)
        except GitHeadError as git_error:
            raised_error = git_error
            formatted_traceback = traceback.format_exc()

    assert raised_error is not None
    assert raised_error.error_type == "GIT_REPOSITORY_IO_ERROR"
    assert str(src) not in caplog.text
    assert "Authorization" not in caplog.text
    assert "repository-secret" not in caplog.text
    assert "/root/private/repo" not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)
    assert "repository-secret" not in formatted_traceback
    assert "/root/private/repo" not in formatted_traceback
    assert str(src) not in formatted_traceback
    assert "During handling of the above exception" not in formatted_traceback


def test_empty_repo_with_deleted_head_fails_as_missing(tmp_path, caplog):
    src = _make_src(tmp_path / "private" / "empty")
    repo = porcelain.init(str(src), bare=False)
    repo.close()
    (src / ".git" / "HEAD").unlink()

    with caplog.at_level(logging.ERROR, logger="xskill.installation"):
        with pytest.raises(GitHeadError) as raised:
            read_skill_head_sha(src)

    assert raised.value.error_type == "GIT_HEAD_MISSING"
    assert str(src) not in caplog.text


def test_close_failure_does_not_override_primary_git_error(
    tmp_path, monkeypatch, caplog,
):
    from xskill.ecosystems import installation

    src = _make_src(tmp_path / "private" / "srcs")
    _init_real_git_repo(src)
    (src / ".git" / "HEAD").write_bytes(b"damaged-head\n")
    real_repo_type = Repo

    class CloseFailRepo:
        def __init__(self, path):
            self._repo = real_repo_type(path)
            self.refs = self._repo.refs
            self.object_store = self._repo.object_store

        def close(self):
            self._repo.close()
            raise OSError(
                "Authorization: Bearer close-secret /root/private/close"
            )

    monkeypatch.setattr(installation, "Repo", CloseFailRepo)
    raised_error = None
    with caplog.at_level(logging.ERROR, logger="xskill.installation"):
        try:
            read_skill_head_sha(src)
        except GitHeadError as git_error:
            raised_error = git_error
            formatted_traceback = traceback.format_exc()

    assert raised_error is not None
    assert raised_error.error_type == "GIT_HEAD_DAMAGED"
    assert "error_type=GIT_HEAD_DAMAGED" in caplog.text
    assert "error_type=GIT_REPOSITORY_CLOSE_ERROR" in caplog.text
    assert "close-secret" not in caplog.text
    assert "/root/private/close" not in caplog.text
    assert "close-secret" not in formatted_traceback
    assert "/root/private/close" not in formatted_traceback
    assert str(src) not in formatted_traceback
    assert "During handling of the above exception" not in formatted_traceback


def test_close_failure_after_valid_head_fails_loud_safely(
    tmp_path, monkeypatch, caplog,
):
    from xskill.ecosystems import installation

    src = _make_src(tmp_path / "private" / "srcs")
    _init_real_git_repo(src)
    real_repo_type = Repo

    class CloseFailRepo:
        def __init__(self, path):
            self._repo = real_repo_type(path)
            self.refs = self._repo.refs
            self.object_store = self._repo.object_store

        def close(self):
            self._repo.close()
            raise OSError("close-secret /root/private/close")

    monkeypatch.setattr(installation, "Repo", CloseFailRepo)
    raised_error = None
    with caplog.at_level(logging.ERROR, logger="xskill.installation"):
        try:
            read_skill_head_sha(src)
        except GitHeadError as git_error:
            raised_error = git_error
            formatted_traceback = traceback.format_exc()

    assert raised_error is not None
    assert raised_error.error_type == "GIT_REPOSITORY_CLOSE_ERROR"
    assert "close-secret" not in caplog.text
    assert "close-secret" not in formatted_traceback
    assert "/root/private/close" not in formatted_traceback
    assert str(src) not in formatted_traceback
    assert "During handling of the above exception" not in formatted_traceback


def test_read_install_metadata_missing_is_normal(tmp_path):
    assert read_install_metadata(tmp_path / "missing-target") is None


def test_copy_install_writes_matching_target_and_sidecar_identity(tmp_path):
    src = _make_src(tmp_path / "srcs")
    dest = tmp_path / "out" / "skill-x"
    dest.parent.mkdir()

    assert install_dir(src, dest, force_mode="copy") == "copy"

    metadata = read_install_metadata(dest)
    marker = json.loads(
        (dest / COPY_INSTALL_MARKER_NAME).read_text(encoding="utf-8"),
    )
    assert metadata is not None
    assert marker["installation_id"] == metadata["installation_id"]
    assert marker["content_identity"] == metadata["content_identity"]
    assert marker["baseline_identity"] == metadata["baseline_identity"]
    baseline = read_copy_install_baseline(dest, src)
    assert set(baseline) == {"SKILL.md", "scripts/run.sh"}
    assert all(len(file_hash) == 64 for file_hash in baseline.values())


def test_read_install_metadata_damaged_fails_safely(
    tmp_path, caplog,
):
    dest = tmp_path / "private" / "target"
    dest.parent.mkdir(parents=True)
    metadata_path = install_metadata_path(dest)
    metadata_path.write_text(
        '{"secret":"metadata-secret", broken',
        encoding="utf-8",
    )

    raised_error = None
    with caplog.at_level(logging.ERROR, logger="xskill.installation"):
        try:
            read_install_metadata(dest)
        except InstallationMetadataError as metadata_error:
            raised_error = metadata_error
            formatted_traceback = traceback.format_exc()

    assert raised_error is not None
    assert raised_error.error_type == "INSTALL_METADATA_DAMAGED"
    assert str(dest) not in caplog.text
    assert "metadata-secret" not in caplog.text
    assert str(dest) not in formatted_traceback
    assert "metadata-secret" not in formatted_traceback
    assert "During handling of the above exception" not in formatted_traceback


def test_read_install_metadata_invalid_schema_fails_safely(
    tmp_path, caplog,
):
    dest = tmp_path / "private" / "target"
    dest.parent.mkdir(parents=True)
    install_metadata_path(dest).write_text(
        '{"mode":[],"source":"metadata-secret"}',
        encoding="utf-8",
    )

    with caplog.at_level(logging.ERROR, logger="xskill.installation"):
        with pytest.raises(InstallationMetadataError) as raised:
            read_install_metadata(dest)

    assert raised.value.error_type == "INSTALL_METADATA_DAMAGED"
    assert str(dest) not in caplog.text
    assert "metadata-secret" not in caplog.text


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO unsupported")
def test_read_install_metadata_rejects_fifo_without_opening_it(tmp_path):
    dest = tmp_path / "target"
    metadata_path = install_metadata_path(dest)
    os.mkfifo(metadata_path)

    with pytest.raises(InstallationMetadataError) as raised:
        read_install_metadata(dest)

    assert raised.value.error_type == "INSTALL_METADATA_DAMAGED"


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="Windows CI 不保证普通用户可创建 symlink",
)
def test_read_install_metadata_rejects_symlink(tmp_path):
    dest = tmp_path / "target"
    real_metadata = tmp_path / "real-meta.json"
    real_metadata.write_text(
        '{"mode":"copy","source":"/source"}',
        encoding="utf-8",
    )
    install_metadata_path(dest).symlink_to(real_metadata)

    with pytest.raises(InstallationMetadataError) as raised:
        read_install_metadata(dest)

    assert raised.value.error_type == "INSTALL_METADATA_DAMAGED"


def test_read_install_metadata_io_failure_has_no_exception_chain(
    tmp_path, monkeypatch, caplog,
):
    from xskill.ecosystems import installation

    dest = tmp_path / "private" / "target"

    class UnreadableMetadata:
        def read_text(self, **_kwargs):
            raise PermissionError(
                "Authorization: Bearer metadata-secret /root/private/meta"
            )

    monkeypatch.setattr(
        installation,
        "install_metadata_path",
        lambda _dest: UnreadableMetadata(),
    )
    raised_error = None
    with caplog.at_level(logging.ERROR, logger="xskill.installation"):
        try:
            read_install_metadata(dest)
        except InstallationMetadataError as metadata_error:
            raised_error = metadata_error
            formatted_traceback = traceback.format_exc()

    assert raised_error is not None
    assert raised_error.error_type == "INSTALL_METADATA_READ_FAILED"
    assert "metadata-secret" not in caplog.text
    assert "/root/private/meta" not in caplog.text
    assert "metadata-secret" not in formatted_traceback
    assert "/root/private/meta" not in formatted_traceback
    assert str(dest) not in formatted_traceback
    assert "During handling of the above exception" not in formatted_traceback


def test_write_install_metadata_failure_is_safe_and_loud(
    tmp_path, monkeypatch, caplog,
):
    from xskill.ecosystems import installation

    src = _make_src(tmp_path / "srcs")
    dest = tmp_path / "private" / "target"

    class UnwritableMetadata:
        def write_text(self, *_args, **_kwargs):
            raise PermissionError(
                "Authorization: Bearer write-secret /root/private/meta"
            )

    monkeypatch.setattr(
        installation,
        "install_metadata_path",
        lambda _dest: UnwritableMetadata(),
    )
    raised_error = None
    with caplog.at_level(logging.ERROR, logger="xskill.installation"):
        try:
            write_install_metadata(dest, src, "copy")
        except InstallationMetadataError as metadata_error:
            raised_error = metadata_error
            formatted_traceback = traceback.format_exc()

    assert raised_error is not None
    assert raised_error.error_type == "INSTALL_METADATA_WRITE_FAILED"
    assert "write-secret" not in caplog.text
    assert "/root/private/meta" not in caplog.text
    assert "write-secret" not in formatted_traceback
    assert "/root/private/meta" not in formatted_traceback
    assert str(dest) not in formatted_traceback
    assert "During handling of the above exception" not in formatted_traceback


def test_invalid_git_head_cannot_write_metadata_or_skip_copy(tmp_path):
    src = _make_src(tmp_path / "srcs")
    _init_real_git_repo(src)
    (src / ".git" / "HEAD").write_text("b" * 40, encoding="ascii")
    dest = tmp_path / "out" / "skill-x"
    dest.mkdir(parents=True)
    metadata_path = install_metadata_path(dest)

    with pytest.raises(GitHeadError):
        write_install_metadata(dest, src, "copy")
    assert not metadata_path.exists()

    metadata_path.write_text(
        '{"mode":"copy","source":"%s","source_sha":"bbbb"}'
        % str(src.resolve()).replace("\\", "\\\\"),
        encoding="utf-8",
    )
    with pytest.raises(GitHeadError):
        copy_install_is_current(src.resolve(), dest)


# ──────────────────────────────────────────────────────────────────
# T1. happy path —— 当前平台默认能 symlink（Linux/macOS）
# ──────────────────────────────────────────────────────────────────


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="Windows symlink 需要 Dev Mode；happy path 在该平台不保证",
)
def test_install_dir_uses_symlink_on_posix(tmp_path):
    src = _make_src(tmp_path / "srcs")
    dest = tmp_path / "out" / "skill-x"
    dest.parent.mkdir(parents=True)

    mode = install_dir(src, dest)

    assert mode == "symlink"
    assert dest.is_symlink()
    assert (dest / "SKILL.md").is_file()
    assert (dest / "scripts" / "run.sh").is_file()


# ──────────────────────────────────────────────────────────────────
# T2. symlink fail → junction success（模拟 Windows Dev Mode 关 + NTFS）
# ──────────────────────────────────────────────────────────────────


def test_install_dir_falls_back_to_junction_when_symlink_fails(tmp_path, monkeypatch):
    """模拟两步：symlink 抛 OSError；junction 返回成功。

    模拟 junction 时不能真跑 ``cmd /c mklink``（Linux 没 cmd），所以
    monkeypatch ``_try_junction`` 自己做一次 copytree（拷贝完返回 True，
    模拟 Windows 上 junction 的可见效果——读端看见目录里有文件）。
    """
    src = _make_src(tmp_path / "srcs")
    dest = tmp_path / "out" / "skill-x"
    dest.parent.mkdir(parents=True)

    def _fail_symlink(s, d):
        return False

    def _fake_junction(s, d):
        # 模拟 junction：在测试机上无法真建 NTFS junction，用 copytree 代表
        # "junction 成功后 dest 可见为目录"。这保证下游断言 dest 存在能过。
        import shutil
        shutil.copytree(s, d)
        return True

    monkeypatch.setattr(install_fallback, "_try_symlink", _fail_symlink)
    monkeypatch.setattr(install_fallback, "_try_junction", _fake_junction)

    mode = install_dir(src, dest)

    assert mode == "junction"
    assert dest.is_dir()
    # symlink 已经被 stub 掉，dest 是 stub 出来的目录，不是 symlink
    assert not dest.is_symlink()
    assert (dest / "SKILL.md").is_file()


# ──────────────────────────────────────────────────────────────────
# T3. symlink fail + junction fail → copy
# ──────────────────────────────────────────────────────────────────


def test_install_dir_falls_back_to_copy_when_both_fail(tmp_path, monkeypatch, caplog):
    src = _make_src(tmp_path / "srcs")
    dest = tmp_path / "out" / "skill-x"
    dest.parent.mkdir(parents=True)

    monkeypatch.setattr(install_fallback, "_try_symlink", lambda s, d: False)
    monkeypatch.setattr(install_fallback, "_try_junction", lambda s, d: False)

    with caplog.at_level(logging.WARNING, logger="xskill.install_fallback"):
        mode = install_dir(src, dest)

    assert mode == "copy"
    assert dest.is_dir()
    assert not dest.is_symlink()
    assert (dest / "SKILL.md").is_file()
    assert (dest / "scripts" / "run.sh").is_file()
    # copy 模式必须 warning，提醒 round-trip 失效
    assert any("fell back to copy" in rec.message for rec in caplog.records)


# ──────────────────────────────────────────────────────────────────
# T4. copy 模式下源更新不穿透（snapshot 语义）
# ──────────────────────────────────────────────────────────────────


def test_copy_mode_is_snapshot_not_live(tmp_path, monkeypatch):
    """copy 模式下：装好后改源仓，dest 看不到改动 —— 这是预期行为，
    UserEditAbsorbAgent 在这条路径下失效。
    """
    src = _make_src(tmp_path / "srcs")
    dest = tmp_path / "out" / "skill-x"
    dest.parent.mkdir(parents=True)

    monkeypatch.setattr(install_fallback, "_try_symlink", lambda s, d: False)
    monkeypatch.setattr(install_fallback, "_try_junction", lambda s, d: False)
    assert install_dir(src, dest) == "copy"

    # 改源
    (src / "SKILL.md").write_text("---\nname: skill-x\nversion: 2\n---\nupdated\n",
                                  encoding="utf-8")

    # dest 仍是旧副本
    dest_text = (dest / "SKILL.md").read_text(encoding="utf-8")
    assert "version: 2" not in dest_text
    assert "updated" not in dest_text


# ──────────────────────────────────────────────────────────────────
# T5. symlink 模式下源更新穿透（live mount 语义）
# ──────────────────────────────────────────────────────────────────


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="Windows symlink 需要 Dev Mode；live mount 测试在该平台不保证",
)
def test_symlink_mode_is_live_mount(tmp_path):
    src = _make_src(tmp_path / "srcs")
    dest = tmp_path / "out" / "skill-x"
    dest.parent.mkdir(parents=True)

    assert install_dir(src, dest) == "symlink"

    (src / "SKILL.md").write_text("updated body", encoding="utf-8")
    assert (dest / "SKILL.md").read_text(encoding="utf-8") == "updated body"

    # 新增源文件也立即可见
    (src / "scripts" / "new.sh").write_text("new\n", encoding="utf-8")
    assert (dest / "scripts" / "new.sh").is_file()


# ──────────────────────────────────────────────────────────────────
# T6. install_to_claude_code 在 copy 模式下打 warning 但返回正确路径
# ──────────────────────────────────────────────────────────────────


def test_install_to_claude_code_copy_mode_returns_correct_path(tmp_path, monkeypatch, caplog):
    """端到端：``install_to_claude_code`` 在 copy fallback 下也要返回
    ``<home>/.claude/skills/<name>/SKILL.md`` 且文件实际存在——这是 SDK
    调用方依赖的契约，不能因为换了 fallback 就破。
    """
    from xskill import ecosystems
    from xskill.skill.frontmatter import serialize as fm_serialize

    skill_src = tmp_path / "srcs" / "list-py"
    skill_src.mkdir(parents=True)
    fm = {
        "name": "list-py",
        "description": "List Python files.",
        "version": 1,
        "source_trajs": ["traj_0001"],
        "metadata": {"tags": ["bash"], "frozen": False},
    }
    (skill_src / "SKILL.md").write_text(fm_serialize(fm, "# list-py\n"), encoding="utf-8")

    monkeypatch.setattr(install_fallback, "_try_symlink", lambda s, d: False)
    monkeypatch.setattr(install_fallback, "_try_junction", lambda s, d: False)

    fake_home = tmp_path / "fake_home"
    with caplog.at_level(logging.WARNING, logger="xskill.ecosystems"):
        dest = ecosystems.install_to_claude_code(skill_src, target_root=fake_home)

    assert dest == fake_home / ".claude" / "skills" / "list-py" / "SKILL.md"
    assert dest.is_file()
    # copy 模式 warning 通过 ecosystems logger 透出（让运维看到）
    assert any("copy-mode install" in rec.message for rec in caplog.records)


# ──────────────────────────────────────────────────────────────────
# T7. install_dir 不接管 "dest 已存在"——上层职责
# ──────────────────────────────────────────────────────────────────


def test_install_dir_does_not_overwrite_existing_dest(tmp_path):
    """``install_dir`` 假设 dest 不存在；如果存在，symlink/junction 会失败，
    copy 也会抛 FileExistsError。这是合约（让上层显式处理冲突，不在 fallback
    层做静默覆盖）。
    """
    src = _make_src(tmp_path / "srcs")
    dest = tmp_path / "out" / "skill-x"
    dest.parent.mkdir(parents=True)
    dest.mkdir()  # 已存在

    with pytest.raises((FileExistsError, OSError)):
        install_dir(src, dest)


# ──────────────────────────────────────────────────────────────────
# T8. _try_junction 在非 Windows 上立即返回 False —— 不调用 subprocess
# ──────────────────────────────────────────────────────────────────


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="本 case 验证非 Windows 的 short-circuit",
)
def test_try_junction_skips_on_non_windows(tmp_path, monkeypatch):
    src = _make_src(tmp_path / "srcs")
    dest = tmp_path / "out" / "skill-x"
    dest.parent.mkdir(parents=True)

    # 哨兵：subprocess.run 若被调用则测试失败
    def _boom(*a, **kw):
        raise AssertionError("_try_junction should not invoke subprocess on non-Windows")

    monkeypatch.setattr(install_fallback.subprocess, "run", _boom)
    assert install_fallback._try_junction(src, dest) is False


# ──────────────────────────────────────────────────────────────────
# T9. 平台/模式 parametrize —— 三平台的预期 mode 矩阵
# ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "symlink_ok, junction_ok, expected_mode",
    [
        (True, False, "symlink"),    # POSIX 常态
        (True, True, "symlink"),     # symlink 优先即使 junction 也行
        (False, True, "junction"),   # Windows Dev Mode off, NTFS 同卷
        (False, False, "copy"),      # 最坏：跨盘/非 NTFS
    ],
)
def test_install_dir_mode_matrix(tmp_path, monkeypatch, symlink_ok, junction_ok, expected_mode):
    """模式矩阵：symlink/junction 各自的成败组合 → 最终 mode。

    所有四种组合都把 dest 物化为目录（用 copytree 当 stub），保证下游断言
    dest 可见。
    """
    import shutil

    src = _make_src(tmp_path / "srcs", name=f"skill-{expected_mode}")
    dest = tmp_path / "out" / f"skill-{expected_mode}"
    dest.parent.mkdir(parents=True, exist_ok=True)

    def _sym(s, d):
        if symlink_ok:
            try:
                d.symlink_to(s, target_is_directory=True)
                return True
            except (OSError, NotImplementedError):
                return False
        return False

    def _junc(s, d):
        if junction_ok:
            shutil.copytree(s, d)
            return True
        return False

    monkeypatch.setattr(install_fallback, "_try_symlink", _sym)
    monkeypatch.setattr(install_fallback, "_try_junction", _junc)

    mode = install_dir(src, dest)
    assert mode == expected_mode
    assert (dest / "SKILL.md").is_file()


# ──────────────────────────────────────────────────────────────────
# T10. copy 模式 no-op 守卫 —— 源未变不重装，源变了重装
# ──────────────────────────────────────────────────────────────────


def _force_copy_mode(monkeypatch):
    """把 symlink/junction 都打成失败，逼 install 走 copy 模式。"""
    monkeypatch.setattr(install_fallback, "_try_symlink", lambda s, d: False)
    monkeypatch.setattr(install_fallback, "_try_junction", lambda s, d: False)


def _count_do_copy(monkeypatch):
    """包一层 _do_copy 计数，返回可变的计数容器。"""
    copy_calls = []
    original_do_copy = install_fallback._do_copy

    def _counting_do_copy(src_dir, dest):
        copy_calls.append((src_dir, dest))
        original_do_copy(src_dir, dest)

    monkeypatch.setattr(install_fallback, "_do_copy", _counting_do_copy)
    return copy_calls


def test_copy_reinstall_skipped_when_git_source_unchanged(tmp_path, monkeypatch):
    """git 源 sha 未变时，第二次 install 应命中守卫直接 no-op。"""
    from xskill import ecosystems

    src = _make_src(tmp_path / "srcs")
    _init_real_git_repo(src)
    fake_home = tmp_path / "fake_home"
    _force_copy_mode(monkeypatch)
    copy_calls = _count_do_copy(monkeypatch)

    first = ecosystems.install_to_claude_code(src, target_root=fake_home)
    assert len(copy_calls) == 1
    assert first.is_file()

    second = ecosystems.install_to_claude_code(src, target_root=fake_home)
    assert second == first
    assert len(copy_calls) == 1  # 守卫命中，没有重新 copy
    assert (first.parent / "scripts" / "run.sh").is_file()  # dest 内容还在


def test_copy_reinstall_happens_when_git_source_changed(tmp_path, monkeypatch):
    """git 源 sha 变了（模拟新 commit），第二次 install 必须重装。"""
    from xskill import ecosystems

    src = _make_src(tmp_path / "srcs")
    _init_real_git_repo(src)
    fake_home = tmp_path / "fake_home"
    _force_copy_mode(monkeypatch)
    copy_calls = _count_do_copy(monkeypatch)

    ecosystems.install_to_claude_code(src, target_root=fake_home)
    (src / "SKILL.md").write_text("---\nname: skill-x\n---\nv2\n", encoding="utf-8")
    porcelain.add(str(src))
    porcelain.commit(
        str(src),
        message=b"source v2",
        author=b"test <test@example.com>",
        committer=b"test <test@example.com>",
    )

    dest_md = ecosystems.install_to_claude_code(src, target_root=fake_home)
    assert len(copy_calls) == 2
    assert "v2" in dest_md.read_text(encoding="utf-8")


def test_copy_reinstall_skipped_when_skillhub_sha_unchanged(tmp_path, monkeypatch):
    """skillhub 源（非 git 仓）用 .xskill_skillhub.json 的 sha 判定未变 → no-op。

    走 install_to_opencode 覆盖 opencode.py 里复制的那份守卫调用点。
    """
    from xskill import ecosystems

    src = _make_src(tmp_path / "srcs")
    (src / ".xskill_skillhub.json").write_text('{"sha": "hub-sha-1"}', encoding="utf-8")
    fake_home = tmp_path / "fake_home"
    _force_copy_mode(monkeypatch)
    copy_calls = _count_do_copy(monkeypatch)

    ecosystems.install_to_opencode(src, target_root=fake_home)
    assert len(copy_calls) == 1

    ecosystems.install_to_opencode(src, target_root=fake_home)
    assert len(copy_calls) == 1  # sha 未变，守卫命中

    (src / ".xskill_skillhub.json").write_text('{"sha": "hub-sha-2"}', encoding="utf-8")
    ecosystems.install_to_opencode(src, target_root=fake_home)
    assert len(copy_calls) == 2  # sha 变了，重装


def test_copy_reinstall_happens_with_legacy_meta_without_sha(tmp_path, monkeypatch):
    """老版本 meta 没有 source_sha 字段：无法判定 → 保守重装，且不崩。"""
    import json as json_mod

    from xskill import ecosystems
    from xskill.ecosystems._fallback import _install_meta_path

    src = _make_src(tmp_path / "srcs")
    _init_real_git_repo(src)
    fake_home = tmp_path / "fake_home"
    _force_copy_mode(monkeypatch)
    copy_calls = _count_do_copy(monkeypatch)

    dest_md = ecosystems.install_to_claude_code(src, target_root=fake_home)
    meta_path = _install_meta_path(dest_md.parent)
    legacy_meta = json_mod.loads(meta_path.read_text(encoding="utf-8"))
    legacy_meta.pop("source_sha")
    meta_path.write_text(json_mod.dumps(legacy_meta), encoding="utf-8")

    ecosystems.install_to_claude_code(src, target_root=fake_home)
    assert len(copy_calls) == 2  # 老 meta 判不了，重装
