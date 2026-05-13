"""
opencode-fixture-generator.py — 生成符合 OpenCode schema 的真实 SQLite fixture

来源：1:1 复现 ``~/learn/opencode/packages/opencode/src/session/session.sql.ts``
里 drizzle ORM 定义的 ``session`` / ``message`` / ``project`` 三表 schema
（main agent 已读完源码 + 本机 ``~/.local/share/opencode/opencode.db``
实测 schema 双向确认）。

P3 subagent 用法：
    cp docs/dev-plan/opencode-fixture-generator.py tests/fixtures/opencode/generate.py
    cd tests/fixtures/opencode && python3 generate.py sample.db
    # 产出 sample.db 入仓（脱敏版，1 session + 2 message）
"""
from __future__ import annotations

import json
import sqlite3
import sys
import uuid as _uuid
from pathlib import Path


# session 表 DDL（来自 ~/.local/share/opencode/opencode.db .schema session）
_SESSION_DDL = """
CREATE TABLE `session` (
    `id` text PRIMARY KEY,
    `project_id` text NOT NULL,
    `parent_id` text,
    `slug` text NOT NULL,
    `directory` text NOT NULL,
    `title` text NOT NULL,
    `version` text NOT NULL,
    `share_url` text,
    `summary_additions` integer,
    `summary_deletions` integer,
    `summary_files` integer,
    `summary_diffs` text,
    `revert` text,
    `permission` text,
    `time_created` integer NOT NULL,
    `time_updated` integer NOT NULL,
    `time_compacting` integer,
    `time_archived` integer,
    `workspace_id` text,
    `path` text
)
"""

_MESSAGE_DDL = """
CREATE TABLE `message` (
    `id` text PRIMARY KEY,
    `session_id` text NOT NULL,
    `time_created` integer NOT NULL,
    `time_updated` integer NOT NULL,
    `data` text NOT NULL
)
"""

_PROJECT_DDL = """
CREATE TABLE `project` (
    `id` text PRIMARY KEY,
    `directory` text NOT NULL,
    `time_created` integer NOT NULL
)
"""

_MESSAGE_INDEX = """
CREATE INDEX `message_session_time_created_id_idx`
ON `message` (`session_id`, `time_created`, `id`)
"""


def write_sample_fixture(out_path: Path) -> Path:
    """生成一份给 xskill OpenCode adapter 单测用的 SQLite fixture。

    Deterministic IDs / timestamps，让 hash-stable 断言成立。

    内容：
    - 1 project（cwd = /tmp/opencode-test-workspace）
    - 1 session（指向 project，agent=build，model=deepseek-v4-flash）
    - 2 message（user + assistant 各一条；data 是 OpenCode 自己的 JSON）
    """
    if out_path.exists():
        out_path.unlink()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(out_path))
    try:
        conn.executescript(_SESSION_DDL + ";" + _MESSAGE_DDL + ";" + _PROJECT_DDL + ";" + _MESSAGE_INDEX)

        # Deterministic IDs
        proj_id = "prj_aaaaaaaaaaaa"
        sess_id = "ses_bbbbbbbbbbbb"
        msg_user_id = "msg_user_111"
        msg_assist_id = "msg_assist_222"

        # Deterministic timestamps (epoch ms in OpenCode)
        ts_proj = 1777530000000
        ts_sess_create = 1777530036000
        ts_sess_update = 1777530090000
        ts_msg_user = 1777530036589
        ts_msg_assist = 1777530080123

        conn.execute(
            "INSERT INTO project (id, directory, time_created) VALUES (?, ?, ?)",
            (proj_id, "/tmp/opencode-test-workspace", ts_proj),
        )

        conn.execute(
            """INSERT INTO session
               (id, project_id, parent_id, slug, directory, title, version,
                time_created, time_updated)
               VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?)""",
            (
                sess_id, proj_id,
                "quiet-harbor", "/tmp/opencode-test-workspace",
                "Hello world task", "0.10.0",
                ts_sess_create, ts_sess_update,
            ),
        )

        # User message — data 字段是 OpenCode 自己的 JSON-in-text
        user_data = {
            "role": "user",
            "time": {"created": ts_msg_user},
            "agent": "build",
            "model": {"providerID": "deepseek", "modelID": "deepseek-v4-flash"},
            "summary": {"diffs": []},
        }
        conn.execute(
            "INSERT INTO message (id, session_id, time_created, time_updated, data) "
            "VALUES (?, ?, ?, ?, ?)",
            (msg_user_id, sess_id, ts_msg_user, ts_msg_user,
             json.dumps(user_data, ensure_ascii=False, separators=(",", ":"))),
        )

        # Assistant message
        assist_data = {
            "parentID": msg_user_id,
            "role": "assistant",
            "mode": "build",
            "agent": "build",
            "path": {"cwd": "/tmp/opencode-test-workspace", "root": "/"},
            "cost": 0.00165858,
            "tokens": {"total": 11710, "input": 9800, "output": 1910},
        }
        conn.execute(
            "INSERT INTO message (id, session_id, time_created, time_updated, data) "
            "VALUES (?, ?, ?, ?, ?)",
            (msg_assist_id, sess_id, ts_msg_assist, ts_msg_assist,
             json.dumps(assist_data, ensure_ascii=False, separators=(",", ":"))),
        )

        conn.commit()
    finally:
        conn.close()

    return out_path


if __name__ == "__main__":
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "./sample.db")
    path = write_sample_fixture(out)
    print(f"wrote: {path}")
    print(f"size: {path.stat().st_size} bytes")
    # Print row counts as a sanity smoke check
    conn = sqlite3.connect(str(path))
    print("rows:")
    for tbl in ("project", "session", "message"):
        n = conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
        print(f"  {tbl}: {n}")
    conn.close()
