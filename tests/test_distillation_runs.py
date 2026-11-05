"""Persistent targeted runs use isolated state and never invoke a model."""
import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from xskill.pipeline.distillation import DistillationStore
from xskill.tasks.locking import root_run_lock


def store_at(tmp_path):
    return DistillationStore(tmp_path / "state" / "registry.db", tmp_path / "skills")


def test_prepare_restart_and_auto_leave_no_target(tmp_path):
    store = store_at(tmp_path)
    assert store.policy().mode == "auto"
    run = store.configure("create_one", "chosen", "A useful skill")
    assert not (tmp_path / "skills" / "chosen").exists()
    restarted = store_at(tmp_path)
    assert restarted.run()["run_id"] == run["run_id"]
    assert restarted.policy().target_skill == "chosen"
    restarted.configure("auto")
    assert restarted.policy().mode == "auto"
    assert restarted.status()["mode"] == "auto"


@pytest.mark.parametrize("mode,name,description", [
    ("create_one", "../outside", "description"),
    ("create_one", "Upper", "description"),
    ("create_one", "references", "description"),
    ("update_only", "unsafe. ", None),
    ("create_one", "chosen", ""),
    ("update_only", "missing", None),
    ("update_only", "missing", "override"),
])
def test_invalid_configuration_has_no_target_sideeffects(tmp_path, mode, name, description):
    store = store_at(tmp_path)
    with pytest.raises(ValueError):
        store.configure(mode, name, description)
    assert store.run() is None
    assert not store.skill_dir.exists()


def test_existing_empty_create_target_is_rejected(tmp_path):
    store = store_at(tmp_path)
    (store.skill_dir / "chosen").mkdir(parents=True)
    with pytest.raises(ValueError, match="exist"):
        store.configure("create_one", "chosen", "description")


def test_live_writer_lock_rejects_configure_immediately(tmp_path):
    store = store_at(tmp_path)
    with root_run_lock(store.db_path.parent, store.skill_dir):
        with ThreadPoolExecutor(1) as executor:
            result = executor.submit(store.configure, "create_one", "chosen", "description")
            with pytest.raises(RuntimeError, match="writer|running|lock"):
                result.result(timeout=2)


def test_atom_progress_uses_source_identity_and_stable_content(tmp_path):
    store = store_at(tmp_path)
    store.configure("create_one", "chosen", "description")
    atom = SimpleNamespace(atom_id="same", content="useful", title="Title", clustered=False)
    store.record_atom(atom, "skipped", source_key="source-a", reason="irrelevant")
    assert store.processed(atom, "source-a")
    atom.clustered = True
    assert store.processed(atom, "source-a")
    assert not store.processed(atom, "source-b")
    atom.content = "changed"
    assert not store.processed(atom, "source-a")


def test_resume_reuses_fixed_identity_and_rejects_conflicting_options(tmp_path):
    store = store_at(tmp_path)
    run = store.configure("create_one", "chosen", "description")
    with pytest.raises(ValueError):
        store.configure("auto", resume=run["run_id"])
    store.configure("auto")
    resumed = store.configure(resume=run["run_id"])
    assert resumed["run_id"] == run["run_id"]
    assert store.policy().mode == "create_one"


def test_corrupt_control_fails_closed(tmp_path):
    import sqlite3
    store = store_at(tmp_path)
    store.configure("create_one", "chosen", "description")
    with sqlite3.connect(store.db_path) as conn:
        conn.execute("UPDATE distillation_control SET active_run_id='missing'")
    with pytest.raises(RuntimeError):
        store.policy()


def test_update_is_rechecked_before_first_runtime_write(tmp_path, monkeypatch):
    from xskill.skill.git import init_skill_repo_on_baby
    monkeypatch.setattr("xskill.skill.catalog_store.notify_native_upsert", lambda *a, **k: None)
    store = store_at(tmp_path)
    target = store.skill_dir / "chosen"
    init_skill_repo_on_baby(str(target), "chosen", "Useful methods")
    store.configure("update_only", "chosen")
    (target / "SKILL.md").write_text((target / "SKILL.md").read_text(encoding="utf-8") + "\nHuman change\n", encoding="utf-8")
    with pytest.raises(ValueError, match="uncommitted"):
        store.ensure_target()


