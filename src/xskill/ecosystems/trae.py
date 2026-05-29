"""
ecosystems/trae.py -- Trae IDE / Trae Agent 生态适配
====================================================

* **Skill 安装**：``~/.trae-cn/skills/<name>/``（国内版 trae.cn）与
  ``~/.trae/skills/<name>/``（国际版 trae.ai）；检测到哪个目录就装哪个。
* **轨迹摄取（IDE）**：Trae 基于 VS Code 系存储，每个工作区在
  ``<AppData>/Trae*/User/workspaceStorage/<hash>/state.vscdb`` 的
  ``ItemTable`` 里以 JSON blob 保存 Builder/Chat 会话（键名如
  ``memento/icube-ai-agent-storage``、``chat.ChatSessionStore.index``）。
* **轨迹摄取（CLI）**：ByteDance ``trae-agent`` 写的
  ``trajectories/trajectory_*.json``（整文件 JSON，非 JSONL）。

参考社区对存储格式的逆向：``trae-chats-exporter``（workspaceStorage +
state.vscdb）。
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from xskill.ecosystems._fallback import install_dir
from xskill.ecosystems._shared import (
    _install_all_with,
    _install_skill_into,
    _sanitize_for_filename,
    _scan_seen_sessions,
    _source_md_for_side,
    submit_trajectory,
)

logger = logging.getLogger("xskill.ecosystems")

# state.vscdb 里可能出现的 chat blob 键（按优先级）
_TRAE_CHAT_KEYS: tuple[str, ...] = (
    "memento/icube-ai-agent-storage",
    "chat.ChatSessionStore.index",
    "ChatStore",
    "memento/icube-ai-chat-storage-7467774676505887760",
    "memento/icube-ai-ng-chat-storage-7467774676505887760",
)

# Trae Agent CLI 默认轨迹目录（相对 HOME）
_TRAE_AGENT_TRAJ_DIRS: tuple[str, ...] = (
    "trajectories",
    ".trae-cn/trajectories",
    ".trae/trajectories",
)


# ─────────────────────────────────────────────────────────────────
# Path helpers
# ─────────────────────────────────────────────────────────────────


def _trae_edition_home_dirs(home: Path) -> tuple[Path, Path]:
    """国内版 / 国际版用户配置根。"""
    return home / ".trae-cn", home / ".trae"


def _trae_skills_roots(home: Path) -> list[Path]:
    """返回应写入的 Trae skill 根目录列表（已存在或父目录存在的版本）。"""
    roots: list[Path] = []
    for edition_home in _trae_edition_home_dirs(home):
        skills = edition_home / "skills"
        if edition_home.is_dir() or skills.is_dir():
            roots.append(skills)
    if not roots:
        # 未安装过 Trae 时默认国内路径，便于首次 install 落盘
        roots.append(home / ".trae-cn" / "skills")
    return roots


def _trae_workspace_storage_roots(home: Path) -> list[Path]:
    """各平台 Trae IDE ``User/workspaceStorage`` 候选路径。"""
    roots: list[Path] = []
    if sys.platform == "darwin":
        for app_name in ("Trae", "Trae CN"):
            roots.append(
                home / "Library" / "Application Support" / app_name
                / "User" / "workspaceStorage"
            )
    elif sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        if appdata:
            for app_name in ("TRAE SOLO CN", "Trae CN", "Trae"):
                roots.append(
                    Path(appdata) / app_name / "User" / "workspaceStorage"
                )
    else:
        for cfg_name in ("Trae", "Trae CN"):
            roots.append(
                home / ".config" / cfg_name / "User" / "workspaceStorage"
            )
    return roots


def _trae_agent_trajectory_roots(home: Path) -> list[Path]:
    """Trae Agent CLI 轨迹目录候选。"""
    out: list[Path] = []
    for rel in _TRAE_AGENT_TRAJ_DIRS:
        p = home / rel
        if p.is_dir():
            out.append(p)
    return out


def _read_workspace_folder(workspace_dir: Path) -> str:
    """从 ``workspace.json`` 读项目文件夹 URI。"""
    wj = workspace_dir / "workspace.json"
    if not wj.is_file():
        return ""
    try:
        data = json.loads(wj.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    folder = data.get("folder") or data.get("configuration", {}).get("folder")
    if not folder:
        return ""
    # file:///c%3A/path → c:\path
    s = str(folder)
    if s.startswith("file:///"):
        from urllib.parse import unquote

        s = unquote(s[8:]).replace("/", os.sep)
    return s


# ─────────────────────────────────────────────────────────────────
# Installer
# ─────────────────────────────────────────────────────────────────


def install_to_trae(
    skill_path: Path | str,
    target_root: Path | str | None = None,
    side: str = "main",
) -> Path:
    """把 skill 装进所有已探测到的 Trae skills 根（cn + 国际版各一份）。

    Windows 上强制 ``copy`` 安装（与 OpenClaw/ngagent 同策略）——实测
    symlink/junction 三阶 fallback 在部分 Win10/11 环境会长时间阻塞，
    导致 ``xskill serve`` 启动卡在 ``install_all_to_trae``。
    """
    skill_path = Path(skill_path).resolve()
    _source_md_for_side(skill_path, side)

    if side == "main":
        src_dir = skill_path
    elif side == "staging":
        src_dir = (skill_path.parent / ".canary" / skill_path.name).resolve()
    else:
        raise ValueError(f"side must be 'main' or 'staging', got {side!r}")

    root = Path(target_root) if target_root else Path.home()
    last: Path | None = None
    for skills_root in _trae_skills_roots(root):
        skills_root.mkdir(parents=True, exist_ok=True)
        dest = skills_root / skill_path.name
        if sys.platform == "win32":
            install_dir(src_dir, dest, force_mode="copy", auto_reset=True)
            last = dest / "SKILL.md"
        else:
            last = _install_skill_into(
                skill_path,
                skills_root,
                side,
                ecosystem_label="trae",
            )
    if last is None:
        raise RuntimeError("no Trae skills root resolved")
    return last


def install_all_to_trae(
    skill_dir: Path | str,
    target_root: Path | str | None = None,
    names: Iterable[str] | None = None,
) -> list[Path]:
    return _install_all_with(install_to_trae, skill_dir, target_root, names)


# ─────────────────────────────────────────────────────────────────
# Detection (for detect_known_ecosystems)
# ─────────────────────────────────────────────────────────────────


def detect_trae_record(home_root: Path) -> dict | None:
    """若本机存在 Trae IDE 或 Trae 配置目录，返回 detection record。"""
    for ws_root in _trae_workspace_storage_roots(home_root):
        if ws_root.is_dir() and any(ws_root.glob("*/state.vscdb")):
            return {
                "ecosystem": "trae",
                "source": ws_root.resolve(),
                "bridge": (home_root / ".xskill" / "trae_sessions").resolve(),
            }
    for edition_home in _trae_edition_home_dirs(home_root):
        if edition_home.is_dir():
            return {
                "ecosystem": "trae",
                "source": edition_home.resolve(),
                "bridge": (home_root / ".xskill" / "trae_sessions").resolve(),
            }
    if _trae_agent_trajectory_roots(home_root):
        return {
            "ecosystem": "trae",
            "source": _trae_agent_trajectory_roots(home_root)[0].resolve(),
            "bridge": (home_root / ".xskill" / "trae_sessions").resolve(),
        }
    return None


# ─────────────────────────────────────────────────────────────────
# state.vscdb extraction
# ─────────────────────────────────────────────────────────────────


def _open_vscdb_readonly(db_path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True)


def _query_chat_blob(conn: sqlite3.Connection) -> tuple[dict | None, str]:
    cur = conn.cursor()
    for key in _TRAE_CHAT_KEYS:
        cur.execute("SELECT value FROM ItemTable WHERE [key] = ?", (key,))
        row = cur.fetchone()
        if not row or not row[0]:
            continue
        try:
            return json.loads(row[0]), key
        except json.JSONDecodeError:
            continue
    cur.execute(
        "SELECT [key], value FROM ItemTable WHERE [key] LIKE '%chat%' "
        "OR [key] LIKE '%memento/icube-ai%' LIMIT 50"
    )
    for key, value in cur.fetchall():
        try:
            data = json.loads(value)
        except json.JSONDecodeError:
            continue
        if data and (
            data.get("sessions")
            or data.get("entries")
            or data.get("list")
            or isinstance(data, list)
        ):
            return data, key
    return None, ""


def _sessions_from_chat_blob(chat_data: dict, used_key: str) -> list[dict]:
    """把 chat store blob 规范成 session dict 列表。"""
    entries: dict[str, Any] = {}

    if used_key == "memento/icube-ai-agent-storage":
        lst = chat_data.get("list")
        if isinstance(lst, list):
            for i, item in enumerate(lst):
                if not isinstance(item, dict):
                    continue
                sid = item.get("sessionId") or item.get("id") or str(i)
                entries[str(sid)] = item
    elif used_key == "ChatStore":
        raw = chat_data.get("sessions") or chat_data.get("entries")
        if isinstance(raw, dict):
            entries = {str(k): v for k, v in raw.items() if isinstance(v, dict)}
        elif isinstance(raw, list):
            entries = {str(i): x for i, x in enumerate(raw) if isinstance(x, dict)}
    elif "memento/icube-ai" in used_key:
        lst = chat_data.get("list")
        if isinstance(lst, list):
            for i, item in enumerate(lst):
                if isinstance(item, dict):
                    sid = item.get("sessionId") or item.get("id") or str(i)
                    entries[str(sid)] = item
        else:
            raw = (
                chat_data.get("sessions")
                or chat_data.get("conversations")
                or chat_data.get("entries")
            )
            if isinstance(raw, dict):
                entries = {str(k): v for k, v in raw.items() if isinstance(v, dict)}
    else:
        raw = chat_data.get("entries")
        if isinstance(raw, dict):
            entries = {str(k): v for k, v in raw.items() if isinstance(v, dict)}
        elif isinstance(raw, list):
            entries = {str(i): x for i, x in enumerate(raw) if isinstance(x, dict)}

    return [s for s in entries.values() if isinstance(s, dict)]


def _message_text(msg: dict) -> str:
    """从 Trae IDE 单条 message 对象抽取可读文本。"""
    for field in ("content", "text", "message", "body", "prompt"):
        val = msg.get(field)
        if val is None:
            continue
        if isinstance(val, str) and val.strip():
            return val.strip()
        if isinstance(val, dict):
            for sub in ("text", "content", "summary"):
                if isinstance(val.get(sub), str) and val[sub].strip():
                    return val[sub].strip()
            if isinstance(val.get("data"), dict):
                summ = val["data"].get("summary")
                if isinstance(summ, str) and summ.strip():
                    return summ.strip()
        if isinstance(val, list):
            chunks: list[str] = []
            for part in val:
                if isinstance(part, dict):
                    if part.get("type") == "text" and part.get("text"):
                        chunks.append(str(part["text"]))
                    elif part.get("text"):
                        chunks.append(str(part["text"]))
            joined = "\n".join(chunks).strip()
            if joined:
                return joined
    return ""


def _collect_tool_names_from_session(session: dict) -> list[str]:
    names: list[str] = []
    for msg in session.get("messages") or []:
        if not isinstance(msg, dict):
            continue
        for key in ("toolName", "tool_name", "name"):
            n = msg.get(key)
            if isinstance(n, str) and n and n not in names:
                names.append(n)
        tools = msg.get("tools") or msg.get("toolCalls") or []
        if isinstance(tools, list):
            for t in tools:
                if isinstance(t, dict):
                    tn = t.get("name") or t.get("toolName")
                    if isinstance(tn, str) and tn and tn not in names:
                        names.append(tn)
    return names


# ─────────────────────────────────────────────────────────────────
# Trajectory adapters
# ─────────────────────────────────────────────────────────────────


def _adapt_trae_ide_session_json(content: str, metadata: dict) -> tuple[str, dict]:
    """单条 Trae IDE chat session（JSON 对象）→ markdown。"""
    session = json.loads(content) if isinstance(content, str) else content
    if not isinstance(session, dict):
        raise ValueError("trae IDE session must be a JSON object")

    workspace_folder = metadata.get("workspace_folder") or ""
    title = (
        session.get("title")
        or session.get("name")
        or session.get("sessionId")
        or "Trae chat"
    )

    lines: list[str] = ["# Trae IDE Session", ""]
    if workspace_folder:
        lines.append(f"**workspace**: `{workspace_folder}`")
        lines.append("")
    if title:
        lines.append(f"**title**: {title}")
        lines.append("")

    messages = session.get("messages") or []
    first_user = ""
    timeline: list[dict] = []
    t = 0

    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role") or "unknown"
        body = _message_text(msg)
        if not body:
            continue
        if role == "user" and not first_user:
            first_user = body[:500]
        timeline.append({"t": t, "role": role, "content": body[:2000]})
        t += 1

    if first_user:
        lines.extend(["## Initial Query", "", first_user, ""])

    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role") or "unknown"
        body = _message_text(msg)
        if not body:
            continue
        if role == "user":
            lines.append("## User")
        elif role == "assistant":
            lines.append("## Assistant")
        else:
            lines.append(f"## {str(role).capitalize()}")
        lines.append("")
        lines.append(body)
        lines.append("")

    md = "\n".join(lines)
    meta = dict(metadata)
    meta.setdefault("source", "trae_ide_session_json")
    meta.setdefault("category", "trae_ide_session")
    meta["timeline"] = timeline
    meta["tool_names"] = _collect_tool_names_from_session(session)
    meta["total_turns"] = len(timeline)
    if first_user:
        meta.setdefault("query", first_user)
    sid = session.get("sessionId") or session.get("id")
    if sid:
        meta.setdefault("session_id", str(sid))
    return md, meta


def _adapt_trae_agent_trajectory_json(content: str, metadata: dict) -> tuple[str, dict]:
    """Trae Agent CLI ``trajectory_*.json`` → markdown。"""
    data = json.loads(content)
    task = data.get("task") or ""
    lines: list[str] = ["# Trae Agent Trajectory", ""]
    if task:
        lines.append("## Initial Query")
        lines.append("")
        lines.append(str(task))
        lines.append("")

    tool_names: list[str] = []
    timeline: list[dict] = []
    t = 0

    for interaction in data.get("llm_interactions") or []:
        if not isinstance(interaction, dict):
            continue
        for msg in interaction.get("input_messages") or []:
            if not isinstance(msg, dict):
                continue
            role = msg.get("role", "unknown")
            body = msg.get("content")
            if isinstance(body, str) and body.strip():
                timeline.append({"t": t, "role": role, "content": body[:2000]})
                t += 1
                lines.append(f"## {str(role).capitalize()}")
                lines.append("")
                lines.append(body)
                lines.append("")
        resp = interaction.get("response") or {}
        if isinstance(resp, dict):
            body = resp.get("content")
            if isinstance(body, str) and body.strip():
                timeline.append({"t": t, "role": "assistant", "content": body[:2000]})
                t += 1
                lines.append("## Assistant")
                lines.append("")
                lines.append(body)
                lines.append("")
            for tc in resp.get("tool_calls") or []:
                if isinstance(tc, dict):
                    name = tc.get("name")
                    if isinstance(name, str) and name and name not in tool_names:
                        tool_names.append(name)

    for step in data.get("agent_steps") or []:
        if not isinstance(step, dict):
            continue
        for tc in step.get("tool_calls") or []:
            if isinstance(tc, dict):
                name = tc.get("name")
                if isinstance(name, str) and name and name not in tool_names:
                    tool_names.append(name)

    md = "\n".join(lines)
    meta = dict(metadata)
    meta.setdefault("source", "trae_agent_trajectory_json")
    meta.setdefault("category", "trae_agent_session")
    meta["timeline"] = timeline
    meta["tool_names"] = tool_names
    meta["total_turns"] = len(timeline)
    if task:
        meta.setdefault("query", str(task)[:500])
    if data.get("model"):
        meta.setdefault("model", data["model"])
    return md, meta


# ─────────────────────────────────────────────────────────────────
# TraeIngester — workspaceStorage + CLI trajectories
# ─────────────────────────────────────────────────────────────────


class TraeIngester:
    """扫 Trae IDE ``state.vscdb`` 与 Trae Agent CLI JSON 轨迹，桥接到 xskill。"""

    def __init__(
        self,
        *,
        target_traj_dir: Path | str,
        home_root: Path | str | None = None,
        poll_interval: float = 10.0,
    ):
        self.target_traj_dir = Path(target_traj_dir)
        self.home_root = Path(home_root) if home_root else Path.home()
        self.poll_interval = poll_interval
        self._seen: set[str] = set()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._stats = {
            "polls": 0, "ingested": 0, "errors": 0, "last_poll": None,
        }

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._seen = _scan_seen_sessions(self.target_traj_dir)
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="xskill-trae-ingester",
        )
        self._thread.start()
        logger.info(
            "TraeIngester started (target=%s, interval=%.1fs, pre-seen=%d)",
            self.target_traj_dir, self.poll_interval, len(self._seen),
        )

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=self.poll_interval + 5)
        logger.info("TraeIngester stopped")

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def stats(self) -> dict:
        return {**self._stats, "seen_sessions": len(self._seen), "running": self.is_running}

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                n = len(self.scan_and_bridge(seen_sessions=self._seen))
                self._stats["polls"] += 1
                self._stats["last_poll"] = time.time()
                if n:
                    self._stats["ingested"] += n
                    logger.info("TraeIngester: bridged %d new session(s)", n)
            except Exception:
                self._stats["errors"] += 1
                logger.exception("TraeIngester scan error")
            self._stop.wait(self.poll_interval)

    def scan_and_bridge(
        self,
        *,
        target_traj_dir: Path | None = None,
        home_root: Path | None = None,
        seen_sessions: Optional[set[str]] = None,
    ) -> list[dict]:
        target = Path(target_traj_dir) if target_traj_dir else self.target_traj_dir
        home = Path(home_root) if home_root else self.home_root
        target.mkdir(parents=True, exist_ok=True)
        seen = seen_sessions if seen_sessions is not None else set()
        submitted: list[dict] = []

        submitted.extend(self._bridge_workspace_storage(target, home, seen))
        submitted.extend(self._bridge_agent_trajectories(target, home, seen))
        return submitted

    def _bridge_workspace_storage(
        self, target: Path, home: Path, seen: set[str],
    ) -> list[dict]:
        out: list[dict] = []
        for ws_root in _trae_workspace_storage_roots(home):
            if not ws_root.is_dir():
                continue
            for ws_dir in sorted(ws_root.iterdir()):
                if not ws_dir.is_dir():
                    continue
                db_path = ws_dir / "state.vscdb"
                if not db_path.is_file():
                    continue
                folder = _read_workspace_folder(ws_dir)
                try:
                    conn = _open_vscdb_readonly(db_path)
                except sqlite3.Error:
                    logger.debug("cannot open %s", db_path, exc_info=True)
                    continue
                try:
                    blob, used_key = _query_chat_blob(conn)
                finally:
                    conn.close()
                if not blob or not used_key:
                    continue
                for session in _sessions_from_chat_blob(blob, used_key):
                    sid = (
                        session.get("sessionId")
                        or session.get("id")
                        or session.get("title")
                    )
                    if not sid:
                        continue
                    dedup_key = f"ide:{ws_dir.name}:{sid}"
                    if dedup_key in seen:
                        continue
                    messages = session.get("messages") or []
                    if not messages:
                        continue
                    meta = {
                        "workspace_id": ws_dir.name,
                        "workspace_folder": folder,
                        # 落盘 dedup_key（而非裸 sid），_scan_seen_sessions
                        # 重启重建 seen 集时才能与下一轮 poll 的 key 对齐，
                        # 否则每次重启首轮都会重桥同一 session、虚增计数。
                        # 与 CLI 路径（_bridge_agent_trajectories）保持一致。
                        "session_id": dedup_key,
                        "source_vscdb": str(db_path),
                        "chat_store_key": used_key,
                    }
                    traj_id = self._make_traj_id(folder, str(sid), prefix="traj_trae_")
                    payload = json.dumps(session, ensure_ascii=False)
                    result = submit_trajectory(
                        content=payload,
                        format="trae_ide_session_json",
                        traj_id=traj_id,
                        traj_dir=target,
                        metadata=meta,
                    )
                    result["session_id"] = dedup_key
                    result["ecosystem"] = "trae"
                    out.append(result)
                    seen.add(dedup_key)
        return out

    def _bridge_agent_trajectories(
        self, target: Path, home: Path, seen: set[str],
    ) -> list[dict]:
        out: list[dict] = []
        for traj_dir in _trae_agent_trajectory_roots(home):
            for json_path in sorted(traj_dir.glob("trajectory_*.json")):
                dedup_key = f"cli:{json_path.resolve()}"
                if dedup_key in seen:
                    continue
                try:
                    content = json_path.read_text(encoding="utf-8")
                except OSError:
                    continue
                if not content.strip():
                    continue
                try:
                    data = json.loads(content)
                except json.JSONDecodeError:
                    continue
                task = data.get("task") or json_path.stem
                traj_id = self._make_traj_id(
                    json_path.parent.name, _sanitize_for_filename(str(task), 16) or json_path.stem,
                    prefix="traj_trae_cli_",
                )
                result = submit_trajectory(
                    content=content,
                    format="trae_agent_trajectory_json",
                    traj_id=traj_id,
                    traj_dir=target,
                    metadata={"source_json": str(json_path), "session_id": dedup_key},
                )
                result["session_id"] = dedup_key
                result["ecosystem"] = "trae"
                out.append(result)
                seen.add(dedup_key)
        return out

    @staticmethod
    def _make_traj_id(folder: str, sid: str, *, prefix: str) -> str:
        project = _sanitize_for_filename(Path(folder).name if folder else "", 32) or "unknown"
        sid_short = _sanitize_for_filename(sid, 8) or "nosid"
        return f"{prefix}{project}_{sid_short}"


def ingest_trae_sessions(
    target_traj_dir: Path | str,
    *,
    home_root: Path | str | None = None,
    seen_sessions: Optional[set[str]] = None,
) -> list[dict]:
    """一次性桥接 Trae IDE / Agent 会话到 xskill 轨迹目录。"""
    return TraeIngester(
        target_traj_dir=target_traj_dir,
        home_root=home_root,
    ).scan_and_bridge(seen_sessions=seen_sessions)
