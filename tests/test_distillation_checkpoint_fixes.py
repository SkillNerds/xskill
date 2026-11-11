"""Exact checkpoint authority and retryable final baby validation; no real models."""
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from xskill.agents import agent_tools
from xskill.agents.skill_edit_agent import SkillEditAgent
from xskill.pipeline.distillation import DistillationStore
from xskill.skill import candidates as C
from xskill.skill.git import current_branch, run_git
from xskill.skill.write_policy import library_write_guard


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr("xskill.skill.catalog_store.notify_native_upsert", lambda *a, **k: None)
    runs = DistillationStore(tmp_path / "state" / "registry.db", tmp_path / "skills")
    runs.configure("create_one", "chosen", "Diagnose failures")
    target = runs.ensure_target()
    context = agent_tools.create_agent_tool_context(
        skill_dir=runs.skill_dir, atom_skill_dir=runs.skill_dir,
        registry_db_path=runs.db_path, distillation_store=runs,
        distillation_policy=runs.policy(), config={"skill_opt": {"enabled": False}},
    )
    return runs, target, context


def commit_text(runs, target, message):
    with library_write_guard(runs.skill_dir, runs.policy(), db_path=runs.db_path):
        path = target / "SKILL.md"
        path.write_text(path.read_text(encoding="utf-8") + "\nCheckpoint work.\n", encoding="utf-8")
        assert run_git(["add", "SKILL.md"], cwd=str(target))[0] == 0
        assert run_git(["commit", "-m", message], cwd=str(target))[0] == 0


@pytest.mark.parametrize("change", ["content", "source"])
def test_id_only_legacy_receipt_cannot_ack_new_evidence(env, change):
    runs, target, _ = env
    old = SimpleNamespace(atom_id="same", content="old evidence")
    runs.record_atom(old, "accepted", source_key="source-a")
    commit_text(runs, target, f'checkpoint\n\nXSkill-Run: {runs.run()["run_id"]}\nXSkill-Atoms: ["same"]')
    new = SimpleNamespace(atom_id="same", content="new evidence" if change == "content" else old.content)
    source = "source-a" if change == "content" else "source-b"
    runs.record_atom(new, "pending", source_key=source)
    with pytest.raises(RuntimeError, match="legacy|unproven"):
        runs.reconcile_checkpoints()
    assert runs.atom_record(new, source)["state"] == "pending"


def test_id_only_legacy_receipt_preserves_existing_candidate_without_audit(env):
    runs, target, _ = env
    with library_write_guard(runs.skill_dir, runs.policy(), db_path=runs.db_path):
        C.save_candidates(target, {"candidates": [{"atom_id": "old", "weightscore": 8}]})
    before = (target / C.CANDIDATES_FILENAME).read_bytes()
    commit_text(runs, target, f'checkpoint\n\nXSkill-Run: {runs.run()["run_id"]}\nXSkill-Atoms: ["old"]')
    with pytest.raises(RuntimeError, match="legacy|unproven"):
        runs.reconcile_checkpoints()
    assert (target / C.CANDIDATES_FILENAME).read_bytes() == before
    assert runs.status()["accepted"] == 0


def test_model_trailer_cannot_ack_batch_external_atom(env):
    runs, target, context = env
    allowed = SimpleNamespace(atom_id="allowed", content="allowed evidence")
    external = SimpleNamespace(atom_id="external", content="outside evidence")
    for atom in (allowed, external):
        runs.record_atom(atom, "accepted", source_key="source-a")
    context = replace(context, atom_store=SimpleNamespace(load=lambda aid: {"allowed": allowed, "external": external}[aid]),
                      distillation_sources={"allowed": "source-a", "external": "source-a"})
    forged_evidence = runs.bind_checkpoint_evidence([(external, "source-a")])
    forged = (f'model text\nXSkill-Run: {runs.run()["run_id"]}\n'
              f'XSkill-Atoms: ["external"]\nXSkill-Evidence: {json.dumps(forged_evidence)}')
    with library_write_guard(runs.skill_dir, runs.policy(), db_path=runs.db_path):
        with agent_tools.use_agent_tool_context(context):
            with agent_tools.use_skill_edit_batch(target.name, ["allowed"]):
                message = agent_tools._run_commit_message(forged)
        commit_text(runs, target, message)
    runs.reconcile_checkpoints()
    assert runs.atom_record(external, "source-a")["state"] == "accepted"
    assert runs.atom_record(allowed, "source-a")["state"] == "committed"
    assert message.count("XSkill-Run:") == 1
    assert message.count("XSkill-Evidence:") == 1
    assert "XSkill-Atoms:" not in message


