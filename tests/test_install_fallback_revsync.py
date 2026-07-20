"""
test_install_fallback_revsync.py -- install_dir 通用 reverse_sync + install-meta 测试
==================================================================================

P2（通用 copy+reverse_sync 框架）新加的钩子覆盖：

* T1 — install-meta 写到 ``dest.parent`` 旁（而非 dest 内部）—— 三种模式都一样
* T2 — ``_is_link_or_junction`` 对 Linux symlink 与"模拟 Windows reparse point" 都 True
* T3 — copy 模式 install + 用户改 dest + 再装：dest 改动被灌回 source
* T4 — link 模式 install：meta 写在 dest.parent，跨重装不丢；mode != copy 时不触发 reverse_sync
* T5 — ``install_dir(force_mode="copy")``：跳过 symlink/junction，直接 copytree
* T6 — ``_reset_dest``：link/junction/dir/file 都能干净清理，不抛 OSError（issue #35 回归锚点）
* T7 — ``install_dir(auto_reset=True)``：覆盖前自动跑 reverse_sync + _reset_dest
"""
from __future__ import annotations

import json
import os
import stat
import sys
import time
from pathlib import Path
from unittest.mock import Mock

import pytest

from xskill.ecosystems import _fallback as fb
from xskill.ecosystems.installation import InstallSafetyError
from xskill.ecosystems._fallback import (
    _install_meta_path,
    _is_link_or_junction,
    _maybe_reverse_sync_before_overwrite,
    _reset_dest,
    install_dir,
)


# ──────────────────────────────────────────────────────────────────
# helpers
# ──────────────────────────────────────────────────────────────────


def _make_skill_src(root: Path, name: str = "test-skill") -> Path:
    """造一个最小 skill 源目录，模拟 ``~/.xskill/skill/<name>``。"""
    src = root / name
    (src / "scripts").mkdir(parents=True)
    (src / "SKILL.md").write_text(
        "---\nname: test-skill\ndescription: A test skill.\n---\n# Body\n",
        encoding="utf-8",
    )
    (src / "scripts" / "run.sh").write_text("#!/bin/bash\necho hi\n", encoding="utf-8")
    return src


# ──────────────────────────────────────────────────────────────────
# T1: install-meta 永远写到 dest.parent，三种模式都一样
# ──────────────────────────────────────────────────────────────────


@pytest.mark.skipif(sys.platform == "win32",
                    reason="symlink 在 Windows 需要 DevMode")
def test_install_meta_at_dest_parent_for_symlink_mode(tmp_path):
    src = _make_skill_src(tmp_path / "srcs")
    dest = tmp_path / "out" / "test-skill"
    dest.parent.mkdir(parents=True)

    mode = install_dir(src, dest)

    assert mode == "symlink"
    meta = _install_meta_path(dest)
    # meta 不在 dest 内部（否则 symlink 模式会污染 source 仓）
    assert meta.parent == dest.parent
    assert meta.name == ".xskill-install-meta-test-skill.json"
    assert meta.is_file()
    data = json.loads(meta.read_text(encoding="utf-8"))
    assert data["mode"] == "symlink"
    assert Path(data["source"]) == src.resolve()
    assert isinstance(data["installed_at"], float)
    # link 模式：dest 内部不应该有 meta（不污染源仓）
    assert not (src / ".xskill-install-meta-test-skill.json").exists()


def test_install_meta_at_dest_parent_for_copy_mode(tmp_path, monkeypatch):
    src = _make_skill_src(tmp_path / "srcs")
    dest = tmp_path / "out" / "test-skill"
    dest.parent.mkdir(parents=True)

    monkeypatch.setattr(fb, "_try_symlink", Mock(return_value=False))
    monkeypatch.setattr(fb, "_try_junction", Mock(return_value=False))

    mode = install_dir(src, dest)

    assert mode == "copy"
    meta = _install_meta_path(dest)
    assert meta.parent == dest.parent
    assert meta.is_file()
    data = json.loads(meta.read_text(encoding="utf-8"))
    assert data["mode"] == "copy"


# ──────────────────────────────────────────────────────────────────
# T2: _is_link_or_junction
# ──────────────────────────────────────────────────────────────────


@pytest.mark.skipif(sys.platform == "win32",
                    reason="symlink 需要 DevMode；mock Win 行为另起 case")
