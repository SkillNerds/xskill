"""skill_dir 盘→库投影合扫。

``ux-scores-sync`` worker 只应 ``iterdir`` **一轮** skill_dir；本模块在同一次
遍历里刷新各投影（当前：``.ux_scores.jsonl``、``.candidates.yml`` pending）。

新投影请在本文件 per-skill 循环里加 handler，禁止再开独立全量扫盘 worker。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from xskill.pipeline.registry import (
    _atom_pending_root_key,
    delete_atom_candidate_pending_for_skill,
    pooled_connection,
    sync_atom_candidate_pending_for_skill,
)
from xskill.pipeline.ux_scores_store import (
    META_FILE_MTIME_PREFIX,
    META_LAST_SYNC,
    UX_SCORES_FILENAME,
    _set_meta,
    get_meta,
    insert_ux_scores_many,
)
from xskill.skill.candidates import CANDIDATES_FILENAME, load_candidates

logger = logging.getLogger("xskill.pipeline.skill_dir_sync")

PENDING_MTIME_PREFIX = "pending_mtime:"  # + skill name → atom_candidate_pending_meta
PENDING_MISSING = "missing"


def iter_skill_dirs(skill_dir: Path | str):
    """Yield skill 子目录（跳过点目录）。"""
    root = Path(skill_dir)
    if not root.is_dir():
        return
    for d in sorted(root.iterdir()):
        if d.is_dir() and not d.name.startswith("."):
            yield d


def _pending_mtime_get(skill: str, *, db_path: Optional[Path]) -> Optional[str]:
    key = f"{PENDING_MTIME_PREFIX}{skill}"
    with pooled_connection(db_path) as conn:
        row = conn.execute(
            "SELECT backfilled_at FROM atom_candidate_pending_meta WHERE root_key=?",
            (key,),
        ).fetchone()
    return None if row is None else str(row["backfilled_at"])


def _pending_mtime_set(skill: str, value: str, *, db_path: Optional[Path]) -> None:
    key = f"{PENDING_MTIME_PREFIX}{skill}"
    with pooled_connection(db_path) as conn:
        conn.execute(
            """
            INSERT INTO atom_candidate_pending_meta(root_key, backfilled_at)
            VALUES (?, ?)
            ON CONFLICT(root_key) DO UPDATE SET backfilled_at=excluded.backfilled_at
            """,
            (key, value),
        )
        conn.commit()


def _sync_ux_for_skill(
    skill_path: Path,
    *,
    db_path: Optional[Path],
    stats: dict,
) -> None:
    path = skill_path / UX_SCORES_FILENAME
    if not path.is_file():
        return
    stats["skills"] += 1
    try:
        mtime = path.stat().st_mtime
    except OSError as exc:
        logger.warning("ux sync stat failed %s: %s", path, exc)
        return
    meta_key = f"{META_FILE_MTIME_PREFIX}{skill_path.name}"
    prev = get_meta(meta_key, db_path=db_path)
    if prev is not None:
        try:
            if float(prev) >= mtime:
                stats["skipped"] += 1
                return
        except ValueError:
            pass
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("ux sync read failed %s: %s", path, exc)
        return
    batch: list[dict] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        stats["lines"] += 1
        try:
            rec = json.loads(line)
        except json.JSONDecodeError as exc:
            logger.warning("ux sync bad json in %s: %s", path, exc)
            continue
        if not rec.get("skill_name"):
            rec["skill_name"] = skill_path.name
        batch.append(rec)
    stats["inserted"] += insert_ux_scores_many(batch, db_path=db_path)
    _set_meta(meta_key, str(mtime), db_path=db_path)


def _sync_pending_for_skill(
    skill_path: Path,
    *,
    db_path: Optional[Path],
    stats: dict,
) -> None:
    skill = skill_path.name
    cand_path = skill_path / CANDIDATES_FILENAME
    stats["skills"] += 1
    if not cand_path.is_file():
        prev = _pending_mtime_get(skill, db_path=db_path)
        if prev == PENDING_MISSING:
            stats["skipped"] += 1
            return
        delete_atom_candidate_pending_for_skill(
            skill,
            db_path=db_path,
            dirty_root_key=_atom_pending_root_key(skill_path.parent),
        )
        _pending_mtime_set(skill, PENDING_MISSING, db_path=db_path)
        stats["synced"] += 1
        return
    try:
        mtime = cand_path.stat().st_mtime
    except OSError as exc:
        logger.warning("pending sync stat failed %s: %s", cand_path, exc)
        return
    prev = _pending_mtime_get(skill, db_path=db_path)
    if prev is not None and prev != PENDING_MISSING:
        try:
            if float(prev) >= mtime:
                stats["skipped"] += 1
                return
        except ValueError:
            pass
    try:
        data = load_candidates(skill_path)
        candidates = data.get("candidates", []) or []
    except Exception:  # pylint: disable=broad-exception-caught
        logger.warning(
            "pending sync load failed %s", skill_path, exc_info=True,
        )
        return
    sync_atom_candidate_pending_for_skill(
        skill,
        candidates,
        db_path=db_path,
        dirty_root_key=_atom_pending_root_key(skill_path.parent),
    )
    _pending_mtime_set(skill, str(mtime), db_path=db_path)
    stats["synced"] += 1
    stats["rows"] += sum(
        1 for c in candidates
        if isinstance(c, dict) and (c.get("atom_id") or "")
    )


def _mark_pending_root_ready(skill_dir: Path, *, db_path: Optional[Path]) -> None:
    root = _atom_pending_root_key(skill_dir)
    with pooled_connection(db_path) as conn:
        conn.execute(
            """
            INSERT INTO atom_candidate_pending_meta(root_key, backfilled_at)
            VALUES (?, datetime('now'))
            ON CONFLICT(root_key) DO UPDATE SET
                backfilled_at=datetime('now')
            """,
            (root,),
        )
        conn.commit()


def _purge_pending_orphan_skills(
    live_skills: set[str],
    *,
    db_path: Optional[Path],
) -> int:
    """盘上已不存在的 skill，清掉其 pending 投影行。"""
    with pooled_connection(db_path) as conn:
        rows = conn.execute(
            "SELECT DISTINCT skill FROM atom_candidate_pending",
        ).fetchall()
        orphans = [str(r["skill"]) for r in rows if str(r["skill"]) not in live_skills]
        for skill in orphans:
            conn.execute(
                "DELETE FROM atom_candidate_pending WHERE skill=?", (skill,),
            )
        conn.commit()
    return len(orphans)


def sync_skill_disk_projections(
    skill_dir: Path | str,
    *,
    db_path: Optional[Path] = None,
) -> dict:
    """一轮 ``iterdir``：刷新 skill_dir 下全部盘→库投影。

    返回 ``{"ux": {...}, "pending": {...}}``。新投影只加 handler，不加扫盘轮次。
    """
    root = Path(skill_dir)
    ux = {"skills": 0, "lines": 0, "inserted": 0, "skipped": 0}
    pending = {
        "skills": 0, "synced": 0, "skipped": 0, "rows": 0, "orphans": 0,
    }
    if not root.is_dir():
        return {"ux": ux, "pending": pending}

    live: set[str] = set()
    for d in iter_skill_dirs(root):
        live.add(d.name)
        _sync_ux_for_skill(d, db_path=db_path, stats=ux)
        _sync_pending_for_skill(d, db_path=db_path, stats=pending)

    pending["orphans"] = _purge_pending_orphan_skills(live, db_path=db_path)
    _mark_pending_root_ready(root, db_path=db_path)
    _set_meta(
        META_LAST_SYNC,
        datetime.now(timezone.utc).isoformat(timespec="seconds"),
        db_path=db_path,
    )
    return {"ux": ux, "pending": pending}
