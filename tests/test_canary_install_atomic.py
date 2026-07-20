"""Canary 安装决策的跨进程原子性与幂等性回归。"""
from __future__ import annotations

import json
import multiprocessing
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from xskill.canary import SessionAssignments, canary_generation
from xskill.ecosystems._history import (
    InstallDecisionCancelled,
    InstallHistory,
    InstallHistoryAppendUncertainError,
    InstallHistoryCorruptError,
    InstallPlan,
    InstallTransactionRequest,
)
from xskill.ecosystems.claude_code import (
    CCSessionIngester,
    ensure_claude_code_install,
    ingest_claude_code_sessions,
    install_to_claude_code,
)
from xskill.skill.git import run_git


class _RecordingInstaller:
    def __init__(self, installed_sides: list[str]):
        self.installed_sides = installed_sides

    def __call__(self, _skill_path, *, target_root, side):
        self.installed_sides.append(side)
        return Path(target_root) / "installed" / "SKILL.md"


def _apply_process_decision(
    history_path: str,
    target_path: str,
    callback_path: str,
    barrier,
    side: str,
) -> None:
    history = InstallHistory(history_path)
    barrier.wait()

    def install_decision(_context, pending_ids):
        def apply_target():
            with Path(callback_path).open(
                "a",
                encoding="utf-8",
            ) as callback_file:
                callback_file.write(f"{side}\n")
            Path(target_path).write_text(side, encoding="utf-8")

        def rollback_target():
            Path(target_path).unlink(missing_ok=True)

        return InstallPlan(
            side=side,
            sha=f"{side}-sha",
            install_decision_ids=pending_ids,
            apply=apply_target,
            rollback=rollback_target,
        )

    history.transact(
        skill="atomic-skill",
        target="claude_code",
        decision_ids=("session:same-input",),
        operation=install_decision,
    )


def _seed_skill(skill_path: Path) -> None:
    skill_path.mkdir(parents=True)
    run_git(["init"], cwd=str(skill_path))
    run_git(["checkout", "-b", "main"], cwd=str(skill_path))
    run_git(["config", "user.email", "test@example.com"], cwd=str(skill_path))
    run_git(["config", "user.name", "test"], cwd=str(skill_path))
    (skill_path / "SKILL.md").write_text("main-v1", encoding="utf-8")
    run_git(["add", "SKILL.md"], cwd=str(skill_path))
    run_git(["commit", "-m", "main"], cwd=str(skill_path))
    run_git(["checkout", "-b", "staging"], cwd=str(skill_path))
    (skill_path / "SKILL.md").write_text("staging-v2", encoding="utf-8")
    run_git(["add", "SKILL.md"], cwd=str(skill_path))
    run_git(["commit", "-m", "staging"], cwd=str(skill_path))
    run_git(["checkout", "main"], cwd=str(skill_path))
    staging_path = skill_path.parent / ".canary" / skill_path.name
    staging_path.mkdir(parents=True)
    (staging_path / "SKILL.md").write_text(
        "staging-v2",
        encoding="utf-8",
    )


def test_same_decision_is_atomic_across_processes(tmp_path):
    """Barrier 同时放行两个短命进程，同一输入只允许一个安装结果。"""
    process_context = multiprocessing.get_context("spawn")
    barrier = process_context.Barrier(3)
    history_path = tmp_path / "install_history.jsonl"
    target_path = tmp_path / "installed-side"
    callback_path = tmp_path / "callbacks.jsonl"
    processes = [
        process_context.Process(
            target=_apply_process_decision,
            args=(
                str(history_path),
                str(target_path),
                str(callback_path),
                barrier,
                side,
            ),
        )
        for side in ("main", "staging")
    ]
    for process in processes:
        process.start()
    barrier.wait()
    for process in processes:
        process.join(timeout=20)
        assert process.exitcode == 0

    records = InstallHistory(history_path).all_records()
    assert len(records) == 1
    assert records[0]["decision_ids"] == ["session:same-input"]
    assert callback_path.read_text(encoding="utf-8").splitlines() in (
        ["main"],
        ["staging"],
    )
    assert target_path.read_text(encoding="utf-8") == records[0]["side"]


def test_same_session_flip_is_idempotent_with_barrier(tmp_path, monkeypatch):
    """两个 ingester 同时处理同一 sid，只安装一次且历史与最终目标一致。"""
    skill_root = tmp_path / "skill"
    skill_path = skill_root / "atomic-skill"
    _seed_skill(skill_path)
    history_path = tmp_path / "install_history.jsonl"
    history = InstallHistory(history_path)
    history.record(
        skill=skill_path.name,
        side="main",
        sha="main-sha",
        target="claude_code",
    )
    installed_sides: list[str] = []

    monkeypatch.setattr(
        "xskill.ecosystems.claude_code.install_to_claude_code",
        _RecordingInstaller(installed_sides),
    )
    ingesters = [
        CCSessionIngester(
            tmp_path / f"traj-{index}",
            skill_dir=skill_root,
            history_path=history_path,
            target_root=tmp_path,
        )
        for index in range(2)
    ]
    barrier = threading.Barrier(3)

    def flip(ingester):
        barrier.wait()
        ingester._attribute_and_rotate(
            skill_path.name,
            [{
                "session_id": "same-id",
                "session_start_t": float("inf"),
                "used_skill": True,
                "rebridged": False,
            }],
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(flip, ingester) for ingester in ingesters]
        barrier.wait()
        for future in futures:
            future.result(timeout=10)

    assert installed_sides == ["staging"]
    records = history.all_records()
    assert len(records) == 3
    assert records[-2]["action"] == "session_assignment"
    assert records[-2]["side"] == "main"
    assert records[-2]["decision_ids"] == [
        "assignment:same-id",
        "flip:same-id",
    ]
    assert records[-1]["decision_ids"] == ["flip:same-id"]
    assert records[-1]["side"] == "staging"


def test_restart_recovers_bridged_session_without_history_receipt(
    tmp_path,
    monkeypatch,
):
    """bridge 落盘后进程被杀，重启必须补 receipt/header/secondary/一次 flip。"""
    skill_root = tmp_path / "skill"
    skill_path = skill_root / "atomic-skill"
    _seed_skill(skill_path)
    history_path = tmp_path / "history.jsonl"
    history = InstallHistory(history_path)
    history.record(
        skill=skill_path.name,
        side="main",
        sha="main-sha",
        target="claude_code",
        t=1.0,
    )
    home_root = tmp_path / "home"
    project_dir = home_root / ".claude" / "projects" / "project"
    project_dir.mkdir(parents=True)
    source_path = project_dir / "killed-session.jsonl"
    source_path.write_text(
        "\n".join((
            json.dumps({
                "type": "user",
                "sessionId": "killed-session",
                "timestamp": "2026-07-17T00:00:00Z",
                "message": {"content": "use the skill"},
            }),
            json.dumps({
                "type": "assistant",
                "sessionId": "killed-session",
                "message": {"content": [{
                    "type": "tool_use",
                    "id": "tool-1",
                    "name": "Skill",
                    "input": {"skill": skill_path.name},
                }]},
            }),
            json.dumps({"type": "last-prompt"}),
        )) + "\n",
        encoding="utf-8",
    )
    old_time = source_path.stat().st_mtime - 300
    os.utime(source_path, (old_time, old_time))
    trajectory_dir = tmp_path / "trajectories"
    crashed_ingester = CCSessionIngester(
        trajectory_dir,
        home_root=home_root,
        skill_dir=skill_root,
        history_path=history_path,
        target_root=home_root,
    )

    def persist_intent(source, session_id, content, session_start_t):
        crashed_ingester._write_receipt_intent(
            source,
            session_id,
            skill_name=skill_path.name,
            used_skill=True,
            generation=canary_generation(skill_path),
            content=content,
            session_start_t=session_start_t,
        )

    bridge_result = ingest_claude_code_sessions(
        trajectory_dir,
        home_root=home_root,
        seen_sessions=set(),
        before_bridge=persist_intent,
    )
    assert len(bridge_result) == 1
    assert not any(
        record.get("action") == "session_assignment"
        for record in history.all_records()
    )
    installed_sides: list[str] = []
    monkeypatch.setattr(
        "xskill.ecosystems.claude_code.install_to_claude_code",
        _RecordingInstaller(installed_sides),
    )
    assignments_path = tmp_path / "assignments.jsonl"
    ingester = CCSessionIngester(
        trajectory_dir,
        home_root=home_root,
        skill_dir=skill_root,
        history_path=history_path,
        assignments_path=assignments_path,
        target_root=home_root,
    )

    recovered = ingester.run_once()

    assert len(recovered) == 1
    assert ingester.assignments.get("killed-session") is not None
    assert Path(recovered[0]["path"]).read_text(encoding="utf-8").startswith(
        "<!-- xskill:skill=atomic-skill side=main sha=main-sha -->"
    )
    assignment_records = [
        record
        for record in history.all_records()
        if record.get("action") == "session_assignment"
    ]
    assert len(assignment_records) == 1
    assert installed_sides == ["staging"]

    restarted = CCSessionIngester(
        trajectory_dir,
        home_root=home_root,
        skill_dir=skill_root,
        history_path=history_path,
        assignments_path=assignments_path,
        target_root=home_root,
    )
    assert restarted.run_once() == []
    assert installed_sides == ["staging"]


def test_seen_session_without_receipt_intent_is_not_recovered(
    tmp_path,
    monkeypatch,
):
    """无 canary 时正常 bridge 的 seen session 不能被误判为 kill 窗口。"""
    skill_root = tmp_path / "skill"
    skill_path = skill_root / "atomic-skill"
    _seed_skill(skill_path)
    history_path = tmp_path / "history.jsonl"
    InstallHistory(history_path).record(
        skill=skill_path.name,
        side="main",
        sha="main-sha",
        target="claude_code",
        t=1.0,
    )
    home_root = tmp_path / "home"
    project_dir = home_root / ".claude" / "projects" / "project"
    project_dir.mkdir(parents=True)
    source_path = project_dir / "normal-session.jsonl"
    source_path.write_text(
        json.dumps({
            "type": "user",
            "sessionId": "normal-session",
            "timestamp": "2026-07-17T00:00:00Z",
            "message": {"content": "normal bridge"},
        }) + "\n" + json.dumps({"type": "last-prompt"}) + "\n",
        encoding="utf-8",
    )
    old_time = source_path.stat().st_mtime - 300
    os.utime(source_path, (old_time, old_time))
    trajectory_dir = tmp_path / "trajectories"
    assert len(ingest_claude_code_sessions(
        trajectory_dir,
        home_root=home_root,
        seen_sessions=set(),
    )) == 1
    installed_sides: list[str] = []
    monkeypatch.setattr(
        "xskill.ecosystems.claude_code.install_to_claude_code",
        _RecordingInstaller(installed_sides),
    )

    restarted = CCSessionIngester(
        trajectory_dir,
        home_root=home_root,
        skill_dir=skill_root,
        history_path=history_path,
        target_root=home_root,
    )

    assert restarted.run_once() == []
    assert restarted._receipt_intents == {}
    assert installed_sides == []
    assert not any(
        record.get("action") == "session_assignment"
        for record in InstallHistory(history_path).all_records()
    )


