"""skills_catalog 投影表：UPSERT / DELETE / backfill / page。"""
from __future__ import annotations

import sqlite3

import pytest

from xskill.skill import catalog_store
from xskill.skill.git import (
    commit_baby_to_main_branch,
    commit_to_staging_branch,
    init_skill_repo_on_baby,
)


@pytest.fixture(autouse=True)
def _isolate_registry(tmp_path, monkeypatch):
    registry = tmp_path / "registry.db"
    monkeypatch.setattr(
        "xskill.config.get_registry_db_path",
        lambda: registry,
    )
    monkeypatch.setattr(
        "xskill.pipeline.registry.get_registry_db_path",
        lambda: registry,
    )


def _init_repo_for_catalog(path):
    init_skill_repo_on_baby(
        str(path),
        name=path.name,
        description=f"{path.name} description",
    )
    assert commit_baby_to_main_branch(str(path), "graduate")
    return path


def test_init_and_graduate_upsert_native_row(tmp_path):
    from xskill.agents import agent_tools
    root = tmp_path / "skills"
    root.mkdir()
    registry = tmp_path / "registry.db"
    context = agent_tools.create_agent_tool_context(
        skill_dir=root,
        atom_skill_dir=root,
        registry_db_path=registry,
    )
    with agent_tools.use_agent_tool_context(context):
        init_skill_repo_on_baby(
            str(root / "demo"), name="demo", description="baby desc",
        )
        page = catalog_store.page_skills_catalog(root, limit=10, db_path=registry)
        assert page["total"] == 1
        assert page["skills"][0]["state"] == "baby"
        assert "baby desc" in page["skills"][0]["description"]
        before = catalog_store.native_catalog_generation(root, db_path=registry)
        catalog_store.upsert_native_skill(root / "demo", db_path=registry)
        assert catalog_store.native_catalog_generation(root, db_path=registry) == before

        assert commit_baby_to_main_branch(str(root / "demo"), "graduate")
        page = catalog_store.page_skills_catalog(root, limit=10, db_path=registry)
        assert page["skills"][0]["state"] == "main"
        assert catalog_store.native_catalog_generation(root, db_path=registry) > before


def test_delete_native_removes_row(tmp_path):
    root = tmp_path / "skills"
    root.mkdir()
    registry = tmp_path / "registry.db"
    init_skill_repo_on_baby(
        str(root / "gone"), name="gone", description="x",
    )
    catalog_store.ensure_skills_catalog(root, db_path=registry)
    before = catalog_store.native_catalog_generation(root, db_path=registry)
    catalog_store.delete_native_skill("gone", db_path=registry)
    assert catalog_store.list_skills_catalog(root, db_path=registry) == []
    assert catalog_store.native_catalog_generation(root, db_path=registry) > before


def test_backfill_replaces_stale_native_and_hub(tmp_path):
    root = tmp_path / "skills"
    root.mkdir()
    registry = tmp_path / "registry.db"
    init_skill_repo_on_baby(
        str(root / "keep"), name="keep", description="keep me",
    )
    catalog_store.ensure_skills_catalog(root, db_path=registry)
    hub = [{
        "display_name": "hub-skill",
        "source_path": "team/x",
        "skill_id": "hub-skill@1",
        "description": "from hub",
        "use_count": 2,
    }]
    count = catalog_store.backfill_skills_catalog(
        root, skillhub=hub, db_path=registry,
    )
    assert count == 2
    rows = catalog_store.list_skills_catalog(
        root, skillhub=hub, db_path=registry,
    )
    assert [row["name"] for row in rows] == ["keep", "hub-skill"]
    assert rows[1]["use_count"] == 2


