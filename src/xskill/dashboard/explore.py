"""dashboard/explore.py —— 轨迹/原子详情、skill 血缘、管线进度、用户连接状态。

全部纯读（registry + atom 文件 + skill 目录 + team_clients.db），无 FastAPI 依赖，
可单测。断链（文件被清理）一律显式标注 ``source_cleaned``，不静默省略（D6）。
"""
from __future__ import annotations

import sqlite3
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from xskill._sqlite_connect import connect_with_lock
from xskill.pipeline.registry import pooled_connection
from xskill.dashboard.metrics import _resolve_local_root, load_usage_records


class TrajExplorer:
    """轨迹/原子详情页数据源（图②）。traj_id = 文件名 stem。"""

    def __init__(self, db_path: Optional[Path], skill_dir: Optional[Path]):
        self._db = db_path
        self._skill_dir = skill_dir
        from xskill.config import get_registry_db_path
        self._db_dir = (Path(db_path).parent if db_path
                        else get_registry_db_path().parent)

    def _traj_row(self, traj_id: str) -> dict:
        with pooled_connection(self._db) as conn:
            row = conn.execute(
                "SELECT t.*, w.path wpath, w.label wlabel, w.ecosystem eco"
                " FROM trajectories t JOIN watch_dirs w ON t.watch_dir_id=w.id"
                " WHERE t.filename=? OR t.filename=?",
                (traj_id, f"{traj_id}.md"),
            ).fetchone()
        if row is None:
            raise KeyError(f"trajectory not found: {traj_id}")
        return dict(row)

    def traj_detail(self, traj_id: str) -> dict:
        r = self._traj_row(traj_id)
        return {
            "traj_id": traj_id,
            "filename": r["filename"],
            "status": r["status"],
            "harness": r.get("source_harness") or "",
            "model": r.get("source_model") or "",
            "user": r.get("wlabel") or "(local)",
            "ecosystem": r.get("eco") or "",
            "atoms": r.get("tasks_extracted") or 0,
            "discovered_at": r.get("discovered_at") or "",
        }

    def _store_root(self, traj_id: str) -> Path:
        r = self._traj_row(traj_id)
        return _resolve_local_root(r["wpath"], self._db_dir)

    def traj_atoms(self, traj_id: str) -> list[dict]:
        """该轨迹全部 atom，按链表序（pre/post_atom_id）。

        链表断裂（atom 文件被部分清理）时按 offset_start 补序，且每条带
        ``chain`` 标记（"linked"/"orphan"）——不静默假装链是完整的。
        """
        from xskill.pipeline.atom import AtomTaskStore
        atoms = AtomTaskStore(root=self._store_root(traj_id)).list_by_traj(traj_id)
        by_id = {a.atom_id: a for a in atoms}
        # 找链头：pre 为空或 pre 不在集合里
        heads = [a for a in atoms
                 if not a.pre_atom_id or a.pre_atom_id not in by_id]
        ordered: list = []
        seen: set[str] = set()
        for head in sorted(heads, key=lambda a: a.offset_start):
            cur = head
            while cur is not None and cur.atom_id not in seen:
                ordered.append(cur)
                seen.add(cur.atom_id)
                cur = by_id.get(cur.post_atom_id) if cur.post_atom_id else None
        orphan_ids = {a.atom_id for a in atoms} - seen
        ordered += sorted((by_id[i] for i in orphan_ids),
                          key=lambda a: a.offset_start)
        out = []
        for a in ordered:
            d = asdict(a)
            d.pop("raw_segment", None)
            d.pop("context_prefix", None)
            d["chain"] = "orphan" if a.atom_id in orphan_ids else "linked"
            # 去向随列表返回，供 traj—atom—skill 关系图一次取数（图②右侧）
            d["destinations"] = self._atom_destinations(a.atom_id)
            out.append(d)
        return out

    def atom_detail(self, traj_id: str, atom_id: str) -> dict:
        from xskill.pipeline.atom import AtomTaskStore
        store = AtomTaskStore(root=self._store_root(traj_id))
        atom = store.load(atom_id)  # 不存在 → 抛错（router 转 404），不造空对象
        d = asdict(atom)
        d.pop("raw_segment", None)
        d.pop("context_prefix", None)
        # 原文切片：offset 是 1-based 行号半开区间 [start, end)
        md = self._store_root(traj_id) / f"{traj_id}.md"
        if md.is_file():
            lines = md.read_text(encoding="utf-8").splitlines()
            seg = lines[max(atom.offset_start - 1, 0):max(atom.offset_end - 1, 0)]
            text = "\n".join(seg)
            d["raw"] = text[:8000]
            d["raw_total_chars"] = len(text)
            d["raw_status"] = "ok"
        else:
            d["raw"] = None
            d["raw_total_chars"] = 0
            d["raw_status"] = "source_cleaned"  # 轨迹原文已清理，显式标注
        d["destinations"] = self._atom_destinations(atom_id)
        return d

    def _atom_destinations(self, atom_id: str) -> list[dict]:
        """该 atom 的去向：进入了哪些 skill（adoption 事件 + 在途 candidates）。"""
        out: list[dict] = []
        with pooled_connection(self._db) as conn:
            rows = conn.execute(
                "SELECT skill, weightscore, ts FROM atom_adoption WHERE atom_id=?"
                " ORDER BY ts", (atom_id,)).fetchall()
        for r in rows:
            out.append({"skill": r["skill"], "weightscore": r["weightscore"],
                        "state": "adopted", "ts": r["ts"]})
        if self._skill_dir and Path(self._skill_dir).is_dir():
            from xskill.skill.candidates import load_candidates
            adopted = {o["skill"] for o in out}
            for d in sorted(Path(self._skill_dir).iterdir()):
                if not d.is_dir() or d.name.startswith(".") or d.name in adopted:
                    continue
                for c in load_candidates(d).get("candidates", []):
                    if c.get("atom_id") == atom_id:
                        out.append({"skill": d.name,
                                    "weightscore": c.get("weightscore"),
                                    "state": "pending", "ts": ""})
        return out


