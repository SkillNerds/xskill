"""SkillEdit 持久脏队列：增量调度、重启恢复与并发版本确认。"""

from __future__ import annotations

import json
import sqlite3
from concurrent.futures import Future
from pathlib import Path
from unittest import mock

import pytest

from xskill.pipeline import registry as reg
from xskill.pipeline.atom import AtomTaskStore
from xskill.pipeline.registry import register_dir
from xskill.pipeline.runner import DirectoryWatcher
from xskill.skill import candidates
from xskill.skill.git import init_skill_repo_on_baby
from tests.pool_helpers import pool_config


def _unused_agent_factory(**_kwargs):
    raise AssertionError("non-actionable skills must not construct an agent")


def _make_watcher(tmp_path: Path, skill_root: Path) -> DirectoryWatcher:
    db = tmp_path / "registry.db"
    watch_dir = tmp_path / "watch"
    watch_dir.mkdir(exist_ok=True)
    register_dir(watch_dir, db_path=db)
    return DirectoryWatcher(
        config={
            "llm": {"base_url": "x", "model": "y", "api_key": "z"},
            "watcher": {"full_reconcile_interval": 3600},
        },
        skill_dir=skill_root,
        poll_interval=1.0,
        pool_config=pool_config(workers=1),
        db_path=db,
        store=AtomTaskStore(root=watch_dir),
        agno_agent_factory=_unused_agent_factory,
        home_root=tmp_path,
        xskill_home=tmp_path,
    )


def _seed_baby(skill_root: Path, name: str, score: int = 1) -> Path:
    skill_path = skill_root / name
    init_skill_repo_on_baby(
        str(skill_path),
        name=name,
        description="dirty queue test",
    )
    candidates.add_atom_contributions(
        skill_path,
        [(f"atom-{name}", score, "")],
    )
    return skill_path


def test_candidate_writes_coalesce_per_skill_and_use_generation_fence(
    tmp_path: Path,
) -> None:
    db = tmp_path / "registry.db"
    skill_root = tmp_path / "skills"
    foo = skill_root / "foo"
    bar = skill_root / "bar"
    foo.mkdir(parents=True)
    bar.mkdir()

    with mock.patch(
        "xskill.skill.catalog_store.resolve_catalog_db_path",
        return_value=db,
    ):
        candidates.add_atom_contributions(foo, [("shared-atom", 4, "")])
        candidates.add_atom_contributions(bar, [("shared-atom", 6, "")])
        candidates.add_atom_contributions(foo, [("shared-atom", 8, "")])

    rows = reg.list_skill_edit_dirty(skill_root, db_path=db)
    assert [(row["skill"], row["generation"]) for row in rows] == [
        ("bar", 1),
        ("foo", 2),
    ]
    with sqlite3.connect(db) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM skill_edit_dirty",
        ).fetchone()[0] == 2

    assert not reg.acknowledge_skill_edit_dirty(
        skill_root,
        "foo",
        1,
        db_path=db,
    )
    assert reg.acknowledge_skill_edit_dirty(
        skill_root,
        "foo",
        2,
        db_path=db,
    )
    assert [
        row["skill"]
        for row in reg.list_skill_edit_dirty(skill_root, db_path=db)
    ] == ["bar"]


@pytest.mark.performance_contract
def test_idle_round_does_not_rescan_skill_directories_or_candidates(
    tmp_path: Path,
    monkeypatch,
) -> None:
    skill_root = tmp_path / "skills"
    skill_root.mkdir()
    _seed_baby(skill_root, "alpha")
    _seed_baby(skill_root, "beta")
    watcher = _make_watcher(tmp_path, skill_root)
    try:
        watcher._check_pending_skill_edits()
        assert reg.list_skill_edit_dirty(
            skill_root,
            db_path=tmp_path / "registry.db",
        ) == []

        def reject_iterdir(_path):
            raise AssertionError("idle SkillEdit round scanned the skill root")

        def reject_candidate_read(_path):
            raise AssertionError("idle SkillEdit round read candidates")

        monkeypatch.setattr(Path, "iterdir", reject_iterdir)
        monkeypatch.setattr(candidates, "load_candidates", reject_candidate_read)
        watcher._check_pending_skill_edits()
    finally:
        watcher.stop()


