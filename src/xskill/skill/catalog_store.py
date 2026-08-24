"""skills_catalog 投影表：磁盘为真相源，表供列表/分页查询。

写路径在确认的出口 UPSERT/DELETE；读路径只查表。空表（或该 root 尚未
backfill）时一次性扫盘灌表——这是初始化，不是每请求静默回退扫盘。
"""
from __future__ import annotations

import logging
import operator
import threading
from pathlib import Path
from typing import Optional

from xskill.pipeline.registry import pooled_connection

logger = logging.getLogger(__name__)

_SOURCE_NATIVE = "native"
_SOURCE_SKILLHUB = "skillhub"
_CATALOG_COMPARE_COLUMNS = (
    "root_key", "name", "repo_name", "source", "state", "description",
    "version", "candidates_count", "main_sha", "staging_sha",
    "distributable", "search_id", "hub", "skill_id", "use_count",
    "content_sha",
)
_CATALOG_INTEGER_COLUMNS = frozenset({
    "version", "candidates_count", "distributable", "use_count",
})

_STATE_ORDER_SQL = """
CASE state
  WHEN 'main' THEN 0
  WHEN 'staging' THEN 0
  WHEN 'baby' THEN 1
  WHEN 'unknown' THEN 2
  WHEN 'skillhub' THEN 3
  ELSE 4
END
"""

_BACKFILL_LOCKS_GUARD = threading.Lock()
_BACKFILL_FLIGHTS: dict[tuple[str, str], "_BackfillFlight"] = {}


class _BackfillFlight:
    def __init__(self) -> None:
        self.done = threading.Event()
        self.error: BaseException | None = None


def _root_key(skill_dir: Path | str) -> str:
    path = Path(skill_dir).expanduser()
    try:
        return str(path.resolve(strict=False))
    except (OSError, RuntimeError):
        return str(path.absolute())


def catalog_root_key(skill_dir: Path | str) -> str:
    """自产 skill 根目录的投影表 root_key（绝对路径字符串）。"""
    return _root_key(skill_dir)


def _native_catalog_key(name: str) -> str:
    return f"native:{name}"


def _skillhub_catalog_key(skill_id: str) -> str:
    return f"skillhub:{skill_id}"


def _branch_names(skill_path: Path) -> set[str]:
    git = skill_path / ".git"
    names: set[str] = set()
    heads = git / "refs" / "heads"
    if heads.is_dir():
        for path in heads.iterdir():
            if path.is_file():
                names.add(path.name)
    packed = git / "packed-refs"
    if packed.is_file():
        try:
            for line in packed.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if " refs/heads/" in line:
                    names.add(line.split("refs/heads/", 1)[1])
        except OSError:
            logger.warning("failed to read packed-refs: %s", packed, exc_info=True)
    return names


def _ref_sha(skill_path: Path, branch: str) -> str:
    loose = skill_path / ".git" / "refs" / "heads" / branch
    if loose.is_file():
        try:
            return loose.read_text(encoding="utf-8").strip()
        except OSError:
            logger.warning("failed to read ref %s", loose, exc_info=True)
            return ""
    packed = skill_path / ".git" / "packed-refs"
    if not packed.is_file():
        return ""
    needle = f" refs/heads/{branch}"
    try:
        for line in packed.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.endswith(needle) and not line.startswith("#"):
                return line.split(" ", 1)[0]
    except OSError:
        logger.warning("failed to read packed-refs for sha: %s", packed, exc_info=True)
    return ""


def _read_native_row(
    skill_path: Path,
    *,
    candidates_count: int | None = None,
) -> dict:
    """从单个 skill 目录读出投影行（API 字段 + 表扩展字段）。

    ``candidates_count`` 非空时跳过读 ``.candidates.yml``（写出口已有 data）。
    """
    from xskill.skill.frontmatter import parse as fm_parse

    skill_path = Path(skill_path)
    branches = _branch_names(skill_path)
    if "staging" in branches:
        state = "staging"
    elif "main" in branches:
        state = "main"
    elif "baby" in branches:
        state = "baby"
    else:
        state = "unknown"
    description, version = "", 0
    skill_md = skill_path / "SKILL.md"
    if skill_md.is_file():
        try:
            frontmatter, _ = fm_parse(skill_md.read_text(encoding="utf-8"))
            description = (
                (frontmatter.get("description") or "").strip().replace("\n", " ")
            )
            metadata = frontmatter.get("metadata", {}) or {}
            version = metadata.get("version", 0)
        except Exception:  # pylint: disable=broad-exception-caught
            logger.warning("failed to read skill metadata: %s", skill_md, exc_info=True)
    if candidates_count is None:
        candidates_count = 0
        candidates_path = skill_path / ".candidates.yml"
        if candidates_path.is_file():
            try:
                import yaml
                data = yaml.safe_load(candidates_path.read_text(encoding="utf-8")) or {}
                candidates_count = len(data.get("candidates", []) or [])
            except Exception:  # pylint: disable=broad-exception-caught
                logger.warning(
                    "failed to read candidate buffer: %s", candidates_path, exc_info=True,
                )
    main_sha = _ref_sha(skill_path, "main")
    staging_sha = _ref_sha(skill_path, "staging")
    distributable = 1 if state in ("main", "staging") and skill_md.is_file() else 0
    desc_cut = description[:300]
    from xskill.recommend.skill_vector_store import content_sha_for_text
    return {
        "catalog_key": _native_catalog_key(skill_path.name),
        "name": skill_path.name,
        "repo_name": skill_path.name,
        "source": _SOURCE_NATIVE,
        "state": state,
        "description": desc_cut,
        "version": version,
        "candidates": candidates_count,
        "candidates_count": int(candidates_count),
        "main_sha": main_sha,
        "staging_sha": staging_sha,
        "distributable": distributable,
        "search_id": skill_path.name,
        "hub": "",
        "skill_id": "",
        "use_count": 0,
        "content_sha": content_sha_for_text(desc_cut) if desc_cut else "",
        "root_key": _root_key(skill_path.parent),
    }