@pytest.mark.parametrize("change", ["content", "source"])
def test_bound_checkpoint_acks_original_snapshot_not_current_row(env, change):
    runs, target, context = env
    old = SimpleNamespace(atom_id="same", content="old evidence")
    new = SimpleNamespace(atom_id="same", content="new evidence" if change == "content" else old.content)
    source = "source-a" if change == "content" else "source-b"
    runs.record_atom(old, "accepted", source_key="source-a")
    context = replace(context, atom_store=SimpleNamespace(load=lambda aid: old),
                      distillation_sources={"same": "source-a"})
    with library_write_guard(runs.skill_dir, runs.policy(), db_path=runs.db_path):
        C.save_candidates(target, {"candidates": [{"atom_id": "same", "weightscore": 10}]})
        with agent_tools.use_agent_tool_context(context):
            with agent_tools.use_skill_edit_batch(target.name, ["same"]):
                runs.record_atom(new, "pending", source_key=source)
                (target / "SKILL.md").write_text(
                    "---\nname: chosen\ndescription: Diagnose failures\n---\nCompare actual diagnostic output.\n", encoding="utf-8")
                result = agent_tools.commit_baby.entrypoint("chosen", "consume original")
                assert not result.startswith("error:"), result
    assert runs.atom_record(new, source)["state"] == "pending"
    assert runs.atom_record(old, "source-a")["state"] == "committed"
    assert [item["atom_id"] for item in C.load_candidates(target)["candidates"]] == ["same"]
    # Reopening after commit-before-ack must recover the same evidence identity.
    runs.reconcile_checkpoints()
    assert runs.atom_record(new, source)["state"] == "pending"


def test_invalid_empty_baby_calls_repair_again_until_valid(env, tmp_path):
    runs, target, context = env
    commit_text(runs, target, "prior checkpoint")
    calls = []

    class Repair:
        def __init__(self, *, tools, **kwargs):
            self.write_file = next(tool.entrypoint for tool in tools if tool.name == "write_file")
        def run(self, message):
            calls.append(message)
            if len(calls) == 2:
                result = self.write_file(
                    "references/missing.md",
                    "Compare observed failures with the diagnostic baseline.\n",
                )
                assert not result.startswith("error:"), result

    with library_write_guard(runs.skill_dir, runs.policy(), db_path=runs.db_path):
        (target / "SKILL.md").write_text(
            "---\nname: chosen\ndescription: Diagnose failures\n---\nRead [checks](references/missing.md) before comparing output.\n", encoding="utf-8")
        assert run_git(["add", "SKILL.md"], cwd=str(target))[0] == 0
        assert run_git(["commit", "-m", "last batch consumed"], cwd=str(target))[0] == 0
        C.save_candidates(target, {"candidates": []})
        editor = SkillEditAgent(target, None, Repair, {}, tmp_path)
        with agent_tools.use_agent_tool_context(context):
            assert editor.maybe_run() is False
            assert len(calls) == 1
            assert current_branch(str(target)) == "baby"
        restarted = DistillationStore(runs.db_path, runs.skill_dir)
        assert restarted.ensure_target() == target
        with agent_tools.use_agent_tool_context(replace(context, distillation_store=restarted)):
            assert SkillEditAgent(target, None, Repair, {}, tmp_path).maybe_run() is True
    assert len(calls) == 2
    assert "references/missing.md" in calls[-1]
    assert current_branch(str(target)) == "main"
    code, content, _ = run_git(["show", "main:references/missing.md"], cwd=str(target))
    assert code == 0 and "diagnostic baseline" in content
    assert C.load_candidates(target)["candidates"] == []


