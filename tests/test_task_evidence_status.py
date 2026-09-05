"""Task learning status comes from the requested tenant's registry projection."""

import pytest

from xskill.pipeline.registry import get_connection
from xskill.tasks.projection import task_evidence_feed_counts, task_graph_overview


def _seed(db_path):
    with get_connection(db_path) as connection:
        connection.executemany(
            "INSERT INTO task_evidence_feed("
            "tenant_id,task_scope_id,task_id,task_generation_id,"
            "task_evidence_fingerprint,learning_eligibility,status)"
            " VALUES(?,?,?,?,?,?,?)",
            [
                (tenant, "scope", str(i), "generation", "digest", "eligible", status)
                for tenant, statuses in (
                    ("tenant-a", ("pending", "pending", "processed", "fallback")),
                    ("tenant-b", ("pending", "rejected")),
                )
                for i, status in enumerate(statuses)
            ],
        )


def test_feed_counts_include_every_state_without_merging_tenants(tmp_path):
    db_path = tmp_path / "registry.db"
    _seed(db_path)

    assert task_evidence_feed_counts(tenant_id="tenant-a", db_path=db_path) == {
        "pending": 2,
        "processed": 1,
        "fallback": 1,
        "rejected": 0,
    }
    assert task_evidence_feed_counts(db_path=db_path) == {
        "pending": 3,
        "processed": 1,
        "fallback": 1,
        "rejected": 1,
    }


@pytest.mark.parametrize("tenant_id", ["", "unknown", "tenant-a' OR 1=1 --"])
def test_missing_or_untrusted_identity_never_selects_other_tenants(tmp_path, tenant_id):
    db_path = tmp_path / "registry.db"
    _seed(db_path)

    assert task_graph_overview(tenant_id, db_path=db_path)["evidence_feed"] == {
        "pending": 0,
        "processed": 0,
        "fallback": 0,
        "rejected": 0,
    }


def test_overview_uses_explicit_registry_and_tenant(tmp_path):
    populated = tmp_path / "populated.db"
    empty = tmp_path / "other-home.db"
    _seed(populated)
    get_connection(empty).close()

    assert task_graph_overview("tenant-b", db_path=populated)["evidence_feed"] == {
        "pending": 1,
        "processed": 0,
        "fallback": 0,
        "rejected": 1,
    }
    assert task_graph_overview("tenant-b", db_path=empty)["evidence_feed"] == {
        "pending": 0,
        "processed": 0,
        "fallback": 0,
        "rejected": 0,
    }