def _skillhub_entries(skillhub) -> list[dict]:
    """归一 skillhub 入参：None / list / SkillHub 对象 → 条目列表。"""
    if skillhub is None:
        return []
    if isinstance(skillhub, list):
        return list(skillhub)
    return list(skillhub._entries(  # pylint: disable=protected-access
        include_vec=False, require_description=True))


def _skillhub_fingerprint(skillhub) -> str:
    if skillhub is None:
        return "none"
    if isinstance(skillhub, list):
        rows = []
        for entry in skillhub:
            rows.append(tuple(
                repr(entry.get(field)) for field in (
                    "display_name", "name", "source_path", "skill_id",
                    "description", "use_count",
                )
            ))
        return "entries:" + repr(tuple(sorted(rows)))
    hub_dir = getattr(skillhub, "dir", None)
    if hub_dir is not None:
        return "skillhub:" + _root_key(hub_dir) + f":{bool(getattr(skillhub, 'enabled', True))}"
    return f"object:{type(skillhub).__module__}.{type(skillhub).__qualname__}:{id(skillhub)}"


def _skillhub_rows(skillhub) -> list[dict]:
    from xskill.recommend.skill_vector_store import content_sha_for_text

    rows: list[dict] = []
    for entry in _skillhub_entries(skillhub):
        description = str(entry.get("description") or "").strip().replace("\n", " ")
        desc_cut = description[:300]
        skill_id = entry.get("skill_id") or entry.get("name") or ""
        name = entry.get("display_name") or entry.get("name") or ""
        hub = entry.get("source_path") or ""
        use_count = entry.get("use_count", 0) or 0
        sha = entry.get("content_sha") or (
            content_sha_for_text(desc_cut) if desc_cut else ""
        )
        rows.append({
            "catalog_key": _skillhub_catalog_key(str(skill_id)),
            "name": name,
            "repo_name": name,
            "source": _SOURCE_SKILLHUB,
            "state": "skillhub",
            "description": desc_cut,
            "version": 0,
            "candidates": 0,
            "candidates_count": 0,
            "main_sha": "",
            "staging_sha": "",
            "distributable": 0,
            "search_id": str(skill_id),
            "hub": hub,
            "skill_id": str(skill_id),
            "use_count": use_count,
            "content_sha": sha,
            "root_key": "",
        })
    rows.sort(key=operator.itemgetter("hub", "name"))
    return rows


def scan_skills_catalog(skill_dir: Path, skillhub=None) -> list[dict]:
    """扫盘得到完整清单行（backfill / 调试用）；排序与旧 dashboard 一致。"""
    skill_dir = Path(skill_dir)
    out: list[dict] = []
    root = _root_key(skill_dir)
    if skill_dir.is_dir():
        for path in sorted(skill_dir.iterdir()):
            if not path.is_dir() or path.name.startswith("."):
                continue
            row = _read_native_row(path)
            row["root_key"] = root
            out.append(row)
    order = {"main": 0, "staging": 0, "baby": 1, "unknown": 2}
    out = [row for _rank, _name, _index, row in sorted(
        (order.get(row["state"], 3), row["name"], index, row)
        for index, row in enumerate(out))]
    hub_rows = _skillhub_rows(skillhub)
    for row in hub_rows:
        row["root_key"] = root
    out.extend(hub_rows)
    return out


def catalog_api_row(stored: dict) -> dict:
    """表行 / 扫盘行 → dashboard API 行形状。"""
    row = {
        "name": stored["name"],
        "state": stored["state"],
        "source": stored["source"],
        "description": stored["description"],
        "version": stored["version"],
        "candidates": stored.get("candidates", stored.get("candidates_count", 0)),
    }
    if stored["source"] == _SOURCE_SKILLHUB:
        row["hub"] = stored.get("hub") or ""
        row["skill_id"] = stored.get("skill_id") or stored.get("search_id") or ""
        row["use_count"] = stored.get("use_count", 0) or 0
    return row


def _vector_target(row: dict | None, *, retired: bool = False):
    if row is None:
        return None
    from xskill.recommend.skill_vector_store import (
        catalog_row_is_indexable,
        content_sha_for_text,
    )

    candidate = {**row, "retired": retired}
    if not catalog_row_is_indexable(candidate):
        return None
    description = (candidate.get("description") or "").strip()
    content_sha = candidate.get("content_sha") or content_sha_for_text(description)
    return (
        content_sha,
        candidate.get("source") or "",
        candidate.get("name") or "",
        description,
    )