@pytest.mark.parametrize("change", ["content", "source"])
def test_recovery_acks_full_receipt_after_commit_before_ack(env, change):
    runs, target, context = env
    old = SimpleNamespace(atom_id="same", content="old evidence")
    new = SimpleNamespace(atom_id="same", content="new evidence" if change == "content" else old.content)
    source = "source-a" if change == "content" else "source-b"
    runs.record_atom(old, "accepted", source_key="source-a")
    context = replace(context, atom_store=SimpleNamespace(load=lambda aid: old),
                      distillation_sources={"same": "source-a"})
    with library_write_guard(runs.skill_dir, runs.policy(), db_path=runs.db_path):
        with agent_tools.use_agent_tool_context(context):
            with agent_tools.use_skill_edit_batch(target.name, ["same"]):
                message = agent_tools._run_commit_message("durable original checkpoint")
        commit_text(runs, target, message)
    runs.record_atom(new, "pending", source_key=source)
    assert runs.reconcile_checkpoints() == 1
    assert runs.atom_record(old, "source-a")["state"] == "committed"
    assert runs.atom_record(new, source)["state"] == "pending"
    assert runs.reconcile_checkpoints() == 0


def test_old_committed_receipt_cannot_ack_later_version(env):
    runs, target, context = env
    old = SimpleNamespace(atom_id="same", content="old evidence")
    runs.record_atom(old, "accepted", source_key="source-a")
    evidence = runs.bind_checkpoint_evidence([(old, "source-a")])
    commit_text(runs, target, f'checkpoint\n\nXSkill-Run: {runs.run()["run_id"]}\nXSkill-Evidence: {json.dumps(evidence)}')
    assert runs.reconcile_checkpoints() == 1
    new = SimpleNamespace(atom_id="same", content="new evidence")
    runs.record_atom(new, "pending", source_key="source-a")
    assert runs.reconcile_checkpoints() == 0
    assert runs.atom_record(new, "source-a")["state"] == "pending"


@pytest.mark.parametrize("trailer", ["run", "evidence"])
def test_duplicate_receipt_trailers_fail_closed(env, trailer):
    runs, target, context = env
    atom = SimpleNamespace(atom_id="same", content="old evidence")
    runs.record_atom(atom, "accepted", source_key="source-a")
    evidence = runs.bind_checkpoint_evidence([(atom, "source-a")])
    run_line = f'XSkill-Run: {runs.run()["run_id"]}'
    evidence_line = f'XSkill-Evidence: {json.dumps(evidence)}'
    message = "checkpoint\n\n" + run_line + "\n" + evidence_line
    message += "\n" + (run_line if trailer == "run" else evidence_line)
    commit_text(runs, target, message)
    with pytest.raises(RuntimeError, match="ambiguous"):
        runs.reconcile_checkpoints()
    assert runs.atom_record(atom, "source-a")["state"] == "accepted"


def test_bound_editor_rejects_evidence_changed_since_acceptance(env):
    runs, target, context = env
    old = SimpleNamespace(atom_id="same", content="old evidence")
    new = SimpleNamespace(atom_id="same", content="new unaccepted evidence")
    runs.record_atom(old, "accepted", source_key="source-a")
    context = replace(context, atom_store=SimpleNamespace(load=lambda aid: new),
                      distillation_sources={"same": "source-a"})
    with library_write_guard(runs.skill_dir, runs.policy(), db_path=runs.db_path):
        with agent_tools.use_agent_tool_context(context):
            with pytest.raises(RuntimeError, match="source/content"):
                with agent_tools.use_skill_edit_batch(target.name, ["same"]):
                    pytest.fail("unaccepted editor evidence was bound")
    assert runs.atom_record(old, "source-a")["state"] == "accepted"


def test_bound_editor_rejects_missing_evidence_store(env):
    runs, target, context = env
    with library_write_guard(runs.skill_dir, runs.policy(), db_path=runs.db_path):
        with agent_tools.use_agent_tool_context(context):
            with pytest.raises(RuntimeError, match="evidence store"):
                with agent_tools.use_skill_edit_batch(target.name, ["same"]):
                    pytest.fail("editor bound without an evidence store")


def test_context_snapshot_preserves_bound_evidence(env):
    runs, target, context = env
    atom = SimpleNamespace(atom_id="same", content="old evidence")
    runs.record_atom(atom, "accepted", source_key="source-a")
    context = replace(context, atom_store=SimpleNamespace(load=lambda aid: atom),
                      distillation_sources={"same": "source-a"})
    with library_write_guard(runs.skill_dir, runs.policy(), db_path=runs.db_path):
        with agent_tools.use_agent_tool_context(context):
            with agent_tools.use_skill_edit_batch(target.name, ["same"]):
                before = agent_tools._run_commit_message("checkpoint")
                saved = agent_tools.agent_tool_config.snapshot()
                agent_tools.agent_tool_config.restore(saved)
                assert agent_tools._run_commit_message("checkpoint") == before