def test_is_link_or_junction_detects_posix_symlink(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    link = tmp_path / "link"
    link.symlink_to(src, target_is_directory=True)

    assert _is_link_or_junction(link) is True
    assert _is_link_or_junction(src) is False  # 真目录
    assert _is_link_or_junction(tmp_path / "nonexistent") is False


def test_is_link_or_junction_handles_nonexistent(tmp_path):
    """不存在的路径不应抛错——返回 False。"""
    assert _is_link_or_junction(tmp_path / "does_not_exist") is False


def test_is_link_or_junction_handles_file(tmp_path):
    """普通文件不算 link/junction。"""
    f = tmp_path / "f.txt"
    f.write_text("hi")
    assert _is_link_or_junction(f) is False


# ──────────────────────────────────────────────────────────────────
# T3: copy 模式 + 用户改 dest + 重装 → 改灌回 source
# ──────────────────────────────────────────────────────────────────


def _build_real_skill_repo(root: Path, name: str = "demo-skill") -> Path:
    """造一个有 git 仓的 skill —— ``reverse_sync_copy_dest`` 要锁源仓。"""
    import subprocess
    sk = root / "src" / name
    sk.mkdir(parents=True)
    (sk / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: A demo skill.\nversion: 1\n---\n# {name}\n\nBody v1.\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "-q", str(sk)], check=True)
    subprocess.run(["git", "-C", str(sk), "config", "user.email", "x@y.z"], check=True)
    subprocess.run(["git", "-C", str(sk), "config", "user.name", "x"], check=True)
    subprocess.run(["git", "-C", str(sk), "add", "."], check=True)
    subprocess.run(["git", "-C", str(sk), "commit", "-q", "-m", "init"], check=True)
    return sk


def test_copy_mode_user_edit_round_trips_via_auto_reset(tmp_path, monkeypatch):
    """copy 模式 install → 用户改 dest → install_dir(auto_reset=True) 再装
    → reverse_sync 灌回 source。

    回归 issue #34 的核心场景：ngagent 的用户改不应在 reinstall 时丢失。
    """
    src = _build_real_skill_repo(tmp_path)
    dest = tmp_path / "out" / "demo-skill"
    dest.parent.mkdir(parents=True)

    # 强 force copy 模拟 ngagent 路径
    mode = install_dir(src, dest, force_mode="copy")
    assert mode == "copy"
    assert (dest / "SKILL.md").is_file()
    meta = _install_meta_path(dest)
    assert meta.is_file()

    # 用户改 dest（让 mtime 比 installed_at 至少大 1 秒）
    time.sleep(1.1)
    (dest / "SKILL.md").write_text("USER MODIFIED IN DEST\n", encoding="utf-8")
    (dest / "scripts").mkdir()
    (dest / "scripts" / "go.sh").write_text("#!/bin/sh\nrun\n", encoding="utf-8")

    # 让 reverse_sync 跳过 quiet_seconds 检查（测试用，免等 3 分钟）。
    # 不能光 monkeypatch USER_EDIT_QUIET_SECONDS——``reverse_sync_copy_dest``
    # 的默认参数在函数定义时已绑定全局值；改全局对它无效。
    # 直接 patch ``reverse_sync_copy_dest`` 为一个总是传 quiet_seconds=0 的薄壳。
    from xskill.agents import user_edit_absorb_agent as ua
    _real = ua.reverse_sync_copy_dest

    def _force_quiet_zero(d, s, **kw):
        kw["quiet_seconds"] = 0
        return _real(d, s, **kw)

    monkeypatch.setattr(ua, "reverse_sync_copy_dest", _force_quiet_zero)

    # 重装（auto_reset=True 触发 reverse_sync→reset→copy）
    install_dir(src, dest, force_mode="copy", auto_reset=True)

    # source 已经吸收了 user 改
    assert "USER MODIFIED IN DEST" in (src / "SKILL.md").read_text(encoding="utf-8")
    assert (src / "scripts" / "go.sh").is_file()
    # dest 被覆盖回 source 的当前内容（含 user 改）
    assert "USER MODIFIED IN DEST" in (dest / "SKILL.md").read_text(encoding="utf-8")


def test_reverse_sync_uses_install_baseline_for_three_way_merge(tmp_path):
    """源仓与安装目录改不同文件时，只回流安装目录相对基线的改动。"""
    from xskill.agents import user_edit_absorb_agent as user_absorb

    src = _build_real_skill_repo(tmp_path)
    (src / "references").mkdir()
    source_only_path = src / "references" / "source-only.md"
    dest_only_path = src / "references" / "dest-only.md"
    source_only_path.write_text("baseline source\n", encoding="utf-8")
    dest_only_path.write_text("baseline dest\n", encoding="utf-8")
    dest = tmp_path / "out" / "demo-skill"
    dest.parent.mkdir(parents=True)
    install_dir(src, dest, force_mode="copy")

    source_only_path.write_text("source v2\n", encoding="utf-8")
    time.sleep(1.1)
    (dest / "references" / "dest-only.md").write_text(
        "dest edit\n", encoding="utf-8",
    )

    assert user_absorb.reverse_sync_copy_dest(
        dest, src, quiet_seconds=0,
    ) == user_absorb.ReverseSyncStatus.SYNCED
    assert source_only_path.read_text(encoding="utf-8") == "source v2\n"
    assert dest_only_path.read_text(encoding="utf-8") == "dest edit\n"


def test_reverse_sync_rejects_three_way_content_conflict(tmp_path):
    """同一文件在源仓和安装目录相对基线分叉时不得覆盖任一侧。"""
    from xskill.agents import user_edit_absorb_agent as user_absorb

    src = _build_real_skill_repo(tmp_path)
    dest = tmp_path / "out" / "demo-skill"
    dest.parent.mkdir(parents=True)
    install_dir(src, dest, force_mode="copy")
    (src / "SKILL.md").write_text("source v2\n", encoding="utf-8")
    time.sleep(1.1)
    (dest / "SKILL.md").write_text("dest v2\n", encoding="utf-8")

    assert user_absorb.reverse_sync_copy_dest(
        dest, src, quiet_seconds=0,
    ) == user_absorb.ReverseSyncStatus.FAILED
    assert (src / "SKILL.md").read_text(
        encoding="utf-8",
    ) == "source v2\n"
    assert (dest / "SKILL.md").read_text(
        encoding="utf-8",
    ) == "dest v2\n"


def test_reverse_sync_detects_edit_with_restored_mtime(tmp_path):
    from xskill.agents import user_edit_absorb_agent as user_absorb

    src = _build_real_skill_repo(tmp_path)
    dest = tmp_path / "out" / "demo-skill"
    dest.parent.mkdir(parents=True)
    install_dir(src, dest, force_mode="copy")
    dest_skill = dest / "SKILL.md"
    original_stat = dest_skill.stat()
    dest_skill.write_bytes(b"X" * original_stat.st_size)
    os.utime(
        dest_skill,
        ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
    )

    assert user_absorb.reverse_sync_copy_dest(
        dest, src, quiet_seconds=0,
    ) == user_absorb.ReverseSyncStatus.SYNCED
    assert (src / "SKILL.md").read_bytes() == (
        b"X" * original_stat.st_size
    )


def test_reverse_sync_rejects_content_swap_after_hash(
    tmp_path, monkeypatch,
):
    from xskill.agents import user_edit_absorb_agent as user_absorb

    src = _build_real_skill_repo(tmp_path)
    dest = tmp_path / "out" / "demo-skill"
    dest.parent.mkdir(parents=True)
    install_dir(src, dest, force_mode="copy")
    source_before = (src / "SKILL.md").read_bytes()
    dest_skill = dest / "SKILL.md"
    time.sleep(1.1)
    dest_skill.write_bytes(b"D" * len(source_before))
    original_hash = user_absorb._hash_verified_file
    swapped = False

    def swap_dest_after_hash(root, file_info):
        nonlocal swapped
        file_hash = original_hash(root, file_info)
        if root == dest and not swapped:
            swapped = True
            current_stat = dest_skill.stat()
            dest_skill.write_bytes(b"R" * len(source_before))
            os.utime(
                dest_skill,
                ns=(current_stat.st_atime_ns, current_stat.st_mtime_ns),
            )
        return file_hash

    monkeypatch.setattr(
        user_absorb, "_hash_verified_file", swap_dest_after_hash,
    )

    assert user_absorb.reverse_sync_copy_dest(
        dest, src, quiet_seconds=0,
    ) == user_absorb.ReverseSyncStatus.FAILED
    assert (src / "SKILL.md").read_bytes() == source_before
    assert dest_skill.read_bytes() == b"R" * len(source_before)


def test_auto_reset_recent_edit_preserves_destination(tmp_path):
    """用户仍在编辑时必须中止覆盖，dest/source 均不能丢内容。"""
    src = _build_real_skill_repo(tmp_path)
    dest = tmp_path / "out" / "demo-skill"
    dest.parent.mkdir(parents=True)
    install_dir(src, dest, force_mode="copy")
    source_before = (src / "SKILL.md").read_text(encoding="utf-8")
    metadata = json.loads(
        _install_meta_path(dest).read_text(encoding="utf-8"),
    )
    recent_mtime = metadata["installed_at"] + 2.0
    (dest / "SKILL.md").write_text(
        "RECENT USER EDIT\n", encoding="utf-8",
    )
    os.utime(dest / "SKILL.md", (recent_mtime, recent_mtime))

    with pytest.raises(InstallSafetyError) as raised:
        install_dir(src, dest, force_mode="copy", auto_reset=True)

    assert raised.value.error_type == "REVERSE_SYNC_RECENT_EDIT"
    assert (dest / "SKILL.md").read_text(
        encoding="utf-8",
    ) == "RECENT USER EDIT\n"
    assert (src / "SKILL.md").read_text(encoding="utf-8") == source_before


def test_auto_reset_reverse_sync_failure_preserves_destination(
    tmp_path, monkeypatch, caplog,
):
    """回流复制异常时安全失败，不覆盖 dest，也不泄露底层异常。"""
    from xskill.agents import user_edit_absorb_agent as user_absorb

    src = _build_real_skill_repo(tmp_path)
    dest = tmp_path / "out" / "demo-skill"
    dest.parent.mkdir(parents=True)
    install_dir(src, dest, force_mode="copy")
    source_before = (src / "SKILL.md").read_text(encoding="utf-8")
    time.sleep(1.1)
    (dest / "SKILL.md").write_text(
        "UNSYNCED USER EDIT\n", encoding="utf-8",
    )
    real_reverse_sync = user_absorb.reverse_sync_copy_dest

    def reverse_without_quiet_period(dest_dir, source_dir):
        return real_reverse_sync(
            dest_dir, source_dir, quiet_seconds=0,
        )

    def fail_copy(*_args, **_kwargs):
        raise OSError(
            "Authorization: Bearer reverse-secret /root/private/reverse"
        )

    monkeypatch.setattr(
        user_absorb,
        "reverse_sync_copy_dest",
        reverse_without_quiet_period,
    )
    monkeypatch.setattr(
        user_absorb, "_copy_verified_file_to_stage", fail_copy,
    )

    with caplog.at_level("WARNING"):
        with pytest.raises(InstallSafetyError) as raised:
            install_dir(src, dest, force_mode="copy", auto_reset=True)

    assert raised.value.error_type == "REVERSE_SYNC_FAILED"
    assert (dest / "SKILL.md").read_text(
        encoding="utf-8",
    ) == "UNSYNCED USER EDIT\n"
    assert (src / "SKILL.md").read_text(encoding="utf-8") == source_before
    assert "reverse-secret" not in caplog.text
    assert "/root/private/reverse" not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="Windows CI 不保证普通用户可创建 symlink",
)
def test_reverse_sync_rejects_symlink_without_touching_source(tmp_path):
    from xskill.agents.user_edit_absorb_agent import (
        ReverseSyncStatus,
        reverse_sync_copy_dest,
    )

    src = _build_real_skill_repo(tmp_path)
    dest = tmp_path / "out" / "demo-skill"
    dest.parent.mkdir(parents=True)
    install_dir(src, dest, force_mode="copy")
    source_before = (src / "SKILL.md").read_bytes()
    (dest / "unsafe-link").symlink_to(src / "SKILL.md")

    status = reverse_sync_copy_dest(
        dest, src, quiet_seconds=0,
    )

    assert status == ReverseSyncStatus.FAILED
    assert (src / "SKILL.md").read_bytes() == source_before
    assert (dest / "unsafe-link").is_symlink()


@pytest.mark.skipif(
    os.open not in os.supports_dir_fd,
    reason="openat/dir_fd unavailable",
)
def test_reverse_sync_rejects_parent_swap_between_scan_and_open(
    tmp_path, monkeypatch,
):
    from xskill.agents import user_edit_absorb_agent as user_absorb

    src = _build_real_skill_repo(tmp_path)
    nested_source = src / "references" / "note.md"
    nested_source.parent.mkdir()
    nested_source.write_text("source\n", encoding="utf-8")
    dest = tmp_path / "out" / "demo-skill"
    dest.parent.mkdir(parents=True)
    install_dir(src, dest, force_mode="copy")
    time.sleep(1.1)
    nested_dest = dest / "references" / "note.md"
    nested_dest.write_text("user edit\n", encoding="utf-8")
    source_before = nested_source.read_bytes()

    external = tmp_path / "external"
    external.mkdir()
    (external / "note.md").write_text("external secret\n", encoding="utf-8")
    moved_references = dest / "references-original"
    original_open = user_absorb.os.open
    swapped = False

    def swap_parent_after_root_open(path, flags, *args, **kwargs):
        nonlocal swapped
        file_descriptor = original_open(path, flags, *args, **kwargs)
        if path == dest and not swapped:
            swapped = True
            (dest / "references").replace(moved_references)
            (dest / "references").symlink_to(
                external, target_is_directory=True,
            )
        return file_descriptor

    monkeypatch.setattr(user_absorb.os, "open", swap_parent_after_root_open)

    status = user_absorb.reverse_sync_copy_dest(
        dest, src, quiet_seconds=0,
    )

    assert status == user_absorb.ReverseSyncStatus.FAILED
    assert nested_source.read_bytes() == source_before
    assert (external / "note.md").read_text(
        encoding="utf-8",
    ) == "external secret\n"


@pytest.mark.skipif(
    not hasattr(os, "mkfifo"),
    reason="当前平台不支持 FIFO",
)
def test_reverse_sync_rejects_fifo_without_touching_source(tmp_path):
    from xskill.agents.user_edit_absorb_agent import (
        ReverseSyncStatus,
        reverse_sync_copy_dest,
    )

    src = _build_real_skill_repo(tmp_path)
    dest = tmp_path / "out" / "demo-skill"
    dest.parent.mkdir(parents=True)
    install_dir(src, dest, force_mode="copy")
    source_before = (src / "SKILL.md").read_bytes()
    fifo_path = dest / "unsafe-fifo"
    os.mkfifo(fifo_path)

    status = reverse_sync_copy_dest(
        dest, src, quiet_seconds=0,
    )

    assert status == ReverseSyncStatus.FAILED
    assert (src / "SKILL.md").read_bytes() == source_before
    assert fifo_path.exists()


@pytest.mark.skipif(
    not hasattr(os, "mkfifo") or os.open not in os.supports_dir_fd,
    reason="FIFO/openat unsupported",
)
def test_reverse_sync_fifo_swap_between_lstat_and_open_never_blocks(
    tmp_path, monkeypatch,
):
    from xskill.agents import user_edit_absorb_agent as user_absorb

    src = _build_real_skill_repo(tmp_path)
    dest = tmp_path / "out" / "demo-skill"
    dest.parent.mkdir(parents=True)
    install_dir(src, dest, force_mode="copy")
    source_before = (src / "SKILL.md").read_bytes()
    time.sleep(1.1)
    (dest / "SKILL.md").write_text("USER EDIT\n", encoding="utf-8")
    moved_file = dest / "SKILL.original"
    original_open = user_absorb.os.open
    swapped = False

    def swap_regular_file_for_fifo(path, flags, *args, **kwargs):
        nonlocal swapped
        if (
            path == "SKILL.md"
            and kwargs.get("dir_fd") is not None
            and not swapped
        ):
            swapped = True
            (dest / "SKILL.md").replace(moved_file)
            os.mkfifo(dest / "SKILL.md")
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(
        user_absorb.os, "open", swap_regular_file_for_fifo,
    )
    started_at = time.monotonic()
    status = user_absorb.reverse_sync_copy_dest(
        dest, src, quiet_seconds=0,
    )

    assert time.monotonic() - started_at < 2
    assert status == user_absorb.ReverseSyncStatus.FAILED
    assert (src / "SKILL.md").read_bytes() == source_before


def test_reverse_sync_fails_closed_without_safe_directory_open(
    tmp_path, monkeypatch,
):
    from xskill.agents import user_edit_absorb_agent as user_absorb

    src = _build_real_skill_repo(tmp_path)
    dest = tmp_path / "out" / "demo-skill"
    dest.parent.mkdir(parents=True)
    install_dir(src, dest, force_mode="copy")
    source_before = (src / "SKILL.md").read_bytes()
    time.sleep(1.1)
    (dest / "SKILL.md").write_text("USER EDIT\n", encoding="utf-8")
    monkeypatch.setattr(
        user_absorb, "_OPEN_SUPPORTS_DIR_FD", False,
    )

    assert user_absorb.reverse_sync_copy_dest(
        dest, src, quiet_seconds=0,
    ) == user_absorb.ReverseSyncStatus.FAILED
    assert (src / "SKILL.md").read_bytes() == source_before


def test_reverse_sync_rejects_simulated_windows_source_reparse(
    tmp_path, monkeypatch,
):
    from xskill.agents import user_edit_absorb_agent as user_absorb

    src = _build_real_skill_repo(tmp_path)
    dest = tmp_path / "out" / "demo-skill"
    dest.parent.mkdir(parents=True)
    install_dir(src, dest, force_mode="copy")
    source_before = (src / "SKILL.md").read_bytes()
    time.sleep(1.1)
    (dest / "SKILL.md").write_text("USER EDIT\n", encoding="utf-8")
    source_stat = src.lstat()
    original_is_reparse = user_absorb._is_reparse_point

    def mark_source_as_reparse(file_stat):
        return (
            file_stat.st_dev == source_stat.st_dev
            and file_stat.st_ino == source_stat.st_ino
        ) or original_is_reparse(file_stat)

    monkeypatch.setattr(
        user_absorb, "_is_reparse_point", mark_source_as_reparse,
    )

    assert user_absorb.reverse_sync_copy_dest(
        dest, src, quiet_seconds=0,
    ) == user_absorb.ReverseSyncStatus.FAILED
    assert (src / "SKILL.md").read_bytes() == source_before


def test_reverse_sync_partial_commit_failure_rolls_back_source(
    tmp_path, monkeypatch,
):
    from xskill.agents import user_edit_absorb_agent as user_absorb

    src = _build_real_skill_repo(tmp_path)
    (src / "scripts").mkdir()
    (src / "scripts" / "run.sh").write_text(
        "source script\n", encoding="utf-8",
    )
    dest = tmp_path / "out" / "demo-skill"
    dest.parent.mkdir(parents=True)
    install_dir(src, dest, force_mode="copy")
    time.sleep(1.1)
    (dest / "SKILL.md").write_text("DEST EDIT\n", encoding="utf-8")
    (dest / "scripts" / "run.sh").write_text(
        "dest script edit\n", encoding="utf-8",
    )
    source_before = {
        relative_path: (src / relative_path).read_bytes()
        for relative_path in (
            Path("SKILL.md"),
            Path("scripts/run.sh"),
        )
    }
    original_replace = Path.replace
    staged_commit_count = 0

    def fail_second_staged_commit(path, destination):
        nonlocal staged_commit_count
        if (
            ".xskill-reverse-" in str(path)
            and "staged" in path.parts
            and str(destination).startswith(str(src))
        ):
            staged_commit_count += 1
            if staged_commit_count == 2:
                raise PermissionError("commit failure after first file")
        return original_replace(path, destination)

    monkeypatch.setattr(Path, "replace", fail_second_staged_commit)
    status = user_absorb.reverse_sync_copy_dest(
        dest, src, quiet_seconds=0,
    )

    assert status == user_absorb.ReverseSyncStatus.FAILED
    assert staged_commit_count == 2
    for relative_path, expected_content in source_before.items():
        assert (src / relative_path).read_bytes() == expected_content
    assert (dest / "SKILL.md").read_text(
        encoding="utf-8",
    ) == "DEST EDIT\n"


def test_reverse_sync_rollback_failure_keeps_backup_for_next_entry(
    tmp_path, monkeypatch,
):
    from xskill.agents import user_edit_absorb_agent as user_absorb

    src = _build_real_skill_repo(tmp_path)
    (src / "scripts").mkdir()
    (src / "scripts" / "run.sh").write_text(
        "source script\n", encoding="utf-8",
    )
    dest = tmp_path / "out" / "demo-skill"
    dest.parent.mkdir(parents=True)
    install_dir(src, dest, force_mode="copy")
    time.sleep(1.1)
    (dest / "SKILL.md").write_text("DEST EDIT\n", encoding="utf-8")
    (dest / "scripts" / "run.sh").write_text(
        "dest script edit\n", encoding="utf-8",
    )
    source_before = {
        relative_path: (src / relative_path).read_bytes()
        for relative_path in (Path("SKILL.md"), Path("scripts/run.sh"))
    }
    original_replace = Path.replace
    staged_commits = 0
    rollback_failed = False

    def fail_commit_and_first_rollback(path, destination):
        nonlocal staged_commits, rollback_failed
        if (
            ".xskill-reverse-" in str(path)
            and "staged" in path.parts
            and str(destination).startswith(str(src))
        ):
            staged_commits += 1
            if staged_commits == 2:
                raise PermissionError("commit failure")
        if (
            "rollback" in path.parts
            and str(destination).startswith(str(src))
            and not rollback_failed
        ):
            rollback_failed = True
            raise PermissionError("rollback failure")
        return original_replace(path, destination)

    monkeypatch.setattr(
        Path, "replace", fail_commit_and_first_rollback,
    )
    assert user_absorb.reverse_sync_copy_dest(
        dest, src, quiet_seconds=0,
    ) == user_absorb.ReverseSyncStatus.FAILED
    assert len(user_absorb._pending_reverse_manifests(src)) == 1

    monkeypatch.setattr(Path, "replace", original_replace)
    assert user_absorb.reverse_sync_copy_dest(
        dest, src, quiet_seconds=999_999,
    ) == user_absorb.ReverseSyncStatus.RECENT_EDIT
    assert user_absorb._pending_reverse_manifests(src) == ()
    for relative_path, expected_content in source_before.items():
        assert (src / relative_path).read_bytes() == expected_content


@pytest.mark.parametrize("crash_stage", ["target_to_backup", "stage_to_target"])
def test_reverse_sync_recovers_each_atomic_rename_crash(
    tmp_path, monkeypatch, crash_stage,
):
    from xskill.agents import user_edit_absorb_agent as user_absorb

    src = _build_real_skill_repo(tmp_path)
    dest = tmp_path / "out" / "demo-skill"
    dest.parent.mkdir(parents=True)
    install_dir(src, dest, force_mode="copy")
    source_before = (src / "SKILL.md").read_bytes()
    time.sleep(1.1)
    (dest / "SKILL.md").write_text("DEST EDIT\n", encoding="utf-8")
    original_replace = Path.replace
    crashed = False

    def crash_after_selected_rename(path, destination):
        nonlocal crashed
        is_target_backup = (
            str(path).startswith(str(src))
            and "rollback" in destination.parts
        )
        is_stage_target = (
            "staged" in path.parts
            and str(destination).startswith(str(src))
        )
        should_crash = (
            crash_stage == "target_to_backup" and is_target_backup
        ) or (
            crash_stage == "stage_to_target" and is_stage_target
        )
        result = original_replace(path, destination)
        if should_crash and not crashed:
            crashed = True
            raise SystemExit("simulated process crash")
        return result

    monkeypatch.setattr(Path, "replace", crash_after_selected_rename)
    with pytest.raises(SystemExit):
        user_absorb.reverse_sync_copy_dest(
            dest, src, quiet_seconds=0,
        )
    assert len(user_absorb._pending_reverse_manifests(src)) == 1

    monkeypatch.setattr(Path, "replace", original_replace)
    assert user_absorb.reverse_sync_copy_dest(
        dest, src, quiet_seconds=999_999,
    ) == user_absorb.ReverseSyncStatus.RECENT_EDIT
    assert user_absorb._pending_reverse_manifests(src) == ()
    assert (src / "SKILL.md").read_bytes() == source_before


def test_reverse_sync_cleanup_failure_is_failed_and_bounded(
    tmp_path, monkeypatch,
):
    from xskill.agents import user_edit_absorb_agent as user_absorb

    src = _build_real_skill_repo(tmp_path)
    dest = tmp_path / "out" / "demo-skill"
    dest.parent.mkdir(parents=True)
    install_dir(src, dest, force_mode="copy")
    time.sleep(1.1)
    (dest / "SKILL.md").write_text("DEST EDIT\n", encoding="utf-8")
    original_rmtree = user_absorb.shutil.rmtree
    block_cleanup = True

    def fail_transaction_cleanup(path, *args, **kwargs):
        if block_cleanup and str(path).endswith(".data"):
            raise PermissionError("transaction cleanup blocked")
        return original_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(
        user_absorb.shutil, "rmtree", fail_transaction_cleanup,
    )
    assert user_absorb.reverse_sync_copy_dest(
        dest, src, quiet_seconds=0,
    ) == user_absorb.ReverseSyncStatus.FAILED
    assert len(user_absorb._pending_reverse_manifests(src)) == 1

    # 未恢复前入口不能再创建第二个事务。
    assert user_absorb.reverse_sync_copy_dest(
        dest, src, quiet_seconds=0,
    ) == user_absorb.ReverseSyncStatus.FAILED
    assert len(user_absorb._pending_reverse_manifests(src)) == 1

    block_cleanup = False
    assert user_absorb.reverse_sync_copy_dest(
        dest, src, quiet_seconds=0,
    ) == user_absorb.ReverseSyncStatus.NO_EDIT
    assert user_absorb._pending_reverse_manifests(src) == ()


def test_reverse_sync_large_tree_stages_only_changed_files(
    tmp_path, monkeypatch,
):
    from xskill.agents import user_edit_absorb_agent as user_absorb

    src = _build_real_skill_repo(tmp_path)
    bulk_dir = src / "references"
    bulk_dir.mkdir()
    for index in range(200):
        (bulk_dir / f"note-{index:03d}.md").write_text(
            f"unchanged {index}\n", encoding="utf-8",
        )
    dest = tmp_path / "out" / "demo-skill"
    dest.parent.mkdir(parents=True)
    install_dir(src, dest, force_mode="copy")
    time.sleep(1.1)
    changed_file = dest / "references" / "note-137.md"
    changed_file.write_text("one changed file\n", encoding="utf-8")
    original_stage_copy = user_absorb._copy_verified_file_to_stage
    staged_paths: list[Path] = []

    def count_staged_copy(dest_root, file_info, staged_path):
        staged_paths.append(file_info.relative_path)
        return original_stage_copy(dest_root, file_info, staged_path)

    monkeypatch.setattr(
        user_absorb,
        "_copy_verified_file_to_stage",
        count_staged_copy,
    )

    assert user_absorb.reverse_sync_copy_dest(
        dest, src, quiet_seconds=0,
    ) == user_absorb.ReverseSyncStatus.SYNCED
    assert staged_paths == [Path("references/note-137.md")]
    assert (src / staged_paths[0]).read_text(
        encoding="utf-8",
    ) == "one changed file\n"


def test_reverse_sync_oversized_9000_entry_manifest_never_renames_source(
    tmp_path, monkeypatch,
):
    from xskill.agents import user_edit_absorb_agent as user_absorb

    src = tmp_path / "source"
    src.mkdir()
    source_file = src / "SKILL.md"
    source_file.write_text("source remains\n", encoding="utf-8")
    manifest_path, data_dir, manifest = (
        user_absorb._create_reverse_transaction(src)
    )
    manifest["state"] = "prepared"
    manifest["files"] = [
        {
            "path": (
                f"references/entry-{index:04d}-"
                f"{'long-name-' * 10}.md"
            ),
            "original_signature": None,
            "staged_signature": [1, 2, 32768, 4, 5],
        }
        for index in range(9000)
    ]
    original_replace = Path.replace
    source_renames: list[tuple[Path, Path]] = []

    def record_source_rename(path, destination):
        if path == source_file or destination == source_file:
            source_renames.append((path, destination))
        return original_replace(path, destination)

    monkeypatch.setattr(Path, "replace", record_source_rename)

    assert user_absorb._commit_reverse_transaction(
        src, manifest_path, data_dir, manifest,
    ) is False
    assert source_renames == []
    assert source_file.read_text(encoding="utf-8") == "source remains\n"
    assert manifest_path.exists()
    assert user_absorb._recover_pending_reverse_transaction(src) is True
    assert not manifest_path.exists()
    assert not data_dir.exists()


# ──────────────────────────────────────────────────────────────────
# T4: link 模式 install + 重装 → 不触发 reverse_sync
# ──────────────────────────────────────────────────────────────────


@pytest.mark.skipif(sys.platform == "win32",
                    reason="symlink 在 Windows 需要 DevMode")
def test_link_mode_skips_reverse_sync(tmp_path):
    """link 模式：dest = source，无需回流；meta.mode 是 'symlink' 不触发钩子。"""
    src = _make_skill_src(tmp_path / "srcs")
    dest = tmp_path / "out" / "test-skill"
    dest.parent.mkdir(parents=True)

    mode = install_dir(src, dest)
    assert mode == "symlink"
    meta = _install_meta_path(dest)
    assert json.loads(meta.read_text())["mode"] == "symlink"

    # 直接调钩子——link 模式应该 no-op（不会去执行 reverse_sync）
    # 这里没法断言"没调过 reverse_sync_copy_dest"，但通过反向证据：
    # auto_reset=True 重装一遍，meta 还在；symlink 本质上 dest=source 同步。
    install_dir(src, dest, auto_reset=True)
    meta_after = _install_meta_path(dest)
    assert meta_after.is_file()
    # 新 meta 仍是 symlink（auto_reset 重装走 symlink）
    assert json.loads(meta_after.read_text())["mode"] == "symlink"


# ──────────────────────────────────────────────────────────────────
# T5: install_dir(force_mode="copy") 强制 copy
# ──────────────────────────────────────────────────────────────────


def test_force_mode_copy_skips_symlink_junction(tmp_path, monkeypatch):
    """``force_mode="copy"`` 不应该试 symlink 或 junction—— 直接走 copytree。"""
    src = _make_skill_src(tmp_path / "srcs")
    dest = tmp_path / "out" / "test-skill"
    dest.parent.mkdir(parents=True)

    calls = {"symlink": 0, "junction": 0}
    orig_sym = fb._try_symlink
    orig_jct = fb._try_junction

    def _wrap_sym(s, d):
        calls["symlink"] += 1
        return orig_sym(s, d)

    def _wrap_jct(s, d):
        calls["junction"] += 1
        return orig_jct(s, d)

    monkeypatch.setattr(fb, "_try_symlink", _wrap_sym)
    monkeypatch.setattr(fb, "_try_junction", _wrap_jct)

    mode = install_dir(src, dest, force_mode="copy")
    assert mode == "copy"
    assert calls["symlink"] == 0
    assert calls["junction"] == 0
    assert dest.is_dir()
    assert not dest.is_symlink()
    assert (dest / "SKILL.md").is_file()


# ──────────────────────────────────────────────────────────────────
# T6: _reset_dest 干净清理 link/junction/dir/file（issue #35 回归锚点）
# ──────────────────────────────────────────────────────────────────


@pytest.mark.skipif(sys.platform == "win32",
                    reason="symlink 需要 DevMode；junction 用 mock 测")
def test_reset_dest_unlinks_symlink_without_rmtree(tmp_path):
    """symlink-to-dir 走 ``unlink``，不递归动 target——避免误删 source。"""
    src = tmp_path / "src"
    src.mkdir()
    (src / "keepme.txt").write_text("source content")
    link = tmp_path / "link"
    link.symlink_to(src, target_is_directory=True)

    _reset_dest(link)
    assert not link.exists() and not _is_link_or_junction(link)
    # target 没被动
    assert (src / "keepme.txt").read_text() == "source content"


def test_reset_dest_rmtrees_real_dir(tmp_path):
    """真目录走 ``shutil.rmtree``。"""
    d = tmp_path / "real_dir"
    d.mkdir()
    (d / "f.txt").write_text("x")

    _reset_dest(d)
    assert not d.exists()


def test_reset_dest_deletes_file(tmp_path):
    """普通文件走 ``unlink``。"""
    f = tmp_path / "f.txt"
    f.write_text("x")

    _reset_dest(f)
    assert not f.exists()


def test_reset_dest_handles_nonexistent(tmp_path):
    """nonexistent dest no-op。"""
    _reset_dest(tmp_path / "ghost")
    # 没抛错就过


def test_reset_dest_cleans_old_meta(tmp_path):
    """``_reset_dest`` 顺手清掉旁边的 meta 文件——避免下一次 install 读到错的 meta。"""
    dest = tmp_path / "out" / "skill-x"
    dest.parent.mkdir(parents=True)
    dest.mkdir()
    meta = _install_meta_path(dest)
    meta.write_text(json.dumps({"mode": "copy", "source": "x", "installed_at": 0}))

    _reset_dest(dest)
    assert not dest.exists()
    assert not meta.exists()


# ──────────────────────────────────────────────────────────────────
# T7: install_dir(auto_reset=True) 安全覆盖（issue #35 端到端回归）
# ──────────────────────────────────────────────────────────────────


@pytest.mark.skipif(sys.platform == "win32",
                    reason="symlink 需要 DevMode；下面用 symlink 模拟 dest=link")
def test_auto_reset_overwrites_existing_link_safely(tmp_path):
    """auto_reset=True 时：dest 是 link（模拟 codex/opencode 装过的 junction），
    再装 openclaw 不应抛 OSError——而是 unlink 旧 link 后 copytree 新内容。
    """
    src_codex = tmp_path / "srcs" / "codex-version"
    src_codex.mkdir(parents=True)
    (src_codex / "SKILL.md").write_text("codex version", encoding="utf-8")

    src_openclaw = _make_skill_src(tmp_path / "srcs2", name="oc-version")
    dest = tmp_path / "out" / "shared-skill"
    dest.parent.mkdir(parents=True)
    # 先模拟 codex 装了一个 symlink
    dest.symlink_to(src_codex, target_is_directory=True)
    assert _is_link_or_junction(dest)

    # openclaw 现在跑——用 auto_reset=True + force_mode='copy'：应清掉 link 再 copy
    install_dir(src_openclaw, dest, force_mode="copy", auto_reset=True)
    assert not _is_link_or_junction(dest)
    assert dest.is_dir()
    assert (dest / "SKILL.md").read_text(encoding="utf-8").startswith("---")


def test_auto_reset_handles_real_dir_overwrites(tmp_path):
    """auto_reset=True 时 dest 是真目录（前一次 copy 装的）→ rmtree 再装新。"""
    src1 = _make_skill_src(tmp_path / "src1", name="v1")
    src2 = _make_skill_src(tmp_path / "src2", name="v1")
    (src2 / "SKILL.md").write_text("version 2", encoding="utf-8")
    dest = tmp_path / "out" / "v1"
    dest.parent.mkdir(parents=True)

    install_dir(src1, dest, force_mode="copy")
    assert "version 2" not in (dest / "SKILL.md").read_text()

    install_dir(src2, dest, force_mode="copy", auto_reset=True)
    assert (dest / "SKILL.md").read_text() == "version 2"