def _stored_vector_row(conn, catalog_key: str) -> tuple[dict | None, bool]:
    row = conn.execute(
        """
        SELECT catalog_key, name, source, description, content_sha, distributable
        FROM skills_catalog WHERE catalog_key=?
        """,
        (catalog_key,),
    ).fetchone()
    if row is None:
        return None, False
    retired = conn.execute(
        "SELECT 1 FROM skill_lifecycle WHERE skill_name=? AND state='retired'",
        (row["name"],),
    ).fetchone() is not None
    return dict(row), retired


def _mark_vector_transition(
    conn,
    catalog_key: str,
    old_target,
    new_target,
) -> None:
    if old_target == new_target:
        return
    from xskill.recommend.vector_dirty import mark_catalog_vector_dirty_on_connection

    mark_catalog_vector_dirty_on_connection(
        conn,
        catalog_key,
        operation="upsert" if new_target is not None else "delete",
        content_sha=new_target[0] if new_target is not None else "",
    )


def _upsert_row(conn, row: dict, *, mark_vector: bool = True) -> None:
    from xskill.recommend.skill_vector_store import content_sha_for_text

    description = row["description"]
    content_sha = row.get("content_sha") or (
        content_sha_for_text(description) if description else ""
    )
    old_row = old_retired = None
    if mark_vector:
        old_row, old_retired = _stored_vector_row(conn, row["catalog_key"])
    conn.execute(
        """
        INSERT INTO skills_catalog(
            catalog_key, root_key, name, repo_name, source, state,
            description, version, candidates_count, main_sha, staging_sha,
            distributable, search_id, hub, skill_id, use_count, content_sha,
            updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'))
        ON CONFLICT(catalog_key) DO UPDATE SET
            root_key=excluded.root_key,
            name=excluded.name,
            repo_name=excluded.repo_name,
            source=excluded.source,
            state=excluded.state,
            description=excluded.description,
            version=excluded.version,
            candidates_count=excluded.candidates_count,
            main_sha=excluded.main_sha,
            staging_sha=excluded.staging_sha,
            distributable=excluded.distributable,
            search_id=excluded.search_id,
            hub=excluded.hub,
            skill_id=excluded.skill_id,
            use_count=excluded.use_count,
            content_sha=excluded.content_sha,
            updated_at=datetime('now')
        """,
        (
            row["catalog_key"],
            row["root_key"],
            row["name"],
            row["repo_name"],
            row["source"],
            row["state"],
            description,
            int(row["version"] or 0),
            int(row["candidates_count"]),
            row["main_sha"],
            row["staging_sha"],
            int(row["distributable"]),
            row["search_id"],
            row.get("hub") or "",
            row.get("skill_id") or "",
            int(row.get("use_count") or 0),
            content_sha,
        ),
    )
    if mark_vector:
        retired = conn.execute(
            "SELECT 1 FROM skill_lifecycle WHERE skill_name=? AND state='retired'",
            (row["name"],),
        ).fetchone() is not None
        new_row = {**row, "content_sha": content_sha}
        _mark_vector_transition(
            conn,
            row["catalog_key"],
            _vector_target(old_row, retired=bool(old_retired)),
            _vector_target(new_row, retired=retired),
        )


def _bump_catalog_generation(conn, root_key: str) -> None:
    """推进已初始化 root 的目录版本；未 backfill 的 root 留给 ensure 初始化。"""
    if not root_key:
        return
    conn.execute(
        """
        UPDATE skills_catalog_meta
        SET generation=generation + 1
        WHERE root_key=?
        """,
        (root_key,),
    )


def _catalog_row_matches(stored, row: dict) -> bool:
    """比较会影响 Cluster 路由的稳定字段，忽略 ``updated_at``。"""
    for column in _CATALOG_COMPARE_COLUMNS:
        expected = row[column]
        if column in _CATALOG_INTEGER_COLUMNS:
            expected = int(expected or 0)
        if stored[column] != expected:
            return False
    return True


_BACKFILL_WAIT_TIMEOUT_SECONDS = 120.0


def resolve_catalog_db_path(explicit: Path | str | None = None) -> Path | None:
    """解析投影表所用 registry 库路径。

    写出口禁止 ``pooled_connection(None)`` 隐式摸全局库：必须显式传入，或从
    当前 ``AgentToolContext.registry_db_path`` 读取。两者皆无则返回 ``None``
    （调用方应跳过投影写入）。
    """
    if explicit is not None:
        return Path(explicit)
    try:
        from xskill.agents.agent_tools import current_agent_tool_context
        context = current_agent_tool_context()
    except Exception:  # pylint: disable=broad-exception-caught
        return None
    if context.configured and context.registry_db_path is not None:
        return Path(context.registry_db_path)
    return None


