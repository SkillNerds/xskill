"""Low-level mutations cannot escape a fixed-target run; all fixtures are temporary."""

import pytest
from xskill.skill.write_policy import (
    DistillationPolicy,
    SkillWriteDenied,
    library_write_guard,
    normalize_root,
)
from xskill.skill import candidates, git


def _policy(root):
    return DistillationPolicy("update_only", "r", normalize_root(root), "target", 1)


@pytest.fixture
def library(tmp_path):
    for name in ("target", "other"):
        git.init_skill_repo_on_baby(str(tmp_path / name), name, "test description")
    locks = tmp_path / ".repo_locks"
    for path in locks.iterdir():
        path.unlink()
    locks.rmdir()
    return tmp_path


@pytest.mark.parametrize(
    "operation",
    [
        lambda root: candidates.save_candidates(root / "other", {"candidates": []}),
        lambda root: candidates.clear_candidates(root / "other"),
        lambda root: git.run_git(["add", "."], str(root / "other")),
        lambda root: git.init_skill_repo_on_baby(str(root / "new"), "new", "test"),
    ],
)
def test_non_target_writes_denied_before_lock_or_directory_creation(library, operation):
    with library_write_guard(library, _policy(library)):
        with pytest.raises(SkillWriteDenied):
            operation(library)
    assert not (library / ".repo_locks").exists()
    assert not (library / "new").exists()
    assert not (library / "other" / ".candidates.yml").exists()


def test_readonly_git_on_other_skill_does_not_create_lock_files(library):
    with library_write_guard(library, _policy(library)):
        assert git.run_git(["rev-parse", "HEAD"], str(library / "other"))[0] == 0
        assert git.run_git(["status", "--porcelain"], str(library / "other"))[0] == 0
    assert not (library / ".repo_locks").exists()


def test_candidates_target_write_and_move_denial(library):
    with library_write_guard(library, _policy(library)):
        candidates.save_candidates(
            library / "target", {"candidates": [{"atom_id": "a", "weightscore": 4}]}
        )
        with pytest.raises(SkillWriteDenied):
            candidates.move_atom_contribution(
                library / "target", library / "target", "a"
            )
    assert (
        candidates.load_candidates(library / "target")["candidates"][0]["atom_id"]
        == "a"
    )


@pytest.mark.parametrize(
    "name", ["delete_skill", "rollback_skill", "freeze_skill", "unfreeze_skill"]
)
def test_management_mutations_even_target_are_blocked(library, name):
    from xskill.skill import skill

    with library_write_guard(library, _policy(library)):
        with pytest.raises(SkillWriteDenied):
            getattr(skill, name)(library, "target")
    assert (library / "target" / "SKILL.md").exists()


def test_import_and_wipe_are_blocked(library, tmp_path):
    from xskill.skill.importer import import_one_skill
    from xskill.skill.repo import SkillRepo

    with library_write_guard(library, _policy(library)):
        with pytest.raises(SkillWriteDenied):
            import_one_skill(library, library / "other")
        with pytest.raises(SkillWriteDenied):
            SkillRepo(library).wipe_all_skills()


def test_non_target_canary_and_scripting_markers_blocked(library):
    from xskill.canary import ensure_gitignore
    from xskill.skill.scripting import clear_scripting_request

    with library_write_guard(library, _policy(library)):
        for mutate in (
            lambda: clear_scripting_request(library / "other"),
            lambda: ensure_gitignore(library / "other"),
        ):
            with pytest.raises(SkillWriteDenied):
                mutate()


def test_repo_lock_reparse_directory_is_rejected(library, tmp_path):
    import os, subprocess

    outside = tmp_path / "external-locks"
    outside.mkdir()
    link = library / ".repo_locks"
    if os.name == "nt":
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(outside)],
            check=True,
            capture_output=True,
        )
    else:
        link.symlink_to(outside, target_is_directory=True)
    try:
        with library_write_guard(library, _policy(library)):
            with pytest.raises(SkillWriteDenied):
                candidates.save_candidates(library / "target", {"candidates": []})
        assert list(outside.iterdir()) == []
    finally:
        if os.name == "nt":
            link.rmdir()
        else:
            link.unlink()


