"""孤儿 dest 自愈回归：迁移保留 + legacy meta 宽松读 + git 基线领养。

生产事故根因（v0.6.29a3 Windows 客户端 DAMAGED 风暴）：
1. 迁移把导入失败的 sidecar 也删了 → dest 变孤儿（无账本行无旁证）。
2. revsync 兼容路径拿 canary 比对 meta 过严格校验 → 必报
   INSTALL_METADATA_DAMAGED → revsync FAILED → 冻结。
3. 孤儿没有基线，三方回流无从谈起。

本文件覆盖三处修复：迁移只删导入成功的文件；老 meta 宽松取 installed_at；
按 source_sha 从 git tree 重建安装基线并登记账本。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from dulwich import porcelain

from xskill.agents.user_edit_absorb_agent import (
    ReverseSyncStatus,
    _read_install_meta_ts,
    reverse_sync_copy_dest,
)
from xskill.ecosystems.install_ledger import InstallLedger
from xskill.ecosystems.installation import (
    COPY_INSTALL_MARKER_NAME,
    adopt_orphan_copy_install,
    read_copy_install_baseline,
    read_install_metadata,
    write_install_metadata,
)

_LEGACY_META_NAME = ".xskill-install-meta.json"
_REVSYNC_EXCLUDE = frozenset({".git", _LEGACY_META_NAME})


def _git_source(tmp_path: Path, files: dict[str, str]) -> tuple[Path, str]:
    src = tmp_path / "src" / "demo"
    src.mkdir(parents=True)
    for rel, text in files.items():
        target = src / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    repo = porcelain.init(str(src), bare=False)
    try:
        porcelain.add(repo)
        sha = porcelain.commit(
            repo,
            message=b"install snapshot",
            author=b"t <t@example.com>",
            committer=b"t <t@example.com>",
        )
    finally:
        repo.close()
    return src, sha.decode("ascii")


def _orphan_dest(tmp_path: Path, files: dict[str, str], sha: str) -> Path:
    dest = tmp_path / "eco" / "demo"
    dest.mkdir(parents=True)
    for rel, text in files.items():
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    legacy = {
        "source_sha": sha,
        "side": "main",
        "installed_at": time.time() - 100,
        "ecosystem": "openclaw",
    }
    (dest / _LEGACY_META_NAME).write_text(
        json.dumps(legacy), encoding="utf-8",
    )
    return dest


def test_migration_keeps_failed_sidecar_for_retry(tmp_path):
    """导入失败的 sidecar 必须保留等下轮重试，不再无脑删成孤儿。"""
    root = tmp_path / "skills"
    (root / "demo").mkdir(parents=True)
    sidecar = root / ".xskill-install-meta-demo.json"
    sidecar.write_text('{"mode":"copy"}', encoding="utf-8")
    ledger = InstallLedger(tmp_path / "installations.sqlite")

    stats = ledger.migrate_from_sidecars([root])
    assert stats["installs_imported"] == 0
    assert stats["files_removed"] == 0
    assert stats["errors"] == 1
    assert sidecar.exists()
    assert ledger.read_install(root / "demo") is None

    src = tmp_path / "src" / "demo"
    src.mkdir(parents=True)
    (src / "SKILL.md").write_text("x", encoding="utf-8")
    valid = {
        "mode": "copy",
        "source": str(src.resolve()),
        "source_sha": "",
        "installed_at": time.time(),
        "installation_id": "a" * 32,
        "content_identity": "b" * 64,
        "baseline_identity": "c" * 64,
        "file_fingerprints": {"SKILL.md": "d" * 64},
    }
    sidecar.write_text(json.dumps(valid), encoding="utf-8")
    stats = ledger.migrate_from_sidecars([root])
    assert stats["installs_imported"] == 1
    assert stats["files_removed"] == 1
    assert not sidecar.exists()
    assert ledger.read_install(root / "demo") is not None


def test_legacy_canary_meta_read_leniently(tmp_path):
    """canary 比对 meta（无 mode 等安装账字段）宽松取 installed_at。"""
    dest = tmp_path / "eco" / "demo"
    dest.mkdir(parents=True)
    ts = time.time() - 50
    (dest / _LEGACY_META_NAME).write_text(
        json.dumps({
            "source_sha": "e" * 40,
            "side": "main",
            "installed_at": ts,
            "ecosystem": "openclaw",
        }),
        encoding="utf-8",
    )
    ok, installed_at = _read_install_meta_ts(dest)
    assert ok is True
    assert installed_at == ts

    (dest / _LEGACY_META_NAME).write_text("not json{", encoding="utf-8")
    ok, installed_at = _read_install_meta_ts(dest)
    assert ok is True
    assert installed_at is None


def test_adopt_orphan_rebuilds_baseline_from_git(tmp_path):
    src, sha = _git_source(tmp_path, {"SKILL.md": "v1\n", "notes/a.txt": "a\n"})
    dest = _orphan_dest(tmp_path, {"SKILL.md": "v1\n", "notes/a.txt": "a\n"}, sha)

    adopted = adopt_orphan_copy_install(
        dest, src, legacy_meta_path=dest / _LEGACY_META_NAME,
    )
    assert adopted is True
    meta = read_install_metadata(dest)
    assert meta is not None
    assert meta["mode"] == "copy"
    assert meta["source_sha"] == sha
    baseline = read_copy_install_baseline(dest)
    assert set(baseline) == {"SKILL.md", "notes/a.txt"}
    assert not (dest / COPY_INSTALL_MARKER_NAME).exists()


def test_adopt_orphan_skipped_when_ledger_row_exists(tmp_path):
    src, sha = _git_source(tmp_path, {"SKILL.md": "v1\n"})
    dest = _orphan_dest(tmp_path, {"SKILL.md": "v1\n"}, sha)
    write_install_metadata(dest, src, "copy")
    before = read_install_metadata(dest)
    assert before is not None

    adopted = adopt_orphan_copy_install(
        dest, src, legacy_meta_path=dest / _LEGACY_META_NAME,
    )
    assert adopted is False
    after = read_install_metadata(dest)
    assert after is not None
    assert after["installation_id"] == before["installation_id"]


def test_adopt_orphan_refused_without_source_sha(tmp_path):
    src, _ = _git_source(tmp_path, {"SKILL.md": "v1\n"})
    dest = _orphan_dest(tmp_path, {"SKILL.md": "v1\n"}, "not-a-sha")
    adopted = adopt_orphan_copy_install(
        dest, src, legacy_meta_path=dest / _LEGACY_META_NAME,
    )
    assert adopted is False
    assert read_install_metadata(dest) is None


def test_revsync_orphan_backports_user_edit(tmp_path):
    """端到端：孤儿 dest 的用户编辑经领养基线三方比较后灌回 source。"""
    src, sha = _git_source(tmp_path, {"SKILL.md": "v1\n"})
    dest = _orphan_dest(tmp_path, {"SKILL.md": "v1\n"}, sha)
    (dest / "SKILL.md").write_text("v2-user-edit\n", encoding="utf-8")

    status = reverse_sync_copy_dest(
        dest, src,
        exclude=_REVSYNC_EXCLUDE,
        quiet_seconds=0,
    )
    assert status == ReverseSyncStatus.SYNCED
    assert (src / "SKILL.md").read_text(encoding="utf-8") == "v2-user-edit\n"
    assert read_install_metadata(dest) is not None


def test_git_tree_fingerprints_prefer_worktree_when_head_matches(tmp_path):
    """HEAD==install sha 时基线跟 worktree（模拟 Windows CRLF 工作区）。"""
    import hashlib

    from xskill.ecosystems.installation import _git_tree_fingerprints

    src, sha = _git_source(tmp_path, {"SKILL.md": "v1\n"})
    (src / "SKILL.md").write_bytes(b"v1\r\n")
    fingerprints = _git_tree_fingerprints(src, sha)
    assert fingerprints is not None
    assert fingerprints["SKILL.md"] == hashlib.sha256(b"v1\r\n").hexdigest()


def test_revsync_orphan_backports_when_worktree_has_crlf(tmp_path):
    """源仓 worktree 为 CRLF、与 blob LF 不一致时，孤儿编辑仍应回流。"""
    src, sha = _git_source(tmp_path, {"SKILL.md": "v1\n"})
    (src / "SKILL.md").write_bytes(b"v1\r\n")
    dest = _orphan_dest(tmp_path, {"SKILL.md": "v1\r\n"}, sha)
    (dest / "SKILL.md").write_bytes(b"v2-user-edit\r\n")

    status = reverse_sync_copy_dest(
        dest, src,
        exclude=_REVSYNC_EXCLUDE,
        quiet_seconds=0,
    )
    assert status == ReverseSyncStatus.SYNCED
    assert (src / "SKILL.md").read_bytes() == b"v2-user-edit\r\n"


def test_revsync_orphan_backports_crlf_edit_after_source_head_advances(
    tmp_path,
):
    """HEAD 前进后，blob LF 基线不应把 worktree CRLF 误判为源侧修改。"""
    src, install_sha = _git_source(tmp_path, {"SKILL.md": "v1\n"})
    (src / "notes.md").write_text("source-only\n", encoding="utf-8")
    repo = porcelain.open_repo(str(src))
    try:
        porcelain.add(repo, paths=["notes.md"])
        porcelain.commit(
            repo,
            message=b"advance source head",
            author=b"t <t@example.com>",
            committer=b"t <t@example.com>",
        )
    finally:
        repo.close()
    (src / "SKILL.md").write_bytes(b"v1\r\n")
    dest = _orphan_dest(
        tmp_path, {"SKILL.md": "v1\r\n"}, install_sha,
    )
    (dest / "SKILL.md").write_bytes(b"v2-user-edit\r\n")

    status = reverse_sync_copy_dest(
        dest, src,
        exclude=_REVSYNC_EXCLUDE,
        quiet_seconds=0,
    )

    assert status == ReverseSyncStatus.SYNCED
    assert (src / "SKILL.md").read_bytes() == b"v2-user-edit\r\n"


def test_revsync_orphan_unedited_is_no_edit(tmp_path):
    src, sha = _git_source(tmp_path, {"SKILL.md": "v1\n"})
    dest = _orphan_dest(tmp_path, {"SKILL.md": "v1\n"}, sha)

    status = reverse_sync_copy_dest(
        dest, src,
        exclude=_REVSYNC_EXCLUDE,
        quiet_seconds=0,
    )
    assert status == ReverseSyncStatus.NO_EDIT
    assert (src / "SKILL.md").read_text(encoding="utf-8") == "v1\n"
