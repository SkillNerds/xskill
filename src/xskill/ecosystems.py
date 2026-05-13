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
import threading
import time
from pathlib import Path
from typing import Iterable, Optional

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


# ─────────────────────────────────────────────────────────────────
# Ecosystem auto-detection
# ─────────────────────────────────────────────────────────────────

# Known agent tools and where each one writes its session trajectories.
# Used by ``detect_known_ecosystems`` at server startup to auto-register
# without making the user run `xskill registry add` for every ecosystem.
_KNOWN_ECOSYSTEMS: list[dict] = [
    {
        "id": "claude_code",
        "source_subpath": ".claude/projects",  # CC writes <home>/<this>/<cwd-hash>/*.jsonl
        "bridge_subpath": ".xskill/cc_sessions",  # we mirror them here as traj_*.md
    },
    # Future: codex / opencode / etc. follow the same {id, source, bridge} shape.
]


def detect_known_ecosystems(home_root: Path | str | None = None) -> list[dict]:
    """Probe the user's HOME for known agent tools and report which ones
    have something on disk. Returns a list of detection records:

        {"ecosystem": "claude_code",
         "source": <abs path of native session dir>,
         "bridge": <abs path of paired xskill watch dir>}

    A record only appears if the source dir exists. The bridge dir is the
    path daemon should ``register_dir(..., ecosystem=...)`` to put under
    Registry control — it may or may not exist yet.
    """
    root = Path(home_root) if home_root else Path.home()
    found: list[dict] = []
    for spec in _KNOWN_ECOSYSTEMS:
        source = root / spec["source_subpath"]
        if not source.is_dir():
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
    root = Path(target_root) if target_root else Path.home()
    skills_root = _cc_skills_path(root)
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
        # 调用方关心 mode 时可看日志；这里不破坏返回值兼容（保留旧 SDK 签名）。
        logger.warning(
            "install_to_claude_code(%s): copy-mode install at %s — "
            "live-update / user-edit-absorb are disabled on this destination",
            name, dest,
        )
    return dest / "SKILL.md"


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
    """
    target_traj_dir = Path(target_traj_dir)
    target_traj_dir.mkdir(parents=True, exist_ok=True)
    root = Path(home_root) if home_root else Path.home()
    proj_root = _cc_projects_path(root)
    if not proj_root.is_dir():
        return []

    seen = seen_sessions if seen_sessions is not None else set()
    submitted: list[dict] = []
    for jsonl_path in sorted(proj_root.glob("*/*.jsonl")):
        sid = jsonl_path.stem
        if sid in seen:
            continue
        content = jsonl_path.read_text(encoding="utf-8")
        if not content.strip():
            continue
        # 给 CC bridged 轨迹起带项目信息的名字，方便事后查阅：
        # traj_cc_<projectname>_<sid8>.md 比 traj_0001.md 包含的语义多。
        traj_id = _cc_traj_id(jsonl_path, sid)
        result = submit_trajectory(
            content=content,
            format="claude_code_jsonl",
            traj_id=traj_id,
            traj_dir=target_traj_dir,
        )
        result["session_id"] = sid
        result["source_jsonl"] = str(jsonl_path)
        result["session_start_t"] = _session_start_t(jsonl_path)
        submitted.append(result)
        seen.add(sid)
    return submitted


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
        installed.append(install_to_claude_code(entry, target_root=target_root))
    return installed
