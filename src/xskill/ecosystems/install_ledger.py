"""本机安装账（InstallLedger）— SQLite 统一记录安装与卸装事务。

设计要点（相对旁路文件账本）：
- 账本只落 ``~/.xskill/installations.sqlite``（可用环境变量覆盖路径）
- 用户生态目录不再写 sidecar / removal 临时文件 / copy identity marker
- 同 dest 重装时原子作废未完成卸装（supersede），消除 PREPARED_MISMATCH 刷屏根因
- 卸装与 FS 删除两阶段；隔离目录仅在 ``~/.xskill/removal-staging/``
"""
from __future__ import annotations

from xskill.skill.write_policy import require_install_write, require_management_write, current_write_policy, SkillWriteDenied

import json
import logging
import os
import secrets
import shutil
import sqlite3
import stat
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Literal

from xskill.config import XSKILL_HOME
from xskill.recommend._sqlite_base import _SqliteStore

logger = logging.getLogger("xskill.install_ledger")

RemovalState = Literal[
    "pending", "deleting", "done", "superseded", "aborted",
]
InstallStatus = Literal["active", "tombstone"]

_ENV_LEDGER = "XSKILL_INSTALL_LEDGER"
_INSTALL_META_PREFIX = ".xskill-install-meta-"
_COPY_MARKER = ".xskill-install-identity.json"

_TLS = threading.local()
_DEFAULT_LOCK = threading.Lock()
_DEFAULT_LEDGER: "InstallLedger | None" = None


def dest_key(path: Path | str) -> str:
    """规范化绝对路径键（跨平台大小写按 normcase）。"""
    return os.path.normcase(
        os.path.abspath(os.path.normpath(str(path))),
    )


def default_ledger_db_path(*, xskill_home: Path | None = None) -> Path:
    override = os.environ.get(_ENV_LEDGER)
    if override:
        return Path(override).expanduser()
    home = Path(xskill_home) if xskill_home is not None else XSKILL_HOME
    return home / "installations.sqlite"


def default_staging_root(*, xskill_home: Path | None = None) -> Path:
    home = Path(xskill_home) if xskill_home is not None else XSKILL_HOME
    return home / "removal-staging"


def reset_default_ledger() -> None:
    """测试用：丢掉进程默认单例，下次按环境变量/路径重建。"""
    global _DEFAULT_LEDGER
    with _DEFAULT_LOCK:
        _DEFAULT_LEDGER = None
    if hasattr(_TLS, "ledger"):
        delattr(_TLS, "ledger")


def push_ledger(ledger: "InstallLedger") -> None:
    """测试用：当前线程临时覆盖默认 ledger。"""
    _TLS.ledger = ledger


def pop_ledger() -> None:
    if hasattr(_TLS, "ledger"):
        delattr(_TLS, "ledger")


@contextmanager
def use_ledger(ledger: "InstallLedger"):
    """Bind an explicit instance ledger to this thread, restoring nested callers."""
    previous = getattr(_TLS, "ledger", None)
    push_ledger(ledger)
    try:
        yield ledger
    finally:
        if previous is None:
            pop_ledger()
        else:
            push_ledger(previous)


def get_default_ledger(*, xskill_home: Path | None = None) -> "InstallLedger":
    thread_ledger = getattr(_TLS, "ledger", None)
    if thread_ledger is not None:
        return thread_ledger
    global _DEFAULT_LEDGER
    with _DEFAULT_LOCK:
        db_path = default_ledger_db_path(xskill_home=xskill_home)
        if (
            _DEFAULT_LEDGER is None
            or _DEFAULT_LEDGER.db_path.resolve() != db_path.expanduser().resolve()
        ):
            staging = default_staging_root(xskill_home=xskill_home)
            _DEFAULT_LEDGER = InstallLedger(db_path, staging_root=staging)
        return _DEFAULT_LEDGER


