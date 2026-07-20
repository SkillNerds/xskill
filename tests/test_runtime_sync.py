"""tests/test_runtime_sync.py — runtime multi-agent sync 单测

覆盖范围
========

G1 — Ingester daemon thread:
    * JsonlIngester.start() / stop() 周期性调 scan_and_bridge，stop 后不留 zombie
    * SqliteIngester.start() / stop() 同上（针对 OpenCode SQLite 形态）

G2 — Watcher multi-agent install + 失败记录:
    * _install_skill_to_all_detected 对 detected 的 3 个生态都调 installer
    * 单个 agent installer raise 不阻断其它 agent；走 record_fail 写入
      ~/.xskill/install_history.jsonl（action="fail" 字段）

这层只跑 in-process 单元测试，无外部进程（codex / opencode CLI）。E2E live
测试在 ``tests/live/test_runtime_sync_live.py`` 单独跑。
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest

from xskill.ecosystems import (
    CC_SPEC,
    CODEX_SPEC,
    OPENCODE_SPEC,
    JsonlIngester,
    SqliteIngester,
)
from xskill.ecosystems._history import InstallHistory


# ─────────────────────────────────────────────────────────────────
# G1 — Ingester daemon thread
# ─────────────────────────────────────────────────────────────────


class TestJsonlIngesterDaemonThread:
    """JsonlIngester.start() / stop() — 周期性 poll，干净退出，无 zombie。"""

    def _make_codex_rollout(self, codex_home: Path, sid: str) -> Path:
        """在 ``<codex_home>/.codex/sessions/2026/05/14/`` 写一条合规 rollout。

        codex JSONL 首行必须是 ``type=session_meta``，否则 cwd 抽取走默认空。
        这里造一条最小可被 ``JsonlIngester(CODEX_SPEC)`` 接受的 session。
        """
        sess_dir = codex_home / ".codex" / "sessions" / "2026" / "05" / "14"
        sess_dir.mkdir(parents=True, exist_ok=True)
        rollout = sess_dir / f"rollout-2026-05-14T10-00-00-{sid}.jsonl"
        rollout.write_text(
            json.dumps({
                "type": "session_meta",
                "payload": {"cwd": "/tmp/codex_proj", "id": sid},
                "timestamp": "2026-05-14T10:00:00.000Z",
            }) + "\n" +
            json.dumps({
                "type": "response_item",
                "payload": {"type": "message", "role": "user",
                            "content": [{"type": "input_text", "text": "hello"}]},
                "timestamp": "2026-05-14T10:00:01.000Z",
            }) + "\n",
            encoding="utf-8",
        )
        return rollout

    def test_start_stop_no_zombie_thread(self, tmp_path):
        """起 daemon thread → stop → 确认 thread 已 dead。"""
        ing = JsonlIngester(
            CODEX_SPEC,
            target_traj_dir=tmp_path / "traj",
            home_root=tmp_path,
            poll_interval=0.1,
        )
        assert not ing.is_running
        ing.start()
        assert ing.is_running
        # 让 loop 至少跑一轮
        time.sleep(0.3)
        ing.stop()
        # stop 后 thread 必须 join 完成
        assert not ing.is_running, "thread still alive after stop() — zombie thread leak"
        # _thread 对象仍在，但 is_alive() == False
        assert ing._thread is not None
        assert not ing._thread.is_alive()

    def test_start_is_idempotent(self, tmp_path):
        """重复 start 不应起多个 thread（必须 no-op）。"""
        ing = JsonlIngester(
            CODEX_SPEC,
            target_traj_dir=tmp_path / "traj",
            home_root=tmp_path,
            poll_interval=0.1,
        )
        ing.start()
        first_thread = ing._thread
        ing.start()  # second call
        assert ing._thread is first_thread, "start() created a 2nd thread; not idempotent"
        ing.stop()

    def test_start_without_target_traj_dir_raises(self):
        """daemon thread 模式必须在 __init__ 传 target_traj_dir。"""
        ing = JsonlIngester(CODEX_SPEC)
        with pytest.raises(RuntimeError, match="target_traj_dir"):
            ing.start()

    def test_loop_picks_up_new_session_added_after_start(self, tmp_path):
        """daemon 跑期间新写入 session → 下一轮 poll 桥接。"""
        traj_dir = tmp_path / "traj"
        ing = JsonlIngester(
            CODEX_SPEC,
            target_traj_dir=traj_dir,
            home_root=tmp_path,
            poll_interval=0.2,
        )
        ing.start()
        # 启动后再写 session：模拟用户跑了一条 codex 命令
        self._make_codex_rollout(tmp_path, sid="11111111-2222-3333-4444-555555555555")
        # 等 ≥2 个 poll 周期 + adapter 处理时间
        deadline = time.time() + 5.0
        bridged_md = None
        while time.time() < deadline:
            md_files = list(traj_dir.glob("traj_codex_*.md"))
            if md_files:
                bridged_md = md_files[0]
                break
            time.sleep(0.1)
        ing.stop()
        assert bridged_md is not None, (
            f"daemon thread did not bridge new codex session within 5s; "
            f"traj_dir contents: {list(traj_dir.glob('*')) if traj_dir.exists() else 'no dir'}"
        )
        # stats 验证
        assert ing.stats["polls"] >= 1
        assert ing.stats["ingested"] >= 1

    def test_cc_spec_also_works_via_jsonl_ingester(self, tmp_path):
        """CC spec 也能走 JsonlIngester.start——CCSessionIngester 是 wrapper
        加灰度逻辑；底层 jsonl 扫盘逻辑共享。"""
        ing = JsonlIngester(
            CC_SPEC,
            target_traj_dir=tmp_path / "traj",
            home_root=tmp_path,
            poll_interval=0.1,
        )
        ing.start()
        time.sleep(0.2)
        ing.stop()
        assert not ing.is_running


class TestSqliteIngesterDaemonThread:
    """SqliteIngester.start() / stop() — OpenCode SQLite 形态。"""

    def _make_opencode_db(self, home_root: Path) -> Path:
        """在 ``<home>/.local/share/opencode/opencode.db`` 造一个最小 schema 的
        SQLite，含一条 session + 一条 message + 一条 text part。
        """
        db_dir = home_root / ".local" / "share" / "opencode"
        db_dir.mkdir(parents=True, exist_ok=True)
        db_path = db_dir / "opencode.db"
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS session (
                    id TEXT PRIMARY KEY,
                    directory TEXT,
                    time_updated INTEGER
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS message (
                    id TEXT PRIMARY KEY,
                    session_id TEXT,
                    time_created INTEGER,
                    data TEXT
                )
            """)
            # OpenCode 把消息内容存在 part 表（见 ecosystems/opencode.py）
            conn.execute("""
                CREATE TABLE IF NOT EXISTS part (
                    id TEXT PRIMARY KEY,
                    message_id TEXT,
                    session_id TEXT,
                    time_created INTEGER,
                    data TEXT
                )
            """)
            ts = int(time.time() * 1000)
            conn.execute(
                "INSERT INTO session (id, directory, time_updated) VALUES (?, ?, ?)",
                ("ses_abcd1234", "/tmp/oc_proj", ts),
            )
            conn.execute(
                "INSERT INTO message (id, session_id, time_created, data) VALUES (?, ?, ?, ?)",
                ("msg_1", "ses_abcd1234", ts,
                 json.dumps({"role": "user", "time": {"created": ts}})),
            )
            conn.execute(
                "INSERT INTO part (id, message_id, session_id, time_created, data) "
                "VALUES (?, ?, ?, ?, ?)",
                ("prt_1", "msg_1", "ses_abcd1234", ts,
                 json.dumps({"type": "text", "text": "hello opencode"})),
            )
            conn.commit()
        finally:
            conn.close()
        return db_path

    def test_start_stop_no_zombie_thread(self, tmp_path):
        ing = SqliteIngester(
            target_traj_dir=tmp_path / "traj",
            home_root=tmp_path,
            spec=OPENCODE_SPEC,
            poll_interval=0.1,
        )
        assert not ing.is_running
        ing.start()
        assert ing.is_running
        time.sleep(0.3)
        ing.stop()
        assert not ing.is_running, "SqliteIngester thread leaked after stop()"
        assert not ing._thread.is_alive()

    def test_start_is_idempotent(self, tmp_path):
        ing = SqliteIngester(
            target_traj_dir=tmp_path / "traj",
            home_root=tmp_path,
            spec=OPENCODE_SPEC,
            poll_interval=0.1,
        )
        ing.start()
        first_thread = ing._thread
        ing.start()
        assert ing._thread is first_thread
        ing.stop()

    def test_loop_picks_up_new_session_added_after_start(self, tmp_path):
        """daemon 跑期间 OpenCode DB 写新 session → 下一轮 poll 桥接。

        SqliteIngester 用 immutable=1 打开，需要 OpenCode 写完后 force
        checkpoint 才能在 immutable 视图下看到新 session（实际 OpenCode
        默认 wal_autocheckpoint=1000，几秒一次）；测试里手动 checkpoint。
        """
        traj_dir = tmp_path / "traj"
        ing = SqliteIngester(
            target_traj_dir=traj_dir,
            home_root=tmp_path,
            spec=OPENCODE_SPEC,
            poll_interval=0.2,
        )
        # 先造空 db（保证 path 存在让 daemon 不会因 db_path.is_file()==False 跳过）
        db_path = self._make_opencode_db(tmp_path)
        # checkpoint 强制 WAL 数据 → 主 db file
        ck = sqlite3.connect(str(db_path))
        try:
            ck.execute("PRAGMA wal_checkpoint(FULL)")
            ck.commit()
        finally:
            ck.close()

        ing.start()
        deadline = time.time() + 5.0
        bridged_md = None
        while time.time() < deadline:
            md_files = list(traj_dir.glob("traj_oc_*.md"))
            if md_files:
                bridged_md = md_files[0]
                break
            time.sleep(0.1)
        ing.stop()
        assert bridged_md is not None, (
            f"daemon thread did not bridge new opencode session within 5s; "
            f"traj_dir contents: {list(traj_dir.glob('*')) if traj_dir.exists() else 'no dir'}"
        )
        assert ing.stats["polls"] >= 1
        assert ing.stats["ingested"] >= 1