def skill_lineage(skill_dir: Path, name: str,
                  db_path: Optional[Path] = None) -> dict:
    """skill 血缘（图①下半区）：贡献原子（adoption 事件 + 在途 candidates）
    及其用户/模型归因。原子文件已清理的行标 ``source_cleaned``，保留可得字段。
    """
    sub = Path(skill_dir) / name
    if not sub.is_dir():
        raise KeyError(f"skill not found: {name}")
    with pooled_connection(db_path) as conn:
        adoption = conn.execute(
            "SELECT atom_id, weightscore, ts FROM atom_adoption WHERE skill=?"
            " ORDER BY ts", (name,)).fetchall()
        wd_rows = conn.execute(
            "SELECT t.filename fn, t.source_model model, w.label label,"
            " w.path wpath FROM trajectories t"
            " JOIN watch_dirs w ON t.watch_dir_id=w.id").fetchall()
    from xskill.config import get_registry_db_path
    db_dir = Path(db_path).parent if db_path else get_registry_db_path().parent
    traj_info: dict[str, dict] = {}
    for r in wd_rows:
        stem = r["fn"][:-3] if r["fn"].endswith(".md") else r["fn"]
        traj_info[stem] = {"user": r["label"] or "(local)",
                           "model": r["model"] or "",
                           "root": _resolve_local_root(r["wpath"], db_dir)}
    from xskill.skill.candidates import load_candidates
    from xskill.dashboard.metrics import _traj_of_atom
    entries: dict[str, dict] = {}
    for r in adoption:
        entries[r["atom_id"]] = {"atom_id": r["atom_id"], "state": "adopted",
                                 "weightscore": r["weightscore"], "ts": r["ts"]}
    for c in load_candidates(sub).get("candidates", []):
        aid = c.get("atom_id") or ""
        if aid and aid not in entries:
            entries[aid] = {"atom_id": aid, "state": "pending",
                            "weightscore": c.get("weightscore"), "ts": ""}
    atoms_out: list[dict] = []
    by_user: dict[str, int] = {}
    by_model: dict[str, int] = {}
    for aid, e in entries.items():
        traj_id = _traj_of_atom(aid)
        info = traj_info.get(traj_id, {})
        user = info.get("user", "(unknown)")
        model = info.get("model", "")
        intent, cleaned = "", True
        root = info.get("root")
        if root is not None:
            af = Path(root) / traj_id / "tasks" / f"{aid}.json"
            if af.is_file():
                import json as _json
                try:
                    intent = _json.loads(
                        af.read_text(encoding="utf-8")).get("intent", "")
                    cleaned = False
                except (OSError, ValueError):
                    cleaned = True
                if not model:
                    model = ""
        atoms_out.append({**e, "traj_id": traj_id, "user": user,
                          "model": model or "unknown", "intent": intent,
                          "source_cleaned": cleaned})
        by_user[user] = by_user.get(user, 0) + 1
        by_model[model or "unknown"] = by_model.get(model or "unknown", 0) + 1
    atoms_out.sort(key=lambda a: -(a["weightscore"] or 0))
    # 版本 UX 概览同源自使用记录（供血缘页头）
    usage = [u for u in load_usage_records(skill_dir) if u["skill"] == name]
    scores = [u["score"] for u in usage if u["score"] is not None]
    return {
        "skill": name,
        "atoms": atoms_out,
        "by_user": sorted(({"user": u, "atoms": n} for u, n in by_user.items()),
                          key=lambda d: -d["atoms"]),
        "by_model": sorted(({"model": m, "atoms": n} for m, n in by_model.items()),
                           key=lambda d: -d["atoms"]),
        "uses": len(usage),
        "avg_ux": round(sum(scores) / len(scores), 2) if scores else None,
    }


