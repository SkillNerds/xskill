"""atom_candidate_pending 投影：写出口同步 + 看板读路径不扫盘。"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest import mock

import pytest

from xskill.dashboard.explore import TrajExplorer, skill_lineage
from xskill.pipeline import registry as reg
from xskill.skill.candidates import (
    CANDIDATES_FILENAME,
    add_atom_contributions,
    remove_candidates,
)


def _write_skill(skill_dir: Path, name: str) -> Path:
    path = skill_dir / name
    path.mkdir(parents=True)
    (path / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: t\n---\n\n# {name}\n",
        encoding="utf-8",
    )
    return path


def _pending_primary_key(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute("PRAGMA table_info(atom_candidate_pending)").fetchall()
    return [
        row["name"]
        for row in sorted(rows, key=lambda row: row["pk"])
        if row["pk"]
    ]


def test_pending_projection_uses_composite_primary_key(tmp_path: Path) -> None:
    conn = reg.get_connection(tmp_path / "registry.db")
    try:
        assert _pending_primary_key(conn) == ["atom_id", "skill"]
        indexes = {
            row["name"]
            for row in conn.execute("PRAGMA index_list(atom_candidate_pending)")
        }
        assert "idx_acp_skill" in indexes
    finally:
        conn.close()


def test_legacy_pending_projection_migrates_without_data_loss(
    tmp_path: Path,
) -> None:
    db = tmp_path / "registry.db"
    legacy = sqlite3.connect(db)
    try:
        legacy.executescript(
            """
            CREATE TABLE atom_candidate_pending (
                atom_id     TEXT PRIMARY KEY,
                skill       TEXT NOT NULL,
                weightscore INTEGER NOT NULL DEFAULT 0,
                updated_at  TEXT DEFAULT (datetime('now'))
            );
            CREATE INDEX idx_acp_skill ON atom_candidate_pending(skill);
            INSERT INTO atom_candidate_pending(
                atom_id, skill, weightscore, updated_at
            ) VALUES
                ('atom_t1_0001', 'foo', 4, '2026-08-17 01:02:03'),
                ('atom_t1_0002', 'bar', 8, '2026-08-17 04:05:06');
            """,
        )
        legacy.commit()
    finally:
        legacy.close()

    conn = reg.get_connection(db)
    try:
        assert _pending_primary_key(conn) == ["atom_id", "skill"]
        rows = [
            tuple(row)
            for row in conn.execute(
                "SELECT atom_id, skill, weightscore, updated_at "
                "FROM atom_candidate_pending ORDER BY atom_id",
            )
        ]
        assert rows == [
            ("atom_t1_0001", "foo", 4, "2026-08-17 01:02:03"),
            ("atom_t1_0002", "bar", 8, "2026-08-17 04:05:06"),
        ]
        conn.execute("PRAGMA user_version=0")
        conn.commit()
    finally:
        conn.close()

    reopened = reg.get_connection(db)
    try:
        assert _pending_primary_key(reopened) == ["atom_id", "skill"]
        assert reopened.execute(
            "SELECT COUNT(*) FROM atom_candidate_pending",
        ).fetchone()[0] == 2
    finally:
        reopened.close()


def test_legacy_migration_rebuilds_multi_skill_projection_despite_stale_meta(
    tmp_path: Path,
) -> None:
    db = tmp_path / "registry.db"
    skill_dir = tmp_path / "skill"
    skill_dir.mkdir()
    skills = (("foo", 4), ("bar", 6), ("baz", 8))
    for name, score in skills:
        skill_path = _write_skill(skill_dir, name)
        (skill_path / CANDIDATES_FILENAME).write_text(
            "candidates:\n"
            "  - atom_id: atom_t1_0001\n"
            f"    weightscore: {score}\n",
            encoding="utf-8",
        )

    legacy = sqlite3.connect(db)
    try:
        legacy.executescript(
            """
            CREATE TABLE atom_candidate_pending (
                atom_id     TEXT PRIMARY KEY,
                skill       TEXT NOT NULL,
                weightscore INTEGER NOT NULL DEFAULT 0,
                updated_at  TEXT DEFAULT (datetime('now'))
            );
            CREATE INDEX idx_acp_skill ON atom_candidate_pending(skill);
            CREATE TABLE atom_candidate_pending_meta (
                root_key      TEXT PRIMARY KEY,
                backfilled_at TEXT NOT NULL
            );
            INSERT INTO atom_candidate_pending(atom_id, skill, weightscore)
            VALUES ('atom_t1_0001', 'foo', 4);
            """,
        )
        legacy.executemany(
            "INSERT INTO atom_candidate_pending_meta(root_key, backfilled_at) "
            "VALUES (?, '9999999999')",
            [
                (reg._atom_pending_root_key(skill_dir),),
                *((f"pending_mtime:{name}",) for name, _score in skills),
            ],
        )
        legacy.commit()
    finally:
        legacy.close()

    migrated = reg.get_connection(db)
    try:
        assert migrated.execute(
            "SELECT COUNT(*) FROM atom_candidate_pending_meta",
        ).fetchone()[0] == 0
    finally:
        migrated.close()

    reg.ensure_atom_pending_backfilled(skill_dir, db_path=db)
    with reg.pooled_connection(db) as conn:
        rows = [
            tuple(row)
            for row in conn.execute(
                "SELECT atom_id, skill, weightscore "
                "FROM atom_candidate_pending ORDER BY skill",
            )
        ]
    assert rows == [
        ("atom_t1_0001", "bar", 6),
        ("atom_t1_0001", "baz", 8),
        ("atom_t1_0001", "foo", 4),
    ]


def test_candidates_save_syncs_pending_projection(tmp_path: Path) -> None:
    db = tmp_path / "registry.db"
    skill_dir = tmp_path / "skill"
    skill_dir.mkdir()
    foo = _write_skill(skill_dir, "foo")
    bar = _write_skill(skill_dir, "bar")

    with mock.patch(
        "xskill.skill.catalog_store.resolve_catalog_db_path",
        return_value=db,
    ):
        add_atom_contributions(foo, [("atom_t1_0001", 7, "")])
        add_atom_contributions(bar, [("atom_t1_0001", 3, "")])
        add_atom_contributions(foo, [("atom_t1_0001", 9, "")])

    with reg.pooled_connection(db) as conn:
        rows = [
            tuple(row)
            for row in conn.execute(
                "SELECT atom_id, skill, weightscore "
                "FROM atom_candidate_pending ORDER BY skill",
            )
        ]
    assert rows == [
        ("atom_t1_0001", "bar", 3),
        ("atom_t1_0001", "foo", 9),
    ]

    with mock.patch(
        "xskill.skill.catalog_store.resolve_catalog_db_path",
        return_value=db,
    ):
        remove_candidates(foo, {"atom_t1_0001"})

    with reg.pooled_connection(db) as conn:
        left = [
            tuple(row)
            for row in conn.execute(
                "SELECT atom_id, skill, weightscore FROM atom_candidate_pending",
            )
        ]
    assert left == [("atom_t1_0001", "bar", 3)]


@pytest.mark.performance_contract
def test_backfill_and_dashboard_keep_multi_skill_pending_associations(
    tmp_path: Path,
) -> None:
    db = tmp_path / "registry.db"
    skill_dir = tmp_path / "skill"
    skill_dir.mkdir()
    foo = _write_skill(skill_dir, "foo")
    bar = _write_skill(skill_dir, "bar")
    for skill_path, score in ((foo, 5), (bar, 8)):
        (skill_path / CANDIDATES_FILENAME).write_text(
            "candidates:\n"
            "  - atom_id: atom_t9_0001\n"
            f"    weightscore: {score}\n",
            encoding="utf-8",
        )
    assert reg.backfill_atom_candidate_pending(skill_dir, db_path=db) == 2

    explorer = TrajExplorer(db_path=db, skill_dir=skill_dir)
    with mock.patch.object(Path, "iterdir", side_effect=AssertionError("扫盘")):
        dests = explorer._atom_destinations("atom_t9_0001")
    assert sorted(dests, key=lambda row: row["skill"]) == [
        {
            "skill": "bar",
            "weightscore": 8,
            "state": "pending",
            "ts": "",
        },
        {
            "skill": "foo",
            "weightscore": 5,
            "state": "pending",
            "ts": "",
        },
    ]
    assert [
        row["atom_id"]
        for row in skill_lineage(skill_dir, "foo", db_path=db)["atoms"]
    ] == ["atom_t9_0001"]
    assert [
        row["atom_id"]
        for row in skill_lineage(skill_dir, "bar", db_path=db)["atoms"]
    ] == ["atom_t9_0001"]