@pytest.mark.parametrize("acknowledged", [False, True])
def test_recovery_cleans_receipt_candidates_after_crash(env, acknowledged, monkeypatch):
    runs, target, context = env
    atom = SimpleNamespace(atom_id="same", content="old evidence")
    runs.record_atom(atom, "accepted", source_key="source-a")
    evidence = runs.bind_checkpoint_evidence([(atom, "source-a")])
    with library_write_guard(runs.skill_dir, runs.policy(), db_path=runs.db_path):
        C.save_candidates(target, {"candidates": [{"atom_id": "same", "weightscore": 10}]})
    commit_text(runs, target, f'checkpoint\n\nXSkill-Run: {runs.run()["run_id"]}\nXSkill-Evidence: {json.dumps(evidence)}')
    sha = run_git(["rev-parse", "HEAD"], cwd=str(target))[1].strip()
    if acknowledged:
        runs.checkpoint_atoms(evidence, sha)
    real_remove = C.remove_candidates
    calls = []
    def interrupted_cleanup(*args, **kwargs):
        calls.append(True)
        raise OSError("simulate crash after database acknowledgement")
    monkeypatch.setattr(C, "remove_candidates", interrupted_cleanup)
    with pytest.raises(OSError, match="simulate crash"):
        runs.reconcile_checkpoints()
    assert calls == [True]
    assert runs.atom_record(atom, "source-a")["state"] == "committed"
    assert len(C.load_candidates(target)["candidates"]) == 1
    monkeypatch.setattr(C, "remove_candidates", real_remove)
    assert runs.reconcile_checkpoints() == 0
    assert C.load_candidates(target)["candidates"] == []


@pytest.mark.parametrize("state", ["pending", "accepted"])
def test_recovery_preserves_candidate_with_other_unproven_identity(env, state):
    runs, target, context = env
    old = SimpleNamespace(atom_id="same", content="old evidence")
    new = SimpleNamespace(atom_id="same", content="new evidence")
    runs.record_atom(old, "accepted", source_key="source-a")
    evidence = runs.bind_checkpoint_evidence([(old, "source-a")])
    with library_write_guard(runs.skill_dir, runs.policy(), db_path=runs.db_path):
        C.save_candidates(target, {"candidates": [{"atom_id": "same", "weightscore": 10}]})
    commit_text(runs, target, f'checkpoint\n\nXSkill-Run: {runs.run()["run_id"]}\nXSkill-Evidence: {json.dumps(evidence)}')
    runs.record_atom(new, state, source_key="source-b")
    assert runs.reconcile_checkpoints() == 1
    assert runs.atom_record(new, "source-b")["state"] == state
    assert len(C.load_candidates(target)["candidates"]) == 1


def test_existing_target_candidates_adopt_unique_actual_evidence_for_this_run(env):
    runs, target, context = env
    atom = SimpleNamespace(atom_id="preexisting", content="existing target evidence")
    context = replace(context, atom_store=SimpleNamespace(load=lambda aid: atom),
                      distillation_sources={"preexisting": "source-a"})
    with library_write_guard(runs.skill_dir, runs.policy(), db_path=runs.db_path):
        C.save_candidates(target, {"candidates": [{"atom_id": "preexisting", "weightscore": 8}]})
        with agent_tools.use_agent_tool_context(context):
            with agent_tools.use_skill_edit_batch(target.name, ["preexisting"]):
                message = agent_tools._run_commit_message("adopt preexisting target evidence")
    row = runs.atom_record(atom, "source-a")
    assert row["state"] == "accepted"
    assert row["weightscore"] == 8
    commit_text(runs, target, message)
    assert runs.reconcile_checkpoints() == 1
    assert runs.atom_record(atom, "source-a")["state"] == "committed"
    assert C.load_candidates(target)["candidates"] == []