@pytest.mark.parametrize("terminal_state", ["removed", "new-generation"])
def test_receipt_intent_never_flips_after_generation_ends(
    tmp_path,
    terminal_state,
):
    """bridge 后宕机再终态/换代，只补显式取消 receipt，绝不翻新代。"""
    skill_root = tmp_path / "skill"
    skill_path = skill_root / "atomic-skill"
    _seed_skill(skill_path)
    history_path = tmp_path / "history.jsonl"
    history = InstallHistory(history_path)
    history.record(
        skill=skill_path.name,
        side="main",
        sha="main-sha",
        target="claude_code",
        t=1.0,
    )
    home_root = tmp_path / "home"
    project_dir = home_root / ".claude" / "projects" / "project"
    project_dir.mkdir(parents=True)
    source_path = project_dir / "generation-session.jsonl"
    source_path.write_text(
        json.dumps({
            "type": "user",
            "sessionId": "generation-session",
            "timestamp": "2026-07-17T00:00:00Z",
            "message": {"content": "use skill"},
        }) + "\n" + json.dumps({
            "type": "assistant",
            "sessionId": "generation-session",
            "message": {"content": [{
                "type": "tool_use",
                "name": "Skill",
                "input": {"skill": skill_path.name},
            }]},
        }) + "\n" + json.dumps({"type": "last-prompt"}) + "\n",
        encoding="utf-8",
    )
    old_time = source_path.stat().st_mtime - 300
    os.utime(source_path, (old_time, old_time))
    trajectory_dir = tmp_path / "trajectories"
    crashed = CCSessionIngester(
        trajectory_dir,
        home_root=home_root,
        skill_dir=skill_root,
        history_path=history_path,
        target_root=home_root,
    )
    original_generation = canary_generation(skill_path)

    def before_bridge(source, session_id, content, session_start_t):
        crashed._write_receipt_intent(
            source,
            session_id,
            skill_name=skill_path.name,
            used_skill=True,
            generation=original_generation,
            content=content,
            session_start_t=session_start_t,
        )

    def after_bridge(
        result,
        _source,
        session_id,
        _content,
        _session_start_t,
    ):
        crashed._mark_receipt_intent_bridged(
            result,
            session_id,
        )

    assert len(ingest_claude_code_sessions(
        trajectory_dir,
        home_root=home_root,
        seen_sessions=set(),
        before_bridge=before_bridge,
        after_bridge=after_bridge,
    )) == 1
    staging_markdown = (
        skill_root / ".canary" / skill_path.name / "SKILL.md"
    )
    if terminal_state == "removed":
        staging_markdown.unlink()
    else:
        run_git(["checkout", "staging"], cwd=str(skill_path))
        (skill_path / "SKILL.md").write_text(
            "staging-v3",
            encoding="utf-8",
        )
        run_git(["add", "SKILL.md"], cwd=str(skill_path))
        run_git(["commit", "-m", "new staging generation"], cwd=str(skill_path))
        run_git(["checkout", "main"], cwd=str(skill_path))
        staging_markdown.write_text("staging-v3", encoding="utf-8")
    assert (
        terminal_state == "removed"
        or canary_generation(skill_path) != original_generation
    )

    restarted = CCSessionIngester(
        trajectory_dir,
        home_root=home_root,
        skill_dir=skill_root,
        history_path=history_path,
        target_root=home_root,
    )

    assert restarted.run_once() == []
    cancellation = history.index().session_assignment(
        skill_path.name,
        "claude_code",
        "generation-session",
    )
    assert cancellation is not None
    assert cancellation["flip_cancelled"] is True
    assert cancellation["generation"] == original_generation
    assert restarted._receipt_intents == {}
    assert [
        record
        for record in history.all_records()
        if record.get("action", "install") == "install"
    ] == [history.all_records()[0]]


def test_generation_read_failure_keeps_receipt_intent_for_retry(
    tmp_path,
    monkeypatch,
):
    """瞬时 Git/IO 失败必须 fail loud 并保留 intent，不能永久误取消。"""
    skill_root = tmp_path / "skill"
    skill_path = skill_root / "atomic-skill"
    _seed_skill(skill_path)
    history_path = tmp_path / "history.jsonl"
    history = InstallHistory(history_path)
    history.record(
        skill=skill_path.name,
        side="main",
        sha="main-sha",
        target="claude_code",
        t=1.0,
    )
    source_path = tmp_path / "source.jsonl"
    content = json.dumps({
        "type": "user",
        "timestamp": "2026-07-17T00:00:00Z",
    }) + "\n"
    source_path.write_text(content, encoding="utf-8")
    ingester = CCSessionIngester(
        tmp_path / "trajectories",
        skill_dir=skill_root,
        history_path=history_path,
    )
    ingester._write_receipt_intent(
        source_path,
        "retry-session",
        skill_name=skill_path.name,
        used_skill=False,
        generation=canary_generation(skill_path),
        content=content,
        session_start_t=2.0,
    )

    def fail_generation(_skill_path):
        raise OSError("injected transient git failure")

    monkeypatch.setattr(
        "xskill.ecosystems.claude_code._strict_canary_generation",
        fail_generation,
    )

    with pytest.raises(OSError, match="transient git failure"):
        ingester._cancel_stale_receipt_intents()
    assert "retry-session" in ingester._receipt_intents
    assert ingester._receipt_intent_path("retry-session").is_file()
    assert not any(
        record.get("action") == "session_receipt_cancelled"
        for record in history.all_records()
    )


def test_missing_session_timestamp_aborts_bridge_without_seen(tmp_path):
    """无法归因开始时间时必须中止 bridge，保留源供修复后重试。"""
    skill_root = tmp_path / "skill"
    skill_path = skill_root / "atomic-skill"
    _seed_skill(skill_path)
    history_path = tmp_path / "history.jsonl"
    InstallHistory(history_path).record(
        skill=skill_path.name,
        side="main",
        sha="main-sha",
        target="claude_code",
        t=1.0,
    )
    home_root = tmp_path / "home"
    project_dir = home_root / ".claude" / "projects" / "project"
    project_dir.mkdir(parents=True)
    source_path = project_dir / "missing-timestamp.jsonl"
    source_path.write_text(
        json.dumps({
            "type": "user",
            "sessionId": "missing-timestamp",
            "message": {"content": "no timestamp"},
        }) + "\n" + json.dumps({"type": "last-prompt"}) + "\n",
        encoding="utf-8",
    )
    old_time = source_path.stat().st_mtime - 300
    os.utime(source_path, (old_time, old_time))
    trajectory_dir = tmp_path / "trajectories"
    ingester = CCSessionIngester(
        trajectory_dir,
        home_root=home_root,
        skill_dir=skill_root,
        history_path=history_path,
    )

    with pytest.raises(RuntimeError, match="requires a session timestamp"):
        ingester.run_once()
    assert "missing-timestamp" not in ingester._seen
    assert list(trajectory_dir.glob("traj_*.md")) == []


def test_canary_bridge_reads_large_source_snapshot_once(
    tmp_path,
    monkeypatch,
):
    """start/used/digest 必须复用一次 content，不能反复全量 read_text。"""
    skill_root = tmp_path / "skill"
    skill_path = skill_root / "atomic-skill"
    _seed_skill(skill_path)
    history_path = tmp_path / "history.jsonl"
    InstallHistory(history_path).record(
        skill=skill_path.name,
        side="main",
        sha="main-sha",
        target="claude_code",
        t=1.0,
    )
    home_root = tmp_path / "home"
    project_dir = home_root / ".claude" / "projects" / "project"
    project_dir.mkdir(parents=True)
    source_path = project_dir / "single-read.jsonl"
    source_path.write_text(
        json.dumps({
            "type": "user",
            "sessionId": "single-read",
            "timestamp": "2026-07-17T00:00:00Z",
            "message": {"content": "x" * 100_000},
        }) + "\n" + json.dumps({"type": "last-prompt"}) + "\n",
        encoding="utf-8",
    )
    old_time = source_path.stat().st_mtime - 300
    os.utime(source_path, (old_time, old_time))
    source_read_count = 0
    original_read_text = Path.read_text

    def count_source_read(path, *args, **kwargs):
        nonlocal source_read_count
        if path == source_path:
            source_read_count += 1
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", count_source_read)
    ingester = CCSessionIngester(
        tmp_path / "trajectories",
        home_root=home_root,
        skill_dir=skill_root,
        history_path=history_path,
    )

    assert len(ingester.run_once()) == 1
    assert source_read_count == 1


def test_session_before_first_install_gets_non_attributable_receipt(
    tmp_path,
):
    """早于首条 install 的 session 显式取消并清 intent，不能永久 rebridge。"""
    skill_root = tmp_path / "skill"
    skill_path = skill_root / "atomic-skill"
    _seed_skill(skill_path)
    history_path = tmp_path / "history.jsonl"
    history = InstallHistory(history_path)
    history.record(
        skill=skill_path.name,
        side="main",
        sha="main-sha",
        target="claude_code",
        t=2_000_000_000.0,
    )
    home_root = tmp_path / "home"
    project_dir = home_root / ".claude" / "projects" / "project"
    project_dir.mkdir(parents=True)
    source_path = project_dir / "early-session.jsonl"
    source_path.write_text(
        json.dumps({
            "type": "user",
            "sessionId": "early-session",
            "timestamp": "2020-01-01T00:00:00Z",
            "message": {"content": "too early"},
        }) + "\n" + json.dumps({"type": "last-prompt"}) + "\n",
        encoding="utf-8",
    )
    old_time = source_path.stat().st_mtime - 300
    os.utime(source_path, (old_time, old_time))
    ingester = CCSessionIngester(
        tmp_path / "trajectories",
        home_root=home_root,
        skill_dir=skill_root,
        history_path=history_path,
    )

    assert len(ingester.run_once()) == 1
    cancellation = next(
        record
        for record in history.all_records()
        if record.get("action") == "session_receipt_cancelled"
    )
    assert cancellation["reason"] == "no_install_at_session_start"
    assert ingester._receipt_intents == {}
    assert ingester.run_once() == []