def test_failed_reconciliation_is_throttled_until_next_interval(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from xskill.pipeline import runner as runner_module

    skill_root = tmp_path / "skills"
    skill_root.mkdir()
    watcher = _make_watcher(tmp_path, skill_root)
    watcher.full_reconcile_interval = 60
    calls: list[Path] = []

    def fail_reconcile(path, *, db_path=None):
        calls.append(Path(path))
        raise sqlite3.OperationalError("registry unavailable")

    monkeypatch.setattr(reg, "reconcile_skill_edit_dirty", fail_reconcile)
    monkeypatch.setattr(reg, "list_skill_edit_dirty", lambda *_a, **_kw: [])
    times = iter((100.0, 120.0, 161.0))
    monkeypatch.setattr(runner_module.time, "monotonic", lambda: next(times))
    try:
        watcher._check_pending_skill_edits()
        watcher._check_pending_skill_edits()
        watcher._check_pending_skill_edits()
    finally:
        watcher.stop()

    assert calls == [skill_root, skill_root]


def test_candidate_change_only_checks_and_schedules_the_changed_skill(
    tmp_path: Path,
    monkeypatch,
) -> None:
    skill_root = tmp_path / "skills"
    skill_root.mkdir()
    alpha = _seed_baby(skill_root, "alpha")
    _seed_baby(skill_root, "beta")
    watcher = _make_watcher(tmp_path, skill_root)
    try:
        watcher._check_pending_skill_edits()
        with mock.patch(
            "xskill.skill.catalog_store.resolve_catalog_db_path",
            return_value=tmp_path / "registry.db",
        ):
            candidates.add_atom_contributions(
                alpha,
                [("atom-alpha", 10, "updated")],
            )

        loaded: list[str] = []
        original_load = candidates.load_candidates

        def counted_load(path):
            loaded.append(Path(path).name)
            return original_load(path)

        submitted: list[str] = []

        def capture_submit(_callable, skill_path, **_kwargs):
            submitted.append(Path(skill_path).name)
            return Future()

        monkeypatch.setattr(candidates, "load_candidates", counted_load)
        monkeypatch.setattr(watcher._pools["edit"], "submit", capture_submit)
        watcher._check_pending_skill_edits()

        assert loaded == ["alpha"]
        assert submitted == ["alpha"]
    finally:
        watcher.stop()


def test_new_main_ux_score_requeues_a_waiting_skill(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from xskill.skill.git import commit_baby_to_main_branch

    skill_root = tmp_path / "skills"
    skill_root.mkdir()
    skill_path = _seed_baby(skill_root, "waiting-for-ux", score=10)
    assert commit_baby_to_main_branch(str(skill_path), "graduate for UX gate")
    watcher = _make_watcher(tmp_path, skill_root)
    db = tmp_path / "registry.db"
    try:
        # 候选已达阈值但还没有 main UX 证据，本轮应确认脏项并等待评分事件。
        watcher._check_pending_skill_edits()
        assert reg.list_skill_edit_dirty(skill_root, db_path=db) == []

        (skill_path / ".ux_scores.jsonl").write_text(
            json.dumps({"side": "main", "score": 8}) + "\n",
            encoding="utf-8",
        )
        watcher._notify_skill_edit_ux_change(skill_path, db_path=db)
        rows = reg.list_skill_edit_dirty(skill_root, db_path=db)
        assert [(row["skill"], row["reason"]) for row in rows] == [
            ("waiting-for-ux", "ux_score"),
        ]

        submitted: list[str] = []

        def capture_submit(_callable, path, **_kwargs):
            submitted.append(Path(path).name)
            return Future()

        monkeypatch.setattr(watcher._pools["edit"], "submit", capture_submit)
        watcher._check_pending_skill_edits()
        assert submitted == ["waiting-for-ux"]
    finally:
        watcher.stop()


def test_skillhub_ux_score_does_not_enter_the_native_dirty_queue(
    tmp_path: Path,
) -> None:
    skill_root = tmp_path / "skills"
    skill_root.mkdir()
    hub_skill = tmp_path / "skillhub" / "external"
    hub_skill.mkdir(parents=True)
    watcher = _make_watcher(tmp_path, skill_root)
    db = tmp_path / "registry.db"
    try:
        watcher._notify_skill_edit_ux_change(hub_skill, db_path=db)
        assert reg.list_skill_edit_dirty(skill_root, db_path=db) == []
    finally:
        watcher.stop()


def test_failed_skill_edit_remains_queued_for_retry(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from xskill.agents import skill_edit_agent

    class _FailingSkillEditAgent:
        def __init__(self, **_kwargs):
            self.next_batch_size = 5

        def maybe_run(self):
            return False

    skill_root = tmp_path / "skills"
    skill_root.mkdir()
    _seed_baby(skill_root, "retry", score=10)
    watcher = _make_watcher(tmp_path, skill_root)
    monkeypatch.setattr(
        skill_edit_agent,
        "SkillEditAgent",
        _FailingSkillEditAgent,
    )
    try:
        watcher._check_pending_skill_edits()
        watcher._drain_futures(stage="skill_edit")
        first = reg.list_skill_edit_dirty(
            skill_root,
            db_path=tmp_path / "registry.db",
        )
        assert [row["skill"] for row in first] == ["retry"]

        watcher._check_pending_skill_edits()
        watcher._drain_futures(stage="skill_edit")
        second = reg.list_skill_edit_dirty(
            skill_root,
            db_path=tmp_path / "registry.db",
        )
        assert [row["skill"] for row in second] == ["retry"]
    finally:
        watcher.stop()


def test_cancelled_skill_edit_remains_queued(
    tmp_path: Path,
    monkeypatch,
) -> None:
    skill_root = tmp_path / "skills"
    skill_root.mkdir()
    _seed_baby(skill_root, "cancelled", score=10)
    watcher = _make_watcher(tmp_path, skill_root)
    cancelled = Future()
    cancelled.cancel()
    monkeypatch.setattr(
        watcher._pools["edit"],
        "submit",
        lambda *_args, **_kwargs: cancelled,
    )
    try:
        watcher._check_pending_skill_edits()
        watcher._harvest()
        assert [
            row["skill"]
            for row in reg.list_skill_edit_dirty(
                skill_root,
                db_path=tmp_path / "registry.db",
            )
        ] == ["cancelled"]
    finally:
        watcher.stop()


def test_new_watcher_schedules_a_persisted_dirty_skill(
    tmp_path: Path,
    monkeypatch,
) -> None:
    skill_root = tmp_path / "skills"
    skill_root.mkdir()
    skill_path = _seed_baby(skill_root, "restart", score=10)
    db = tmp_path / "registry.db"
    reg.mark_skill_edit_dirty(skill_path, db_path=db)

    watcher = _make_watcher(tmp_path, skill_root)
    submitted: list[str] = []

    def capture_submit(_callable, path, **_kwargs):
        submitted.append(Path(path).name)
        return Future()

    monkeypatch.setattr(watcher._pools["edit"], "submit", capture_submit)
    try:
        watcher._check_pending_skill_edits()
        assert submitted == ["restart"]
    finally:
        watcher.stop()


def test_successful_skill_edit_acknowledges_an_empty_buffer(
    tmp_path: Path,
    monkeypatch,
) -> None:
    skill_root = tmp_path / "skills"
    skill_root.mkdir()
    skill_path = skill_root / "done"
    skill_path.mkdir()
    candidates.save_candidates(skill_path, {"candidates": []})
    watcher = _make_watcher(tmp_path, skill_root)
    reg.mark_skill_edit_dirty(
        skill_path,
        db_path=tmp_path / "registry.db",
    )
    monkeypatch.setattr(
        watcher,
        "_install_skill_to_all_detected",
        lambda _path: None,
    )
    try:
        watcher._on_skill_edit_done(
            (skill_path, True, watcher.skill_edit_batch_size),
        )
        assert reg.list_skill_edit_dirty(
            skill_root,
            db_path=tmp_path / "registry.db",
        ) == []
    finally:
        watcher.stop()


def test_success_ack_does_not_consume_a_concurrent_dirty_generation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    skill_root = tmp_path / "skills"
    skill_root.mkdir()
    skill_path = skill_root / "raced"
    skill_path.mkdir()
    candidates.save_candidates(skill_path, {"candidates": []})
    watcher = _make_watcher(tmp_path, skill_root)
    db = tmp_path / "registry.db"
    reg.mark_skill_edit_dirty(skill_path, db_path=db)
    original_generation = reg.skill_edit_dirty_generation

    def generation_then_concurrent_mark(path, *, db_path=None):
        generation = original_generation(path, db_path=db_path)
        reg.mark_skill_edit_dirty(
            skill_path,
            reason="concurrent_candidate",
            db_path=db,
        )
        return generation

    monkeypatch.setattr(
        reg,
        "skill_edit_dirty_generation",
        generation_then_concurrent_mark,
    )
    monkeypatch.setattr(
        watcher,
        "_install_skill_to_all_detected",
        lambda _path: None,
    )
    try:
        watcher._on_skill_edit_done(
            (skill_path, True, watcher.skill_edit_batch_size),
        )
        rows = reg.list_skill_edit_dirty(skill_root, db_path=db)
        assert [(row["skill"], row["generation"]) for row in rows] == [
            ("raced", 2),
        ]
    finally:
        watcher.stop()
