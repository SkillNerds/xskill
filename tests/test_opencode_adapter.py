"""
test_opencode_adapter.py -- P3 OpenCode adapter 行为
====================================================

覆盖：

* T1. SqliteIngester.run_once() 从 fixture sample.db 抽 session/message 正确
* T2. cursor 增量：第二次 run_once 返回空（cursor 卡到 max time_updated）
* T3. 新增 session 后再 run_once 能拿到
* T4. install_to_opencode 写到 ``<target_root>/.agents/skills/<name>``（共享目录）
* T5. WAL 并发 stress：后台线程持续 INSERT，主线程持续读，不触发
       ``sqlite3.OperationalError: database is locked`` —— 设计 doc R2 验证
* T6. 路径解析跨平台：``_opencode_db_path`` / ``_agents_skills_path`` 用
       ``Path()`` 拼接，POSIX 与 Windows 上 home 拼出来都正确

SQLite WAL 并发的关键：xskill 用 ``mode=ro&immutable=1`` 打开，即使写端
持有 WAL 写锁也读得到（甚至读到的可能是 stale 数据，但**永远不会拿不到
连接**）。这一条是 OpenCode adapter 上 ingester 能与运行中 opencode
共存的硬约束。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

import pytest

from xskill.ecosystems import (
    SqliteEcosystemSpec,
    OPENCODE_SPEC,
    SqliteIngester,
    _agents_skills_path,
    _opencode_db_path,
    install_to_opencode,
)


# ────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────

FIXTURE_DB = Path(__file__).parent / "fixtures" / "opencode" / "sample.db"


def _seed_opencode_home(home: Path) -> Path:
    """把 fixture sample.db 复制到 ``<home>/.local/share/opencode/opencode.db``。

    返回 db 路径。模拟 OpenCode 在该 home 下跑过的 layout。
    """
    target_dir = home / ".local" / "share" / "opencode"
    target_dir.mkdir(parents=True, exist_ok=True)
    db_path = target_dir / "opencode.db"
    db_path.write_bytes(FIXTURE_DB.read_bytes())
    return db_path


def _make_skill(skill_root: Path, name: str = "test-skill") -> Path:
    """造一个最小可装的 skill：``<skill_root>/<name>/SKILL.md``。"""
    skill_path = skill_root / name
    skill_path.mkdir(parents=True, exist_ok=True)
    (skill_path / "SKILL.md").write_text(
        "---\nname: test-skill\ndescription: just for unit test\n---\n\n# Body\n",
        encoding="utf-8",
    )
    return skill_path


# ────────────────────────────────────────────────────────────────
# T1: ingest a fixture session
# ────────────────────────────────────────────────────────────────


def test_ingester_extracts_session_and_messages(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    _seed_opencode_home(home)

    ing = SqliteIngester(tmp_path / "traj", home_root=home)
    results = ing.run_once()

    assert len(results) == 1
    rec = results[0]
    assert rec["session_id"] == "ses_bbbbbbbbbbbb"
    assert rec["session_directory"] == "/tmp/opencode-test-workspace"
    assert rec["session_time_updated"] == 1777530090000

    # 抽取两条 message，role 分别是 user / assistant
    msgs = rec["messages"]
    assert len(msgs) == 2
    assert msgs[0]["role"] == "user"
    assert msgs[1]["role"] == "assistant"
    # assistant message 的 path.cwd 抽出来与 session.directory 一致
    assert msgs[1]["path"]["cwd"] == "/tmp/opencode-test-workspace"

    # traj 文件落盘成功
    traj_md = Path(rec["path"])
    assert traj_md.is_file()
    body = traj_md.read_text()
    assert "ses_bbbbbbbbbbbb" in body
    assert "/tmp/opencode-test-workspace" in body

    # cursor 卡到这条 session 的 time_updated
    assert ing.cursor_ms == 1777530090000


# ────────────────────────────────────────────────────────────────
# T2: idempotent — second run yields nothing
# ────────────────────────────────────────────────────────────────


def test_ingester_is_idempotent_no_new_session(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    _seed_opencode_home(home)

    ing = SqliteIngester(tmp_path / "traj", home_root=home)
    first = ing.run_once()
    second = ing.run_once()

    assert len(first) == 1
    assert second == []


# ────────────────────────────────────────────────────────────────
# T3: a brand-new session shows up next poll
# ────────────────────────────────────────────────────────────────


def test_ingester_picks_up_new_session_on_next_poll(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    db_path = _seed_opencode_home(home)

    ing = SqliteIngester(tmp_path / "traj", home_root=home)
    first = ing.run_once()
    assert len(first) == 1
    cursor_after_first = ing.cursor_ms

    # 用一个独立 RW 连接造一条新 session + 一条新 message（模拟 OpenCode 跑时
    # 写盘）
    conn = sqlite3.connect(str(db_path))
    try:
        new_sid = "ses_newonenewone"
        ts = cursor_after_first + 5000
        conn.execute(
            "INSERT INTO session "
            "(id, project_id, parent_id, slug, directory, title, version, "
            " time_created, time_updated) "
            "VALUES (?, 'prj_aaaaaaaaaaaa', NULL, ?, ?, ?, ?, ?, ?)",
            (new_sid, "fresh-breeze", "/tmp/another-cwd", "second session", "0.10.0",
             ts, ts),
        )
        msg_data = {"role": "user", "time": {"created": ts}, "agent": "build"}
        conn.execute(
            "INSERT INTO message (id, session_id, time_created, time_updated, data) "
            "VALUES (?, ?, ?, ?, ?)",
            ("msg_new_user", new_sid, ts, ts,
             json.dumps(msg_data, ensure_ascii=False, separators=(",", ":"))),
        )
        conn.commit()
    finally:
        conn.close()

    second = ing.run_once()
    assert len(second) == 1
    assert second[0]["session_id"] == new_sid
    assert second[0]["session_directory"] == "/tmp/another-cwd"
    assert ing.cursor_ms > cursor_after_first


# ────────────────────────────────────────────────────────────────
# T4: install_to_opencode writes to shared ~/.agents/skills/
# ────────────────────────────────────────────────────────────────


def test_install_to_opencode_writes_to_shared_agents_skills(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    skill_root = tmp_path / "skills"
    skill_path = _make_skill(skill_root, name="test-skill")

    dest_md = install_to_opencode(skill_path, target_root=home)

    # 共享目录 —— ``<home>/.agents/skills/<name>/SKILL.md``，不是 ``.opencode/skills/``
    expected = home / ".agents" / "skills" / "test-skill" / "SKILL.md"
    assert dest_md == expected
    assert dest_md.is_file()
    # 内容随源（symlink 模式下立刻穿透；copy 模式下也是初次复制）
    assert "name: test-skill" in dest_md.read_text()


def test_install_to_opencode_does_not_write_to_repo_local_dir(tmp_path):
    """``install_to_opencode`` 不应该走 ``<repo>/.opencode/skills/``——那是
    项目级备选；xskill 统一只写全局共享。"""
    home = tmp_path / "home"
    home.mkdir()
    skill_root = tmp_path / "skills"
    skill_path = _make_skill(skill_root, name="test-skill")

    install_to_opencode(skill_path, target_root=home)

    # 关键：在 target_root（=home）下不应该出现 ``.opencode/skills/``
    repo_local = home / ".opencode" / "skills"
    assert not repo_local.exists()


def test_install_to_opencode_is_idempotent_symlink(tmp_path):
    """第二次装同一 skill，dest symlink 已正确 → no-op，不抛错。"""
    home = tmp_path / "home"
    home.mkdir()
    skill_root = tmp_path / "skills"
    skill_path = _make_skill(skill_root, name="test-skill")

    first = install_to_opencode(skill_path, target_root=home)
    second = install_to_opencode(skill_path, target_root=home)

    assert first == second
    assert first.is_file()


# ────────────────────────────────────────────────────────────────
# T5: WAL concurrency stress — no `database is locked` from reader
# ────────────────────────────────────────────────────────────────


def test_sqlite_ingester_no_lock_under_wal_writer_stress(tmp_path):
    """模拟 OpenCode 持续写 + xskill 持续读：xskill 端**不应该**触发
    ``sqlite3.OperationalError: database is locked``。

    这是设计 doc R2 的核心验证：SqliteIngester 用 ``mode=ro&immutable=1``
    URI 后，**完全跳过 WAL 协议**，即使写端持有所有锁也读得到（拿不到最新
    数据是允许的——poll 周期内下一次会拿到）。

    跑 30+ 次 read：每次开 fresh 连接（与 ingester 实际行为一致），与一个
    后台 INSERT 线程并发。0 锁错误才算过。
    """
    home = tmp_path / "home"
    home.mkdir()
    db_path = _seed_opencode_home(home)

    # 让写端用 WAL 模式（OpenCode 默认就是 WAL）
    init_conn = sqlite3.connect(str(db_path))
    init_conn.execute("PRAGMA journal_mode=WAL")
    init_conn.close()

    stop_writer = threading.Event()
    writer_errors: list[Exception] = []

    def writer():
        """持续 INSERT session/message 直到 stop_writer 触发。"""
        try:
            # 写端用普通 RW 连接
            conn = sqlite3.connect(str(db_path), timeout=5.0)
            try:
                i = 0
                while not stop_writer.is_set():
                    sid = f"ses_writer_{i:04d}"
                    ts = 1777530100000 + i * 100
                    conn.execute(
                        "INSERT INTO session "
                        "(id, project_id, parent_id, slug, directory, title, "
                        " version, time_created, time_updated) "
                        "VALUES (?, 'prj_aaaaaaaaaaaa', NULL, 'x', ?, ?, ?, ?, ?)",
                        (sid, "/tmp/x", f"stress {i}", "0.10.0", ts, ts),
                    )
                    msg_data = {"role": "user", "time": {"created": ts}}
                    conn.execute(
                        "INSERT INTO message "
                        "(id, session_id, time_created, time_updated, data) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (f"msg_{i:04d}", sid, ts, ts,
                         json.dumps(msg_data, ensure_ascii=False,
                                    separators=(",", ":"))),
                    )
                    conn.commit()
                    i += 1
                    time.sleep(0.005)
            finally:
                conn.close()
        except Exception as e:
            writer_errors.append(e)

    t = threading.Thread(target=writer, daemon=True)
    t.start()
    try:
        # 主线程跑 50 次 read（fresh ingester instance + run_once 每次）
        lock_errors: list[Exception] = []
        for _ in range(50):
            ing = SqliteIngester(tmp_path / "traj", home_root=home)
            try:
                ing.run_once()
            except sqlite3.OperationalError as e:
                lock_errors.append(e)
            time.sleep(0.01)
    finally:
        stop_writer.set()
        t.join(timeout=5.0)

    # 写端自身不应该挂（如挂了说明 fixture / 测试逻辑有问题，与 ingester 无关）
    assert not writer_errors, f"writer thread crashed: {writer_errors}"
    # 关键断言：reader 0 锁错误
    assert lock_errors == [], (
        f"SqliteIngester triggered 'database is locked' "
        f"{len(lock_errors)} time(s) under WAL writer stress: "
        f"{[str(e) for e in lock_errors[:3]]}"
    )


# ────────────────────────────────────────────────────────────────
# T6: path helpers — cross-platform
# ────────────────────────────────────────────────────────────────


def test_opencode_db_path_under_xdg_default():
    """``_opencode_db_path`` 在任何平台返回 ``<home>/.local/share/opencode/opencode.db``。

    这是 XDG_DATA_HOME 的默认值；env 解析由 daemon 上层做。
    """
    home = Path("/some/home")
    assert _opencode_db_path(home) == home / ".local" / "share" / "opencode" / "opencode.db"


def test_agents_skills_path_under_home():
    """``_agents_skills_path`` 返回 ``<home>/.agents/skills``（跨 agent 共享根）。"""
    home = Path("/home/u")
    assert _agents_skills_path(home) == home / ".agents" / "skills"


def test_opencode_spec_is_frozen_and_typed():
    """``OPENCODE_SPEC`` 是 frozen dataclass，name/source_kind 字段正确。"""
    assert OPENCODE_SPEC.name == "opencode"
    assert OPENCODE_SPEC.source_kind == "sqlite"
    assert OPENCODE_SPEC.cursor_strategy == "sqlite_time_updated"
    # frozen → 改字段抛 FrozenInstanceError
    with pytest.raises(Exception):  # dataclasses.FrozenInstanceError 是 dataclasses 内部异常
        OPENCODE_SPEC.name = "x"  # type: ignore[misc]


def test_sqlite_ingester_rejects_non_sqlite_spec():
    """误传 source_kind='jsonl' 的 spec 应 fail-loud（CLAUDE.md throw error）。"""
    bad_spec = SqliteEcosystemSpec(
        name="fake",
        source_kind="jsonl",
        path_resolver=lambda h: h,
        cursor_strategy="mtime_offset",
        label="fake",
    )
    with pytest.raises(ValueError, match="sqlite"):
        SqliteIngester("/tmp/x", spec=bad_spec)