@pytest.mark.parametrize("used_skill", [False, True])
def test_growth_kill_before_submit_keeps_versioned_intent(
    tmp_path,
    monkeypatch,
    used_skill,
):
    """同 used 状态续写在 submit 前宕机，旧 assignment 不能清新 source intent。"""
    skill_root = tmp_path / "skill"
    skill_path = skill_root / "atomic-skill"
    _seed_skill(skill_path)
    history_path = tmp_path / "history.jsonl"
    history = InstallHistory(history_path)
    history.record(
        skill=skill_path.name,
        side="main",
        sha="main-sha",
        target="claude_code",
        t=1.0,
    )
    home_root = tmp_path / "home"
    project_dir = home_root / ".claude" / "projects" / "project"
    project_dir.mkdir(parents=True)
    source_path = project_dir / "versioned-growth.jsonl"

    def source_content(marker):
        events = [{
            "type": "user",
            "sessionId": "versioned-growth",
            "timestamp": "2026-07-17T00:00:00Z",
            "message": {"content": marker},
        }]
        if used_skill:
            events.append({
                "type": "assistant",
                "sessionId": "versioned-growth",
                "message": {"content": [{
                    "type": "tool_use",
                    "name": "Skill",
                    "input": {"skill": skill_path.name},
                }]},
            })
        events.append({"type": "last-prompt"})
        return "\n".join(json.dumps(event) for event in events) + "\n"

    source_path.write_text(source_content("FIRST"), encoding="utf-8")
    old_time = source_path.stat().st_mtime - 300
    os.utime(source_path, (old_time, old_time))
    installed_sides: list[str] = []
    monkeypatch.setattr(
        "xskill.ecosystems.claude_code.install_to_claude_code",
        _RecordingInstaller(installed_sides),
    )
    trajectory_dir = tmp_path / "trajectories"
    assignments_path = tmp_path / "assignments.jsonl"
    ingester = CCSessionIngester(
        trajectory_dir,
        home_root=home_root,
        skill_dir=skill_root,
        history_path=history_path,
        assignments_path=assignments_path,
        target_root=home_root,
    )
    assert len(ingester.run_once()) == 1
    installs_after_first = list(installed_sides)
    source_path.write_text(source_content("SECOND"), encoding="utf-8")
    old_time = source_path.stat().st_mtime - 300
    os.utime(source_path, (old_time, old_time))
    generation = canary_generation(skill_path)

    def persist_then_kill(
        source,
        session_id,
        content,
        session_start_t,
    ):
        ingester._write_receipt_intent(
            source,
            session_id,
            skill_name=skill_path.name,
            used_skill=used_skill,
            generation=generation,
            content=content,
            session_start_t=session_start_t,
        )
        raise RuntimeError("injected kill before submit")

    with pytest.raises(RuntimeError, match="injected kill"):
        ingest_claude_code_sessions(
            trajectory_dir,
            home_root=home_root,
            seen_sessions=ingester._seen,
            candidate_paths=(source_path,),
            bridged_markdown_index=ingester._bridged_markdown_index,
            force_rebridge_sessions={"versioned-growth"},
            before_bridge=persist_then_kill,
        )

    restarted = CCSessionIngester(
        trajectory_dir,
        home_root=home_root,
        skill_dir=skill_root,
        history_path=history_path,
        assignments_path=assignments_path,
        target_root=home_root,
    )
    recovered = restarted.run_once()

    assert len(recovered) == 1
    assert "SECOND" in Path(recovered[0]["path"]).read_text(encoding="utf-8")
    assignments = [
        record
        for record in history.all_records()
        if record.get("action") == "session_assignment"
        and record.get("session_id") == "versioned-growth"
    ]
    assert len(assignments) == 2
    assert assignments[0]["source_digest"] != assignments[1]["source_digest"]
    assert [record["used_skill"] for record in assignments] == [
        used_skill,
        used_skill,
    ]
    assert installed_sides == installs_after_first
    assert restarted._receipt_intents == {}


def test_source_growth_false_to_true_upgrades_receipt_and_flips_once(
    tmp_path,
    monkeypatch,
):
    """长 session 后续首次使用 skill 时，false receipt 必须原子升级并翻一次。"""
    skill_root = tmp_path / "skill"
    skill_path = skill_root / "atomic-skill"
    _seed_skill(skill_path)
    history_path = tmp_path / "history.jsonl"
    InstallHistory(history_path).record(
        skill=skill_path.name,
        side="main",
        sha="main-sha",
        target="claude_code",
        t=1.0,
    )
    home_root = tmp_path / "home"
    project_dir = home_root / ".claude" / "projects" / "project"
    project_dir.mkdir(parents=True)
    source_path = project_dir / "growing-session.jsonl"
    source_path.write_text(
        json.dumps({
            "type": "user",
            "sessionId": "growing-session",
            "timestamp": "2026-07-17T00:00:00Z",
            "message": {"content": "first"},
        }) + "\n" + json.dumps({"type": "last-prompt"}) + "\n",
        encoding="utf-8",
    )
    old_time = source_path.stat().st_mtime - 300
    os.utime(source_path, (old_time, old_time))
    installed_sides: list[str] = []
    monkeypatch.setattr(
        "xskill.ecosystems.claude_code.install_to_claude_code",
        _RecordingInstaller(installed_sides),
    )
    ingester = CCSessionIngester(
        tmp_path / "trajectories",
        home_root=home_root,
        skill_dir=skill_root,
        history_path=history_path,
        assignments_path=tmp_path / "assignments.jsonl",
        target_root=home_root,
    )

    first = ingester.run_once()
    assert len(first) == 1
    assert ingester.assignments.get("growing-session")["used_skill"] is False
    assert installed_sides == []

    with source_path.open("a", encoding="utf-8") as source_file:
        source_file.write(
            json.dumps({
                "type": "assistant",
                "sessionId": "growing-session",
                "message": {"content": [{
                    "type": "tool_use",
                    "name": "Skill",
                    "input": {"skill": skill_path.name},
                }]},
            }) + "\n" + json.dumps({"type": "last-prompt"}) + "\n"
        )
    old_time = source_path.stat().st_mtime - 300
    os.utime(source_path, (old_time, old_time))
    for project_directory in ingester._source_directory_rescan_deadlines:
        ingester._source_directory_rescan_deadlines[project_directory] = 0.0

    second = ingester.run_once()

    assert len(second) == 1
    assert second[0]["xskill_used_skill"] is True
    assert installed_sides == ["staging"]
    assert ingester.assignments.get("growing-session")["used_skill"] is True
    assignments = [
        record
        for record in InstallHistory(history_path).all_records()
        if record.get("action") == "session_assignment"
        and record.get("session_id") == "growing-session"
    ]
    assert [record["used_skill"] for record in assignments] == [False, True]
    assert ingester.run_once() == []
    assert installed_sides == ["staging"]


def test_twelve_session_boundaries_use_real_alternating_installs(
    tmp_path,
    monkeypatch,
):
    """轻量轮询逐 session 完成时，12 条真实安装近似 1:1。"""
    skill_root = tmp_path / "skill"
    skill_path = skill_root / "alternating-skill"
    _seed_skill(skill_path)
    history_path = tmp_path / "install_history.jsonl"
    history = InstallHistory(history_path)
    history.record(
        skill=skill_path.name,
        side="main",
        sha="main-sha",
        target="claude_code",
    )
    installed_sides: list[str] = []
    monkeypatch.setattr(
        "xskill.ecosystems.claude_code.install_to_claude_code",
        _RecordingInstaller(installed_sides),
    )
    ingester = CCSessionIngester(
        tmp_path / "traj",
        skill_dir=skill_root,
        history_path=history_path,
        target_root=tmp_path,
    )

    assignment_sides = []
    for session_index in range(12):
        session_id = f"session-{session_index:02d}"
        assignments = ingester._attribute_and_rotate(
            skill_path.name,
            [{
                "session_id": session_id,
                "session_start_t": float("inf"),
                "used_skill": True,
                "rebridged": False,
            }],
        )
        assignment_sides.append(assignments[session_id]["side"])

    assert assignment_sides.count("main") == 6
    assert assignment_sides.count("staging") == 6
    assert installed_sides == [
        "staging" if index % 2 == 0 else "main"
        for index in range(12)
    ]


def test_six_even_batches_keep_real_attribution_balanced(
    tmp_path,
    monkeypatch,
):
    """同批已完成 session 同 side；批尾切换让连续偶数批真实交替。"""
    skill_root = tmp_path / "skill"
    skill_path = skill_root / "even-batch-skill"
    _seed_skill(skill_path)
    history_path = tmp_path / "install_history.jsonl"
    history = InstallHistory(history_path)
    history.record(
        skill=skill_path.name,
        side="staging",
        sha="staging-sha",
        target="claude_code",
    )
    installed_sides: list[str] = []
    monkeypatch.setattr(
        "xskill.ecosystems.claude_code.install_to_claude_code",
        _RecordingInstaller(installed_sides),
    )
    ingester = CCSessionIngester(
        tmp_path / "traj",
        skill_dir=skill_root,
        history_path=history_path,
        target_root=tmp_path,
    )

    assignment_sides = []
    for batch_index in range(6):
        observations = [
            {
                "session_id": f"session-{batch_index}-{offset}",
                "session_start_t": float("inf"),
                "used_skill": True,
                "rebridged": False,
            }
            for offset in range(2)
        ]
        assignments = ingester._attribute_and_rotate(
            skill_path.name,
            observations,
        )
        batch_sides = {
            assignments[observation["session_id"]]["side"]
            for observation in observations
        }
        assert len(batch_sides) == 1
        assignment_sides.extend(
            [next(iter(batch_sides))] * len(observations)
        )

    assert assignment_sides.count("main") == 6
    assert assignment_sides.count("staging") == 6
    assert installed_sides == [
        "main" if index % 2 == 0 else "staging"
        for index in range(6)
    ]


def test_flip_batch_records_receipts_without_repeated_full_scans(
    tmp_path,
    monkeypatch,
):
    """批内按真实历史归因；批尾只翻一次，切换仅影响下一批。"""
    skill_root = tmp_path / "skill"
    skill_path = skill_root / "batch-skill"
    _seed_skill(skill_path)
    history_path = tmp_path / "install_history.jsonl"
    history = InstallHistory(history_path)
    history.record(
        skill=skill_path.name,
        side="main",
        sha="main-sha",
        target="claude_code",
    )
    installed_sides: list[str] = []
    monkeypatch.setattr(
        "xskill.ecosystems.claude_code.install_to_claude_code",
        _RecordingInstaller(installed_sides),
    )
    ingester = CCSessionIngester(
        tmp_path / "traj",
        skill_dir=skill_root,
        history_path=history_path,
        target_root=tmp_path,
    )

    first_assignments = ingester._attribute_and_rotate(
        skill_path.name,
        [
            {
                "session_id": session_id,
                "session_start_t": float("inf"),
                "used_skill": True,
                "rebridged": False,
            }
            for session_id in ("one", "two")
        ],
    )
    assert {
        first_assignments[session_id]["side"]
        for session_id in ("one", "two")
    } == {"main"}
    assert installed_sides == ["staging"]
    assert history.lookup(
        float("inf"),
        skill=skill_path.name,
        target="claude_code",
    )["side"] == "staging"

    next_assignments = ingester._attribute_and_rotate(
        skill_path.name,
        [{
            "session_id": "three",
            "session_start_t": float("inf"),
            "used_skill": True,
            "rebridged": False,
        }],
    )
    assert next_assignments["three"]["side"] == "staging"
    assert installed_sides == ["staging", "main"]
    assert history.lookup(
        float("inf"),
        skill=skill_path.name,
        target="claude_code",
    )["side"] == "main"


def test_ensure_preserves_staging_then_converges_to_promoted_main(tmp_path):
    """启动 ensure 不降回 main；staging 消失后只安装已晋升的新 main。"""
    skill_root = tmp_path / "skill"
    skill_path = skill_root / "promotion-skill"
    _seed_skill(skill_path)
    home_root = tmp_path / "home"
    history = InstallHistory(tmp_path / "install_history.jsonl")
    install_to_claude_code(
        skill_path,
        target_root=home_root,
        side="staging",
    )
    history.record(
        skill=skill_path.name,
        side="staging",
        sha=run_git(
            ["rev-parse", "staging"],
            cwd=str(skill_path),
        )[1].strip(),
        target="claude_code",
    )

    ensure_claude_code_install(history, skill_path, target_root=home_root)
    assert history.count_by_side(
        skill=skill_path.name,
        target="claude_code",
    ) == {"main": 0, "staging": 1}
    installed_path = home_root / ".claude" / "skills" / skill_path.name
    assert installed_path.resolve() == (
        skill_path.parent / ".canary" / skill_path.name
    ).resolve()

    (skill_path / "SKILL.md").write_text("main-v2-promoted", encoding="utf-8")
    staging_path = skill_path.parent / ".canary" / skill_path.name
    for child in staging_path.iterdir():
        child.unlink()
    staging_path.rmdir()
    ensure_claude_code_install(history, skill_path, target_root=home_root)

    assert installed_path.resolve() == skill_path.resolve()
    assert (installed_path / "SKILL.md").read_text(
        encoding="utf-8",
    ) == "main-v2-promoted"
    assert history.lookup(
        float("inf"),
        skill=skill_path.name,
        target="claude_code",
    )["side"] == "main"