def upsert_native_skill(
    skill_path: Path | str,
    *,
    db_path: Path,
    candidates_count: int | None = None,
) -> None:
    """按磁盘现状 UPSERT 一条自产 skill。``db_path`` 必填，禁止隐式全局库。"""
    if db_path is None:
        raise TypeError("skills_catalog upsert requires explicit db_path")
    path = Path(skill_path)
    if not path.is_dir():
        raise FileNotFoundError(f"skill path not found for catalog upsert: {path}")
    row = _read_native_row(path, candidates_count=candidates_count)
    with pooled_connection(Path(db_path)) as conn:
        existing = conn.execute(
            "SELECT * FROM skills_catalog WHERE catalog_key=?",
            (row["catalog_key"],),
        ).fetchone()
        if existing is not None and _catalog_row_matches(existing, row):
            return
        _upsert_row(conn, row)
        affected_roots = {row["root_key"]}
        if existing is not None:
            affected_roots.add(existing["root_key"])
        for affected_root in affected_roots:
            _bump_catalog_generation(conn, affected_root)
        conn.commit()


def delete_native_skill(name: str, *, db_path: Path) -> None:
    if db_path is None:
        raise TypeError("skills_catalog delete requires explicit db_path")
    with pooled_connection(Path(db_path)) as conn:
        catalog_key = _native_catalog_key(name)
        existing = conn.execute(
            "SELECT root_key FROM skills_catalog WHERE catalog_key=?",
            (catalog_key,),
        ).fetchone()
        old_row, old_retired = _stored_vector_row(conn, catalog_key)
        cursor = conn.execute(
            "DELETE FROM skills_catalog WHERE catalog_key=?",
            (catalog_key,),
        )
        if old_row is not None:
            _mark_vector_transition(
                conn,
                catalog_key,
                _vector_target(old_row, retired=old_retired),
                None,
            )
        if cursor.rowcount > 0 and existing is not None:
            _bump_catalog_generation(conn, existing["root_key"])
        conn.commit()


def delete_all_native(*, root_key: str = "", db_path: Path) -> int:
    """wipe_all_skills：删该 root 下全部 native 行；root_key 空则删所有 native。"""
    if db_path is None:
        raise TypeError("skills_catalog wipe requires explicit db_path")
    with pooled_connection(Path(db_path)) as conn:
        where = "source=? AND root_key=?" if root_key else "source=?"
        params = (_SOURCE_NATIVE, root_key) if root_key else (_SOURCE_NATIVE,)
        old_rows = conn.execute(
            f"SELECT catalog_key FROM skills_catalog WHERE {where}",  # noqa: S608
            params,
        ).fetchall()
        if root_key:
            affected_roots = [root_key]
            cursor = conn.execute(
                "DELETE FROM skills_catalog WHERE source=? AND root_key=?",
                (_SOURCE_NATIVE, root_key),
            )
        else:
            affected_roots = [
                row["root_key"] for row in conn.execute(
                    "SELECT DISTINCT root_key FROM skills_catalog WHERE source=?",
                    (_SOURCE_NATIVE,),
                ).fetchall()
                if row["root_key"]
            ]
            cursor = conn.execute(
                "DELETE FROM skills_catalog WHERE source=?",
                (_SOURCE_NATIVE,),
            )
        from xskill.recommend.vector_dirty import mark_catalog_vector_dirty_on_connection
        for row in old_rows:
            mark_catalog_vector_dirty_on_connection(
                conn, row["catalog_key"], operation="delete",
            )
        if cursor.rowcount > 0:
            for affected_root in affected_roots:
                _bump_catalog_generation(conn, affected_root)
        conn.commit()
        return int(cursor.rowcount or 0)


def reconcile_native_canary_catalog(
    skill_dir: Path | str,
    *,
    db_path: Path,
) -> int:
    """低频从 Git refs 修复自产 skill 的 Canary 状态投影。

    已有行只刷新分支状态与 SHA，避免为了 Canary 对账重复解析全部
    ``SKILL.md`` 和 ``.candidates.yml``。新目录仍完整建行，消失的目录从
    投影删除；SkillHub 行不受影响。
    """
    root_path = Path(skill_dir)
    root = _root_key(root_path)
    paths = []
    if root_path.is_dir():
        paths = [
            path
            for path in sorted(root_path.iterdir())
            if path.is_dir() and not path.name.startswith(".")
        ]
    names = {path.name for path in paths}
    active = 0
    with pooled_connection(Path(db_path)) as conn:
        existing = {
            row["name"]: dict(row)
            for row in conn.execute(
                """
                SELECT name, state, main_sha, staging_sha, distributable
                FROM skills_catalog
                WHERE source=? AND root_key=?
                """,
                (_SOURCE_NATIVE, root),
            ).fetchall()
        }
        for path in paths:
            branches = _branch_names(path)
            state = (
                "staging" if "staging" in branches
                else "main" if "main" in branches
                else "baby" if "baby" in branches
                else "unknown"
            )
            if state == "staging":
                active += 1
            if path.name not in existing:
                row = _read_native_row(path)
                row["root_key"] = root
                _upsert_row(conn, row)
                continue
            current_main_sha = _ref_sha(path, "main")
            current_staging_sha = _ref_sha(path, "staging")
            distributable = int(
                state in ("main", "staging")
                and (path / "SKILL.md").is_file()
            )
            stored = existing[path.name]
            if (
                stored["state"] == state
                and stored["main_sha"] == current_main_sha
                and stored["staging_sha"] == current_staging_sha
                and int(stored["distributable"]) == distributable
            ):
                continue
            conn.execute(
                """
                UPDATE skills_catalog
                SET state=?, main_sha=?, staging_sha=?, distributable=?,
                    updated_at=datetime('now')
                WHERE catalog_key=? AND source=? AND root_key=?
                """,
                (
                    state,
                    current_main_sha,
                    current_staging_sha,
                    distributable,
                    _native_catalog_key(path.name),
                    _SOURCE_NATIVE,
                    root,
                ),
            )
        removed = set(existing) - names
        if removed:
            conn.executemany(
                """
                DELETE FROM skills_catalog
                WHERE catalog_key=? AND source=? AND root_key=?
                """,
                [
                    (_native_catalog_key(name), _SOURCE_NATIVE, root)
                    for name in sorted(removed)
                ],
            )
        conn.execute(
            """
            INSERT INTO skills_catalog_meta(root_key, backfilled_at, skillhub_key)
            VALUES (?, datetime('now'), ?)
            ON CONFLICT(root_key) DO UPDATE SET backfilled_at=datetime('now')
            """,
            (root, _skillhub_fingerprint(None)),
        )
        conn.commit()
    return active