def test_checkpoint_trailer_reconciles_pending_audit_after_crash(tmp_path, monkeypatch):
    from xskill.skill.git import run_git
    from xskill.skill.write_policy import library_write_guard
    monkeypatch.setattr("xskill.skill.catalog_store.notify_native_upsert", lambda *a, **k: None)
    store = store_at(tmp_path)
    run = store.configure("create_one", "chosen", "Useful methods")
    target = store.ensure_target()
    atom = SimpleNamespace(atom_id="a1", content="useful")
    store.record_atom(atom, "pending", source_key="source-a")
    with library_write_guard(store.skill_dir, store.policy(), db_path=store.db_path):
        (target / "SKILL.md").write_text((target / "SKILL.md").read_text(encoding="utf-8") + "\nCheckpoint\n", encoding="utf-8")
        run_git(["add", "SKILL.md"], cwd=str(target))
        evidence = store.bind_checkpoint_evidence([(atom, "source-a")])
        rc, _, _ = run_git(["commit", "-m", f'checkpoint\n\nXSkill-Run: {run["run_id"]}\nXSkill-Evidence: {json.dumps(evidence)}'], cwd=str(target))
        assert rc == 0
    assert store.reconcile_checkpoints() == 1
    row = store.atom_record(atom, "source-a")
    assert row["state"] == "committed"
    assert row["checkpoint_sha"]
    assert store.reconcile_checkpoints() == 0


def test_interrupted_update_with_unproven_dirty_content_cannot_resume(tmp_path, monkeypatch):
    from xskill.skill.git import init_skill_repo_on_baby
    monkeypatch.setattr("xskill.skill.catalog_store.notify_native_upsert", lambda *a, **k: None)
    store = store_at(tmp_path)
    target = store.skill_dir / "chosen"
    init_skill_repo_on_baby(str(target), "chosen", "Useful methods")
    run = store.configure("update_only", "chosen")
    store.set_state("interrupted")
    content = (target / "SKILL.md").read_bytes() + b"\nUnproven manual work\n"
    (target / "SKILL.md").write_bytes(content)
    with pytest.raises(ValueError, match="uncommitted"):
        store.configure(resume=run["run_id"])
    with pytest.raises(ValueError, match="uncommitted"):
        store.ensure_target()
    assert (target / "SKILL.md").read_bytes() == content


def test_cross_process_library_lock_rejects_second_writer_without_waiting(tmp_path):
    import subprocess
    import sys
    script = """
import sys, time
from pathlib import Path
from xskill.tasks.locking import root_run_lock
started = time.monotonic()
try:
    with root_run_lock(Path(sys.argv[1]), Path(sys.argv[2])):
        raise AssertionError('second process acquired library writer lock')
except RuntimeError:
    assert time.monotonic() - started < 1.0
    sys.exit(23)
"""
    state_root = tmp_path / "state"
    skill_root = tmp_path / "skills"
    with root_run_lock(state_root, skill_root):
        result = subprocess.run([sys.executable, "-c", script, str(state_root), str(skill_root)],
                                capture_output=True, text=True, timeout=30)
    assert result.returncode == 23, result.stderr


def test_checkpoint_without_evidence_rejects_id_only_ack_atomically(tmp_path):
    store = store_at(tmp_path)
    store.configure("create_one", "chosen", "Useful methods")
    unique = SimpleNamespace(atom_id="unique", content="first")
    duplicate = SimpleNamespace(atom_id="duplicate", content="second")
    store.record_atom(unique, "accepted", source_key="source-a")
    store.record_atom(duplicate, "accepted", source_key="source-a")
    store.record_atom(duplicate, "accepted", source_key="source-b")
    with pytest.raises(RuntimeError, match="source/content"):
        store.checkpoint_atoms(["unique", "duplicate"], "fake-sha")
    assert store.atom_record(unique, "source-a")["state"] == "accepted"