def test_target_ux_scores_link_cannot_modify_other_file(library):
    import os
    from xskill.canary import AtomCanary

    victim = library / "other" / "score"
    victim.write_text("untouched")
    os.link(victim, library / "target" / ".ux_scores.jsonl")
    with library_write_guard(library, _policy(library)):
        with pytest.raises(SkillWriteDenied):
            AtomCanary(library / "target").append(
                atom_id="a",
                skill_name="target",
                side="main",
                commit_sha="a" * 40,
                score=8,
                reasons="test",
            )
    assert victim.read_text() == "untouched"


def test_non_target_installer_denied_before_destination_parent_creation(library):
    from xskill.ecosystems._shared import _install_skill_into

    dest = library / "install" / "skills"
    with library_write_guard(library, _policy(library)):
        with pytest.raises(SkillWriteDenied):
            _install_skill_into(library / "other", dest, "main", ecosystem_label="test")
    assert not dest.exists()


def test_install_rejects_destination_junction_to_other_skill(library):
    import os, subprocess
    from xskill.ecosystems._shared import _install_skill_into

    dest = library / "install"
    dest.mkdir()
    link = dest / "target"
    if os.name == "nt":
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(library / "other")],
            check=True,
            capture_output=True,
        )
    else:
        link.symlink_to(library / "other", target_is_directory=True)
    try:
        with library_write_guard(library, _policy(library)):
            with pytest.raises(SkillWriteDenied):
                _install_skill_into(
                    library / "target", dest, "main", ecosystem_label="test"
                )
        assert link.exists()
    finally:
        if os.name == "nt":
            link.rmdir()
        else:
            link.unlink()


def test_direct_install_ledger_writes_obey_library_fence(library):
    from xskill.ecosystems.install_ledger import InstallLedger

    ledger = InstallLedger(library / "state" / "ledger.db")
    dest = library.parent / (library.name + "-installed") / "other"
    kwargs = dict(
        skill_name="other",
        mode="copy",
        source=str(library / "other"),
        source_sha="a" * 40,
        installation_id="b" * 32,
        content_identity="c" * 64,
        baseline_identity="d" * 64,
        file_fingerprints={},
    )
    ledger.record_install(dest, **kwargs)
    original = ledger.read_install(dest)
    with library_write_guard(library, _policy(library)):
        with pytest.raises(SkillWriteDenied):
            ledger.record_install(dest, **kwargs)
        with pytest.raises(SkillWriteDenied):
            ledger.update_copy_baseline(
                dest,
                file_fingerprints={"SKILL.md": "f" * 64},
                baseline_identity="e" * 64,
            )
        with pytest.raises(SkillWriteDenied):
            ledger.begin_removal(dest)
    assert ledger.read_install(dest) == original


def test_canary_cleanup_path_is_checked_before_git_refs_change(library):
    import os, subprocess
    from xskill.canary import discard_staging, has_staging

    target = library / "target"
    assert git.run_git(["checkout", "-b", "main"], str(target))[0] == 0
    assert git.run_git(["branch", "staging", "HEAD"], str(target))[0] == 0
    canary = library / ".canary"
    canary.mkdir()
    link = canary / "target"
    if os.name == "nt":
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(library / "other")],
            check=True,
            capture_output=True,
        )
    else:
        link.symlink_to(library / "other", target_is_directory=True)
    try:
        with library_write_guard(library, _policy(library)):
            with pytest.raises(SkillWriteDenied):
                discard_staging(target)
            assert has_staging(target)
    finally:
        if os.name == "nt":
            link.rmdir()
        else:
            link.unlink()


