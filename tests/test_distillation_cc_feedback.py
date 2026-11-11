"""Targeted CC ingestion preserves attributable usage without touching siblings."""
import json
import os
from pathlib import Path

import pytest

from xskill.ecosystems._history import InstallHistory
from xskill.ecosystems.claude_code import CCSessionIngester
from xskill.pipeline.distillation import DistillationStore
from xskill.pipeline.trajectory import parse_traj_header
from xskill.skill.git import (
    commit_baby_to_main_branch, commit_to_staging_branch,
    init_skill_repo_on_baby, run_git,
)


def _skill(root, name, staging=False):
    path = root / name
    init_skill_repo_on_baby(str(path), name, "Diagnose integration failures")
    (path / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: Diagnose integration failures\n---\n"
        "Run the diagnostic checks and compare actual and expected output.\n",
        encoding="utf-8",
    )
    assert commit_baby_to_main_branch(str(path), "main ready")
    if staging:
        with (path / "SKILL.md").open("a", encoding="utf-8") as stream:
            stream.write("Record the additional staging checks.\n")
        assert commit_to_staging_branch(str(path), "candidate ready")
    return path


def _session(home, name, skill):
    project = home / ".claude/projects/project"
    project.mkdir(parents=True, exist_ok=True)
    path = project / f"{name}.jsonl"
    path.write_text("\n".join([
        json.dumps({"type": "user", "sessionId": name, "timestamp": "2026-11-05T00:00:00Z", "message": {"content": "Diagnose this integration failure"}}),
        json.dumps({"type": "assistant", "sessionId": name, "message": {"content": [{"type": "tool_use", "id": "skill-call", "name": "Skill", "input": {"skill": skill}}]}}),
        json.dumps({"type": "last-prompt"}),
    ]) + "\n", encoding="utf-8")
    old = path.stat().st_mtime - 300
    os.utime(path, (old, old))
    return path


def _setup(tmp_path, *, target_staging=False):
    from xskill.ecosystems.claude_code import install_to_claude_code

    root = tmp_path / "skills"
    root.mkdir()
    other = _skill(root, "a-other", staging=True)
    target = _skill(root, "z-target", staging=target_staging)
    home = tmp_path / "home"
    (home / ".claude/projects").mkdir(parents=True)
    install_to_claude_code(target, target_root=home, side="main")
    history = InstallHistory(tmp_path / "install_history.jsonl")
    main_sha = run_git(["rev-parse", "main"], cwd=str(target))[1].strip()
    history.record(skill=target.name, side="main", sha=main_sha, target="claude_code", t=1.0)
    history.record(skill=other.name, side="staging", sha="other-sha", target="claude_code", t=1.0)
    runs = DistillationStore(tmp_path / "registry.db", root)
    runs.configure("update_only", skill=target.name)
    return root, target, other, home, history, runs, main_sha


def _ingester(tmp_path, target, home, history, runs):
    return CCSessionIngester(
        tmp_path / "trajectories", home_root=home, skill_dir=target.parent,
        target_root=home, history_path=history.path,
        assignments_path=tmp_path / "assignments.jsonl",
        registry_db_path=runs.db_path, target_skill=target.name,
    )


def test_target_main_usage_is_attributed_without_staging_or_install(tmp_path, monkeypatch):
    _root, target, other, home, history, runs, main_sha = _setup(tmp_path)
    _session(home, "target-main-session", target.name)
    _session(home, "unrelated-session", other.name)
    before_other = [r for r in history.all_records() if r.get("skill") == other.name]

    def forbidden(*a, **kw):
        raise AssertionError("main-only attribution must not install or rotate")

    monkeypatch.setattr("xskill.ecosystems.claude_code.install_to_claude_code", forbidden)
    ingester = _ingester(tmp_path, target, home, history, runs)
    results = ingester.run_once()
    assert len(results) == 2
    by_sid = {r["session_id"]: r for r in results}
    target_header = parse_traj_header(Path(by_sid["target-main-session"]["path"]).read_text(encoding="utf-8"))
    assert target_header == {"skill": target.name, "side": "main", "sha": main_sha}
    assert parse_traj_header(Path(by_sid["unrelated-session"]["path"]).read_text(encoding="utf-8")) is None
    assert [r for r in history.all_records() if r.get("skill") == other.name] == before_other
    assert ingester.stats["flips"] == 0
    assert ingester.run_once() == []