class InstallLedger(_SqliteStore):
    """installations + removal_jobs。"""

    _SCHEMA = """
    CREATE TABLE IF NOT EXISTS installations (
        dest_key TEXT PRIMARY KEY,
        skill_name TEXT NOT NULL,
        mode TEXT NOT NULL,
        source TEXT NOT NULL,
        source_sha TEXT NOT NULL DEFAULT '',
        installation_id TEXT NOT NULL,
        content_identity TEXT NOT NULL,
        baseline_identity TEXT,
        file_fingerprints_json TEXT,
        generation INTEGER NOT NULL DEFAULT 1,
        installed_at REAL NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('active', 'tombstone'))
    );
    CREATE INDEX IF NOT EXISTS idx_installations_skill
        ON installations(skill_name);
    CREATE INDEX IF NOT EXISTS idx_installations_status
        ON installations(status);

    CREATE TABLE IF NOT EXISTS removal_jobs (
        job_id TEXT PRIMARY KEY,
        dest_key TEXT NOT NULL,
        expected_generation INTEGER NOT NULL,
        expected_installation_id TEXT NOT NULL,
        expected_content_identity TEXT NOT NULL,
        expected_target_identity_json TEXT,
        state TEXT NOT NULL CHECK(state IN (
            'pending', 'deleting', 'done', 'superseded', 'aborted'
        )),
        mode TEXT NOT NULL,
        updated_at REAL NOT NULL,
        last_error TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_removal_jobs_dest_state
        ON removal_jobs(dest_key, state);
    """

    def __init__(
        self,
        db_path: Path | str,
        *,
        staging_root: Path | str | None = None,
    ):
        self.staging_root = Path(
            staging_root
            if staging_root is not None
            else Path(db_path).expanduser().resolve().parent / "removal-staging"
        )
        self.staging_root.mkdir(parents=True, exist_ok=True)
        super().__init__(db_path)

    # ── read / write install ─────────────────────────────────────

    def read_install(self, dest: Path | str) -> dict | None:
        key = dest_key(dest)
        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT * FROM installations WHERE dest_key=? AND status='active'",
                (key,),
            ).fetchone()
            if row is None:
                return None
            return self._row_to_metadata(row)
        finally:
            conn.close()

    def read_install_row(self, dest: Path | str) -> sqlite3.Row | None:
        key = dest_key(dest)
        conn = self._conn()
        try:
            return conn.execute(
                "SELECT * FROM installations WHERE dest_key=?",
                (key,),
            ).fetchone()
        finally:
            conn.close()

    def list_active_install_targets(
        self,
        *,
        mode: str | None = None,
        skill_name: str | None = None,
    ) -> list[dict[str, str]]:
        """轻量返回稳定排序的 active 安装目标，不解析基线 JSON。"""
        clauses = ["status='active'"]
        parameters: list[str] = []
        if mode is not None:
            clauses.append("mode=?")
            parameters.append(mode)
        if skill_name is not None:
            clauses.append("skill_name=?")
            parameters.append(skill_name)
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT dest_key, skill_name FROM installations WHERE "
                + " AND ".join(clauses)
                + " ORDER BY skill_name, dest_key",
                tuple(parameters),
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    def _require_destination_write(self, dest, *, removal=False):
        row = self.read_install_row(dest)
        if row is not None:
            source = Path(row["source"])
            if removal:
                root = source.parent.parent if source.parent.name == ".canary" else source.parent
                require_management_write(root, operation="uninstall")
            else:
                require_install_write(source, dest)
        else:
            policy = current_write_policy()
            if policy.mode != "auto":
                if removal:
                    raise SkillWriteDenied("uninstall unavailable in restricted run")
                require_install_write(Path(policy.root_key) / policy.target_skill, dest)

    def _require_job_write(self, job_id):
        conn = self._conn()
        try:
            row = conn.execute("SELECT dest_key FROM removal_jobs WHERE job_id=?", (job_id,)).fetchone()
        finally:
            conn.close()
        if row is not None:
            self._require_destination_write(row["dest_key"], removal=True)

    def record_install(
        self,
        dest: Path | str,
        *,
        skill_name: str,
        mode: str,
        source: str,
        source_sha: str,
        installation_id: str,
        content_identity: str,
        baseline_identity: str | None = None,
        file_fingerprints: dict | None = None,
        installed_at: float | None = None,
    ) -> dict:
        """原子：作废未完成卸装 + upsert active 安装（generation+1）。"""
        policy = require_install_write(source, dest)
        if policy.mode != 'auto':
            policy.require_skill(skill_name, operation='publish')
        key = dest_key(dest)
        now = float(installed_at if installed_at is not None else time.time())
        fp_json = (
            json.dumps(file_fingerprints, ensure_ascii=False, sort_keys=True)
            if file_fingerprints is not None
            else None
        )
        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            self._supersede_open_jobs_locked(conn, key, now)
            prev = conn.execute(
                "SELECT generation FROM installations WHERE dest_key=?",
                (key,),
            ).fetchone()
            generation = int(prev["generation"]) + 1 if prev is not None else 1
            conn.execute(
                """
                INSERT INTO installations(
                    dest_key, skill_name, mode, source, source_sha,
                    installation_id, content_identity, baseline_identity,
                    file_fingerprints_json, generation, installed_at, status
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?, 'active')
                ON CONFLICT(dest_key) DO UPDATE SET
                    skill_name=excluded.skill_name,
                    mode=excluded.mode,
                    source=excluded.source,
                    source_sha=excluded.source_sha,
                    installation_id=excluded.installation_id,
                    content_identity=excluded.content_identity,
                    baseline_identity=excluded.baseline_identity,
                    file_fingerprints_json=excluded.file_fingerprints_json,
                    generation=excluded.generation,
                    installed_at=excluded.installed_at,
                    status='active'
                """,
                (
                    key, skill_name, mode, source, source_sha,
                    installation_id, content_identity, baseline_identity,
                    fp_json, generation, now,
                ),
            )
            conn.commit()
            return {
                "mode": mode,
                "source": source,
                "source_sha": source_sha,
                "installed_at": now,
                "installation_id": installation_id,
                "content_identity": content_identity,
                "baseline_identity": baseline_identity,
                "file_fingerprints": file_fingerprints,
                "generation": generation,
            }
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def update_copy_baseline(
        self,
        dest: Path | str,
        *,
        file_fingerprints: dict[str, str],
        baseline_identity: str,
    ) -> bool:
        """同步更新 active copy 的内容指纹，不升 generation / 不换 installation_id。

        供安装器在 ``record_install`` 之后再次写入 dest（如 openclaw 老位置
        meta、auxiliary 刷新）时调用，保证账本等于「我们最后一次写入的内容」。
        """
        self._require_destination_write(dest)
        key = dest_key(dest)
        fp_json = json.dumps(
            file_fingerprints, ensure_ascii=False, sort_keys=True,
        )
        conn = self._conn()
        try:
            cur = conn.execute(
                """
                UPDATE installations
                SET file_fingerprints_json=?, baseline_identity=?
                WHERE dest_key=? AND status='active' AND mode='copy'
                """,
                (fp_json, baseline_identity, key),
            )
            conn.commit()
            return cur.rowcount > 0
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def compare_and_swap_copy_baseline(
        self,
        dest: Path | str,
        *,
        expected_generation: int,
        expected_installation_id: str,
        expected_baseline_identity: str,
        file_fingerprints: dict[str, str],
        baseline_identity: str,
    ) -> bool:
        """在安装世代与旧基线均未变化时更新 active copy 基线。"""
        self._require_destination_write(dest)
        key = dest_key(dest)
        fp_json = json.dumps(
            file_fingerprints, ensure_ascii=False, sort_keys=True,
        )
        conn = self._conn()
        try:
            cur = conn.execute(
                """
                UPDATE installations
                SET file_fingerprints_json=?, baseline_identity=?
                WHERE dest_key=? AND status='active' AND mode='copy'
                  AND generation=? AND installation_id=?
                  AND baseline_identity=?
                  AND NOT EXISTS (
                      SELECT 1 FROM removal_jobs
                      WHERE removal_jobs.dest_key=installations.dest_key
                        AND state IN ('pending', 'deleting')
                  )
                """,
                (
                    fp_json,
                    baseline_identity,
                    key,
                    expected_generation,
                    expected_installation_id,
                    expected_baseline_identity,
                ),
            )
            conn.commit()
            return cur.rowcount == 1
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def supersede_removals(self, dest: Path | str) -> int:
        self._require_destination_write(dest)
        key = dest_key(dest)
        now = time.time()
        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            n = self._supersede_open_jobs_locked(conn, key, now)
            conn.commit()
            return n
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _supersede_open_jobs_locked(
        self, conn: sqlite3.Connection, key: str, now: float,
    ) -> int:
        cur = conn.execute(
            """
            UPDATE removal_jobs
            SET state='superseded', updated_at=?, last_error=NULL
            WHERE dest_key=? AND state IN ('pending', 'deleting')
            """,
            (now, key),
        )
        count = cur.rowcount if cur.rowcount is not None else 0
        # 清理对应 staging（best-effort，不在事务失败路径）
        for row in conn.execute(
            "SELECT job_id FROM removal_jobs WHERE dest_key=? AND state='superseded'",
            (key,),
        ):
            self._cleanup_staging(row["job_id"])
        return int(count)

    # ── removal jobs ─────────────────────────────────────────────

    def begin_removal(
        self,
        dest: Path | str,
        *,
        target_identity: tuple[int, int, int] | list[int] | None = None,
    ) -> dict | None:
        """对 active 安装开卸装 job；无 active 则返回 None。"""
        self._require_destination_write(dest, removal=True)
        key = dest_key(dest)
        now = time.time()
        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM installations WHERE dest_key=? AND status='active'",
                (key,),
            ).fetchone()
            if row is None:
                conn.commit()
                return None
            # 已有活 job：直接返回，避免重复刷
            existing = conn.execute(
                """
                SELECT * FROM removal_jobs
                WHERE dest_key=? AND state IN ('pending', 'deleting')
                ORDER BY updated_at DESC LIMIT 1
                """,
                (key,),
            ).fetchone()
            if existing is not None:
                conn.commit()
                return dict(existing)
            # generation 已变但旧 job 不应存在；若有 aborted 忽略
            job_id = secrets.token_hex(12)
            identity_json = (
                json.dumps(list(target_identity))
                if target_identity is not None
                else None
            )
            mode = "link" if row["mode"] in {"symlink", "junction"} else "copy"
            conn.execute(
                """
                INSERT INTO removal_jobs(
                    job_id, dest_key, expected_generation,
                    expected_installation_id, expected_content_identity,
                    expected_target_identity_json, state, mode, updated_at
                ) VALUES (?,?,?,?,?,?, 'pending', ?, ?)
                """,
                (
                    job_id, key, int(row["generation"]),
                    row["installation_id"], row["content_identity"],
                    identity_json, mode, now,
                ),
            )
            conn.commit()
            return {
                "job_id": job_id,
                "dest_key": key,
                "expected_generation": int(row["generation"]),
                "expected_installation_id": row["installation_id"],
                "expected_content_identity": row["content_identity"],
                "expected_target_identity_json": identity_json,
                "state": "pending",
                "mode": mode,
                "updated_at": now,
            }
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def mark_deleting(self, job_id: str) -> bool:
        self._require_job_write(job_id)
        now = time.time()
        conn = self._conn()
        try:
            cur = conn.execute(
                """
                UPDATE removal_jobs SET state='deleting', updated_at=?
                WHERE job_id=? AND state='pending'
                """,
                (now, job_id),
            )
            conn.commit()
            return cur.rowcount == 1
        finally:
            conn.close()

    def finish_removal(
        self,
        job_id: str,
        *,
        ok: bool,
        error: str | None = None,
    ) -> str:
        """成功则 tombstone 安装行；generation 已变则视为 superseded。"""
        self._require_job_write(job_id)
        now = time.time()
        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            job = conn.execute(
                "SELECT * FROM removal_jobs WHERE job_id=?",
                (job_id,),
            ).fetchone()
            if job is None:
                conn.commit()
                return "missing"
            if job["state"] in {"done", "superseded", "aborted"}:
                conn.commit()
                return job["state"]
            inst = conn.execute(
                "SELECT * FROM installations WHERE dest_key=?",
                (job["dest_key"],),
            ).fetchone()
            if (
                inst is None
                or int(inst["generation"]) != int(job["expected_generation"])
                or inst["installation_id"] != job["expected_installation_id"]
            ):
                conn.execute(
                    """
                    UPDATE removal_jobs
                    SET state='superseded', updated_at=?, last_error=?
                    WHERE job_id=?
                    """,
                    (now, error, job_id),
                )
                conn.commit()
                self._cleanup_staging(job_id)
                return "superseded"
            if not ok:
                conn.execute(
                    """
                    UPDATE removal_jobs
                    SET state='aborted', updated_at=?, last_error=?
                    WHERE job_id=?
                    """,
                    (now, error or "removal_failed", job_id),
                )
                conn.commit()
                return "aborted"
            conn.execute(
                """
                UPDATE installations SET status='tombstone'
                WHERE dest_key=? AND generation=?
                """,
                (job["dest_key"], int(job["expected_generation"])),
            )
            conn.execute(
                """
                UPDATE removal_jobs
                SET state='done', updated_at=?, last_error=NULL
                WHERE job_id=?
                """,
                (now, job_id),
            )
            conn.commit()
            self._cleanup_staging(job_id)
            return "done"
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def get_open_removal(self, dest: Path | str) -> dict | None:
        key = dest_key(dest)
        conn = self._conn()
        try:
            row = conn.execute(
                """
                SELECT * FROM removal_jobs
                WHERE dest_key=? AND state IN ('pending', 'deleting')
                ORDER BY updated_at DESC LIMIT 1
                """,
                (key,),
            ).fetchone()
            return dict(row) if row is not None else None
        finally:
            conn.close()

    def list_open_removals(self) -> list[dict]:
        conn = self._conn()
        try:
            rows = conn.execute(
                """
                SELECT * FROM removal_jobs
                WHERE state IN ('pending', 'deleting')
                ORDER BY updated_at
                """,
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def staging_path(self, job_id: str) -> Path:
        return self.staging_root / job_id

    def _cleanup_staging(self, job_id: str) -> None:
        self._require_job_write(job_id)
        path = self.staging_path(job_id)
        if not path.exists():
            return
        try:
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path, ignore_errors=True)
            else:
                path.unlink(missing_ok=True)
        except OSError:
            logger.warning(
                "removal staging cleanup failed job_id=%s", job_id[:12],
            )

    # ── migrate legacy sidecars ──────────────────────────────────

    def migrate_from_sidecars(
        self, ecosystem_roots: Iterable[Path],
    ) -> dict[str, int]:
        """扫描生态根，导入 sidecar/事务旁路后删除这些隐藏文件。"""
        stats = {
            "installs_imported": 0,
            "removals_imported": 0,
            "files_removed": 0,
            "errors": 0,
        }
        for root in ecosystem_roots:
            root = Path(root)
            if not root.is_dir():
                continue
            try:
                entries = list(root.iterdir())
            except OSError:
                stats["errors"] += 1
                continue
            for entry in entries:
                name = entry.name
                try:
                    if name.startswith(_INSTALL_META_PREFIX) and name.endswith(
                        ".json",
                    ) and ".removal-transaction" not in name and ".removing" not in name:
                        dest_name = name[len(_INSTALL_META_PREFIX):-len(".json")]
                        dest = root / dest_name
                        if self._import_sidecar(entry, dest):
                            stats["installs_imported"] += 1
                            entry.unlink(missing_ok=True)
                            stats["files_removed"] += 1
                        else:
                            # 导入失败必须保留原文件等下轮重试：账本行缺失时
                            # sidecar 是该 dest 唯一的安装记录，删掉即成孤儿。
                            stats["errors"] += 1
                    elif ".removal-transaction-" in name:
                        if self._import_removal_record(entry, root):
                            stats["removals_imported"] += 1
                            entry.unlink(missing_ok=True)
                            stats["files_removed"] += 1
                        else:
                            stats["errors"] += 1
                    elif ".removing-" in name or name.startswith(
                        ".xskill-removing-target-",
                    ):
                        # 旁路隔离物：直接清掉（用户目录不再保留）
                        if entry.is_symlink() or entry.is_file():
                            entry.unlink(missing_ok=True)
                        else:
                            shutil.rmtree(entry, ignore_errors=True)
                        stats["files_removed"] += 1
                    elif entry.is_dir() and not entry.is_symlink():
                        marker = entry / _COPY_MARKER
                        if marker.is_file():
                            # 身份已在 sidecar 导入；只删 marker
                            marker.unlink(missing_ok=True)
                            stats["files_removed"] += 1
                except OSError:
                    stats["errors"] += 1
                    logger.warning(
                        "sidecar migrate failed name=%s", name[:64],
                    )
        return stats

    def _import_sidecar(self, metadata_path: Path, dest: Path) -> bool:
        try:
            raw = metadata_path.read_text(encoding="utf-8")
            meta = json.loads(raw)
        except (OSError, ValueError, TypeError):
            return False
        if not isinstance(meta, dict):
            return False
        mode = meta.get("mode")
        source = meta.get("source")
        installation_id = meta.get("installation_id")
        content_identity = meta.get("content_identity")
        if (
            mode not in {"symlink", "junction", "copy"}
            or not isinstance(source, str)
            or not isinstance(installation_id, str)
            or not isinstance(content_identity, str)
        ):
            return False
        # 已有更新 generation 则不覆盖
        existing = self.read_install_row(dest)
        if existing is not None and existing["status"] == "active":
            return False
        skill_name = dest.name
        self.record_install(
            dest,
            skill_name=skill_name,
            mode=mode,
            source=source,
            source_sha=str(meta.get("source_sha") or ""),
            installation_id=installation_id,
            content_identity=content_identity,
            baseline_identity=(
                meta["baseline_identity"]
                if isinstance(meta.get("baseline_identity"), str)
                else None
            ),
            file_fingerprints=(
                meta["file_fingerprints"]
                if isinstance(meta.get("file_fingerprints"), dict)
                else None
            ),
            installed_at=(
                float(meta["installed_at"])
                if isinstance(meta.get("installed_at"), (int, float))
                else None
            ),
        )
        return True

    def _import_removal_record(self, record_path: Path, root: Path) -> bool:
        try:
            raw = record_path.read_text(encoding="utf-8")
            record = json.loads(raw)
        except (OSError, ValueError, TypeError):
            return False
        if not isinstance(record, dict):
            return False
        # 文件名: .xskill-install-meta-<dest>.json.removal-transaction-<id>
        name = record_path.name
        marker = ".json.removal-transaction-"
        if marker not in name or not name.startswith(_INSTALL_META_PREFIX):
            return False
        mid = name.find(marker)
        dest_name = name[len(_INSTALL_META_PREFIX):mid]
        dest = root / dest_name
        job_id = name[mid + len(marker):]
        if len(job_id) != 24:
            job_id = secrets.token_hex(12)
        key = dest_key(dest)
        now = time.time()
        # 旧事务一律标 superseded：盘上多半已换代；活卸装由新状态机重开
        conn = self._conn()
        try:
            conn.execute(
                """
                INSERT OR IGNORE INTO removal_jobs(
                    job_id, dest_key, expected_generation,
                    expected_installation_id, expected_content_identity,
                    expected_target_identity_json, state, mode, updated_at,
                    last_error
                ) VALUES (?,?,?,?,?,?, 'superseded', ?, ?, ?)
                """,
                (
                    job_id, key,
                    0,
                    str(record.get("installation_id") or ""),
                    str(record.get("content_identity") or ""),
                    json.dumps(record.get("target_identity"))
                    if record.get("target_identity") is not None
                    else None,
                    "link" if record.get("mode") == "link" else "copy",
                    now,
                    "migrated_legacy_sidecar",
                ),
            )
            conn.commit()
            return True
        finally:
            conn.close()

    @staticmethod
    def _row_to_metadata(row: sqlite3.Row) -> dict:
        fingerprints = None
        raw_fp = row["file_fingerprints_json"]
        if isinstance(raw_fp, str) and raw_fp:
            try:
                parsed = json.loads(raw_fp)
                if isinstance(parsed, dict):
                    fingerprints = parsed
            except ValueError:
                fingerprints = None
        meta: dict[str, Any] = {
            "mode": row["mode"],
            "source": row["source"],
            "source_sha": row["source_sha"] or "",
            "installed_at": row["installed_at"],
            "installation_id": row["installation_id"],
            "content_identity": row["content_identity"],
            "generation": int(row["generation"]),
        }
        if row["baseline_identity"]:
            meta["baseline_identity"] = row["baseline_identity"]
        if fingerprints is not None:
            meta["file_fingerprints"] = fingerprints
        return meta


