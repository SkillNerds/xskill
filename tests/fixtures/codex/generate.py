"""
codex-fixture-generator.py — 生成符合 codex-rs schema 的真实 rollout fixture

来源：完整 1:1 移植 ``~/learn/codex/codex-rs/rollout/src/tests.rs::write_session_file_with_provider``
逻辑（main agent 已读完源码确认）。Schema 100% 真实——来自 codex 项目自己的
单测，他们用同一份 JSON 写文件、读取、断言 round-trip。

P2 subagent 用法：
    cp docs/dev-plan/codex-fixture-generator.py tests/fixtures/codex/generate.py
    cd tests/fixtures/codex && python3 generate.py
    # 产出 sample_rollout.jsonl 入仓
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import sys
import uuid as _uuid
from pathlib import Path


# RolloutItem schema 摘要（来自 codex-rs/protocol/src/protocol.rs）
# ┌─────────────────────────────────────────────────────────────────────┐
# │ #[serde(tag = "type", content = "payload", rename_all = "snake_case")]│
# │ pub enum RolloutItem {                                              │
# │     SessionMeta(SessionMetaLine),    -> type: "session_meta"        │
# │     ResponseItem(ResponseItem),      -> type: "response_item"       │
# │     Compacted(CompactedItem),        -> type: "compacted"           │
# │     TurnContext(TurnContextItem),    -> type: "turn_context"        │
# │     EventMsg(EventMsg),              -> type: "event_msg"           │
# │ }                                                                    │
# └─────────────────────────────────────────────────────────────────────┘
#
# 包装格式（每行）：
#   {"timestamp": "<ISO ts>", "type": "<variant>", "payload": {...}}


def _ts_iso(dt: _dt.datetime) -> str:
    """Codex 文件名用 ``YYYY-MM-DDTHH-MM-SS``（: 替换 - 兼容 NTFS）。"""
    return dt.strftime("%Y-%m-%dT%H-%M-%S")


def write_session_file(
    root: Path,
    *,
    ts: _dt.datetime | None = None,
    session_uuid: _uuid.UUID | None = None,
    cwd: str = ".",
    originator: str = "codex-cli",
    cli_version: str = "0.130.0",
    model_provider: str | None = "openai",
    num_response_records: int = 2,
) -> Path:
    """生成一份 codex-rs schema 的 rollout JSONL 文件。

    Args:
        root: codex home（``~/.codex/`` 等价），fixture 内部会自动追加
              ``sessions/YYYY/MM/DD/`` 子目录
        ts: 会话时间戳；默认 ``2026-01-15T10:00:00 UTC``（deterministic）
        session_uuid: 会话 UUID；默认 deterministic 值
        cwd: SessionMeta.cwd（user 当时的工作目录）
        originator: ``codex-cli`` / ``vscode`` / ``atlas`` / ``chatgpt``
        cli_version: 版本字符串
        model_provider: provider 名（``openai`` / ``deepseek`` etc.）
        num_response_records: 额外多少条 ``response_item`` 占位记录

    Returns:
        生成的 ``.jsonl`` 文件绝对路径
    """
    if ts is None:
        ts = _dt.datetime(2026, 1, 15, 10, 0, 0, tzinfo=_dt.timezone.utc)
    if session_uuid is None:
        session_uuid = _uuid.UUID("01234567-89ab-cdef-0123-456789abcdef")

    ts_str = _ts_iso(ts)
    sessions_dir = root / "sessions" / f"{ts.year:04d}" / f"{ts.month:02d}" / f"{ts.day:02d}"
    sessions_dir.mkdir(parents=True, exist_ok=True)

    filename = f"rollout-{ts_str}-{session_uuid}.jsonl"
    file_path = sessions_dir / filename

    lines: list[dict] = []

    # 1. session_meta 行（首行，永远存在）
    meta_payload = {
        "id": str(session_uuid),
        "timestamp": ts_str,
        "cwd": cwd,
        "originator": originator,
        "cli_version": cli_version,
        "base_instructions": None,
    }
    if model_provider is not None:
        meta_payload["model_provider"] = model_provider
    lines.append({
        "timestamp": ts_str,
        "type": "session_meta",
        "payload": meta_payload,
    })

    # 2. 一条 user_message event（codex-rs 单测里这是 "Hello from user"）
    lines.append({
        "timestamp": ts_str,
        "type": "event_msg",
        "payload": {
            "type": "user_message",
            "message": "Hello from user",
            "kind": "plain",
        },
    })

    # 3. N 条 response_item 占位（codex-rs 单测的 num_records 参数）
    for i in range(num_response_records):
        lines.append({
            "timestamp": ts_str,
            "type": "response_item",
            "payload": {
                "record_type": "response",
                "index": i,
            },
        })

    with file_path.open("w", encoding="utf-8") as f:
        for line in lines:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")

    # 同步 mtime 到 ts（codex-rs 单测最后 set_modified；让 mtime-based
    # cursor 测试稳定）
    epoch = ts.timestamp()
    os.utime(file_path, (epoch, epoch))

    return file_path


def write_sample_fixture(out_dir: Path) -> Path:
    """生成一份给 xskill Codex adapter 单测用的 fixture。

    用 deterministic 时间戳和 UUID，让单测可以 hash-stable 断言。
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    return write_session_file(
        root=out_dir,
        ts=_dt.datetime(2026, 1, 15, 10, 0, 0, tzinfo=_dt.timezone.utc),
        session_uuid=_uuid.UUID("11111111-2222-3333-4444-555555555555"),
        cwd="/tmp/codex-test-workspace",
        originator="codex-cli",
        cli_version="0.130.0",
        model_provider="openai",
        num_response_records=3,
    )


if __name__ == "__main__":
    # Usage: python codex-fixture-generator.py [output_dir]
    out = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
    path = write_sample_fixture(out)
    print(f"wrote: {path}")
    print(f"size: {path.stat().st_size} bytes")
