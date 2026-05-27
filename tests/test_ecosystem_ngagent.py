"""
test_ecosystem_ngagent.py -- ngagent (opencode 企业分支) 适配器单测
======================================================================

ngagent 与 opencode 共用 SQLite schema，所以 SqliteIngester 主体逻辑
（cursor / part 渲染 / WAL 安全）在 ``test_opencode_adapter.py`` 已覆盖；
本文件只验证 ngagent 与 opencode 的**差异点**：

* T1. ``_ngagent_db_path`` 解析到 ``<home>/.local/share/opencode/db/ngagent.db``
* T2. ``_ngagent_skills_path`` 解析到 ``<home>/.config/opencode/skills``
* T3. ``detect_known_ecosystems`` 看到 ngagent.db 时返回 ngagent 记录
* T4. ``install_to_ngagent`` 写到 ``<root>/.config/opencode/skills/<name>/SKILL.md``
* T5. ``install_to_ngagent`` 二次跑 idempotent
* T6. ``SqliteIngester(spec=NGAGENT_SPEC)`` 跑通：traj_id 用 ``traj_ng_`` 前缀
       + bridge dir 出现 traj_md
* T7. opencode 与 ngagent 并存：两个 DB 同时存在 → detect 返回两条记录
"""
from __future__ import annotations

from pathlib import Path

import pytest

from xskill.ecosystems import (
    NGAGENT_SPEC,
    SqliteIngester,
    detect_known_ecosystems,
    install_to_ngagent,
    _ngagent_db_path,
    _ngagent_skills_path,
)


# 入仓的 opencode sample.db —— schema 一致，ngagent 直接复用做 fixture
FIXTURE_DB = Path(__file__).parent / "fixtures" / "opencode" / "sample.db"


# ──────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────


def _seed_ngagent_home(home: Path) -> Path:
    """把 fixture sample.db 复制到 ngagent 路径
    （``<home>/.local/share/opencode/db/ngagent.db``）。

    schema 与 opencode 完全一致，所以同一份 fixture 既能给 opencode
    测试也能给 ngagent 测试用——不另维护一份 ngagent fixture。
    """
    target = home / ".local" / "share" / "opencode" / "db"
    target.mkdir(parents=True, exist_ok=True)
    db_path = target / "ngagent.db"
    db_path.write_bytes(FIXTURE_DB.read_bytes())
    return db_path


def _seed_opencode_home(home: Path) -> Path:
    """把 fixture sample.db 复制到 opencode 默认路径——给"并存"测试用。"""
    target = home / ".local" / "share" / "opencode"
    target.mkdir(parents=True, exist_ok=True)
    db_path = target / "opencode.db"
    db_path.write_bytes(FIXTURE_DB.read_bytes())
    return db_path


def _make_skill(skill_root: Path, name: str = "ng-test-skill") -> Path:
    skill_path = skill_root / name
    skill_path.mkdir(parents=True, exist_ok=True)
    (skill_path / "SKILL.md").write_text(
        "---\nname: ng-test-skill\ndescription: unit test for ngagent\n---\n\n# Body\n",
        encoding="utf-8",
    )
    return skill_path


# ──────────────────────────────────────────────────────────────────
# T1 / T2: path helpers
# ──────────────────────────────────────────────────────────────────


def test_ngagent_db_path_resolves_correctly():
    """``_ngagent_db_path`` 在任何平台返回
    ``<home>/.local/share/opencode/db/ngagent.db``——注意是 ``db/`` 子目录。
    """
    home = Path("/some/home")
    expected = home / ".local" / "share" / "opencode" / "db" / "ngagent.db"
    assert _ngagent_db_path(home) == expected


def test_ngagent_skills_path_resolves_correctly():
    """``_ngagent_skills_path`` 返回 ``<home>/.config/opencode/skills``——
    XDG_CONFIG_HOME 默认值下的 opencode/skills。
    """
    home = Path("/u")
    assert _ngagent_skills_path(home) == home / ".config" / "opencode" / "skills"


# ──────────────────────────────────────────────────────────────────
# T3: detect_known_ecosystems sees ngagent
# ──────────────────────────────────────────────────────────────────


