#!/usr/bin/env python3.11
"""一次性回填存量轨迹的用户 agent 模型(批2,Issue #43 关联)。

桥接产物(~/.xskill/<eco>_sessions/traj_*.json)历史上没存 model;但原始 source 有:
  - Claude Code: ~/.claude/projects/**/<session_id>.jsonl 的 assistant `message.model`
  - OpenCode:    ~/.local/share/opencode/opencode.db 的 message.data.model.modelID
  - Codex:       桥接 .json 已有的 `model_provider`(provider 级,best we have)

把抽到的 model 写回每个 traj 的 .json sidecar(discover 读它填 registry.source_model),
并顺手 UPDATE 本机 registry。拿不到的留空(= 后续 stats 里的 'unknown')。幂等。

用法:  python3.11 scripts/backfill_source_model.py
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

HOME = Path.home()
XS = HOME / ".xskill"
CLAUDE_PROJ = HOME / ".claude" / "projects"
OPENCODE_DB = HOME / ".local" / "share" / "opencode" / "opencode.db"
REGISTRY_DB = XS / "registry.db"
# 只处理活动 bridge 目录,跳过 .backup-* / .wiped-* 备份
LIVE_DIRS = ["cc_sessions", "codex_sessions", "opencode_sessions", "ngagent_sessions"]


def _cc_index() -> dict[str, Path]:
    """session_id -> jsonl 路径(CC 原始 session 文件名即 session_id)。"""
    idx: dict[str, Path] = {}
    if CLAUDE_PROJ.is_dir():
        for p in CLAUDE_PROJ.rglob("*.jsonl"):
            idx[p.stem] = p
    return idx


def _cc_model(jsonl: Path) -> str:
    try:
        for line in jsonl.read_text(encoding="utf-8").splitlines():
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            if e.get("type") == "assistant":
                m = (e.get("message") or {}).get("model")
                if m:
                    return str(m)
    except OSError:
        pass
    return ""


def _oc_model_map() -> dict[str, str]:
    """session_id -> modelID(从 opencode.db 一次性建表)。"""
    out: dict[str, str] = {}
    if not OPENCODE_DB.is_file():
        return out
    try:
        conn = sqlite3.connect(f"file:{OPENCODE_DB}?mode=ro&immutable=1", uri=True)
        for sid, data in conn.execute("SELECT session_id, data FROM message"):
            if sid in out:
                continue
            try:
                m = json.loads(data).get("model")
            except (json.JSONDecodeError, TypeError):
                m = None
            if isinstance(m, dict) and m.get("modelID"):
                out[sid] = m["modelID"]
        conn.close()
    except sqlite3.Error:
        pass
    return out


def main() -> int:
    cc_idx = _cc_index()
    oc_map = _oc_model_map()
    reg = sqlite3.connect(REGISTRY_DB) if REGISTRY_DB.is_file() else None
    if reg is not None:
        try:
            reg.execute("ALTER TABLE trajectories ADD COLUMN source_model TEXT")
        except sqlite3.OperationalError:
            pass   # 列已存在(get_connection 迁移过 / 本脚本重跑)
    stats: dict[str, list[int]] = {}   # eco -> [filled, already, unknown]

    for eco in LIVE_DIRS:
        d = XS / eco
        if not d.is_dir():
            continue
        s = stats.setdefault(eco, [0, 0, 0])
        for jf in sorted(d.glob("traj_*.json")):
            try:
                meta = json.loads(jf.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if meta.get("model"):
                s[1] += 1
                continue
            sid = meta.get("session_id", "")
            model = ""
            if eco == "cc_sessions" and sid in cc_idx:
                model = _cc_model(cc_idx[sid])
            elif eco == "opencode_sessions":
                model = oc_map.get(sid, "")
            elif eco == "codex_sessions":
                model = meta.get("model_provider", "")
            if not model:
                s[2] += 1
                continue
            meta["model"] = model
            jf.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
            if reg is not None:
                reg.execute("UPDATE trajectories SET source_model=? WHERE filename=?",
                            (model, jf.with_suffix(".md").name))
            s[0] += 1
    if reg is not None:
        reg.commit(); reg.close()

    print("=== 回填结果(filled / already / unknown) ===")
    for eco, (f, a, u) in stats.items():
        print(f"  {eco:<20} filled={f}  already={a}  unknown={u}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