def test_batch_records_each_actual_side_with_one_history_read(
    tmp_path,
    monkeypatch,
):
    """H 条历史 + S 条 session 只解析一次，每条 session 独立归因。"""
    skill_root = tmp_path / "skill"
    skill_path = skill_root / "indexed-skill"
    _seed_skill(skill_path)
    history_path = tmp_path / "install_history.jsonl"
    history = InstallHistory(history_path)
    for index in range(100):
        history.record(
            skill=skill_path.name,
            side="main" if index % 2 == 0 else "staging",
            sha=f"sha-{index}",
            target="claude_code",
            t=float(index + 1),
        )
    installed_sides: list[str] = []
    monkeypatch.setattr(
        "xskill.ecosystems.claude_code.install_to_claude_code",
        _RecordingInstaller(installed_sides),
    )
    ingester = CCSessionIngester(
        tmp_path / "traj",
        skill_dir=skill_root,
        history_path=history_path,
        target_root=tmp_path,
    )
    observations = [
        {
            "session_id": f"session-{index}",
            "session_start_t": float(index + 1),
            "used_skill": True,
            "rebridged": False,
        }
        for index in range(50)
    ]

    before = ingester.history.read_count
    assignments = ingester._attribute_and_rotate(
        skill_path.name,
        observations,
    )

    assert ingester.history.read_count - before == 1
    assert len(assignments) == 50
    assert assignments["session-0"]["side"] == "main"
    assert assignments["session-1"]["side"] == "staging"
    assert installed_sides == ["main"]
    session_records = [
        record
        for record in history.all_records()
        if record.get("action") == "session_assignment"
    ]
    assert len(session_records) == 50


def test_append_order_defines_current_when_clock_moves_backward(tmp_path):
    """current 只看追加序号；wall-clock 只用于历史 session 归因。"""
    history = InstallHistory(tmp_path / "history.jsonl")
    history.record(
        skill="clock-skill",
        target="claude_code",
        side="main",
        sha="main",
        t=200.0,
    )
    history.record(
        skill="clock-skill",
        target="claude_code",
        side="staging",
        sha="staging",
        t=100.0,
    )

    index = history.index()
    assert index.latest("clock-skill", "claude_code")["side"] == "staging"
    assert index.lookup_at(
        250.0,
        skill="clock-skill",
        target="claude_code",
    )["side"] == "main"


def test_append_failure_rolls_back_physical_target(tmp_path, monkeypatch):
    """callback 成功但 history append 失败时，不留下虚假成功或新目标。"""
    history = InstallHistory(tmp_path / "history.jsonl")
    history.record(
        skill="failure-skill",
        target="claude_code",
        side="main",
        sha="main",
    )
    target = tmp_path / "target"
    target.write_text("main", encoding="utf-8")

    def prepare(_context, pending_ids):
        def apply_staging():
            target.write_text("staging", encoding="utf-8")

        def rollback_main():
            target.write_text("main", encoding="utf-8")

        return InstallPlan(
            side="staging",
            sha="staging",
            install_decision_ids=pending_ids,
            apply=apply_staging,
            rollback=rollback_main,
        )

    def fail_append(*_args, **_kwargs):
        raise OSError("injected append failure")

    monkeypatch.setattr(history, "_append_records", fail_append)
    with pytest.raises(OSError, match="injected append failure"):
        history.transact(
            skill="failure-skill",
            target="claude_code",
            decision_ids=("window:1",),
            operation=prepare,
        )

    assert target.read_text(encoding="utf-8") == "main"
    assert len(InstallHistory(history.path).all_records()) == 1
    assert not history._recovery_path(
        "failure-skill",
        "claude_code",
    ).exists()


def test_callback_failure_has_no_success_and_same_decision_replays(tmp_path):
    """物理 callback 失败不消费 decision；修复后同一输入可以安全重放。"""
    history = InstallHistory(tmp_path / "history.jsonl")
    history.record(
        skill="callback-skill",
        target="claude_code",
        side="main",
        sha="main",
    )
    target = tmp_path / "target"
    target.write_text("main", encoding="utf-8")

    def failing_plan(_context, pending_ids):
        def fail_install():
            raise OSError("injected callback failure")

        def restore_main():
            target.write_text("main", encoding="utf-8")

        return InstallPlan(
            side="staging",
            sha="staging",
            install_decision_ids=pending_ids,
            apply=fail_install,
            rollback=restore_main,
        )

    with pytest.raises(OSError, match="injected callback failure"):
        history.transact(
            skill="callback-skill",
            target="claude_code",
            decision_ids=("session:replay",),
            operation=failing_plan,
        )
    assert len(history.all_records()) == 1

    def succeeding_plan(_context, pending_ids):
        def install_staging():
            target.write_text("staging", encoding="utf-8")

        return InstallPlan(
            side="staging",
            sha="staging",
            install_decision_ids=pending_ids,
            apply=install_staging,
        )

    result = history.transact(
        skill="callback-skill",
        target="claude_code",
        decision_ids=("session:replay",),
        operation=succeeding_plan,
    )
    assert result.current["side"] == "staging"
    assert target.read_text(encoding="utf-8") == "staging"
    assert len(history.all_records()) == 2


def test_append_and_rollback_failure_leaves_recoverable_transaction(
    tmp_path,
    monkeypatch,
):
    """无法补偿时保留可恢复事务，且 history 不声称成功。"""
    history = InstallHistory(tmp_path / "history.jsonl")
    history.record(
        skill="recovery-skill",
        target="claude_code",
        side="main",
        sha="main",
    )
    target = tmp_path / "target"
    target.write_text("main", encoding="utf-8")

    def prepare(_context, pending_ids):
        def apply_staging():
            target.write_text("staging", encoding="utf-8")

        def fail_rollback():
            raise OSError("injected rollback failure")

        return InstallPlan(
            side="staging",
            sha="staging",
            install_decision_ids=pending_ids,
            apply=apply_staging,
            rollback=fail_rollback,
        )

    def fail_append(*_args, **_kwargs):
        raise OSError("injected append failure")

    monkeypatch.setattr(history, "_append_records", fail_append)
    with pytest.raises(OSError, match="injected append failure"):
        history.transact(
            skill="recovery-skill",
            target="claude_code",
            decision_ids=("window:1",),
            operation=prepare,
        )

    assert target.read_text(encoding="utf-8") == "staging"
    recovery = history._read_recovery(
        "recovery-skill",
        "claude_code",
    )
    assert recovery is not None
    assert recovery["side"] == "staging"
    assert len(InstallHistory(history.path).all_records()) == 1


def test_uncertain_append_keeps_new_target_and_recovery(tmp_path, monkeypatch):
    """write/fsync 结果不确定时禁止回滚，避免 success 行与物理 side 相反。"""
    history = InstallHistory(tmp_path / "history.jsonl")
    history.record(
        skill="uncertain-skill",
        target="claude_code",
        side="main",
        sha="main",
    )
    target = tmp_path / "target"
    target.write_text("main", encoding="utf-8")
    rollback_called = []

    def prepare(_context, pending_ids):
        def install_staging():
            target.write_text("staging", encoding="utf-8")

        def rollback_main():
            rollback_called.append(True)
            target.write_text("main", encoding="utf-8")

        return InstallPlan(
            side="staging",
            sha="staging",
            install_decision_ids=pending_ids,
            apply=install_staging,
            rollback=rollback_main,
        )

    def uncertain_append(*_args, **_kwargs):
        raise InstallHistoryAppendUncertainError("injected uncertain append")

    monkeypatch.setattr(history, "_append_records", uncertain_append)
    with pytest.raises(
        InstallHistoryAppendUncertainError,
        match="injected uncertain append",
    ):
        history.transact(
            skill="uncertain-skill",
            target="claude_code",
            decision_ids=("window:1",),
            operation=prepare,
        )
    assert target.read_text(encoding="utf-8") == "staging"
    assert rollback_called == []
    assert history._read_recovery(
        "uncertain-skill",
        "claude_code",
    )["side"] == "staging"


def test_recovery_restores_all_receipts_after_partial_append(
    tmp_path,
    monkeypatch,
):
    """中断追加只补缺失原记录，重复 session 不再执行 operation。"""
    history = InstallHistory(tmp_path / "history.jsonl")
    history.record(
        skill="recovery-skill",
        target="claude_code",
        side="main",
        sha="main-sha",
    )
    installed_state = {
        "side": "main",
        "sha": "main-sha",
        "generation": "generation-1",
    }

    def prepare(_context, pending_ids):
        def install_staging():
            installed_state.update(
                side="staging",
                sha="staging-sha",
                generation="generation-1",
            )

        return InstallPlan(
            side="staging",
            sha="staging-sha",
            generation="generation-1",
            records=[{
                "action": "session_assignment",
                "session_id": "session-1",
                "side": "main",
                "sha": "main-sha",
                "decision_ids": [
                    "assignment:session-1",
                    "flip:session-1",
                ],
            }],
            install_decision_ids=pending_ids,
            apply=install_staging,
        )

    original_append = history._append_records
    append_attempts = 0

    def append_partially(records, *, minimum_sequence=0):
        nonlocal append_attempts
        append_attempts += 1
        record_list = list(records)
        if append_attempts != 1:
            return original_append(
                record_list,
                minimum_sequence=minimum_sequence,
            )
        original_append(
            record_list[:1],
            minimum_sequence=minimum_sequence,
        )
        partial_record_id = record_list[1]["record_id"][:12]
        with history.path.open("ab") as history_file:
            history_file.write(
                f'{{"record_id": "{partial_record_id}'.encode("ascii")
            )
            history_file.flush()
        raise InstallHistoryAppendUncertainError(
            "injected partial append"
        )

    monkeypatch.setattr(history, "_append_records", append_partially)
    with pytest.raises(
        InstallHistoryAppendUncertainError,
        match="injected partial append",
    ):
        history.transact(
            skill="recovery-skill",
            target="claude_code",
            decision_ids=(
                "assignment:session-1",
                "flip:session-1",
            ),
            operation=prepare,
        )

    installed_state.update(
        side="main",
        sha="main-sha",
        generation="generation-1",
    )
    monkeypatch.setattr(history, "_append_records", original_append)
    operation_called = False

    def should_not_run(_context, _pending_ids):
        nonlocal operation_called
        operation_called = True
        return None

    def read_installed_state():
        return (
            installed_state["side"],
            installed_state["sha"],
            installed_state["generation"],
        )

    def recover_install(recovery):
        expected = recovery["expected"]
        installed_state.update(
            side=expected["side"],
            sha=expected["sha"],
            generation=expected["generation"],
        )

    history.transact(
        skill="recovery-skill",
        target="claude_code",
        decision_ids=(
            "assignment:session-1",
            "flip:session-1",
        ),
        operation=should_not_run,
        installed_state_reader=read_installed_state,
        recovery_operation=recover_install,
    )

    assert not operation_called
    assert read_installed_state() == (
        "staging",
        "staging-sha",
        "generation-1",
    )
    records = history.all_records()
    assignment_records = [
        record
        for record in records
        if record.get("action") == "session_assignment"
    ]
    staging_installs = [
        record
        for record in records
        if record.get("action") == "install"
        and record.get("side") == "staging"
    ]
    assert len(assignment_records) == 1
    assert len(staging_installs) == 1
    assert len({
        record["record_id"]
        for record in (*assignment_records, *staging_installs)
    }) == 2
    assert not history.has_pending_recovery(
        "recovery-skill",
        "claude_code",
    )


