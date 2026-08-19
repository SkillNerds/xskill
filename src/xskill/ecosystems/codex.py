"""
ecosystems/codex.py -- Codex CLI 生态适配
=========================================

把蒸馏出的 Skill 装进 Codex 的 user-scope skill 目录
（``~/.agents/skills/<name>/``——与 OpenCode / OpenClaw 共享），并把 Codex CLI
原生 rollout JSONL（``~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl``）桥接回
xskill 的标准 ``traj_*.md`` 格式。

本模块含 Codex 平台的「读」（``_adapt_codex_rollout_jsonl`` +
``ingest_codex_sessions``）与「写」（``install_to_codex`` /
``install_all_to_codex``）。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Iterable, Optional

from xskill.ecosystems._shared import (
    short_sid,
    EcosystemSpec,
    JsonlIngester,
    _agents_skills_path,
    _install_all_with,
    _install_skill_into,
    _sanitize_for_filename,
)

logger = logging.getLogger("xskill.ecosystems")


# ─────────────────────────────────────────────────────────────────
# Path helpers
# ─────────────────────────────────────────────────────────────────


def _codex_sessions_path(home: Path) -> Path:
    """Codex CLI rollout JSONL 根目录：``<home>/.codex/sessions``。

    实际文件落在 ``<this>/YYYY/MM/DD/rollout-<ts>-<uuid>.jsonl``——codex-rs
    `recorder.rs::precompute_log_file_info()` 按日期分桶，文件名时间戳的 `:`
    已替换为 `-` 兼容 NTFS（Windows）。

    跨平台一致：macOS / Linux / Windows 都走 ``<HOME>/.codex/...``，不走 XDG
    （codex 是"传统 ~/.<app>/" 风格，参 docs/dev-plan/adapter-research.md
    "Codex CLI > 轨迹采集"段）。
    """
    return home / ".codex" / "sessions"


# ─────────────────────────────────────────────────────────────────
# Installer
# ─────────────────────────────────────────────────────────────────


def install_to_codex(
    skill_path: Path | str,
    target_root: Path | str | None = None,
    side: str = "main",
) -> Path:
    """把一个 skill 装到 ``<target_root>/.agents/skills/<name>``——codex 的 user
    scope skill 目录。

    **重要**：路径是 ``.agents``（跨生态共享）而非 ``.codex``。codex 0.130 的
    ``core-skills/src/loader.rs:294`` 已把 ``$CODEX_HOME/skills/`` 标 ``/* Deprecated */``
    ——首选 user-scope 路径是 ``$HOME/.agents/skills/``。OpenCode 也扫这里，
    所以 codex 与 opencode 装到同一个目录，xskill 不重复写。

    其它语义（main/staging、三阶 fallback、symlink no-op、replaced-by-symlink
    备份）与 ``install_to_claude_code`` 完全一致——共享底层 ``_install_skill_into``
    实现。
    """
    root = Path(target_root) if target_root else Path.home()
    return _install_skill_into(
        Path(skill_path),
        _agents_skills_path(root),
        side,
        ecosystem_label="codex",
    )


def install_all_to_codex(
    skill_dir: Path | str,
    target_root: Path | str | None = None,
    names: Iterable[str] | None = None,
) -> list[Path]:
    """Install every skill under ``skill_dir`` (each subdir = one skill) to
    Codex's discovery root (``<target_root>/.agents/skills``). If ``names`` is
    given, restrict to those.
    """
    return _install_all_with(install_to_codex, skill_dir, target_root, names)


# ─────────────────────────────────────────────────────────────────
# Codex-specific trajectory helpers
# ─────────────────────────────────────────────────────────────────


def _codex_session_id_from_path(jsonl_path: Path) -> str:
    """从 codex rollout 文件名抽 session UUID。

    文件名形如 ``rollout-2026-01-15T10-00-00-11111111-2222-3333-4444-555555555555.jsonl``。
    timestamp 字段（前 19 个字符 = ``YYYY-MM-DDTHH-MM-SS``）后 + ``-`` + UUID。

    我们用从右起的最后 5 个 ``-`` 段拼出 UUID（标准 UUID 含 4 个 ``-``，加上文件名
    里 UUID 之前的那一个 ``-``，所以从右数倒数 ``[-5:]`` 段就是 UUID 的全部）。
    """
    stem = jsonl_path.stem  # 去掉 .jsonl
    parts = stem.split("-")
    if len(parts) >= 5:
        # 标准 UUID 5 段：xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
        return "-".join(parts[-5:])
    # 文件名不符合预期，退化为整个 stem（避免崩，让上层去重照常）
    return stem


def _read_cwd_from_codex_jsonl(jsonl_content: str) -> str:
    """从 codex rollout JSONL 字符串抽 cwd。

    codex schema：首行（且仅首行）是 ``type=session_meta`` 行，``payload.cwd``
    即用户当时的 cwd。与 CC 不同——CC 每条事件都带 ``cwd``。
    """
    for line in jsonl_content.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("type") == "session_meta":
            payload = ev.get("payload") or {}
            cwd = payload.get("cwd")
            if cwd:
                return str(cwd)
        # session_meta 不出现在首条则放弃（codex schema 保证首条就是它）
        break
    return ""


def _codex_traj_id(jsonl_path: Path, session_id: str) -> str:
    """codex bridged 轨迹 ID：``traj_codex_<projectname>_<sid>``。

    与 ``_cc_traj_id`` 同形，前缀换成 ``traj_codex_`` 让 trajectory 元数据能
    一眼区分来源。cwd 从 codex JSONL 首行抽（不是 CC 的 per-event 字段）。
    """
    content = jsonl_path.read_text(encoding="utf-8", errors="ignore") if jsonl_path.is_file() else ""
    cwd = _read_cwd_from_codex_jsonl(content)
    project = _sanitize_for_filename(Path(cwd).name if cwd else "", maxlen=32) or "unknown"
    sid_short = short_sid(session_id)
    return f"traj_codex_{project}_{sid_short}"


# ─────────────────────────────────────────────────────────────────
# Ecosystem spec
# ─────────────────────────────────────────────────────────────────

CODEX_SPEC = EcosystemSpec(
    name="codex",
    source_kind="jsonl",
    sessions_path=_codex_sessions_path,
    sessions_glob="*/*/*/rollout-*.jsonl",  # YYYY/MM/DD/rollout-<ts>-<uuid>.jsonl
    session_id_from_path=_codex_session_id_from_path,
    cwd_from_content=_read_cwd_from_codex_jsonl,
    adapter_format="codex_rollout_jsonl",
    traj_id_prefix="traj_codex_",
    skills_install_path=_agents_skills_path,
    label="codex",
)


# ─────────────────────────────────────────────────────────────────
# Trajectory adapter
# ─────────────────────────────────────────────────────────────────


def _codex_message_text(content) -> str:
    """``response_item/message`` 的 ``content`` → 纯文本。0.148 的形态是
    ``[{"type": "input_text"|"output_text", "text": ...}]``；兼容 string。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                tx = block.get("text")
                if tx:
                    parts.append(str(tx))
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(parts)
    return ""


