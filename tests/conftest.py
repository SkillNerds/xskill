"""pytest 入口 + 全局防御。

历史背景
========

2026-05-13: watcher 的 ``_install_skill_to_cc`` 落到 ``~/.claude/skills/``
时只取 ``server._home_root()``，而 server 模块未启动时该函数 fallback 到
``Path.home()`` —— 测试用 ``tmp_path`` 跑完后这些 symlink 全变 broken，
导致用户真实 ``claude`` 命令启动扫 skill 卡住。

本 conftest 提供三道防御：

1. 把 ``src/`` 加进 ``sys.path``
2. **autouse function-scope fixture**：每个测试前后都断言用户真实
   ``~/.claude/skills/`` 没新增任何指向 ``/tmp/pytest-*`` 的子项。一旦
   被污染（不论哪个测试写的）当场 fail 并清理污染条目。
3. **install_to_claude_code 守卫**（autouse monkeypatch）：拦截
   ``target_root`` 解析后等于真 ``Path.home()`` 或 ``None`` 的调用，直接
   raise。生产 daemon 显式调用不走 pytest 入口，不受影响。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


# ─────────────────────────────────────────────────────────────────
# 真实 ~/.xskill/registry.db 防污染
# ─────────────────────────────────────────────────────────────────
# 管线/埋点(instrumentation)里部分 record_*(db_path=None) 会落到全局 registry。
# 测试期间统一把 get_registry_db_path 重定向到 tmp：① 不污染真库；② 不与线上
# serve 抢 WAL 锁(否则时序敏感测试会偶发变慢/闪红)。显式传 db_path 的测试不受影响。
@pytest.fixture(autouse=True)
def _isolate_registry_db(tmp_path_factory, monkeypatch):
    db = tmp_path_factory.mktemp("xskill_reg") / "registry.db"
    monkeypatch.setattr("xskill.pipeline.registry.get_registry_db_path", lambda: db)


# ─────────────────────────────────────────────────────────────────
# 真实 ~/.claude/skills/ 防污染快照
# ─────────────────────────────────────────────────────────────────

REAL_HOME = Path(os.path.expanduser("~"))
REAL_SKILLS_DIR = REAL_HOME / ".claude" / "skills"


def _snapshot_skills_dir() -> set[str]:
    """返回当前 ~/.claude/skills/ 的子项名集合。"""
    if not REAL_SKILLS_DIR.is_dir():
        return set()
    try:
        return {p.name for p in REAL_SKILLS_DIR.iterdir()}
    except OSError:
        return set()


def _find_tmp_polluted_entries() -> list[Path]:
    """列出 ~/.claude/skills/ 下指向 /tmp/ 或 pytest-* 的 symlink，
    以及任何 broken symlink。"""
    if not REAL_SKILLS_DIR.is_dir():
        return []
    out: list[Path] = []
    try:
        for p in REAL_SKILLS_DIR.iterdir():
            if p.is_symlink():
                try:
                    target = os.readlink(p)
                except OSError:
                    continue
                target_str = str(target)
                if "/tmp/" in target_str or "pytest-" in target_str:
                    out.append(p)
                elif not p.exists():
                    out.append(p)  # broken symlink
    except OSError:
        pass
    return out


@pytest.fixture(autouse=True)
def _block_real_home_pollution(request):
    """每个测试前后扫描真实 ~/.claude/skills/，确保没被污染。

    - **setUp**：扫描 + 清理（避免上个测试遗留干扰本测试 baseline）
    - **tearDown**：再扫，新增指向 tmp 的 symlink → fail 测试并清理
    """
    baseline = _snapshot_skills_dir()
    for p in _find_tmp_polluted_entries():
        try:
            p.unlink()
        except OSError:
            pass

    yield

    post_polluted = _find_tmp_polluted_entries()
    new_polluted = [p for p in post_polluted if p.name not in baseline]
    # 不管是不是新加的，只要指向 tmp 都清掉（防止 cascade）
    for p in post_polluted:
        try:
            p.unlink()
        except OSError:
            pass
    if new_polluted:
        names = ", ".join(p.name for p in new_polluted)
        pytest.fail(
            f"测试污染了真实 ~/.claude/skills/：新增指向 /tmp 的 symlink "
            f"{names}（test: {request.node.nodeid}）。说明该测试调 "
            f"install_to_claude_code / DirectoryWatcher 没显式传 "
            f"target_root / home_root=tmp_path。已自动清理。"
        )


# ─────────────────────────────────────────────────────────────────
# install_to_claude_code 守卫
# ─────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _guard_install_to_claude_code(monkeypatch):
    """拦截任何 ``install_to_claude_code`` 调用，如果解析后的 root 等于真
    ``Path.home()`` 或 target_root=None，直接 raise（fail-loud，符合
    CLAUDE.md "no fallback" 原则）。

    生产 daemon 通过 ``xskill.api.app._home_root()`` 取 root，daemon 不走
    pytest 入口，所以这层守卫只影响测试。
    """
    import xskill.ecosystems as _eco

    real_install = _eco.install_to_claude_code
    real_install_all = _eco.install_all_to_claude_code
    real_home = Path(os.path.expanduser("~")).resolve()

    def _guarded_install(skill_path, *, target_root=None, side="main", **kw):
        if target_root is None:
            raise RuntimeError(
                "[conftest guard] install_to_claude_code 被调时 target_root=None；"
                "测试必须显式传 target_root=tmp_path 防污染用户真 ~/.claude/。"
            )
        resolved = Path(target_root).expanduser().resolve()
        if resolved == real_home:
            raise RuntimeError(
                f"[conftest guard] install_to_claude_code target_root 解析为真 "
                f"Path.home() = {real_home}；测试必须传 tmp_path 之类的隔离目录。"
            )
        return real_install(skill_path, target_root=target_root, side=side, **kw)

    def _guarded_install_all(skill_dir, *, target_root=None, names=None, **kw):
        if target_root is None:
            raise RuntimeError(
                "[conftest guard] install_all_to_claude_code 被调时 target_root=None；"
                "测试必须显式传 target_root=tmp_path。"
            )
        resolved = Path(target_root).expanduser().resolve()
        if resolved == real_home:
            raise RuntimeError(
                f"[conftest guard] install_all_to_claude_code target_root 解析为真 "
                f"Path.home() = {real_home}；测试必须传隔离目录。"
            )
        return real_install_all(skill_dir, target_root=target_root, names=names, **kw)

    monkeypatch.setattr(_eco, "install_to_claude_code", _guarded_install)
    monkeypatch.setattr(_eco, "install_all_to_claude_code", _guarded_install_all)
    # watcher.py 是 ``from xskill.ecosystems import install_to_claude_code``
    # 但在函数内 lazy import，所以只 patch 模块属性就够了——下次 from import
    # 拿到的是 patched 版本。