def list_active_native_canaries(
    skill_dir: Path | str,
    *,
    db_path: Path,
) -> list[str]:
    """从可重建 catalog 投影返回当前有 staging 的自产 skill 名。"""
    root = _root_key(skill_dir)
    with pooled_connection(Path(db_path)) as conn:
        rows = conn.execute(
            """
            SELECT name FROM skills_catalog
            WHERE source=? AND root_key=? AND state='staging'
            ORDER BY name
            """,
            (_SOURCE_NATIVE, root),
        ).fetchall()
    return [row["name"] for row in rows]


def rename_native_skill(
    old_name: str,
    new_skill_path: Path | str,
    *,
    db_path: Path,
) -> None:
    """同一连接内 DELETE 旧行 + UPSERT 新行，避免中间态丢行。"""
    if db_path is None:
        raise TypeError("skills_catalog rename requires explicit db_path")
    path = Path(new_skill_path)
    if not path.is_dir():
        raise FileNotFoundError(f"skill path not found for catalog rename: {path}")
    row = _read_native_row(path)
    with pooled_connection(Path(db_path)) as conn:
        old_key = _native_catalog_key(old_name)
        existing = conn.execute(
            "SELECT root_key FROM skills_catalog WHERE catalog_key=?",
            (old_key,),
        ).fetchone()
        old_row, old_retired = _stored_vector_row(conn, old_key)
        conn.execute(
            "DELETE FROM skills_catalog WHERE catalog_key=?",
            (old_key,),
        )
        if old_row is not None:
            _mark_vector_transition(
                conn,
                old_key,
                _vector_target(old_row, retired=old_retired),
                None,
            )
        _upsert_row(conn, row)
        affected_roots = {row["root_key"]}
        if existing is not None:
            affected_roots.add(existing["root_key"])
        for affected_root in affected_roots:
            _bump_catalog_generation(conn, affected_root)
        conn.commit()


def update_native_candidates_count(
    skill_path: Path | str,
    candidates_count: int,
    *,
    db_path: Path,
) -> None:
    """热路径：只改 ``candidates_count``，不重读 SKILL.md / yaml / refs。"""
    if db_path is None:
        raise TypeError("skills_catalog candidates update requires explicit db_path")
    path = Path(skill_path)
    name = path.name
    with pooled_connection(Path(db_path)) as conn:
        existing = conn.execute(
            "SELECT root_key FROM skills_catalog WHERE catalog_key=?",
            (_native_catalog_key(name),),
        ).fetchone()
        count = int(candidates_count)
        cursor = conn.execute(
            """
            UPDATE skills_catalog
            SET candidates_count=?, updated_at=datetime('now')
            WHERE catalog_key=? AND candidates_count<>?
            """,
            (count, _native_catalog_key(name), count),
        )
        if cursor.rowcount > 0 and existing is not None:
            _bump_catalog_generation(conn, existing["root_key"])
        conn.commit()


def notify_native_upsert(
    skill_path: Path | str,
    *,
    db_path: Path | str | None = None,
    candidates_count: int | None = None,
) -> None:
    """写出口钩子：投影失败只记日志，绝不砸穿磁盘真相写路径。"""
    try:
        resolved = resolve_catalog_db_path(db_path)
        if resolved is None:
            logger.debug(
                "skills_catalog skip upsert (no registry_db_path): %s", skill_path,
            )
            return
        upsert_native_skill(
            skill_path, db_path=resolved, candidates_count=candidates_count,
        )
    except Exception:  # pylint: disable=broad-exception-caught
        logger.exception("skills_catalog upsert failed: %s", skill_path)


def notify_native_candidates_count(
    skill_path: Path | str,
    candidates_count: int,
    *,
    db_path: Path | str | None = None,
) -> None:
    """candidates 落盘钩子：只用内存 count 更新投影列。"""
    try:
        resolved = resolve_catalog_db_path(db_path)
        if resolved is None:
            logger.debug(
                "skills_catalog skip candidates_count (no registry_db_path): %s",
                skill_path,
            )
            return
        update_native_candidates_count(
            skill_path, candidates_count, db_path=resolved,
        )
    except Exception:  # pylint: disable=broad-exception-caught
        logger.exception(
            "skills_catalog candidates_count update failed: %s", skill_path,
        )