@pytest.mark.parametrize(
    "cut_mode",
    (
        "first-byte",
        "early-field",
        "record-id",
        "middle",
        "closing-brace",
    ),
)
def test_recovery_accepts_any_matching_eof_record_prefix(
    tmp_path,
    monkeypatch,
    cut_mode,
):
    """Crash 可停在任意字节；完整 journal 应补齐匹配的 EOF 残片。"""
    history = InstallHistory(tmp_path / "history.jsonl")
    original_append = history._append_records

    def interrupted_append(records, *, minimum_sequence=0):
        record_list = list(records)
        source_record = record_list[0]
        record_id = source_record["record_id"]
        serialized_record = {
            "record_id": record_id,
            **source_record,
            "append_sequence": max(minimum_sequence, 0) + 1,
        }
        payload = (
            json.dumps(serialized_record, ensure_ascii=False) + "\n"
        ).encode("utf-8")
        cut_lengths = {
            "first-byte": 1,
            "early-field": 5,
            "record-id": min(24, len(payload) - 1),
            "middle": len(payload) // 2,
            "closing-brace": len(payload) - 2,
        }
        cut_length = cut_lengths[cut_mode]
        with history.path.open("ab") as history_file:
            history_file.write(payload[:cut_length])
            history_file.flush()
            os.fsync(history_file.fileno())
        raise InstallHistoryAppendUncertainError(
            "injected arbitrary EOF interruption"
        )

    def prepare_receipt(_context, pending_ids):
        return InstallPlan(
            records=[{
                "action": "receipt",
                "label": "包含中文以覆盖 UTF-8 中断",
                "decision_ids": list(pending_ids),
            }],
        )

    monkeypatch.setattr(history, "_append_records", interrupted_append)
    with pytest.raises(InstallHistoryAppendUncertainError):
        history.transact(
            skill="prefix-skill",
            target="claude_code",
            decision_ids=("decision:prefix",),
            operation=prepare_receipt,
        )

    monkeypatch.setattr(history, "_append_records", original_append)
    operation_called = False

    def should_not_run(_context, _pending_ids):
        nonlocal operation_called
        operation_called = True
        return None

    history.transact(
        skill="prefix-skill",
        target="claude_code",
        decision_ids=("decision:prefix",),
        operation=should_not_run,
    )

    assert not operation_called
    records = history.all_records()
    assert len(records) == 1
    assert records[0]["decision_ids"] == ["decision:prefix"]


def test_cancelled_batch_never_recovers_unapplied_later_request(tmp_path):
    """中途取消后，尚未 apply 的 prepared journal 不得产生 phantom receipt。"""
    history = InstallHistory(tmp_path / "history.jsonl")
    applied: list[str] = []

    def make_request(
        skill_name: str,
        *,
        cancel: bool = False,
    ) -> InstallTransactionRequest:
        def prepare(_context, pending_ids):
            def apply():
                if cancel:
                    raise InstallDecisionCancelled("injected cancellation")
                applied.append(skill_name)

            def rollback():
                if skill_name in applied:
                    applied.remove(skill_name)

            return InstallPlan(
                records=[{
                    "action": "receipt",
                    "decision_ids": list(pending_ids),
                }],
                apply=apply,
                rollback=rollback,
            )

        return InstallTransactionRequest(
            skill=skill_name,
            target="working_tree",
            decision_ids=(f"decision:{skill_name}",),
            operation=prepare,
        )

    history.transact_many((
        make_request("first"),
        make_request("cancelled", cancel=True),
        make_request("never-applied"),
    ))

    assert applied == []
    assert history.all_records() == []
    assert not history.has_pending_recovery(
        "never-applied",
        "working_tree",
    )
    def no_new_plan(_context, _pending_ids):
        return None

    history.transact(
        skill="never-applied",
        target="working_tree",
        decision_ids=("probe",),
        operation=no_new_plan,
    )
    assert history.all_records() == []


def test_cancelled_apply_with_failed_rollback_never_recovers_success(
    tmp_path,
):
    """已知取消且回滚失败必须保留 cancelled 诊断，重启不得转成成功。"""
    history = InstallHistory(tmp_path / "history.jsonl")
    target_state = {"value": "old", "generation": "source-generation"}

    def prepare(_context, _pending_ids):
        def apply():
            target_state["value"] = "new"
            raise InstallDecisionCancelled(
                "cancel after target change",
                target_changed=True,
            )

        def rollback():
            raise RuntimeError("injected rollback failure")

        return InstallPlan(
            side="main",
            sha="new-sha",
            generation="target-generation",
            apply=apply,
            rollback=rollback,
        )

    def read_generation():
        return target_state["generation"]

    with pytest.raises(
        InstallHistoryCorruptError,
        match="could not be rolled back",
    ):
        history.transact(
            skill="cancelled-skill",
            target="working_tree",
            decision_ids=("cancelled-decision",),
            operation=prepare,
            generation_reader=read_generation,
        )

    recovery = history._read_recovery(
        "cancelled-skill",
        "working_tree",
    )
    assert recovery is not None
    assert recovery["state"] == "cancelled"
    assert history.all_records() == []
    with pytest.raises(
        InstallHistoryCorruptError,
        match="cancelled install transaction",
    ):
        history.transact(
            skill="cancelled-skill",
            target="working_tree",
            decision_ids=("probe",),
            operation=prepare,
            generation_reader=read_generation,
        )
    assert history.all_records() == []


def test_ambiguous_recovery_rejects_changed_generation(tmp_path, monkeypatch):
    """crash journal 只能在 source/expected generation 未被用户改写时恢复。"""
    history = InstallHistory(tmp_path / "history.jsonl")
    state = {
        "generation": "source-generation",
        "installed": ("main", "target-sha", "target-generation"),
    }

    def prepare(_context, _pending_ids):
        return InstallPlan(
            side="main",
            sha="target-sha",
            generation="target-generation",
            records=[{"action": "receipt"}],
        )

    def read_generation():
        return state["generation"]

    def read_installed_state():
        return state["installed"]

    def uncertain_append(*_args, **_kwargs):
        raise InstallHistoryAppendUncertainError("injected uncertain append")

    monkeypatch.setattr(history, "_append_records", uncertain_append)
    with pytest.raises(InstallHistoryAppendUncertainError):
        history.transact(
            skill="generation-skill",
            target="working_tree",
            decision_ids=("generation-decision",),
            operation=prepare,
            generation_reader=read_generation,
            installed_state_reader=read_installed_state,
        )
    state["generation"] = "user-edit-generation"

    with pytest.raises(
        InstallHistoryCorruptError,
        match="generation changed",
    ):
        history.transact(
            skill="generation-skill",
            target="working_tree",
            decision_ids=("probe",),
            operation=prepare,
            generation_reader=read_generation,
            installed_state_reader=read_installed_state,
        )
    assert history.all_records() == []
    assert history.has_pending_recovery(
        "generation-skill",
        "working_tree",
    )


def test_committed_assignment_repairs_secondary_table_and_header(tmp_path):
    """history commit 后宕机时，新进程应重建查询表与 UX header。"""
    trajectory_dir = tmp_path / "trajectories"
    trajectory_dir.mkdir()
    trajectory_path = trajectory_dir / "traj_cc_demo_session1.md"
    trajectory_path.write_text("# Trajectory\n", encoding="utf-8")
    (trajectory_dir / "traj_cc_demo_session1.json").write_text(
        json.dumps({"session_id": "session-1"}),
        encoding="utf-8",
    )
    history = InstallHistory(tmp_path / "history.jsonl")
    history._append_records(({
        "action": "session_assignment",
        "skill": "repair-skill",
        "target": "claude_code",
        "session_id": "session-1",
        "side": "main",
        "sha": "main-sha",
        "used_skill": True,
        "t": 42.0,
        "trajectory_path": str(trajectory_path),
        "decision_ids": [
            "assignment:session-1",
            "flip:session-1",
        ],
    },))
    assignments_path = tmp_path / "session_assignments.jsonl"
    ingester = CCSessionIngester(
        trajectory_dir,
        history_path=history.path,
        assignments_path=assignments_path,
    )

    before = ingester.history.read_count
    ingester._repair_materialized_history()

    assert ingester.history.read_count - before == 1
    assert ingester.assignments.get("session-1") == {
        "sid": "session-1",
        "side": "main",
        "sha": "main-sha",
        "used_skill": True,
        "t": 42.0,
    }
    assert trajectory_path.read_text(encoding="utf-8").startswith(
        "<!-- xskill:skill=repair-skill side=main sha=main-sha -->"
    )
    assert len(assignments_path.read_text(encoding="utf-8").splitlines()) == 1

    reads_after_repair = ingester.history.read_count
    ingester._repair_materialized_history()
    assert ingester.history.read_count == reads_after_repair
    assert len(assignments_path.read_text(encoding="utf-8").splitlines()) == 1


def test_history_repair_does_not_advance_past_concurrent_append(
    tmp_path,
    monkeypatch,
):
    """物化期间追加的 receipt 必须由下一轮消费，不能被事后 stat 跳过。"""
    trajectory_dir = tmp_path / "trajectories"
    trajectory_dir.mkdir()
    history = InstallHistory(tmp_path / "history.jsonl")
    trajectory_paths = {}
    for session_id in ("session-first", "session-second"):
        trajectory_path = trajectory_dir / f"{session_id}.md"
        trajectory_path.write_text("# Trajectory\n", encoding="utf-8")
        trajectory_paths[session_id] = trajectory_path
    first_record = {
        "action": "session_assignment",
        "skill": "repair-skill",
        "target": "claude_code",
        "session_id": "session-first",
        "side": "main",
        "sha": "main-sha",
        "used_skill": True,
        "t": 42.0,
        "trajectory_path": str(trajectory_paths["session-first"]),
    }
    second_record = {
        **first_record,
        "session_id": "session-second",
        "side": "staging",
        "sha": "staging-sha",
        "t": 43.0,
        "trajectory_path": str(trajectory_paths["session-second"]),
    }
    history._append_records((first_record,))
    ingester = CCSessionIngester(
        trajectory_dir,
        history_path=history.path,
        assignments_path=tmp_path / "assignments.jsonl",
    )
    original_materialize = ingester._materialize_assignment_record
    appended = False

    def append_while_materializing(record, *, assignment_batch):
        nonlocal appended
        if not appended:
            appended = True
            history._append_records((second_record,))
        original_materialize(
            record,
            assignment_batch=assignment_batch,
        )

    monkeypatch.setattr(
        ingester,
        "_materialize_assignment_record",
        append_while_materializing,
    )

    ingester._repair_materialized_history()
    assert ingester.assignments.get("session-first") is not None
    assert ingester.assignments.get("session-second") is None

    ingester._repair_materialized_history()
    assert ingester.assignments.get("session-second") == {
        "sid": "session-second",
        "side": "staging",
        "sha": "staging-sha",
        "used_skill": True,
        "t": 43.0,
    }
    assert trajectory_paths["session-second"].read_text(
        encoding="utf-8"
    ).startswith(
        "<!-- xskill:skill=repair-skill side=staging sha=staging-sha -->"
    )
    reads_after_second_repair = ingester.history.read_count
    ingester._repair_materialized_history()
    assert ingester.history.read_count == reads_after_second_repair