@pytest.mark.parametrize("concurrent_new", [False, True])
def test_main_final_cleanup_preserves_new_pending_evidence(env, tmp_path, monkeypatch, concurrent_new):
    runs, target, context = env
    from xskill.skill.git import commit_baby_to_main_branch
    with library_write_guard(runs.skill_dir, runs.policy(), db_path=runs.db_path):
        (target / "SKILL.md").write_text("---\nname: chosen\ndescription: Diagnose failures\n---\nCompare diagnostic output.\n", encoding="utf-8")
        assert commit_baby_to_main_branch(str(target), "initial useful version")
    runs.configure("update_only", "chosen")
    old = SimpleNamespace(atom_id="same", content="old evidence")
    new = SimpleNamespace(atom_id="same", content="new evidence")
    runs.record_atom(old, "accepted", source_key="source-a")
    context = replace(context, distillation_policy=runs.policy(),
                      atom_store=SimpleNamespace(load=lambda aid: old),
                      distillation_sources={"same": "source-a"})
    editor = SkillEditAgent(target, None, lambda **_: None, {}, tmp_path)
    monkeypatch.setattr(editor, "_main_has_ux_score", lambda: True)
    def publish_old(*args, **kwargs):
        bound = agent_tools.current_agent_tool_context()
        evidence = [dict(item) for item in bound.skill_edit_batch_evidence]
        if not concurrent_new:
            runs.record_atom(new, "pending", source_key="source-a")
        path = target / "SKILL.md"
        path.write_text(path.read_text(encoding="utf-8") + "\nA longer diagnostic explanation.\n", encoding="utf-8")
        runs.checkpoint_atoms(evidence, "old-version-committed")
        return True
    monkeypatch.setattr(editor, "_run_bound", publish_old)
    from concurrent.futures import ThreadPoolExecutor
    from xskill.skill.git import _repo_lock_for, skill_repo_lock

    def competing_writer_can_lock():
        lock = _repo_lock_for(target)
        acquired = lock.acquire(blocking=False)
        if acquired:
            lock.release()
        return acquired

    def add_new_candidate():
        with library_write_guard(runs.skill_dir, runs.policy(), db_path=runs.db_path):
            with skill_repo_lock(target):
                runs.record_atom(new, "pending", source_key="source-a")
                C.save_candidates(target, {"candidates": [{"atom_id": "same", "weightscore": 9}]})

    real_ids = runs.checkpoint_candidate_ids
    with ThreadPoolExecutor(max_workers=1) as pool:
        def check_cleanup_lock(evidence):
            ids = real_ids(evidence)
            # Probe exactly between deciding which IDs are removable and
            # removing them. The competing writer must see the actual lock.
            assert not pool.submit(competing_writer_can_lock).result(timeout=30)
            return ids

        if concurrent_new:
            monkeypatch.setattr(runs, "checkpoint_candidate_ids", check_cleanup_lock)
        with library_write_guard(runs.skill_dir, runs.policy(), db_path=runs.db_path):
            C.save_candidates(target, {"candidates": [{"atom_id": "same", "weightscore": 10}]})
            with agent_tools.use_agent_tool_context(context):
                assert editor.maybe_run() is True
        if concurrent_new:
            pool.submit(add_new_candidate).result(timeout=60)
    assert len(C.load_candidates(target)["candidates"]) == 1
    assert runs.atom_record(new, "source-a")["state"] == "pending"


def test_auto_with_inactive_distillation_store_keeps_candidate_consumption(env):
    runs, target, context = env
    runs.configure("auto")
    context = replace(context, distillation_policy=runs.policy())
    C.save_candidates(target, {"candidates": [{"atom_id": "same", "weightscore": 10}]})
    with agent_tools.use_agent_tool_context(context):
        with agent_tools.use_skill_edit_batch(target.name, ["same"]):
            (target / "SKILL.md").write_text("---\nname: chosen\ndescription: Diagnose failures\n---\nCompare diagnostic output.\n", encoding="utf-8")
            result = agent_tools.commit_baby.entrypoint("chosen", "auto checkpoint")
    assert not result.startswith("error:"), result
    assert C.load_candidates(target)["candidates"] == []
    assert "XSkill-" not in run_git(["log", "-1", "--format=%B"], cwd=str(target))[1]


@pytest.mark.parametrize("late_state", ["pending", "accepted", "failed"])
def test_late_cluster_result_cannot_reopen_committed_identity(env, late_state):
    runs, _target, _context = env
    original = SimpleNamespace(atom_id="same", content="committed original evidence")
    continued = SimpleNamespace(atom_id="same", content="new pending evidence")
    runs.record_atom(original, "accepted", source_key="source-a", weightscore=8)
    evidence = runs.bind_checkpoint_evidence([(original, "source-a")])
    assert runs.checkpoint_atoms(evidence, "original-commit") == 1
    runs.record_atom(continued, "pending", source_key="source-a")
    runs.record_atom(original, late_state, source_key="source-a", reason="late result")
    original_record = runs.atom_record(original, "source-a")
    assert original_record["state"] == "committed"
    assert original_record["checkpoint_sha"] == "original-commit"
    assert original_record["candidate_applied"] == 1
    assert original_record["weightscore"] == 8
    assert runs.atom_record(continued, "source-a")["state"] == "pending"