def notify_native_delete(
    name: str,
    *,
    db_path: Path | str | None = None,
) -> None:
    try:
        resolved = resolve_catalog_db_path(db_path)
        if resolved is None:
            logger.debug(
                "skills_catalog skip delete (no registry_db_path): %s", name,
            )
            return
        delete_native_skill(name, db_path=resolved)
    except Exception:  # pylint: disable=broad-exception-caught
        logger.exception("skills_catalog delete failed: %s", name)


def notify_native_rename(
    old_name: str,
    new_skill_path: Path | str,
    *,
    db_path: Path | str | None = None,
) -> None:
    try:
        resolved = resolve_catalog_db_path(db_path)
        if resolved is None:
            logger.debug(
                "skills_catalog skip rename (no registry_db_path): %s → %s",
                old_name, new_skill_path,
            )
            return
        rename_native_skill(old_name, new_skill_path, db_path=resolved)
    except Exception:  # pylint: disable=broad-exception-caught
        logger.exception(
            "skills_catalog rename failed: %s → %s", old_name, new_skill_path,
        )


def notify_native_wipe(
    *,
    root_key: str = "",
    db_path: Path | str | None = None,
) -> None:
    try:
        resolved = resolve_catalog_db_path(db_path)
        if resolved is None:
            logger.debug(
                "skills_catalog skip wipe (no registry_db_path): root_key=%s",
                root_key,
            )
            return
        delete_all_native(root_key=root_key, db_path=resolved)
    except Exception:  # pylint: disable=broad-exception-caught
        logger.exception("skills_catalog wipe failed: root_key=%s", root_key)


def _meta_ready(conn, root_key: str, skillhub_key: str) -> bool:
    row = conn.execute(
        "SELECT skillhub_key FROM skills_catalog_meta WHERE root_key=?",
        (root_key,),
    ).fetchone()
    if row is None:
        return False
    return row["skillhub_key"] == skillhub_key


def backfill_skills_catalog(
    skill_dir: Path | str,
    skillhub=None,
    *,
    db_path: Optional[Path] = None,
) -> int:
    """全量用磁盘扫描替换该 root 的投影行，并标记 meta 已就绪。"""
    skill_dir = Path(skill_dir)
    root = _root_key(skill_dir)
    skillhub_key = _skillhub_fingerprint(skillhub)
    rows = scan_skills_catalog(skill_dir, skillhub=skillhub)
    with pooled_connection(db_path) as conn:
        old_rows = conn.execute(
            """
            SELECT catalog_key, name, source, description, content_sha, distributable
            FROM skills_catalog WHERE root_key=?
            """,
            (root,),
        ).fetchall()
        retired = {
            row["skill_name"] for row in conn.execute(
                "SELECT skill_name FROM skill_lifecycle WHERE state='retired'"
            ).fetchall()
        }
        old_targets = {
            row["catalog_key"]: _vector_target(
                dict(row), retired=row["name"] in retired,
            )
            for row in old_rows
        }
        conn.execute(
            "DELETE FROM skills_catalog WHERE root_key=?",
            (root,),
        )
        for row in rows:
            row["root_key"] = root
            _upsert_row(conn, row, mark_vector=False)
        new_targets = {
            row["catalog_key"]: _vector_target(
                row, retired=row["name"] in retired,
            )
            for row in rows
        }
        for catalog_key in old_targets.keys() | new_targets.keys():
            _mark_vector_transition(
                conn,
                catalog_key,
                old_targets.get(catalog_key),
                new_targets.get(catalog_key),
            )
        conn.execute(
            """
            INSERT INTO skills_catalog_meta(
                root_key, backfilled_at, skillhub_key, generation
            )
            VALUES (?, datetime('now'), ?, 1)
            ON CONFLICT(root_key) DO UPDATE SET
                backfilled_at=datetime('now'),
                skillhub_key=excluded.skillhub_key,
                generation=skills_catalog_meta.generation + 1
            """,
            (root, skillhub_key),
        )
        conn.commit()
    logger.info(
        "skills_catalog backfill: root=%s rows=%d", root, len(rows),
    )
    return len(rows)