def remove_owned_dest(
    dest: Path,
    source_dir: Path | None = None,
    *,
    ledger: InstallLedger | None = None,
    is_link_or_junction: Any = None,
) -> bool:
    """基于 ledger 的卸装：世代匹配才删；否则 superseded 且不刷 MISMATCH。

    copy 还会在删除前复核 ``file_fingerprints`` / ``baseline_identity``：
    同路径被带外替换成别的内容时拒删，避免清掉用户手写目录。
    """
    if source_dir is not None:
        require_management_write(Path(source_dir).parent, operation='uninstall')
    if is_link_or_junction is None:
        from xskill.ecosystems.installation import is_link_or_junction as _ilj
        is_link_or_junction = _ilj

    lg = ledger or get_default_ledger()
    dest = Path(dest)

    if not dest.exists() and not dest.is_symlink():
        job = lg.get_open_removal(dest)
        if job is not None:
            lg.finish_removal(job["job_id"], ok=True)
            return True
        row = lg.read_install_row(dest)
        if row is not None and row["status"] == "active":
            job = lg.begin_removal(dest, target_identity=None)
            if job is not None:
                lg.finish_removal(job["job_id"], ok=True)
            return True
        return lg.read_install(dest) is None

    meta = lg.read_install(dest)
    if meta is None:
        if source_dir is not None and is_link_or_junction(dest):
            try:
                if dest_key(os.path.realpath(str(dest))) == dest_key(
                    source_dir,
                ):
                    dest.unlink()
                    return True
            except OSError:
                return False
        return False

    if source_dir is not None:
        try:
            if dest_key(Path(meta["source"])) != dest_key(source_dir):
                return False
        except (OSError, KeyError, TypeError):
            return False

    # copy：删除前复核 dest 内容指纹，挡住同路径带外替换后的误删。
    if meta.get("mode") == "copy" and not is_link_or_junction(dest):
        expected_fp = meta.get("file_fingerprints")
        expected_baseline = meta.get("baseline_identity")
        if (
            not isinstance(expected_fp, dict)
            or not isinstance(expected_baseline, str)
        ):
            return False
        try:
            from xskill.ecosystems.installation import (
                _copy_baseline_identity,
                _safe_copy_file_fingerprints,
            )

            current_fp = _safe_copy_file_fingerprints(dest)
            if (
                current_fp != expected_fp
                or _copy_baseline_identity(current_fp) != expected_baseline
            ):
                return False
        except OSError:
            return False

    identity = None
    try:
        st = dest.lstat()
        identity = (st.st_dev, st.st_ino, stat.S_IFMT(st.st_mode))
    except OSError:
        return False

    job = lg.begin_removal(dest, target_identity=identity)
    if job is None:
        return False

    # 活 job 来自更早一代（重装未 supersede 的极端窗口）：丢弃
    if int(job["expected_generation"]) != int(meta.get("generation", -1)):
        lg.finish_removal(job["job_id"], ok=False, error="generation_drift")
        return False
    if job["expected_installation_id"] != meta["installation_id"]:
        lg.finish_removal(job["job_id"], ok=False, error="installation_id_drift")
        return False

    lg.mark_deleting(job["job_id"])
    staging = lg.staging_path(job["job_id"])
    try:
        if is_link_or_junction(dest):
            staging.mkdir(parents=True, exist_ok=True)
            (staging / "link-target.txt").write_text(
                str(dest), encoding="utf-8",
            )
            dest.unlink()
        elif dest.is_dir():
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
            dest.rename(staging)
            shutil.rmtree(staging)
        else:
            dest.unlink()
    except FileNotFoundError:
        lg.finish_removal(job["job_id"], ok=True)
        return True
    except OSError as exc:
        lg.finish_removal(
            job["job_id"], ok=False, error=type(exc).__name__,
        )
        logger.warning(
            "install removal fs failed error_type=INSTALL_TARGET_REMOVE_FAILED",
        )
        return False

    state = lg.finish_removal(job["job_id"], ok=True)
    return state in {"done", "superseded"}
