"""
ecosystems.py -- Install distilled skills into AI-agent ecosystems
==================================================================

xskill produces skills in a self-managed directory (``~/.xskill/skill/<name>/``)
that is internally a git repo with canary / staging branches. External
ecosystems (Claude Code, OpenCode, Codex, openclaw) discover skills from their
own well-known directories.

This module bridges the two: it copies the stable (``main`` branch) SKILL.md
into the ecosystem's discovery path so the host agent can load it.

Only the stable side is installed. Canary / staging trials are an internal
A/B mechanism; once a staging variant wins (``canary.merge_staging_to_main``)
it lands on ``main`` and the next ``install_*`` call ships it to the host.
"""

from __future__ import annotations

import json
import logging
import shutil
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Literal, Optional

from xskill.adapters import submit_trajectory
from xskill.install_fallback import InstallMode, install_dir

logger = logging.getLogger("xskill.ecosystems")


# ─────────────────────────────────────────────────────────────────
# Path helpers (per-ecosystem)
# ─────────────────────────────────────────────────────────────────
#
# 把"Claude Code 在 home 下哪些子路径"集中到一处。后续 P2/P3 加 Codex /
# OpenCode 时，沿用同样的 ``_<ecosystem>_<role>_path(home)`` 命名，避免
# 路径拼接散落在各 install / ingester 实现里。
#
# 这一层故意**只**做路径运算（不碰 fs / 不 mkdir），让单测可以纯函数地
# 断言路径正确性，跨平台行为更稳。


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


def _agents_skills_path(home: Path) -> Path:
    """跨生态共享的 user-scope skill 安装目录：``<home>/.agents/skills``。

    Codex 0.130 的 ``core-skills/src/loader.rs`` 把这条路径列为 user scope 的
    **首选**（``$CODEX_HOME/skills/`` 已被标 deprecated）；OpenCode 的
    ``packages/opencode/src/skill/index.ts::discoverSkills`` 同样扫这里。
    xskill 装 Codex / OpenCode 都写这一处，不再 per-agent 各写一份。
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
    return found


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

    被 ``install_to_claude_code`` / ``install_to_codex`` / 未来 ``install_to_opencode``
    共用——三者只在 ``skills_root`` 的解析上不同，安装语义完全一致。

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

    # 已有 symlink 且指向正确：no-op
    if dest.is_symlink():
        try:
            cur = dest.resolve(strict=False)
        except OSError:
            cur = None
        if cur == src_dir:
            return dest / "SKILL.md"
        # 指向别处的 symlink 或断链 → 删
        dest.unlink()
    elif dest.exists():
        # 旧 install 留下的真实目录或文件 → 删（保留备份避免误删用户手写）
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


def install_to_claude_code(
    skill_path: Path | str,
    target_root: Path | str | None = None,
    side: str = "main",
) -> Path:
    """把一个 skill 装到 ``<target_root>/.claude/skills/<name>``。

    ``side='main'``  → 链接到 ``<skill_path>/`` 整目录
    ``side='staging'`` → 链接到 ``<skill_path>/../.canary/<name>/`` 整目录

    安装方式按平台能力**三阶 fallback**（详见 ``install_fallback.install_dir``）：

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


def _session_start_t(jsonl_path: Path) -> Optional[float]:
    """读 CC session JSONL 第一条带 timestamp 的事件，转 epoch float。

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


_SAFE_NAME_RE = None  # lazy init below

def _sanitize_for_filename(s: str, maxlen: int = 32) -> str:
    """把任意字符串转成"能进文件名"的版本：保留 a-z A-Z 0-9 - _ . 其余转 _ ."""
    import re as _re
    global _SAFE_NAME_RE
    if _SAFE_NAME_RE is None:
        _SAFE_NAME_RE = _re.compile(r"[^A-Za-z0-9._-]")
    if not s:
        return ""
    cleaned = _SAFE_NAME_RE.sub("_", s).strip("._-")
    return cleaned[:maxlen] if cleaned else ""


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


def _cc_traj_id(jsonl_path: Path, session_id: str) -> str:
    """为 CC bridged 轨迹生成 ``traj_cc_<projectname>_<sid8>`` 形式的 ID。

    保留 ``traj_`` 前缀让 watcher 的 ``traj_*.md`` glob 仍能匹配；
    ``projectname`` 从 JSONL 的 ``cwd`` 字段取 basename，无 cwd 退化为
    ``unknown``；``sid8`` 是 session UUID 前 8 字符（碰撞概率极低）。

    例：
      cwd=/home/admin/dataharness, sid=f2eb54d4-... → traj_cc_dataharness_f2eb54d4
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