def reconcile_native_skills_catalog(
    skill_dir: Path | str,
    *,
    db_path: Path,
) -> dict[str, int]:
    """低频磁盘→投影对账；generation fence 防止旧扫描覆盖并发写入。"""
    if db_path is None:
        raise TypeError("skills_catalog reconcile requires explicit db_path")
    skill_dir = Path(skill_dir)
    root = _root_key(skill_dir)

    # 先确保 generation 存在。这里只补 meta，不碰 catalog 行，因而不会误删同
    # root 的 SkillHub 投影。之后所有正常写出口都会 bump，供扫描后的 fence 检查。
    with pooled_connection(Path(db_path)) as conn:
        cursor = conn.execute(
            """
            INSERT INTO skills_catalog_meta(
                root_key, backfilled_at, skillhub_key, generation
            ) VALUES (?, datetime('now'), 'none', 1)
            ON CONFLICT(root_key) DO NOTHING
            """,
            (root,),
        )
        meta_created = cursor.rowcount > 0
        start_generation = int(conn.execute(
            "SELECT generation FROM skills_catalog_meta WHERE root_key=?",
            (root,),
        ).fetchone()["generation"])
        conn.commit()

    rows = scan_skills_catalog(skill_dir)
    wanted = {row["catalog_key"]: row for row in rows}
    with pooled_connection(Path(db_path)) as conn:
        # 锁内先验 generation 必须仍等于扫描前版本。若期间有写出口推进版本，
        # 本轮旧快照直接放弃；写出口已经把新行写入，低频对账下轮再补外部漂移。
        conn.execute("BEGIN IMMEDIATE")
        current_generation = int(conn.execute(
            "SELECT generation FROM skills_catalog_meta WHERE root_key=?",
            (root,),
        ).fetchone()["generation"])
        if current_generation != start_generation:
            conn.rollback()
            return {"upserted": 0, "deleted": 0, "changed": 0, "skipped": 1}

        existing_rows = conn.execute(
            f"""
            SELECT catalog_key, {', '.join(_CATALOG_COMPARE_COLUMNS)}
            FROM skills_catalog
            WHERE root_key=? AND source=?
            """,
            (root, _SOURCE_NATIVE),
        ).fetchall()
        existing = {row["catalog_key"]: dict(row) for row in existing_rows}
        upserted = 0
        for key, row in wanted.items():
            current = existing.get(key)
            if current is not None and _catalog_row_matches(current, row):
                continue
            _upsert_row(conn, row)
            upserted += 1

        stale = set(existing) - set(wanted)
        if stale:
            placeholders = ",".join("?" for _ in stale)
            conn.execute(
                f"DELETE FROM skills_catalog WHERE catalog_key IN ({placeholders})",
                tuple(sorted(stale)),
            )

        changed = bool(upserted or stale)
        if changed:
            _bump_catalog_generation(conn, root)
        conn.commit()
    return {
        "upserted": upserted,
        "deleted": len(stale),
        "changed": int(changed or meta_created),
        "skipped": 0,
    }


def _ensure_native_catalog_ready(skill_dir: Path | str, *, db_path: Path) -> None:
    """只要该 root 已有任意 catalog backfill 即可读 native 投影。"""
    root = _root_key(skill_dir)
    with pooled_connection(Path(db_path)) as conn:
        ready = conn.execute(
            "SELECT 1 FROM skills_catalog_meta WHERE root_key=?",
            (root,),
        ).fetchone()
    if ready is None:
        ensure_skills_catalog(skill_dir, db_path=Path(db_path))


def native_catalog_generation(skill_dir: Path | str, *, db_path: Path) -> int:
    """返回 native 路由目录的跨进程失效版本。"""
    if db_path is None:
        raise TypeError("skills_catalog generation requires explicit db_path")
    root = _root_key(skill_dir)
    with pooled_connection(Path(db_path)) as conn:
        row = conn.execute(
            "SELECT generation FROM skills_catalog_meta WHERE root_key=?",
            (root,),
        ).fetchone()
    if row is None:
        ensure_skills_catalog(skill_dir, db_path=Path(db_path))
        with pooled_connection(Path(db_path)) as conn:
            row = conn.execute(
                "SELECT generation FROM skills_catalog_meta WHERE root_key=?",
                (root,),
            ).fetchone()
    return int(row["generation"] if row is not None else 0)


def list_native_cluster_catalog(
    skill_dir: Path | str,
    *,
    db_path: Path,
) -> list[dict]:
    """按旧 Cluster 路由顺序读取 native 投影；不改写 SkillHub fingerprint。"""
    if db_path is None:
        raise TypeError("native cluster catalog requires explicit db_path")
    _ensure_native_catalog_ready(skill_dir, db_path=Path(db_path))
    root = _root_key(skill_dir)
    with pooled_connection(Path(db_path)) as conn:
        rows = conn.execute(
            """
            SELECT name, state, description, candidates_count
            FROM skills_catalog
            WHERE root_key=? AND source=?
            ORDER BY name
            """,
            (root, _SOURCE_NATIVE),
        ).fetchall()
    return [dict(row) for row in rows]


def ensure_skills_catalog(
    skill_dir: Path | str,
    skillhub=None,
    *,
    db_path: Optional[Path] = None,
) -> None:
    """该 root 尚未 backfill（或 skillhub 身份变了）时做一次灌表。

    并发请求单飞：一次成功灌表，失败时等待方共享同一异常（不再各自重扫）。
    """
    skill_dir = Path(skill_dir)
    root = _root_key(skill_dir)
    skillhub_key = _skillhub_fingerprint(skillhub)
    with pooled_connection(db_path) as conn:
        if _meta_ready(conn, root, skillhub_key):
            return
    flight_key = (root, skillhub_key)
    owner = False
    with _BACKFILL_LOCKS_GUARD:
        with pooled_connection(db_path) as conn:
            if _meta_ready(conn, root, skillhub_key):
                return
        flight = _BACKFILL_FLIGHTS.get(flight_key)
        if flight is None:
            flight = _BackfillFlight()
            _BACKFILL_FLIGHTS[flight_key] = flight
            owner = True
    if not owner:
        if not flight.done.wait(timeout=_BACKFILL_WAIT_TIMEOUT_SECONDS):
            raise TimeoutError(
                f"skills_catalog backfill wait timed out after "
                f"{_BACKFILL_WAIT_TIMEOUT_SECONDS}s: root={root}"
            )
        if flight.error is not None:
            raise flight.error
        return
    try:
        backfill_skills_catalog(skill_dir, skillhub=skillhub, db_path=db_path)
    except BaseException as error:
        flight.error = error
        raise
    finally:
        with _BACKFILL_LOCKS_GUARD:
            if _BACKFILL_FLIGHTS.get(flight_key) is flight:
                _BACKFILL_FLIGHTS.pop(flight_key, None)
        flight.done.set()