def skill_ux_daily(skill_dir: Path, name: str) -> list[dict]:
    """按日 × side 的 ux 均值与样本数（得分趋势折线数据源）。"""
    agg: dict[tuple[str, str], list] = {}
    for u in load_usage_records(skill_dir):
        if u["skill"] != name or u["score"] is None:
            continue
        day = (u["scored_at"] or "")[:10]
        if not day:
            continue
        s = agg.setdefault((day, u["side"]), [0.0, 0])
        s[0] += u["score"]
        s[1] += 1
    out = [{"date": day, "side": side, "avg_ux": round(ssum / n, 2), "n": n}
           for (day, side), (ssum, n) in agg.items()]
    out.sort(key=lambda d: (d["date"], d["side"]))
    return out


def pipeline_progress(db_path: Optional[Path],
                      skill_dir: Optional[Path]) -> dict:
    """总览页蒸馏管线进度（图⑥）：状态计数 + 冷启动信号 + 候选孵化进度。"""
    with pooled_connection(db_path) as conn:
        rows = dict(conn.execute(
            "SELECT status, COUNT(*) FROM trajectories GROUP BY status"
        ).fetchall())
    stages = {
        "pending_split": rows.get("discovered", 0) + rows.get("meta_done", 0),
        "splitting": rows.get("splitting", 0),
        "clustering": rows.get("split_done", 0) + rows.get("indexed", 0)
                      + rows.get("clustering", 0),
        "done": rows.get("done", 0),
        "error": rows.get("error", 0),
    }
    # 冷启动：signal 文件存在才渲染该区块（不存在 → None，前端整块不出现）。
    # home 从 registry.db 同级推（独立只读实例也指向数据所在的 XSKILL_HOME）。
    cold = None
    from xskill.config import get_registry_db_path
    from xskill.pipeline.cold_start import ColdStartSignal
    home = Path(db_path).parent if db_path else get_registry_db_path().parent
    if ColdStartSignal(xskill_home=home).exists:
        cold = {"active": True}
    candidates_out: list[dict] = []
    if skill_dir and Path(skill_dir).is_dir():
        from xskill.skill.candidates import (
            load_candidates, ATOM_PROMOTION_THRESHOLD)
        for d in sorted(Path(skill_dir).iterdir()):
            if not d.is_dir() or d.name.startswith("."):
                continue
            cands = load_candidates(d).get("candidates", [])
            if not cands:
                continue
            total = sum(int(c.get("weightscore") or 0) for c in cands)
            candidates_out.append({
                "skill": d.name,
                "weightscore": total,
                "threshold": ATOM_PROMOTION_THRESHOLD,
                "atoms": len(cands),
                "progress": round(min(total / ATOM_PROMOTION_THRESHOLD, 1.0), 2),
            })
        candidates_out.sort(key=lambda c: -c["progress"])
    return {"stages": stages, "cold_start": cold, "candidates": candidates_out}