def test_bound_editor_rejects_store_returning_a_different_atom_id(env):
    runs, target, context = env
    different = SimpleNamespace(atom_id="different", content="evidence outside the batch")
    runs.record_atom(different, "accepted", source_key="source-a")
    context = replace(context, atom_store=SimpleNamespace(load=lambda aid: different),
                      distillation_sources={"requested": "source-a"})
    with library_write_guard(runs.skill_dir, runs.policy(), db_path=runs.db_path):
        with agent_tools.use_agent_tool_context(context):
            with pytest.raises(RuntimeError, match="source/content"):
                with agent_tools.use_skill_edit_batch(target.name, ["requested"]):
                    pytest.fail("unrelated atom identity was bound")


@pytest.mark.parametrize("bad_evidence", ["{}", '""'])
def test_checkpoint_receipt_requires_json_list(env, bad_evidence):
    runs, target, _context = env
    commit_text(runs, target, f'checkpoint\n\nXSkill-Run: {runs.run()["run_id"]}\nXSkill-Evidence: {bad_evidence}')
    with pytest.raises(RuntimeError, match="checkpoint receipt"):
        runs.reconcile_checkpoints()


@pytest.mark.parametrize("new_staging", [False, True])
def test_recovery_refuses_unfinished_jam_but_allows_new_staging(env, new_staging):
    runs, target, _context = env
    from xskill.skill.git import commit_baby_to_main_branch
    with library_write_guard(runs.skill_dir, runs.policy(), db_path=runs.db_path):
        (target / "SKILL.md").write_text("---\nname: chosen\ndescription: Diagnose failures\n---\nCompare diagnostic output.\n", encoding="utf-8")
        assert commit_baby_to_main_branch(str(target), "initial useful version")
    runs.configure("update_only", "chosen")
    atom = SimpleNamespace(atom_id="same", content="jam evidence")
    runs.record_atom(atom, "accepted", source_key="source-a")
    evidence = runs.bind_checkpoint_evidence([(atom, "source-a")])
    message = f'jam checkpoint\n\nXSkill-Run: {runs.run()["run_id"]}\nXSkill-Evidence: {json.dumps(evidence)}'
    with library_write_guard(runs.skill_dir, runs.policy(), db_path=runs.db_path):
        C.save_candidates(target, {"candidates": [{"atom_id": "same", "weightscore": 10}]})
        if new_staging:
            commit_text(runs, target, message)
        assert run_git(["checkout", "-b", "staging"], cwd=str(target))[0] == 0
        commit_text(runs, target, "staging work")
        assert run_git(["checkout", "main"], cwd=str(target))[0] == 0
        if not new_staging:
            commit_text(runs, target, message)
    staging_sha = run_git(["rev-parse", "staging"], cwd=str(target))[1].strip()
    if new_staging:
        assert runs.reconcile_checkpoints() == 1
        assert runs.atom_record(atom, "source-a")["state"] == "committed"
        assert C.load_candidates(target)["candidates"] == []
    else:
        with pytest.raises(RuntimeError, match="staging|jam"):
            runs.reconcile_checkpoints()
        assert runs.atom_record(atom, "source-a")["state"] == "accepted"
        assert len(C.load_candidates(target)["candidates"]) == 1
    assert run_git(["rev-parse", "staging"], cwd=str(target))[1].strip() == staging_sha


