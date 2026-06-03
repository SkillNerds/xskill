"""
ecosystems/_shared.py -- 跨平台共享件
=====================================

xskill 把蒸馏出的 Skill 装进各 AI-agent 生态（Claude Code / Codex / Cursor /
Trae / OpenClaw / OpenCode），并把这些生态的原生会话轨迹桥接回 xskill 的标准
``traj_*.md`` 格式。

本模块收集**不属于任何单一平台**的共享件：

- ``EcosystemSpec`` / ``SqliteEcosystemSpec`` —— 描述一个生态的轨迹来源 + 安装目标
- ``detect_known_ecosystems`` —— 启动时探测用户机器上装了哪些 agent 工具
- ``_install_skill_into`` —— 三阶 fallback 的 skill 安装实现（被各 ``install_to_*`` 共用）
- ``JsonlIngester`` —— spec 驱动的 JSONL 扫盘 + 桥接基类（CC / Codex / Cursor /
  OpenClaw 各平台子类化或直接复用）
- ``adapt_trajectory`` / ``submit_trajectory`` / ``generate_traj_id`` —— 轨迹格式
  适配 + 提交分发层（``adapt_trajectory`` 按 format 字符串分发到各平台 ``_adapt_*``）
- ``_agents_skills_path`` —— ``~/.agents/skills`` 跨生态共享 skill 目录
- 各类被多平台共用的 helper（``_sanitize_for_filename`` / ``_session_start_t`` 等）

只装稳定侧（``main`` 分支）。Canary / staging 是内部 A/B 机制；某个 staging
变体胜出后落 ``main``，下次 ``install_*`` 才会把它推给宿主 agent。
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Literal, Optional

from xskill.config import get_traj_dir
from xskill.ecosystems._fallback import (
    InstallMode, _is_link_or_junction, install_dir,
)

logger = logging.getLogger("xskill.ecosystems")


# ─────────────────────────────────────────────────────────────────
# Shared path helper — 跨生态共享的 skill 安装目录
# ─────────────────────────────────────────────────────────────────


def _agents_skills_path(home: Path) -> Path:
    """跨生态共享的 user-scope skill 安装目录：``<home>/.agents/skills``。

    Codex 0.130 的 ``core-skills/src/loader.rs`` 把这条路径列为 user scope 的
    **首选**（``$CODEX_HOME/skills/`` 已被标 deprecated）；OpenCode 的
    ``packages/opencode/src/skill/index.ts::discoverSkills`` 同样扫这里；
    OpenClaw 把 ``~/.agents/skills`` 列为 personal-agent tier。
    xskill 装 Codex / OpenCode / OpenClaw 都写这一处，不再 per-agent 各写一份。
    """
    return home / ".agents" / "skills"


# ─────────────────────────────────────────────────────────────────
# Ecosystem spec — JsonlIngester 的参数化形态
# ─────────────────────────────────────────────────────────────────
#
# CC / Codex 都是 JSONL append-only 形态，差异集中在：
#   1. 文件位置（`<home>/.claude/projects/*/*.jsonl` vs
#      `<home>/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`）
#   2. cwd 抽取方式（CC 在每事件上带 `cwd`；codex 只在首行 `session_meta.payload.cwd`）
#   3. adapter format name（喂给 `adapt_trajectory` 的字符串）
#
# 用一个 `EcosystemSpec` dataclass 把这些差异参数化，让 ingester 主体逻辑
# （扫盘 / 去重 / submit_trajectory / 记 metadata）跨生态共享。


@dataclass(frozen=True)
class EcosystemSpec:
    """描述一个 agent 生态的轨迹来源 + 安装目标。

    Attributes:
        name: 生态标识（``claude_code`` / ``codex`` / ``opencode``）；用于 logging
            和 `detect_known_ecosystems` 上报
        source_kind: 轨迹存储形态。P2 只支持 ``jsonl``；P3 加 ``sqlite``
        sessions_path: ``(home_root) -> Path``，返回该生态的 sessions/projects 根目录
        sessions_glob: 相对 ``sessions_path`` 的 glob，用于扫所有 session 文件
        session_id_from_path: ``(jsonl_path) -> session_id``。CC 用文件名（``stem``），
            codex 用文件名里的 uuid 段
        cwd_from_content: ``(jsonl_content) -> cwd``。CC 扫每条事件找首个 ``cwd``
            字段；codex 只读首行 ``session_meta.payload.cwd``
        adapter_format: 喂给 ``adapt_trajectory`` 的 format 字符串
        traj_id_prefix: 桥过来的 ``traj_*.md`` 文件名 ID 前缀（``traj_cc_`` /
            ``traj_codex_``）
        skills_install_path: ``(home_root) -> Path``，skill 安装目标根目录
        label: 短标签，给 logger 用
    """

    name: str
    source_kind: Literal["jsonl"]
    sessions_path: Callable[[Path], Path]
    sessions_glob: str
    session_id_from_path: Callable[[Path], str]
    cwd_from_content: Callable[[str], str]
    adapter_format: str
    traj_id_prefix: str
    skills_install_path: Callable[[Path], Path]
    label: str


@dataclass(frozen=True)
class SqliteEcosystemSpec:
    """SQLite-back 生态系统 spec（独立于 JsonlIngester 的 EcosystemSpec）。

    EcosystemSpec 是 JSONL ingester 专用 spec（含 sessions_glob 等 JSONL-only
    字段），不适合 SQLite。SqliteIngester 用本类，字段集中在 SQLite 视角：
    path_resolver 解析到 .db 文件、cursor_strategy 用 time_updated。

    ``traj_id_prefix`` —— 桥过来的 ``traj_*.md`` 文件名 ID 前缀（``traj_oc_`` /
    ``traj_ng_`` 等）。由 ingester 按 spec 派生 traj_id 而非硬编码——新增同形
    SQLite 生态（如 ngagent，opencode 的企业分支）时只换 spec 即可，避免
    ingester 里出现 ``if spec.name == "ngagent"`` 这种熵增分支。
    """

    name: str                                       # "opencode" | "ngagent" | ...
    source_kind: Literal["jsonl", "sqlite"]
    path_resolver: Callable[[Path], Path]           # (home) -> db file / dir
    cursor_strategy: Literal["mtime_offset", "sqlite_time_updated"]
    label: str                                      # adapter / metadata 标签
    traj_id_prefix: str = "traj_"                   # bridged traj_*.md filename prefix


# ─────────────────────────────────────────────────────────────────
# Ecosystem auto-detection
# ─────────────────────────────────────────────────────────────────

# Known agent tools and where each one writes its session trajectories.
# Used by ``detect_known_ecosystems`` at server startup to auto-register
# without making the user run `xskill registry add` for every ecosystem.
_KNOWN_ECOSYSTEMS: list[dict] = [
    {
        "id": "claude_code",
        # CC writes <home>/<this>/<cwd-hash>/*.jsonl
        "source_subpath": ".claude/projects",
        "bridge_subpath": ".xskill/cc_sessions",
        "source_kind": "dir",  # 目录存在即视为该生态可用
    },
    {
        "id": "codex",
        # Codex CLI 写 <home>/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl
        "source_subpath": ".codex/sessions",
        "bridge_subpath": ".xskill/codex_sessions",
        "source_kind": "dir",
    },
    {
        "id": "opencode",
        # OpenCode 走 XDG (~/.local/share/opencode/opencode.db)——SQLite
        # 文件，不是目录。detect 用 ``source_kind="file"`` 判存在。
        "source_subpath": ".local/share/opencode/opencode.db",
        "bridge_subpath": ".xskill/opencode_sessions",
        "source_kind": "file",
    },
    {
        "id": "ngagent",
        # ngagent: opencode 的企业分支，schema 与 opencode 一致，
        # 但 DB 在 ``~/.local/share/opencode/db/ngagent.db``（子目录里的
        # 独立 DB 文件，不是 opencode.db），可与 opencode 并存。
        "source_subpath": ".local/share/opencode/db/ngagent.db",
        "bridge_subpath": ".xskill/ngagent_sessions",
        "source_kind": "file",
    },
    {
        "id": "openclaw",
        # OpenClaw 写 <home>/.openclaw/agents/<agent>/sessions/<sid>.trajectory.jsonl
        "source_subpath": ".openclaw/agents",
        "bridge_subpath": ".xskill/openclaw_sessions",
        "source_kind": "dir",
    },
    {
        "id": "cursor",
        # Cursor 写 <home>/.cursor/projects/<encoded-cwd>/agent-transcripts/<sid>.jsonl
        "source_subpath": ".cursor/projects",
        "bridge_subpath": ".xskill/cursor_sessions",
        "source_kind": "dir",
    },
]


def detect_known_ecosystems(home_root: Path | str | None = None) -> list[dict]:
    """Probe the user's HOME for known agent tools and report which ones
    have something on disk. Returns a list of detection records:

        {"ecosystem": "claude_code" | "codex" | "opencode",
         "source": <abs path of native session dir or db file>,
         "bridge": <abs path of paired xskill watch dir>}

    A record only appears if the source dir/file exists (按
    ``source_kind`` 区分用 ``is_dir`` 还是 ``is_file``). The bridge dir is
    the path daemon should ``register_dir(..., ecosystem=...)`` to put
    under Registry control — it may or may not exist yet.

    设计：每 install 前 watcher 实时调本函数判 detected list（3 次
    ``Path.is_dir/is_file`` 开销可忽略）——避免启动时缓存导致用户中途
    装了 codex 后 daemon 看不到。
    """
    root = Path(home_root) if home_root else Path.home()
    found: list[dict] = []
    for spec in _KNOWN_ECOSYSTEMS:
        source = root / spec["source_subpath"]
        kind = spec.get("source_kind", "dir")
        if kind == "dir" and not source.is_dir():
            continue
        if kind == "file" and not source.is_file():
            continue
        found.append({
            "ecosystem": spec["id"],
            "source": source.resolve(),
            "bridge": (root / spec["bridge_subpath"]).resolve(),
        })
    # Trae：多路径探测（IDE workspaceStorage / ~/.trae-cn / CLI trajectories）
    from xskill.ecosystems.trae import detect_trae_record

    trae_det = detect_trae_record(root)
    if trae_det is not None:
        found.append(trae_det)
    return found


# ─────────────────────────────────────────────────────────────────
# Shared skill-install implementation
# ─────────────────────────────────────────────────────────────────


def _source_md_for_side(skill_path: Path, side: str) -> Path:
    """根据 side 选磁盘上的内容源。

    main:     ``<skill_path>/SKILL.md``                       (git@main 的工作树)
    staging:  ``<skill_path>/../.canary/<name>/SKILL.md``    (canary.materialize_staging 物化)

    两侧都不存在则抛 FileNotFoundError——daemon 翻牌子时这两个文件应当**已经**
    都准备好了；找不到说明灰度状态不一致，应该 fail-loud 而不是 silently
    fall back（CLAUDE.md "遇到问题 throw error"）。
    """
    if side == "main":
        src = skill_path / "SKILL.md"
        if not src.is_file():
            raise FileNotFoundError(f"main SKILL.md not found: {src}")
        return src
    if side == "staging":
        canary_md = skill_path.parent / ".canary" / skill_path.name / "SKILL.md"
        if not canary_md.is_file():
            raise FileNotFoundError(
                f"staging SKILL.md not found: {canary_md} "
                f"(did you forget canary.materialize_staging?)"
            )
        return canary_md
    raise ValueError(f"side must be 'main' or 'staging', got {side!r}")


def _install_skill_into(
    skill_path: Path,
    skills_root: Path,
    side: str,
    *,
    ecosystem_label: str,
) -> Path:
    """共享的 skill 安装实现：把 ``skill_path``（或其 staging 物化版）装到
    ``skills_root/<name>``，走三阶 fallback。

    被 ``install_to_claude_code`` / ``install_to_codex`` / ``install_to_opencode``
    共用——它们只在 ``skills_root`` 的解析上不同，安装语义完全一致。

    ``ecosystem_label`` 仅用于 warning log 时打"是哪个生态遇到 copy fallback"，
    便于运维定位。

    Args:
        skill_path: ``main`` 时即源 skill 目录；``staging`` 时取 ``..canary/<name>``
        skills_root: 安装目标根（``<home>/.claude/skills`` 或
            ``<home>/.agents/skills``）
        side: ``main`` / ``staging``
        ecosystem_label: ``claude_code`` / ``codex`` /... 用于日志

    Returns:
        ``<skills_root>/<name>/SKILL.md`` 路径
    """
    skill_path = Path(skill_path).resolve()
    if not skill_path.is_dir():
        raise NotADirectoryError(f"skill_path is not a directory: {skill_path}")

    # 校验源是否齐备（main: SKILL.md 必须有；staging: .canary/<name>/SKILL.md 必须有）
    _source_md_for_side(skill_path, side)

    if side == "main":
        src_dir = skill_path
    elif side == "staging":
        src_dir = (skill_path.parent / ".canary" / skill_path.name).resolve()
    else:
        raise ValueError(f"side must be 'main' or 'staging', got {side!r}")

    name = skill_path.name
    skills_root.mkdir(parents=True, exist_ok=True)
    dest = skills_root / name

    # 已有 symlink/junction 且指向正确：no-op。``_is_link_or_junction``
    # 而非 ``is_symlink`` —— pathlib 在 Windows 对 junction 返回 False
    # （issue #35 同源 bug），统一处理 link/junction 两种 reparse point。
    if _is_link_or_junction(dest):
        try:
            cur = dest.resolve(strict=False)
        except OSError:
            cur = None
        if cur == src_dir:
            return dest / "SKILL.md"
        # 指向别处的 link/junction 或断链 → ``unlink`` 删 reparse 本体
        # （不递归动 target）
        dest.unlink()
    elif dest.exists():
        # 旧 install 留下的真实目录或文件 → 删（保留备份避免误删用户手写）。
        # ``.replaced-by-symlink`` 备份保留——这是用户手写 skill 目录的保护机制，
        # 不是 boilerplate；不能直接走 ``_reset_dest`` 删掉。
        if dest.is_dir():
            backup = skills_root / f".{name}.replaced-by-symlink"
            if backup.exists():
                shutil.rmtree(backup)
            dest.rename(backup)
        else:
            dest.unlink()

    mode: InstallMode = install_dir(src_dir, dest)
    if mode == "copy":
        # copy 模式下 UserEditAbsorbAgent 失效 —— 用户改副本源仓看不到。
        logger.warning(
            "install_to_%s(%s): copy-mode install at %s — "
            "live-update / user-edit-absorb are disabled on this destination",
            ecosystem_label, name, dest,
        )
    return dest / "SKILL.md"


def _read_skill_head_sha(skill_path: Path) -> str:
    """读 skill 仓当前 HEAD 的 sha。读不到（非 git 仓 / 没有 HEAD）就返回空串——
    用于 install-meta 记录，缺失不影响 install 本身。
    """
    head = skill_path / ".git" / "HEAD"
    if not head.is_file():
        return ""
    try:
        ref = head.read_text(encoding="utf-8").strip()
        if ref.startswith("ref: "):
            ref_path = skill_path / ".git" / ref[5:]
            if ref_path.is_file():
                return ref_path.read_text(encoding="utf-8").strip()
        return ref  # detached HEAD
    except OSError:
        return ""


def _install_all_with(
    installer: Callable[..., Path],
    skill_dir: Path | str,
    target_root: Path | str | None,
    names: Iterable[str] | None,
) -> list[Path]:
    """``install_all_to_*`` 的共享实现——只是把 per-skill installer 作为参数注入。"""
    skill_dir = Path(skill_dir)
    if not skill_dir.is_dir():
        raise NotADirectoryError(f"skill_dir is not a directory: {skill_dir}")

    name_filter = set(names) if names is not None else None
    installed: list[Path] = []
    for entry in sorted(skill_dir.iterdir()):
        if not entry.is_dir():
            continue
        if name_filter is not None and entry.name not in name_filter:
            continue
        if not (entry / "SKILL.md").exists():
            continue
        installed.append(installer(entry, target_root=target_root))
    return installed


# ─────────────────────────────────────────────────────────────────
# Shared timestamp / filename helpers
# ─────────────────────────────────────────────────────────────────


def _parse_iso_to_epoch(s: str) -> Optional[float]:
    """ISO-8601 (CC JSONL 时间戳格式，例 '2026-05-11T10:05:47.962Z') → epoch float。"""
    if not s:
        return None
    from datetime import datetime, timezone
    s = s.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(s).replace(tzinfo=datetime.fromisoformat(s).tzinfo or timezone.utc).timestamp()
    except ValueError:
        return None


def _session_start_t(jsonl_path: Path) -> Optional[float]:
    """读 session JSONL 第一条带 timestamp 的事件，转 epoch float。

    queue-operation / file-history-snapshot 等元事件也带 timestamp，取第一条
    即可——daemon 关心的是"这个 session 在哪一刻开始活跃"，第一条元事件距
    用户真发出请求只差几毫秒。
    """
    if not jsonl_path.is_file():
        return None
    for line in jsonl_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        ts = ev.get("timestamp")
        if ts:
            t = _parse_iso_to_epoch(ts)
            if t is not None:
                return t
    return None


_SAFE_NAME_RE = None  # lazy init below


def _sanitize_for_filename(s: str, maxlen: int = 32) -> str:
    """把任意字符串转成"能进文件名"的版本：保留 a-z A-Z 0-9 - _ . 其余转 _ ."""
    global _SAFE_NAME_RE
    if _SAFE_NAME_RE is None:
        _SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]")
    if not s:
        return ""
    cleaned = _SAFE_NAME_RE.sub("_", s).strip("._-")
    return cleaned[:maxlen] if cleaned else ""


def _scan_seen_sessions(target_traj_dir: Path) -> set[str]:
    """重启时重建 ``seen_sessions``。

    桥接出的 ``traj_NNNN.json`` 的 metadata 里已经存了 ``session_id``（由
    ``_adapt_claude_code_jsonl`` 写入）。扫一遍 ``target_traj_dir`` 下所有
    json，把它们的 session_id 集进 set，避免 daemon 重启时把同一条 CC
    session 再桥一遍。
    """
    seen: set[str] = set()
    if not target_traj_dir.is_dir():
        return seen
    for jp in target_traj_dir.glob("traj_*.json"):
        try:
            meta = json.loads(jp.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        sid = meta.get("session_id")
        if sid:
            seen.add(sid)
    return seen


# ─────────────────────────────────────────────────────────────────
# Trajectory adaptation + submission dispatch layer
# ─────────────────────────────────────────────────────────────────
#
# ``adapt_trajectory`` 按 format 字符串分发到各平台的 ``_adapt_*`` 实现——
# 各平台的 ``_adapt_*`` 函数留在各自平台文件，这里只做分发。``markdown`` /
# ``json`` / ``raw`` 三种通用格式与具体平台无关，实现直接留在本文件。


def generate_traj_id(traj_dir: Path = None) -> str:
    """
    Auto-generate a traj ID like ``traj_0301`` based on existing files
    in *traj_dir*.  Scans for ``traj_*.md`` and picks max + 1.
    """
    traj_dir = traj_dir or get_traj_dir()
    traj_dir.mkdir(parents=True, exist_ok=True)

    existing_ids: list[int] = []
    for f in traj_dir.glob("traj_*.md"):
        m = re.match(r"traj_(\d+)", f.stem)
        if m:
            existing_ids.append(int(m.group(1)))

    next_id = max(existing_ids) + 1 if existing_ids else 1
    return f"traj_{next_id:04d}"


def adapt_trajectory(
    content: str,
    format: str,
    metadata: Optional[dict] = None,
) -> tuple[str, dict]:
    """
    Convert various input formats to the standard xskill representation.

    Supported *format* values:

    - ``markdown`` -- passthrough; content is already ``traj_*.md`` format.
    - ``json`` -- JSON object with fields like ``messages``, ``tool_calls``, etc.
      Converted to a markdown trajectory.
    - ``raw`` -- plain text; wrapped in a basic trajectory markdown template.
    - ``claude_code_jsonl`` / ``codex_rollout_jsonl`` /
      ``openclaw_trajectory_jsonl`` / ``cursor_transcripts_jsonl`` /
      ``trae_ide_session_json`` / ``trae_agent_trajectory_json`` -- 各 agent
      生态原生 session；分发到对应平台模块的 ``_adapt_*``。

    Returns ``(md_content, json_metadata)``.
    """
    # 平台 ``_adapt_*`` 延迟 import，避免 _shared <-> 平台模块循环 import。
    from xskill.ecosystems.claude_code import _adapt_claude_code_jsonl
    from xskill.ecosystems.codex import _adapt_codex_rollout_jsonl
    from xskill.ecosystems.openclaw import _adapt_openclaw_trajectory_jsonl
    from xskill.ecosystems.cursor import _adapt_cursor_transcripts_jsonl
    from xskill.ecosystems.trae import (
        _adapt_trae_agent_trajectory_json,
        _adapt_trae_ide_session_json,
    )

    metadata = metadata or {}

    if format == "markdown":
        return content, metadata

    if format == "json":
        return _adapt_json(content, metadata)

    if format == "raw":
        return _adapt_raw(content, metadata)

    if format == "claude_code_jsonl":
        return _adapt_claude_code_jsonl(content, metadata)

    if format == "codex_rollout_jsonl":
        return _adapt_codex_rollout_jsonl(content, metadata)

    if format == "openclaw_trajectory_jsonl":
        return _adapt_openclaw_trajectory_jsonl(content, metadata)

    if format == "cursor_transcripts_jsonl":
        return _adapt_cursor_transcripts_jsonl(content, metadata)

    if format == "trae_ide_session_json":
        return _adapt_trae_ide_session_json(content, metadata)

    if format == "trae_agent_trajectory_json":
        return _adapt_trae_agent_trajectory_json(content, metadata)

    raise ValueError(f"unsupported trajectory format: {format!r}")


def _adapt_json(content: str, metadata: dict) -> tuple[str, dict]:
    """Convert a JSON trajectory to markdown + metadata."""
    data = json.loads(content)

    # Merge top-level keys (except messages/tool_calls) into metadata
    meta = dict(metadata)
    for key in ("model", "instance_id", "repo", "task", "result", "exit_status"):
        if key in data and key not in meta:
            meta[key] = data[key]

    # Build markdown from messages / tool_calls
    lines: list[str] = []
    lines.append(f"# Trajectory")
    if meta.get("instance_id"):
        lines.append(f"\n**instance_id**: {meta['instance_id']}")
    if meta.get("model"):
        lines.append(f"**model**: {meta['model']}")
    lines.append("")

    messages = data.get("messages", [])
    tool_calls = data.get("tool_calls", [])

    for msg in messages:
        role = msg.get("role", "unknown")
        text = msg.get("content", "")
        lines.append(f"## {role.capitalize()}")
        lines.append("")
        if isinstance(text, str):
            lines.append(text)
        elif isinstance(text, list):
            # multi-part content
            for part in text:
                if isinstance(part, dict):
                    lines.append(part.get("text", str(part)))
                else:
                    lines.append(str(part))
        lines.append("")

    if tool_calls:
        lines.append("## Tool Calls")
        lines.append("")
        for tc in tool_calls:
            name = tc.get("name", tc.get("function", {}).get("name", "unknown"))
            args = tc.get("arguments", tc.get("function", {}).get("arguments", ""))
            lines.append(f"### {name}")
            lines.append("```")
            lines.append(args if isinstance(args, str) else json.dumps(args, ensure_ascii=False))
            lines.append("```")
            if tc.get("output"):
                lines.append(f"\n**output**:\n```\n{tc['output']}\n```")
            lines.append("")

    md_content = "\n".join(lines)
    return md_content, meta


def _adapt_raw(content: str, metadata: dict) -> tuple[str, dict]:
    """Wrap plain text in a basic trajectory markdown template."""
    lines = [
        "# Trajectory",
        "",
        "## Raw Content",
        "",
        content,
        "",
    ]
    md_content = "\n".join(lines)
    return md_content, dict(metadata)


def submit_trajectory(
    content: str,
    format: str = "markdown",
    metadata: Optional[dict] = None,
    traj_id: Optional[str] = None,
    traj_dir: Optional[Path] = None,
) -> dict:
    """
    Complete submission flow:

    1. Resolve *traj_dir* (from param or ``get_traj_dir()``).
    2. Generate *traj_id* if not provided.
    3. Adapt the input format to standard markdown + JSON metadata.
    4. Write ``traj_{id}.md`` and optionally ``traj_{id}.json``.
    5. Return ``{"traj_id": ..., "path": ..., "status": "stored"}``.
    """
    traj_dir = Path(traj_dir) if traj_dir else get_traj_dir()
    traj_dir.mkdir(parents=True, exist_ok=True)

    if not traj_id:
        traj_id = generate_traj_id(traj_dir)

    md_content, json_metadata = adapt_trajectory(content, format, metadata)

    # Write markdown
    md_path = traj_dir / f"{traj_id}.md"
    md_path.write_text(md_content, encoding="utf-8")

    # Write JSON metadata if non-empty
    if json_metadata:
        json_path = traj_dir / f"{traj_id}.json"
        json_path.write_text(
            json.dumps(json_metadata, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    return {
        "traj_id": traj_id,
        "path": str(md_path),
        "status": "stored",
    }


# ─────────────────────────────────────────────────────────────────
# JsonlIngester — spec-driven 扫盘 + 桥接
# ─────────────────────────────────────────────────────────────────


class JsonlIngester:
    """跨生态 JSONL session ingester——spec 化的扫盘 + bridge 逻辑。

    职责（**只**这些，不含 staging / flip / header 注入——那是 CC 专属的
    `CCSessionIngester` 在 wrapper 层做的事）：

    1. 扫 ``spec.sessions_path(home_root)`` 下匹配 ``spec.sessions_glob`` 的文件
    2. 用 ``spec.session_id_from_path`` 抽 session id，跟 ``seen_sessions`` 去重
    3. 用 ``spec.adapter_format`` 喂 ``submit_trajectory`` 桥成 ``traj_*.md``
    4. traj_id 取 ``<spec.traj_id_prefix><project>_<sid8>``——保留 ``traj_`` 前缀
       让 watcher 的 ``traj_*.md`` glob 继续匹配

    用法两种：

    - **One-shot**: ``ingester.scan_and_bridge(target_traj_dir, home_root=)``
      返回 record list 后由调用方处理（live test / 单测 / CLI 单跑用）
    - **Daemon thread**: ``ingester.start()`` 起后台 daemon 线程周期性
      ``_loop`` 调 ``scan_and_bridge``；``ingester.stop()`` 干净退出。
      用于 server.py startup hook 让生态 ingester 与 CC 一样常青运行。
      使用 daemon 模式时 ``target_traj_dir`` / ``home_root`` 必须在
      ``__init__`` 时传入（毕竟 thread 自己跑循环，没有调用方）。
    """

    def __init__(
        self,
        spec: EcosystemSpec,
        *,
        target_traj_dir: Path | str | None = None,
        home_root: Path | str | None = None,
        poll_interval: float = 10.0,
        on_new_sessions: Callable[[list[dict]], None] | None = None,
    ):
        if spec.source_kind != "jsonl":
            # SQLite ingester 用单独的 SqliteIngester；早 fail 避免走错路。
            raise ValueError(
                f"JsonlIngester only supports source_kind='jsonl', got {spec.source_kind!r}"
            )
        self.spec = spec
        # daemon thread 用：one-shot 调用方不传，scan_and_bridge 参数兜底。
        self.target_traj_dir = Path(target_traj_dir) if target_traj_dir else None
        self.home_root = Path(home_root) if home_root else None
        self.poll_interval = poll_interval
        # on_new_sessions: 可选 hook，每轮 scan 桥接到新 session 后调
        # 一次（参数是 submitted records）。openclaw 用这个 hook 做 canary
        # flip——发现新 session → pick_side → 跟 install_history 对比 →
        # 触发 copy-overwrite。codex / claude_code 不用（它们走 symlink，
        # 灰度由 CCSessionIngester / 老逻辑负责）。
        self.on_new_sessions = on_new_sessions
        # daemon thread 内部用：seen_sessions 持久化在 instance 上避免每轮
        # _scan_seen_sessions 重扫（thread 跑期间 traj_*.json 自己也在生成）。
        self._seen: set[str] = set()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._stats = {
            "polls": 0, "ingested": 0, "errors": 0, "last_poll": None,
        }

    # ── daemon thread lifecycle ──────────────────────────────────

    def start(self) -> None:
        """起 daemon 线程，周期性调 ``scan_and_bridge``。幂等：已在跑则
        no-op。

        要求 ``__init__`` 传入了 ``target_traj_dir``——daemon 线程没有调
        用方传参数。``home_root`` 可空（fallback ``Path.home()``）。
        """
        if self._thread and self._thread.is_alive():
            return
        if self.target_traj_dir is None:
            raise RuntimeError(
                "JsonlIngester.start() requires target_traj_dir in __init__"
            )
        # 重启场景：从磁盘上已有 traj_*.json 恢复 seen 集合，避免重复桥接。
        self._seen = _scan_seen_sessions(self.target_traj_dir)
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, daemon=True,
            name=f"xskill-{self.spec.name}-ingester",
        )
        self._thread.start()
        logger.info(
            "JsonlIngester(%s) started "
            "(source=%s, target=%s, interval=%.1fs, %d sessions pre-seen)",
            self.spec.name,
            self.spec.sessions_path(self.home_root or Path.home()),
            self.target_traj_dir,
            self.poll_interval,
            len(self._seen),
        )

    def stop(self) -> None:
        """干净停止 daemon 线程（避免 zombie）。"""
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=self.poll_interval + 5)
        logger.info("JsonlIngester(%s) stopped", self.spec.name)

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def stats(self) -> dict:
        return {**self._stats, "seen_sessions": len(self._seen),
                "running": self.is_running}

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                submitted = self.scan_and_bridge(
                    target_traj_dir=self.target_traj_dir,
                    home_root=self.home_root,
                    seen_sessions=self._seen,
                )
                self._stats["polls"] += 1
                self._stats["last_poll"] = time.time()
                if submitted:
                    self._stats["ingested"] += len(submitted)
                    logger.info(
                        "JsonlIngester(%s): bridged %d new session(s) → %s",
                        self.spec.name, len(submitted), self.target_traj_dir,
                    )
                    if self.on_new_sessions is not None:
                        try:
                            self.on_new_sessions(submitted)
                        except Exception:
                            logger.exception(
                                "JsonlIngester(%s) on_new_sessions hook failed",
                                self.spec.name,
                            )
            except Exception:
                self._stats["errors"] += 1
                logger.exception("JsonlIngester(%s) scan error", self.spec.name)
            self._stop.wait(self.poll_interval)

    def scan_and_bridge(
        self,
        target_traj_dir: Path,
        *,
        home_root: Path | None = None,
        seen_sessions: Optional[set[str]] = None,
    ) -> list[dict]:
        """单次扫盘 + 桥接。

        Args:
            target_traj_dir: ``traj_*.md`` / ``.json`` 落盘目录（xskill 的 watch dir）
            home_root: 用户 HOME；为 ``None`` 时用 ``Path.home()``。测试时用 tmp_path
                做隔离
            seen_sessions: 已处理 session id 集合；in-place 更新

        Returns:
            每条新桥接 session 的 submission 结果（含 ``session_id`` /
            ``source_jsonl`` / ``session_start_t``）
        """
        target_traj_dir.mkdir(parents=True, exist_ok=True)
        root = Path(home_root) if home_root else Path.home()
        sessions_root = self.spec.sessions_path(root)
        if not sessions_root.is_dir():
            return []

        seen = seen_sessions if seen_sessions is not None else set()
        submitted: list[dict] = []
        for jsonl_path in sorted(sessions_root.glob(self.spec.sessions_glob)):
            sid = self.spec.session_id_from_path(jsonl_path)
            if sid in seen:
                continue
            content = jsonl_path.read_text(encoding="utf-8", errors="ignore")
            if not content.strip():
                continue
            traj_id = self._make_traj_id(content, sid)
            result = submit_trajectory(
                content=content,
                format=self.spec.adapter_format,
                traj_id=traj_id,
                traj_dir=target_traj_dir,
            )
            result["session_id"] = sid
            result["source_jsonl"] = str(jsonl_path)
            result["session_start_t"] = _session_start_t(jsonl_path)
            result["ecosystem"] = self.spec.name
            submitted.append(result)
            seen.add(sid)
        return submitted

    def _make_traj_id(self, content: str, sid: str) -> str:
        """``<prefix><project>_<sid8>``——project = cwd basename，sid8 = sid 前 8 字符。"""
        cwd = self.spec.cwd_from_content(content)
        project = _sanitize_for_filename(Path(cwd).name if cwd else "", maxlen=32) or "unknown"
        sid_short = _sanitize_for_filename(sid, maxlen=8) or "nosid"
        return f"{self.spec.traj_id_prefix}{project}_{sid_short}"