def test_readonly_diff_does_not_stage_non_target_worktree(library):
    from hashlib import sha256

    other = library / "other"
    skill_md = other / "SKILL.md"
    skill_md.write_text(
        skill_md.read_text(encoding="utf-8") + "\nchanged\n", encoding="utf-8"
    )

    def snapshot():
        return {
            str(p.relative_to(other / ".git")): sha256(p.read_bytes()).hexdigest()
            for p in (other / ".git").rglob("*")
            if p.is_file()
        }

    before = snapshot()
    with library_write_guard(library, _policy(library)):
        code, diff, _ = git.run_git(["diff", "HEAD"], str(other))
        assert code == 0
        assert "+changed" in diff
        assert git.run_git(["diff", "--cached", "--name-only"], str(other))[0] == 0
    assert snapshot() == before
    assert not (library / ".repo_locks").exists()


def test_persisted_restriction_denies_fresh_framework_caller(library):
    import json, sqlite3

    database = library / "state" / "registry.db"
    database.parent.mkdir()
    fixed = _policy(library)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE distillation_control (root_key TEXT PRIMARY KEY, active_run_id TEXT, generation INTEGER)"
        )
        connection.execute(
            "CREATE TABLE distillation_runs (run_id TEXT PRIMARY KEY, root_key TEXT, mode TEXT, target_skill TEXT)"
        )
        connection.execute(
            "INSERT INTO distillation_control VALUES (?, ?, ?)",
            (fixed.root_key, fixed.run_id, fixed.generation),
        )
        connection.execute(
            "INSERT INTO distillation_runs VALUES (?, ?, ?, ?)",
            (fixed.run_id, fixed.root_key, fixed.mode, fixed.target_skill),
        )
    (library / ".distillation-control.json").write_text(
        json.dumps({"root_key": fixed.root_key, "db_path": str(database)})
    )
    with pytest.raises(SkillWriteDenied):
        candidates.save_candidates(library / "other", {"candidates": []})
    with pytest.raises(SkillWriteDenied):
        candidates.save_candidates(library / "target", {"candidates": []})
    with pytest.raises(SkillWriteDenied):
        with library_write_guard(library, DistillationPolicy()):
            pass
    assert not (library / ".repo_locks").exists()


def _persisted_state(root):
    import json
    import sqlite3

    database = root / "state" / "registry.db"
    database.parent.mkdir()
    fixed = _policy(root)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE distillation_control (root_key TEXT PRIMARY KEY, active_run_id TEXT, generation INTEGER)"
        )
        connection.execute(
            "CREATE TABLE distillation_runs (run_id TEXT PRIMARY KEY, root_key TEXT, mode TEXT, target_skill TEXT)"
        )
        connection.execute(
            "INSERT INTO distillation_control VALUES (?, ?, ?)",
            (fixed.root_key, fixed.run_id, fixed.generation),
        )
        connection.execute(
            "INSERT INTO distillation_runs VALUES (?, ?, ?, ?)",
            (fixed.run_id, fixed.root_key, fixed.mode, fixed.target_skill),
        )
    (root / ".distillation-control.json").write_text(
        json.dumps({"root_key": fixed.root_key, "db_path": str(database)})
    )
    return fixed, database


