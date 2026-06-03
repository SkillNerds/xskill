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

import json
import logging
import threading
import time
from pathlib import Path
from typing import Iterable, Optional

from xskill.ecosystems._shared import (
    EcosystemSpec,
    JsonlIngester,
    _install_all_with,
    _install_skill_into,
    _sanitize_for_filename,
    _scan_seen_sessions,
)

logger = logging.getLogger("xskill.ecosystems")


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
    for line in jsonl_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("type") != "assistant":
            continue
        msg = ev.get("message") or {}
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
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
    for line in content.splitlines():
        line = line.strip()
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
    cwd = _read_cwd_from_jsonl(jsonl_path)
    project = _sanitize_for_filename(Path(cwd).name if cwd else "", maxlen=32) or "unknown"
    sid_short = _sanitize_for_filename(session_id, maxlen=8) or "nosid"
    return f"traj_cc_{project}_{sid_short}"


def _prepend_xskill_header(traj_md_path: Path, *, skill: str, side: str, sha: str) -> None:
    """把 ``<!-- xskill:skill=X side=Y sha=Z -->`` 注到 traj_*.md 顶部。

    watcher._score_new 通过 parse_traj_header 抽这个 marker 来决定要不要给
    这条 traj 跑 LLM ux 评分。CC native 桥过来的 traj 默认没有 header，
    ingester 在确认 session 应当被哪 side 标注后补上。
    """
    text = traj_md_path.read_text(encoding="utf-8")
    header = f"<!-- xskill:skill={skill} side={side} sha={sha} -->\n"
    traj_md_path.write_text(header + text, encoding="utf-8")


def _read_head_sha(skill_path: Path, *, ref: str) -> str:
    """读 skill 子仓 ``ref`` 分支的 HEAD sha；读不到返回 ""。"""
    try:
        from xskill.skill.git import run_git
        code, out, _ = run_git(["rev-parse", ref], cwd=str(skill_path))
        if code == 0 and out:
            return out.strip()
    except Exception:
        pass
    return ""


# ─────────────────────────────────────────────────────────────────
# Ecosystem spec
# ─────────────────────────────────────────────────────────────────

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
)


# ─────────────────────────────────────────────────────────────────
# Trajectory adapter
# ─────────────────────────────────────────────────────────────────


def _adapt_claude_code_jsonl(content: str, metadata: dict) -> tuple[str, dict]:
    """Convert a Claude Code session JSONL (``~/.claude/projects/.../*.jsonl``) to
    markdown + metadata.

    Each line is one event. Recognised event types:

    - ``user``: ``message.content`` may be a string (real user input) or a list
      containing ``tool_result`` parts.
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
                    if part.get("type") == "tool_result":
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
    return JsonlIngester(CC_SPEC).scan_and_bridge(
        target_traj_dir=Path(target_traj_dir),
        home_root=Path(home_root) if home_root else None,
        seen_sessions=seen_sessions,
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

        self._seen: set[str] = _scan_seen_sessions(self.target_traj_dir)
        # 如果 assignments 表里已经登记过的 sid 也算"见过"——daemon 重启时
        # 不重复处理。两个来源（traj.json 元数据 / 显式 assignments）并集。
        if self.assignments is not None:
            self._seen.update(self.assignments.all_sids())
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._stats = {
            "polls": 0, "ingested": 0, "headers_injected": 0,
            "flips": 0, "skipped_unused": 0,
            "errors": 0, "last_poll": None,
        }

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
        """单次扫描 + 桥接 + 判 used_skill + 标 side + 翻牌。

        翻牌策略（按用户要求）：**不是无脑见 session 就翻**，而是只在
        session 真正触发 ``tool_use=Skill, input.skill==<canary_skill>``
        时才算它消耗了一次灰度配额——这样的 session 才打 header、才翻牌、
        才进 ux 评分链路。其余 session 桥过来后透明跳过，不污染 A/B 分布。

        Session→side 持久化：每条 session 不管 used 与否都在
        ``session_assignments.jsonl`` 留一条 record，供 daemon 外部
        ``GET /api/v1/session/<sid>/side`` 之类的查询。
        """
        self._stats["polls"] += 1
        self._stats["last_poll"] = time.time()
        submitted = ingest_claude_code_sessions(
            target_traj_dir=self.target_traj_dir,
            home_root=self.home_root,
            seen_sessions=self._seen,
        )
        if submitted:
            self._stats["ingested"] += len(submitted)
            logger.info(
                "CCSessionIngester: bridged %d new CC session(s) → %s",
                len(submitted), self.target_traj_dir,
            )

        # 退化模式：没配置 skill_dir + history（如老调用）→ 只 bridge，
        # 不做灰度。
        if not (self.skill_dir and self.history and submitted):
            return submitted

        staging_skills = _staging_skills_under(self.skill_dir)
        if not staging_skills:
            # 当前没 skill 处于灰度——所有 session 透明桥接即可。
            return submitted

        # v1: 一次只翻一个 skill（多 skill 并发灰度等下版本再说）
        canary_skill = staging_skills[0]

        for rec in submitted:
            sid = rec.get("session_id")
            start_t = rec.get("session_start_t")
            jsonl_path = Path(rec.get("source_jsonl", ""))
            if not sid or start_t is None or not jsonl_path.is_file():
                continue

            entry = self.history.lookup(start_t, skill=canary_skill)
            if entry is None:
                # session 早于 daemon 第一次 install——没法标 side
                continue
            side = entry.get("side") or "main"
            sha = entry.get("sha") or ""

            used = _session_used_skill(jsonl_path, canary_skill)

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

            # 翻牌：仅在 used_skill 时翻一次，让下个真用 skill 的 session
            # 拿到对面 side。每个 used session 翻一次；多个 used session
            # 在一个 poll 内被见到 → 翻多次（净 effect 视奇偶决定下次 side）。
            self._flip(canary_skill)
        return submitted

    # ── flip helper ──────────────────────────────────────────────

    def _flip(self, skill_name: str) -> None:
        """装 opposite-of-current side；记 history。"""
        assert self.skill_dir is not None and self.history is not None
        skill_path = self.skill_dir / skill_name
        if not skill_path.is_dir():
            return
        last = self.history.lookup(time.time(), skill=skill_name)
        current_side = last.get("side") if last else "staging"  # 没记录 → 默认下次装 main
        next_side = "staging" if current_side == "main" else "main"
        try:
            install_to_claude_code(
                skill_path, target_root=self.target_root, side=next_side,
            )
        except FileNotFoundError as e:
            # staging side 的内容物化文件不在了——说明灰度已结束，停止翻。
            logger.info("flip(%s, %s) skipped: %s", skill_name, next_side, e)
            return
        sha = _read_head_sha(skill_path, ref="staging" if next_side == "staging" else "main")
        self.history.record(skill=skill_name, side=next_side, sha=sha)
        self._stats["flips"] += 1
        logger.info("CCSessionIngester: flipped %s → %s (sha=%s)",
                    skill_name, next_side, sha[:8] if sha else "?")