# ─────────────────────────────────────────────────────────────────
# G2 — Watcher multi-agent install + 失败记录
# ─────────────────────────────────────────────────────────────────


def _build_skill(skill_root: Path, name: str = "list-py-files") -> Path:
    """造一个最小可被 install_to_* 接受的 skill 目录。"""
    skill = skill_root / name
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: " + name + "\ndescription: test\n---\n# Test skill\n",
        encoding="utf-8",
    )
    return skill


class TestWatcherInstallSkillToAllDetected:
    """Watcher._install_skill_to_all_detected — multi-agent + 失败处理。"""

    def _make_watcher(self, tmp_path):
        from xskill.pipeline.runner import DirectoryWatcher
        return DirectoryWatcher(
            llm=None, embed_client=None, config={},
            skill_dir=tmp_path / "skill",
            home_root=tmp_path,
            xskill_home=tmp_path,
        )

    def _make_all_3_agents_detected(self, tmp_path):
        """造 3 个生态都 detected 的 home 布局：

        * ``<home>/.claude/projects/`` (dir for claude_code)
        * ``<home>/.codex/sessions/`` (dir for codex)
        * ``<home>/.local/share/opencode/opencode.db`` (file for opencode)
        """
        (tmp_path / ".claude" / "projects").mkdir(parents=True)
        (tmp_path / ".codex" / "sessions").mkdir(parents=True)
        (tmp_path / ".local" / "share" / "opencode").mkdir(parents=True)
        (tmp_path / ".local" / "share" / "opencode" / "opencode.db").write_bytes(b"")

    def test_installs_to_all_3_detected_agents(self, tmp_path):
        """3 agent 都 detect → 3 个 installer 都被调；都成功 → results 含 3 entry。"""
        self._make_all_3_agents_detected(tmp_path)
        skill = _build_skill(tmp_path / "skill")
        watcher = self._make_watcher(tmp_path)

        results = watcher._install_skill_to_all_detected(skill)

        assert set(results.keys()) == {"claude_code", "codex", "opencode"}
        for agent, val in results.items():
            assert not isinstance(val, Exception), (
                f"{agent} install raised: {val}"
            )
            assert isinstance(val, Path), f"{agent} unexpected result: {val}"
            assert val.exists(), f"{agent} install dest does not exist: {val}"

    def test_single_agent_failure_does_not_block_others(
        self, tmp_path, monkeypatch
    ):
        """codex installer raise → claude_code + opencode 仍装成功 + 失败记录写入。"""
        self._make_all_3_agents_detected(tmp_path)
        skill = _build_skill(tmp_path / "skill")
        watcher = self._make_watcher(tmp_path)

        # 让 install_to_codex 抛错（模拟跨盘 copy fallback 等运行时异常）
        from xskill import ecosystems as eco

        def _broken_codex(skill_path, *, target_root=None, side="main"):
            raise RuntimeError("simulated codex install crash")

        monkeypatch.setattr(eco, "install_to_codex", _broken_codex)

        # watcher 在构造时已绑定实例根，历史记录必须落在该实例内。
        history_path = tmp_path / "install_history.jsonl"

        results = watcher._install_skill_to_all_detected(skill)

        # claude_code / opencode 成功
        assert isinstance(results["claude_code"], Path)
        assert isinstance(results["opencode"], Path)
        # codex 失败
        assert isinstance(results["codex"], Exception)
        assert "simulated codex install crash" in str(results["codex"])

        # 失败记录写入了 install_history.jsonl 并且只有 codex 这条
        assert history_path.is_file()
        ih = InstallHistory(history_path)
        fails = ih.fail_records()
        assert len(fails) == 1
        assert fails[0]["action"] == "fail"
        assert fails[0]["agent"] == "codex"
        assert fails[0]["skill"] == skill.name
        assert "codex install crash" in fails[0]["reason"]

    def test_install_history_lookup_unaffected_by_fail_record(
        self, tmp_path, monkeypatch
    ):
        """fail 行不污染 lookup() / count_by_side() 的语义。"""
        history_path = tmp_path / "ih.jsonl"
        ih = InstallHistory(history_path)
        ih.record(skill="s1", side="main", sha="abc", t=100.0)
        ih.record_fail(skill="s1", agent="codex", reason="boom", t=150.0)
        ih.record(skill="s1", side="staging", sha="def", t=200.0)

        # lookup at t=180 → 应当返回 main 那条（t=100），不是 fail 那条
        rec = ih.lookup(180.0, skill="s1")
        assert rec is not None and rec["side"] == "main"
        # lookup at t=250 → staging
        rec2 = ih.lookup(250.0, skill="s1")
        assert rec2 is not None and rec2["side"] == "staging"

        # count 只算成功
        counts = ih.count_by_side(skill="s1")
        assert counts == {"main": 1, "staging": 1}

        # fail_records 拿到那一条
        fails = ih.fail_records()
        assert len(fails) == 1
        assert fails[0]["agent"] == "codex"

    def test_no_agent_detected_returns_empty_results(self, tmp_path):
        """home 下啥也没造 → detect 空 → installer 一个都不调 → results = {}。"""
        skill_root = tmp_path / "skill_root"
        skill_root.mkdir(parents=True)
        skill = _build_skill(skill_root)
        watcher = self._make_watcher(tmp_path)
        results = watcher._install_skill_to_all_detected(skill)
        assert results == {}

    def test_old_install_skill_to_cc_still_works_as_wrapper(self, tmp_path):
        """``_install_skill_to_cc`` 别名仍 work，走的是 multi-agent 逻辑。"""
        self._make_all_3_agents_detected(tmp_path)
        skill = _build_skill(tmp_path / "skill")
        watcher = self._make_watcher(tmp_path)
        results = watcher._install_skill_to_cc(skill)
        assert set(results.keys()) == {"claude_code", "codex", "opencode"}