def list_skills_catalog(
    skill_dir: Path | str,
    skillhub=None,
    *,
    db_path: Optional[Path] = None,
) -> list[dict]:
    """读投影表全量（API 行形状）。"""
    ensure_skills_catalog(skill_dir, skillhub=skillhub, db_path=db_path)
    root = _root_key(skill_dir)
    with pooled_connection(db_path) as conn:
        rows = conn.execute(
            f"""
            SELECT name, state, source, description, version, candidates_count,
                   hub, skill_id, search_id, use_count
            FROM skills_catalog
            WHERE root_key=?
            ORDER BY {_STATE_ORDER_SQL},
                     CASE WHEN source='skillhub' THEN hub ELSE '' END,
                     name
            """,
            (root,),
        ).fetchall()
    out: list[dict] = []
    for row in rows:
        stored = dict(row)
        stored["candidates"] = stored.pop("candidates_count")
        if not stored.get("skill_id"):
            stored["skill_id"] = stored.get("search_id") or ""
        out.append(catalog_api_row(stored))
    return out


def page_skills_catalog(
    skill_dir: Path | str,
    skillhub=None,
    *,
    limit: int = 0,
    offset: int = 0,
    name: str = "",
    q: str = "",
    db_path: Optional[Path] = None,
) -> dict:
    """分页读投影表；形状与旧 skills_catalog_page 一致。

    ``name`` 精确匹配；``q`` 对 name/description 做子串模糊（忽略大小写）。
    ``by_state`` 始终按全库统计；``total`` 在有 ``q``/``name`` 时为过滤后条数。
    """
    ensure_skills_catalog(skill_dir, skillhub=skillhub, db_path=db_path)
    root = _root_key(skill_dir)
    qn = (q or "").strip()
    with pooled_connection(db_path) as conn:
        by_state_rows = conn.execute(
            """
            SELECT state, COUNT(*) AS n FROM skills_catalog
            WHERE root_key=? GROUP BY state
            """,
            (root,),
        ).fetchall()
        by_state = {row["state"]: row["n"] for row in by_state_rows}
        if name:
            selected = conn.execute(
                f"""
                SELECT name, state, source, description, version, candidates_count,
                       hub, skill_id, search_id, use_count
                FROM skills_catalog
                WHERE root_key=? AND name=?
                ORDER BY {_STATE_ORDER_SQL},
                         CASE WHEN source='skillhub' THEN hub ELSE '' END,
                         name
                """,
                (root, name),
            ).fetchall()
            total = len(selected)
        elif qn:
            like = f"%{qn}%"
            total = conn.execute(
                """
                SELECT COUNT(*) AS n FROM skills_catalog
                WHERE root_key=? AND (
                    name LIKE ? COLLATE NOCASE
                    OR IFNULL(description, '') LIKE ? COLLATE NOCASE
                )
                """,
                (root, like, like),
            ).fetchone()["n"]
            query = f"""
                SELECT name, state, source, description, version, candidates_count,
                       hub, skill_id, search_id, use_count
                FROM skills_catalog
                WHERE root_key=? AND (
                    name LIKE ? COLLATE NOCASE
                    OR IFNULL(description, '') LIKE ? COLLATE NOCASE
                )
                ORDER BY {_STATE_ORDER_SQL},
                         CASE WHEN source='skillhub' THEN hub ELSE '' END,
                         name
            """
            params: list = [root, like, like]
            if limit > 0:
                query += " LIMIT ? OFFSET ?"
                params.extend([limit, offset])
            elif offset > 0:
                query += " LIMIT -1 OFFSET ?"
                params.append(offset)
            selected = conn.execute(query, params).fetchall()
        else:
            total = conn.execute(
                "SELECT COUNT(*) AS n FROM skills_catalog WHERE root_key=?",
                (root,),
            ).fetchone()["n"]
            query = f"""
                SELECT name, state, source, description, version, candidates_count,
                       hub, skill_id, search_id, use_count
                FROM skills_catalog
                WHERE root_key=?
                ORDER BY {_STATE_ORDER_SQL},
                         CASE WHEN source='skillhub' THEN hub ELSE '' END,
                         name
            """
            params = [root]
            if limit > 0:
                query += " LIMIT ? OFFSET ?"
                params.extend([limit, offset])
            elif offset > 0:
                query += " LIMIT -1 OFFSET ?"
                params.append(offset)
            selected = conn.execute(query, params).fetchall()
    skills: list[dict] = []
    for row in selected:
        stored = dict(row)
        stored["candidates"] = stored.pop("candidates_count")
        if not stored.get("skill_id"):
            stored["skill_id"] = stored.get("search_id") or ""
        skills.append(catalog_api_row(stored))
    out = {
        "total": int(total),
        "by_state": by_state,
        "offset": offset,
        "limit": limit,
        "skills": skills,
    }
    if qn:
        out["q"] = qn
    return out
