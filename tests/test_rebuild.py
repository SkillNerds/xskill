"""tests/test_rebuild.py — `xskill rebuild` 重置轨迹 + --force 清仓清原子（子项目 A）

并含换模型护栏（0.6.1a2）：daemon 模型 ≠ config 模型时默认拒绝。
"""
from __future__ import annotations

import argparse
import json

import pytest

from xskill.cli import cmd_rebuild
from xskill.pipeline.registry import (
    register_dir,
    discover_trajectories,
    update_traj_status,
    update_traj_offset,
    get_trajs_by_status,
    get_connection,
    mark_not_fit,
    reset_trajectories,
)
from xskill.skill.repo import SkillRepo


def _rebuild_args(**over):
    base = dict(force=False, eco=None, traj=None, ignore_model_mismatch=False)
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

    n = reset_trajectories(eco="ngagent", db_path=db_path)

    assert n == 1
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

    n = reset_trajectories(eco="ngagent", db_path=db_path)

    assert n == 1
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


def test_reset_requeues_not_fit_and_clears_interest_fields(tmp_path, db_path):
    trajectory_directory = tmp_path / "ngagent_sessions"
    trajectory_directory.mkdir()
    (trajectory_directory / "traj_ng_x.md").write_text(
        "# traj\n## User\nhi\n", encoding="utf-8",
    )
    watch_dir_id = register_dir(
        trajectory_directory,
        label="ngagent",
        ecosystem="ngagent",
        db_path=db_path,
    )
    discover_trajectories(watch_dir_id, trajectory_directory, db_path=db_path)
    mark_not_fit(
        watch_dir_id,
        "traj_ng_x.md",
        "not infra",
        "old-interest",
        db_path=db_path,
    )

    reset_count = reset_trajectories(eco="ngagent", db_path=db_path)

    assert reset_count == 1
    assert "traj_ng_x.md" in get_trajs_by_status(
        watch_dir_id, "discovered", db_path=db_path,
    )
    connection = get_connection(db_path)
    try:
        row = connection.execute(
            "SELECT process_action, interest_fingerprint, error_msg "
            "FROM trajectories WHERE filename='traj_ng_x.md'"
        ).fetchone()
    finally:
        connection.close()
    assert row["process_action"] is None
    assert row["interest_fingerprint"] is None
    assert row["error_msg"] is None


def test_wipe_all_skills_removes_skill_dirs_keeps_references(tmp_path):
    root = tmp_path / "skill"
    root.mkdir()
    foo = root / "foo"
    foo.mkdir()
    (foo / "SKILL.md").write_text("---\nname: foo\ndescription: d\n---\n", encoding="utf-8")
    bar = root / "bar"
    (bar / ".git").mkdir(parents=True)  # baby 态：只有 .git 还没 SKILL.md
    refs = root / "references"
    refs.mkdir()
    (refs / "x.md").write_text("keep me", encoding="utf-8")

    n = SkillRepo(root).wipe_all_skills()

    assert n == 2
    assert not foo.exists() and not bar.exists()
    assert refs.exists(), "references 不应被删"


# ── 换模型护栏（0.6.1a2）──────────────────────────────────────────

def test_rebuild_refuses_on_model_mismatch(monkeypatch, capsys):
    """daemon 在跑且其模型 ≠ config 模型 → 拒绝(返回2)，且不重置轨迹。"""
    monkeypatch.setattr("xskill.runtime.read_status",
                        lambda: {"running": True, "llm_model": "old-model"})
    monkeypatch.setattr("xskill.runtime.config_models",
                        lambda: {"llm_model": "new-model"})
    called = {"reset": False}
    monkeypatch.setattr("xskill.pipeline.registry.reset_trajectories",
                        lambda **_kwargs: called.__setitem__("reset", True))

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
                        lambda **_kwargs: 0)

    assert cmd_rebuild(_rebuild_args(), None) == 0


def test_rebuild_ignore_flag_bypasses_mismatch(monkeypatch):
    monkeypatch.setattr("xskill.runtime.read_status",
                        lambda: {"running": True, "llm_model": "old"})
    monkeypatch.setattr("xskill.runtime.config_models",
                        lambda: {"llm_model": "new"})
    monkeypatch.setattr("xskill.pipeline.registry.reset_trajectories",
                        lambda **_kwargs: 0)

    assert cmd_rebuild(_rebuild_args(ignore_model_mismatch=True), None) == 0


def test_rebuild_no_guard_when_daemon_not_running(monkeypatch):
    """daemon 没跑 → 不比对(重启 serve 时会读新 config),正常重置。"""
    monkeypatch.setattr("xskill.runtime.read_status",
                        lambda: {"running": False})
    monkeypatch.setattr("xskill.runtime.config_models",
                        lambda: {"llm_model": "new"})
    monkeypatch.setattr("xskill.pipeline.registry.reset_trajectories",
                        lambda **_kwargs: 0)

    assert cmd_rebuild(_rebuild_args(), None) == 0
