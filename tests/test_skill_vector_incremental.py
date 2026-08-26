"""catalog_vector_dirty 增量同步、generation fence 与低频修复。"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from xskill.pipeline import registry as registry_mod
from xskill.pipeline.registry import get_connection, pooled_connection
from xskill.recommend.heavy_worker import (
    VECTOR_RECONCILE_INTERVAL_SECONDS,
    run_vector_sync,
)
from xskill.recommend.skill_vector_store import (
    DEFAULT_DIM,
    MemorySkillVectorIndex,
    MilvusLiteSkillVectorIndex,
    content_sha_for_text,
    fake_embed,
)
from xskill.recommend.vector_dirty import (
    clear_catalog_vector_dirty,
    list_catalog_vector_dirty,
    mark_catalog_vector_dirty_on_connection,
)
from xskill.skill.catalog_store import (
    delete_native_skill,
    rename_native_skill,
    upsert_native_skill,
)


class CountingIndex(MemorySkillVectorIndex):
    def __init__(self) -> None:
        super().__init__(dim=DEFAULT_DIM)
        self.calls = {"upsert": 0, "delete": 0, "get": 0, "list_keys": 0}

    def reset_calls(self) -> None:
        for key in self.calls:
            self.calls[key] = 0

    def upsert(self, *args, **kwargs) -> None:
        self.calls["upsert"] += 1
        super().upsert(*args, **kwargs)

    def delete(self, *args, **kwargs) -> None:
        self.calls["delete"] += 1
        super().delete(*args, **kwargs)

    def get(self, *args, **kwargs):
        self.calls["get"] += 1
        return super().get(*args, **kwargs)

    def list_keys(self) -> set[str]:
        self.calls["list_keys"] += 1
        return super().list_keys()


@pytest.fixture()
def registry_db(tmp_path: Path) -> Path:
    db = tmp_path / "registry.db"
    get_connection(db).close()
    return db


def _catalog_row(key: str, description: str, *, name: str | None = None) -> dict:
    return {
        "catalog_key": key,
        "name": name or key.split(":", 1)[-1],
        "source": "native",
        "description": description,
        "content_sha": content_sha_for_text(description),
        "distributable": 1,
    }


def _store_catalog(db: Path, row: dict, *, mark: bool = True) -> None:
    with pooled_connection(db) as conn:
        conn.execute(
            """
            INSERT INTO skills_catalog(
                catalog_key, name, source, state, description, distributable,
                content_sha
            ) VALUES (?, ?, ?, 'main', ?, ?, ?)
            ON CONFLICT(catalog_key) DO UPDATE SET
                name=excluded.name,
                source=excluded.source,
                description=excluded.description,
                distributable=excluded.distributable,
                content_sha=excluded.content_sha
            """,
            (
                row["catalog_key"], row["name"], row["source"],
                row["description"], row["distributable"], row["content_sha"],
            ),
        )
        if mark:
            mark_catalog_vector_dirty_on_connection(
                conn,
                row["catalog_key"],
                operation="upsert",
                content_sha=row["content_sha"],
            )
        conn.commit()


def _sync(db: Path, index, *, model: str = "model-a", now: float = 100.0, embed=None):
    return run_vector_sync(
        db_path=db,
        vector_db_path=db.parent / "vectors.db",
        index=index,
        embed=embed or (lambda text: fake_embed(text, DEFAULT_DIM)),
        model_fingerprint=f"test:{model}:{DEFAULT_DIM}",
        now=now,
    )


def _write_native_skill(root: Path, name: str, description: str) -> Path:
    skill = root / name
    skill.mkdir(parents=True, exist_ok=True)
    (skill / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n",
        encoding="utf-8",
    )
    ref = skill / ".git" / "refs" / "heads" / "main"
    ref.parent.mkdir(parents=True)
    ref.write_text("a" * 40, encoding="utf-8")
    return skill


@pytest.mark.performance_contract
def test_idle_tick_does_not_scan_vector_index(registry_db):
    index = CountingIndex()
    _store_catalog(registry_db, _catalog_row("native:alpha", "alpha"))
    first = _sync(registry_db, index)
    assert first["mode"] == "full"
    assert index.get("native:alpha") is not None

    index.reset_calls()
    embeds = []
    second = _sync(
        registry_db,
        index,
        now=101,
        embed=lambda text: embeds.append(text) or fake_embed(text, DEFAULT_DIM),
    )
    assert second == {
        "upserted": 0, "deleted": 0, "skipped": 0, "deferred": 0,
        "mode": "incremental", "reason": "",
    }
    assert index.calls == {"upsert": 0, "delete": 0, "get": 0, "list_keys": 0}
    assert embeds == []


def test_upgrade_bootstrap_reuses_matching_legacy_vectors(registry_db):
    index = CountingIndex()
    row = _catalog_row("native:alpha", "alpha")
    _store_catalog(registry_db, row, mark=False)
    index.upsert(
        row["catalog_key"],
        fake_embed(row["description"]),
        content_sha=row["content_sha"],
        source=row["source"],
        name=row["name"],
    )
    index.reset_calls()
    embeds = []
    stats = _sync(
        registry_db,
        index,
        embed=lambda text: embeds.append(text) or fake_embed(text, DEFAULT_DIM),
    )
    assert stats["reason"] == "bootstrap"
    assert stats["skipped"] == 1
    assert index.calls["upsert"] == 0
    assert embeds == []


@pytest.mark.performance_contract
def test_one_dirty_skill_only_updates_that_key(registry_db):
    index = CountingIndex()
    _store_catalog(registry_db, _catalog_row("native:alpha", "alpha"))
    _store_catalog(registry_db, _catalog_row("native:beta", "beta"))
    _sync(registry_db, index)
    index.reset_calls()

    _store_catalog(registry_db, _catalog_row("native:beta", "beta v2"))
    stats = _sync(registry_db, index, now=101)
    assert stats["mode"] == "incremental"
    assert stats["upserted"] == 1
    assert index.calls == {"upsert": 1, "delete": 0, "get": 0, "list_keys": 0}
    assert index._rows["native:alpha"]["content_sha"] == content_sha_for_text("alpha")
    assert index._rows["native:beta"]["content_sha"] == content_sha_for_text("beta v2")


def test_generation_cas_keeps_late_update_and_prevents_stale_write(registry_db):
    index = CountingIndex()
    _store_catalog(registry_db, _catalog_row("native:alpha", "v1"))
    _sync(registry_db, index)
    _store_catalog(registry_db, _catalog_row("native:alpha", "v2"))

    def racing_embed(text: str):
        assert text == "v2"
        _store_catalog(registry_db, _catalog_row("native:alpha", "v3"))
        return fake_embed(text, DEFAULT_DIM)

    stats = _sync(registry_db, index, now=101, embed=racing_embed)
    assert stats["upserted"] == 0
    assert stats["deferred"] == 1
    assert index._rows["native:alpha"]["content_sha"] == content_sha_for_text("v1")
    assert list_catalog_vector_dirty(db_path=registry_db)[0]["generation"] == 3

    stats = _sync(registry_db, index, now=102)
    assert stats["upserted"] == 1
    assert index._rows["native:alpha"]["content_sha"] == content_sha_for_text("v3")
    assert list_catalog_vector_dirty(db_path=registry_db) == []


def test_generation_watermark_prevents_aba_clear(registry_db):
    row = _catalog_row("native:alpha", "v1")
    _store_catalog(registry_db, row)
    first = list_catalog_vector_dirty(db_path=registry_db)[0]
    assert clear_catalog_vector_dirty(
        first["catalog_key"], first["generation"], db_path=registry_db,
    )

    _store_catalog(registry_db, _catalog_row("native:alpha", "v2"))
    second = list_catalog_vector_dirty(db_path=registry_db)[0]
    assert second["generation"] > first["generation"]
    assert not clear_catalog_vector_dirty(
        first["catalog_key"], first["generation"], db_path=registry_db,
    )
    assert list_catalog_vector_dirty(db_path=registry_db)[0]["generation"] == second[
        "generation"
    ]


def test_model_switch_forces_reembedding_and_periodic_repairs_ghost(registry_db):
    index = CountingIndex()
    _store_catalog(registry_db, _catalog_row("native:alpha", "alpha"))
    _sync(registry_db, index, model="model-a")
    index.reset_calls()

    changed = _sync(registry_db, index, model="model-b", now=101)
    assert changed["mode"] == "full"
    assert changed["reason"] == "model_changed"
    assert changed["upserted"] == 1
    assert index.calls["list_keys"] == 1
    assert index.calls["upsert"] == 1

    index.upsert(
        "native:ghost", fake_embed("ghost"), content_sha="ghost",
        source="native", name="ghost",
    )
    repaired = _sync(
        registry_db,
        index,
        model="model-b",
        now=101 + VECTOR_RECONCILE_INTERVAL_SECONDS,
    )
    assert repaired["reason"] == "periodic"
    assert repaired["deleted"] == 1
    assert index.get("native:ghost") is None


def test_full_reconcile_updates_metadata_when_content_sha_is_unchanged(registry_db):
    index = CountingIndex()
    row = _catalog_row("native:alpha", "same text", name="old-name")
    _store_catalog(registry_db, row)
    _sync(registry_db, index)

    renamed = {**row, "name": "new-name", "source": "skillhub"}
    _store_catalog(registry_db, renamed)
    embeds = []
    stats = _sync(
        registry_db,
        index,
        now=100 + VECTOR_RECONCILE_INTERVAL_SECONDS,
        embed=lambda text: embeds.append(text) or fake_embed(text, DEFAULT_DIM),
    )
    assert stats["mode"] == "full"
    assert stats["upserted"] == 1
    assert index._rows["native:alpha"]["name"] == "new-name"
    assert index._rows["native:alpha"]["source"] == "skillhub"
    assert embeds == []


def test_catalog_writes_coalesce_and_emit_rename_tombstone(registry_db, tmp_path):
    root = tmp_path / "skills"
    old = _write_native_skill(root, "old-name", "first description")
    upsert_native_skill(old, db_path=registry_db)
    event = list_catalog_vector_dirty(db_path=registry_db)[0]
    assert event["catalog_key"] == "native:old-name"
    assert event["operation"] == "upsert"

    upsert_native_skill(old, db_path=registry_db)
    unchanged = list_catalog_vector_dirty(db_path=registry_db)[0]
    assert unchanged["generation"] == event["generation"]

    assert clear_catalog_vector_dirty(
        unchanged["catalog_key"], unchanged["generation"], db_path=registry_db,
    )
    new = _write_native_skill(root, "new-name", "renamed description")
    rename_native_skill("old-name", new, db_path=registry_db)
    events = {row["catalog_key"]: row for row in list_catalog_vector_dirty(
        db_path=registry_db,
    )}
    assert events["native:old-name"]["operation"] == "delete"
    assert events["native:new-name"]["operation"] == "upsert"

    delete_native_skill("new-name", db_path=registry_db)
    events = {row["catalog_key"]: row for row in list_catalog_vector_dirty(
        db_path=registry_db,
    )}
    assert events["native:new-name"]["operation"] == "delete"


def test_retire_and_unretire_emit_delete_and_upsert(registry_db):
    row = _catalog_row("native:alpha", "alpha")
    _store_catalog(registry_db, row, mark=False)
    registry_mod.retire_skill(skill_name="alpha", set_by="test", db_path=registry_db)
    event = list_catalog_vector_dirty(db_path=registry_db)[0]
    assert event["operation"] == "delete"
    assert registry_mod.unretire_skill(skill_name="alpha", db_path=registry_db)
    event = list_catalog_vector_dirty(db_path=registry_db)[0]
    assert event["operation"] == "upsert"
    assert event["generation"] == 2


def test_heavy_tick_reuses_known_embedding_dimension(registry_db, tmp_path):
    from xskill.recommend.heavy_worker import run_recommend_heavy_once

    class EmbedClient:
        dim = DEFAULT_DIM
        model = "known-dim"

        def __init__(self):
            self.calls = []

        def encode(self, text):
            self.calls.append(text)
            return fake_embed(text, DEFAULT_DIM)

    class Engine:
        embed_client = EmbedClient()

    stats = run_recommend_heavy_once(
        engine=Engine(),
        db_path=registry_db,
        vector_db_path=tmp_path / "vectors.db",
        memory_index=MemorySkillVectorIndex(),
    )
    assert stats["vector"]["mode"] == "full"
    assert Engine.embed_client.calls == []


@pytest.mark.parametrize(
    ("description", "expected"),
    [
        ({"fields": [{"name": "vector", "params": {"dim": 1536}}]}, 1536),
        ({"fields": [{"name": "vector", "dim": "1024"}]}, 1024),
        ({"fields": [{"name": "catalog_key", "params": {}}]}, None),
    ],
)
def test_milvus_description_dimension_parsing(description, expected):
    assert MilvusLiteSkillVectorIndex._described_vector_dim(description) == expected


def test_milvus_dimension_change_recreates_projection(monkeypatch):
    class Schema:
        def __init__(self):
            self.fields = []

        def add_field(self, *args, **kwargs):
            self.fields.append((args, kwargs))

    class Client:
        def __init__(self):
            self.dropped = []
            self.schema = Schema()
            self.created = []

        def has_collection(self, _name):
            return True

        def describe_collection(self, _name):
            return {"fields": [{"name": "vector", "params": {"dim": 4}}]}

        def drop_collection(self, name):
            self.dropped.append(name)

        def create_schema(self, **_kwargs):
            return self.schema

        def prepare_index_params(self):
            return SimpleNamespace(add_index=lambda **_kwargs: None)

        def create_collection(self, **kwargs):
            self.created.append(kwargs)

    monkeypatch.setitem(
        sys.modules,
        "pymilvus",
        SimpleNamespace(
            DataType=SimpleNamespace(INT64=1, VARCHAR=2, FLOAT_VECTOR=3),
        ),
    )
    index = object.__new__(MilvusLiteSkillVectorIndex)
    index.dim = DEFAULT_DIM
    index._client = Client()
    index._ensure_collection()
    assert index._client.dropped == ["skill_vectors"]
    assert index._client.created
    vector_fields = [
        kwargs for _args, kwargs in index._client.schema.fields
        if _args and _args[0] == "vector"
    ]
    assert vector_fields[0]["dim"] == DEFAULT_DIM