def test_fresh_process_rejects_persisted_restriction_at_real_write_entrances(library):
    import hashlib, subprocess, sys

    _persisted_state(library)

    def snapshot():
        return {
            str(file.relative_to(library)): hashlib.sha256(
                file.read_bytes()
            ).hexdigest()
            for name in ("target", "other")
            for file in (library / name).rglob("*")
            if file.is_file()
        }

    before = snapshot()
    script = r"""
import sys
from pathlib import Path
from xskill.skill import candidates, git
from xskill.skill.skill import delete_skill
from xskill.skill.write_policy import SkillWriteDenied
root = Path(sys.argv[1])
for operation in (
    lambda: candidates.save_candidates(root / 'other', {'candidates': []}),
    lambda: candidates.save_candidates(root / 'target', {'candidates': []}),
    lambda: git.run_git(['add', '.'], str(root / 'other')),
    lambda: delete_skill(root, 'target'),
):
    try:
        operation()
    except SkillWriteDenied:
        continue
    raise AssertionError('fresh process bypassed persisted write restriction')
assert git.run_git(['rev-parse', 'HEAD'], str(root / 'other'))[0] == 0
assert not (root / '.repo_locks').exists()
"""
    completed = subprocess.run(
        [sys.executable, "-X", "utf8", "-c", script, str(library)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    assert snapshot() == before


@pytest.mark.parametrize(
    "payload",
    ["invalid-json", '{"root_key":"wrong-root","db_path":"not-a-registry"}', "{}"],
)
def test_unverifiable_locator_fails_before_mutation(library, payload):
    (library / ".distillation-control.json").write_text(payload)
    with pytest.raises(SkillWriteDenied):
        candidates.save_candidates(library / "other", {"candidates": []})
    assert not (library / ".repo_locks").exists()
    assert not (library / "other" / ".candidates.yml").exists()


def test_guard_cannot_use_foreign_registry_for_registered_library(library):
    fixed, _ = _persisted_state(library)
    with pytest.raises(SkillWriteDenied):
        with library_write_guard(library, fixed, db_path=library / "foreign.db"):
            pass
    assert not (library / "foreign.db").exists()


def test_registry_parent_junction_cannot_substitute_auto_state(library):
    import os, sqlite3, subprocess

    fixed, database = _persisted_state(library)
    auto_state = library / "auto-state"
    auto_state.mkdir()
    with sqlite3.connect(auto_state / "registry.db") as connection:
        connection.execute(
            "CREATE TABLE distillation_control (root_key TEXT PRIMARY KEY,active_run_id TEXT,generation INTEGER)"
        )
        connection.execute(
            "CREATE TABLE distillation_runs (run_id TEXT PRIMARY KEY,root_key TEXT,mode TEXT,target_skill TEXT)"
        )
        connection.execute(
            "INSERT INTO distillation_control VALUES (?,NULL,0)", (fixed.root_key,)
        )
    import json

    linked_parent = library / "state-alias"
    (library / ".distillation-control.json").write_text(
        json.dumps(
            {"root_key": fixed.root_key, "db_path": str(linked_parent / "registry.db")}
        )
    )
    if os.name == "nt":
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(linked_parent), str(auto_state)],
            check=True,
            capture_output=True,
        )
    else:
        linked_parent.symlink_to(auto_state, target_is_directory=True)
    try:
        with pytest.raises(SkillWriteDenied):
            candidates.save_candidates(library / "other", {"candidates": []})
        assert not (library / ".repo_locks").exists()
        assert not (library / "other" / ".candidates.yml").exists()
    finally:
        if os.name == "nt":
            linked_parent.rmdir()
        else:
            linked_parent.unlink()


def test_locator_parent_junction_cannot_alias_library_control(library):
    import os, subprocess
    from xskill.skill.write_policy import current_write_policy

    _persisted_state(library)
    alias = library.parent / (library.name + "-alias")
    if os.name == "nt":
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(alias), str(library)],
            check=True,
            capture_output=True,
        )
    else:
        alias.symlink_to(library, target_is_directory=True)
    try:
        with pytest.raises(SkillWriteDenied):
            current_write_policy(alias)
    finally:
        if os.name == "nt":
            alias.rmdir()
        else:
            alias.unlink()


def test_locator_file_symlink_is_rejected_when_platform_supports_it(library):
    from xskill.skill.write_policy import current_write_policy

    _persisted_state(library)
    locator = library / ".distillation-control.json"
    original = library / "original-control.json"
    locator.rename(original)
    try:
        locator.symlink_to(original)
    except OSError as error:
        pytest.skip(f"file symlink capability unavailable: {error.errno}")
    with pytest.raises(SkillWriteDenied):
        current_write_policy(library)


