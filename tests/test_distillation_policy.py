from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from pathlib import Path
import os
import pytest

from xskill.skill.write_policy import (
    DistillationPolicy,
    SkillWriteDenied,
    current_write_policy,
    library_write_guard,
    normalize_root,
    require_skill_write,
    use_write_policy,
)


def policy(root, target="target", **kw):
    return DistillationPolicy(
        mode="update_only",
        run_id="run",
        root_key=normalize_root(root),
        target_skill=target,
        generation=1,
        **kw,
    )


def test_policy_is_fixed_and_rejects_other_identities(tmp_path):
    p = policy(tmp_path)
    with pytest.raises(FrozenInstanceError):
        p.target_skill = "other"
    for name in ("other", "../target", "C:target", ".", ".git"):
        with pytest.raises(SkillWriteDenied):
            p.require_skill(name, operation="write_content")
    for op in ("rename", "move", "create"):
        with pytest.raises(SkillWriteDenied):
            p.require_skill("target", operation=op)


def test_process_fence_denies_unbound_thread_and_nested_broadening(tmp_path):
    (tmp_path / "target").mkdir()
    p = policy(tmp_path)
    with library_write_guard(tmp_path, p):
        assert current_write_policy(tmp_path) == p
        require_skill_write(tmp_path / "target" / "SKILL.md")
        with pytest.raises(SkillWriteDenied):
            with use_write_policy(DistillationPolicy()):
                require_skill_write(tmp_path / "target")
        with ThreadPoolExecutor(1) as pool:
            with pytest.raises(SkillWriteDenied):
                pool.submit(require_skill_write, tmp_path / "target").result()
        with pytest.raises(SkillWriteDenied):
            require_skill_write(tmp_path / "other")
        with pytest.raises(SkillWriteDenied):
            require_skill_write(tmp_path.parent / "foreign" / "target")


def test_paths_links_hardlinks_and_git_aliases_rejected(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    outside = tmp_path / "outside"
    outside.write_text("x")
    os.link(outside, target / "hard")
    with library_write_guard(tmp_path, policy(tmp_path)):
        with pytest.raises(SkillWriteDenied):
            require_skill_write(target / "hard")
        with pytest.raises(SkillWriteDenied):
            require_skill_write(target / ".." / "outside")
        (target / ".git").write_text("gitdir: ../elsewhere")
        with pytest.raises(SkillWriteDenied):
            require_skill_write(target, operation="git_mutation")


def test_create_once_then_allow_target_content(tmp_path):
    p = DistillationPolicy(
        mode="create_one",
        run_id="run",
        root_key=normalize_root(tmp_path),
        target_skill="new-skill",
        generation=1,
    )
    with library_write_guard(tmp_path, p):
        target = tmp_path / "new-skill"
        require_skill_write(target, operation="create")
        target.mkdir()
        require_skill_write(target / "SKILL.md")
        with pytest.raises(SkillWriteDenied):
            require_skill_write(target, operation="create")


def test_update_only_missing_target_cannot_be_recreated(tmp_path):
    with library_write_guard(tmp_path, policy(tmp_path)):
        with pytest.raises(SkillWriteDenied):
            require_skill_write(tmp_path / "target")


def test_link_in_target_directory_is_rejected_at_framework_entry(tmp_path):
    import subprocess

    target = tmp_path / "target"
    target.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    link = target / "references"
    if os.name == "nt":
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(outside)],
            check=True,
            capture_output=True,
        )
    else:
        link.symlink_to(outside, target_is_directory=True)
    try:
        with library_write_guard(tmp_path, policy(tmp_path)):
            with pytest.raises(SkillWriteDenied):
                require_skill_write(target)
    finally:
        if os.name == "nt":
            link.rmdir()
        else:
            link.unlink()


def test_verified_write_does_not_truncate_file_swapped_after_preflight(
    tmp_path, monkeypatch
):
    from xskill.skill.write_policy import safe_write_text

    target = tmp_path / "target"
    target.mkdir()
    path = target / "content"
    path.write_text("initial")
    victim = tmp_path / "victim"
    victim.write_text("untouched")
    original = os.open

    def swap_then_open(file, flags, *args, **kwargs):
        if Path(file) == path or (
            kwargs.get("dir_fd") is not None and Path(file) == Path(path.name)
        ):
            path.unlink()
            os.link(victim, path)
        return original(file, flags, *args, **kwargs)

    with library_write_guard(tmp_path, policy(tmp_path)):
        monkeypatch.setattr(os, "open", swap_then_open)
        with pytest.raises(SkillWriteDenied):
            safe_write_text(path, "replacement")
    assert victim.read_text() == "untouched"


def test_low_level_content_write_cannot_change_frontmatter_identity(tmp_path):
    from xskill.skill.write_policy import safe_write_text

    target = tmp_path / "target"
    target.mkdir()
    skill_md = target / "SKILL.md"
    skill_md.write_text("---\nname: target\ndescription: test\n---\nbody")
    with library_write_guard(tmp_path, policy(tmp_path)):
        with pytest.raises(SkillWriteDenied):
            safe_write_text(skill_md, "---\nname: other\ndescription: test\n---\nbody")
    assert "name: target" in skill_md.read_text()


def test_bound_restricted_context_cannot_outlive_library_guard(tmp_path):
    (tmp_path / "target").mkdir()
    fixed = policy(tmp_path)
    with use_write_policy(fixed):
        with library_write_guard(tmp_path, fixed):
            require_skill_write(tmp_path / "target")
        with pytest.raises(SkillWriteDenied):
            require_skill_write(tmp_path / "target")


