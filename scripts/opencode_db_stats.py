#!/usr/bin/env python3
"""
opencode_db_stats.py — opencode SQLite DB 轨迹与统计信息检查脚本

跨平台（Windows / Linux / macOS）。仅依赖 Python 标准库。
以只读模式打开 opencode.db（不会干扰正在运行的 opencode）。

Usage:
    python opencode_db_stats.py                # 自动定位
    python opencode_db_stats.py --db PATH      # 显式指定 db 路径
    python opencode_db_stats.py --json         # 以 JSON 输出
    python opencode_db_stats.py --top 10       # 列出最近 N 条会话
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path


# ---------- 定位 opencode.db ----------

def candidate_paths() -> list[Path]:
    """按平台返回 opencode.db 的候选位置（不保证存在）。

    依据 opencode 官方 troubleshooting 文档：
      - Linux / macOS : ~/.local/share/opencode/
      - Windows       : %USERPROFILE%\\.local\\share\\opencode  (opencode 在
        Windows 上也沿用 Linux XDG 风格，不走 %APPDATA%；见 issue sst/opencode#8235)
      - 所有平台均会先尊重 $XDG_DATA_HOME（如有）。
    其余路径仅作兜底（自定义环境 / 旧版本）。
    """
    home = Path.home()
    cands: list[Path] = []

    # 1) XDG_DATA_HOME（所有平台优先）
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        cands.append(Path(xdg) / "opencode" / "opencode.db")

    # 2) 平台默认
    cands.append(home / ".local" / "share" / "opencode" / "opencode.db")

    # 3) 兜底
    if sys.platform.startswith("win"):
        # 极少数情况下可能走原生 Windows 目录，作为兜底
        for env in ("LOCALAPPDATA", "APPDATA"):
            v = os.environ.get(env)
            if v:
                cands.append(Path(v) / "opencode" / "opencode.db")
    elif sys.platform == "darwin":
        cands.append(home / "Library" / "Application Support" / "opencode" / "opencode.db")

    cands.append(home / ".opencode" / "opencode.db")

    # 去重保序
    seen: set[str] = set()
    uniq: list[Path] = []
    for p in cands:
        key = str(p)
        if key not in seen:
            seen.add(key)
            uniq.append(p)
    return uniq


def locate_db(explicit: str | None) -> Path:
    if explicit:
        p = Path(explicit).expanduser()
        if not p.exists():
            raise FileNotFoundError(f"指定的 db 文件不存在: {p}")
        return p
    for c in candidate_paths():
        if c.exists():
            return c
    raise FileNotFoundError(
        "未找到 opencode.db，已检查:\n  " + "\n  ".join(str(c) for c in candidate_paths())
    )


# ---------- 工具 ----------

def open_ro(path: Path) -> sqlite3.Connection:
    """只读打开 SQLite 数据库（避免影响在跑的 opencode）。"""
    uri = f"file:{path.as_posix()}?mode=ro"
    return sqlite3.connect(uri, uri=True)


def fmt_ts(ms: int | None) -> str | None:
    """opencode 的 time_* 字段是毫秒级 unix 时间。"""
    if ms is None:
        return None
    try:
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone().isoformat(timespec="seconds")
    except (OverflowError, OSError, ValueError):
        return str(ms)


def _pretty_model(raw):
    """opencode 的 session.model 是 JSON blob，取 providerID/id 拼出可读名。"""
    if raw is None:
        return "(none)"
    try:
        obj = json.loads(raw)
        if isinstance(obj, dict) and "id" in obj:
            provider = obj.get("providerID") or obj.get("provider") or ""
            return f"{provider}/{obj['id']}" if provider else obj["id"]
    except (json.JSONDecodeError, TypeError):
        pass
    return str(raw)


def fmt_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def col_exists(conn: sqlite3.Connection, table: str, col: str) -> bool:
    for r in conn.execute(f"PRAGMA table_info({table})"):
        if r[1] == col:
            return True
    return False


# ---------- 统计 ----------

def collect_stats(db_path: Path, top: int) -> dict:
    size = db_path.stat().st_size
    info: dict = {
        "db_path": str(db_path),
        "db_size_bytes": size,
        "db_size_human": fmt_size(size),
    }

    with open_ro(db_path) as conn:
        conn.row_factory = sqlite3.Row

        # 总行数
        counts = {}
        for t in ("session", "message", "part", "project", "workspace", "todo",
                  "event", "session_message", "session_share", "permission"):
            if table_exists(conn, t):
                counts[t] = conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
        info["counts"] = counts

        if not table_exists(conn, "session"):
            info["warning"] = "session 表不存在，无法统计轨迹"
            return info

        # 时间范围
        row = conn.execute(
            "SELECT min(time_created) AS first, max(time_created) AS last,"
            "       max(time_updated) AS last_update FROM session"
        ).fetchone()
        info["session_time_range"] = {
            "first_created": fmt_ts(row["first"]),
            "last_created": fmt_ts(row["last"]),
            "last_updated": fmt_ts(row["last_update"]),
        }

        # 聚合: token / cost
        agg_cols = []
        for c in ("cost", "tokens_input", "tokens_output", "tokens_reasoning",
                  "tokens_cache_read", "tokens_cache_write"):
            if col_exists(conn, "session", c):
                agg_cols.append(c)
        if agg_cols:
            select = ", ".join(f"COALESCE(SUM({c}),0) AS {c}" for c in agg_cols)
            row = conn.execute(f"SELECT {select} FROM session").fetchone()
            info["totals"] = {c: row[c] for c in agg_cols}

        # 每个 project 的 session 数
        if table_exists(conn, "project"):
            rows = conn.execute(
                "SELECT p.id, p.name, p.worktree, count(s.id) AS n_sessions "
                "FROM project p LEFT JOIN session s ON s.project_id = p.id "
                "GROUP BY p.id ORDER BY n_sessions DESC"
            ).fetchall()
            info["projects"] = [
                {
                    "id": r["id"],
                    "name": r["name"] or "",
                    "worktree": r["worktree"],
                    "sessions": r["n_sessions"],
                }
                for r in rows
            ]

        # agent / model 分布
        if col_exists(conn, "session", "agent"):
            rows = conn.execute(
                "SELECT COALESCE(agent,'(none)') AS k, count(*) AS n "
                "FROM session GROUP BY agent ORDER BY n DESC"
            ).fetchall()
            info["by_agent"] = [{"agent": r["k"], "sessions": r["n"]} for r in rows]
        if col_exists(conn, "session", "model"):
            rows = conn.execute(
                "SELECT model, count(*) AS n FROM session GROUP BY model ORDER BY n DESC"
            ).fetchall()
            info["by_model"] = [
                {"model": _pretty_model(r["model"]), "sessions": r["n"]} for r in rows
            ]

        # 每条 session 的 message 数 (平均/中位/最大)
        if table_exists(conn, "message"):
            rows = conn.execute(
                "SELECT session_id, count(*) AS n FROM message GROUP BY session_id"
            ).fetchall()
            if rows:
                ns = sorted(r["n"] for r in rows)
                info["messages_per_session"] = {
                    "min": ns[0],
                    "median": ns[len(ns) // 2],
                    "max": ns[-1],
                    "avg": round(sum(ns) / len(ns), 2),
                    "sessions_with_messages": len(ns),
                    "empty_sessions": counts.get("session", 0) - len(ns),
                }

        # 最近 N 条 session
        if top > 0:
            extra_cols = [c for c in ("agent", "model", "cost",
                                       "tokens_input", "tokens_output")
                          if col_exists(conn, "session", c)]
            sel = ["id", "title", "time_created", "time_updated", "project_id"] + extra_cols
            rows = conn.execute(
                f"SELECT {', '.join(sel)} FROM session "
                f"ORDER BY time_updated DESC LIMIT ?",
                (top,),
            ).fetchall()
            info["recent_sessions"] = [
                {
                    **{k: r[k] for k in sel if k not in ("time_created", "time_updated")},
                    "time_created": fmt_ts(r["time_created"]),
                    "time_updated": fmt_ts(r["time_updated"]),
                }
                for r in rows
            ]

    return info


# ---------- 输出 ----------

def print_human(info: dict) -> None:
    print(f"opencode DB : {info['db_path']}")
    print(f"  size      : {info['db_size_human']} ({info['db_size_bytes']} bytes)")
    if "warning" in info:
        print(f"  ⚠ {info['warning']}")
        return

    c = info["counts"]
    print()
    print(f"轨迹（session） : {c.get('session', 0)}")
    print(f"消息（message） : {c.get('message', 0)}")
    print(f"分片（part）    : {c.get('part', 0)}")
    print(f"项目（project） : {c.get('project', 0)}")
    if "workspace" in c:
        print(f"workspace      : {c['workspace']}")
    if "todo" in c:
        print(f"todo           : {c['todo']}")

    tr = info["session_time_range"]
    print()
    print("时间范围:")
    print(f"  最早 created : {tr['first_created']}")
    print(f"  最新 created : {tr['last_created']}")
    print(f"  最近 updated : {tr['last_updated']}")

    if "totals" in info:
        t = info["totals"]
        print()
        print("总用量:")
        if "cost" in t:
            print(f"  cost ($)             : {t['cost']:.4f}")
        if "tokens_input" in t:
            print(f"  tokens input         : {t['tokens_input']:,}")
        if "tokens_output" in t:
            print(f"  tokens output        : {t['tokens_output']:,}")
        if "tokens_reasoning" in t:
            print(f"  tokens reasoning     : {t['tokens_reasoning']:,}")
        if "tokens_cache_read" in t:
            print(f"  tokens cache read    : {t['tokens_cache_read']:,}")
        if "tokens_cache_write" in t:
            print(f"  tokens cache write   : {t['tokens_cache_write']:,}")

    if "messages_per_session" in info:
        m = info["messages_per_session"]
        print()
        print("每轨迹的 message 数:")
        print(f"  min/median/max/avg = {m['min']}/{m['median']}/{m['max']}/{m['avg']}")
        if m["empty_sessions"]:
            print(f"  空轨迹（无 message）: {m['empty_sessions']}")

    if info.get("projects"):
        print()
        print("项目分布:")
        for p in info["projects"]:
            name = p["name"] or "(unnamed)"
            print(f"  [{p['sessions']:>4}] {name}  ({p['worktree']})")

    if info.get("by_agent"):
        print()
        print("agent 分布:")
        for r in info["by_agent"]:
            print(f"  [{r['sessions']:>4}] {r['agent']}")

    if info.get("by_model"):
        print()
        print("model 分布:")
        for r in info["by_model"]:
            print(f"  [{r['sessions']:>4}] {r['model']}")

    if info.get("recent_sessions"):
        print()
        print(f"最近 {len(info['recent_sessions'])} 条轨迹:")
        for s in info["recent_sessions"]:
            title = (s.get("title") or "").strip().replace("\n", " ")
            if len(title) > 60:
                title = title[:57] + "..."
            agent = s.get("agent") or "-"
            model = _pretty_model(s.get("model")) if s.get("model") else "-"
            print(f"  {s['time_updated']}  [{agent} / {model}]  {title}")


def main() -> int:
    ap = argparse.ArgumentParser(description="检查 opencode SQLite DB 的轨迹与统计信息")
    ap.add_argument("--db", help="opencode.db 路径（不指定则自动定位）")
    ap.add_argument("--json", action="store_true", help="以 JSON 输出")
    ap.add_argument("--top", type=int, default=5, help="列出最近 N 条会话（默认 5，0=不列）")
    args = ap.parse_args()

    try:
        db_path = locate_db(args.db)
    except FileNotFoundError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    info = collect_stats(db_path, top=max(args.top, 0))
    if args.json:
        print(json.dumps(info, indent=2, ensure_ascii=False))
    else:
        print_human(info)
    return 0


if __name__ == "__main__":
    sys.exit(main())
