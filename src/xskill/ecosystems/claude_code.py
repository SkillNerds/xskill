"""
ecosystems/claude_code.py -- Claude Code 生态适配
=================================================

把蒸馏出的 Skill 装进 Claude Code 的 skill discovery 目录
（``~/.claude/skills/<name>/``），并把 CC 原生 session JSONL
（``~/.claude/projects/<cwd-hash>/<sid>.jsonl``）桥接回 xskill 的标准
``traj_*.md`` 格式。

本模块含 CC 平台的「读」（``_adapt_claude_code_jsonl`` + ``ingest_claude_code_sessions``
+ ``CCSessionIngester``）与「写」（``install_to_claude_code`` /
``install_all_to_claude_code``）。CC 专属：``CCSessionIngester`` 在 bridge 之外
额外做灰度翻牌 + ``<!-- xskill:skill=... -->`` header 注入。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import stat
import threading
import time
from collections import deque
from pathlib import Path
from typing import Callable, Iterable, Optional

from xskill.ecosystems._shared import (
    EcosystemSpec,
    JsonlIngester,
    _install_all_with,
    _install_skill_into,
    _sanitize_for_filename,
    _scan_seen_sessions,
)
from xskill.ecosystems.installation import (
    copy_install_is_current,
    link_install_metadata_is_current,
)
from xskill.ecosystems._history import (
    InstallDecisionContext,
    InstallHistoryFileSignature,
    InstallPlan,
)

logger = logging.getLogger("xskill.ecosystems")
SOURCE_DIRECTORY_RESCAN_INTERVAL_SECONDS = 30.0
SOURCE_DIRECTORY_STAT_BUDGET = 64
SOURCE_ROOT_SCAN_BUDGET = 256
SOURCE_FILE_SCAN_BUDGET = 512
SOURCE_FILE_SCAN_QUANTUM = 64


# ─────────────────────────────────────────────────────────────────
# Path helpers
# ─────────────────────────────────────────────────────────────────


def _cc_projects_path(home: Path) -> Path:
    """Claude Code session JSONL 根目录：``<home>/.claude/projects``。

    实际文件在 ``<this>/<cwd-hash>/<session-id>.jsonl``——CC 自己按 cwd
    hash 分目录。
    """
    return home / ".claude" / "projects"


def _cc_skills_path(home: Path) -> Path:
    """Claude Code skill discovery 根目录：``<home>/.claude/skills``。

    每个 skill 落到 ``<this>/<name>/SKILL.md``，CC 启动时扫这里。
    """
    return home / ".claude" / "skills"


# ─────────────────────────────────────────────────────────────────
# Installer
# ─────────────────────────────────────────────────────────────────


def install_to_claude_code(
    skill_path: Path | str,
    target_root: Path | str | None = None,
    side: str = "main",
) -> Path:
    """把一个 skill 装到 ``<target_root>/.claude/skills/<name>``。

    ``side='main'``  → 链接到 ``<skill_path>/`` 整目录
    ``side='staging'`` → 链接到 ``<skill_path>/../.canary/<name>/`` 整目录

    安装方式按平台能力**三阶 fallback**（详见 ``_fallback.install_dir``）：

    1. **symlink** — Linux / macOS / Windows Dev Mode 走这条。源仓更新即时
       可见；用户在 dest 改 SKILL.md 实际改的是源仓，UserEditAbsorbAgent
       能 round-trip 收编。
    2. **directory junction** — Windows 非 Dev Mode 走这条。NTFS reparse
       point，对读端表现等同 symlink，但只能在同卷建。
    3. **copy** — junction 也建不出来的极端情况（跨盘 / 非 NTFS）。**这一档
       下 xskill 更新不能 live propagate，用户手改也不会回到源仓**。模块
       日志会显式 warning。

    若 dest 已是 symlink 且指向相同 source，直接返回不动；
    若 dest 是普通文件/目录或指向其他位置的 symlink，先删后重装。
    """
    root = Path(target_root) if target_root else Path.home()
    return _install_skill_into(
        Path(skill_path),
        _cc_skills_path(root),
        side,
        ecosystem_label="claude_code",
    )


def install_all_to_claude_code(
    skill_dir: Path | str,
    target_root: Path | str | None = None,
    names: Iterable[str] | None = None,
) -> list[Path]:
    """Install every skill under ``skill_dir`` (each subdir = one skill) to
    Claude Code's discovery root. If ``names`` is given, restrict to those.
    Returns the list of destination ``SKILL.md`` paths actually written.
    """
    return _install_all_with(install_to_claude_code, skill_dir, target_root, names)


def claude_code_install_is_current(
    skill_path: Path,
    *,
    target_root: Path,
    side: str,
) -> bool:
    """目标安装是否精确指向指定 side 的当前内容。"""
    resolved_skill_path = Path(skill_path).resolve()
    if side == "main":
        source_dir = resolved_skill_path
    elif side == "staging":
        source_dir = (
            resolved_skill_path.parent
            / ".canary"
            / resolved_skill_path.name
        ).resolve()
    else:
        raise ValueError(f"side must be 'main' or 'staging', got {side!r}")
    destination = _cc_skills_path(Path(target_root)) / resolved_skill_path.name
    return (
        link_install_metadata_is_current(destination, source_dir)
        or copy_install_is_current(source_dir, destination)
    )


def ensure_claude_code_install(
    history,
    skill_path: Path,
    *,
    target_root: Path,
) -> Optional[dict]:
    """按安装历史恢复 Claude Code 目标，不在 sweep 启动时强制回 main。"""
    from xskill.canary import canary_generation

    resolved_skill_path = Path(skill_path).resolve()
    expected_generation = canary_generation(resolved_skill_path)

    def ensure_current_install(
        context: InstallDecisionContext,
        _pending_ids: tuple[str, ...],
    ) -> Optional[InstallPlan]:
        latest = context.latest
        recovery = context.recovery or {}
        current_generation = context.current_generation or ""
        if recovery.get("generation") == current_generation:
            side = recovery.get("side") or "main"
        elif (
            latest is not None
            and latest.get("generation", current_generation)
            == current_generation
        ):
            side = latest.get("side") or "main"
        else:
            side = "main"
        staging_source = (
            resolved_skill_path.parent
            / ".canary"
            / resolved_skill_path.name
            / "SKILL.md"
        )
        if side == "staging" and not staging_source.is_file():
            side = "main"
        sha = _read_head_sha(resolved_skill_path, ref=side)
        if claude_code_install_is_current(
            resolved_skill_path,
            target_root=Path(target_root),
            side=side,
        ):
            if (
                latest is not None
                and latest.get("sha") == sha
                and not recovery
            ):
                return None
        rollback_side = _installed_claude_code_side(
            resolved_skill_path,
            target_root=Path(target_root),
        )

        def apply_install() -> None:
            install_to_claude_code(
                resolved_skill_path,
                target_root=target_root,
                side=side,
            )

        def rollback_install() -> None:
            if rollback_side is None:
                raise RuntimeError("previous Claude Code target is unknown")
            install_to_claude_code(
                resolved_skill_path,
                target_root=target_root,
                side=rollback_side,
            )

        return InstallPlan(
            side=side,
            sha=sha,
            generation=current_generation,
            apply=apply_install,
            rollback=rollback_install if rollback_side is not None else None,
        )

    def read_generation() -> str:
        return canary_generation(resolved_skill_path)

    def read_installed_state() -> tuple[str, str, str]:
        return claude_code_installed_state(
            resolved_skill_path,
            target_root=Path(target_root),
        )

    def recover_install(recovery: dict) -> None:
        recover_claude_code_install(
            recovery,
            skill_path=resolved_skill_path,
            target_root=Path(target_root),
        )

    outcome = history.transact(
        skill=resolved_skill_path.name,
        target="claude_code",
        decision_ids=(f"ensure:{expected_generation}",),
        operation=ensure_current_install,
        expected_generation=expected_generation,
        generation_reader=read_generation,
        invoke_when_consumed=True,
        installed_state_reader=read_installed_state,
        recovery_operation=recover_install,
    )
    return outcome.current


def _installed_claude_code_side(
    skill_path: Path,
    *,
    target_root: Path,
) -> Optional[str]:
    for side in ("main", "staging"):
        if claude_code_install_is_current(
            skill_path,
            target_root=target_root,
            side=side,
        ):
            return side
    return None


def claude_code_installed_state(
    skill_path: Path,
    *,
    target_root: Path,
) -> tuple[str, str, str]:
    """返回已安装目标的可验证 side/SHA/generation，未知状态直接报错。"""
    from xskill.canary import canary_generation

    side = _installed_claude_code_side(
        skill_path,
        target_root=target_root,
    )
    if side is None:
        raise RuntimeError(
            f"Claude Code install side is unknown for {skill_path.name!r}"
        )
    return (
        side,
        _read_head_sha(skill_path, ref=side),
        canary_generation(skill_path),
    )


def recover_claude_code_install(
    recovery: dict,
    *,
    skill_path: Path,
    target_root: Path,
) -> None:
    """按 journal 顺序重放每次物理切换；最终状态必须等于 expected。"""
    expected = recovery["expected"]
    expected_side = expected["side"]
    if expected_side not in ("main", "staging"):
        raise RuntimeError(
            f"invalid Claude Code recovery side: {expected_side!r}"
        )
    install_sides = [
        record["side"]
        for record in recovery["records"]
        if record.get("action", "install") == "install"
        and record.get("side") in ("main", "staging")
    ]
    if not install_sides:
        install_sides.append(expected_side)
    for install_side in install_sides:
        install_to_claude_code(
            skill_path,
            target_root=target_root,
            side=install_side,
        )


# ─────────────────────────────────────────────────────────────────
# CC-specific trajectory helpers
# ─────────────────────────────────────────────────────────────────


def _session_used_skill(jsonl_path: Path, skill_name: str) -> bool:
    """扫 CC session JSONL，看模型是否真触发了 ``tool_use=Skill, input.skill==skill_name``。

    "用了 skill" 不是"CC 把 skill 列入 system prompt"——后者每个 session
    都会发生（CC 在启动时把所有装着的 skill 列进 'following skills are
    available' 段落）。真"用了"要看模型有没有发出 ``Skill`` tool 调用
    且参数指到我们关心的那个名字。这是 daemon 区分"消耗灰度配额的
    session"与"路过 session"的唯一可靠信号。
    """
    if not jsonl_path.is_file():
        return False
    content = jsonl_path.read_text(encoding="utf-8", errors="ignore")
    return _session_content_used_skill(content, skill_name)


def _session_content_used_skill(content: str, skill_name: str) -> bool:
    """判断一次已读取的 CC JSONL 快照是否调用了目标 skill。"""
    line_start = 0
    content_length = len(content)
    while line_start < content_length:
        line_end = content.find("\n", line_start)
        if line_end < 0:
            line_end = content_length
        line = content[line_start:line_end].strip()
        line_start = line_end + 1
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("type") != "assistant":
            continue
        msg = ev.get("message") or {}
        message_content = msg.get("content")
        if not isinstance(message_content, list):
            continue
        for part in message_content:
            if not isinstance(part, dict):
                continue
            if part.get("type") != "tool_use":
                continue
            if part.get("name") != "Skill":
                continue
            inp = part.get("input") or {}
            # CC 的 Skill tool 入参 schema: {"skill": "<name>", "args": ...}
            if inp.get("skill") == skill_name:
                return True
    return False


def _staging_skills_under(skill_dir: Path) -> list[str]:
    """返回 ``skill_dir/.canary/<name>/SKILL.md`` 存在的 skill 名列表。

    这才是 daemon 翻牌子翻得动的真实候选——staging 分支在 git 里有不算，必须
    canary.materialize_staging 把内容物化到 .canary/ 才能被
    ``install_to_claude_code(side='staging')`` 读到。
    """
    canary_root = skill_dir / ".canary"
    if not canary_root.is_dir():
        return []
    out = []
    for entry in sorted(canary_root.iterdir()):
        if entry.is_dir() and (entry / "SKILL.md").is_file():
            out.append(entry.name)
    return out


def _read_cwd_from_jsonl(jsonl_path: Path) -> str:
    """读 CC session JSONL 第一条带 ``cwd`` 字段的事件，返回工作目录路径。

    CC 在 user / assistant event 上都会塞 ``cwd``（=用户在 ~/.claude/...
    那个 -tmp-...-workdir hash 反推不出原路径——这是 CC 自己生成的 hash，
    我们要的是 ``cwd``）。
    """
    if not jsonl_path.is_file():
        return ""
    for line in jsonl_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        cwd = ev.get("cwd")
        if cwd:
            return cwd
    return ""


def _read_cwd_from_cc_jsonl_content(content: str) -> str:
    """CC 版 cwd 抽取的 (content) 重载——与 ``_read_cwd_from_jsonl(path)`` 同语义。"""
    line_start = 0
    content_length = len(content)
    while line_start < content_length:
        line_end = content.find("\n", line_start)
        if line_end < 0:
            line_end = content_length
        line = content[line_start:line_end].strip()
        line_start = line_end + 1
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        cwd = ev.get("cwd")
        if cwd:
            return str(cwd)
    return ""


def _cc_traj_id(jsonl_path: Path, session_id: str) -> str:
    """为 CC bridged 轨迹生成 ``traj_cc_<projectname>_<sid8>`` 形式的 ID。

    保留 ``traj_`` 前缀让 watcher 的 ``traj_*.md`` glob 仍能匹配；
    ``projectname`` 从 JSONL 的 ``cwd`` 字段取 basename，无 cwd 退化为
    ``unknown``；``sid8`` 是 session UUID 前 8 字符（碰撞概率极低）。

    例：
      cwd=/home/user/dataharness, sid=f2eb54d4-... → traj_cc_dataharness_f2eb54d4
      cwd 不存在,                  sid=abc-...       → traj_cc_unknown_abc12345
    """
    try:
        content = jsonl_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        # 保留旧版容错：源 session 可能在扫描与读取之间被清理；此时仍可
        # 用 session_id 生成稳定轨迹 ID，而不是让整轮扫描失败。
        content = ""
    return _cc_traj_id_from_content(content, session_id)


def _cc_traj_id_from_content(content: str, session_id: str) -> str:
    """从已读取快照生成 CC trajectory ID，不再次读取大 JSONL。"""
    cwd = _read_cwd_from_cc_jsonl_content(content)
    project = _sanitize_for_filename(Path(cwd).name if cwd else "", maxlen=32) or "unknown"
    sid_short = _sanitize_for_filename(session_id, maxlen=8) or "nosid"
    return f"traj_cc_{project}_{sid_short}"


def _prepend_xskill_header(
    traj_md_path: Path,
    *,
    skill: str,
    side: str,
    sha: str,
) -> bool:
    """把 ``<!-- xskill:skill=X side=Y sha=Z -->`` 注到 traj_*.md 顶部。

    watcher._score_new 通过 parse_traj_header 抽这个 marker 来决定要不要给
    这条 traj 跑 LLM ux 评分。CC native 桥过来的 traj 默认没有 header，
    ingester 在确认 session 应当被哪 side 标注后补上。
    """
    header = f"<!-- xskill:skill={skill} side={side} sha={sha} -->\n"
    text = traj_md_path.read_text(encoding="utf-8")
    if text.startswith(header):
        return False
    lines = text.splitlines(keepends=True)
    if lines and lines[0].startswith("<!-- xskill:"):
        updated_text = header + "".join(lines[1:])
    else:
        updated_text = header + text
    temporary_path = traj_md_path.with_suffix(
        f"{traj_md_path.suffix}.xskill-header.tmp"
    )
    try:
        with temporary_path.open("w", encoding="utf-8") as trajectory_file:
            trajectory_file.write(updated_text)
            trajectory_file.flush()
            os.fsync(trajectory_file.fileno())
        os.replace(temporary_path, traj_md_path)
        from xskill.ecosystems._history import fsync_directory
        fsync_directory(traj_md_path.parent)
        return True
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise


def _read_head_sha(skill_path: Path, *, ref: str) -> str:
    """读取 ref SHA；失败会记录安全上下文并中止目标切换。"""
    try:
        from xskill.skill.git import run_git
        code, out, _error = run_git(["rev-parse", ref], cwd=str(skill_path))
        if code == 0 and out:
            return out.strip()
    except Exception:
        logger.exception(
            "read skill SHA raised skill=%s ref=%s",
            skill_path.name,
            ref,
        )
        raise
    logger.error(
        "read skill SHA failed skill=%s ref=%s git_exit=%s",
        skill_path.name,
        ref,
        code,
    )
    raise RuntimeError(
        f"cannot resolve {ref!r} for skill {skill_path.name!r}"
    )


def _strict_canary_generation(skill_path: Path) -> str:
    """严格读取 main/staging refs；仅明确不存在的 staging 视为空。"""
    from dulwich.repo import Repo
    from xskill.skill.git import skill_repo_lock

    with skill_repo_lock(skill_path):
        repository = Repo(str(skill_path))
        try:
            main_reference = repository.refs[b"refs/heads/main"].decode(
                "ascii",
                errors="strict",
            )
        except KeyError as error:
            raise RuntimeError(
                f"main ref is missing for {skill_path.name!r}"
            ) from error
        try:
            staging_reference = repository.refs[
                b"refs/heads/staging"
            ].decode("ascii", errors="strict")
        except KeyError:
            staging_reference = ""
    return f"{main_reference}:{staging_reference}"


# ─────────────────────────────────────────────────────────────────
# Ecosystem spec
# ─────────────────────────────────────────────────────────────────


def _cc_session_complete(content: str) -> bool:
    """Claude Code 正常结束时最后一条事件为 ``last-prompt``。"""
    line_end = len(content)
    while line_end > 0:
        line_start = content.rfind("\n", 0, line_end) + 1
        line = content[line_start:line_end].strip()
        line_end = max(0, line_start - 1)
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return False
        return isinstance(event, dict) and event.get("type") == "last-prompt"
    return False


CC_SPEC = EcosystemSpec(
    name="claude_code",
    source_kind="jsonl",
    sessions_path=_cc_projects_path,
    sessions_glob="*/*.jsonl",  # <projects>/<cwd-hash>/<sid>.jsonl
    session_id_from_path=lambda p: p.stem,
    cwd_from_content=_read_cwd_from_cc_jsonl_content,
    adapter_format="claude_code_jsonl",
    traj_id_prefix="traj_cc_",
    skills_install_path=_cc_skills_path,
    label="claude_code",
    is_session_complete=_cc_session_complete,
)


# ─────────────────────────────────────────────────────────────────
# Trajectory adapter
# ─────────────────────────────────────────────────────────────────


def _adapt_claude_code_jsonl(content: str, metadata: dict) -> tuple[str, dict]:
    """Convert a Claude Code session JSONL (``~/.claude/projects/.../*.jsonl``) to
    markdown + metadata.

    Each line is one event. Recognised event types:

    - ``user``: ``message.content`` may be a string (real user input) or a list.
      List content is scanned for ``text`` parts (user-typed input, which CC
      often wraps as ``[{"type":"text","text":...}]``) and ``tool_result``
      parts (tool outputs returned to the model).
    - ``assistant``: ``message.content`` is a list of parts -- ``text``,
      ``tool_use``, ``thinking``.
    - Anything else (``permission-mode``, ``file-history-snapshot``,
      ``system``, ``attachment``, ``last-prompt``) is skipped.

    Produces a markdown body with ``## User`` / ``## Assistant`` / ``## Tool
    Call`` / ``## Tool Output`` sections and a metadata dict containing
    ``session_id``, ``cwd``, ``git_branch``, ``timeline`` (structured), and
    ``tool_names``.
    """
    timeline: list[dict] = []
    tool_calls: list[dict] = []
    tool_names: list[str] = []
    session_id = ""
    cwd = ""
    git_branch = ""
    model = ""           # 用户 agent 模型(批2):取 assistant message.model
    first_user_query = ""
    t = 0
    step = 0
    pending_tool_by_id: dict[str, str] = {}

    for raw_line in content.splitlines():
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        try:
            event = json.loads(raw_line)
        except json.JSONDecodeError:
            continue

        ev_type = event.get("type")
        if ev_type not in ("user", "assistant"):
            continue

        session_id = session_id or event.get("sessionId", "") or ""
        cwd = cwd or event.get("cwd", "") or ""
        git_branch = git_branch or event.get("gitBranch", "") or ""

        msg = event.get("message") or {}
        if ev_type == "assistant" and not model:
            model = msg.get("model") or ""
        msg_content = msg.get("content")

        if ev_type == "user":
            if isinstance(msg_content, str):
                if not first_user_query:
                    first_user_query = msg_content[:500]
                timeline.append({
                    "t": t, "role": "user",
                    "content": msg_content[:2000],
                })
                t += 1
            elif isinstance(msg_content, list):
                for part in msg_content:
                    if not isinstance(part, dict):
                        continue
                    ptype = part.get("type")
                    if ptype == "text":
                        # CC 常把用户键入的文本包成 list[{"type":"text"}]
                        # （而非裸字符串）；必须抽出，否则 traj 无 ## User 段、
                        # 被 validate_trajectory_source 判 no_user_intent 误杀。
                        text = (part.get("text") or "").strip()
                        if text:
                            if not first_user_query:
                                first_user_query = text[:500]
                            timeline.append({
                                "t": t, "role": "user",
                                "content": text[:2000],
                            })
                            t += 1
                    elif ptype == "tool_result":
                        tc_id = part.get("tool_use_id", "")
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
                        # Backfill the matching tool_calls entry
                        for entry in reversed(tool_calls):
                            if entry.get("_tc_id") == tc_id:
                                entry["output"] = output_text
                                entry["output_available"] = True
                                break
                        t += 1

        else:  # assistant
            if isinstance(msg_content, list):
                for part in msg_content:
                    if not isinstance(part, dict):
                        continue
                    ptype = part.get("type")
                    if ptype == "text":
                        text = (part.get("text") or "").strip()
                        if text:
                            timeline.append({
                                "t": t, "role": "assistant",
                                "content": text[:2000],
                            })
                            t += 1
                    elif ptype == "tool_use":
                        tc_id = part.get("id", "")
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
                    # `thinking` parts are intentionally skipped.

    # Strip internal _tc_id from tool_calls before returning
    for entry in tool_calls:
        entry.pop("_tc_id", None)

    # Build markdown body
    lines: list[str] = ["# Claude Code Session Trajectory", ""]
    if session_id:
        lines.append(f"**session_id**: {session_id}")
    if cwd:
        lines.append(f"**cwd**: {cwd}")
    if git_branch:
        lines.append(f"**git_branch**: {git_branch}")
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
    meta.setdefault("source", "claude_code_session_jsonl")
    meta.setdefault("category", "claude_code_session")
    if session_id:
        meta.setdefault("session_id", session_id)
    if model:
        meta.setdefault("model", model)
    if cwd:
        meta.setdefault("cwd", cwd)
    if git_branch:
        meta.setdefault("git_branch", git_branch)
    meta["timeline"] = timeline
    meta["tool_calls"] = tool_calls
    meta["tool_names"] = tool_names
    meta["total_tool_calls"] = len(tool_calls)
    meta["total_turns"] = len(timeline)
    if first_user_query:
        meta.setdefault("query", first_user_query)

    return md, meta


# ─────────────────────────────────────────────────────────────────
# Ingest — bridge CC session JSONL into xskill traj dir
# ─────────────────────────────────────────────────────────────────


def ingest_claude_code_sessions(
    target_traj_dir: Path | str,
    *,
    home_root: Path | str | None = None,
    seen_sessions: Optional[set[str]] = None,
    registry_db_path: Path | str | None = None,
    candidate_paths: Optional[Iterable[Path]] = None,
    bridged_markdown_index: Optional[dict[str, Path]] = None,
    force_rebridge_sessions: Optional[set[str]] = None,
    before_bridge: Optional[
        Callable[[Path, str, str, Optional[float]], None]
    ] = None,
    after_bridge: Optional[
        Callable[[dict, Path, str, str, Optional[float]], None]
    ] = None,
) -> list[dict]:
    """Bridge Claude Code session JSONLs into xskill's trajectory directory.

    Scans ``<home_root>/.claude/projects/*/*.jsonl`` and submits any session
    whose ``sessionId`` is not in ``seen_sessions`` as a new trajectory
    (``traj_NNNN.md`` + ``.json``) under ``target_traj_dir`` using the
    ``claude_code_jsonl`` adapter. ``seen_sessions`` is updated in place so
    repeat calls are idempotent. Returns the list of submission results
    (each augmented with ``session_id``, ``source_jsonl``, ``session_start_t``).

    本函数仅是 ``JsonlIngester(CC_SPEC).scan_and_bridge`` 的 thin wrapper，
    保留独立签名以兼容老调用方（SDK 用户 / 测试）。
    """
    return JsonlIngester(
        CC_SPEC, registry_db_path=registry_db_path
    ).scan_and_bridge(
        target_traj_dir=Path(target_traj_dir),
        home_root=Path(home_root) if home_root else None,
        seen_sessions=seen_sessions,
        candidate_paths=candidate_paths,
        bridged_markdown_index=bridged_markdown_index,
        force_rebridge_sessions=force_rebridge_sessions,
        before_bridge=before_bridge,
        after_bridge=after_bridge,
    )


class CCSessionIngester:
    """周期性把 Claude Code 会话 JSONL 桥到 xskill 的 watch 目录 + 灰度翻牌。

    服务启动时实例化一份长跑线程；它和 DirectoryWatcher 并行，但只负责
    "从 native 源拉到 xskill 这边"+ 灰度翻转。后续 meta / index / skill 生成
    都走 DirectoryWatcher 现有流水线。

    每轮 ``run_once()`` 做四件事：

    1. ``ingest_claude_code_sessions`` 把新出现的 CC JSONL 桥成 traj_*.md
       （顺手记下 session_start_t）。
    2. 扫 ``skill_dir/.canary/*/SKILL.md``，找出**当前有 staging 物化**的
       skill——这是灰度链路里 daemon 能翻牌子的真实候选。
    3. 对每条新桥的 traj：用 ``install_history.lookup(session_start_t)``
       倒查"那一刻 daemon 给这个 skill 装的是哪 side"，把
       ``<!-- xskill:skill=X side=Y sha=Z -->`` 注到 traj_*.md 顶部——
       这是 watcher._score_new 触发 LLM ux 评分的唯一门槛。
    4. **翻牌子**：对每个 staging-active 的 skill，往 history 查当前 side，
       下次装 install_to_claude_code(side=opposite) + history.record。

    设计上：
      - ``seen_sessions`` 重启可恢复：扫 target dir 的 traj_*.json 重建。
      - 周期 poll；没有用 inotify（移植性差且并发上没必要——见 install_history
        模块顶部注释）。
      - 找不到 source 目录是正常情况（用户机器上压根没装 CC），不报错。
      - 没有 ``skill_dir`` 或 ``install_history``（旧调用约定）时，**仅**做
        桥接，不注 header / 不翻牌——退化成纯 ingester。
    """

    def __init__(
        self,
        target_traj_dir: Path | str,
        *,
        home_root: Path | str | None = None,
        poll_interval: float = 10.0,
        skill_dir: Path | str | None = None,
        target_root: Path | str | None = None,
        history_path: Path | str | None = None,
        assignments_path: Path | str | None = None,
        registry_db_path: Path | str | None = None,
    ):
        from xskill.ecosystems._history import InstallHistory
        from xskill.canary import SessionAssignments

        self.target_traj_dir = Path(target_traj_dir)
        self.home_root = Path(home_root) if home_root else Path.home()
        self.poll_interval = poll_interval
        self.skill_dir = Path(skill_dir) if skill_dir else None
        self.target_root = Path(target_root) if target_root else self.home_root
        self.history: InstallHistory | None = (
            InstallHistory(history_path) if history_path else None
        )
        self.assignments: SessionAssignments | None = (
            SessionAssignments(assignments_path) if assignments_path else None
        )
        self.registry_db_path = (
            Path(registry_db_path)
            if registry_db_path is not None
            else None
        )

        self._seen: set[str] = _scan_seen_sessions(self.target_traj_dir)
        # 如果 assignments 表里已经登记过的 sid 也算"见过"——daemon 重启时
        # 不重复处理。两个来源（traj.json 元数据 / 显式 assignments）并集。
        if self.assignments is not None:
            self._seen.update(self.assignments.all_sids())
        self._receipt_intent_dir = (
            self.target_traj_dir / ".cc_receipt_intents"
        )
        self._receipt_intents = self._load_receipt_intents()
        self._sessions_missing_receipts = set(self._receipt_intents)
        self._recoverable_receipt_sessions: set[str] = set()
        self._bridged_markdown_index = JsonlIngester(
            CC_SPEC,
        ).bridged_markdown_by_session_prefix(self.target_traj_dir)
        self._source_directory_signatures: dict[
            Path, tuple[int, int, int]
        ] = {}
        self._source_directory_rescan_deadlines: dict[Path, float] = {}
        self._source_directory_order: list[Path] = []
        self._source_directory_cursor = 0
        self._source_file_signatures: dict[
            Path, tuple[int | None, int | None, int, int]
        ] = {}
        self._source_files_by_project: dict[Path, set[Path]] = {}
        self._source_file_scans: dict[Path, object] = {}
        self._source_file_scan_seen: dict[Path, set[Path]] = {}
        self._source_file_scan_queue: deque[tuple[Path, object]] = deque()
        self._force_rebridge_sessions: set[str] = set()
        self._projects_root_signature: tuple[int, int, int] | None = None
        self._projects_root_rescan_deadline: float | None = None
        self._projects_root_scan = None
        self._projects_root_scan_seen: set[Path] = set()
        self._projects_root_scan_signature: (
            tuple[int, int, int] | None
        ) = None
        self._pending_source_paths: set[Path] = set()
        self._source_scan_errors: set[tuple[str, str]] = set()
        self._history_repair_signature: (
            InstallHistoryFileSignature | None
        ) = None
        self._history_repair_record_limit = 0
        self._history_repair_initialized = False
        self._trajectory_paths_by_session: dict[str, Path] | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._stats = {
            "polls": 0, "ingested": 0, "headers_injected": 0,
            "flips": 0, "skipped_unused": 0,
            "errors": 0, "last_poll": None,
        }

    def _receipt_intent_path(self, session_id: str) -> Path:
        identity = hashlib.sha256(
            session_id.encode("utf-8")
        ).hexdigest()
        return self._receipt_intent_dir / f"{identity}.json"

    def _load_receipt_intents(self) -> dict[str, dict]:
        """只恢复显式持久化的 bridge→history 窗口，不从 seen 猜测。"""
        if not self._receipt_intent_dir.is_dir():
            return {}
        intents: dict[str, dict] = {}
        for intent_path in self._receipt_intent_dir.glob("*.json"):
            try:
                intent = json.loads(
                    intent_path.read_text(encoding="utf-8")
                )
            except (OSError, UnicodeError, json.JSONDecodeError) as error:
                raise RuntimeError(
                    f"invalid Claude Code receipt intent: {intent_path}"
                ) from error
            if (
                not isinstance(intent, dict)
                or intent.get("schema_version") != 2
                or not isinstance(intent.get("session_id"), str)
                or not intent["session_id"]
                or not isinstance(intent.get("skill"), str)
                or not intent["skill"]
                or not isinstance(intent.get("used_skill"), bool)
                or not isinstance(intent.get("generation"), str)
                or not intent["generation"]
                or intent.get("phase") not in ("prepared", "bridged")
                or not isinstance(intent.get("source_digest"), str)
                or not intent["source_digest"]
                or not isinstance(
                    intent.get("session_start_t"),
                    (int, float),
                )
            ):
                raise RuntimeError(
                    f"invalid Claude Code receipt intent: {intent_path}"
                )
            intents[intent["session_id"]] = intent
        return intents

    def _write_receipt_intent(
        self,
        source_path: Path,
        session_id: str,
        *,
        skill_name: str,
        used_skill: bool,
        generation: str,
        content: str,
        session_start_t: Optional[float],
    ) -> None:
        """在 trajectory bridge 前落 durable intent，封住 kill 窗口。"""
        if session_start_t is None:
            raise RuntimeError(
                "Claude Code receipt intent requires a session timestamp: "
                f"{source_path}"
            )
        assignment = (
            self.history.snapshot().index.lookup_at(
                session_start_t,
                skill=skill_name,
                target="claude_code",
            )
            if self.history is not None
            else None
        )
        intent = {
            "schema_version": 2,
            "session_id": session_id,
            "skill": skill_name,
            "used_skill": used_skill,
            "generation": generation,
            "phase": "prepared",
            "source_digest": hashlib.sha256(
                content.encode("utf-8")
            ).hexdigest(),
            "session_start_t": session_start_t,
            "source_jsonl": str(source_path),
            "trajectory_path": str(
                self.target_traj_dir
                / f"{_cc_traj_id_from_content(content, session_id)}.md"
            ),
            "side": (
                assignment.get("side")
                if assignment is not None
                else None
            ),
            "sha": (
                str(assignment.get("sha", ""))
                if assignment is not None
                else ""
            ),
        }
        self._persist_receipt_intent(intent)
        self._receipt_intents[session_id] = intent
        self._sessions_missing_receipts.add(session_id)
        self._recoverable_receipt_sessions.add(session_id)

    def _persist_receipt_intent(self, intent: dict) -> None:
        from xskill.ecosystems._history import fsync_directory

        self._receipt_intent_dir.mkdir(parents=True, exist_ok=True)
        session_id = intent["session_id"]
        intent_path = self._receipt_intent_path(session_id)
        temporary_path = intent_path.with_name(
            f".{intent_path.name}.{os.getpid()}.{time.time_ns()}.tmp"
        )
        try:
            with temporary_path.open("x", encoding="utf-8") as intent_file:
                json.dump(intent, intent_file, ensure_ascii=False)
                intent_file.flush()
                os.fsync(intent_file.fileno())
            os.replace(temporary_path, intent_path)
            fsync_directory(intent_path.parent)
        finally:
            temporary_path.unlink(missing_ok=True)

    def _mark_receipt_intent_bridged(
        self,
        result: dict,
        session_id: str,
    ) -> None:
        intent = self._receipt_intents.get(session_id)
        if intent is None:
            return
        bridged_intent = {
            **intent,
            "phase": "bridged",
            "trajectory_path": str(result["path"]),
        }
        self._persist_receipt_intent(bridged_intent)
        self._receipt_intents[session_id] = bridged_intent

    def _clear_receipt_intent(self, session_id: str) -> None:
        from xskill.ecosystems._history import fsync_directory

        intent_path = self._receipt_intent_path(session_id)
        try:
            intent_path.unlink()
        except FileNotFoundError:
            pass
        else:
            fsync_directory(intent_path.parent)
        self._receipt_intents.pop(session_id, None)
        self._sessions_missing_receipts.discard(session_id)
        self._recoverable_receipt_sessions.discard(session_id)

    @staticmethod
    def _receipt_decision_id(intent: dict) -> str:
        return (
            f"assignment:{intent['session_id']}:"
            f"{intent['source_digest'][:16]}"
        )

    def _cancel_stale_receipt_intents(self) -> None:
        """代次已结束/变化时写显式取消 receipt，绝不翻到新 generation。"""
        if self.history is None or self.skill_dir is None:
            return
        for session_id, intent in tuple(self._receipt_intents.items()):
            skill_path = self.skill_dir / intent["skill"]
            staging_path = (
                skill_path.parent
                / ".canary"
                / intent["skill"]
                / "SKILL.md"
            )
            current_generation = _strict_canary_generation(skill_path)
            if (
                staging_path.is_file()
                and current_generation == intent["generation"]
            ):
                continue
            decision_id = self._receipt_decision_id(intent)
            cancellation_decision_ids = (
                (decision_id, f"flip:{session_id}")
                if intent["used_skill"]
                else (decision_id,)
            )

            def record_cancellation(
                _context: InstallDecisionContext,
                pending_ids: tuple[str, ...],
                *,
                cancelled_intent=intent,
            ) -> InstallPlan:
                if not pending_ids:
                    return InstallPlan()
                record = {
                    "action": "session_receipt_cancelled",
                    "session_id": cancelled_intent["session_id"],
                    "used_skill": cancelled_intent["used_skill"],
                    "source_digest": cancelled_intent["source_digest"],
                    "generation": cancelled_intent["generation"],
                    "reason": "canary_generation_changed",
                    "decision_ids": list(pending_ids),
                }
                if (
                    cancelled_intent["phase"] == "bridged"
                    and cancelled_intent.get("side")
                    in ("main", "staging")
                ):
                    record.update({
                        "action": "session_assignment",
                        "side": cancelled_intent["side"],
                        "sha": cancelled_intent.get("sha", ""),
                        "t": cancelled_intent["session_start_t"],
                        "trajectory_path": cancelled_intent[
                            "trajectory_path"
                        ],
                        "flip_cancelled": True,
                    })
                return InstallPlan(records=[record])

            self.history.transact(
                skill=intent["skill"],
                target="claude_code",
                decision_ids=cancellation_decision_ids,
                operation=record_cancellation,
                invoke_when_consumed=True,
            )
            self._clear_receipt_intent(session_id)

    def _warn_source_scan_error(
        self,
        operation: str,
        path: Path,
        error: OSError,
    ) -> None:
        error_key = (operation, str(path))
        if error_key in self._source_scan_errors:
            return
        self._source_scan_errors.add(error_key)
        logger.warning(
            "Claude Code incremental scan failed operation=%s path=%s "
            "error_type=%s",
            operation,
            path,
            type(error).__name__,
        )

    def _clear_source_scan_error(self, operation: str, path: Path) -> None:
        self._source_scan_errors.discard((operation, str(path)))

    def _close_project_source_scan(self, project_directory: Path) -> None:
        source_scan = self._source_file_scans.pop(
            project_directory,
            None,
        )
        if source_scan is not None:
            close_scan = getattr(source_scan, "close", None)
            if close_scan is not None:
                close_scan()
        self._source_file_scan_seen.pop(project_directory, None)

    def _close_all_project_source_scans(self) -> None:
        for project_directory in tuple(self._source_file_scans):
            self._close_project_source_scan(project_directory)
        self._source_file_scan_queue.clear()

    def _forget_project_sources(self, project_directory: Path) -> None:
        source_paths = set(
            self._source_files_by_project.pop(project_directory, set())
        )
        source_paths.update(
            self._source_file_scan_seen.get(project_directory, set())
        )
        self._close_project_source_scan(project_directory)
        for source_path in source_paths:
            self._source_file_signatures.pop(source_path, None)
            self._force_rebridge_sessions.discard(source_path.stem)

    # ── lifecycle ─────────────────────────────────────────────────

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="xskill-cc-ingester",
        )
        self._thread.start()
        logger.info(
            "CCSessionIngester started "
            "(source=%s, target=%s, skill_dir=%s, interval=%.1fs, %d sessions pre-seen)",
            _cc_projects_path(self.home_root),
            self.target_traj_dir,
            self.skill_dir,
            self.poll_interval,
            len(self._seen),
        )

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=self.poll_interval + 5)
        logger.info("CCSessionIngester stopped")

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def stats(self) -> dict:
        return {**self._stats, "seen_sessions": len(self._seen),
                "running": self.is_running}

    # ── main loop ─────────────────────────────────────────────────

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception:
                self._stats["errors"] += 1
                logger.exception("CCSessionIngester scan error")
            self._stop.wait(self.poll_interval)

    def run_once(self) -> list[dict]:
        """跨进程串行一轮桥接，避免 sweep 与轻量调度器重复写同一轨迹。"""
        from xskill.ecosystems._history import exclusive_path_lock

        lock_root = (
            self.history.path.parent
            if self.history is not None
            else self.target_traj_dir
        )
        with exclusive_path_lock(lock_root / ".cc_session_ingest.lock"):
            return self._run_once_locked()

    def _run_once_locked(self) -> list[dict]:
        """单次扫描 + 桥接 + 判 used_skill + 标 side + 翻牌。

        翻牌策略（按用户要求）：**不是无脑见 session 就翻**，而是只在
        session 真正触发 ``tool_use=Skill, input.skill==<canary_skill>``
        时才算它消耗了一次灰度配额——这样的 session 才打 header、才翻牌、
        才进 ux 评分链路。其余 session 桥过来后透明跳过，不污染 A/B 分布。

        Session→side 持久化：每条 session 不管 used 与否都在
        ``session_assignments.jsonl`` 留一条 record，供 daemon 外部
        ``GET /api/v1/session/<sid>/side`` 之类的查询。
        """
        self._repair_materialized_history()
        self._cancel_stale_receipt_intents()
        self._stats["polls"] += 1
        self._stats["last_poll"] = time.time()
        staging_skills = (
            _staging_skills_under(self.skill_dir)
            if self.skill_dir is not None and self.history is not None
            else []
        )
        canary_skill = staging_skills[0] if staging_skills else None
        active_generation = None
        if canary_skill is not None:
            active_generation = _strict_canary_generation(
                self.skill_dir / canary_skill
            )
        self._recoverable_receipt_sessions = {
            session_id
            for session_id, intent in self._receipt_intents.items()
            if (
                intent["skill"] == canary_skill
                and intent["generation"] == active_generation
            )
        }
        before_bridge = None
        after_bridge = None
        if canary_skill is not None:
            def persist_receipt_intent(
                source_path: Path,
                session_id: str,
                content: str,
                session_start_t: Optional[float],
            ) -> None:
                self._write_receipt_intent(
                    source_path,
                    session_id,
                    skill_name=canary_skill,
                    used_skill=_session_content_used_skill(
                        content,
                        canary_skill,
                    ),
                    generation=active_generation,
                    content=content,
                    session_start_t=session_start_t,
                )

            before_bridge = persist_receipt_intent

            def persist_bridge_receipt(
                result: dict,
                _source_path: Path,
                session_id: str,
                _content: str,
                _session_start_t: Optional[float],
            ) -> None:
                self._mark_receipt_intent_bridged(
                    result,
                    session_id,
                )

            after_bridge = persist_bridge_receipt
        candidate_paths = self._incremental_source_candidates()
        submitted = ingest_claude_code_sessions(
            target_traj_dir=self.target_traj_dir,
            home_root=self.home_root,
            seen_sessions=self._seen,
            registry_db_path=self.registry_db_path,
            candidate_paths=candidate_paths,
            bridged_markdown_index=self._bridged_markdown_index,
            force_rebridge_sessions=self._force_rebridge_sessions,
            before_bridge=before_bridge,
            after_bridge=after_bridge,
        )
        if submitted:
            self._stats["ingested"] += len(submitted)
            for submitted_record in submitted:
                source_path = Path(
                    submitted_record.get("source_jsonl", "")
                )
                self._pending_source_paths.discard(source_path)
                try:
                    source_stat = source_path.stat()
                except OSError as error:
                    self._warn_source_scan_error(
                        "stat_submitted_session",
                        source_path,
                        error,
                    )
                else:
                    self._clear_source_scan_error(
                        "stat_submitted_session",
                        source_path,
                    )
                    self._source_file_signatures[source_path] = (
                        (
                            int(source_stat.st_dev)
                            if source_stat.st_dev
                            else None
                        ),
                        (
                            int(source_stat.st_ino)
                            if source_stat.st_ino
                            else None
                        ),
                        int(source_stat.st_size),
                        int(source_stat.st_mtime_ns),
                    )
                trajectory_path = Path(submitted_record["path"])
                session_id = submitted_record.get("session_id")
                if isinstance(session_id, str):
                    if session_id in self._recoverable_receipt_sessions:
                        submitted_record["receipt_recovery"] = True
                    session_prefix = (
                        _sanitize_for_filename(session_id, maxlen=8)
                        or "nosid"
                    )
                    self._bridged_markdown_index[
                        session_prefix
                    ] = trajectory_path
            logger.info(
                "CCSessionIngester: bridged %d new CC session(s) → %s",
                len(submitted), self.target_traj_dir,
            )

        # 退化模式：没配置 skill_dir + history（如老调用）→ 只 bridge，
        # 不做灰度。
        if not (self.skill_dir and self.history and submitted):
            return submitted

        if not staging_skills:
            # 当前没 skill 处于灰度——所有 session 透明桥接即可。
            return submitted

        # v1: 一次只翻一个 skill（多 skill 并发灰度等下版本再说）
        if canary_skill is None:
            return submitted

        observations: list[dict] = []
        for rec in submitted:
            sid = rec.get("session_id")
            start_t = rec.get("session_start_t")
            jsonl_path = Path(rec.get("source_jsonl", ""))
            if not sid or start_t is None or not jsonl_path.is_file():
                continue
            intent = self._receipt_intents.get(sid)
            used = (
                intent["used_skill"]
                if intent is not None
                and intent["skill"] == canary_skill
                else _session_used_skill(jsonl_path, canary_skill)
            )
            observations.append({
                "record": rec,
                "session_id": sid,
                "session_start_t": start_t,
                "used_skill": used,
                "rebridged": (
                    bool(rec.get("rebridged"))
                    and not bool(rec.get("receipt_recovery"))
                ),
                "source_digest": (
                    intent["source_digest"]
                    if intent is not None
                    and intent["skill"] == canary_skill
                    else ""
                ),
                "source_generation": (
                    intent["generation"]
                    if intent is not None
                    and intent["skill"] == canary_skill
                    else ""
                ),
            })

        try:
            assignments = self._attribute_and_rotate(
                canary_skill,
                observations,
            )
        except Exception:
            # Bridge 已把 sid 放进内存 seen；事务若在 append 前/中失败，下一轮
            # 必须重新进入同一 decision，才能用 journal 收敛，不能被 seen 永久跳过。
            for observation in observations:
                self._seen.discard(observation["session_id"])
                source_record = observation.get("record")
                if isinstance(source_record, dict):
                    source_path = Path(
                        source_record.get("source_jsonl", "")
                    )
                    if source_path.is_file():
                        self._pending_source_paths.add(source_path)
            raise
        for observation in observations:
            rec = observation["record"]
            sid = observation["session_id"]
            start_t = observation["session_start_t"]
            used = observation["used_skill"]
            entry = assignments.get(sid)
            if entry is None:
                continue
            side = entry["side"]
            sha = entry.get("sha", "")

            # 持久化 session→side 映射（不管 used 与否——查询需要）
            if self.assignments is not None:
                self.assignments.record(
                    sid=sid, side=side, sha=sha, used_skill=used, t=start_t,
                )

            if not used:
                # 模型这条 session 根本没 invoke 我们关心的 skill；不打
                # header、不翻牌、不评分。透明放过。
                self._stats["skipped_unused"] += 1
                rec["xskill_used_skill"] = False
                continue

            # 真用了 → 打 header 让 watcher._score_new 触发 ux 评分员
            traj_md = Path(rec["path"])
            _prepend_xskill_header(traj_md, skill=canary_skill, side=side, sha=sha)
            rec["xskill_used_skill"] = True
            rec["xskill_side"] = side
            rec["xskill_skill"] = canary_skill
            self._stats["headers_injected"] += 1
        for observation in observations:
            session_id = observation["session_id"]
            if session_id in assignments:
                intent = self._receipt_intents.get(session_id)
                if (
                    intent is not None
                    and intent["skill"] == canary_skill
                    and intent["phase"] == "bridged"
                    and intent["used_skill"]
                    == observation["used_skill"]
                ):
                    self._clear_receipt_intent(session_id)
                self._force_rebridge_sessions.discard(session_id)
        if self._receipt_intents:
            consumed = self.history.index().consumed(
                canary_skill,
                "claude_code",
            )
            for observation in observations:
                session_id = observation["session_id"]
                intent = self._receipt_intents.get(session_id)
                if (
                    intent is not None
                    and self._receipt_decision_id(intent) in consumed
                ):
                    self._clear_receipt_intent(session_id)
                    self._force_rebridge_sessions.discard(session_id)
        return submitted

    def _incremental_source_candidates(self) -> tuple[Path, ...]:
        """增量枚举目录项，并持续轮询尚未完成的少量活跃 session。

        目录时间戳只作快速提示；每次变化后追加一次即时校验，并每 30 秒
        扫描一次文件名，覆盖 overlayfs 同一时钟粒度内的签名碰撞。周期扫描
        不打开或解析历史 JSONL，稳定轮询只 stat 目录和 active set。
        """
        projects_root = _cc_projects_path(self.home_root)
        try:
            root_stat = projects_root.stat()
        except FileNotFoundError:
            if self._projects_root_scan is not None:
                self._projects_root_scan.close()
                self._projects_root_scan = None
            self._projects_root_scan_seen.clear()
            self._projects_root_scan_signature = None
            self._close_all_project_source_scans()
            self._source_directory_signatures.clear()
            self._source_directory_rescan_deadlines.clear()
            self._source_directory_order.clear()
            self._source_directory_cursor = 0
            self._source_file_signatures.clear()
            self._source_files_by_project.clear()
            self._force_rebridge_sessions.clear()
            self._pending_source_paths.clear()
            self._projects_root_signature = None
            self._projects_root_rescan_deadline = None
            return ()
        except OSError as error:
            self._warn_source_scan_error("stat_root", projects_root, error)
            return tuple(sorted(self._pending_source_paths))
        self._clear_source_scan_error("stat_root", projects_root)
        if not stat.S_ISDIR(root_stat.st_mode):
            if self._projects_root_scan is not None:
                self._projects_root_scan.close()
                self._projects_root_scan = None
            self._projects_root_scan_seen.clear()
            self._projects_root_scan_signature = None
            self._close_all_project_source_scans()
            self._source_directory_signatures.clear()
            self._source_directory_rescan_deadlines.clear()
            self._source_directory_order.clear()
            self._source_directory_cursor = 0
            self._source_file_signatures.clear()
            self._source_files_by_project.clear()
            self._force_rebridge_sessions.clear()
            self._pending_source_paths.clear()
            self._projects_root_signature = None
            self._projects_root_rescan_deadline = None
            return ()
        scan_time = time.monotonic()
        root_signature = (
            root_stat.st_mtime_ns,
            root_stat.st_ctime_ns,
            root_stat.st_size,
        )
        root_changed = (
            self._projects_root_signature != root_signature
        )
        root_rescan_due = (
            self._projects_root_rescan_deadline is None
            or scan_time >= self._projects_root_rescan_deadline
        )
        if (
            (root_changed or root_rescan_due)
            and self._projects_root_scan is None
        ):
            try:
                self._projects_root_scan = os.scandir(projects_root)
            except OSError as error:
                self._warn_source_scan_error(
                    "list_projects",
                    projects_root,
                    error,
                )
            else:
                self._clear_source_scan_error(
                    "list_projects",
                    projects_root,
                )
                self._projects_root_scan_seen = set()
                self._projects_root_scan_signature = root_signature
        if self._projects_root_scan is not None:
            root_scan_complete = False
            try:
                for _entry_index in range(SOURCE_ROOT_SCAN_BUDGET):
                    try:
                        directory_entry = next(self._projects_root_scan)
                    except StopIteration:
                        root_scan_complete = True
                        break
                    if not directory_entry.is_dir():
                        continue
                    project_directory = Path(directory_entry.path)
                    self._projects_root_scan_seen.add(project_directory)
                    if (
                        project_directory
                        not in self._source_directory_signatures
                    ):
                        self._source_directory_signatures[
                            project_directory
                        ] = (-1, -1, -1)
                        self._source_directory_order.append(
                            project_directory
                        )
            except OSError as error:
                self._warn_source_scan_error(
                    "list_projects",
                    projects_root,
                    error,
                )
                self._projects_root_scan.close()
                self._projects_root_scan = None
                self._projects_root_scan_seen.clear()
                self._projects_root_scan_signature = None
            if root_scan_complete:
                self._projects_root_scan.close()
                self._projects_root_scan = None
                removed_directories = (
                    set(self._source_directory_signatures)
                    - self._projects_root_scan_seen
                )
                for removed_directory in removed_directories:
                    self._source_directory_signatures.pop(
                        removed_directory,
                        None,
                    )
                    self._source_directory_rescan_deadlines.pop(
                        removed_directory,
                        None,
                    )
                    self._forget_project_sources(removed_directory)
                if (
                    len(self._source_directory_order)
                    > 2 * max(1, len(self._source_directory_signatures))
                ):
                    self._source_directory_order = [
                        project_directory
                        for project_directory
                        in self._source_directory_order
                        if project_directory
                        in self._source_directory_signatures
                    ]
                if self._source_directory_order:
                    self._source_directory_cursor %= len(
                        self._source_directory_order
                    )
                else:
                    self._source_directory_cursor = 0
                self._projects_root_signature = (
                    self._projects_root_scan_signature
                )
                self._projects_root_rescan_deadline = (
                    scan_time
                    + SOURCE_DIRECTORY_RESCAN_INTERVAL_SECONDS
                )
                self._projects_root_scan_seen.clear()
                self._projects_root_scan_signature = None
        directory_count = len(self._source_directory_order)
        selected_directories: list[Path] = []
        selected_directory_set: set[Path] = set()
        active_scan_slots = min(
            len(self._source_file_scan_queue),
            SOURCE_DIRECTORY_STAT_BUDGET,
            max(
                1,
                SOURCE_FILE_SCAN_BUDGET
                // (2 * SOURCE_FILE_SCAN_QUANTUM),
            ),
        )
        for _queue_index in range(active_scan_slots):
            project_directory, queued_scan = (
                self._source_file_scan_queue.popleft()
            )
            if (
                self._source_file_scans.get(project_directory)
                is not queued_scan
            ):
                continue
            self._source_file_scan_queue.append(
                (project_directory, queued_scan)
            )
            if project_directory in selected_directory_set:
                continue
            selected_directories.append(project_directory)
            selected_directory_set.add(project_directory)
        round_robin_count = min(
            SOURCE_DIRECTORY_STAT_BUDGET,
            directory_count,
        )
        for offset in range(round_robin_count):
            if len(selected_directories) >= SOURCE_DIRECTORY_STAT_BUDGET:
                break
            project_directory = self._source_directory_order[
                (self._source_directory_cursor + offset) % directory_count
            ]
            if (
                project_directory in selected_directory_set
                or project_directory in self._source_file_scans
            ):
                continue
            selected_directories.append(project_directory)
            selected_directory_set.add(project_directory)
        if directory_count:
            self._source_directory_cursor = (
                self._source_directory_cursor
                + round_robin_count
            ) % directory_count
        remaining_file_scan_budget = SOURCE_FILE_SCAN_BUDGET
        for project_directory in selected_directories:
            previous_signature = self._source_directory_signatures.get(
                project_directory
            )
            if previous_signature is None:
                continue
            try:
                directory_stat = project_directory.stat()
            except FileNotFoundError:
                self._source_directory_signatures.pop(project_directory, None)
                self._source_directory_rescan_deadlines.pop(
                    project_directory, None
                )
                self._forget_project_sources(project_directory)
                continue
            except OSError as error:
                self._warn_source_scan_error(
                    "stat_project",
                    project_directory,
                    error,
                )
                continue
            self._clear_source_scan_error("stat_project", project_directory)
            current_signature = (
                directory_stat.st_mtime_ns,
                directory_stat.st_ctime_ns,
                directory_stat.st_size,
            )
            directory_changed = current_signature != previous_signature
            directory_deadline = (
                self._source_directory_rescan_deadlines.get(
                    project_directory
                )
            )
            directory_rescan_due = (
                directory_deadline is None
                or scan_time >= directory_deadline
            )
            if (
                project_directory not in self._source_file_scans
                and not directory_changed
                and not directory_rescan_due
            ):
                continue
            if remaining_file_scan_budget <= 0:
                continue
            if project_directory not in self._source_file_scans:
                try:
                    self._source_file_scans[
                        project_directory
                    ] = os.scandir(project_directory)
                except OSError as error:
                    self._warn_source_scan_error(
                        "list_sessions",
                        project_directory,
                        error,
                    )
                    continue
                self._source_file_scan_seen[project_directory] = set()
                self._source_file_scan_queue.append((
                    project_directory,
                    self._source_file_scans[project_directory],
                ))
                self._clear_source_scan_error(
                    "list_sessions",
                    project_directory,
                )
            source_scan = self._source_file_scans[project_directory]
            scan_limit = min(
                SOURCE_FILE_SCAN_QUANTUM,
                remaining_file_scan_budget,
            )
            source_scan_complete = False
            scanned_entries = 0
            try:
                for _source_index in range(scan_limit):
                    try:
                        directory_entry = next(source_scan)
                    except StopIteration:
                        source_scan_complete = True
                        break
                    scanned_entries += 1
                    if not directory_entry.name.endswith(".jsonl"):
                        continue
                    source_path = Path(directory_entry.path)
                    self._source_file_scan_seen[
                        project_directory
                    ].add(source_path)
                    session_id = source_path.stem
                    try:
                        source_stat = source_path.stat()
                    except FileNotFoundError:
                        continue
                    except OSError as error:
                        self._warn_source_scan_error(
                            "stat_session",
                            source_path,
                            error,
                        )
                        continue
                    self._clear_source_scan_error(
                        "stat_session",
                        source_path,
                    )
                    source_signature = (
                        (
                            int(source_stat.st_dev)
                            if source_stat.st_dev
                            else None
                        ),
                        (
                            int(source_stat.st_ino)
                            if source_stat.st_ino
                            else None
                        ),
                        int(source_stat.st_size),
                        int(source_stat.st_mtime_ns),
                    )
                    previous_source_signature = (
                        self._source_file_signatures.get(source_path)
                    )
                    self._source_file_signatures[
                        source_path
                    ] = source_signature
                    if (
                        session_id not in self._seen
                        or session_id
                        in self._recoverable_receipt_sessions
                    ):
                        self._pending_source_paths.add(source_path)
                        if session_id in self._seen:
                            self._force_rebridge_sessions.add(session_id)
                    elif (
                        previous_source_signature is not None
                        and previous_source_signature != source_signature
                    ):
                        self._pending_source_paths.add(source_path)
                        self._force_rebridge_sessions.add(session_id)
            except OSError as error:
                self._warn_source_scan_error(
                    "list_sessions",
                    project_directory,
                    error,
                )
                self._close_project_source_scan(project_directory)
                continue
            remaining_file_scan_budget -= scanned_entries
            if not source_scan_complete:
                continue
            current_source_paths = self._source_file_scan_seen[
                project_directory
            ]
            for removed_source_path in (
                self._source_files_by_project.get(
                    project_directory,
                    set(),
                ) - current_source_paths
            ):
                self._source_file_signatures.pop(removed_source_path, None)
                self._force_rebridge_sessions.discard(
                    removed_source_path.stem
                )
            self._source_files_by_project[
                project_directory
            ] = set(current_source_paths)
            self._close_project_source_scan(project_directory)
            self._source_directory_signatures[
                project_directory
            ] = current_signature
            self._source_directory_rescan_deadlines[
                project_directory
            ] = (
                scan_time
                if directory_changed
                else scan_time + SOURCE_DIRECTORY_RESCAN_INTERVAL_SECONDS
            )
        retained_pending_paths: set[Path] = set()
        for source_path in self._pending_source_paths:
            try:
                pending_stat = source_path.stat()
            except FileNotFoundError:
                continue
            except OSError as error:
                self._warn_source_scan_error(
                    "stat_pending_session",
                    source_path,
                    error,
                )
                retained_pending_paths.add(source_path)
                continue
            self._clear_source_scan_error(
                "stat_pending_session",
                source_path,
            )
            if stat.S_ISREG(pending_stat.st_mode):
                retained_pending_paths.add(source_path)
        self._pending_source_paths = retained_pending_paths
        return tuple(sorted(self._pending_source_paths))

    def _current_history_signature(
        self,
    ) -> InstallHistoryFileSignature | None:
        if self.history is None:
            return None
        return self.history.current_signature()

    def _repair_materialized_history(self) -> None:
        """把 history 中已提交的 session receipt 补到查询表和轨迹 header。

        history 是真源；``session_assignments.jsonl`` 与 Markdown header 都是
        可重建物化。正常路径在事务返回后立即写，进程若恰好在两者之间退出，
        新进程首轮会在扫描新 session 前补齐。文件签名未变化时 O(1) 早退，
        不在 0.5 秒 supervisor poll 中反复全量解析历史。
        """
        if self.history is None:
            return
        signature = self._current_history_signature()
        if (
            self._history_repair_initialized
            and signature == self._history_repair_signature
        ):
            return
        snapshot = self.history.snapshot()
        snapshot_records = snapshot.index.records
        for session_id, intent in tuple(self._receipt_intents.items()):
            assignment = snapshot.index.session_assignment(
                intent["skill"],
                "claude_code",
                session_id,
            )
            if (
                assignment is not None
                and intent["phase"] == "bridged"
                and assignment.get("source_digest")
                == intent["source_digest"]
            ):
                self._clear_receipt_intent(session_id)
        can_continue = (
            self._history_repair_signature is not None
            and snapshot.signature is not None
            and self._history_repair_signature.device is not None
            and self._history_repair_signature.inode is not None
            and (
                self._history_repair_signature.device,
                self._history_repair_signature.inode,
            ) == (
                snapshot.signature.device,
                snapshot.signature.inode,
            )
            and snapshot.signature.size
            >= self._history_repair_signature.cursor
            and len(snapshot_records)
            >= self._history_repair_record_limit
        )
        starting_position = (
            self._history_repair_record_limit if can_continue else 0
        )
        assignment_batch: list[dict] = []
        for position in range(
            starting_position,
            len(snapshot_records),
        ):
            record = snapshot_records[position]
            if (
                record.get("action") != "session_assignment"
                or record.get("target") != "claude_code"
            ):
                continue
            self._materialize_assignment_record(
                record,
                assignment_batch=assignment_batch,
            )
        if self.assignments is not None:
            self.assignments.record_many(assignment_batch)
        self._history_repair_signature = snapshot.signature
        self._history_repair_record_limit = len(snapshot_records)
        self._history_repair_initialized = True

    def _materialize_assignment_record(
        self,
        record: dict,
        *,
        assignment_batch: list[dict],
    ) -> None:
        session_id = record.get("session_id")
        side = record.get("side")
        skill_name = record.get("skill")
        installed_at = record.get("t")
        if (
            not isinstance(session_id, str)
            or side not in ("main", "staging")
            or not isinstance(skill_name, str)
            or not isinstance(installed_at, (int, float))
        ):
            logger.error(
                "invalid session assignment materialization record "
                "record_id=%s",
                record.get("record_id", ""),
            )
            return
        used_skill = bool(record.get("used_skill"))
        sha = str(record.get("sha", ""))
        if self.assignments is not None:
            assignment_batch.append({
                "sid": session_id,
                "side": side,
                "sha": sha,
                "used_skill": used_skill,
                "t": float(installed_at),
            })
        if not used_skill:
            return
        trajectory_path = self._validated_trajectory_path(
            record.get("trajectory_path"),
            session_id=session_id,
        )
        if trajectory_path is None:
            logger.warning(
                "cannot materialize xskill header: trajectory missing "
                "session_id_hash=%s skill=%s",
                hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:12],
                skill_name,
            )
            return
        if _prepend_xskill_header(
            trajectory_path,
            skill=skill_name,
            side=side,
            sha=sha,
        ):
            self._stats["headers_injected"] += 1

    def _validated_trajectory_path(
        self,
        raw_path,
        *,
        session_id: str,
    ) -> Path | None:
        target_root = self.target_traj_dir.resolve()
        if isinstance(raw_path, str) and raw_path:
            candidate = Path(raw_path).resolve()
            try:
                candidate.relative_to(target_root)
            except ValueError:
                logger.error(
                    "session assignment trajectory escaped target root "
                    "session_id_hash=%s",
                    hashlib.sha256(
                        session_id.encode("utf-8")
                    ).hexdigest()[:12],
                )
            else:
                if candidate.is_file():
                    return candidate
        if self._trajectory_paths_by_session is None:
            self._trajectory_paths_by_session = {}
            for metadata_path in self.target_traj_dir.glob("traj_*.json"):
                try:
                    metadata = json.loads(
                        metadata_path.read_text(encoding="utf-8")
                    )
                except (OSError, json.JSONDecodeError):
                    continue
                stored_session_id = metadata.get("session_id")
                markdown_path = metadata_path.with_suffix(".md")
                if isinstance(stored_session_id, str) and markdown_path.is_file():
                    self._trajectory_paths_by_session[
                        stored_session_id
                    ] = markdown_path
        return self._trajectory_paths_by_session.get(session_id)

    def _attribute_and_rotate(
        self,
        skill_name: str,
        observations: list[dict],
    ) -> dict[str, dict]:
        """按 start time 归因真实 side；批尾一次切换只影响下一批 session。"""
        assert self.skill_dir is not None and self.history is not None
        if not observations:
            return {}
        skill_path = self.skill_dir / skill_name
        if not skill_path.is_dir():
            return {}
        observation_generations = {
            observation.get("source_generation")
            for observation in observations
            if observation.get("source_generation")
        }
        expected_generation = (
            next(iter(observation_generations))
            if len(observation_generations) == 1
            else _strict_canary_generation(skill_path)
        )
        decision_ids = []
        for observation in observations:
            session_id = observation["session_id"]
            source_digest = observation.get("source_digest", "")
            assignment_id = (
                f"assignment:{session_id}:{source_digest[:16]}"
                if source_digest
                else f"assignment:{session_id}"
            )
            decision_ids.append(assignment_id)
            if observation["used_skill"] and not observation["rebridged"]:
                decision_ids.append(f"flip:{session_id}")

        def prepare_batch(
            context: InstallDecisionContext,
            pending_ids: tuple[str, ...],
        ) -> InstallPlan:
            pending = set(pending_ids)
            records: list[dict] = []
            attributed: dict[str, dict] = {}
            pending_flip_ids: list[str] = []

            for observation in observations:
                session_id = observation["session_id"]
                source_digest = observation.get("source_digest", "")
                assignment_id = (
                    f"assignment:{session_id}:{source_digest[:16]}"
                    if source_digest
                    else f"assignment:{session_id}"
                )
                flip_id = f"flip:{session_id}"
                entry = context.index.session_assignment(
                    skill_name,
                    "claude_code",
                    session_id,
                )
                if entry is None:
                    entry = context.index.lookup_at(
                        observation["session_start_t"],
                        skill=skill_name,
                        target="claude_code",
                    )
                if entry is None:
                    # 该 session 早于第一条可证明的 install，不能凭 current 猜 side。
                    receipt_ids = [
                        decision_id
                        for decision_id in (assignment_id, flip_id)
                        if decision_id in pending
                    ]
                    if receipt_ids:
                        records.append({
                            "action": "session_receipt_cancelled",
                            "session_id": session_id,
                            "used_skill": observation["used_skill"],
                            "source_digest": source_digest,
                            "generation": expected_generation,
                            "reason": "no_install_at_session_start",
                            "decision_ids": receipt_ids,
                        })
                    continue
                attributed[session_id] = {
                    "side": entry["side"],
                    "sha": entry.get("sha", ""),
                }
                receipt_ids: list[str] = []
                if assignment_id in pending:
                    receipt_ids.append(assignment_id)
                if flip_id in pending:
                    receipt_ids.append(flip_id)
                    pending_flip_ids.append(flip_id)
                if receipt_ids:
                    source_record = observation.get("record")
                    trajectory_path = (
                        str(source_record.get("path", ""))
                        if isinstance(source_record, dict)
                        else ""
                    )
                    records.append({
                        "t": observation["session_start_t"],
                        "action": "session_assignment",
                        "session_id": session_id,
                        "side": entry["side"],
                        "sha": entry.get("sha", ""),
                        "used_skill": observation["used_skill"],
                        "source_digest": source_digest,
                        "generation": expected_generation,
                        "trajectory_path": trajectory_path,
                        "decision_ids": receipt_ids,
                    })

            if context.latest is None and pending_flip_ids:
                raise RuntimeError(
                    f"rotate({skill_name}) requires initialized history"
                )
            apply_install = None
            rollback_install = None
            plan_side = None
            plan_sha = context.latest.get("sha", "") if context.latest else ""
            install_decision_ids: tuple[str, ...] = ()
            if pending_flip_ids:
                original_side = context.latest["side"]
                plan_side = (
                    "staging" if original_side == "main" else "main"
                )
                if plan_side == "staging":
                    staging_md = (
                        skill_path.parent
                        / ".canary"
                        / skill_name
                        / "SKILL.md"
                    )
                    if not staging_md.is_file():
                        raise RuntimeError(
                            f"staging content unavailable for {skill_name!r}"
                        )
                plan_sha = _read_head_sha(skill_path, ref=plan_side)
                install_decision_ids = tuple(pending_flip_ids)

                def apply_target() -> None:
                    install_to_claude_code(
                        skill_path,
                        target_root=self.target_root,
                        side=plan_side,
                    )

                def rollback_target() -> None:
                    install_to_claude_code(
                        skill_path,
                        target_root=self.target_root,
                        side=original_side,
                    )

                apply_install = apply_target
                rollback_install = rollback_target
            return InstallPlan(
                side=plan_side,
                sha=plan_sha,
                generation=context.current_generation or "",
                records=records,
                install_decision_ids=install_decision_ids,
                apply=apply_install,
                rollback=rollback_install,
                value={
                    "assignments": attributed,
                    "flip_count": 1 if pending_flip_ids else 0,
                    "decision_count": len(pending_flip_ids),
                },
            )

        def read_generation() -> str:
            return _strict_canary_generation(skill_path)

        def read_installed_state() -> tuple[str, str, str]:
            return claude_code_installed_state(
                skill_path,
                target_root=self.target_root,
            )

        def recover_install(recovery: dict) -> None:
            recover_claude_code_install(
                recovery,
                skill_path=skill_path,
                target_root=self.target_root,
            )

        result = self.history.transact(
            skill=skill_name,
            target="claude_code",
            decision_ids=decision_ids,
            operation=prepare_batch,
            expected_generation=expected_generation,
            generation_reader=read_generation,
            invoke_when_consumed=True,
            installed_state_reader=read_installed_state,
            recovery_operation=recover_install,
        )
        value = result.value or {}
        applied_count = int(value.get("flip_count", 0))
        self._stats["flips"] += applied_count
        if applied_count:
            current = result.current
            logger.info(
                "CCSessionIngester: applied %d batch-boundary flip(s) "
                "%s; physical target=%s",
                applied_count,
                skill_name,
                current.get("side") if current else "?",
            )
        return value.get("assignments", {})