def test_detect_known_ecosystems_includes_ngagent(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    _seed_ngagent_home(home)

    found = detect_known_ecosystems(home_root=home)
    ecos = {r["ecosystem"] for r in found}
    assert "ngagent" in ecos

    rec = next(r for r in found if r["ecosystem"] == "ngagent")
    expected_source = (home / ".local" / "share" / "opencode" / "db" / "ngagent.db").resolve()
    expected_bridge = (home / ".xskill" / "ngagent_sessions").resolve()
    assert rec["source"] == expected_source
    assert rec["bridge"] == expected_bridge


def test_detect_skips_ngagent_when_db_absent(tmp_path):
    """ngagent.db 不存在时 detect 不应返回 ngagent 条目。"""
    found = detect_known_ecosystems(home_root=tmp_path)
    ecos = {r["ecosystem"] for r in found}
    assert "ngagent" not in ecos


# ──────────────────────────────────────────────────────────────────
# T4 / T5: install_to_ngagent writes to ~/.config/opencode/skills/
# ──────────────────────────────────────────────────────────────────


def test_install_to_ngagent_writes_to_xdg_config_path(tmp_path):
    """install_to_ngagent 必须装到 ``<root>/.config/opencode/skills/<name>/``——
    不是 opencode 用的 ``.agents/skills`` 也不是 CC 用的 ``.claude/skills``。
    """
    home = tmp_path / "home"
    home.mkdir()
    skill_root = tmp_path / "skills"
    skill_path = _make_skill(skill_root, name="ng-test-skill")

    dest_md = install_to_ngagent(skill_path, target_root=home)

    expected = home / ".config" / "opencode" / "skills" / "ng-test-skill" / "SKILL.md"
    assert dest_md == expected
    assert dest_md.is_file()
    assert "name: ng-test-skill" in dest_md.read_text(encoding="utf-8")

    # 关键回归锚点：不应该污染 ``~/.agents/skills``（那是 opencode 的目录）
    assert not (home / ".agents" / "skills" / "ng-test-skill").exists()


def test_install_to_ngagent_idempotent(tmp_path):
    """同样的 skill 装两次不抛错；第二次 no-op。"""
    home = tmp_path / "home"
    home.mkdir()
    skill_root = tmp_path / "skills"
    skill_path = _make_skill(skill_root, name="ng-test-skill")

    first = install_to_ngagent(skill_path, target_root=home)
    second = install_to_ngagent(skill_path, target_root=home)

    assert first == second
    assert first.is_file()


# ──────────────────────────────────────────────────────────────────
# T4b. issue #34 回归：dest 必须是真目录（不是 link/junction）
# ──────────────────────────────────────────────────────────────────


def test_install_to_ngagent_dest_is_real_dir_not_link(tmp_path):
    """issue #34 回归锚点：ngagent install 必须把 dest 装成真目录，
    不能是 symlink 或 junction——否则 Node.js Dirent.isDirectory()
    返回 false，ngagent skill discovery 看不到 skill。
    """
    from xskill.ecosystems._fallback import _is_link_or_junction

    home = tmp_path / "home"
    home.mkdir()
    skill_root = tmp_path / "skills"
    skill_path = _make_skill(skill_root, name="ng-test-skill")

    install_to_ngagent(skill_path, target_root=home)

    dest = home / ".config" / "opencode" / "skills" / "ng-test-skill"
    assert dest.is_dir()
    # 关键：不是 link/junction（issue #34 的核心约束）
    assert not _is_link_or_junction(dest)


def test_install_to_ngagent_user_edit_round_trips(tmp_path, monkeypatch):
    """issue #34 完整链路验收：copy 模式装好 → 用户改 dest → 重装前
    ``reverse_sync_copy_dest`` 把改灌回 source 仓。
    """
    import subprocess
    import time as _t

    home = tmp_path / "home"
    home.mkdir()
    skill_root = tmp_path / "skills"
    # ngagent reverse_sync 要 source 是 git 仓（抢 skill_repo_lock）
    sk = skill_root / "ng-test-skill"
    sk.mkdir(parents=True)
    (sk / "SKILL.md").write_text(
        "---\nname: ng-test-skill\ndescription: original.\n---\n# original body\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "-q", str(sk)], check=True)
    subprocess.run(["git", "-C", str(sk), "config", "user.email", "x@y.z"], check=True)
    subprocess.run(["git", "-C", str(sk), "config", "user.name", "x"], check=True)
    subprocess.run(["git", "-C", str(sk), "add", "."], check=True)
    subprocess.run(["git", "-C", str(sk), "commit", "-q", "-m", "init"], check=True)

    install_to_ngagent(sk, target_root=home)
    dest = home / ".config" / "opencode" / "skills" / "ng-test-skill"

    # 让 reverse_sync 跳 quiet 检查（测试免等 3 分钟）
    from xskill.agents import user_edit_absorb_agent as ua
    _real = ua.reverse_sync_copy_dest

    def _force_quiet_zero(d, s, **kw):
        kw["quiet_seconds"] = 0
        return _real(d, s, **kw)

    monkeypatch.setattr(ua, "reverse_sync_copy_dest", _force_quiet_zero)

    # 用户改 dest（mtime 比 installed_at 至少大 1 秒）
    _t.sleep(1.1)
    (dest / "SKILL.md").write_text(
        "---\nname: ng-test-skill\ndescription: USER EDIT.\n---\n# user body\n",
        encoding="utf-8",
    )

    # 重装：reverse_sync 应该把改灌回 source
    install_to_ngagent(sk, target_root=home)
    assert "USER EDIT" in (sk / "SKILL.md").read_text(encoding="utf-8")


# ──────────────────────────────────────────────────────────────────
# T6: SqliteIngester with NGAGENT_SPEC bridges sessions correctly
# ──────────────────────────────────────────────────────────────────


def test_sqlite_ingester_with_ngagent_spec(tmp_path):
    """SqliteIngester 用 NGAGENT_SPEC 时：
    * 读 ngagent.db（不是 opencode.db）
    * traj_id 用 ``traj_ng_`` 前缀（不是 ``traj_oc_``）
    * bridge dir 出现 traj_md
    """
    home = tmp_path / "home"
    home.mkdir()
    _seed_ngagent_home(home)

    bridge = tmp_path / "bridge"
    ing = SqliteIngester(bridge, home_root=home, spec=NGAGENT_SPEC)
    results = ing.run_once()

    assert len(results) == 1
    rec = results[0]
    # spec 派生的 traj_id 前缀
    assert rec["traj_id"].startswith("traj_ng_"), \
        f"expected traj_ng_ prefix, got {rec['traj_id']!r}"
    # metadata.ecosystem 用 spec.label = 'ngagent'
    md_body = Path(rec["path"]).read_text(encoding="utf-8")
    assert "## User" in md_body  # 渲染产物与 opencode 同形
    assert rec["session_id"] == "ses_bbbbbbbbbbbb"

    # bridge dir 真有 traj_ng_*.md 落盘
    traj_files = list(bridge.glob("traj_ng_*.md"))
    assert len(traj_files) == 1


# ──────────────────────────────────────────────────────────────────
# T7: opencode + ngagent coexist
# ──────────────────────────────────────────────────────────────────


def test_ngagent_opencode_coexist(tmp_path):
    """opencode.db 和 ngagent.db 同时存在时，detect 应返回两条独立记录。

    这是 ngagent 设计的关键不变量：它是 opencode 企业分支，但与 opencode
    可以**并存**于同一 HOME，daemon 应为两者各起一个 ingester。
    """
    home = tmp_path / "home"
    home.mkdir()
    _seed_opencode_home(home)
    _seed_ngagent_home(home)

    found = detect_known_ecosystems(home_root=home)
    ecos = {r["ecosystem"] for r in found}
    assert "opencode" in ecos
    assert "ngagent" in ecos

    # 两条记录的 source 路径独立、bridge 路径也独立
    opencode_rec = next(r for r in found if r["ecosystem"] == "opencode")
    ngagent_rec = next(r for r in found if r["ecosystem"] == "ngagent")
    assert opencode_rec["source"] != ngagent_rec["source"]
    assert opencode_rec["bridge"] != ngagent_rec["bridge"]


# ──────────────────────────────────────────────────────────────────
# Spec sanity
# ──────────────────────────────────────────────────────────────────


def test_ngagent_spec_is_frozen_and_typed():
    """``NGAGENT_SPEC`` 是 frozen dataclass，关键字段正确。"""
    assert NGAGENT_SPEC.name == "ngagent"
    assert NGAGENT_SPEC.source_kind == "sqlite"
    assert NGAGENT_SPEC.cursor_strategy == "sqlite_time_updated"
    assert NGAGENT_SPEC.traj_id_prefix == "traj_ng_"
    # frozen → 改字段抛 FrozenInstanceError
    with pytest.raises(Exception):
        NGAGENT_SPEC.name = "x"  # type: ignore[misc]
