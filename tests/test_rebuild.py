"""tests/test_rebuild.py — `xskill rebuild` 重置轨迹 + --force 清仓清原子（子项目 A）

并含换模型护栏（0.6.1a2）：daemon 模型 ≠ config 模型时默认拒绝。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from unittest.mock import Mock

import pytest

from xskill.cli import cmd_rebuild
from xskill.pipeline.registry import (
    get_connection,
    register_dir,
    discover_trajectories,
    update_traj_status,
    update_traj_offset,
    get_trajs_by_status,
    reset_trajectories,
    mark_not_fit,
    clear_rebuild_derived_state,
)
from xskill.skill.repo import SkillRepo


def _rebuild_args(**over):
    base = dict(
        force=False,
        eco=None,
        traj=None,
        ignore_model_mismatch=False,
    )
    base.update(over)
    return argparse.Namespace(**base)


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "reg.db"


def _seed_done_traj(tmp_path, db_path, *, with_atoms=False):
    d = tmp_path / "ngagent_sessions"
    d.mkdir()
    (d / "traj_ng_x.md").write_text("# traj\n## User\nhi\n", encoding="utf-8")
    wid = register_dir(d, label="ngagent", ecosystem="ngagent", db_path=db_path)
    discover_trajectories(wid, d, db_path=db_path)
    update_traj_status(wid, "traj_ng_x.md", "done", db_path=db_path)
    update_traj_offset(wid, "traj_ng_x.md", last_offset=500,
                       last_atom_id="atom_traj_ng_x_0002", tasks_extracted=2,
                       db_path=db_path)
    if with_atoms:
        tasks = d / "traj_ng_x" / "tasks"
        tasks.mkdir(parents=True)
        (tasks / "atom_traj_ng_x_0001.json").write_text(
            json.dumps({"atom_id": "atom_traj_ng_x_0001"}), encoding="utf-8")
        (tasks / "atom_traj_ng_x_0002.json").write_text(
            json.dumps({"atom_id": "atom_traj_ng_x_0002"}), encoding="utf-8")
    return d, wid


def test_reset_flips_status_to_discovered_and_zeros_offset(tmp_path, db_path):
    _d, wid = _seed_done_traj(tmp_path, db_path)
    assert "traj_ng_x.md" not in get_trajs_by_status(wid, "discovered", db_path=db_path)

    reset_ids = reset_trajectories(eco="ngagent", db_path=db_path)

    assert len(reset_ids) == 1
    assert "traj_ng_x.md" in get_trajs_by_status(wid, "discovered", db_path=db_path)


def test_reset_eco_filter_skips_other_ecosystems(tmp_path, db_path):
    _seed_done_traj(tmp_path, db_path)
    # 另一个生态的轨迹不应被 eco=ngagent 的重置波及
    other = tmp_path / "cc_sessions"
    other.mkdir()
    (other / "traj_cc_y.md").write_text("# t", encoding="utf-8")
    wid2 = register_dir(other, ecosystem="claude_code", db_path=db_path)
    discover_trajectories(wid2, other, db_path=db_path)
    update_traj_status(wid2, "traj_cc_y.md", "done", db_path=db_path)

    reset_ids = reset_trajectories(eco="ngagent", db_path=db_path)

    assert len(reset_ids) == 1
    assert "traj_cc_y.md" not in get_trajs_by_status(wid2, "discovered", db_path=db_path)


def test_reset_always_deletes_atom_files(tmp_path, db_path):
    """splitter 续接点取自 atom 文件——必须删 atom 才能真正触发重拆（0.6.1a1 洞）。"""
    d, _wid = _seed_done_traj(tmp_path, db_path, with_atoms=True)
    tasks = d / "traj_ng_x" / "tasks"
    assert list(tasks.glob("atom_*.json"))

    reset_trajectories(eco="ngagent", db_path=db_path)

    assert not list(tasks.glob("atom_*.json")), "reset 应删光原子文件"


def test_reset_deletes_stale_index_pkl(tmp_path, db_path):
    """删 atom 同时删该目录的 index.pkl——否则陈旧 embedding 指向已删 atom。"""
    d, _wid = _seed_done_traj(tmp_path, db_path, with_atoms=True)
    idx = d / "index.pkl"
    idx.write_bytes(b"stale-index")
    assert idx.is_file()

    reset_trajectories(eco="ngagent", db_path=db_path)

    assert not idx.exists(), "reset 应删陈旧 index.pkl"


def test_reset_removes_only_target_trajectory_from_vector_projection(tmp_path, db_path):
    from tests.test_atom_task_store import _FakeEmbed
    from xskill.pipeline.atom import AtomTaskStore

    directory, _watch_dir_id = _seed_done_traj(
        tmp_path, db_path, with_atoms=True,
    )
    store = AtomTaskStore(directory)
    store.rebuild_vector_index(_FakeEmbed())
    connection = sqlite3.connect(directory / store.VECTOR_INDEX_FILE)
    try:
        assert connection.execute("SELECT COUNT(*) FROM atom_vectors").fetchone()[0]
    finally:
        connection.close()

    reset_trajectories(traj_id="traj_ng_x", db_path=db_path)
    connection = sqlite3.connect(directory / store.VECTOR_INDEX_FILE)
    try:
        remaining = connection.execute(
            "SELECT COUNT(*) FROM atom_vectors WHERE traj_id='traj_ng_x'"
        ).fetchone()[0]
    finally:
        connection.close()
    assert remaining == 0


def test_reset_deletes_atom_location_projection_rows(tmp_path, db_path):
    from xskill.pipeline.atom import AtomTask, AtomTaskStore

    directory, _wid = _seed_done_traj(tmp_path, db_path)
    store = AtomTaskStore(directory)
    atom = AtomTask(
        atom_id="atom_traj_ng_x_0001",
        traj_id="traj_ng_x",
        offset_start=1,
        offset_end=2,
        intent="intent",
        summary="summary",
    )
    store.save(atom)

    reset_trajectories(eco="ngagent", db_path=db_path)

    connection = store._location_connection()
    try:
        count = connection.execute(
            "SELECT COUNT(*) FROM atom_locations WHERE traj_id=?",
            ("traj_ng_x",),
        ).fetchone()[0]
    finally:
        connection.close()
    assert count == 0


def test_reset_marks_only_team_profile_dirty(tmp_path, db_path):
    from xskill.recommend.profile_dirty import list_dirty_profiles

    sessions = tmp_path / "trajectories" / "clients" / "alice" / "sessions"
    sessions.mkdir(parents=True)
    (sessions / "traj_team.md").write_text("# team", encoding="utf-8")
    watch_dir_id = register_dir(
        sessions,
        label="alice",
        ecosystem="team_client",
        db_path=db_path,
    )
    discover_trajectories(watch_dir_id, sessions, db_path=db_path)

    reset_trajectories(traj_id="traj_team", db_path=db_path)

    dirty = list_dirty_profiles(db_path=db_path)
    assert [(row["user_key"], row["reason"]) for row in dirty] == [
        ("alice", "atom_reset"),
    ]


def test_reset_requeues_not_fit_and_clears_interest_fields(tmp_path, db_path):
    directory_path, watch_dir_id = _seed_done_traj(tmp_path, db_path)
    mark_not_fit(
        watch_dir_id,
        "traj_ng_x.md",
        "not infra",
        "fingerprint-old",
        db_path=db_path,
    )

    reset_ids = reset_trajectories(eco="ngagent", db_path=db_path)

    assert len(reset_ids) == 1
    assert "traj_ng_x.md" in get_trajs_by_status(
        watch_dir_id, "discovered", db_path=db_path)
    conn = get_connection(db_path)
    row = conn.execute(
        "SELECT process_action, error_msg, interest_fingerprint "
        "FROM trajectories WHERE filename='traj_ng_x.md'"
    ).fetchone()
    conn.close()
    assert row["process_action"] is None
    assert row["error_msg"] is None
    assert row["interest_fingerprint"] is None
    assert directory_path.is_dir()


def test_reset_clears_skill_usage_canary_and_ux_fields(tmp_path, db_path):
    _, watch_dir_id = _seed_done_traj(tmp_path, db_path)
    connection = get_connection(db_path)
    connection.execute(
        "UPDATE trajectories SET skill_generated=?, skill_used=?, "
        "canary_side=?, ux_score=? WHERE filename=?",
        ("legacy-skill", "legacy-skill", "staging", 9.0, "traj_ng_x.md"),
    )
    connection.commit()
    connection.close()

    reset_trajectories(eco="ngagent", db_path=db_path)

    connection = get_connection(db_path)
    trajectory_row = connection.execute(
        "SELECT skill_generated, skill_used, canary_side, ux_score "
        "FROM trajectories WHERE filename='traj_ng_x.md'"
    ).fetchone()
    connection.close()
    assert trajectory_row["skill_generated"] is None
    assert trajectory_row["skill_used"] is None
    assert trajectory_row["canary_side"] is None
    assert trajectory_row["ux_score"] is None


def test_clear_rebuild_derived_state_keeps_llm_usage(tmp_path):
    registry_path = tmp_path / "registry.db"
    connection = get_connection(registry_path)
    connection.execute(
        "INSERT INTO recommendation_log(client_id, skill, side, bucket) "
        "VALUES('client-one', 'skill-one', 'main', 'recommended')"
    )
    connection.execute(
        "INSERT INTO atom_adoption(atom_id, skill, weightscore, was_new) "
        "VALUES('atom-one', 'skill-one', 5, 1)"
    )
    connection.execute(
        "INSERT INTO canary_decision(skill, action, main_avg, staging_avg, "
        "main_samples, staging_samples, age_days) "
        "VALUES('skill-one', 'promoted', 8.0, 9.0, 6, 6, 1.0)"
    )
    connection.execute(
        "INSERT INTO skill_trigger_eval(skill, exp_id, train_score, "
        "test_score, n_cases, catalog_size) "
        "VALUES('skill-one', 'experiment-one', 0.7, 0.6, 10, 4)"
    )
    connection.execute(
        "INSERT INTO llm_usage(step, model, prompt, completion, total, cost_usd, "
        "price_source) VALUES('split', 'model-one', 1, 2, 3, 0.01, 'config')"
    )
    connection.commit()
    connection.close()

    deleted_counts = clear_rebuild_derived_state(registry_path=registry_path)

    assert deleted_counts == {
        "recommendation_log": 1,
        "atom_adoption": 1,
        "canary_decision": 1,
        "skill_trigger_eval": 1,
    }
    connection = get_connection(registry_path)
    assert connection.execute(
        "SELECT COUNT(*) FROM recommendation_log").fetchone()[0] == 0
    assert connection.execute(
        "SELECT COUNT(*) FROM atom_adoption").fetchone()[0] == 0
    assert connection.execute(
        "SELECT COUNT(*) FROM canary_decision").fetchone()[0] == 0
    assert connection.execute(
        "SELECT COUNT(*) FROM skill_trigger_eval").fetchone()[0] == 0
    assert connection.execute("SELECT COUNT(*) FROM llm_usage").fetchone()[0] == 1
    connection.close()


def test_wipe_all_skills_removes_skill_dirs_keeps_references(tmp_path):
    skill_root = tmp_path / "skill"
    skill_root.mkdir()
    first_skill_path = skill_root / "foo"
    first_skill_path.mkdir()
    (first_skill_path / "SKILL.md").write_text(
        "---\nname: foo\ndescription: d\n---\n",
        encoding="utf-8",
    )
    second_skill_path = skill_root / "bar"
    (second_skill_path / ".git").mkdir(parents=True)  # baby 态：只有 .git 还没 SKILL.md
    references_directory = skill_root / "references"
    references_directory.mkdir()
    (references_directory / "x.md").write_text("keep me", encoding="utf-8")
    (skill_root / ".skill_index.pkl").write_bytes(b"stale-skill-index")

    removed_count, kept_names = SkillRepo(skill_root).wipe_all_skills()

    assert removed_count == 2
    assert kept_names == []
    assert not first_skill_path.exists() and not second_skill_path.exists()
    assert references_directory.exists(), "references 不应被删"
    assert not (skill_root / ".skill_index.pkl").exists()


def test_wipe_all_skills_retries_enotempty_on_git_objects(tmp_path, monkeypatch):
    """serve/git 还在写 .git/objects 时 rmtree 会 ENOTEMPTY；应重试删干净。"""
    import errno
    import shutil
    import xskill.skill.skill as skill_mod

    skill_root = tmp_path / "skill"
    skill_path = skill_root / "foo"
    objects = skill_path / ".git" / "objects" / "ab"
    objects.mkdir(parents=True)
    blob = objects / ("c" * 38)
    blob.write_bytes(b"git-obj")
    blob.chmod(0o444)
    (skill_path / "SKILL.md").write_text(
        "---\nname: foo\ndescription: d\n---\n",
        encoding="utf-8",
    )

    real_rmtree = shutil.rmtree
    calls = {"n": 0}

    def flaky(path, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError(errno.ENOTEMPTY, "Directory not empty", "objects")
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(skill_mod.shutil, "rmtree", flaky)
    monkeypatch.setattr(skill_mod.time, "sleep", lambda _s: None)

    removed_count, kept_names = SkillRepo(skill_root).wipe_all_skills()

    assert removed_count == 1
    assert kept_names == []
    assert not skill_path.exists()
    leftovers = [p for p in skill_root.iterdir() if p.name.startswith(".wipe-")]
    assert leftovers == []
    assert calls["n"] >= 2


def test_wipe_all_skills_cleans_leftover_wipe_dir(tmp_path):
    skill_root = tmp_path / "skill"
    leftover = skill_root / ".wipe-foo-1"
    (leftover / ".git").mkdir(parents=True)
    (leftover / "SKILL.md").write_text("# leftover\n", encoding="utf-8")

    removed_count, kept_names = SkillRepo(skill_root).wipe_all_skills()

    assert removed_count == 1
    assert kept_names == []
    assert not leftover.exists()


def test_wipe_all_skills_deletes_readonly_git_objects(tmp_path):
    skill_root = tmp_path / "skill"
    skill_path = skill_root / "packed"
    objects = skill_path / ".git" / "objects" / "de"
    objects.mkdir(parents=True)
    blob = objects / ("f" * 38)
    blob.write_bytes(b"packed")
    blob.chmod(0o444)
    (skill_path / "SKILL.md").write_text(
        "---\nname: packed\ndescription: d\n---\n",
        encoding="utf-8",
    )

    assert SkillRepo(skill_root).wipe_all_skills() == (1, [])
    assert not skill_path.exists()


def test_wipe_all_skills_keeps_imported_skill(tmp_path, monkeypatch):
    from xskill.skill.importer import import_one_skill

    monkeypatch.setattr(
        "xskill.config.get_registry_db_path", lambda: tmp_path / "r.db")
    skill_root = tmp_path / "skill"
    distilled = skill_root / "distilled"
    distilled.mkdir(parents=True)
    (distilled / "SKILL.md").write_text(
        "---\nname: distilled\ndescription: d\n---\n",
        encoding="utf-8",
    )
    incoming = tmp_path / "incoming" / "kept-import"
    incoming.mkdir(parents=True)
    (incoming / "SKILL.md").write_text(
        "---\nname: kept-import\ndescription: user brought this\n---\nbody\n",
        encoding="utf-8",
    )
    imported = import_one_skill(skill_root, incoming)
    assert imported.name == "kept-import"

    removed_count, kept_names = SkillRepo(skill_root).wipe_all_skills(
        db_path=tmp_path / "r.db",
    )

    assert removed_count == 1
    assert kept_names == ["kept-import"]
    assert not distilled.exists()
    assert (skill_root / "kept-import" / "SKILL.md").is_file()
    assert (skill_root / "kept-import" / ".xskill-origin").read_text(
        encoding="utf-8",
    ).strip() == "import"


def test_wipe_all_skills_keeps_legacy_import_commit_without_marker(tmp_path, monkeypatch):
    """0.6.30a1 纳入时还没有 .xskill-origin，靠 import: 提交认出来。"""
    from xskill.skill.git import init_imported_repo_on_main

    monkeypatch.setattr(
        "xskill.config.get_registry_db_path", lambda: tmp_path / "r.db")
    skill_root = tmp_path / "skill"
    legacy = skill_root / "legacy-import"
    legacy.mkdir(parents=True)
    (legacy / "SKILL.md").write_text(
        "---\nname: legacy-import\ndescription: old import\n---\n",
        encoding="utf-8",
    )
    init_imported_repo_on_main(legacy, "import: legacy-import from /tmp/old")
    distilled = skill_root / "baby-only"
    distilled.mkdir(parents=True)
    (distilled / ".git").mkdir()
    (distilled / "SKILL.md").write_text("# baby\n", encoding="utf-8")

    removed_count, kept_names = SkillRepo(skill_root).wipe_all_skills(
        db_path=tmp_path / "r.db",
    )

    assert removed_count == 1
    assert kept_names == ["legacy-import"]
    assert (legacy / "SKILL.md").is_file()
    assert not distilled.exists()


def test_rebuild_force_clears_derived_state_and_install_history(
    monkeypatch, tmp_path,
):
    skill_root = tmp_path / "skill"
    skill_root.mkdir()
    install_history_path = tmp_path / "install_history.jsonl"
    install_history_path.write_text("{}\n", encoding="utf-8")
    cleanup_mock = Mock(
        return_value={
            "recommendation_log": 1,
            "atom_adoption": 2,
            "canary_decision": 3,
            "skill_trigger_eval": 4,
        }
    )
    reset_mock = Mock(return_value=[])
    monkeypatch.setattr("xskill.config.XSKILL_HOME", tmp_path)
    monkeypatch.setattr("xskill.config.get_skill_dir", Mock(return_value=skill_root))
    monkeypatch.setattr("xskill.runtime.read_status", Mock(return_value={"running": False}))
    monkeypatch.setattr(
        "xskill.pipeline.registry.clear_rebuild_derived_state",
        cleanup_mock,
    )
    monkeypatch.setattr("xskill.pipeline.registry.reset_trajectories", reset_mock)

    assert cmd_rebuild(_rebuild_args(force=True), None) == 0

    cleanup_mock.assert_called_once_with()
    reset_mock.assert_called_once_with(eco=None, traj_id=None)
    assert not install_history_path.exists()


# ── 换模型护栏（0.6.1a2）──────────────────────────────────────────

def test_rebuild_refuses_on_model_mismatch(monkeypatch, capsys):
    """daemon 在跑且其模型 ≠ config 模型 → 拒绝(返回2)，且不重置轨迹。"""
    monkeypatch.setattr("xskill.runtime.read_status",
                        lambda: {"running": True, "llm_model": "old-model"})
    monkeypatch.setattr("xskill.runtime.config_models",
                        lambda: {"llm_model": "new-model"})
    called = {"reset": False}
    monkeypatch.setattr("xskill.pipeline.registry.reset_trajectories",
                        lambda **_ignored_keyword_arguments: called.__setitem__(
                            "reset", True,
                        ))

    rc = cmd_rebuild(_rebuild_args(), None)

    assert rc == 2
    assert called["reset"] is False, "拒绝时不应重置轨迹"
    err = capsys.readouterr().err
    assert "old-model" in err and "new-model" in err


def test_rebuild_proceeds_when_models_match(monkeypatch):
    monkeypatch.setattr("xskill.runtime.read_status",
                        lambda: {"running": True, "llm_model": "m"})
    monkeypatch.setattr("xskill.runtime.config_models",
                        lambda: {"llm_model": "m"})
    monkeypatch.setattr("xskill.pipeline.registry.reset_trajectories",
                        lambda **_ignored_keyword_arguments: [])

    assert cmd_rebuild(_rebuild_args(), None) == 0


def test_rebuild_ignore_flag_bypasses_mismatch(monkeypatch):
    monkeypatch.setattr("xskill.runtime.read_status",
                        lambda: {"running": True, "llm_model": "old"})
    monkeypatch.setattr("xskill.runtime.config_models",
                        lambda: {"llm_model": "new"})
    monkeypatch.setattr("xskill.pipeline.registry.reset_trajectories",
                        lambda **_ignored_keyword_arguments: [])

    assert cmd_rebuild(_rebuild_args(ignore_model_mismatch=True), None) == 0


def test_rebuild_no_guard_when_daemon_not_running(monkeypatch):
    """daemon 没跑 → 不比对(重启 serve 时会读新 config),正常重置。"""
    monkeypatch.setattr("xskill.runtime.read_status",
                        lambda: {"running": False})
    monkeypatch.setattr("xskill.runtime.config_models",
                        lambda: {"llm_model": "new"})
    monkeypatch.setattr("xskill.pipeline.registry.reset_trajectories",
                        lambda **_ignored_keyword_arguments: [])

    assert cmd_rebuild(_rebuild_args(), None) == 0


def test_rebuild_writes_single_cold_start_signal(monkeypatch, tmp_path):
    monkeypatch.setattr("xskill.config.XSKILL_HOME", tmp_path)
    monkeypatch.setattr(
        "xskill.runtime.read_status",
        lambda: {"running": False, "role": "server", "mode": "server"},
    )
    monkeypatch.setattr("xskill.pipeline.registry.reset_trajectories",
                        lambda **_ignored_keyword_arguments: [7, 8])

    assert cmd_rebuild(_rebuild_args(), None) == 0

    assert (tmp_path / "COLD_START").exists()
    assert not (tmp_path / "COLD_START_REQUEST").exists()
    assert not (tmp_path / "COLD_START_FLUSH").exists()
    from xskill.pipeline.cold_start import ColdStartSignal
    snapshot_payload = ColdStartSignal(tmp_path).snapshot()
    assert snapshot_payload is not None
    assert snapshot_payload["trajectory_ids"] == [7, 8]