def test_canary_reconcile_preserves_skillhub_and_lists_only_staging(tmp_path):
    root = tmp_path / "skills"
    root.mkdir()
    registry = tmp_path / "registry.db"
    main = _init_repo_for_catalog(root / "main-only")
    staged = _init_repo_for_catalog(root / "active")
    (staged / ".git" / "refs" / "heads" / "staging").write_text(
        "b" * 40 + "\n",
        encoding="ascii",
    )
    hub = [{
        "display_name": "hub-skill",
        "source_path": "team/x",
        "skill_id": "hub-skill@1",
        "description": "from hub",
    }]
    catalog_store.backfill_skills_catalog(root, skillhub=hub, db_path=registry)

    assert catalog_store.reconcile_native_canary_catalog(
        root,
        db_path=registry,
    ) == 1
    assert catalog_store.list_active_native_canaries(
        root,
        db_path=registry,
    ) == ["active"]
    rows = catalog_store.list_skills_catalog(
        root,
        skillhub=hub,
        db_path=registry,
    )
    assert {row["name"] for row in rows} == {
        main.name,
        staged.name,
        "hub-skill",
    }


def test_staging_write_hook_immediately_enters_active_projection(tmp_path):
    from xskill.agents import agent_tools

    root = tmp_path / "skills"
    root.mkdir()
    registry = tmp_path / "registry.db"
    skill = _init_repo_for_catalog(root / "new-canary")
    context = agent_tools.create_agent_tool_context(
        skill_dir=root,
        atom_skill_dir=root,
        registry_db_path=registry,
    )
    catalog_store.reconcile_native_canary_catalog(root, db_path=registry)
    (skill / "SKILL.md").write_text("staging body", encoding="utf-8")

    with agent_tools.use_agent_tool_context(context):
        assert commit_to_staging_branch(str(skill), "create staging")

    assert catalog_store.list_active_native_canaries(
        root,
        db_path=registry,
    ) == ["new-canary"]


def test_candidates_notify_uses_count_not_reread(tmp_path, monkeypatch):
    registry = tmp_path / "registry.db"
    root = tmp_path / "skills"
    skill = root / "demo"
    skill.mkdir(parents=True)
    (skill / ".git" / "refs" / "heads").mkdir(parents=True)
    (skill / ".git" / "refs" / "heads" / "baby").write_text("sha\n", encoding="utf-8")
    (skill / "SKILL.md").write_text(
        "---\nname: demo\ndescription: d\nmetadata:\n  version: 0\n---\n",
        encoding="utf-8",
    )
    catalog_store.ensure_skills_catalog(root, db_path=registry)
    before = catalog_store.native_catalog_generation(root, db_path=registry)
    catalog_store.notify_native_candidates_count(skill, 3, db_path=registry)
    page = catalog_store.page_skills_catalog(root, db_path=registry)
    assert page["skills"][0]["candidates"] == 3
    after = catalog_store.native_catalog_generation(root, db_path=registry)
    assert after > before

    catalog_store.notify_native_candidates_count(skill, 3, db_path=registry)
    assert catalog_store.native_catalog_generation(root, db_path=registry) == after


def test_notify_upsert_without_db_path_skips_global(tmp_path, monkeypatch):
    """无 registry_db_path 时 hook 不得创建全局库。"""
    global_db = tmp_path / "global" / "registry.db"
    monkeypatch.setattr(
        "xskill.config.get_registry_db_path",
        lambda: global_db,
    )
    monkeypatch.setattr(
        "xskill.pipeline.registry.get_registry_db_path",
        lambda: global_db,
    )
    root = tmp_path / "skills"
    root.mkdir()
    skill = root / "solo"
    skill.mkdir()
    (skill / "SKILL.md").write_text(
        "---\nname: solo\ndescription: d\n---\n", encoding="utf-8",
    )
    catalog_store.notify_native_upsert(skill)
    assert not global_db.exists()