def test_target_staging_rotation_never_selects_first_non_target_canary(tmp_path, monkeypatch):
    from xskill.ecosystems import claude_code

    _root, target, other, home, history, runs, main_sha = _setup(tmp_path, target_staging=True)
    _session(home, "target-staging-session", target.name)
    installs = []
    original = claude_code.install_to_claude_code

    def record_install(path, **kwargs):
        from xskill.ecosystems.install_ledger import get_default_ledger
        assert get_default_ledger().db_path == runs.db_path.parent / "installations.sqlite"
        installs.append((Path(path).name, kwargs["side"]))
        return original(path, **kwargs)

    monkeypatch.setattr(claude_code, "install_to_claude_code", record_install)
    before_other = [r for r in history.all_records() if r.get("skill") == other.name]
    ingester = _ingester(tmp_path, target, home, history, runs)
    results = ingester.run_once()
    assert len(results) == 1
    header = parse_traj_header(Path(results[0]["path"]).read_text(encoding="utf-8"))
    assert header == {"skill": target.name, "side": "main", "sha": main_sha}
    assert installs == [(target.name, "staging")]
    assert [r for r in history.all_records() if r.get("skill") == other.name] == before_other


def test_repair_does_not_materialize_other_skill_receipts(tmp_path):
    _root, target, other, home, history, runs, _sha = _setup(tmp_path)
    trajectories = tmp_path / "trajectories"
    trajectories.mkdir()
    other_md = trajectories / "traj_other.md"
    other_md.write_text("User evidence must retain its existing header.\n", encoding="utf-8")
    history._append_records([{
        "action": "session_assignment", "skill": other.name,
        "target": "claude_code", "session_id": "old-other-session",
        "side": "staging", "sha": "other-sha", "used_skill": True,
        "t": 1.0, "trajectory_path": str(other_md),
    }])
    before = other_md.read_bytes()
    ingester = _ingester(tmp_path, target, home, history, runs)
    ingester.run_once()
    assert other_md.read_bytes() == before
    assert ingester.assignments.get("old-other-session") is None


def test_target_filter_requires_explicit_authority_database(tmp_path):
    with pytest.raises(ValueError, match="database|registry_db_path|explicit"):
        CCSessionIngester(tmp_path / "trajectories", skill_dir=tmp_path / "skills", target_skill="target")


def test_independent_ingester_rejects_replaced_target_identity(tmp_path):
    import shutil

    _root, target, _other, home, history, runs, _sha = _setup(tmp_path)
    _session(home, "replacement-session", target.name)
    original = target.with_name("original-target")
    target.rename(original)
    shutil.copytree(original, target)
    before = history.all_records()
    ingester = _ingester(tmp_path, target, home, history, runs)
    with pytest.raises((ValueError, PermissionError), match="identity|replaced|changed"):
        ingester.run_once()
    assert history.all_records() == before


def test_uncreated_target_bridges_evidence_without_creating_skill(tmp_path):
    root = tmp_path / "skills"
    root.mkdir()
    runs = DistillationStore(tmp_path / "registry.db", root)
    runs.configure("create_one", skill="target", description="Diagnose integration failures")
    home = tmp_path / "home"
    _session(home, "before-target-creation", "target")
    history = InstallHistory(tmp_path / "install_history.jsonl")
    target = root / "target"
    ingester = _ingester(tmp_path, target, home, history, runs)
    results = ingester.run_once()
    assert len(results) == 1
    assert parse_traj_header(Path(results[0]["path"]).read_text(encoding="utf-8")) is None
    assert not target.exists()
    assert history.all_records() == []


def test_independent_cc_callbacks_use_explicit_state_ledger(tmp_path, monkeypatch):
    from xskill.ecosystems.install_ledger import get_default_ledger

    original = get_default_ledger()
    observed = []

    def inspect_ledger(self):
        ledger = get_default_ledger()
        expected = self.registry_db_path.parent
        assert ledger.db_path == expected / "installations.sqlite"
        assert ledger.staging_root == expected / "removal-staging"
        observed.append(ledger.db_path)
        return []

    monkeypatch.setattr(CCSessionIngester, "_run_once_locked", inspect_ledger)
    for name in ("state-a", "state-b"):
        state = tmp_path / name
        root = state / "skills"
        root.mkdir(parents=True)
        runs = DistillationStore(state / "registry.db", root)
        runs.configure("create_one", skill="target", description="Diagnose failures")
        ingester = CCSessionIngester(
            state / "trajectories", home_root=state / "home", skill_dir=root,
            registry_db_path=runs.db_path, target_skill="target",
        )
        assert ingester.run_once() == []
        assert get_default_ledger() is original
    assert observed == [tmp_path / name / "installations.sqlite" for name in ("state-a", "state-b")]