def test_update_baseline_is_main_when_staging_is_checked_out(tmp_path, monkeypatch):
    from xskill.skill.git import init_skill_repo_on_baby, run_git
    monkeypatch.setattr("xskill.skill.catalog_store.notify_native_upsert", lambda *a, **k: None)
    store = store_at(tmp_path)
    target = store.skill_dir / "chosen"
    init_skill_repo_on_baby(str(target), "chosen", "Useful methods")
    assert run_git(["checkout", "-b", "main"], cwd=str(target))[0] == 0
    main_sha = run_git(["rev-parse", "main"], cwd=str(target))[1].strip()
    assert run_git(["checkout", "-b", "staging"], cwd=str(target))[0] == 0
    (target / "SKILL.md").write_text((target / "SKILL.md").read_text(encoding="utf-8") + "\nStaged update\n", encoding="utf-8")
    assert run_git(["add", "SKILL.md"], cwd=str(target))[0] == 0
    assert run_git(["commit", "-m", "staged update"], cwd=str(target))[0] == 0
    assert store.configure("update_only", "chosen")["base_sha"] == main_sha


def test_different_state_roots_cannot_lock_same_real_library(tmp_path):
    with root_run_lock(tmp_path / "state-a", tmp_path / "skills"):
        with pytest.raises(RuntimeError, match="lock|running|writer"):
            with root_run_lock(tmp_path / "state-b", tmp_path / "skills"):
                pass


@pytest.mark.parametrize("alive,allowed", [(True, False), (None, False), (False, True)])
def test_known_legacy_writer_pid_requires_confirmed_exit(tmp_path, monkeypatch, alive, allowed):
    import json
    store = store_at(tmp_path)
    store.db_path.parent.mkdir(parents=True)
    (store.db_path.parent / "agent_worker_status.json").write_text(json.dumps({"pid": 123456, "ok": True}), encoding="utf-8")
    monkeypatch.setattr("xskill.pipeline.distillation._pid_alive_strict", lambda pid: alive)
    monkeypatch.setattr("xskill.pipeline.distillation._legacy_writer_inventory", lambda *a: [])
    if allowed:
        assert store.configure("create_one", "chosen", "Useful methods")
    else:
        with pytest.raises(RuntimeError, match="running|confirm"):
            store.configure("create_one", "chosen", "Useful methods")
        assert not (store.skill_dir / "chosen").exists()


def test_legacy_writer_inventory_failure_cannot_be_treated_as_exit(tmp_path, monkeypatch):
    import json
    store = store_at(tmp_path)
    store.db_path.parent.mkdir(parents=True)
    (store.db_path.parent / "watcher_status.json").write_text(json.dumps({"ok": True, "stats": {}}), encoding="utf-8")
    def unknown(*args):
        raise TimeoutError("bounded process inventory timed out")
    monkeypatch.setattr("xskill.pipeline.distillation._legacy_writer_inventory", unknown)
    with pytest.raises(RuntimeError, match="cannot confirm"):
        store.configure("create_one", "chosen", "Useful methods")


def test_continued_atom_content_supersedes_only_same_source_pending_record(tmp_path):
    store = store_at(tmp_path)
    store.configure("create_one", "chosen", "Useful methods")
    old = SimpleNamespace(atom_id="continued", content="first")
    store.record_atom(old, "accepted", source_key="source-a")
    new = SimpleNamespace(atom_id="continued", content="first plus more")
    assert not store.processed(new, "source-a")
    store.record_atom(new, "pending", source_key="source-a")
    store.record_atom(new, "accepted", source_key="source-a")
    assert store.atom_record(old, "source-a")["state"] == "superseded"
    assert store.atom_record(new, "source-a")["retry_count"] == 0
    store.checkpoint_atoms(store.bind_checkpoint_evidence([(new, "source-a")]), "checkpoint-sha")
    assert store.atom_record(new, "source-a")["state"] == "committed"