def test_rename_is_single_transaction(tmp_path):
    registry = tmp_path / "registry.db"
    root = tmp_path / "skills"
    old_path = root / "old"
    new_path = root / "new"
    old_path.mkdir(parents=True)
    (old_path / ".git" / "refs" / "heads").mkdir(parents=True)
    (old_path / ".git" / "refs" / "heads" / "baby").write_text("a\n", encoding="utf-8")
    (old_path / "SKILL.md").write_text(
        "---\nname: old\ndescription: d\n---\n", encoding="utf-8",
    )
    catalog_store.upsert_native_skill(old_path, db_path=registry)
    catalog_store.ensure_skills_catalog(root, db_path=registry)
    before = catalog_store.native_catalog_generation(root, db_path=registry)
    old_path.rename(new_path)
    (new_path / "SKILL.md").write_text(
        "---\nname: new\ndescription: d\n---\n", encoding="utf-8",
    )
    catalog_store.rename_native_skill("old", new_path, db_path=registry)
    rows = catalog_store.list_skills_catalog(root, db_path=registry)
    assert [row["name"] for row in rows] == ["new"]
    assert catalog_store.native_catalog_generation(root, db_path=registry) > before


def test_native_reconcile_preserves_skillhub_rows(tmp_path):
    registry = tmp_path / "registry.db"
    root = tmp_path / "skills"
    root.mkdir()
    init_skill_repo_on_baby(
        str(root / "alpha"), name="alpha", description="native alpha",
    )
    hub = [{
        "display_name": "hub-skill",
        "source_path": "team/x",
        "skill_id": "hub-skill@1",
        "description": "from hub",
        "use_count": 2,
    }]
    catalog_store.backfill_skills_catalog(root, skillhub=hub, db_path=registry)

    init_skill_repo_on_baby(
        str(root / "beta"), name="beta", description="native beta",
    )
    stats = catalog_store.reconcile_native_skills_catalog(root, db_path=registry)

    assert stats == {
        "upserted": 1, "deleted": 0, "changed": 1, "skipped": 0,
    }
    rows = catalog_store.list_skills_catalog(root, skillhub=hub, db_path=registry)
    assert [row["name"] for row in rows] == ["alpha", "beta", "hub-skill"]


def test_native_reconcile_does_not_overwrite_concurrent_upsert(tmp_path, monkeypatch):
    registry = tmp_path / "registry.db"
    root = tmp_path / "skills"
    root.mkdir()
    skill = root / "alpha"
    init_skill_repo_on_baby(
        str(skill), name="alpha", description="old description",
    )
    catalog_store.ensure_skills_catalog(root, db_path=registry)
    original_scan = catalog_store.scan_skills_catalog

    def scan_then_write(skill_dir, skillhub=None):
        stale = original_scan(skill_dir, skillhub=skillhub)
        (skill / "SKILL.md").write_text(
            "---\nname: alpha\ndescription: concurrent description\n---\n",
            encoding="utf-8",
        )
        catalog_store.upsert_native_skill(skill, db_path=registry)
        return stale

    monkeypatch.setattr(catalog_store, "scan_skills_catalog", scan_then_write)
    stats = catalog_store.reconcile_native_skills_catalog(root, db_path=registry)

    assert stats == {"upserted": 0, "deleted": 0, "changed": 0, "skipped": 1}
    rows = catalog_store.list_native_cluster_catalog(root, db_path=registry)
    assert rows[0]["description"] == "concurrent description"


def test_legacy_catalog_meta_adds_generation_column(tmp_path):
    registry = tmp_path / "registry.db"
    with sqlite3.connect(registry) as conn:
        conn.execute(
            """
            CREATE TABLE skills_catalog_meta (
                root_key TEXT PRIMARY KEY,
                backfilled_at TEXT NOT NULL,
                skillhub_key TEXT NOT NULL DEFAULT ''
            )
            """
        )
        conn.execute(
            "INSERT INTO skills_catalog_meta VALUES ('legacy', 'now', 'none')"
        )

    from xskill.pipeline.registry import get_connection

    conn = get_connection(registry)
    columns = {
        row[1] for row in conn.execute(
            "PRAGMA table_info(skills_catalog_meta)"
        ).fetchall()
    }
    generation = conn.execute(
        "SELECT generation FROM skills_catalog_meta WHERE root_key='legacy'"
    ).fetchone()[0]
    conn.close()

    assert "generation" in columns
    assert generation == 0
