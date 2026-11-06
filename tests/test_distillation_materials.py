"""Restricted routing must preserve original learning eligibility."""
from types import SimpleNamespace

import pytest

from xskill.pipeline.atom import AtomTask, AtomTaskStore
from xskill.pipeline.runner import process_atom_batch


def _atom(atom_id, *, clustered=False):
    return AtomTask(
        atom_id=atom_id, traj_id="traj-test", offset_start=1, offset_end=3,
        intent="debug an integration", summary="debug an integration",
        raw_segment="## User\nFix an API permission failure", clustered=clustered,
    )


def _setup(tmp_path):
    from xskill.pipeline.distillation import DistillationStore

    root = tmp_path / "skills"
    root.mkdir()
    store = DistillationStore(tmp_path / "registry.db", root)
    store.configure("create_one", skill="feishu-debug", description="Debug Feishu APIs")
    atoms = AtomTaskStore(tmp_path / "history")
    atoms.save(_atom("atom_good", clustered=True))
    atoms.save(_atom("atom_skip"))
    return root, store, atoms


class _Factory:
    def __init__(self, *, omit=False):
        self.omit = omit
        self.instructions = []

    def __call__(self, *, instructions, tools, **kwargs):
        self.instructions.extend(instructions)
        by_name = {t.name: t for t in tools}

        def run(_message):
            assert "score_task" not in by_name
            result = by_name["add_tasks_to_skill"].entrypoint(
                "feishu-debug", [{"atom_id": "atom_good", "weightscore": 5}],
            )
            assert not result.startswith("error:"), result
            if not self.omit:
                result = by_name["skip_atom"].entrypoint("atom_skip", "unrelated task")
                assert not result.startswith("error:"), result
            return SimpleNamespace(content="routed")

        return SimpleNamespace(run=run)


def test_restricted_batch_records_skip_without_global_consumption(tmp_path):
    root, runs, atoms = _setup(tmp_path)
    from xskill.skill.write_policy import library_write_guard, use_write_policy

    before = {a.atom_id: a.to_json() for a in atoms.all_atoms()}
    assert set(before) == {"atom_good", "atom_skip"}
    policy = runs.policy()
    factory = _Factory()
    with library_write_guard(root, policy, db_path=runs.db_path), use_write_policy(policy):
        runs.ensure_target()
        result = process_atom_batch(
            atom_ids=["atom_good", "atom_skip"], config={}, skill_dir=root,
            store=atoms, embed_client=None, agno_agent_factory=factory,
            db_path=runs.db_path, distillation_store=runs,
            distillation_policy=policy,
        )
    assert [r["action"] for r in result] == ["clustered", "skipped"]
    assert {a.atom_id: a.to_json() for a in atoms.all_atoms()} == before
    assert all(runs.processed(a, source_key=str(atoms.root.resolve())) for a in atoms.all_atoms())
    assert "Debug Feishu APIs" in "\n".join(factory.instructions)


def test_missing_explicit_result_remains_retryable(tmp_path):
    root, runs, atoms = _setup(tmp_path)
    from xskill.skill.write_policy import library_write_guard, use_write_policy

    policy = runs.policy()
    with library_write_guard(root, policy, db_path=runs.db_path), use_write_policy(policy):
        runs.ensure_target()
        with pytest.raises(RuntimeError, match="explicit|result|unhandled"):
            process_atom_batch(
                atom_ids=["atom_good", "atom_skip"], config={}, skill_dir=root,
                store=atoms, embed_client=None, agno_agent_factory=_Factory(omit=True),
                db_path=runs.db_path, distillation_store=runs,
                distillation_policy=policy,
            )
    assert not runs.processed(atoms.load("atom_skip"), source_key=str(atoms.root.resolve()))


def test_model_cannot_replace_distillation_authority_metadata(tmp_path):
    from xskill.agents import agent_tools
    from xskill.skill.write_policy import library_write_guard

    root, runs, atoms = _setup(tmp_path)
    policy = runs.policy()
    with library_write_guard(root, policy, db_path=runs.db_path):
        target = runs.ensure_target()
        marker = target / ".distillation-run.json"
        original = marker.read_bytes()
        context = agent_tools.create_agent_tool_context(
            skill_dir=root, atom_skill_dir=root, atom_store=atoms,
            distillation_policy=policy, distillation_store=runs,
        )
        with agent_tools.use_agent_tool_context(context):
            for name in (".distillation-control.json", ".distillation-run.json"):
                result = agent_tools.write_file.entrypoint(name, "{}")
                assert result.startswith("error:")
        assert marker.read_bytes() == original
        assert not (target / ".distillation-control.json").exists()