def test_history_snapshot_replace_between_stat_and_open_rebuilds(
    tmp_path,
    monkeypatch,
):
    """stat 后 replace 不能把新文件 tail 接到旧索引。"""
    history_path = tmp_path / "history.jsonl"
    history = InstallHistory(history_path)
    history._append_records(({
        "action": "receipt",
        "skill": "old-skill",
        "target": "working_tree",
        "record_id": "old-1",
    },))
    initial_snapshot = history.snapshot()
    InstallHistory(history_path)._append_records(({
        "action": "receipt",
        "skill": "old-skill",
        "target": "working_tree",
        "record_id": "old-2",
    },))
    replacement_path = tmp_path / "replacement.jsonl"
    replacement_history = InstallHistory(replacement_path)
    replacement_history._append_records((
        {
            "action": "receipt",
            "skill": "new-skill",
            "target": "working_tree",
            "record_id": "new-1",
        },
        {
            "action": "receipt",
            "skill": "new-skill",
            "target": "working_tree",
            "record_id": "new-2",
        },
    ))
    original_read = history._read_stable_bytes_locked
    replaced = False

    def replace_before_open(*, offset=0):
        nonlocal replaced
        if offset and not replaced:
            replaced = True
            os.replace(replacement_path, history_path)
        return original_read(offset=offset)

    monkeypatch.setattr(
        history,
        "_read_stable_bytes_locked",
        replace_before_open,
    )

    replacement_snapshot = history.snapshot()

    assert len(initial_snapshot.index.records) == 1
    assert {
        record["record_id"]
        for record in replacement_snapshot.index.records
    } == {"new-1", "new-2"}
    assert "old-1" not in replacement_snapshot.index.record_ids


def test_history_snapshot_truncate_rebuilds_without_stale_records(tmp_path):
    """同 inode truncate 后索引必须清空，再追加只暴露新一代记录。"""
    history_path = tmp_path / "history.jsonl"
    writer = InstallHistory(history_path)
    writer._append_records((
        {
            "action": "receipt",
            "skill": "old-skill",
            "target": "working_tree",
            "record_id": "old-1",
        },
        {
            "action": "receipt",
            "skill": "old-skill",
            "target": "working_tree",
            "record_id": "old-2",
        },
    ))
    reader = InstallHistory(history_path)
    old_snapshot = reader.snapshot()
    history_path.write_bytes(b"")
    truncated_snapshot = reader.snapshot()
    InstallHistory(history_path)._append_records(({
        "action": "receipt",
        "skill": "new-skill",
        "target": "working_tree",
        "record_id": "new-1",
    },))
    new_snapshot = reader.snapshot()

    assert len(old_snapshot.index.records) == 2
    assert len(truncated_snapshot.index.records) == 0
    assert [
        record["record_id"] for record in new_snapshot.index.records
    ] == ["new-1"]


def test_history_snapshot_truncate_and_regrow_same_inode_rebuilds(tmp_path):
    """同 inode 原地改写后即使文件变大，也不能误判成 append。"""
    history_path = tmp_path / "history.jsonl"
    first_line = json.dumps({
        "action": "receipt",
        "skill": "same-size",
        "target": "working_tree",
        "record_id": "old-1",
        "append_sequence": 1,
    }) + "\n"
    replacement_first_line = json.dumps({
        "action": "receipt",
        "skill": "same-size",
        "target": "working_tree",
        "record_id": "new-1",
        "append_sequence": 1,
    }) + "\n"
    replacement_second_line = json.dumps({
        "action": "receipt",
        "skill": "same-size",
        "target": "working_tree",
        "record_id": "new-2",
        "append_sequence": 2,
    }) + "\n"
    assert len(first_line) == len(replacement_first_line)
    history_path.write_text(first_line, encoding="utf-8")
    history = InstallHistory(history_path)
    original_inode = history_path.stat().st_ino
    initial = history.snapshot()

    history_path.write_text(
        replacement_first_line + replacement_second_line,
        encoding="utf-8",
    )
    assert history_path.stat().st_ino == original_inode
    rebuilt = history.snapshot()

    assert "old-1" in initial.index.record_ids
    assert {
        record["record_id"]
        for record in rebuilt.index.records
    } == {"new-1", "new-2"}
    assert "old-1" not in rebuilt.index.record_ids


def test_history_incremental_index_parses_each_new_line_once(tmp_path):
    """1k→2k→4k 追加只解析新增 tail，旧快照保持不可变。"""
    history_path = tmp_path / "history.jsonl"
    writer = InstallHistory(history_path)
    reader = InstallHistory(history_path)

    def records(start: int, stop: int):
        return (
            {
                "action": "install",
                "skill": "scale-skill",
                "target": "claude_code",
                "side": "main",
                "sha": f"sha-{position}",
                "t": float(position),
                "record_id": f"record-{position}",
                "decision_ids": [f"decision-{position}"],
            }
            for position in range(start, stop)
        )

    writer._append_records(records(0, 1000))
    first_snapshot = reader.snapshot()
    assert reader.parsed_line_count == 1000
    writer._append_records(records(1000, 2000))
    second_snapshot = reader.snapshot()
    assert reader.parsed_line_count == 2000
    writer._append_records(records(2000, 4000))
    final_snapshot = reader.snapshot()

    assert reader.parsed_line_count == 4000
    assert len(first_snapshot.index.records) == 1000
    assert len(second_snapshot.index.records) == 2000
    assert len(final_snapshot.index.records) == 4000
    assert (
        final_snapshot.index.latest("scale-skill", "claude_code")["sha"]
        == "sha-3999"
    )
    assert (
        final_snapshot.index.lookup_at(
            2500.5,
            skill="scale-skill",
            target="claude_code",
        )["sha"]
        == "sha-2500"
    )
    assert "decision-3999" in final_snapshot.index.consumed(
        "scale-skill",
        "claude_code",
    )
    reads_after_final_snapshot = reader.read_count
    reader.snapshot()
    assert reader.read_count == reads_after_final_snapshot
    assert reader.parsed_line_count == 4000


def test_history_full_build_sorts_descending_times_once():
    """倒序 legacy 时间全量重建不得逐条头插导致 O(N²) 搬移。"""
    record_count = 16_000
    index = InstallHistory._build_index(
        {
            "action": "install",
            "skill": "descending-skill",
            "target": "claude_code",
            "side": "main",
            "t": float(record_count - position),
            "append_sequence": position + 1,
        }
        for position in range(record_count)
    )

    assert index._state.timed_merge_input_count == 0
    assert index.lookup_at(
        float(record_count),
        skill="descending-skill",
        target="claude_code",
    ) is not None
    assert len(index.records) == record_count


def test_history_incremental_descending_times_use_logarithmic_runs():
    """逐条回拨时间的增量快照不得反复在旧N条前中插成 O(K*N)。"""
    record_count = 8000
    index = InstallHistory._build_index(
        {
            "action": "install",
            "skill": "incremental-descending",
            "target": "claude_code",
            "side": "main",
            "t": float(record_count + position),
            "append_sequence": position + 1,
        }
        for position in range(record_count)
    )
    state = index._state

    for position in range(record_count):
        InstallHistory._extend_index_state(state, ({
            "action": "install",
            "skill": "incremental-descending",
            "target": "claude_code",
            "side": "staging",
            "t": float(-position),
            "append_sequence": record_count + position + 1,
        },))

    extended = type(index)(
        _state=state,
        _record_limit=len(state.records),
        max_append_sequence=record_count * 2,
    )
    assert state.timed_merge_input_count < record_count * 40
    assert extended.lookup_at(
        float(record_count * 3),
        skill="incremental-descending",
        target="claude_code",
    ) is not None
    assert len(extended.records) == record_count * 2


def test_history_materializer_only_consumes_new_tail(
    tmp_path,
    monkeypatch,
):
    """第二批只物化新增 M 条，assignment 每批只获取一次锁。"""
    trajectory_dir = tmp_path / "trajectories"
    trajectory_dir.mkdir()
    history = InstallHistory(tmp_path / "history.jsonl")

    def assignment_records(start: int, stop: int):
        return (
            {
                "action": "session_assignment",
                "skill": "tail-skill",
                "target": "claude_code",
                "session_id": f"tail-session-{position}",
                "side": "main",
                "sha": f"sha-{position}",
                "used_skill": True,
                "t": float(position),
                "trajectory_path": str(
                    trajectory_dir / f"tail-{position}.md"
                ),
            }
            for position in range(start, stop)
        )

    for position in range(13):
        (trajectory_dir / f"tail-{position}.md").write_text(
            "# Trajectory\n",
            encoding="utf-8",
        )
    history._append_records(assignment_records(0, 10))
    ingester = CCSessionIngester(
        trajectory_dir,
        history_path=history.path,
        assignments_path=tmp_path / "assignments.jsonl",
    )
    batch_sizes = []
    header_calls = []
    original_record_many = ingester.assignments.record_many

    def count_assignment_batch(records):
        record_list = list(records)
        batch_sizes.append(len(record_list))
        original_record_many(record_list)

    def count_header(path, *, skill, side, sha):
        header_calls.append((path, skill, side, sha))
        return False

    monkeypatch.setattr(
        ingester.assignments,
        "record_many",
        count_assignment_batch,
    )
    monkeypatch.setattr(
        "xskill.ecosystems.claude_code._prepend_xskill_header",
        count_header,
    )

    ingester._repair_materialized_history()
    history._append_records(assignment_records(10, 13))
    ingester._repair_materialized_history()
    ingester._repair_materialized_history()

    assert batch_sizes == [10, 3]
    assert len(header_calls) == 13
    assert ingester.history.parsed_line_count == 13


def test_empty_legacy_sequence_rebuilds_across_blank_lines_and_restart(
    tmp_path,
):
    """旧 history 空行的物理行序号必须被后续 sequence 延续。"""
    history_path = tmp_path / "history.jsonl"
    history_path.write_text(
        '{"action":"fail","skill":"one"}\n\n'
        '{"action":"fail","skill":"two"}\n',
        encoding="utf-8",
    )
    history_path.with_name("history.jsonl.sequence").write_text(
        "",
        encoding="ascii",
    )
    InstallHistory(history_path).record_fail(
        skill="three",
        agent="test",
        reason="first restart",
    )
    InstallHistory(history_path).record_fail(
        skill="four",
        agent="test",
        reason="second restart",
    )

    assert [
        record["append_sequence"]
        for record in InstallHistory(history_path).all_records()
    ] == [1, 3, 4, 5]


def test_incremental_legacy_append_preserves_blank_line_sequence(tmp_path):
    """增量解析 legacy 记录时，默认序号必须沿用物理行而非记录数。"""
    history_path = tmp_path / "history.jsonl"
    history_path.write_text(
        '{"action":"fail","skill":"one"}\n\n'
        '{"action":"fail","skill":"two"}\n',
        encoding="utf-8",
    )
    history = InstallHistory(history_path)
    initial = history.snapshot()
    with history_path.open("a", encoding="utf-8") as history_file:
        history_file.write('{"action":"fail","skill":"three"}\n')

    extended = history.snapshot()

    assert [
        record["append_sequence"]
        for record in initial.index.records
    ] == [1, 3]
    assert [
        record["append_sequence"]
        for record in extended.index.records
    ] == [1, 3, 4]


def test_empty_sequence_rebuilds_after_reader_was_already_validated(tmp_path):
    """同一 reader 运行中 sequence 损坏也必须从 history 恢复，不能重复编号。"""
    history = InstallHistory(tmp_path / "history.jsonl")
    first = history._append_records(({
        "action": "receipt",
        "skill": "sequence-skill",
        "target": "working_tree",
    },))[0]
    history.snapshot()
    history._sequence_path.write_text("", encoding="ascii")

    second = history._append_records(({
        "action": "receipt",
        "skill": "sequence-skill",
        "target": "working_tree",
    },))[0]

    assert second["append_sequence"] > first["append_sequence"]
    assert [
        record["append_sequence"]
        for record in history.all_records()
    ] == [1, 2]


