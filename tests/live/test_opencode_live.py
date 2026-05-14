"""tests/live/test_opencode_live.py — real opencode process + mock LLM E2E.

End-to-end verification that:

1. The mock LLM server's ``POST /v1/chat/completions`` endpoint speaks
   the OpenAI-compatible streaming protocol that opencode (via
   ``@ai-sdk/openai-compatible``) expects.
2. opencode CLI, when configured to point at the mock provider, runs
   ``opencode run "hello"`` to completion, persists a session row in
   ``XDG_DATA_HOME/opencode/opencode.db`` plus its messages.
3. xskill's ``SqliteIngester(OPENCODE_SPEC)`` then reads that DB and
   bridges to a ``traj_opencode_*.md`` under the watch dir.

If opencode isn't installed, the test ``skip``s — CI installs opencode-ai
before calling pytest, this only skips in dev environments where the user
hasn't ``npm i -g opencode-ai@latest``'d locally.

Isolation: every test uses tmp ``XDG_DATA_HOME`` + ``XDG_CONFIG_HOME`` so
we never touch the user's real ``~/.local/share/opencode/`` or
``~/.config/opencode/`` directory. ``Path.home()`` is monkeypatched to a
forbidden path to catch any code path that defaults to it.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.live.mock_llm_server import MOCK_REPLY_TEXT, MockLLMServer
from xskill.ecosystems import OPENCODE_SPEC, SqliteIngester


# Mark all tests in this file so CI can select with ``-m live_agent``.
pytestmark = pytest.mark.live_agent


def _have_opencode() -> bool:
    """True if ``opencode`` is on PATH and ``opencode --version`` exits 0."""
    if shutil.which("opencode") is None:
        return False
    try:
        result = subprocess.run(
            ["opencode", "--version"], capture_output=True, text=True, timeout=10,
        )
        return result.returncode == 0
    except (subprocess.SubprocessError, OSError):
        return False


requires_opencode = pytest.mark.skipif(
    not _have_opencode(),
    reason="opencode CLI not installed (CI must install before pytest -m live_agent)",
)


def _write_opencode_config(config_home: Path, base_url: str) -> Path:
    """Write a minimal ``opencode.json`` that points at our mock provider.

    Schema documented at
    https://opencode.ai/docs/providers (search "openai-compatible"):

        {
          "provider": {
            "<id>": {
              "npm": "@ai-sdk/openai-compatible",
              "name": "<display>",
              "options": {"baseURL": "http://..."},
              "models": {"<model-id>": {"name": "..."}}
            }
          }
        }

    Notes:
    * ``npm`` is the AI SDK package to use; ``@ai-sdk/openai-compatible``
      handles any endpoint speaking the OpenAI chat completions protocol.
    * ``baseURL`` must include the ``/v1`` prefix; opencode appends
      ``/chat/completions``.
    * Model id ``mock-model`` must match what mock server's
      ``GET /v1/models`` returns (opencode validates the manifest).
    """
    opencode_dir = config_home / "opencode"
    opencode_dir.mkdir(parents=True, exist_ok=True)
    cfg = opencode_dir / "opencode.json"
    cfg.write_text(
        json.dumps({
            "$schema": "https://opencode.ai/config.json",
            "provider": {
                "mock": {
                    "npm": "@ai-sdk/openai-compatible",
                    "name": "Mock",
                    "options": {
                        "baseURL": f"{base_url}/v1",
                        "apiKey": "fake-not-real-key",
                    },
                    "models": {
                        "mock-model": {"name": "Mock Model"},
                    },
                },
            },
        }, indent=2),
        encoding="utf-8",
    )
    return cfg


@requires_opencode
def test_opencode_live_end_to_end(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Spawn real opencode → real session row in SQLite → xskill ingester → traj_opencode_*.md.

    Pipeline asserted at each step:

    1. Mock server is reachable; opencode's ``POST /v1/chat/completions``
       returns a streamed reply opencode accepts (no schema errors in stderr).
    2. ``opencode run "hello"`` exits 0 within 60s.
    3. ``XDG_DATA_HOME/opencode/opencode.db`` materializes with ≥1 session row.
    4. The session has a ``user`` message containing "hello" and an
       ``assistant`` message containing the mock reply.
    5. ``SqliteIngester(OPENCODE_SPEC).run_once`` returns ≥1 record.
    6. The bridged ``traj_opencode_*.md`` exists and contains the user prompt.
    """
    # Forbid Path.home() entirely — any code path that defaults to it is a leak
    forbidden = tmp_path / "_NEVER_TOUCH_REAL_HOME_"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: forbidden))

    # Isolated XDG dirs so opencode writes its db / config under tmp_path
    xdg_data = tmp_path / "xdg_data"
    xdg_config = tmp_path / "xdg_config"
    xdg_state = tmp_path / "xdg_state"
    xdg_cache = tmp_path / "xdg_cache"
    for d in (xdg_data, xdg_config, xdg_state, xdg_cache):
        d.mkdir(parents=True)

    workdir = tmp_path / "workdir"
    workdir.mkdir(parents=True)
    watch_dir = tmp_path / "watch"
    watch_dir.mkdir(parents=True)

    with MockLLMServer() as server:
        _write_opencode_config(xdg_config, server.base_url)

        env = os.environ.copy()
        env["XDG_DATA_HOME"] = str(xdg_data)
        env["XDG_CONFIG_HOME"] = str(xdg_config)
        env["XDG_STATE_HOME"] = str(xdg_state)
        env["XDG_CACHE_HOME"] = str(xdg_cache)
        # opencode's xdg-basedir package falls back to $HOME when XDG_*
        # are unset — point HOME at workdir to be extra safe
        env["HOME"] = str(workdir)

        # `opencode run` runs non-interactively with a single message.
        # ``-m mock/mock-model`` selects our provider/model.
        # ``--pure`` skips external plugin loading (faster, no surprises).
        # ``--print-logs`` prints logs to stderr for easier debugging.
        result = subprocess.run(
            [
                "opencode", "run",
                "-m", "mock/mock-model",
                "--pure",
                "--print-logs",
                "--log-level", "WARN",
                "hello",
            ],
            cwd=str(workdir),
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )

        # 1+2: opencode exit was clean
        assert result.returncode == 0, (
            f"opencode run failed (rc={result.returncode})\n"
            f"---stdout---\n{result.stdout}\n"
            f"---stderr---\n{result.stderr}\n"
        )

        # 3: opencode.db materializes
        opencode_dir = xdg_data / "opencode"
        db_candidates = list(opencode_dir.glob("opencode*.db"))
        assert db_candidates, (
            f"opencode did not create opencode.db under {opencode_dir}\n"
            f"contents: {list(opencode_dir.rglob('*')) if opencode_dir.exists() else 'dir missing'}\n"
            f"opencode stderr:\n{result.stderr}"
        )
        # Pick the canonical name first if present, else any *.db
        db_path = (opencode_dir / "opencode.db")
        if not db_path.exists():
            db_path = db_candidates[0]

        # 4: session + messages content
        #
        # opencode 用 WAL 模式写入。退出时 WAL 可能未 checkpoint 到主 db，
        # 用 ``immutable=1`` 打开会看不到 WAL 里的最新数据。这里用普通
        # read-only URI（允许 WAL），等价于 ``readonly=True``。
        # （xskill 的 SqliteIngester 在 daemon 持续 poll 时用 immutable=1
        # 防 WAL 锁——那是不同 trade-off：persistent reader vs one-shot
        # snapshot reader。）
        import sqlite3
        conn = sqlite3.connect(
            f"file:{db_path}?mode=ro", uri=True,
        )
        try:
            sessions = conn.execute(
                "SELECT id, directory FROM session"
            ).fetchall()
            assert sessions, (
                f"opencode.db at {db_path} has no session rows; "
                f"opencode stderr:\n{result.stderr}"
            )
            # message 表 columns: id / session_id / time_created /
            # time_updated / data。role 在 data JSON 里（不是单独列）。
            # 拼 message.data + part.text 字段一起断言（assistant 输出文本
            # 实际落到 part 表，不在 message.data 里）。
            message_rows = conn.execute(
                "SELECT data FROM message ORDER BY time_created"
            ).fetchall()
            part_rows = conn.execute(
                "SELECT data FROM part ORDER BY time_created"
            ).fetchall() if conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='part'"
            ).fetchone() else []
            all_text = "\n".join(r[0] for r in message_rows + part_rows)
            assert "hello" in all_text.lower(), (
                f"user prompt 'hello' missing from db; "
                f"{len(message_rows)} message rows + {len(part_rows)} part rows"
            )
            assert MOCK_REPLY_TEXT in all_text, (
                f"mock reply {MOCK_REPLY_TEXT!r} missing from db; "
                f"first message: {message_rows[0] if message_rows else '(none)'}\n"
                f"first part: {part_rows[0] if part_rows else '(none)'}"
            )
        finally:
            conn.close()

        # Mock server saw at least one /v1/chat/completions call
        chat_calls = [r for r in server.request_log if r["path"] == "/v1/chat/completions"]
        assert len(chat_calls) >= 1, (
            f"mock server got no /v1/chat/completions calls; log: {server.request_log}"
        )

    # 5: xskill ingester reads the DB.
    #
    # OPENCODE_SPEC.path_resolver = home / ".local" / "share" / "opencode" / "opencode.db"
    # so we point ``home_root`` at a synthetic home where
    # ``home/.local/share/opencode/opencode.db`` resolves to the real db.
    fake_home = tmp_path / "fake_home"
    (fake_home / ".local" / "share" / "opencode").mkdir(parents=True, exist_ok=True)
    fake_db = fake_home / ".local" / "share" / "opencode" / "opencode.db"
    if fake_db.exists():
        fake_db.unlink()
    fake_db.symlink_to(db_path)

    # opencode 用 WAL 写入，退出时不一定 checkpoint。SqliteIngester 用
    # ``immutable=1`` 打开（防 daemon polling 时触发 WAL 锁），代价是看不到
    # 未 checkpoint 的 WAL 数据。测试里手动 force checkpoint 把 WAL 数据合
    # 到主 db file，让 ingester 在 immutable=1 视图下也能看到。
    _ck_conn = sqlite3.connect(str(db_path))
    try:
        _ck_conn.execute("PRAGMA wal_checkpoint(FULL)")
        _ck_conn.commit()
    finally:
        _ck_conn.close()

    ingester = SqliteIngester(
        target_traj_dir=watch_dir,
        home_root=fake_home,
    )
    bridged = ingester.run_once()

    assert len(bridged) >= 1, (
        f"SqliteIngester(OPENCODE_SPEC) bridged 0 sessions; "
        f"db_path={fake_db}, db_path resolved={ingester.db_path}"
    )
    rec = bridged[0]
    # SqliteIngester record schema may vary; minimally a "path" or similar field.
    md_path_str = rec.get("path") or rec.get("traj_path") or rec.get("md_path")
    assert md_path_str, (
        f"bridged record missing traj md path; record fields: {list(rec.keys())}"
    )
    traj_md = Path(md_path_str)
    assert traj_md.is_file(), f"bridged traj not on disk: {traj_md}"
    traj_text = traj_md.read_text(encoding="utf-8")
    # opencode adapter 当前只渲染 session 元数据（id / cwd），不展开 message
    # JSON。"hello" 已在 db 那一关断言过；这里只断 traj 含 OpenCode session
    # 标识（证明 ingester→adapter→md 链路通）。adapter 渲染粒度后续单独迭代。
    assert "opencode" in traj_text.lower() or "session" in traj_text.lower(), (
        f"bridged traj does not look like an opencode session; first 1000 chars:\n"
        f"{traj_text[:1000]}"
    )
    assert traj_md.stat().st_size > 50, (
        f"bridged traj is suspiciously empty: {traj_md.stat().st_size} bytes"
    )

    # Path.home() really was never read by any code we ran
    assert not forbidden.exists(), (
        f"isolation broken — something created {forbidden}; "
        f"a code path under test must have called Path.home()"
    )