@pytest.mark.parametrize("state", ["pending", "accepted"])
def test_truncated_checkpoint_recovery_cannot_retry_unproven_pending_material(tmp_path, monkeypatch, state):
    from xskill.skill.git import run_git
    from xskill.skill.write_policy import library_write_guard
    monkeypatch.setattr("xskill.skill.catalog_store.notify_native_upsert", lambda *a, **k: None)
    store = store_at(tmp_path)
    run = store.configure("create_one", "chosen", "Useful methods")
    target = store.ensure_target()
    atom = SimpleNamespace(atom_id="consumed-before-crash", content="useful")
    store.record_atom(atom, state, source_key="source-a")
    with library_write_guard(store.skill_dir, store.policy(), db_path=store.db_path):
        evidence = store.bind_checkpoint_evidence([(atom, "source-a")])
        for message in (f'checkpoint\n\nXSkill-Run: {run["run_id"]}\nXSkill-Evidence: {json.dumps(evidence)}',
                        "later unrelated target work"):
            text = (target / "SKILL.md").read_text(encoding="utf-8") + "\nLater work\n"
            (target / "SKILL.md").write_text(text, encoding="utf-8")
            assert run_git(["add", "SKILL.md"], cwd=str(target))[0] == 0
            assert run_git(["commit", "-m", message], cwd=str(target))[0] == 0
    with pytest.raises(RuntimeError, match="truncat|bounded|unprov"):
        store.reconcile_checkpoints(limit=1)
    assert store.atom_record(atom, "source-a")["state"] == state
    assert store.reconcile_checkpoints(limit=200) == 1
    assert store.atom_record(atom, "source-a")["state"] == "committed"


def test_verified_checkpoint_can_acknowledge_unique_pending_receipt(tmp_path):
    store = store_at(tmp_path)
    store.configure("create_one", "chosen", "Useful methods")
    atom = SimpleNamespace(atom_id="pending-receipt", content="already written before DB acknowledgement")
    store.record_atom(atom, "pending", source_key="source-a")
    store.checkpoint_atoms(store.bind_checkpoint_evidence([(atom, "source-a")]), "verified-checkpoint-sha")
    record = store.atom_record(atom, "source-a")
    assert record["state"] == "committed"
    assert record["candidate_applied"] == 1
    assert record["checkpoint_sha"] == "verified-checkpoint-sha"


def test_store_methods_close_sqlite_handles_before_state_directory_moves(tmp_path, monkeypatch):
    import sqlite3
    from xskill.pipeline import distillation
    store = store_at(tmp_path)
    store.configure("create_one", "chosen", "Useful methods")
    real_connect = sqlite3.connect
    opened = []
    class ObservedConnection(sqlite3.Connection):
        explicitly_closed = False
        def close(self):
            self.explicitly_closed = True
            return super().close()
    def connect(*args, **kwargs):
        kwargs["factory"] = ObservedConnection
        connection = real_connect(*args, **kwargs)
        opened.append(connection)  # Keep alive to expose missing explicit close.
        return connection
    monkeypatch.setattr(distillation.sqlite3, "connect", connect)
    atom = SimpleNamespace(atom_id="one", content="useful")
    store.policy()
    store.run()
    store.record_atom(atom, "pending", source_key="source-a")
    store.atom_record(atom, "source-a")
    store.set_state("stopped")
    store.set_artifact("waiting_evidence")
    store.status()
    with pytest.raises(sqlite3.OperationalError):
        with store._connect() as connection:
            connection.execute("SELECT * FROM missing_test_table")
    assert opened
    assert all(connection.explicitly_closed for connection in opened)
    state_root = store.db_path.parent
    moved_root = state_root.with_name("released-state")
    state_root.rename(moved_root)
    assert (moved_root / "registry.db").exists()
