"""
ecosystems/openclaw.py -- OpenClaw 生态适配
===========================================

把蒸馏出的 Skill **拷贝**进 OpenClaw 的 personal-agent tier skill 目录
（``~/.agents/skills/<name>/``），并把 OpenClaw 原生 trajectory JSONL
（``~/.openclaw/agents/<agent>/sessions/<sid>.trajectory.jsonl``）桥接回
xskill 的标准 ``traj_*.md`` 格式。

本模块含 OpenClaw 平台的「读」（``_adapt_openclaw_trajectory_jsonl`` +
``ingest_openclaw_sessions``）与「写」（``install_to_openclaw`` /
``install_all_to_openclaw`` + ``make_openclaw_canary_flip_hook``）。

与其它平台不同：openclaw 走 copy 不走 symlink——openclaw skill discovery 对
resolved 路径做安全检查，symlink 会被 skip。复用 ``_fallback.install_dir``
的 ``force_mode="copy" + auto_reset=True`` 一站式完成 reverse_sync 回流保护
+ junction-safe dest 清理 + copytree + install-meta。Windows 上额外撞
directory junction 兼容性（issue #35）：``Path.is_symlink()`` 对 junction
返回 False，让 ``shutil.rmtree`` 误把 junction 当真目录走而抛 OSError；
``_fallback._reset_dest`` 用 ``_is_link_or_junction`` 判定避免这个 bug。
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Callable, Iterable, Optional

from xskill.ecosystems._fallback import install_dir
from xskill.ecosystems._shared import (
    EcosystemSpec,
    JsonlIngester,
    _agents_skills_path,
    _install_all_with,
    _read_skill_head_sha,
    _source_md_for_side,
)

logger = logging.getLogger("xskill.ecosystems")


_OPENCLAW_INSTALL_META = ".xskill-install-meta.json"


# ─────────────────────────────────────────────────────────────────
# Path helpers
# ─────────────────────────────────────────────────────────────────


def _openclaw_agents_path(home: Path) -> Path:
    """OpenClaw trajectory 根目录：``<home>/.openclaw/agents``。

    实际文件在 ``<this>/<agent-id>/sessions/<sid>.trajectory.jsonl``——OpenClaw
    把每个 agent 的 session 单独成目录，``main`` 是默认 agent id。
    同目录下还有 ``<sid>.jsonl``（runtime session）、``.jsonl.bak-*``（周期
    备份）、``.jsonl.reset.*Z``（reset 留档）、``.trajectory-path.json``（指针
    json），glob 必须用 ``*.trajectory.jsonl`` 精确锁定才不会扫错。
    """
    return home / ".openclaw" / "agents"


# ─────────────────────────────────────────────────────────────────
# Installer
# ─────────────────────────────────────────────────────────────────


def install_to_openclaw(
    skill_path: Path | str,
    target_root: Path | str | None = None,
    side: str = "main",
) -> Path:
    """把一个 skill **拷贝**到 ``<target_root>/.agents/skills/<name>``——OpenClaw
    的 personal-agent tier skill 目录。

    与 install_to_codex / install_to_opencode 不同：openclaw 走 ``shutil.copytree``
    不走 symlink。原因：openclaw skill discovery 对 personal-agent / workspace /
    extra-dir 都做 realpath 安全检查（resolved 路径必须留在 configured root
    内），xskill 源仓在 ``~/.xskill/skill/<name>/`` 跑出 ``~/.agents/skills/``
    root，symlink 会被 openclaw skip。copy dest 是真目录，过检查。

    dest 里会落一份 ``.xskill-install-meta.json``，记 ``{source_sha, side,
    installed_at}``，给 canary flip 判定和 dest→source 用户改回流检测用。

    与共享 ``~/.agents/skills/`` 的 codex / opencode symlink dest **并存不冲突**——
    openclaw dest 是真目录，三家 install 路径在同一父目录下不同 name 即可。
    """
    skill_path = Path(skill_path).resolve()
    if not skill_path.is_dir():
        raise NotADirectoryError(f"skill_path is not a directory: {skill_path}")

    _source_md_for_side(skill_path, side)  # 抛 FileNotFoundError 若 main / staging 不齐

    src_dir = (
        skill_path if side == "main"
        else (skill_path.parent / ".canary" / skill_path.name).resolve()
    )

    root = Path(target_root) if target_root else Path.home()
    skills_root = _agents_skills_path(root)
    skills_root.mkdir(parents=True, exist_ok=True)
    name = skill_path.name
    dest = skills_root / name

    # 显式 reverse_sync（dest → source）：openclaw 历史上是第一个走 copy
    # 模式的生态，单独显式跑 ``reverse_sync_openclaw_dest`` 以便 canary flip
    # 路径上的回流不依赖 ``_fallback._maybe_reverse_sync_before_overwrite``
    # 的默认 quiet_seconds（180s）—— flip 节奏远比 180s 快。``install_dir``
    # 的 auto_reset 路径里也会再尝试一次（默认 quiet 检查通常返回 False
    # no-op），是安全网不是主路径。
    if dest.is_dir() and not dest.is_symlink():
        try:
            from xskill.agents.user_edit_absorb_agent import reverse_sync_openclaw_dest
            reverse_sync_openclaw_dest(dest, skill_path)
        except Exception:
            logger.warning(
                "install_to_openclaw(%s): reverse_sync before copy failed; "
                "proceeding with overwrite",
                name, exc_info=True,
            )

    # 强制 copy + auto_reset 一站式：
    # 1. ``_maybe_reverse_sync_before_overwrite`` —— 二次回流保护（默认
    #    quiet=180s），上面已经显式跑过一次，这里通常 no-op。
    # 2. ``_reset_dest`` 用 ``_is_link_or_junction`` 判 dest 是否为 link/junction，
    #    避免 issue #35 的 ``shutil.rmtree(junction)`` 抛 OSError 死循环——
    #    例如同一 ``~/.agents/skills/<name>`` 之前被 codex/opencode install 装了
    #    junction（Windows non-DevMode），此时 openclaw 跑要清掉它换 copy 装。
    # 3. ``force_mode="copy"`` 跳过 symlink/junction 直接 copytree——openclaw
    #    discovery 对 resolved 路径做安全检查会拒收 symlink。
    # 4. 自动写新位置 install-meta（``dest.parent`` 旁）给后续 reverse_sync 用。
    install_dir(src_dir, dest, force_mode="copy", auto_reset=True)

    # 额外落 openclaw 专属老位置 meta（``dest/.xskill-install-meta.json``）：
    # 含 ``source_sha`` / ``side`` 字段，给 ``make_openclaw_canary_flip_hook``
    # 比对当前装的 side。``_fallback`` 写的新位置 meta 只跟踪 mode/source/ts，
    # 不带 sha/side。两份 meta 并存：新位置（``dest.parent``）由 ``_fallback``
    # 管理用于 reverse_sync；老位置（``dest`` 内部）保留供 canary flip 比对。
    source_sha = _read_skill_head_sha(skill_path)
    legacy_meta = {
        "source_sha": source_sha,
        "side": side,
        "installed_at": time.time(),
        "ecosystem": "openclaw",
    }
    (dest / _OPENCLAW_INSTALL_META).write_text(
        json.dumps(legacy_meta, indent=2), encoding="utf-8",
    )

    return dest / "SKILL.md"


def install_all_to_openclaw(
    skill_dir: Path | str,
    target_root: Path | str | None = None,
    names: Iterable[str] | None = None,
) -> list[Path]:
    """Install every skill under ``skill_dir`` (each subdir = one skill) to
    OpenClaw's personal-agent skill root (``<target_root>/.agents/skills``——
    与 codex / opencode 共享). If ``names`` is given, restrict to those.

    重复 install 同名 skill 是 idempotent——三个生态走同一目录，install_fallback
    会先 unlink 后重链。
    """
    return _install_all_with(install_to_openclaw, skill_dir, target_root, names)


def make_openclaw_canary_flip_hook(
    skill_dir: Path,
    target_root: Path,
    history: "InstallHistory",  # forward ref; imported lazily where called
    probability: float,
) -> Callable[[list[dict]], None]:
    """生成 JsonlIngester ``on_new_sessions`` 回调：每条新 session 跑 pick_side
    + 跟 install_history 比对，需要翻牌就调 ``install_to_openclaw`` 重 copy。

    用法：server openclaw 分支起 ingester 时传：
        ``on_new_sessions=make_openclaw_canary_flip_hook(skill_dir, target_root,
                                                         history, canary_cfg.probability)``

    只对**已有 staging 分支**的 skill 做 flip——无 staging 的 skill 没东西可
    分流，每次 pick_side 都返回 main，跟当前装的 main 一致，no-op。

    翻牌之后 ``install_history`` 记一条新 side——下一条 session 进来 lookup
    时能反查到当前装的是哪 side。
    """
    def _flip(submitted: list[dict]) -> None:
        if probability <= 0 or not skill_dir.is_dir():
            return
        from xskill.canary import has_staging, pick_side

        for rec in submitted:
            traj_id = rec.get("traj_id") or rec.get("session_id") or ""
            if not traj_id:
                continue
            for sk_path in sorted(skill_dir.iterdir()):
                if not sk_path.is_dir() or sk_path.name.startswith("."):
                    continue
                if not (sk_path / ".git").is_dir():
                    continue
                if not has_staging(sk_path):
                    continue  # 无 staging：分流没意义，永远 main
                target_side = pick_side(traj_id, sk_path.name, probability)
                # 当前装的哪 side：查 install_history 最近一条
                cur_rec = history.lookup(time.time(), skill=sk_path.name) if history else None
                cur_side = (cur_rec or {}).get("side", "main")
                if cur_side == target_side:
                    continue  # 已对齐，no-op
                try:
                    install_to_openclaw(
                        sk_path, target_root=target_root, side=target_side,
                    )
                    if history is not None:
                        history.record(skill=sk_path.name, side=target_side, sha="")
                    logger.info(
                        "openclaw canary flip: %s %s -> %s (traj=%s)",
                        sk_path.name, cur_side, target_side, traj_id[:8],
                    )
                except Exception:
                    logger.exception(
                        "openclaw canary flip failed: %s -> %s",
                        sk_path.name, target_side,
                    )
    return _flip


# ─────────────────────────────────────────────────────────────────
# OpenClaw-specific trajectory helpers
# ─────────────────────────────────────────────────────────────────


def _openclaw_session_id_from_path(jsonl_path: Path) -> str:
    """``<sid>.trajectory.jsonl`` → ``<sid>``。glob 已经保证 .trajectory. 中缀存在。"""
    return jsonl_path.name.split(".trajectory.")[0]


def _read_workspace_dir_from_openclaw_jsonl(content: str) -> str:
    """从 OpenClaw trajectory JSONL 抽 cwd 替身（workspaceDir）。

    每条 event 顶层都带 ``workspaceDir``；首行通常是 ``session.started``，
    其 ``data.workspaceDir`` 也有。两者都不存在则返回空（让上层退化为 "unknown"）。
    """
    for line in content.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        ws = ev.get("workspaceDir")
        if ws:
            return str(ws)
        data = ev.get("data") or {}
        ws = data.get("workspaceDir")
        if ws:
            return str(ws)
    return ""


# ─────────────────────────────────────────────────────────────────
# Ecosystem spec
# ─────────────────────────────────────────────────────────────────

OPENCLAW_SPEC = EcosystemSpec(
    name="openclaw",
    source_kind="jsonl",
    sessions_path=_openclaw_agents_path,
    sessions_glob="*/sessions/*.trajectory.jsonl",  # <agent>/sessions/<sid>.trajectory.jsonl
    session_id_from_path=_openclaw_session_id_from_path,
    cwd_from_content=_read_workspace_dir_from_openclaw_jsonl,
    adapter_format="openclaw_trajectory_jsonl",
    traj_id_prefix="traj_oc_",
    skills_install_path=_agents_skills_path,  # 与 codex/opencode 共用 ~/.agents/skills
    label="openclaw",
)


# ─────────────────────────────────────────────────────────────────
# Trajectory adapter
# ─────────────────────────────────────────────────────────────────


def _adapt_openclaw_trajectory_jsonl(content: str, metadata: dict) -> tuple[str, dict]:
    """Convert an OpenClaw trajectory JSONL (``~/.openclaw/agents/<agent>/sessions/<sid>.trajectory.jsonl``)
    to markdown + metadata.

    Trajectory file 是结构化时间线（事件类型：``session.started`` /
    ``trace.metadata`` / ``context.compiled`` / ``prompt.submitted`` /
    ``model.completed`` / ``model.fallback_step`` / ``trace.artifacts`` /
    ``session.ended``）。Transcript 唯一权威源是 **最后一条 ``model.completed``
    的 ``data.messagesSnapshot``** —— 每次 model.completed 都带从 session 起点到
    当前 turn 的完整 snapshot，拿最后一条等于拿完整 transcript。

    messagesSnapshot 里每条消息是 ``{role: user|assistant|custom, content: list|str,
    timestamp}``。content 是 Anthropic 风格 content blocks: ``text`` /
    ``tool_use`` / ``tool_result``。
    """
    session_id = ""
    agent_id = ""
    workspace_dir = ""
    provider = ""
    model_id = ""
    message_provider = ""
    final_status = ""
    last_snapshot: list = []
    eligible_skills: list[str] = []

    for raw_line in content.splitlines():
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        try:
            event = json.loads(raw_line)
        except json.JSONDecodeError:
            # 截断行（256 KiB 限制）容忍
            continue

        if event.get("traceSchema") != "openclaw-trajectory":
            continue

        ev_type = event.get("type")
        data = event.get("data") or {}

        if ev_type == "session.started":
            session_id = session_id or event.get("sessionId", "") or ""
            agent_id = agent_id or data.get("agentId", "") or ""
            workspace_dir = workspace_dir or event.get("workspaceDir") or data.get("workspaceDir") or ""
            provider = provider or event.get("provider", "") or ""
            model_id = model_id or event.get("modelId", "") or ""
            message_provider = message_provider or data.get("messageProvider", "") or ""

        elif ev_type == "trace.metadata":
            skills_meta = data.get("skills")
            if isinstance(skills_meta, list):
                for s in skills_meta:
                    if isinstance(s, dict):
                        nm = s.get("name") or s.get("id")
                        if nm and nm not in eligible_skills:
                            eligible_skills.append(str(nm))
                    elif isinstance(s, str):
                        if s not in eligible_skills:
                            eligible_skills.append(s)

        elif ev_type == "model.completed":
            snap = data.get("messagesSnapshot")
            if isinstance(snap, list) and snap:
                last_snapshot = snap

        elif ev_type == "trace.artifacts":
            fs = data.get("finalStatus")
            if fs:
                final_status = str(fs)

    # 从 messagesSnapshot 构 timeline
    timeline: list[dict] = []
    tool_calls: list[dict] = []
    tool_names: list[str] = []
    first_user_query = ""
    pending_tool_by_id: dict[str, str] = {}
    t = 0
    step = 0

    for msg in last_snapshot:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role") or ""
        msg_content = msg.get("content")

        # content 是 str（custom messages 常见）
        if isinstance(msg_content, str):
            if role == "user" and not first_user_query:
                first_user_query = msg_content[:500]
            timeline.append({
                "t": t, "role": role or "custom",
                "content": msg_content[:2000],
            })
            t += 1
            continue

        # content 是 list of blocks
        if isinstance(msg_content, list):
            for part in msg_content:
                if not isinstance(part, dict):
                    continue
                ptype = part.get("type")
                if ptype == "text":
                    text_part = part.get("text")
                    if text_part is None:
                        text = ""
                    elif not isinstance(text_part, str):
                        # text 可能是 dict（thinking/自定义块），跳过以修复 .strip() 调用
                        continue
                    else:
                        text = text_part.strip()
                    if not text:
                        continue
                    if role == "user" and not first_user_query:
                        first_user_query = text[:500]
                    timeline.append({
                        "t": t, "role": role or "custom",
                        "content": text[:2000],
                    })
                    t += 1
                elif ptype == "tool_use":
                    tc_id = part.get("id", "") or ""
                    tool_name = part.get("name", "unknown")
                    tool_input = part.get("input") or {}
                    if tool_name not in tool_names:
                        tool_names.append(tool_name)
                    pending_tool_by_id[tc_id] = tool_name
                    timeline.append({
                        "t": t, "role": "tool_call",
                        "tool": tool_name,
                        "input": tool_input,
                    })
                    tool_calls.append({
                        "step": step,
                        "tool": tool_name,
                        "input": tool_input,
                        "output": "",
                        "output_available": False,
                        "_tc_id": tc_id,
                    })
                    step += 1
                    t += 1
                elif ptype == "tool_result":
                    tc_id = part.get("tool_use_id", "") or ""
                    tool_name = pending_tool_by_id.get(tc_id, "unknown")
                    result_content = part.get("content")
                    if isinstance(result_content, list):
                        parts_text = []
                        for rp in result_content:
                            if isinstance(rp, dict) and rp.get("type") == "text":
                                parts_text.append(rp.get("text") or "")
                        output_text = "\n".join(parts_text)
                    else:
                        output_text = str(result_content) if result_content else ""
                    output_text = output_text[:2000]
                    timeline.append({
                        "t": t, "role": "tool_output",
                        "tool": tool_name,
                        "output": output_text,
                        "is_error": bool(part.get("is_error")),
                    })
                    for entry in reversed(tool_calls):
                        if entry.get("_tc_id") == tc_id:
                            entry["output"] = output_text
                            entry["output_available"] = True
                            break
                    t += 1

    for entry in tool_calls:
        entry.pop("_tc_id", None)

    # Build markdown body
    lines: list[str] = ["# OpenClaw Session Trajectory", ""]
    if session_id:
        lines.append(f"**session_id**: {session_id}")
    if agent_id:
        lines.append(f"**agent_id**: {agent_id}")
    if workspace_dir:
        lines.append(f"**workspace_dir**: {workspace_dir}")
    if provider:
        lines.append(f"**provider**: {provider}")
    if model_id:
        lines.append(f"**model_id**: {model_id}")
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
        elif role == "custom":
            lines.append("## System Note")
            lines.append("")
            lines.append(entry["content"])
            lines.append("")
        elif role == "tool_call":
            lines.append(f"## Tool Call: {entry['tool']}")
            lines.append("```json")
            lines.append(json.dumps(entry["input"], ensure_ascii=False)[:1000])
            lines.append("```")
            lines.append("")
        elif role == "tool_output":
            err_tag = " (error)" if entry.get("is_error") else ""
            lines.append(f"## Tool Output: {entry['tool']}{err_tag}")
            lines.append("```")
            lines.append(entry["output"])
            lines.append("```")
            lines.append("")

    md = "\n".join(lines)

    meta = dict(metadata)
    meta.setdefault("source", "openclaw_trajectory_jsonl")
    meta.setdefault("category", "openclaw_session")
    if session_id:
        meta.setdefault("session_id", session_id)
    if workspace_dir:
        meta.setdefault("cwd", workspace_dir)
        meta.setdefault("workspace_dir", workspace_dir)
    if agent_id:
        meta.setdefault("agent_id", agent_id)
    if provider:
        meta.setdefault("provider", provider)
    if model_id:
        meta.setdefault("model_id", model_id)
    if message_provider:
        meta.setdefault("message_provider", message_provider)
    if final_status:
        meta.setdefault("final_status", final_status)
    if eligible_skills:
        meta.setdefault("eligible_skills", eligible_skills)
    meta["timeline"] = timeline
    meta["tool_calls"] = tool_calls
    meta["tool_names"] = tool_names
    meta["total_tool_calls"] = len(tool_calls)
    meta["total_turns"] = len(timeline)
    if first_user_query:
        meta.setdefault("query", first_user_query)

    return md, meta


# ─────────────────────────────────────────────────────────────────
# Ingest — bridge OpenClaw trajectory JSONL into xskill traj dir
# ─────────────────────────────────────────────────────────────────


def ingest_openclaw_sessions(
    target_traj_dir: Path | str,
    *,
    home_root: Path | str | None = None,
    seen_sessions: Optional[set[str]] = None,
) -> list[dict]:
    """Bridge OpenClaw trajectory JSONLs into xskill's trajectory directory.

    Scans ``<home_root>/.openclaw/agents/<agent>/sessions/*.trajectory.jsonl``
    and submits any session whose id is not in ``seen_sessions`` as a new
    trajectory under ``target_traj_dir`` using the
    ``openclaw_trajectory_jsonl`` adapter.

    glob 显式带 ``.trajectory.`` 中缀，自动避开同目录下的 runtime ``<sid>.jsonl``、
    ``<sid>.jsonl.bak-*`` 备份和 ``<sid>.jsonl.reset.*Z`` reset 留档。
    """
    return JsonlIngester(OPENCLAW_SPEC).scan_and_bridge(
        target_traj_dir=Path(target_traj_dir),
        home_root=Path(home_root) if home_root else None,
        seen_sessions=seen_sessions,
    )
