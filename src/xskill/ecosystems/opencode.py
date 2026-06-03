"""
ecosystems/opencode.py -- OpenCode 生态适配
===========================================

把蒸馏出的 Skill 装进 OpenCode 的 user-scope skill 目录
（``~/.agents/skills/<name>/``——与 Codex / OpenClaw 共享），并把 OpenCode 原生
session（SQLite ``~/.local/share/opencode/opencode.db``）桥接回 xskill 的标准
``traj_*.md`` 格式。

OpenCode 与 CC / Codex 形态最大的区别：**它不是 JSONL append-only，而是
SQLite + WAL**。详见 docs/dev-plan/adapter-research.md §OpenCode。所以本平台
用独立的 ``SqliteIngester``（不复用 / 不耦合 ``JsonlIngester``）。

设计要点（来自 design doc §2.2 / R2）：

1. **只读连接**：``sqlite3.connect("file:...?mode=ro&immutable=1", uri=True)``
   避免 OpenCode 跑时 ingester 触发 WAL 写锁 (``database is locked``)。
2. **cursor 策略**：按 ``session.time_updated`` 增量取——OpenCode 写新 message
   会同时 bump 该 session 的 ``time_updated``，比逐 message 扫便宜。
3. **``message.data`` 是 JSON-in-text**（drizzle ``text({ mode: "json" })``）：
   ``json.loads`` 后从 ``data["role"]`` / ``data["path"]["cwd"]`` 抽字段。
4. **Skill 安装目录**：``<home>/.agents/skills/<name>/``——与 Codex 共享，
   OpenCode discoverSkills 扫此路径。**不是** ``<repo>/.opencode/skills/``。
"""

from __future__ import annotations

import json
import logging
import shutil
import sqlite3
import threading
import time
from pathlib import Path
from typing import Iterable

from xskill.ecosystems._fallback import (
    InstallMode, _is_link_or_junction, install_dir,
)
from xskill.ecosystems._shared import (
    SqliteEcosystemSpec,
    _agents_skills_path,
    _install_all_with,
    _sanitize_for_filename,
    _scan_seen_sessions,
    _source_md_for_side,
    submit_trajectory,
)

logger = logging.getLogger("xskill.ecosystems")


# ─────────────────────────────────────────────────────────────────
# Path helpers
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
# Ecosystem spec
# ─────────────────────────────────────────────────────────────────

OPENCODE_SPEC = SqliteEcosystemSpec(
    name="opencode",
    source_kind="sqlite",
    path_resolver=_opencode_db_path,
    cursor_strategy="sqlite_time_updated",
    label="opencode",
    traj_id_prefix="traj_oc_",
)


# ─────────────────────────────────────────────────────────────────
# Session → markdown 渲染（读 part 表还原真实对话内容）
# ─────────────────────────────────────────────────────────────────
#
# OpenCode 数据模型：``message`` 表只存信封（role / model / cost / tokens），
# 真实内容在 ``part`` 表——一条 message 对应多条 part。``part.data`` 的 type：
#
#   text         真实文本（user 提问 / assistant 回复）
#   reasoning    assistant 思考
#   tool         工具调用 + 输入/输出（state.status: completed | error）
#   step-start / step-finish   step 边界 + 计费，噪声，渲染时丢弃
#   patch        一次 patch（文件列表）
#
# 渲染成与 CC bridged 轨迹同构的 ``## User`` / ``## Assistant`` /
# ``## Tool Call`` 结构，让 TaskAgent 能按 ``## User`` 切 atom。

_NOISE_PART_TYPES = {"step-start", "step-finish"}


def _render_tool_part(part: dict) -> str:
    """一个 ``tool`` part → ``## Tool Call: <tool>`` 段。"""
    tool = part.get("tool") or "?"
    state = part.get("state") or {}
    status = state.get("status", "?")
    head = f"## Tool Call: {tool}" + (" [ERROR]" if status == "error" else "")
    lines = [head, "", "input:", "```json",
             json.dumps(state.get("input", {}), ensure_ascii=False, indent=2),
             "```", ""]
    if status == "error":
        lines += ["error:", "```", str(state.get("error") or ""), "```"]
    else:
        lines += ["output:", "```", str(state.get("output") or ""), "```"]
    return "\n".join(lines)


def _render_patch_part(part: dict) -> str:
    """一个 ``patch`` part → ``## Patch`` 段（文件列表）。"""
    files = part.get("files") or []
    body = "\n".join(f"- `{f}`" for f in files) or "(no files)"
    return f"## Patch\n\n{body}"


def _render_message(role: str, parts: list[dict]) -> list[str]:
    """一条 message + 它的 parts → 若干 markdown 段（保持 part 时间顺序）。"""
    if role == "user":
        texts = [str(p.get("text") or "").strip()
                 for p in parts if p.get("type") == "text"]
        texts = [t for t in texts if t]
        return [f"## User\n\n{chr(10).join(texts)}"] if texts else []

    # assistant（及其他非 user role）：reasoning / text 累积成 ## Assistant，
    # 遇到 tool / patch 就先 flush 再单独成段，保持真实时间线。
    sections: list[str] = []
    buf: list[str] = []

    def flush() -> None:
        if buf:
            sections.append("## Assistant\n\n" + "\n\n".join(buf))
            buf.clear()

    for p in parts:
        ptype = p.get("type")
        if ptype in _NOISE_PART_TYPES:
            continue
        if ptype == "reasoning":
            txt = str(p.get("text") or "").strip()
            if txt:
                buf.append(f"_(reasoning)_\n\n{txt}")
        elif ptype == "text":
            txt = str(p.get("text") or "").strip()
            if txt:
                buf.append(txt)
        elif ptype == "tool":
            flush()
            sections.append(_render_tool_part(p))
        elif ptype == "patch":
            flush()
            sections.append(_render_patch_part(p))
        else:
            # 未知 part type：不静默丢，原样留个 JSON 块
            flush()
            sections.append(
                f"## Part: {ptype}\n\n```json\n"
                + json.dumps(p, ensure_ascii=False, indent=2) + "\n```")
    flush()
    return sections


