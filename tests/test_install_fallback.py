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

import logging
import sys
from pathlib import Path

import pytest

from xskill import install_fallback
from xskill.install_fallback import install_dir


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
    from xskill.frontmatter import serialize as fm_serialize

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