def users_status(db_path: Optional[Path], *,
                 online_window_s: int = 600) -> dict:
    """用户连接状态看板（图⑧，P1 读侧）。

    在线 = team_clients.db 的 last_seen 距今 ≤ online_window_s（约 2 个同步
    周期）。team_clients.db 不存在（非 team server 部署）→ 无连接状态可言，
    users 为空列表 + reason 显式说明。版本列 P2 版本上报后才渲染。
    """
    from xskill.config import get_registry_db_path
    db_dir = Path(db_path).parent if db_path else get_registry_db_path().parent
    clients_db = db_dir / "team_clients.db"
    if not clients_db.is_file():
        return {"users": [], "online": 0,
                "reason": "no team_clients.db (not a team server)"}
    cconn = connect_with_lock(sqlite3.connect, str(clients_db))
    cconn.row_factory = sqlite3.Row
    try:
        ccols = {r[1] for r in cconn.execute("PRAGMA table_info(clients)")}
        # P2-2.10:client_version 列点亮版本列;旧库无列时按未上报处理
        ver_expr = ("COALESCE(client_version,'')" if "client_version" in ccols
                    else "''")
        crows = cconn.execute(
            "SELECT client_id, COALESCE(user_name,'') user_name,"
            f" COALESCE(label,'') label, {ver_expr} client_version,"
            " last_seen, joined_at FROM clients"
        ).fetchall()
    finally:
        cconn.close()
    # 轨迹/原子/harness/模型聚合（registry）：team_client 目录按 label 归 client
    with pooled_connection(db_path) as conn:
        trows = conn.execute(
            "SELECT w.label label, COUNT(t.id) trajs,"
            " COALESCE(SUM(t.tasks_extracted),0) atoms"
            " FROM watch_dirs w LEFT JOIN trajectories t ON t.watch_dir_id=w.id"
            " WHERE w.ecosystem='team_client' GROUP BY w.label").fetchall()
        hrows = conn.execute(
            "SELECT w.label label, t.source_harness harness, COUNT(*) n"
            " FROM trajectories t JOIN watch_dirs w ON t.watch_dir_id=w.id"
            " WHERE w.ecosystem='team_client' GROUP BY w.label, t.source_harness"
        ).fetchall()
        mrows = conn.execute(
            "SELECT w.label label, t.source_model model, COUNT(*) n"
            " FROM trajectories t JOIN watch_dirs w ON t.watch_dir_id=w.id"
            " WHERE w.ecosystem='team_client' GROUP BY w.label, t.source_model"
        ).fetchall()
    stats = {r["label"]: {"trajs": r["trajs"], "atoms": r["atoms"]}
             for r in trows}
    harness: dict[str, list] = {}
    for r in hrows:
        harness.setdefault(r["label"], []).append(
            {"harness": r["harness"] or "unknown", "n": r["n"]})
    models: dict[str, list] = {}
    for r in mrows:
        models.setdefault(r["label"], []).append(
            {"model": r["model"] or "unknown", "n": r["n"]})
    now = datetime.now(timezone.utc)
    users = []
    online = 0
    for c in crows:
        # 目录 label 可能是 user_name 明文（有名）或 client_id（匿名）
        keys = [k for k in (c["user_name"], c["client_id"], c["label"]) if k]
        st = next((stats[k] for k in keys if k in stats),
                  {"trajs": 0, "atoms": 0})
        hs = next((harness[k] for k in keys if k in harness), [])
        ms = next((models[k] for k in keys if k in models), [])
        last_seen = c["last_seen"] or ""
        is_online = False
        if last_seen:
            try:
                seen = datetime.fromisoformat(last_seen.replace(" ", "T"))
                if seen.tzinfo is None:
                    seen = seen.replace(tzinfo=timezone.utc)
                is_online = (now - seen).total_seconds() <= online_window_s
            except ValueError:
                is_online = False
        if is_online:
            online += 1
        total_h = sum(h["n"] for h in hs) or 1
        total_m = sum(m["n"] for m in ms) or 1
        cver = c["client_version"] or ""
        users.append({
            "user": c["user_name"] or c["client_id"],
            "online": is_online,
            "last_seen": last_seen,
            # 版本列(P2-2.10):空=未上报(旧 client);低于 server 版本标落后
            "client_version": cver,
            "version_stale": bool(cver) and _version_lt(cver, _server_version()),
            "trajs": st["trajs"], "atoms": st["atoms"],
            "harness": sorted(({**h, "pct": round(h["n"] / total_h * 100)}
                               for h in hs), key=lambda x: -x["n"]),
            "models": sorted(({**m, "pct": round(m["n"] / total_m * 100)}
                              for m in ms), key=lambda x: -x["n"]),
        })
    users.sort(key=lambda u: (not u["online"], u["last_seen"] and
                              -_ts_key(u["last_seen"])))
    return {"users": users, "online": online, "reason": ""}


def _server_version() -> str:
    from xskill import __version__
    return __version__


def _version_lt(a: str, b: str) -> bool:
    """a < b?解析失败(dev 版本等)按不落后处理——不给误导性标注。"""
    try:
        from packaging.version import Version
        return Version(a) < Version(b)
    except Exception:  # pylint: disable=broad-exception-caught
        return False


def _ts_key(ts: str) -> float:
    try:
        t = datetime.fromisoformat(ts.replace(" ", "T"))
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return t.timestamp()
    except ValueError:
        return 0.0
