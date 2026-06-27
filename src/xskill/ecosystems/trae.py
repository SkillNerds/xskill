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

TRAE_CN_DIR = ".trae-cn"
TRAE_CN_APP_NAME = "Trae CN"
XSKILL_DIR = ".xskill"

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
    return home / TRAE_CN_DIR, home / ".trae"


def _trae_skills_roots(home: Path) -> list[Path]:
    """返回应写入的 Trae skill 根目录列表（已存在或父目录存在的版本）。"""
    roots: list[Path] = []
    for edition_home in _trae_edition_home_dirs(home):
        skills = edition_home / "skills"
        if edition_home.is_dir() or skills.is_dir():
            roots.append(skills)
    if not roots:
        # 未安装过 Trae 时默认国内路径，便于首次 install 落盘
        roots.append(home / TRAE_CN_DIR / "skills")
    return roots


def _trae_workspace_storage_roots(home: Path) -> list[Path]:
    """各平台 Trae IDE ``User/workspaceStorage`` 候选路径。"""
    roots: list[Path] = []
    if sys.platform == "darwin":
        for app_name in ("Trae", TRAE_CN_APP_NAME):
            roots.append(
                home / "Library" / "Application Support" / app_name
                / "User" / "workspaceStorage"
            )
    elif sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        if appdata:
            for app_name in ("TRAE SOLO CN", TRAE_CN_APP_NAME, "Trae"):
                roots.append(
                    Path(appdata) / app_name / "User" / "workspaceStorage"
                )
    else:
        for cfg_name in ("Trae", TRAE_CN_APP_NAME):
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
                "bridge": (home_root / XSKILL_DIR / "trae_sessions").resolve(),
            }
    for edition_home in _trae_edition_home_dirs(home_root):
        if edition_home.is_dir():
            return {
                "ecosystem": "trae",
                "source": edition_home.resolve(),
                "bridge": (home_root / XSKILL_DIR / "trae_sessions").resolve(),
            }
    if _trae_agent_trajectory_roots(home_root):
        return {
            "ecosystem": "trae",
            "source": _trae_agent_trajectory_roots(home_root)[0].resolve(),
            "bridge": (home_root / XSKILL_DIR / "trae_sessions").resolve(),
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


def _entries_from_mapping(raw: Any) -> dict[str, dict]:
    if not isinstance(raw, dict):
        return {}
    return {str(k): v for k, v in raw.items() if isinstance(v, dict)}


def _entries_from_list(raw: Any) -> dict[str, dict]:
    if not isinstance(raw, list):
        return {}
    return {str(i): x for i, x in enumerate(raw) if isinstance(x, dict)}


def _entries_from_session_list(raw: Any) -> dict[str, dict]:
    entries: dict[str, dict] = {}
    if not isinstance(raw, list):
        return entries
    for i, item in enumerate(raw):
        if isinstance(item, dict):
            sid = item.get("sessionId") or item.get("id") or str(i)
            entries[str(sid)] = item
    return entries


def _entries_from_collection(raw: Any) -> dict[str, dict]:
    if isinstance(raw, dict):
        return _entries_from_mapping(raw)
    return _entries_from_list(raw)


def _memento_entries(chat_data: dict) -> dict[str, dict]:
    lst = chat_data.get("list")
    if isinstance(lst, list):
        return _entries_from_session_list(lst)
    raw = (
        chat_data.get("sessions")
        or chat_data.get("conversations")
        or chat_data.get("entries")
    )
    return _entries_from_mapping(raw)


def _sessions_from_chat_blob(chat_data: dict, used_key: str) -> list[dict]:
    """把 chat store blob 规范成 session dict 列表。"""
    if used_key == "memento/icube-ai-agent-storage":
        entries = _entries_from_session_list(chat_data.get("list"))
    elif used_key == "ChatStore":
        entries = _entries_from_collection(
            chat_data.get("sessions") or chat_data.get("entries")
        )
    elif "memento/icube-ai" in used_key:
        entries = _memento_entries(chat_data)
    else:
        entries = _entries_from_collection(chat_data.get("entries"))
    return list(entries.values())


def _message_dict_text(val: dict) -> str:
    for sub in ("text", "content", "summary"):
        text = val.get(sub)
        if isinstance(text, str) and text.strip():
            return text.strip()
    data = val.get("data")
    if isinstance(data, dict):
        summary = data.get("summary")
        if isinstance(summary, str) and summary.strip():
            return summary.strip()
    return ""


def _message_list_text(val: list) -> str:
    chunks: list[str] = []
    for part in val:
        if isinstance(part, dict) and part.get("text"):
            chunks.append(str(part["text"]))
    return "\n".join(chunks).strip()


def _message_value_text(val: Any) -> str:
    if val is None:
        return ""
    if isinstance(val, str):
        return val.strip()
    if isinstance(val, dict):
        return _message_dict_text(val)
    if isinstance(val, list):
        return _message_list_text(val)
    return ""


def _message_text(msg: dict) -> str:
    """从 Trae IDE 单条 message 对象抽取可读文本。"""
    for field in ("content", "text", "message", "body", "prompt"):
        text = _message_value_text(msg.get(field))
        if text:
            return text
    return ""


def _append_unique_name(names: list[str], value: Any) -> None:
    if isinstance(value, str) and value and value not in names:
        names.append(value)


def _append_tool_call_names(names: list[str], tools: Any) -> None:
    if not isinstance(tools, list):
        return
    for tool in tools:
        if isinstance(tool, dict):
            _append_unique_name(names, tool.get("name") or tool.get("toolName"))


def _append_message_tool_names(names: list[str], msg: dict) -> None:
    for key in ("toolName", "tool_name", "name"):
        _append_unique_name(names, msg.get(key))
    _append_tool_call_names(names, msg.get("tools") or msg.get("toolCalls") or [])


def _collect_tool_names_from_session(session: dict) -> list[str]:
    names: list[str] = []
    for msg in session.get("messages") or []:
        if isinstance(msg, dict):
            _append_message_tool_names(names, msg)
    return names


# ─────────────────────────────────────────────────────────────────
# Trajectory adapters
# ─────────────────────────────────────────────────────────────────


def _load_trae_ide_session(content: str | dict) -> dict:
    session = json.loads(content) if isinstance(content, str) else content
    if not isinstance(session, dict):
        raise ValueError("trae IDE session must be a JSON object")
    return session


def _trae_ide_title(session: dict) -> Any:
    return (
        session.get("title")
        or session.get("name")
        or session.get("sessionId")
        or "Trae chat"
    )


def _append_trae_ide_header(
    lines: list[str], workspace_folder: str, title: Any,
) -> None:
    if workspace_folder:
        lines.append(f"**workspace**: `{workspace_folder}`")
        lines.append("")
    if title:
        lines.append(f"**title**: {title}")
        lines.append("")


def _trae_message_entries(messages: Any) -> list[tuple[Any, str]]:
    entries: list[tuple[Any, str]] = []
    for msg in messages or []:
        if not isinstance(msg, dict):
            continue
        body = _message_text(msg)
        if body:
            entries.append((msg.get("role") or "unknown", body))
    return entries


def _timeline_from_entries(entries: list[tuple[Any, str]]) -> tuple[list[dict], str]:
    first_user = ""
    timeline: list[dict] = []
    for role, body in entries:
        if role == "user" and not first_user:
            first_user = body[:500]
        timeline.append({"t": len(timeline), "role": role, "content": body[:2000]})
    return timeline, first_user


def _append_role_heading(lines: list[str], role: Any) -> None:
    if role == "user":
        lines.append("## User")
    elif role == "assistant":
        lines.append("## Assistant")
    else:
        lines.append(f"## {str(role).capitalize()}")


def _append_message_sections(lines: list[str], entries: list[tuple[Any, str]]) -> None:
    for role, body in entries:
        _append_role_heading(lines, role)
        lines.append("")
        lines.append(body)
        lines.append("")


def _set_trae_ide_metadata(
    meta: dict, session: dict, timeline: list[dict], first_user: str,
) -> None:
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


def _adapt_trae_ide_session_json(content: str, metadata: dict) -> tuple[str, dict]:
    """单条 Trae IDE chat session（JSON 对象）→ markdown。"""
    session = _load_trae_ide_session(content)
    workspace_folder = metadata.get("workspace_folder") or ""

    lines: list[str] = ["# Trae IDE Session", ""]
    _append_trae_ide_header(lines, workspace_folder, _trae_ide_title(session))

    entries = _trae_message_entries(session.get("messages") or [])
    timeline, first_user = _timeline_from_entries(entries)
    if first_user:
        lines.extend(["## Initial Query", "", first_user, ""])

    _append_message_sections(lines, entries)

    md = "\n".join(lines)
    meta = dict(metadata)
    _set_trae_ide_metadata(meta, session, timeline, first_user)
    return md, meta


def _append_initial_query(lines: list[str], task: Any) -> None:
    if task:
        lines.append("## Initial Query")
        lines.append("")
        lines.append(str(task))
        lines.append("")


def _append_timeline_turn(
    lines: list[str], timeline: list[dict], role: Any, body: str,
) -> None:
    timeline.append({"t": len(timeline), "role": role, "content": body[:2000]})
    _append_role_heading(lines, role)
    lines.append("")
    lines.append(body)
    lines.append("")


def _append_agent_input_messages(
    lines: list[str], timeline: list[dict], messages: Any,
) -> None:
    for msg in messages or []:
        if not isinstance(msg, dict):
            continue
        body = msg.get("content")
        if isinstance(body, str) and body.strip():
            _append_timeline_turn(lines, timeline, msg.get("role", "unknown"), body)


def _append_response_tool_names(tool_names: list[str], tool_calls: Any) -> None:
    if not isinstance(tool_calls, list):
        return
    for tool_call in tool_calls:
        if isinstance(tool_call, dict):
            _append_unique_name(tool_names, tool_call.get("name"))


def _append_agent_response(
    lines: list[str], timeline: list[dict], tool_names: list[str], response: Any,
) -> None:
    if not isinstance(response, dict):
        return
    body = response.get("content")
    if isinstance(body, str) and body.strip():
        _append_timeline_turn(lines, timeline, "assistant", body)
    _append_response_tool_names(tool_names, response.get("tool_calls") or [])


def _append_agent_interaction(
    lines: list[str], timeline: list[dict], tool_names: list[str], interaction: Any,
) -> None:
    if not isinstance(interaction, dict):
        return
    _append_agent_input_messages(lines, timeline, interaction.get("input_messages") or [])
    _append_agent_response(
        lines, timeline, tool_names, interaction.get("response") or {},
    )


def _append_agent_step_tool_names(tool_names: list[str], steps: Any) -> None:
    for step in steps or []:
        if isinstance(step, dict):
            _append_response_tool_names(tool_names, step.get("tool_calls") or [])


def _set_trae_agent_metadata(
    meta: dict, data: dict, timeline: list[dict], tool_names: list[str], task: Any,
) -> None:
    meta.setdefault("source", "trae_agent_trajectory_json")
    meta.setdefault("category", "trae_agent_session")
    meta["timeline"] = timeline
    meta["tool_names"] = tool_names
    meta["total_turns"] = len(timeline)
    if task:
        meta.setdefault("query", str(task)[:500])
    if data.get("model"):
        meta.setdefault("model", data["model"])


def _adapt_trae_agent_trajectory_json(content: str, metadata: dict) -> tuple[str, dict]:
    """Trae Agent CLI ``trajectory_*.json`` → markdown。"""
    data = json.loads(content)
    task = data.get("task") or ""
    lines: list[str] = ["# Trae Agent Trajectory", ""]
    _append_initial_query(lines, task)

    tool_names: list[str] = []
    timeline: list[dict] = []

    for interaction in data.get("llm_interactions") or []:
        _append_agent_interaction(lines, timeline, tool_names, interaction)
    _append_agent_step_tool_names(tool_names, data.get("agent_steps") or [])

    md = "\n".join(lines)
    meta = dict(metadata)
    _set_trae_agent_metadata(meta, data, timeline, tool_names, task)
    return md, meta


# ─────────────────────────────────────────────────────────────────
# TraeIngester — workspaceStorage + CLI trajectories
# ─────────────────────────────────────────────────────────────────


def _iter_workspace_vscdbs(home: Path) -> Iterable[tuple[Path, Path]]:
    for ws_root in _trae_workspace_storage_roots(home):
        if not ws_root.is_dir():
            continue
        for ws_dir in sorted(ws_root.iterdir()):
            db_path = ws_dir / "state.vscdb"
            if ws_dir.is_dir() and db_path.is_file():
                yield ws_dir, db_path


def _read_chat_blob_from_db(db_path: Path) -> tuple[dict | None, str]:
    try:
        conn = _open_vscdb_readonly(db_path)
    except sqlite3.Error:
        logger.debug("cannot open %s", db_path, exc_info=True)
        return None, ""
    try:
        return _query_chat_blob(conn)
    finally:
        conn.close()


def _session_identifier(session: dict) -> Any:
    return session.get("sessionId") or session.get("id") or session.get("title")


def _ide_session_metadata(
    ws_dir: Path, folder: str, db_path: Path, used_key: str, dedup_key: str,
) -> dict:
    return {
        "workspace_id": ws_dir.name,
        "workspace_folder": folder,
        # Persist the full dedup key so _scan_seen_sessions can rebuild the same
        # in-memory key after daemon restarts.
        "session_id": dedup_key,
        "source_vscdb": str(db_path),
        "chat_store_key": used_key,
    }


def _iter_agent_trajectory_files(home: Path) -> Iterable[Path]:
    for traj_dir in _trae_agent_trajectory_roots(home):
        yield from sorted(traj_dir.glob("trajectory_*.json"))


def _read_agent_trajectory(json_path: Path) -> tuple[str, dict] | None:
    try:
        content = json_path.read_text(encoding="utf-8")
    except OSError:
        return None
    if not content.strip():
        return None
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        return None
    return content, data


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
        for ws_dir, db_path in _iter_workspace_vscdbs(home):
            out.extend(self._bridge_workspace_db(target, ws_dir, db_path, seen))
        return out

    def _bridge_workspace_db(
        self, target: Path, ws_dir: Path, db_path: Path, seen: set[str],
    ) -> list[dict]:
        folder = _read_workspace_folder(ws_dir)
        blob, used_key = _read_chat_blob_from_db(db_path)
        if not blob or not used_key:
            return []

        out: list[dict] = []
        for session in _sessions_from_chat_blob(blob, used_key):
            result = self._bridge_ide_session(
                target, ws_dir, folder, db_path, used_key, session, seen,
            )
            if result:
                out.append(result)
        return out

    def _bridge_ide_session(
        self,
        target: Path,
        ws_dir: Path,
        folder: str,
        db_path: Path,
        used_key: str,
        session: dict,
        seen: set[str],
    ) -> dict | None:
        sid = _session_identifier(session)
        if not sid:
            return None
        dedup_key = f"ide:{ws_dir.name}:{sid}"
        if dedup_key in seen or not (session.get("messages") or []):
            return None

        result = submit_trajectory(
            content=json.dumps(session, ensure_ascii=False),
            format="trae_ide_session_json",
            traj_id=self._make_traj_id(folder, str(sid), prefix="traj_trae_"),
            traj_dir=target,
            metadata=_ide_session_metadata(
                ws_dir, folder, db_path, used_key, dedup_key,
            ),
        )
        result["session_id"] = dedup_key
        result["ecosystem"] = "trae"
        seen.add(dedup_key)
        return result

    def _bridge_agent_trajectories(
        self, target: Path, home: Path, seen: set[str],
    ) -> list[dict]:
        out: list[dict] = []
        for json_path in _iter_agent_trajectory_files(home):
            result = self._bridge_agent_trajectory_file(target, json_path, seen)
            if result:
                out.append(result)
        return out

    def _bridge_agent_trajectory_file(
        self, target: Path, json_path: Path, seen: set[str],
    ) -> dict | None:
        dedup_key = f"cli:{json_path.resolve()}"
        if dedup_key in seen:
            return None
        parsed = _read_agent_trajectory(json_path)
        if parsed is None:
            return None
        content, data = parsed
        task = data.get("task") or json_path.stem
        traj_id = self._make_traj_id(
            json_path.parent.name,
            _sanitize_for_filename(str(task), 16) or json_path.stem,
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
        seen.add(dedup_key)
        return result

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