def _adapt_codex_rollout_jsonl(content: str, metadata: dict) -> tuple[str, dict]:
    """Convert a Codex CLI rollout JSONL (``~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl``)
    to markdown + metadata.

    Codex rollout schema（来自 ``codex-rs/protocol/src/protocol.rs::RolloutItem``）：
    每行是 ``{"timestamp", "type", "payload"}`` 三件套，``type`` 是 tagged-union 标签：

    - ``session_meta`` —— 首行，``payload`` 含 ``id``/``cwd``/``originator``/
      ``cli_version``/``model_provider`` 等
    - ``event_msg`` —— 事件流。``payload.type=user_message`` 携带用户输入
    - ``response_item`` —— 模型响应（message / tool call / function output）
    - ``turn_context`` —— 每 turn 的 cwd / approval / sandbox / model
    - ``compacted`` —— 上下文压缩事件

    P2 阶段我们抽 ``session_meta``（session_id + cwd + originator + cli_version）+
    ``event_msg::user_message``（user query），其它行**透传到 timeline 但不深度解析**
    （codex 的 ``response_item`` 内部结构与 CC 不同，等 P4 再深化）。
    """
    timeline: list[dict] = []
    session_id = ""
    cwd = ""
    originator = ""
    cli_version = ""
    model_provider = ""
    first_user_query = ""
    t = 0
    response_count = 0

    for raw_line in content.splitlines():
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        try:
            event = json.loads(raw_line)
        except json.JSONDecodeError:
            continue

        ev_type = event.get("type")
        payload = event.get("payload") or {}

        if ev_type == "session_meta":
            session_id = session_id or str(payload.get("id") or "")
            cwd = cwd or str(payload.get("cwd") or "")
            originator = originator or str(payload.get("originator") or "")
            cli_version = cli_version or str(payload.get("cli_version") or "")
            mp = payload.get("model_provider")
            if mp:
                model_provider = model_provider or str(mp)
            continue

        if ev_type == "event_msg":
            sub_type = payload.get("type")
            if sub_type == "user_message":
                msg = str(payload.get("message") or "")
                if msg:
                    if not first_user_query:
                        first_user_query = msg[:500]
                    timeline.append({
                        "t": t, "role": "user",
                        "content": msg[:2000],
                    })
                    t += 1
            continue

        if ev_type == "response_item":
            response_count += 1
            # codex-cli ≥0.148 把用户输入也写成 response_item/message/role=user
            # （不再有 event_msg::user_message），并把注入上下文混在同一形态里：
            # role=developer 的 skills 说明、role=user 的 <environment_context>
            # 环境快照——只有不以 "<" 开头的 user 文本才是用户真正说的话。
            # 助手回复同形态 role=assistant / output_text。旧版（≤0.147）没有这
            # 类 role=user 的 response_item，走上面的 event_msg 分支，两种格式并存。
            if payload.get("type") == "message":
                role = payload.get("role")
                text = _codex_message_text(payload.get("content"))
                if role == "user" and text and not text.lstrip().startswith("<"):
                    if not first_user_query:
                        first_user_query = text[:500]
                    timeline.append({"t": t, "role": "user", "content": text[:2000]})
                    t += 1
                    continue
                if role == "assistant" and text:
                    timeline.append({"t": t, "role": "assistant", "content": text[:2000]})
                    t += 1
                    continue
                if role in ("user", "developer"):
                    continue  # 注入的运行时上下文 / 系统说明，不进 timeline
            # 其它 response_item（tool call / function output / 未知）：计数 + 透传占位
            timeline.append({
                "t": t, "role": "assistant",
                "content": f"[codex response_item #{payload.get('index', response_count - 1)}]",
            })
            t += 1
            continue

        # turn_context / compacted / 未来变体：透传，不深析
        timeline.append({
            "t": t, "role": "event",
            "kind": ev_type or "unknown",
        })
        t += 1

    # Build markdown
    lines: list[str] = ["# Codex Rollout Trajectory", ""]
    if session_id:
        lines.append(f"**session_id**: {session_id}")
    if cwd:
        lines.append(f"**cwd**: {cwd}")
    if originator:
        lines.append(f"**originator**: {originator}")
    if cli_version:
        lines.append(f"**cli_version**: {cli_version}")
    if model_provider:
        lines.append(f"**model_provider**: {model_provider}")
    lines.append("")
    if first_user_query:
        lines.append("## Initial Query")
        lines.append("")
        lines.append(first_user_query)
        lines.append("")

    for entry in timeline:
        role = entry["role"]
        if role == "user":
            lines.append("## User")
            lines.append("")
            lines.append(entry["content"])
            lines.append("")
        elif role == "assistant":
            lines.append("## Assistant")
            lines.append("")
            lines.append(entry["content"])
            lines.append("")
        elif role == "event":
            lines.append(f"## Event: {entry['kind']}")
            lines.append("")

    md = "\n".join(lines)

    meta = dict(metadata)
    meta.setdefault("source", "codex_rollout_jsonl")
    meta.setdefault("category", "codex_session")
    if session_id:
        meta.setdefault("session_id", session_id)
    if cwd:
        meta.setdefault("cwd", cwd)
    if originator:
        meta.setdefault("originator", originator)
    if cli_version:
        meta.setdefault("cli_version", cli_version)
    if model_provider:
        meta.setdefault("model_provider", model_provider)
    meta["timeline"] = timeline
    meta["total_turns"] = len(timeline)
    meta["response_items"] = response_count
    if first_user_query:
        meta.setdefault("query", first_user_query)

    return md, meta


# ─────────────────────────────────────────────────────────────────
# Ingest — bridge Codex rollout JSONL into xskill traj dir
# ─────────────────────────────────────────────────────────────────


def ingest_codex_sessions(
    target_traj_dir: Path | str,
    *,
    home_root: Path | str | None = None,
    seen_sessions: Optional[set[str]] = None,
) -> list[dict]:
    """Bridge Codex CLI rollout JSONLs into xskill's trajectory directory.

    Scans ``<home_root>/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`` and
    submits any session whose UUID is not in ``seen_sessions`` as a new
    trajectory under ``target_traj_dir`` using the ``codex_rollout_jsonl``
    adapter. ``seen_sessions`` is updated in place so repeat calls are
    idempotent. Returns the list of submission results (each augmented with
    ``session_id``, ``source_jsonl``, ``session_start_t``).

    与 ``ingest_claude_code_sessions`` 同形——同一 ``JsonlIngester`` 基类，
    只是 spec 不同。
    """
    return JsonlIngester(CODEX_SPEC).scan_and_bridge(
        target_traj_dir=Path(target_traj_dir),
        home_root=Path(home_root) if home_root else None,
        seen_sessions=seen_sessions,
    )
