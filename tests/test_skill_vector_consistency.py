"""skills_catalog ↔ 向量索引最终一致性（TC1–TC10）。

默认用 MemorySkillVectorIndex + fake_embed，不依赖本机 pymilvus / 外网。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from xskill.pipeline.registry import get_connection, pooled_connection
from xskill.recommend.recommend_store import (
    load_recommend_slots,
    mark_recommend_dirty,
    save_recommend_slots,
)
from xskill.recommend.skill_vector_store import (
    DEFAULT_DIM,
    MemorySkillVectorIndex,
    content_sha_for_text,
    fake_embed,
    reconcile_catalog_to_index,
)


def _upsert_catalog(db: Path, row: dict) -> None:
    with pooled_connection(db) as conn:
        conn.execute(
            """
            INSERT INTO skills_catalog(
                catalog_key, root_key, name, repo_name, source, state,
                description, version, candidates_count, main_sha, staging_sha,
                distributable, search_id, hub, skill_id, use_count, content_sha,
                updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'))
            ON CONFLICT(catalog_key) DO UPDATE SET
                description=excluded.description,
                content_sha=excluded.content_sha,
                source=excluded.source,
                name=excluded.name,
                skill_id=excluded.skill_id,
                updated_at=datetime('now')
            """,
            (
                row["catalog_key"],
                row.get("root_key") or "",
                row["name"],
                row.get("repo_name") or "",
                row.get("source") or "native",
                row.get("state") or "main",
                row["description"],
                row.get("version") or "",
                int(row.get("candidates_count") or 0),
                row.get("main_sha") or "",
                row.get("staging_sha") or "",
                int(row.get("distributable") or 1),
                row.get("search_id") or "",
                row.get("hub") or "",
                row.get("skill_id") or "",
                int(row.get("use_count") or 0),
                row["content_sha"],
            ),
        )
        conn.commit()


def _delete_catalog(db: Path, catalog_key: str) -> None:
    with pooled_connection(db) as conn:
        conn.execute(
            "DELETE FROM skills_catalog WHERE catalog_key=?", (catalog_key,),
        )
        conn.commit()


def _load_catalog(db: Path) -> list[dict]:
    with pooled_connection(db) as conn:
        rows = conn.execute(
            """
            SELECT catalog_key, name, source, description, content_sha, skill_id
            FROM skills_catalog
            """
        ).fetchall()
    return [dict(r) for r in rows]


def _row(key: str, desc: str, *, source: str = "native", name: str | None = None) -> dict:
    return {
        "catalog_key": key,
        "name": name or key.split(":", 1)[-1],
        "source": source,
        "description": desc,
        "content_sha": content_sha_for_text(desc),
        "skill_id": key.split(":", 1)[-1] if source == "skillhub" else "",
    }


@pytest.fixture()
def registry_db(tmp_path: Path) -> Path:
    db = tmp_path / "registry.db"
    # 触发 schema（含 skills_catalog / client_recommend_slots）
    conn = get_connection(db)
    conn.close()
    return db


@pytest.fixture()
def index() -> MemorySkillVectorIndex:
    return MemorySkillVectorIndex(dim=DEFAULT_DIM)


def _reconcile(db: Path, index: MemorySkillVectorIndex) -> dict:
    return reconcile_catalog_to_index(
        index, _load_catalog(db), embed=lambda t: fake_embed(t, DEFAULT_DIM),
    )


def test_tc1_insert_aligns(registry_db, index):
    row = _row("native:alpha", "alpha skill for networking")
    _upsert_catalog(registry_db, row)
    stats = _reconcile(registry_db, index)
    assert stats["upserted"] == 1
    got = index.get("native:alpha")
    assert got is not None
    assert got["content_sha"] == row["content_sha"]


def test_tc2_update_reembeds(registry_db, index):
    row = _row("native:alpha", "old description text")
    _upsert_catalog(registry_db, row)
    _reconcile(registry_db, index)
    old_vec = list(index.get("native:alpha")["vector"])

    updated = _row("native:alpha", "brand new description about databases")
    _upsert_catalog(registry_db, updated)
    stats = _reconcile(registry_db, index)
    assert stats["upserted"] == 1
    got = index.get("native:alpha")
    assert got["content_sha"] == updated["content_sha"]
    assert list(got["vector"]) != old_vec
    assert len(index.list_keys()) == 1


def test_tc3_delete_aligns(registry_db, index):
    row = _row("native:alpha", "to be deleted")
    _upsert_catalog(registry_db, row)
    _reconcile(registry_db, index)
    _delete_catalog(registry_db, "native:alpha")
    stats = _reconcile(registry_db, index)
    assert stats["deleted"] == 1
    assert index.get("native:alpha") is None


def test_tc4_reconcile_fills_hole(registry_db, index):
    row = _row("native:hole", "exists only in catalog")
    _upsert_catalog(registry_db, row)
    # 不对账前 Milvus/index 为空
    assert index.get("native:hole") is None
    stats = _reconcile(registry_db, index)
    assert stats["upserted"] == 1
    assert index.get("native:hole")["content_sha"] == row["content_sha"]


def test_tc5_reconcile_clears_ghost(registry_db, index):
    index.upsert(
        "native:ghost",
        fake_embed("ghost", DEFAULT_DIM),
        content_sha="deadbeef",
        source="native",
        name="ghost",
    )
    stats = _reconcile(registry_db, index)
    assert stats["deleted"] == 1
    assert index.get("native:ghost") is None


def test_tc6_reconcile_repairs_stale_sha(registry_db, index):
    row = _row("native:stale", "fresh description")
    _upsert_catalog(registry_db, row)
    index.upsert(
        "native:stale",
        fake_embed("old description", DEFAULT_DIM),
        content_sha="stale_sha_value",
        source="native",
        name="stale",
    )
    stats = _reconcile(registry_db, index)
    assert stats["upserted"] == 1
    assert index.get("native:stale")["content_sha"] == row["content_sha"]


def test_tc7_mixed_native_skillhub(registry_db, index):
    _upsert_catalog(registry_db, _row("native:n1", "native one"))
    _upsert_catalog(
        registry_db,
        _row("skillhub:hub-1", "hub skill desc", source="skillhub", name="Hub Skill"),
    )
    _reconcile(registry_db, index)
    assert index.get("native:n1")["source"] == "native"
    assert index.get("skillhub:hub-1")["source"] == "skillhub"
    assert len(index.list_keys()) == 2


def test_tc8_empty_description_not_indexed(registry_db, index):
    _upsert_catalog(
        registry_db,
        {
            "catalog_key": "native:empty",
            "name": "empty",
            "source": "native",
            "description": "",
            "content_sha": "",
            "skill_id": "",
        },
    )
    stats = _reconcile(registry_db, index)
    assert stats["upserted"] == 0
    assert index.get("native:empty") is None


def test_tc9_double_upsert_single_key(registry_db, index):
    row = _row("native:dup", "first")
    _upsert_catalog(registry_db, row)
    _reconcile(registry_db, index)
    row2 = _row("native:dup", "second version")
    _upsert_catalog(registry_db, row2)
    _reconcile(registry_db, index)
    _reconcile(registry_db, index)
    assert len(index.list_keys()) == 1
    assert index.get("native:dup")["content_sha"] == row2["content_sha"]


def test_tc10_readonly_catalog_page_does_not_mutate_index(
    registry_db, index, tmp_path, monkeypatch,
):
    """看板只读投影表时不应写向量索引。"""
    row = _row("native:ro", "readonly probe")
    _upsert_catalog(registry_db, row)
    _reconcile(registry_db, index)
    before = {k: dict(v) for k, v in index._rows.items()}

    writes = {"n": 0}
    orig_upsert = index.upsert
    orig_delete = index.delete

    def counting_upsert(*a, **k):
        writes["n"] += 1
        return orig_upsert(*a, **k)

    def counting_delete(*a, **k):
        writes["n"] += 1
        return orig_delete(*a, **k)

    monkeypatch.setattr(index, "upsert", counting_upsert)
    monkeypatch.setattr(index, "delete", counting_delete)

    from xskill.skill import catalog_store

    # 跳过扫盘回填：本测只验证只读分页不碰向量索引
    monkeypatch.setattr(catalog_store, "ensure_skills_catalog", lambda *a, **k: None)
    skill_dir = tmp_path / "skills"
    skill_dir.mkdir()
    page = catalog_store.page_skills_catalog(
        skill_dir, limit=10, offset=0, db_path=registry_db,
    )
    assert "items" in page or "skills" in page or isinstance(page, dict)
    assert writes["n"] == 0
    assert index._rows == before


def test_recommend_slots_stale_still_readable(registry_db):
    save_recommend_slots("alice", ["a", "b"], db_path=registry_db)
    mark_recommend_dirty("alice", reason="test", db_path=registry_db)
    assert load_recommend_slots("alice", db_path=registry_db) == ["a", "b"]


def test_heavy_compute_recommend_from_centers(registry_db, index):
    from xskill.recommend.heavy_worker import compute_recommend_for_user

    _upsert_catalog(registry_db, _row("native:net", "network debugging tools"))
    _upsert_catalog(registry_db, _row("native:db", "database migration helpers"))
    _reconcile(registry_db, index)
    center = fake_embed("network debugging tools", DEFAULT_DIM)
    names = compute_recommend_for_user(
        "bob",
        db_path=registry_db,
        vector_index=index,
        profile_centers=[center],
        top_k=1,
    )
    assert names
    assert load_recommend_slots("bob", db_path=registry_db) == names


class _ScriptedSearchIndex:
    def __init__(self, results):
        self.results = results
        self.search_calls = []

    def search(self, vector, *, top_k=10):
        key = tuple(vector)
        self.search_calls.append((key, top_k))
        return self.results[key][:top_k]

    def get(self, catalog_key):
        return {"name": catalog_key.split(":", 1)[-1], "source": "native"}


def test_heavy_compute_recommend_interleaves_all_profile_centers(registry_db):
    from xskill.recommend.heavy_worker import compute_recommend_for_user

    first = (1.0, 0.0)
    second = (0.0, 1.0)
    index = _ScriptedSearchIndex({
        first: [("native:first-a", 1.0), ("native:first-b", 0.9)],
        second: [("native:second-a", 1.0), ("native:second-b", 0.9)],
    })

    names = compute_recommend_for_user(
        "multi",
        db_path=registry_db,
        vector_index=index,
        profile_centers=[list(first), list(second)],
        top_k=4,
    )

    assert names == ["first-a", "second-a", "first-b", "second-b"]
    assert index.search_calls == [(first, 4), (second, 4)]
    with pooled_connection(registry_db) as conn:
        fingerprint = conn.execute(
            "SELECT fingerprint FROM client_recommend_slots WHERE user_key=?",
            ("multi",),
        ).fetchone()["fingerprint"]
    assert fingerprint == "centers=2;fusion=round_robin_v1;sources=0,1,0,1"


def test_heavy_compute_recommend_deduplicates_across_centers(registry_db):
    from xskill.recommend.heavy_worker import compute_recommend_for_user

    first = (1.0, 0.0)
    second = (0.0, 1.0)
    index = _ScriptedSearchIndex({
        first: [("native:shared", 1.0), ("native:first", 0.9)],
        second: [("native:shared", 1.0), ("native:second", 0.9)],
    })

    names = compute_recommend_for_user(
        "deduplicated",
        db_path=registry_db,
        vector_index=index,
        profile_centers=[list(first), list(second)],
        top_k=3,
    )

    assert names == ["shared", "second", "first"]
    assert len(names) == len(set(names))


def test_heavy_compute_recommend_center_order_keeps_each_center_represented(
    registry_db,
):
    from xskill.recommend.heavy_worker import compute_recommend_for_user

    first = (1.0, 0.0)
    second = (0.0, 1.0)
    index = _ScriptedSearchIndex({
        first: [("native:first-a", 1.0), ("native:first-b", 0.9)],
        second: [("native:second-a", 1.0), ("native:second-b", 0.9)],
    })

    names = compute_recommend_for_user(
        "reordered",
        db_path=registry_db,
        vector_index=index,
        profile_centers=[list(second), list(first)],
        top_k=4,
    )

    assert names == ["second-a", "first-a", "second-b", "first-b"]