def test_generation_change_invalidates_bound_writes(tmp_path):
    import sqlite3

    (tmp_path / "target").mkdir()
    fixed = policy(tmp_path)
    database = tmp_path / "registry.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE distillation_control (root_key TEXT PRIMARY KEY, active_run_id TEXT, generation INTEGER)"
        )
        connection.execute(
            "INSERT INTO distillation_control VALUES (?, ?, ?)",
            (fixed.root_key, fixed.run_id, fixed.generation),
        )
        connection.execute(
            "CREATE TABLE distillation_runs (run_id TEXT PRIMARY KEY,mode TEXT,target_skill TEXT,root_key TEXT,target_identity TEXT,owns_target INTEGER)"
        )
        import json

        identity = (tmp_path / "target").stat()
        connection.execute(
            "INSERT INTO distillation_runs VALUES (?,?,?,?,?,?)",
            (
                fixed.run_id,
                fixed.mode,
                fixed.target_skill,
                fixed.root_key,
                json.dumps({"dev": identity.st_dev, "ino": identity.st_ino}),
                0,
            ),
        )
    with library_write_guard(tmp_path, fixed, db_path=database):
        require_skill_write(tmp_path / "target")
        with sqlite3.connect(database) as connection:
            connection.execute(
                "UPDATE distillation_control SET generation = generation + 1"
            )
        with pytest.raises(SkillWriteDenied):
            require_skill_write(tmp_path / "target")


@pytest.mark.skipif(
    os.name != "nt", reason="Windows directory handle replacement protection"
)
def test_verified_write_pins_target_prefix_against_replacement(tmp_path, monkeypatch):
    from xskill.skill.write_policy import safe_write_text

    target = tmp_path / "target"
    target.mkdir()
    path = target / "content"
    original = os.open
    attempts = []

    def try_replace_then_open(file, flags, *args, **kwargs):
        if Path(file) == path or (
            kwargs.get("dir_fd") is not None and Path(file) == Path(path.name)
        ):
            try:
                target.rename(tmp_path / "moved-target")
            except OSError:
                attempts.append("blocked")
        return original(file, flags, *args, **kwargs)

    with library_write_guard(tmp_path, policy(tmp_path)):
        monkeypatch.setattr(os, "open", try_replace_then_open)
        safe_write_text(path, "safe")
    assert attempts == ["blocked"]
    assert path.read_text() == "safe"


def test_background_context_cannot_write_after_guard_closes(tmp_path):
    from contextvars import copy_context
    import threading

    target = tmp_path / "target"
    target.mkdir()
    release = threading.Event()
    fixed = policy(tmp_path)

    def delayed_write():
        assert release.wait(5)
        require_skill_write(target / "late-content")

    with ThreadPoolExecutor(1) as pool:
        with library_write_guard(tmp_path, fixed):
            copied = copy_context()
            pending = pool.submit(copied.run, delayed_write)
        release.set()
        with pytest.raises(SkillWriteDenied):
            pending.result(timeout=5)
    assert not (target / "late-content").exists()


def test_fresh_guard_rejects_replaced_persisted_target_directory(tmp_path):
    import shutil
    from xskill.pipeline.distillation import DistillationStore

    store = DistillationStore(tmp_path / "state" / "registry.db", tmp_path / "skills")
    store.configure("create_one", "target", "test description")
    target = store.ensure_target()
    previous = tmp_path / "original-target"
    target.rename(previous)
    shutil.copytree(previous, target)
    with pytest.raises(SkillWriteDenied):
        with library_write_guard(
            store.skill_dir, store.policy(), db_path=store.db_path
        ):
            pass


@pytest.mark.skipif(os.name != "nt", reason="Windows startup root prefix pinning")
def test_create_startup_pins_library_root_before_raw_scaffold_mkdir(
    tmp_path, monkeypatch
):
    from xskill.pipeline.distillation import DistillationStore

    store = DistillationStore(tmp_path / "state" / "registry.db", tmp_path / "skills")
    store.configure("create_one", "target", "test description")
    target = store.skill_dir / "target"
    original = Path.mkdir
    attempts = []

    def attack(path, *args, **kwargs):
        if path == target and not attempts:
            try:
                store.skill_dir.rename(tmp_path / "replaced-skills")
            except OSError:
                attempts.append("blocked")
            else:
                attempts.append("replaced")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", attack)
    store.ensure_target()
    assert attempts == ["blocked"]
    assert (target / "SKILL.md").exists()
    assert not (tmp_path / "replaced-skills").exists()


def test_readonly_policy_connections_close_without_waiting_for_gc(
    tmp_path, monkeypatch
):
    import sqlite3
    from xskill.pipeline.distillation import DistillationStore
    from xskill.skill import write_policy

    store = DistillationStore(tmp_path / "state" / "registry.db", tmp_path / "skills")
    store.configure("create_one", "target", "test description")
    store.ensure_target()
    fixed = store.policy()
    retained = []

    class Tracked(sqlite3.Connection):
        closed_explicitly = False

        def close(self):
            self.closed_explicitly = True
            super().close()

    connect = sqlite3.connect

    def track(*args, **kwargs):
        kwargs["factory"] = Tracked
        connection = connect(*args, **kwargs)
        retained.append(connection)
        return connection

    monkeypatch.setattr(write_policy.sqlite3, "connect", track)
    with library_write_guard(store.skill_dir, fixed, db_path=store.db_path):
        require_skill_write(store.skill_dir / "target")
    assert retained
    assert all(connection.closed_explicitly for connection in retained)