def test_sequence_replace_failure_leaves_history_and_sequence_consistent(
    tmp_path,
    monkeypatch,
):
    """sequence 原子 replace 失败时不得先追加 history 或破坏旧 sequence。"""
    history_path = tmp_path / "history.jsonl"
    history = InstallHistory(history_path)
    history.record_fail(skill="first", agent="test", reason="seed")
    sequence_path = history_path.with_name("history.jsonl.sequence")
    original_sequence = sequence_path.read_text(encoding="ascii")
    original_history = history_path.read_bytes()
    original_replace = os.replace

    def fail_sequence_replace(source, destination):
        if Path(destination) == sequence_path:
            raise OSError("injected sequence replace failure")
        return original_replace(source, destination)

    monkeypatch.setattr(os, "replace", fail_sequence_replace)
    with pytest.raises(OSError, match="injected sequence replace failure"):
        history.record_fail(
            skill="second",
            agent="test",
            reason="must not append",
        )

    assert sequence_path.read_text(encoding="ascii") == original_sequence
    assert history_path.read_bytes() == original_history
    assert list(tmp_path.glob(".history.jsonl.sequence.*.tmp")) == []


def test_supervisor_steady_poll_does_not_rescan_source_or_target_globs(
    tmp_path,
    monkeypatch,
):
    """无目录变化时，0.5s supervisor poll 只检查目录 mtime。"""
    home_root = tmp_path / "home"
    (home_root / ".claude" / "projects" / "project").mkdir(
        parents=True
    )
    trajectory_dir = tmp_path / "trajectories"
    ingester = CCSessionIngester(
        trajectory_dir,
        home_root=home_root,
    )
    assert ingester.run_once() == []

    def unexpected_glob(_path, _pattern):
        raise AssertionError("steady poll must not glob historical files")

    monkeypatch.setattr(Path, "glob", unexpected_glob)
    assert ingester.run_once() == []


def test_idle_source_poll_stats_only_a_bounded_project_shard(
    tmp_path,
    monkeypatch,
):
    """大量 project 的稳定 poll 每轮只能 stat 固定预算，不能退化为 O(D)。"""
    home_root = tmp_path / "home"
    projects_root = home_root / ".claude" / "projects"
    projects_root.mkdir(parents=True)
    ingester = CCSessionIngester(
        tmp_path / "trajectories",
        home_root=home_root,
    )
    root_stat = projects_root.stat()
    root_signature = (
        root_stat.st_mtime_ns,
        root_stat.st_ctime_ns,
        root_stat.st_size,
    )
    project_directories = [
        projects_root / f"project-{index:04d}"
        for index in range(10_000)
    ]
    ingester._projects_root_signature = root_signature
    ingester._projects_root_rescan_deadline = float("inf")
    ingester._source_directory_order = project_directories
    ingester._source_directory_signatures = {
        project_directory: root_signature
        for project_directory in project_directories
    }
    ingester._source_directory_rescan_deadlines = {
        project_directory: float("inf")
        for project_directory in project_directories
    }
    original_stat = Path.stat
    project_stat_count = 0

    def count_project_stat(path, *args, **kwargs):
        nonlocal project_stat_count
        if path in ingester._source_directory_signatures:
            project_stat_count += 1
            return root_stat
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", count_project_stat)

    assert ingester._incremental_source_candidates() == ()
    assert project_stat_count <= 64


def test_projects_root_discovery_is_bounded_per_poll(
    tmp_path,
    monkeypatch,
):
    """root 下项目发现也必须分片，单轮不能遍历全部目录。"""
    home_root = tmp_path / "home"
    projects_root = home_root / ".claude" / "projects"
    projects_root.mkdir(parents=True)
    ingester = CCSessionIngester(
        tmp_path / "trajectories",
        home_root=home_root,
    )
    entry_checks = 0

    class FakeEntry:
        def __init__(self, index):
            self.path = str(projects_root / f"project-{index:05d}")

        def is_dir(self):
            nonlocal entry_checks
            entry_checks += 1
            return True

    class FakeScandir:
        def __init__(self):
            self._entries = iter(FakeEntry(index) for index in range(10_000))

        def __iter__(self):
            return self

        def __next__(self):
            return next(self._entries)

        def close(self):
            return None

    def fake_scandir(path):
        assert path == projects_root
        return FakeScandir()

    monkeypatch.setattr(os, "scandir", fake_scandir)
    monkeypatch.setattr(
        "xskill.ecosystems.claude_code.SOURCE_DIRECTORY_STAT_BUDGET",
        0,
    )

    assert ingester._incremental_source_candidates() == ()
    assert entry_checks == 256
    assert ingester._projects_root_scan is not None


def test_single_project_file_discovery_is_bounded_per_poll(
    tmp_path,
    monkeypatch,
):
    """单 project 含大量 session 时也只消费固定 entry quantum。"""
    home_root = tmp_path / "home"
    projects_root = home_root / ".claude" / "projects"
    project_directory = projects_root / "project"
    project_directory.mkdir(parents=True)
    ingester = CCSessionIngester(
        tmp_path / "trajectories",
        home_root=home_root,
    )
    root_stat = projects_root.stat()
    project_stat = project_directory.stat()
    ingester._projects_root_signature = (
        root_stat.st_mtime_ns,
        root_stat.st_ctime_ns,
        root_stat.st_size,
    )
    ingester._projects_root_rescan_deadline = float("inf")
    ingester._source_directory_order = [project_directory]
    ingester._source_directory_signatures = {
        project_directory: (-1, -1, -1)
    }
    entry_checks = 0

    class FakeEntry:
        def __init__(self, index):
            self.name = f"session-{index:05d}.jsonl"
            self.path = str(project_directory / self.name)

    class FakeScandir:
        def __init__(self):
            self._entries = iter(FakeEntry(index) for index in range(2000))

        def __iter__(self):
            return self

        def __next__(self):
            nonlocal entry_checks
            entry_checks += 1
            return next(self._entries)

        def close(self):
            return None

    original_stat = Path.stat

    def fake_stat(path, *args, **kwargs):
        if path.parent == project_directory and path.suffix == ".jsonl":
            return project_stat
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", fake_stat)
    monkeypatch.setattr(os, "scandir", lambda _path: FakeScandir())

    ingester._incremental_source_candidates()

    assert entry_checks == 64
    assert project_directory in ingester._source_file_scans


def test_active_project_file_scans_rotate_fairly(
    tmp_path,
    monkeypatch,
):
    """超过一轮token槽位的长目录必须轮转，后排不能被前8永久饿死。"""
    home_root = tmp_path / "home"
    projects_root = home_root / ".claude" / "projects"
    projects_root.mkdir(parents=True)
    ingester = CCSessionIngester(
        tmp_path / "trajectories",
        home_root=home_root,
    )
    root_stat = projects_root.stat()
    ingester._projects_root_signature = (
        root_stat.st_mtime_ns,
        root_stat.st_ctime_ns,
        root_stat.st_size,
    )
    ingester._projects_root_rescan_deadline = float("inf")
    project_directories = [
        projects_root / f"project-{index:02d}"
        for index in range(12)
    ]
    ingester._source_directory_order = project_directories
    ingester._source_directory_signatures = {
        project_directory: (
            root_stat.st_mtime_ns,
            root_stat.st_ctime_ns,
            root_stat.st_size,
        )
        for project_directory in project_directories
    }
    ingester._source_directory_rescan_deadlines = {
        project_directory: float("inf")
        for project_directory in project_directories
    }
    progress = {project_directory: 0 for project_directory in project_directories}

    class LongScan:
        def __init__(self, project_directory):
            self.project_directory = project_directory

        def __iter__(self):
            return self

        def __next__(self):
            progress[self.project_directory] += 1
            return type("Entry", (), {
                "name": "not-a-session.txt",
                "path": str(self.project_directory / "not-a-session.txt"),
            })()

        def close(self):
            return None

    for project_directory in project_directories:
        source_scan = LongScan(project_directory)
        ingester._source_file_scans[project_directory] = source_scan
        ingester._source_file_scan_seen[project_directory] = set()
        ingester._source_file_scan_queue.append(
            (project_directory, source_scan)
        )
    original_stat = Path.stat

    def fake_stat(path, *args, **kwargs):
        if path in progress:
            return root_stat
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", fake_stat)

    for _poll in range(3):
        ingester._incremental_source_candidates()

    assert all(entry_count == 64 for entry_count in progress.values())


def test_project_removal_does_not_scan_global_source_index(
    tmp_path,
    monkeypatch,
):
    """项目删除只能触碰本项目文件集合，不能每目录全扫全局 S。"""
    home_root = tmp_path / "home"
    projects_root = home_root / ".claude" / "projects"
    project_directory = projects_root / "project"
    project_directory.mkdir(parents=True)
    source_path = project_directory / "session.jsonl"
    source_path.write_text("{}\n", encoding="utf-8")
    ingester = CCSessionIngester(
        tmp_path / "trajectories",
        home_root=home_root,
    )
    root_stat = projects_root.stat()
    project_stat = project_directory.stat()
    project_signature = (
        project_stat.st_mtime_ns,
        project_stat.st_ctime_ns,
        project_stat.st_size,
    )
    ingester._projects_root_signature = (
        root_stat.st_mtime_ns,
        root_stat.st_ctime_ns,
        root_stat.st_size,
    )
    ingester._projects_root_rescan_deadline = float("inf")
    ingester._source_directory_order = [project_directory]
    ingester._source_directory_signatures = {
        project_directory: project_signature
    }
    ingester._source_directory_rescan_deadlines = {
        project_directory: 0.0
    }
    ingester._source_files_by_project = {
        project_directory: {source_path}
    }

    class NoGlobalIteration(dict):
        def __iter__(self):
            raise AssertionError("must not scan global source signatures")

    ingester._source_file_signatures = NoGlobalIteration({
        source_path: (None, None, 3, 1),
        **{
            projects_root / f"other-{index}" / "session.jsonl": (
                None,
                None,
                3,
                1,
            )
            for index in range(3000)
        },
    })
    monkeypatch.setattr(os, "scandir", lambda _path: iter(()))

    ingester._incremental_source_candidates()

    assert source_path not in ingester._source_file_signatures


def test_session_assignments_repairs_only_incomplete_eof(tmp_path):
    """半条 EOF append 可截断恢复，完整前缀保留且后续仍可写。"""
    assignments_path = tmp_path / "assignments.jsonl"
    first = {
        "sid": "first",
        "side": "main",
        "sha": "sha-1",
        "used_skill": False,
        "t": 1.0,
    }
    assignments_path.write_bytes(
        (json.dumps(first) + "\n").encode("utf-8")
        + b'{"sid":"partial","side":'
    )

    assignments = SessionAssignments(assignments_path)
    assignments.record(
        sid="second",
        side="staging",
        sha="sha-2",
        used_skill=True,
        t=2.0,
    )

    assert assignments.get("first") == first
    assert assignments.get("second")["side"] == "staging"
    assert assignments_path.read_bytes().endswith(b"\n")
    assert [
        json.loads(line)["sid"]
        for line in assignments_path.read_text(
            encoding="utf-8"
        ).splitlines()
    ] == ["first", "second"]