# ─────────────────────────────────────────────────────────────────
# SqliteIngester (独立新类——不复用 / 不耦合 JsonlIngester)
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

    强行抽公共基类会引入 stub 字段 / dead method，得不偿失。

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
        """单次扫描:用 cursor 取 `time_updated > cursor` 的所有 session，
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

                # 抽这条 session 的所有 message（含 id —— 用来关联 part 表）
                cur.execute(
                    "SELECT id, data FROM message WHERE session_id = ? "
                    "ORDER BY time_created",
                    (sid,),
                )
                msg_rows = cur.fetchall()
                # OpenCode 把消息内容存在 part 表（message 只是信封）。一次取
                # 全 session 的 part，按 message_id 分组挂回各 message。
                cur.execute(
                    "SELECT message_id, data FROM part WHERE session_id = ? "
                    "ORDER BY time_created",
                    (sid,),
                )
                parts_by_msg: dict[str, list[dict]] = {}
                for mid, pdata in cur.fetchall():
                    parts_by_msg.setdefault(mid, []).append(
                        self._parse_json_col(pdata))
                messages = []
                for mid, mdata in msg_rows:
                    msg = self._parse_json_col(mdata)
                    msg["_parts"] = parts_by_msg.get(mid, [])
                    messages.append(msg)

                # 生成 traj_id：含 project basename + sid8（同 CC 命名风格）；
                # 前缀按 spec.traj_id_prefix 派生，避免对 ecosystem 硬编码 if 分支。
                traj_id = self._sqlite_traj_id(sid, directory)
                # 用户 agent 模型(批2):message.data.model = {providerID, modelID}
                model = next(
                    (m["model"].get("modelID") for m in messages
                     if isinstance(m.get("model"), dict) and m["model"].get("modelID")),
                    "",
                )
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
                        **({"model": model} if model else {}),
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
    def _parse_json_col(data_text: str) -> dict:
        """``message.data`` / ``part.data`` 都是 JSON-in-text（drizzle
        ``text({ mode: "json" })`` 裸 JSON）。

        解析失败直接抛——data 列 NOT NULL，OpenCode 自己反序列化也会炸；
        抛上去由 daemon 层 logger.exception 记录，不做容错的容错。
        """
        return json.loads(data_text)

    @staticmethod
    def _render_session_md(
        *, sid: str, directory: str, messages: list[dict],
    ) -> str:
        """把一条 OpenCode session 渲染成 xskill watcher 能消费的 trajectory
        markdown —— 读每条 message 的 ``_parts``，还原真实对话内容。

        产出与 CC bridged 轨迹同构的 ``## User`` / ``## Assistant`` /
        ``## Tool Call`` 结构（详见模块上方 ``_render_message`` 一节），让
        TaskAgent 能按 ``## User`` 切 atom。
        """
        out: list[str] = [
            f"# OpenCode session {sid}",
            "",
            f"- cwd: `{directory}`",
            "",
        ]
        for msg in messages:
            role = msg.get("role", "?")
            for section in _render_message(role, msg.get("_parts", [])):
                out.append(section)
                out.append("")
        return "\n".join(out)

    def _sqlite_traj_id(self, sid: str, directory: str) -> str:
        """``<spec.traj_id_prefix><projectname>_<sid8>`` 命名，与 CC bridged
        同风格。

        OpenCode / ngagent session id 是 ``ses_xxxx`` 形式（带前缀），
        sid8 取前 8 字符即可，碰撞概率忽略。前缀由 ``spec.traj_id_prefix``
        决定（``traj_oc_`` for opencode、``traj_ng_`` for ngagent），
        非硬编码。
        """
        project = _sanitize_for_filename(
            Path(directory).name if directory else "", maxlen=32,
        ) or "unknown"
        sid_short = _sanitize_for_filename(sid, maxlen=8) or "nosid"
        return f"{self.spec.traj_id_prefix}{project}_{sid_short}"


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
      （复用 ``_fallback.install_dir``）。
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

    # 已有 link/junction 且指向正确：no-op。``_is_link_or_junction``
    # 而非 ``is_symlink`` —— Windows 对 junction 返回 False（issue #35 同源
    # bug），统一处理 link/junction 两种 reparse point。
    if _is_link_or_junction(dest):
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


def install_all_to_opencode(
    skill_dir: Path | str,
    target_root: Path | str | None = None,
    names: Iterable[str] | None = None,
) -> list[Path]:
    """Install every skill under ``skill_dir`` (each subdir = one skill) to
    OpenCode's discovery root (``<target_root>/.agents/skills``——与 Codex 共享
    user-scope skills 目录). If ``names`` is given, restrict to those.

    注意：Codex 与 OpenCode 的 install 目标是**同一个目录**——重复 install
    同一 skill 是 idempotent（``_fallback.install_dir`` 会先 unlink 后重链）。
    """
    return _install_all_with(install_to_opencode, skill_dir, target_root, names)
