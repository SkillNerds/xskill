"""Algorithm-kernel contract, discovery, publication, and evaluation tests."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from xskill import canary
from xskill.config import kernel_config
from xskill.kernels.base import KernelRunResult, SkillSubmission
from xskill.kernels.catalog import KernelCatalog
from xskill.kernels.context import SkillPublisher, SkillReader, TrajectoryReader
from xskill.kernels.runtime import KernelEvaluationStore, KernelRuntime
from xskill.pipeline.registry import register_dir
from xskill.skill.frontmatter import parse
from xskill.skill.git import current_branch


def _skill_md(name: str, body: str = "body") -> str:
    return (
        "---\n"
        f"name: {name}\n"
        f"description: {name} description\n"
        "metadata:\n"
        "  source_trajs: []\n"
        "---\n\n"
        f"# {name}\n\n{body}\n"
    )


def test_kernel_config_defaults_and_validates(tmp_path):
    default = kernel_config({}, xskill_home=tmp_path)
    assert default == {
        "active": "native",
        "plugin_dir": (tmp_path / "kernels").resolve(),
    }
    relative = kernel_config(
        {"kernel": {"active": "rule-based-demo", "plugin_dir": "plugins"}},
        xskill_home=tmp_path,
    )
    assert relative["plugin_dir"] == (tmp_path / "plugins").resolve()
    with pytest.raises(ValueError, match="kernel id"):
        kernel_config({"kernel": {"active": "../escape"}}, xskill_home=tmp_path)
    with pytest.raises(ValueError, match="mapping"):
        kernel_config({"kernel": "native"}, xskill_home=tmp_path)


def test_catalog_discovers_local_bridge_and_reports_import_failure(tmp_path):
    plugin_dir = tmp_path / "kernels"
    valid = plugin_dir / "local-test"
    valid.mkdir(parents=True)
    (valid / "kernel.py").write_text(
        "from xskill.kernels import BaseKernel, KernelManifest, KernelRunResult\n"
        "class LocalKernel(BaseKernel):\n"
        "    manifest = KernelManifest(id='local-test', name='Local', "
        "version='1', description='test')\n"
        "    def run(self, context):\n"
        "        return KernelRunResult(metrics={'ok': True})\n"
        "KERNEL_CLASS = LocalKernel\n",
        encoding="utf-8",
    )
    broken = plugin_dir / "broken"
    broken.mkdir()
    (broken / "kernel.py").write_text(
        "import dependency_that_does_not_exist\n", encoding="utf-8",
    )

    catalog = KernelCatalog(plugin_dir=plugin_dir, xskill_home=tmp_path)
    descriptors = {item.id: item for item in catalog.list()}

    assert {"native", "rule-based-demo", "local-test", "broken"} <= set(descriptors)
    assert descriptors["local-test"].available is True
    assert descriptors["local-test"].config_path == valid / "config.yaml"
    assert catalog.create("local-test").manifest.id == "local-test"
    assert descriptors["broken"].available is False
    assert "dependency_that_does_not_exist" in descriptors["broken"].error


def test_publisher_creates_main_then_stages_update_with_kernel_attribution(tmp_path):
    skill_dir = tmp_path / "skills"
    first = SkillPublisher(
        skill_dir=skill_dir,
        kernel_id="local-test",
        kernel_version="1",
        run_id="run-one",
    ).submit(SkillSubmission(
        name="example-skill",
        skill_md=_skill_md("example-skill", "first"),
        files={"scripts/helper.py": "print('ok')\n"},
        source_trajectory_ids=("1:traj_a.md",),
    ))
    skill_path = skill_dir / "example-skill"
    assert first.action == "created"
    assert current_branch(str(skill_path)) == "main"
    frontmatter, body = parse((skill_path / "SKILL.md").read_text(encoding="utf-8"))
    assert frontmatter["metadata"]["kernel"] == {
        "id": "local-test", "version": "1",
    }
    assert frontmatter["metadata"]["source_trajs"] == ["1:traj_a.md"]
    assert "first" in body

    second = SkillPublisher(
        skill_dir=skill_dir,
        kernel_id="local-test",
        kernel_version="2",
        run_id="run-two",
    ).submit(SkillSubmission(
        name="example-skill",
        skill_md=_skill_md("example-skill", "second"),
        files={"references/new.md": "new\n"},
        base_commit_sha=first.commit_sha,
    ))
    assert second.action == "staged"
    assert canary.has_staging(skill_path)
    # The main worktree remains the old version; the update is materialized in canary.
    assert "first" in (skill_path / "SKILL.md").read_text(encoding="utf-8")
    staged = skill_dir / ".canary" / "example-skill" / "SKILL.md"
    staged_frontmatter, staged_body = parse(staged.read_text(encoding="utf-8"))
    assert staged_frontmatter["metadata"]["kernel"] == {
        "id": "local-test", "version": "2",
    }
    assert "second" in staged_body
    assert (staged.parent / "references" / "new.md").read_text() == "new\n"
    assert not (staged.parent / "scripts" / "helper.py").exists()
    assert second.previous_commit_sha == first.commit_sha


def test_skill_checkout_is_editable_copy_and_submit_is_version_checked(tmp_path):
    skill_dir = tmp_path / "skills"
    created = SkillPublisher(
        skill_dir=skill_dir,
        kernel_id="local-test",
        kernel_version="1",
        run_id="create",
    ).submit(SkillSubmission(
        name="bundle-skill",
        skill_md=_skill_md("bundle-skill", "main"),
        files={"references/checklist.md": "old\n"},
    ))
    reader = SkillReader(skill_dir, workspace=tmp_path / "workspace")
    resource = reader.get("bundle-skill")
    assert resource.main_commit_sha == created.commit_sha
    assert resource.list_files() == ("SKILL.md", "references/checklist.md")
    checkout = reader.checkout("bundle-skill")
    assert skill_dir not in checkout.path.parents
    (checkout.path / "references" / "checklist.md").write_text(
        "new\n", encoding="utf-8",
    )
    published = SkillPublisher(
        skill_dir=skill_dir,
        kernel_id="local-test",
        kernel_version="2",
        run_id="update",
    ).submit_checkout(checkout, message="edit complete bundle")
    assert published.action == "staged"
    assert (skill_dir / "bundle-skill" / "references" / "checklist.md").read_text() == "old\n"
    assert (skill_dir / ".canary" / "bundle-skill" / "references" / "checklist.md").read_text() == "new\n"


def test_trajectory_reader_exposes_registered_roots_for_batch_tools(tmp_path):
    trajectory_dir = tmp_path / "trajectories"
    trajectory_dir.mkdir()
    registry_db = tmp_path / "registry.db"
    register_dir(
        trajectory_dir,
        label="benchmark",
        ecosystem="evaluation",
        db_path=registry_db,
    )
    reader = TrajectoryReader(registry_db)
    directories = reader.directories()
    assert len(directories) == 1
    assert directories[0].path == trajectory_dir.resolve()
    assert directories[0].read_only is True
    assert list(reader.iter()) == []


def test_publisher_rejects_traversal_before_creating_skill(tmp_path):
    publisher = SkillPublisher(
        skill_dir=tmp_path / "skills",
        kernel_id="local-test",
        kernel_version="1",
        run_id="run",
    )
    with pytest.raises(ValueError, match="unsafe"):
        publisher.submit(SkillSubmission(
            name="unsafe-skill",
            skill_md=_skill_md("unsafe-skill"),
            files={"../escape.txt": "no"},
        ))
    assert not (tmp_path / "skills" / "unsafe-skill").exists()


def test_rule_based_demo_runs_through_runtime_and_is_idempotent(tmp_path):
    plugin_dir = tmp_path / "kernels"
    skill_dir = tmp_path / "skills"
    trajectory_dir = tmp_path / "trajectories"
    trajectory_dir.mkdir()
    trajectory = trajectory_dir / "traj_demo.md"
    trajectory.write_text(
        "## User\n\nPlease demonstrate the kernel contract.\n",
        encoding="utf-8",
    )
    (trajectory_dir / "traj_demo.md.meta").write_text(
        '{"kernel_demo": true, "success": true}', encoding="utf-8",
    )
    registry_db = tmp_path / "registry.db"
    register_dir(trajectory_dir, db_path=registry_db)
    catalog = KernelCatalog(plugin_dir=plugin_dir, xskill_home=tmp_path)
    store = KernelEvaluationStore(tmp_path / "kernel_runs.db")
    runtime = KernelRuntime(
        active_kernel="rule-based-demo",
        catalog=catalog,
        skill_dir=skill_dir,
        registry_db_path=registry_db,
        evaluation_store=store,
    )

    def native_must_not_run(_request):
        raise AssertionError("native adapter should not run")

    descriptor, first = runtime.run_active(native_runner=native_must_not_run)
    assert descriptor.id == "rule-based-demo"
    assert len(first.processed_trajectory_ids) == 1
    assert len(first.submitted_skills) == 1
    assert (plugin_dir / "rule-based-demo" / "config.yaml").is_file()
    assert (plugin_dir / "rule-based-demo" / "workspace" / "processed.json").is_file()

    _, second = runtime.run_active(native_runner=native_must_not_run)
    assert second.processed_trajectory_ids == ()
    runs = store.list_runs(limit=10)
    assert len(runs) == 2
    assert all(run["status"] == "success" for run in runs)
    assert sum(run["output_count"] for run in runs) == 1
    summaries = store.summaries(
        kernel_ids=["native", "rule-based-demo"], skill_dir=skill_dir,
    )
    demo_summary = next(
        item for item in summaries if item["kernel_id"] == "rule-based-demo"
    )
    assert demo_summary["runs"] == 2
    assert demo_summary["skills_owned"] == 1


def test_native_runner_is_recorded_without_changing_adapter_result(tmp_path):
    runtime = KernelRuntime(
        active_kernel="native",
        catalog=KernelCatalog(
            plugin_dir=tmp_path / "kernels", xskill_home=tmp_path,
        ),
        skill_dir=tmp_path / "skills",
        registry_db_path=tmp_path / "registry.db",
        evaluation_store=KernelEvaluationStore(tmp_path / "runs.db"),
    )

    descriptor, result = runtime.run_active(
        native_runner=lambda _request: KernelRunResult(
            metrics={"polls": 1, "skills_edited": 0},
        ),
    )
    assert descriptor.id == "native"
    assert result.metrics["polls"] == 1
    assert runtime.evaluations.list_runs(limit=1)[0]["kernel_id"] == "native"


def test_documented_starter_kernel_is_discoverable_runnable_and_idempotent(
    tmp_path,
):
    plugin_dir = tmp_path / "kernels"
    shutil.copytree(
        Path(__file__).parents[1] / "examples" / "kernels" / "starter",
        plugin_dir / "starter",
    )
    trajectory_dir = tmp_path / "trajectories"
    trajectory_dir.mkdir()
    (trajectory_dir / "traj_starter.md").write_text(
        "## User\n\nExercise the documented starter kernel.\n",
        encoding="utf-8",
    )
    (trajectory_dir / "traj_starter.md.meta").write_text(
        '{"kernel_demo": true, "success": true}',
        encoding="utf-8",
    )
    registry_db = tmp_path / "registry.db"
    register_dir(trajectory_dir, db_path=registry_db)
    store = KernelEvaluationStore(tmp_path / "kernel_runs.db")
    runtime = KernelRuntime(
        active_kernel="starter",
        catalog=KernelCatalog(
            plugin_dir=plugin_dir,
            xskill_home=tmp_path,
        ),
        skill_dir=tmp_path / "skills",
        registry_db_path=registry_db,
        evaluation_store=store,
    )

    def native_must_not_run(_request):
        raise AssertionError("starter must run through the public contract")

    descriptor, first = runtime.run_active(native_runner=native_must_not_run)
    assert descriptor.id == "starter"
    assert len(first.processed_trajectory_ids) == 1
    assert len(first.submitted_skills) == 1
    assert descriptor.config_path.is_file()
    assert (descriptor.workspace / "processed.json").is_file()

    _, second = runtime.run_active(native_runner=native_must_not_run)
    assert second.processed_trajectory_ids == ()
    assert second.submitted_skills == ()
    assert len(store.list_runs(limit=10)) == 2