def test_session_assignments_rejects_corruption_before_eof(tmp_path):
    """坏行不在 EOF 时必须 fail loud，不能借尾部修复偷偷跳过。"""
    assignments_path = tmp_path / "assignments.jsonl"
    assignments_path.write_bytes(
        b'{"sid":"first","side":"main","t":1}\n'
        b'{"sid":broken}\n'
        b'{"sid":"partial"'
    )

    with pytest.raises(RuntimeError, match="invalid session assignment JSON"):
        SessionAssignments(assignments_path)


def test_supervisor_keeps_incomplete_new_source_in_incremental_set(tmp_path):
    """目录只在创建时变化；未完成文件后续写完仍须被 active set 发现。"""
    home_root = tmp_path / "home"
    project_dir = home_root / ".claude" / "projects" / "project"
    project_dir.mkdir(parents=True)
    trajectory_dir = tmp_path / "trajectories"
    ingester = CCSessionIngester(
        trajectory_dir,
        home_root=home_root,
    )
    assert ingester.run_once() == []

    source_path = project_dir / "incremental-session.jsonl"
    source_path.write_text(
        json.dumps({
            "type": "user",
            "timestamp": "2026-07-17T00:00:00Z",
            "message": {"content": "hello"},
        }) + "\n",
        encoding="utf-8",
    )
    assert ingester.run_once() == []
    assert source_path in ingester._pending_source_paths

    with source_path.open("a", encoding="utf-8") as source_file:
        source_file.write(json.dumps({"type": "last-prompt"}) + "\n")
    submitted = ingester.run_once()

    assert [record["session_id"] for record in submitted] == [
        "incremental-session"
    ]
    assert source_path not in ingester._pending_source_paths
    assert ingester.run_once() == []


def test_same_tick_directory_signature_gets_bounded_name_rescan(
    tmp_path,
    monkeypatch,
):
    """目录签名碰撞时，周期名称扫描仍会发现新 session。"""
    home_root = tmp_path / "home"
    project_dir = home_root / ".claude" / "projects" / "project"
    project_dir.mkdir(parents=True)
    ingester = CCSessionIngester(
        tmp_path / "trajectories",
        home_root=home_root,
    )
    ingester._incremental_source_candidates()
    ingester._incremental_source_candidates()
    unchanged_stat = project_dir.stat()
    source_path = project_dir / "same-tick.jsonl"
    source_path.write_text('{"type":"user"}\n', encoding="utf-8")
    original_stat = Path.stat

    def same_directory_signature(path, *args, **kwargs):
        if path == project_dir:
            return unchanged_stat
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", same_directory_signature)
    assert ingester._incremental_source_candidates() == ()
    ingester._source_directory_rescan_deadlines[project_dir] = 0.0

    assert ingester._incremental_source_candidates() == (source_path,)


def test_incremental_source_idle_poll_does_not_list_directory(
    tmp_path,
    monkeypatch,
):
    """两次变更后校验完成，deadline 前稳定 poll 不再枚举历史文件名。"""
    home_root = tmp_path / "home"
    project_dir = home_root / ".claude" / "projects" / "project"
    project_dir.mkdir(parents=True)
    ingester = CCSessionIngester(
        tmp_path / "trajectories",
        home_root=home_root,
    )
    ingester._incremental_source_candidates()
    ingester._incremental_source_candidates()

    def unexpected_scandir(_path):
        raise AssertionError("stable idle poll must not scan directory names")

    monkeypatch.setattr(os, "scandir", unexpected_scandir)
    assert ingester._incremental_source_candidates() == ()


def test_incremental_source_io_warning_is_rate_limited_and_recovers(
    tmp_path,
    monkeypatch,
    caplog,
):
    """同一权限故障只报警一次，恢复后重新进入正常扫描。"""
    home_root = tmp_path / "home"
    project_dir = home_root / ".claude" / "projects" / "project"
    project_dir.mkdir(parents=True)
    ingester = CCSessionIngester(
        tmp_path / "trajectories",
        home_root=home_root,
    )
    ingester._incremental_source_candidates()
    original_stat = Path.stat
    fail_stat = True

    def permission_error_then_recover(path, *args, **kwargs):
        if path == project_dir and fail_stat:
            raise PermissionError("injected permission failure")
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", permission_error_then_recover)
    ingester._incremental_source_candidates()
    ingester._incremental_source_candidates()
    matching_warnings = [
        record
        for record in caplog.records
        if "operation=stat_project" in record.getMessage()
    ]
    assert len(matching_warnings) == 1

    fail_stat = False
    ingester._incremental_source_candidates()
    assert not any(
        error_key[0] == "stat_project"
        for error_key in ingester._source_scan_errors
    )


def test_source_scan_error_recovery_clears_each_path_in_constant_work(
    tmp_path,
    monkeypatch,
):
    """大量路径恢复必须逐 key 删除，不能反复复制完整错误集合。"""
    class OperationCountingSet(set):
        def __init__(self):
            super().__init__()
            self.add_count = 0
            self.discard_count = 0
            self.iteration_count = 0

        def add(self, value):
            self.add_count += 1
            super().add(value)

        def discard(self, value):
            self.discard_count += 1
            super().discard(value)

        def __iter__(self):
            self.iteration_count += 1
            return super().__iter__()

    ingester = CCSessionIngester(
        tmp_path / "trajectories",
        home_root=tmp_path / "home",
    )
    error_keys = OperationCountingSet()
    ingester._source_scan_errors = error_keys
    monkeypatch.setattr(
        "xskill.ecosystems.claude_code.logger.warning",
        lambda *args, **kwargs: None,
    )
    source_paths = [
        tmp_path / "sessions" / f"{index}.jsonl"
        for index in range(10_000)
    ]

    for source_path in source_paths:
        ingester._warn_source_scan_error(
            "stat_session",
            source_path,
            PermissionError("injected"),
        )
    for source_path in source_paths:
        ingester._clear_source_scan_error("stat_session", source_path)

    assert error_keys.add_count == len(source_paths)
    assert error_keys.discard_count == len(source_paths)
    assert error_keys.iteration_count == 0
    assert not error_keys


def test_seen_session_source_growth_is_rebridged_once(tmp_path):
    """已桥长会话后续增长应覆盖同一轨迹，稳定后不重复解析。"""
    home_root = tmp_path / "home"
    project_dir = home_root / ".claude" / "projects" / "project"
    project_dir.mkdir(parents=True)
    source_path = project_dir / "long-session.jsonl"
    source_path.write_text(
        "\n".join((
            json.dumps({
                "type": "user",
                "sessionId": "long-session",
                "timestamp": "2026-07-17T00:00:00Z",
                "message": {"content": "first"},
            }),
            json.dumps({
                "type": "assistant",
                "sessionId": "long-session",
                "message": {
                    "content": [{"type": "text", "text": "FIRST_MARKER"}]
                },
            }),
            json.dumps({"type": "last-prompt"}),
        )) + "\n",
        encoding="utf-8",
    )
    old_time = source_path.stat().st_mtime - 300
    os.utime(source_path, (old_time, old_time))
    ingester = CCSessionIngester(
        tmp_path / "trajectories",
        home_root=home_root,
    )
    first_submission = ingester.run_once()
    assert len(first_submission) == 1
    trajectory_path = Path(first_submission[0]["path"])

    with source_path.open("a", encoding="utf-8") as source_file:
        source_file.write(
            json.dumps({
                "type": "assistant",
                "sessionId": "long-session",
                "message": {
                    "content": [{"type": "text", "text": "SECOND_MARKER"}]
                },
            }) + "\n" + json.dumps({"type": "last-prompt"}) + "\n"
        )
    old_time = source_path.stat().st_mtime - 300
    os.utime(source_path, (old_time, old_time))
    for project_directory in ingester._source_directory_rescan_deadlines:
        ingester._source_directory_rescan_deadlines[
            project_directory
        ] = 0.0
    second_submission = ingester.run_once()

    assert len(second_submission) == 1
    assert second_submission[0]["rebridged"] is True
    assert "SECOND_MARKER" in trajectory_path.read_text(encoding="utf-8")
    assert ingester.run_once() == []


def test_transact_many_parses_history_once_for_twenty_five_skills(tmp_path):
    """多 skill 一批的 history 复杂度不随 skill 数重复 parse。"""
    history = InstallHistory(tmp_path / "history.jsonl")
    requests = []
    callback_count = 0
    for skill_index in range(25):
        skill_name = f"skill-{skill_index:02d}"

        def prepare_receipt(
            _context,
            pending_ids,
            *,
            prepared_skill_name=skill_name,
        ):
            nonlocal callback_count
            callback_count += 1
            return InstallPlan(records=[{
                "action": "batch_decision",
                "skill": prepared_skill_name,
                "decision_ids": list(pending_ids),
            }])

        requests.append(InstallTransactionRequest(
            skill=skill_name,
            target="working_tree",
            decision_ids=("window:42",),
            operation=prepare_receipt,
            decision_kind="window",
            decision_sequence=42,
        ))

    results = history.transact_many(requests)

    assert callback_count == 25
    assert history.read_count == 1
    assert len(results) == 25
    assert len(history.all_records()) == 25


def test_bad_history_line_fails_loud_without_logging_content(tmp_path, caplog):
    history_path = tmp_path / "history.jsonl"
    history_path.write_text(
        '{"skill":"safe"}\nSECRET-BROKEN-CONTENT\n',
        encoding="utf-8",
    )

    with pytest.raises(InstallHistoryCorruptError):
        InstallHistory(history_path).all_records()

    assert "line=2" in caplog.text
    assert "SECRET-BROKEN-CONTENT" not in caplog.text


def test_terminal_and_rotation_share_generation_fence(tmp_path):
    """任意锁顺序下旧 rotation 都不能覆盖终态 main。"""
    history_path = tmp_path / "history.jsonl"
    history = InstallHistory(history_path)
    history.record(
        skill="race-skill",
        target="claude_code",
        side="main",
        sha="main-v1",
    )
    state = {"generation": "main-v1:staging-v2", "target": "main"}
    barrier = threading.Barrier(3)

    def rotate():
        barrier.wait()

        def prepare(_context, pending_ids):
            def install_staging():
                state["target"] = "staging"

            def install_main():
                state["target"] = "main"

            return InstallPlan(
                side="staging",
                sha="staging-v2",
                generation=state["generation"],
                install_decision_ids=pending_ids,
                apply=install_staging,
                rollback=install_main,
            )

        def read_generation():
            return state["generation"]

        return InstallHistory(history_path).transact(
            skill="race-skill",
            target="claude_code",
            decision_ids=("window:101",),
            operation=prepare,
            decision_kind="window",
            decision_sequence=101,
            expected_generation="main-v1:staging-v2",
            generation_reader=read_generation,
        )

    def terminate():
        barrier.wait()

        def prepare(_context, pending_ids):
            state["generation"] = "main-v2:"

            def install_main():
                state["target"] = "main"

            return InstallPlan(
                side="main",
                sha="main-v2",
                generation=state["generation"],
                install_decision_ids=pending_ids,
                apply=install_main,
            )

        def read_generation():
            return state["generation"]

        return InstallHistory(history_path).transact(
            skill="race-skill",
            target="claude_code",
            decision_ids=("terminal:main-v1:staging-v2",),
            operation=prepare,
            expected_generation="main-v1:staging-v2",
            generation_reader=read_generation,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        rotate_future = executor.submit(rotate)
        terminal_future = executor.submit(terminate)
        barrier.wait()
        rotate_future.result(timeout=10)
        terminal_future.result(timeout=10)

    assert state == {"generation": "main-v2:", "target": "main"}
    assert history.index().latest("race-skill", "claude_code")["sha"] == "main-v2"