@pytest.mark.parametrize("already_accepted", [False, True])
def test_restricted_cluster_adopts_existing_target_candidate_before_classification(env, tmp_path, already_accepted):
    runs, target, _context = env
    from xskill.pipeline.atom import AtomTask, AtomTaskStore
    from xskill.pipeline.distillation import source_key_for
    from xskill.pipeline.runner import process_atom_batch
    from xskill.skill.git import commit_baby_to_main_branch
    with library_write_guard(runs.skill_dir, runs.policy(), db_path=runs.db_path):
        (target / "SKILL.md").write_text("---\nname: chosen\ndescription: Diagnose failures\n---\nCompare diagnostic output.\n", encoding="utf-8")
        assert commit_baby_to_main_branch(str(target), "initial useful version")
    runs.configure("update_only", "chosen")
    atoms = AtomTaskStore(tmp_path / "history")
    for aid in ("preexisting", "unrelated"):
        atoms.save(AtomTask(atom_id=aid, traj_id="traj-test", offset_start=1,
                            offset_end=3, intent=aid, summary=aid, raw_segment="Evidence"))
    calls = []
    def factory(*, tools, **kwargs):
        skip = next(tool.entrypoint for tool in tools if tool.name == "skip_atom")
        def run(prompt):
            calls.append(prompt)
            for aid in ("preexisting", "unrelated"):
                if f"atom_id:   {aid}" in prompt:
                    result = skip(aid, "not relevant to this target")
                    assert not result.startswith("error:"), result
            return SimpleNamespace(content="classification complete")
        return SimpleNamespace(run=run)
    with library_write_guard(runs.skill_dir, runs.policy(), db_path=runs.db_path):
        C.save_candidates(target, {"candidates": [{"atom_id": "preexisting", "weightscore": 8}]})
        if already_accepted:
            # The editor may adopt this candidate after the scanner queued it.
            runs.record_atom(atoms.load("preexisting"), "accepted", source_key=source_key_for(atoms.root), weightscore=8)
        results = process_atom_batch(
            atom_ids=["preexisting", "unrelated"], config={}, skill_dir=runs.skill_dir,
            store=atoms, embed_client=None, agno_agent_factory=factory, db_path=runs.db_path,
            distillation_store=runs, distillation_policy=runs.policy(),
        )
    assert [(item["action"], item["weightscore"]) for item in results] == [("clustered", 8), ("skipped", None)]
    assert len(calls) == 1 and "atom_id:   preexisting" not in calls[0]
    source = source_key_for(atoms.root)
    assert runs.atom_record(atoms.load("preexisting"), source)["state"] == "accepted"
    assert runs.atom_record(atoms.load("unrelated"), source)["state"] == "skipped"
    assert C.load_candidates(target)["candidates"] == [{"atom_id": "preexisting", "weightscore": 8}]


def test_restricted_editor_accepts_committed_reference_only_change(env, tmp_path, monkeypatch):
    import os
    from xskill.skill.git import commit_baby_to_main_branch
    runs, target, context = env
    skill_md = target / "SKILL.md"
    reference = target / "references" / "checks.md"
    with library_write_guard(runs.skill_dir, runs.policy(), db_path=runs.db_path):
        skill_md.write_text("---\nname: chosen\ndescription: Diagnose failures\n---\nRead [checks](references/checks.md) before comparing output.\n", encoding="utf-8")
        reference.write_text("Original diagnostic check.\n", encoding="utf-8")
        assert commit_baby_to_main_branch(str(target), "initial useful version")
    runs.configure("update_only", "chosen")
    atom = SimpleNamespace(atom_id="one", content="new reference check")
    runs.record_atom(atom, "accepted", source_key="source-a")
    context = replace(context, distillation_policy=runs.policy(),
                      atom_store=SimpleNamespace(load=lambda aid: atom),
                      distillation_sources={"one": "source-a"})
    original_stat = skill_md.stat()
    def factory(*, tools, **kwargs):
        commit = next(tool.entrypoint for tool in tools if tool.name == "commit_to_staging")
        def run(prompt):
            reference.write_text("New diagnostic check.\n", encoding="utf-8")
            result = commit("chosen", "improve referenced diagnostic check")
            assert not result.startswith("error:"), result
            os.utime(skill_md, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
        return SimpleNamespace(run=run)
    editor = SkillEditAgent(target, None, factory, {}, tmp_path)
    monkeypatch.setattr(editor, "_main_has_ux_score", lambda: True)
    with library_write_guard(runs.skill_dir, runs.policy(), db_path=runs.db_path):
        C.save_candidates(target, {"candidates": [{"atom_id": "one", "weightscore": 10}]})
        with agent_tools.use_agent_tool_context(context):
            assert editor.maybe_run() is True
    assert skill_md.stat().st_mtime_ns == original_stat.st_mtime_ns
    assert C.load_candidates(target)["candidates"] == []
    code, contents, _ = run_git(["show", "staging:references/checks.md"], cwd=str(target))
    assert code == 0 and "New diagnostic check." in contents