# ─────────────────────────────────────────────────────────────────
# Codex-specific helpers
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
    """codex bridged 轨迹 ID：``traj_codex_<projectname>_<sid8>``。

    与 ``_cc_traj_id`` 同形，前缀换成 ``traj_codex_`` 让 trajectory 元数据能
    一眼区分来源。cwd 从 codex JSONL 首行抽（不是 CC 的 per-event 字段）。
    """
    content = jsonl_path.read_text(encoding="utf-8", errors="ignore") if jsonl_path.is_file() else ""
    cwd = _read_cwd_from_codex_jsonl(content)
    project = _sanitize_for_filename(Path(cwd).name if cwd else "", maxlen=32) or "unknown"
    sid_short = _sanitize_for_filename(session_id, maxlen=8) or "nosid"
    return f"traj_codex_{project}_{sid_short}"


# 内部 wrapper：让 spec 的 cwd_from_content 接 (path) 输入仍能复用 CC 的 per-event 扫描
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


# ─────────────────────────────────────────────────────────────────
# Concrete ecosystem specs
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

    P2 起本函数仅是 ``JsonlIngester(CC_SPEC).scan_and_bridge`` 的 thin wrapper，
    保留独立签名以兼容老调用方（SDK 用户 / 测试）。
    """
    return JsonlIngester(CC_SPEC).scan_and_bridge(
        target_traj_dir=Path(target_traj_dir),
        home_root=Path(home_root) if home_root else None,
        seen_sessions=seen_sessions,
    )


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
        桥接，不注 header / 不翻牌——退化成 commit b250f0a 的纯 ingester。
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
        from xskill.install_history import InstallHistory
        from xskill.session_assignments import SessionAssignments

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


def _read_head_sha(skill_path: Path, *, ref: str) -> str:
    """读 skill 子仓 ``ref`` 分支的 HEAD sha；读不到返回 ""。"""
    try:
        from xskill.git_lock import run_git
        code, out, _ = run_git(["rev-parse", ref], cwd=str(skill_path))
        if code == 0 and out:
            return out.strip()
    except Exception:
        pass
    return ""


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


def install_all_to_opencode(
    skill_dir: Path | str,
    target_root: Path | str | None = None,
    names: Iterable[str] | None = None,
) -> list[Path]:
    """Install every skill under ``skill_dir`` (each subdir = one skill) to
    OpenCode's discovery root (``<target_root>/.agents/skills``——与 Codex 共享
    user-scope skills 目录). If ``names`` is given, restrict to those.

    注意：Codex 与 OpenCode 的 install 目标是**同一个目录**——重复 install
    同一 skill 是 idempotent（install_fallback.install_dir 会先 unlink 后重链）。
    """
    return _install_all_with(install_to_opencode, skill_dir, target_root, names)


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


# ═════════════════════════════════════════════════════════════════
# OpenCode adapter (P3)
# ═════════════════════════════════════════════════════════════════
#
# OpenCode 与 CC / Codex 形态最大的区别：**它不是 JSONL append-only，而是
# SQLite + WAL**。详见 docs/dev-plan/adapter-research.md §OpenCode。
#
# 设计要点（来自 design doc §2.2 / R2）：
#
# 1. **只读连接**：`sqlite3.connect("file:...?mode=ro&immutable=1", uri=True)`
#    避免 OpenCode 跑时 ingester 触发 WAL 写锁 (`database is locked`)。
# 2. **cursor 策略**：按 `session.time_updated` 增量取——OpenCode 写新 message
#    会同时 bump 该 session 的 `time_updated`，比逐 message 扫便宜。
# 3. **`message.data` 是 JSON-in-text**（drizzle `text({ mode: "json" })`）：
#    `json.loads` 后从 `data["role"]` / `data["path"]["cwd"]` 抽字段。
#    message 表本身**没有** role 列。
# 4. **Skill 安装目录**：`<home>/.agents/skills/<name>/`——与 Codex 共享，
#    OpenCode discoverSkills 扫此路径，详见 packages/opencode/src/skill/index.ts。
#    **不是** `<repo>/.opencode/skills/`（那是项目级备选，xskill 默认走全局）。
#
# TODO(P2 对齐): EcosystemSpec dataclass 形状在 design doc §2.2.1 草拟，但
# P2 抽 JsonlIngester 时可能微调。这里先按 design doc 形状自加，注明 TODO；
# P2 merge 后 rebase 时与 P2 的 spec 形状对齐（届时 dataclass + Callable +
# Literal 的 import 也可能由 P2 已添到顶部——本 PR 直接在顶部 import）。


# ─────────────────────────────────────────────────────────────────
# Path helpers (OpenCode + shared ~/.agents/skills/)
# ─────────────────────────────────────────────────────────────────


def _opencode_db_path(home: Path) -> Path:
    """OpenCode 主 DB 路径：``<home>/.local/share/opencode/opencode.db``。

    走 XDG_DATA_HOME 默认值（``$HOME/.local/share``）。OpenCode 自己用
    npm `xdg-basedir` 包解析；本 helper 不读 env，只做 XDG 默认值路径运算
    （让单测可纯函数断言）。如果用户显式设了 `XDG_DATA_HOME` / `OPENCODE_DB`，
    daemon 层需要在调用前自己覆盖 home_root——这里不做 env 解析以保持
    跨平台一致性（Windows 上 xdg-basedir 行为非标，见 R1）。
    """
    return home / ".local" / "share" / "opencode" / "opencode.db"



# ─────────────────────────────────────────────────────────────────
# Ecosystem spec (P3, OpenCode)
# ─────────────────────────────────────────────────────────────────
#
# TODO(P2 对齐): design doc §2.2.1 草拟的 EcosystemSpec dataclass 真正引入要
# 等 P2 抽 JsonlIngester 时定型。本 PR 只为 OpenCode 自加一个轻量 spec，让
# SqliteIngester 拿到稳定的 (path_resolver, label) 输入；rebase 时与 P2 对齐。

@dataclass(frozen=True)
class SqliteEcosystemSpec:
    """SQLite-back 生态系统 spec（独立于 JsonlIngester 的 EcosystemSpec）。

    P2 的 EcosystemSpec 是 JSONL ingester 专用 spec（含 sessions_glob 等
    JSONL-only 字段），不适合 SQLite。SqliteIngester 用本类，字段集中在
    SQLite 视角：path_resolver 解析到 .db 文件、cursor_strategy 用
    time_updated。
    """

    name: str                                       # "opencode" | "codex" | "claude_code"
    source_kind: Literal["jsonl", "sqlite"]
    path_resolver: Callable[[Path], Path]           # (home) -> db file / dir
    cursor_strategy: Literal["mtime_offset", "sqlite_time_updated"]
    label: str                                      # adapter / metadata 标签


OPENCODE_SPEC = SqliteEcosystemSpec(
    name="opencode",
    source_kind="sqlite",
    path_resolver=_opencode_db_path,
    cursor_strategy="sqlite_time_updated",
    label="opencode",
)


# ─────────────────────────────────────────────────────────────────
# SqliteIngester (P3, 独立新类——不复用 / 不耦合 JsonlIngester)
# ─────────────────────────────────────────────────────────────────


class SqliteIngester:
    """把 SQLite-back 的 agent session（当前仅 OpenCode）桥到 xskill watch dir。

    与 ``CCSessionIngester`` (JSONL append-only) **设计上独立**——不共享基类、
    不共享 cursor 文件格式、不共享 ingest 函数。原因：

    * **读端模型不同**：JSONL 用"mtime + byte offset"作 cursor；SQLite 用
      "上次见过的 session.time_updated 最大值"作 cursor。
    * **连接生命期不同**：JSONL 是 per-file open/read/close；SQLite 是
      per-poll open URI / cursor / close（用 `immutable=1` 避免 WAL 锁）。
    * **schema 演化不同**：JSONL 由 RolloutLine 自描述；SQLite 由 drizzle
      migrations 控制，xskill 端只读 (id, directory, time_updated) +
      (session_id, data)，对 schema 演化最不敏感的两层。

    强行抽公共基类会引入 stub 字段 / dead method，得不偿失。P2 抽
    JsonlIngester 时如发现真有"扫描周期 / seen 集合管理"等公共点，**再** 抽
    `IngesterBase`，把本类当 mixin 接进去。

    用法（daemon 起 thread 用，但这里只暴露同步 ``run_once``——线程封装由
    caller 提供，与 CCSessionIngester 的线程逻辑解耦）：

        ing = SqliteIngester(
            target_traj_dir="/path/to/traj",
            home_root=Path.home(),
            spec=OPENCODE_SPEC,
        )
        results = ing.run_once()   # 返回这一轮新桥的 record list
    """

    def __init__(
        self,
        target_traj_dir: Path | str,
        *,
        home_root: Path | str | None = None,
        spec: SqliteEcosystemSpec = OPENCODE_SPEC,
        poll_interval: float = 10.0,
    ):
        if spec.source_kind != "sqlite":
            raise ValueError(
                f"SqliteIngester only accepts source_kind='sqlite', "
                f"got {spec.source_kind!r} for spec {spec.name!r}"
            )
        self.target_traj_dir = Path(target_traj_dir)
        self.home_root = Path(home_root) if home_root else Path.home()
        self.spec = spec
        self.poll_interval = poll_interval
        # cursor: 上次见过的 session.time_updated 最大值（毫秒，OpenCode 用 epoch ms）
        self._cursor_ms: int = 0
        # 已桥接过的 session id（重启后由 _scan_seen_sessions 重建——同 CC 思路）
        self._seen: set[str] = _scan_seen_sessions(self.target_traj_dir)
        # daemon thread
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._stats = {
            "polls": 0, "ingested": 0, "errors": 0, "last_poll": None,
        }

    # ── daemon thread lifecycle ──────────────────────────────────

    def start(self) -> None:
        """起 daemon 线程周期性调 ``run_once``。幂等：已在跑则 no-op。

        SqliteIngester 用 ``?mode=ro&immutable=1`` 打开 DB，与 OpenCode
        写端并发**永远不会**触发 ``database is locked``——daemon 长跑安全。
        """
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, daemon=True,
            name=f"xskill-{self.spec.name}-ingester",
        )
        self._thread.start()
        logger.info(
            "SqliteIngester(%s) started "
            "(db=%s, target=%s, interval=%.1fs, %d sessions pre-seen)",
            self.spec.name, self.db_path, self.target_traj_dir,
            self.poll_interval, len(self._seen),
        )

    def stop(self) -> None:
        """干净停止 daemon 线程（避免 zombie）。"""
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=self.poll_interval + 5)
        logger.info("SqliteIngester(%s) stopped", self.spec.name)

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def stats(self) -> dict:
        return {**self._stats, "seen_sessions": len(self._seen),
                "cursor_ms": self._cursor_ms, "running": self.is_running}

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                submitted = self.run_once()
                self._stats["polls"] += 1
                self._stats["last_poll"] = time.time()
                if submitted:
                    self._stats["ingested"] += len(submitted)
                    logger.info(
                        "SqliteIngester(%s): bridged %d new session(s) → %s",
                        self.spec.name, len(submitted), self.target_traj_dir,
                    )
            except Exception:
                self._stats["errors"] += 1
                logger.exception("SqliteIngester(%s) scan error", self.spec.name)
            self._stop.wait(self.poll_interval)

    # ── public API ────────────────────────────────────────────────

    @property
    def db_path(self) -> Path:
        """spec 解析后的 DB 绝对路径。"""
        return self.spec.path_resolver(self.home_root)

    @property
    def cursor_ms(self) -> int:
        """当前 cursor（最近一次看到的 session.time_updated 最大值，单位 ms）。"""
        return self._cursor_ms

    def run_once(self) -> list[dict]:
        """单次扫描：用 cursor 取 `time_updated > cursor` 的所有 session，
        每个 session 把 message 拼成 markdown 提交一条 trajectory。

        返回这一轮新桥的 record list（与 ``ingest_claude_code_sessions`` 同型）。
        DB 不存在（用户机器上压根没装 opencode）是正常情况，返回空。
        """
        db_path = self.db_path
        if not db_path.is_file():
            return []

        submitted: list[dict] = []
        # 只读 URI 连接：immutable=1 让 SQLite 完全跳过 WAL 协议（不读 -wal /
        # -shm），与 OpenCode 写端并发时**绝不会**触发 `database is locked`。
        # 代价：cursor 增量需要每次 reopen——但 OpenCode session 数量 <1k 量级，
        # poll 周期 >5s，可忽略。
        conn = self._open_ro(db_path)
        try:
            cur = conn.cursor()
            # 抓增量 session
            cur.execute(
                "SELECT id, directory, time_updated FROM session "
                "WHERE time_updated > ? ORDER BY time_updated",
                (self._cursor_ms,),
            )
            sessions = cur.fetchall()

            for sid, directory, time_updated in sessions:
                if sid in self._seen:
                    # cursor 落后于 seen 集合时（重启场景），跳过已桥的
                    if time_updated > self._cursor_ms:
                        self._cursor_ms = time_updated
                    continue

                # 抽这条 session 的所有 message
                cur.execute(
                    "SELECT data FROM message WHERE session_id = ? ORDER BY time_created",
                    (sid,),
                )
                messages = [self._parse_message_data(row[0]) for row in cur.fetchall()]

                # 生成 traj_id：含 project basename + sid8（同 CC 命名风格）
                traj_id = self._opencode_traj_id(sid, directory)
                # 拼 markdown 内容
                md_content = self._render_session_md(
                    sid=sid, directory=directory, messages=messages,
                )
                result = submit_trajectory(
                    content=md_content,
                    format="raw",
                    traj_id=traj_id,
                    traj_dir=self.target_traj_dir,
                    metadata={
                        "ecosystem": self.spec.label,
                        "session_id": sid,
                        "cwd": directory,
                    },
                )
                result["session_id"] = sid
                result["session_directory"] = directory
                result["session_time_updated"] = time_updated
                result["messages"] = messages  # 让单测能直接断言抽取正确性
                submitted.append(result)
                self._seen.add(sid)
                if time_updated > self._cursor_ms:
                    self._cursor_ms = time_updated
        finally:
            conn.close()
        return submitted

    # ── internals ─────────────────────────────────────────────────

    @staticmethod
    def _open_ro(db_path: Path) -> "sqlite3.Connection":
        """打开只读 + immutable 连接。

        ``mode=ro`` 单纯关写权；``immutable=1`` 额外让 SQLite 假设文件"不会
        被任何其他进程改"——它会**完全跳过 WAL 协议**（不读 -wal / -shm 文件，
        不抢任何锁）。代价：拿不到 OpenCode 写端尚未 checkpoint 进主 db 的
        最新数据。但 OpenCode 用默认 PRAGMA `journal_mode=WAL` +
        `wal_autocheckpoint=1000`，几秒内就 checkpoint 一次，xskill ingester
        poll 周期 ≥5s 完全够用。

        最重要的：**永远不会触发 `database is locked`**（设计 doc R2）。
        """
        # uri=True 必须；URI 中 mode=ro 等价于 SQLITE_OPEN_READONLY，
        # immutable=1 是 SQLite >= 3.8 的扩展（Python 3.7+ stdlib 都带）。
        uri = f"file:{db_path}?mode=ro&immutable=1"
        return sqlite3.connect(uri, uri=True)

    @staticmethod
    def _parse_message_data(data_text: str) -> dict:
        """把 `message.data` (JSON-in-text) 解出来抽 role / content / cwd 等。

        OpenCode `message.data` 没有固定 schema——drizzle 端是 `text({ mode: "json" })`
        裸 JSON。我们关心的字段：

        * `role`: "user" | "assistant" | ...
        * `path.cwd`: assistant message 上才有，是实际 cwd（与 session.directory
          冗余但有时不同）
        * `agent`: "build" | "plan" | ...
        * `model`: {providerID, modelID}

        其余字段透传到 metadata，给下游 task agent 看。
        """
        try:
            obj = json.loads(data_text)
        except json.JSONDecodeError:
            # 不做容错的容错——data 列 NOT NULL 且 OpenCode 自己反序列化也会炸；
            # 抛上去，daemon 层 logger.exception 记下来。
            raise
        return obj

    @staticmethod
    def _render_session_md(
        *, sid: str, directory: str, messages: list[dict],
    ) -> str:
        """把一条 OpenCode session 拼成 xskill watcher 能消费的 trajectory markdown。

        不走 `_adapt_opencode_*` adapter 是故意的——P3 scope 只做 ingester，
        不动 adapters。用 ``format="raw"`` 让 submit_trajectory 直接落盘，
        待 P4 / 未来再加专用 adapter（如果发现 raw 的语义损失太大）。
        """
        lines = [
            f"# OpenCode session {sid}",
            "",
            f"- cwd: `{directory}`",
            f"- messages: {len(messages)}",
            "",
        ]
        for i, msg in enumerate(messages):
            role = msg.get("role", "?")
            lines.append(f"## msg {i} (role={role})")
            lines.append("")
            lines.append("```json")
            lines.append(json.dumps(msg, ensure_ascii=False, indent=2))
            lines.append("```")
            lines.append("")
        return "\n".join(lines)

    @staticmethod
    def _opencode_traj_id(sid: str, directory: str) -> str:
        """traj_oc_<projectname>_<sid8> 命名，与 CC bridged 同风格。

        OpenCode session id 是 `ses_xxxx` 形式（带前缀），sid8 取前 8 字符
        即可，碰撞概率忽略。
        """
        project = _sanitize_for_filename(
            Path(directory).name if directory else "", maxlen=32,
        ) or "unknown"
        sid_short = _sanitize_for_filename(sid, maxlen=8) or "nosid"
        return f"traj_oc_{project}_{sid_short}"


# ─────────────────────────────────────────────────────────────────
# Installer: install_to_opencode (writes to ~/.agents/skills/ — shared)
# ─────────────────────────────────────────────────────────────────


def install_to_opencode(
    skill_path: Path | str,
    target_root: Path | str | None = None,
    side: str = "main",
) -> Path:
    """把一个 skill 装到 ``<target_root>/.agents/skills/<name>``。

    与 ``install_to_claude_code`` 的区别：

    * **写到共享目录**：``~/.agents/skills/<name>/``——OpenCode（以及 Codex）
      官方 discover 路径，不是 ``<repo>/.opencode/skills/``。
    * **同 fallback 链**：symlink → directory junction (Win) → copy + warning
      （复用 P1 ``install_fallback.install_dir``）。
    * **same source switch (main / staging)**：同样支持 ``side`` 参数，
      ``staging`` 链到 ``<skill_path>/../.canary/<name>/``。

    返回 dest 下的 SKILL.md 路径（约定，与 CC 版一致）。
    """
    skill_path = Path(skill_path).resolve()
    if not skill_path.is_dir():
        raise NotADirectoryError(f"skill_path is not a directory: {skill_path}")

    # 校验 source 齐备（main: SKILL.md 必须有；staging: .canary/<name>/SKILL.md 必须有）
    _source_md_for_side(skill_path, side)

    if side == "main":
        src_dir = skill_path
    elif side == "staging":
        src_dir = (skill_path.parent / ".canary" / skill_path.name).resolve()
    else:
        raise ValueError(f"side must be 'main' or 'staging', got {side!r}")

    name = skill_path.name
    root = Path(target_root) if target_root else Path.home()
    skills_root = _agents_skills_path(root)
    skills_root.mkdir(parents=True, exist_ok=True)
    dest = skills_root / name

    # 已有 symlink 且指向正确：no-op（同 install_to_claude_code 的 idempotent 逻辑）
    if dest.is_symlink():
        try:
            cur = dest.resolve(strict=False)
        except OSError:
            cur = None
        if cur == src_dir:
            return dest / "SKILL.md"
        dest.unlink()
    elif dest.exists():
        if dest.is_dir():
            backup = skills_root / f".{name}.replaced-by-symlink"
            if backup.exists():
                shutil.rmtree(backup)
            dest.rename(backup)
        else:
            dest.unlink()

    mode: InstallMode = install_dir(src_dir, dest)
    if mode == "copy":
        logger.warning(
            "install_to_opencode(%s): copy-mode install at %s — "
            "live-update / user-edit-absorb are disabled on this destination",
            name, dest,
        )
    return dest / "SKILL.md"