def test_nested_auto_locator_cannot_shadow_outer_restriction_in_fresh_process(library):
    import json, sqlite3, subprocess, sys

    fixed, _ = _persisted_state(library)
    nested = library / "target"
    database = library / "nested-auto.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE distillation_control (root_key TEXT PRIMARY KEY,active_run_id TEXT,generation INTEGER)"
        )
        connection.execute(
            "CREATE TABLE distillation_runs (run_id TEXT PRIMARY KEY,root_key TEXT,mode TEXT,target_skill TEXT)"
        )
        connection.execute(
            "INSERT INTO distillation_control VALUES (?,NULL,1)",
            (normalize_root(nested),),
        )
    (nested / ".distillation-control.json").write_text(
        json.dumps({"root_key": normalize_root(nested), "db_path": str(database)})
    )
    script = r"""
import sys
from pathlib import Path
from xskill.skill import candidates
from xskill.skill.write_policy import SkillWriteDenied, current_write_policy
root = Path(sys.argv[1])
assert current_write_policy(root / 'target').mode == 'update_only'
try:
    candidates.save_candidates(root / 'target', {'candidates': []})
except SkillWriteDenied:
    pass
else:
    raise AssertionError('nested auto control shadowed outer fixed run')
assert not (root / '.repo_locks').exists()
"""
    result = subprocess.run(
        [sys.executable, "-X", "utf8", "-c", script, str(library)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert not (nested / ".candidates.yml").exists()


def test_guard_cannot_register_auto_inside_live_restricted_root(library):
    from concurrent.futures import ThreadPoolExecutor

    with library_write_guard(library, _policy(library)):

        def enter_nested():
            with library_write_guard(library / "target", DistillationPolicy()):
                pass

        with ThreadPoolExecutor(1) as pool:
            with pytest.raises(SkillWriteDenied):
                pool.submit(enter_nested).result()


def test_generic_target_writer_cannot_create_control_locator(library):
    from xskill.skill.write_policy import safe_write_text

    with library_write_guard(library, _policy(library)):
        with pytest.raises(SkillWriteDenied):
            safe_write_text(library / "target" / ".distillation-control.json", "{}")
    assert not (library / "target" / ".distillation-control.json").exists()


@pytest.mark.skipif(
    __import__("os").name != "nt", reason="Windows directory prefix pinning"
)
@pytest.mark.parametrize("stage", ["copy", "reset"])
def test_installer_pins_prefix_at_copy_and_reset_mutation(library, monkeypatch, stage):
    from xskill.ecosystems import _fallback
    from xskill.agents.user_edit_absorb_agent import ReverseSyncStatus

    source = library / "target"
    parent = library.parent / (library.name + "-copy-install")
    parent.mkdir()
    dest = parent / "target"
    if stage == "reset":
        dest.mkdir()
        (dest / "old.txt").write_text("old")
    monkeypatch.setattr(_fallback, "_write_install_meta", lambda *args: None)
    monkeypatch.setattr(
        _fallback,
        "_maybe_reverse_sync_before_overwrite",
        lambda *args: ReverseSyncStatus.NO_EDIT,
    )
    attempts = []
    original = (
        _fallback.safe_write_bytes
        if stage == "copy"
        else _fallback.safe_remove_auxiliary_tree
    )

    def mutate(*args, **kwargs):
        try:
            parent.rename(parent.with_name(parent.name + "-moved"))
        except OSError:
            attempts.append("blocked")
        return original(*args, **kwargs)

    monkeypatch.setattr(
        _fallback,
        "safe_write_bytes" if stage == "copy" else "safe_remove_auxiliary_tree",
        mutate,
    )
    with library_write_guard(library, _policy(library)):
        _fallback.install_dir(
            source, dest, force_mode="copy", auto_reset=stage == "reset"
        )
    assert attempts and set(attempts) == {"blocked"}
    assert (dest / "SKILL.md").read_bytes() == (source / "SKILL.md").read_bytes()


@pytest.mark.skipif(
    __import__("os").name != "nt", reason="Windows directory prefix pinning"
)
def test_shared_installer_pins_prefix_at_existing_copy_backup(library, monkeypatch):
    from xskill.ecosystems import _shared
    from pathlib import Path
    import shutil

    parent = library.parent / (library.name + "-backup-install")
    parent.mkdir()
    dest = parent / "target"
    dest.mkdir()
    (dest / "manual.txt").write_text("preserve")
    monkeypatch.setattr(_shared, "copy_install_is_current", lambda *args: False)

    def copy_install(source, output):
        shutil.copytree(source, output, ignore=shutil.ignore_patterns(".git"))
        return "copy"

    monkeypatch.setattr(_shared, "install_dir", copy_install)
    original = Path.rename
    attempts = []

    def rename(path, output):
        if path == dest:
            try:
                original(parent, parent.with_name(parent.name + "-moved"))
            except OSError:
                attempts.append("blocked")
        return original(path, output)

    monkeypatch.setattr(Path, "rename", rename)
    with library_write_guard(library, _policy(library)):
        _shared._install_skill_into(
            library / "target", parent, "main", ecosystem_label="test"
        )
    assert attempts == ["blocked"]
    assert (
        parent / ".target.replaced-by-symlink" / "manual.txt"
    ).read_text() == "preserve"
    assert (dest / "SKILL.md").exists()


def test_authorized_target_read_waits_for_other_process_writer(library):
    from concurrent.futures import ThreadPoolExecutor, TimeoutError
    from contextvars import copy_context
    import subprocess, sys, time

    ready = library.parent / (library.name + "-writer-ready")
    script = r"""
import sys
from pathlib import Path
from xskill.skill.git import skill_repo_lock
from xskill.skill.write_policy import DistillationPolicy, library_write_guard, normalize_root
root, ready = Path(sys.argv[1]), Path(sys.argv[2])
p = DistillationPolicy('update_only', 'r', normalize_root(root), 'target', 1)
with library_write_guard(root, p):
    with skill_repo_lock(root / 'target', use_git_write_limit=False):
        ready.write_text('ready')
        sys.stdin.readline()
"""
    writer = subprocess.Popen(
        [sys.executable, "-X", "utf8", "-c", script, str(library), str(ready)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 15
        while (
            not ready.exists() and writer.poll() is None and time.monotonic() < deadline
        ):
            time.sleep(0.01)
        assert ready.exists(), "temporary writer failed to acquire repository lock"
        with library_write_guard(library, _policy(library)):
            before = {p.name for p in (library / ".repo_locks").iterdir()}
            assert git.run_git(["rev-parse", "HEAD"], str(library / "other"))[0] == 0
            assert {p.name for p in (library / ".repo_locks").iterdir()} == before
            with ThreadPoolExecutor(1) as pool:
                copied = copy_context()
                pending = pool.submit(
                    copied.run,
                    git.run_git,
                    ["rev-parse", "HEAD"],
                    str(library / "target"),
                )
                try:
                    with pytest.raises(TimeoutError):
                        pending.result(timeout=0.2)
                finally:
                    writer.communicate("\n", timeout=10)
                assert pending.result(timeout=10)[0] == 0
    finally:
        if writer.poll() is None:
            writer.kill()
            writer.communicate(timeout=10)
        ready.unlink(missing_ok=True)


def test_locator_without_control_row_fails_closed(library):
    import sqlite3

    _, database = _persisted_state(library)
    with sqlite3.connect(database) as connection:
        connection.execute("DELETE FROM distillation_control")
    with pytest.raises(SkillWriteDenied):
        candidates.save_candidates(library / "other", {"candidates": []})
    assert not (library / ".repo_locks").exists()


def test_conflicting_nested_registered_restrictions_fail_closed(library):
    import json, sqlite3
    from xskill.skill.write_policy import current_write_policy

    _persisted_state(library)
    nested = library / "target"
    database = library / "nested-restricted.db"
    key = normalize_root(nested)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE distillation_control (root_key TEXT PRIMARY KEY,active_run_id TEXT,generation INTEGER)"
        )
        connection.execute(
            "CREATE TABLE distillation_runs (run_id TEXT PRIMARY KEY,root_key TEXT,mode TEXT,target_skill TEXT)"
        )
        connection.execute(
            "INSERT INTO distillation_control VALUES (?,'inner',1)", (key,)
        )
        connection.execute(
            "INSERT INTO distillation_runs VALUES ('inner',?,'update_only','inner-skill')",
            (key,),
        )
    (nested / ".distillation-control.json").write_text(
        json.dumps({"root_key": key, "db_path": str(database)})
    )
    with pytest.raises(SkillWriteDenied):
        current_write_policy(nested)


@pytest.mark.skipif(
    __import__("os").name != "nt", reason="Windows destination root pinning"
)
def test_restricted_copy_pins_destination_root_at_first_file_write(
    library, monkeypatch
):
    from xskill.ecosystems import _fallback

    parent = library.parent / (library.name + "-root-pin")
    parent.mkdir()
    dest = parent / "target"
    attempted = []
    original = _fallback.safe_write_bytes

    def attack(path, content, **kwargs):
        if not attempted:
            try:
                dest.rename(parent / "swapped-target")
            except OSError:
                attempted.append("blocked")
        return original(path, content, **kwargs)

    monkeypatch.setattr(_fallback, "safe_write_bytes", attack)
    with library_write_guard(library, _policy(library)):
        _fallback._do_copy(library / "target", dest)
    assert attempted == ["blocked"]
    assert (dest / "SKILL.md").read_bytes() == (
        library / "target" / "SKILL.md"
    ).read_bytes()


@pytest.mark.skipif(
    __import__("os").name != "nt", reason="Windows deletion prefix pinning"
)
def test_auxiliary_delete_pins_root_and_parent_at_leaf_unlink(library, monkeypatch):
    from pathlib import Path
    from xskill.skill.write_policy import safe_remove_auxiliary_tree

    out = library / ".canary" / "target"
    (out / "scripts").mkdir(parents=True)
    (out / "scripts" / "content.bin").write_bytes(b"\x00\xff")
    original = Path.unlink
    attempts = []

    def attack(path, *args, **kwargs):
        if path.is_relative_to(out) and not attempts:
            for directory, label in ((out, "root"), (out.parent, "parent")):
                try:
                    directory.rename(directory.with_name(directory.name + "-moved"))
                except OSError:
                    attempts.append(label)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", attack)
    victim = (library / "other" / "SKILL.md").read_bytes()
    with library_write_guard(library, _policy(library)):
        safe_remove_auxiliary_tree(out, library / "target")
    assert attempts == ["root", "parent"]
    assert not out.exists()
    assert (library / "other" / "SKILL.md").read_bytes() == victim


@pytest.mark.parametrize("side", ["main", "staging"])
def test_existing_target_install_mount_is_authorized_for_both_sides(library, side):
    from xskill.skill.write_policy import auxiliary_write_guard
    import os, subprocess

    canary = library / ".canary" / "target"
    canary.mkdir(parents=True)
    (canary / "SKILL.md").write_bytes((library / "target" / "SKILL.md").read_bytes())
    parent = library.parent / (library.name + "-existing-install")
    parent.mkdir()
    dest = parent / "target"
    source = library / "target" if side == "main" else canary
    if os.name == "nt":
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(dest), str(source)],
            check=True,
            capture_output=True,
        )
    else:
        dest.symlink_to(source, target_is_directory=True)
    try:
        with library_write_guard(library, _policy(library)):
            with auxiliary_write_guard(
                dest, library / "target", allow_source_link=True
            ):
                assert dest.resolve() == source
    finally:
        if os.name == "nt":
            dest.rmdir()
        else:
            dest.unlink()
