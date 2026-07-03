"""SQLite state for team client trajectory uploads."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Iterable


class TrajectoryUploadStateStore:
    """Persist per-trajectory upload state for one team server."""

    def __init__(
        self,
        *,
        db_path: Path,
        legacy_cursor_path: Path | None = None,
        home_root: Path | None = None,
        time_fn=time.time,
    ):
        self.db_path = Path(db_path)
        self.legacy_cursor_path = Path(legacy_cursor_path) if legacy_cursor_path else None
        self.home_root = Path(home_root) if home_root else Path.home()
        self._now = time_fn
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.row_factory = sqlite3.Row
        self._init_schema()
        self._migrate_legacy_json_once()

    def _init_schema(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS trajectory_upload_state (
              trajectory_id TEXT PRIMARY KEY,
              file_path TEXT NOT NULL,
              harness_name TEXT DEFAULT '',
              model_name TEXT DEFAULT '',

              file_size_bytes INTEGER,
              file_modified_time_nanoseconds INTEGER,
              file_changed_time_nanoseconds INTEGER,

              original_content_hash TEXT,
              cleaned_content_hash TEXT,

              uploaded_cleaned_content_hash TEXT,
              uploaded_at_seconds REAL,

              waiting_content_hash TEXT,
              waiting_started_at_seconds REAL,

              first_seen_at_seconds REAL NOT NULL,
              last_seen_at_seconds REAL NOT NULL,
              updated_at_seconds REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS client_state_metadata (
              key TEXT PRIMARY KEY,
              value TEXT NOT NULL
            );
            """
        )
        self._conn.commit()

    def _migrate_legacy_json_once(self) -> None:
        if self._metadata_get("legacy_json_migrated_at") is not None:
            return
        now = self._now()
        path_by_id = self._known_trajectory_paths()
        if self.legacy_cursor_path and self.legacy_cursor_path.is_file():
            for trajectory_id, uploaded_hash in self._read_json_dict(
                self.legacy_cursor_path
            ).items():
                if isinstance(uploaded_hash, str) and uploaded_hash:
                    self._upsert_legacy_uploaded(
                        trajectory_id=trajectory_id,
                        uploaded_cleaned_content_hash=uploaded_hash,
                        file_path=str(path_by_id.get(trajectory_id, "")),
                        now=now,
                    )

        legacy_debounce_path = (
            self.legacy_cursor_path.with_suffix(".debounce.json")
            if self.legacy_cursor_path else None
        )
        if legacy_debounce_path and legacy_debounce_path.is_file():
            for trajectory_id, state in self._read_json_dict(
                legacy_debounce_path
            ).items():
                if not isinstance(state, dict):
                    continue
                waiting_hash = state.get("sha")
                waiting_since = state.get("since")
                if isinstance(waiting_hash, str) and waiting_hash:
                    try:
                        waiting_since = float(waiting_since)
                    except (TypeError, ValueError):
                        waiting_since = now
                    self._upsert_legacy_waiting(
                        trajectory_id=trajectory_id,
                        waiting_content_hash=waiting_hash,
                        waiting_started_at_seconds=waiting_since,
                        file_path=str(path_by_id.get(trajectory_id, "")),
                        now=now,
                    )
        self._metadata_set("legacy_json_migrated_at", str(now))
        self._conn.commit()

    def _known_trajectory_paths(self) -> dict[str, Path]:
        bridge_root = self.home_root / ".xskill"
        if not bridge_root.is_dir():
            return {}
        return {
            path.stem: path
            for path in bridge_root.glob("*_sessions/traj_*.md")
            if path.is_file()
        }

    @staticmethod
    def _read_json_dict(path: Path) -> dict:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _metadata_get(self, key: str) -> str | None:
        row = self._conn.execute(
            "SELECT value FROM client_state_metadata WHERE key = ?", (key,)
        ).fetchone()
        return None if row is None else str(row["value"])

    def _metadata_set(self, key: str, value: str) -> None:
        self._conn.execute(
            """
            INSERT INTO client_state_metadata (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )

    def _upsert_legacy_uploaded(
        self,
        *,
        trajectory_id: str,
        uploaded_cleaned_content_hash: str,
        file_path: str,
        now: float,
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO trajectory_upload_state (
              trajectory_id, file_path, uploaded_cleaned_content_hash,
              first_seen_at_seconds, last_seen_at_seconds, updated_at_seconds
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(trajectory_id) DO UPDATE SET
              file_path = CASE
                WHEN trajectory_upload_state.file_path = '' THEN excluded.file_path
                ELSE trajectory_upload_state.file_path
              END,
              uploaded_cleaned_content_hash = COALESCE(
                trajectory_upload_state.uploaded_cleaned_content_hash,
                excluded.uploaded_cleaned_content_hash
              ),
              last_seen_at_seconds = excluded.last_seen_at_seconds,
              updated_at_seconds = excluded.updated_at_seconds
            """,
            (trajectory_id, file_path, uploaded_cleaned_content_hash, now, now, now),
        )

    def _upsert_legacy_waiting(
        self,
        *,
        trajectory_id: str,
        waiting_content_hash: str,
        waiting_started_at_seconds: float,
        file_path: str,
        now: float,
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO trajectory_upload_state (
              trajectory_id, file_path, waiting_content_hash,
              waiting_started_at_seconds, first_seen_at_seconds,
              last_seen_at_seconds, updated_at_seconds
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(trajectory_id) DO UPDATE SET
              file_path = CASE
                WHEN trajectory_upload_state.file_path = '' THEN excluded.file_path
                ELSE trajectory_upload_state.file_path
              END,
              waiting_content_hash = COALESCE(
                trajectory_upload_state.waiting_content_hash,
                excluded.waiting_content_hash
              ),
              waiting_started_at_seconds = COALESCE(
                trajectory_upload_state.waiting_started_at_seconds,
                excluded.waiting_started_at_seconds
              ),
              last_seen_at_seconds = excluded.last_seen_at_seconds,
              updated_at_seconds = excluded.updated_at_seconds
            """,
            (
                trajectory_id,
                file_path,
                waiting_content_hash,
                waiting_started_at_seconds,
                now,
                now,
                now,
            ),
        )

    def get(self, trajectory_id: str) -> sqlite3.Row | None:
        return self._conn.execute(
            """
            SELECT * FROM trajectory_upload_state
            WHERE trajectory_id = ?
            """,
            (trajectory_id,),
        ).fetchone()

    def record_seen_file(
        self,
        *,
        trajectory_id: str,
        file_path: str,
        harness_name: str,
        model_name: str,
        file_size_bytes: int,
        file_modified_time_nanoseconds: int,
        file_changed_time_nanoseconds: int,
        original_content_hash: str | None = None,
        cleaned_content_hash: str | None = None,
    ) -> None:
        now = self._now()
        self._conn.execute(
            """
            INSERT INTO trajectory_upload_state (
              trajectory_id, file_path, harness_name, model_name,
              file_size_bytes, file_modified_time_nanoseconds,
              file_changed_time_nanoseconds, original_content_hash,
              cleaned_content_hash, first_seen_at_seconds,
              last_seen_at_seconds, updated_at_seconds
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(trajectory_id) DO UPDATE SET
              file_path = excluded.file_path,
              harness_name = excluded.harness_name,
              model_name = excluded.model_name,
              file_size_bytes = excluded.file_size_bytes,
              file_modified_time_nanoseconds =
                excluded.file_modified_time_nanoseconds,
              file_changed_time_nanoseconds =
                excluded.file_changed_time_nanoseconds,
              original_content_hash = COALESCE(
                excluded.original_content_hash,
                trajectory_upload_state.original_content_hash
              ),
              cleaned_content_hash = COALESCE(
                excluded.cleaned_content_hash,
                trajectory_upload_state.cleaned_content_hash
              ),
              last_seen_at_seconds = excluded.last_seen_at_seconds,
              updated_at_seconds = excluded.updated_at_seconds
            """,
            (
                trajectory_id,
                file_path,
                harness_name,
                model_name,
                file_size_bytes,
                file_modified_time_nanoseconds,
                file_changed_time_nanoseconds,
                original_content_hash,
                cleaned_content_hash,
                now,
                now,
                now,
            ),
        )
        self._conn.commit()

    def set_waiting(
        self,
        *,
        trajectory_id: str,
        waiting_content_hash: str,
        waiting_started_at_seconds: float,
    ) -> None:
        now = self._now()
        self._conn.execute(
            """
            UPDATE trajectory_upload_state
            SET waiting_content_hash = ?,
                waiting_started_at_seconds = ?,
                updated_at_seconds = ?
            WHERE trajectory_id = ?
            """,
            (waiting_content_hash, waiting_started_at_seconds, now, trajectory_id),
        )
        self._conn.commit()

    def clear_waiting(self, trajectory_id: str) -> None:
        now = self._now()
        self._conn.execute(
            """
            UPDATE trajectory_upload_state
            SET waiting_content_hash = NULL,
                waiting_started_at_seconds = NULL,
                updated_at_seconds = ?
            WHERE trajectory_id = ?
            """,
            (now, trajectory_id),
        )
        self._conn.commit()

    def clear_waiting_for_missing(self, seen_trajectory_ids: Iterable[str]) -> None:
        seen = set(seen_trajectory_ids)
        if not seen:
            self._conn.execute(
                """
                UPDATE trajectory_upload_state
                SET waiting_content_hash = NULL,
                    waiting_started_at_seconds = NULL,
                    updated_at_seconds = ?
                WHERE waiting_content_hash IS NOT NULL
                """,
                (self._now(),),
            )
            self._conn.commit()
            return
        rows = self._conn.execute(
            """
            SELECT trajectory_id FROM trajectory_upload_state
            WHERE waiting_content_hash IS NOT NULL
            """
        ).fetchall()
        for row in rows:
            if row["trajectory_id"] not in seen:
                self.clear_waiting(str(row["trajectory_id"]))

    def mark_uploaded(self, trajectory_id: str, cleaned_content_hash: str) -> None:
        now = self._now()
        self._conn.execute(
            """
            INSERT INTO trajectory_upload_state (
              trajectory_id, file_path, uploaded_cleaned_content_hash,
              uploaded_at_seconds, first_seen_at_seconds,
              last_seen_at_seconds, updated_at_seconds
            ) VALUES (?, '', ?, ?, ?, ?, ?)
            ON CONFLICT(trajectory_id) DO UPDATE SET
              uploaded_cleaned_content_hash =
                excluded.uploaded_cleaned_content_hash,
              uploaded_at_seconds = excluded.uploaded_at_seconds,
              waiting_content_hash = NULL,
              waiting_started_at_seconds = NULL,
              last_seen_at_seconds = excluded.last_seen_at_seconds,
              updated_at_seconds = excluded.updated_at_seconds
            """,
            (trajectory_id, cleaned_content_hash, now, now, now, now),
        )
        self._conn.commit()

    def uploaded_trajectory_ids(self) -> set[str]:
        rows = self._conn.execute(
            """
            SELECT trajectory_id FROM trajectory_upload_state
            WHERE uploaded_cleaned_content_hash IS NOT NULL
            """
        ).fetchall()
        return {str(row["trajectory_id"]) for row in rows}
