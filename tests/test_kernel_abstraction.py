"""Algorithm-kernel contract, discovery, publication, and evaluation tests."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from xskill import canary
from xskill.config import kernel_config
from xskill.kernels.base import KernelRunResult, SkillSubmission
from xskill.kernels.catalog import KernelCatalog
from xskill.kernels.context import SkillPublisher, SkillReader, TrajectoryReader
from xskill.kernels.runtime import (
    KernelEvaluationStore,
    KernelRuntime,
    kernel_run_interval,
)
from xskill.pipeline.registry import (
    discover_trajectories,
    mark_skill_used,
    record_canary_decision,
    register_dir,
)
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
    canonical = kernel_config(
        {
            "kernel": {
                "kernel_id": "rule-based-demo",
                "kernels_path": "provider-kernels",
            }
        },
        xskill_home=tmp_path,
    )
    assert canonical == {
        "active": "rule-based-demo",
        "plugin_dir": (tmp_path / "provider-kernels").resolve(),
    }
    with pytest.raises(ValueError, match="不能冲突"):
        kernel_config({
            "kernel": {"kernel_id": "native", "active": "rule-based-demo"},
        }, xskill_home=tmp_path)
    with pytest.raises(ValueError, match="不能冲突"):
        kernel_config({
            "kernel": {"kernels_path": "one", "plugin_dir": "two"},
        }, xskill_home=tmp_path)
    with pytest.raises(ValueError, match="kernel id"):
        kernel_config({"kernel": {"active": "../escape"}}, xskill_home=tmp_path)
    with pytest.raises(ValueError, match="mapping"):
        kernel_config({"kernel": "native"}, xskill_home=tmp_path)


def test_catalog_discovers_local_bridge_and_reports_import_failure(tmp_path):
    plugin_dir = tmp_path / "kernels"
    valid = plugin_dir / "local-test"
    valid.mkdir(parents=True)
    (valid / "kernel.py").write_text(
        "from xskill.kernels import BaseKernel, KernelMetadata, KernelRunResult\n"
        "class LocalKernel(BaseKernel):\n"
        "    metadata = KernelMetadata(id='local-test', name='Local', "
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
    assert catalog.create("local-test").metadata.id == "local-test"
    assert descriptors["broken"].available is False
    assert "dependency_that_does_not_exist" in descriptors["broken"].error


def test_runtime_injects_model_clients_config_path_environment_and_reuses_kernel(
    tmp_path, monkeypatch,
):
    monkeypatch.delenv("LLM_MODEL_NAME", raising=False)
    monkeypatch.delenv("EMBED_MODEL_NAME", raising=False)
    plugin_dir = tmp_path / "kernels"
    kernel_dir = plugin_dir / "model-probe"
    kernel_dir.mkdir(parents=True)
    (kernel_dir / "kernel.py").write_text(
        "import os\n"
        "from xskill.kernels import BaseKernel, KernelMetadata, KernelRunResult\n"
        "class ModelProbe(BaseKernel):\n"
        "    metadata = KernelMetadata(id='model-probe', name='Model Probe', "
        "version='1', description='test', triggers=('scheduled',))\n"
        "    def __init__(self): self.calls = 0\n"
        "    def run(self, context, run_interval=7):\n"
        "        self.calls += 1\n"
        "        return KernelRunResult(metrics={\n"
        "            'calls': self.calls,\n"
        "            'llm_model': context.llm.model,\n"
        "            'embedding_model': context.embedding.model,\n"
        "            'config_path': str(context.xskill_config_path),\n"
        "            'llm_env': os.environ.get('LLM_MODEL_NAME'),\n"
        "            'embed_env': os.environ.get('EMBED_MODEL_NAME'),\n"
        "        })\n"
        "KERNEL_CLASS = ModelProbe\n",
        encoding="utf-8",
    )
    config_path = tmp_path / "config.yaml"
    config = {
        "llm": {
            "base_url": "https://llm.invalid/v1",
            "model": "llm-test",
            "api_key": "llm-secret",
            "rate_limit": {"rpm": 60, "request_burst": 2},
        },
        "embedding": {
            "base_url": "https://embed.invalid/v1",
            "model": "embed-test",
            "api_key": "embed-secret",
            "rate_limit": {"max_inflight": 2},
        },
    }
    runtime = KernelRuntime(
        active_kernel="model-probe",
        catalog=KernelCatalog(plugin_dir=plugin_dir, xskill_home=tmp_path),
        skill_dir=tmp_path / "skills",
        registry_db_path=tmp_path / "registry.db",
        evaluation_store=KernelEvaluationStore(tmp_path / "runs.db"),
        xskill_config=config,
        xskill_config_path=config_path,
    )

    assert runtime.external_run_interval() == 7.0
    assert "LLM_MODEL_NAME" not in os.environ
    first = runtime.run_active(
        native_runner=lambda _invocation: pytest.fail("unexpected native kernel"),
    )[1]
    second = runtime.run_active(
        native_runner=lambda _invocation: pytest.fail("unexpected native kernel"),
    )[1]

    assert first.metrics == {
        "calls": 1,
        "llm_model": "llm-test",
        "embedding_model": "embed-test",
        "config_path": str(config_path.resolve()),
        "llm_env": "llm-test",
        "embed_env": "embed-test",
    }
    assert second.metrics["calls"] == 2
    assert "LLM_MODEL_NAME" not in os.environ


def test_kernel_run_interval_requires_a_positive_numeric_default():
    class LegacyKernel:
        def run(self, context):
            del context

    class MissingDefault:
        def run(self, context, run_interval):
            del context, run_interval

    class InvalidDefault:
        def run(self, context, run_interval=0):
            del context, run_interval

    assert kernel_run_interval(LegacyKernel()) == 30.0
    with pytest.raises(TypeError, match="default value"):
        kernel_run_interval(MissingDefault())
    with pytest.raises(ValueError, match="> 0"):
        kernel_run_interval(InvalidDefault())


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


def test_kernel_feedback_attributes_main_and_staging_to_their_owners(tmp_path):
    skill_dir = tmp_path / "skills"
    created = SkillPublisher(
        skill_dir=skill_dir,
        kernel_id="first-kernel",
        kernel_version="1",
        run_id="create",
    ).submit(SkillSubmission(
        name="shared-skill",
        skill_md=_skill_md("shared-skill", "main"),
    ))
    staged = SkillPublisher(
        skill_dir=skill_dir,
        kernel_id="second-kernel",
        kernel_version="2",
        run_id="update",
    ).submit(SkillSubmission(
        name="shared-skill",
        skill_md=_skill_md("shared-skill", "candidate"),
        base_commit_sha=created.commit_sha,
    ))
    skill_path = skill_dir / "shared-skill"
    canary.append_ux_score(
        skill_path,
        traj_id="main-feedback",
        skill_name="shared-skill",
        side="main",
        commit_sha=created.commit_sha,
        score=0.6,
        reasons="main",
    )
    canary.append_ux_score(
        skill_path,
        traj_id="staging-feedback",
        skill_name="shared-skill",
        side="staging",
        commit_sha=staged.commit_sha,
        score=0.9,
        reasons="candidate",
    )

    summaries = KernelEvaluationStore(
        tmp_path / "runs.db"
    ).summaries(
        kernel_ids=["first-kernel", "second-kernel"],
        skill_dir=skill_dir,
    )
    by_kernel = {row["kernel_id"]: row for row in summaries}
    assert by_kernel["first-kernel"]["skills_owned"] == 1
    assert by_kernel["first-kernel"]["avg_ux"] == 0.6
    assert by_kernel["second-kernel"]["skills_owned"] == 1
    assert by_kernel["second-kernel"]["avg_ux"] == 0.9


def test_trajectory_reader_exposes_registered_roots_for_batch_tools(tmp_path):
    trajectory_dir = tmp_path / "trajectories"
    trajectory_dir.mkdir()
    registry_db = tmp_path / "registry.db"
    watch_dir_id = register_dir(
        trajectory_dir,
        label="offline-input",
        ecosystem="offline-distill",
        db_path=registry_db,
    )
    trajectory = trajectory_dir / "traj_used.md"
    trajectory.write_text("## User\n\nregistered input\n", encoding="utf-8")
    discover_trajectories(watch_dir_id, trajectory_dir, db_path=registry_db)
    mark_skill_used(
        watch_dir_id,
        trajectory.name,
        "first-skill, second-skill, first-skill",
        "main",
        db_path=registry_db,
    )
    reader = TrajectoryReader(registry_db)
    directories = reader.directories()
    assert len(directories) == 1
    assert directories[0].path == trajectory_dir.resolve()
    assert directories[0].read_only is True
    resources = list(reader.iter())
    assert len(resources) == 1
    assert resources[0].used_skills == ("first-skill", "second-skill")


def test_trajectory_reader_falls_back_to_recursive_manual_root(tmp_path):
    trajectory_root = tmp_path / "manual-input"
    nested = trajectory_root / "client-a" / "sessions"
    nested.mkdir(parents=True)
    trajectory = nested / "traj_nested.md"
    trajectory.write_text("## User\n\nnested input\n", encoding="utf-8")
    trajectory.with_name(trajectory.name + ".meta").write_text(
        '{"success": true}', encoding="utf-8",
    )

    reader = TrajectoryReader(
        tmp_path / "registry.db",
        root=trajectory_root,
    )

    assert reader.root == trajectory_root.resolve()
    directories = reader.directories()
    assert len(directories) == 1
    assert directories[0].path == trajectory_root.resolve()
    assert directories[0].trajectory_count == 1
    resources = list(reader.iter())
    assert len(resources) == 1
    assert resources[0].id == "root:client-a/sessions/traj_nested.md"
    assert resources[0].path == trajectory.resolve()
    assert dict(resources[0].metadata) == {"success": True}


def test_kernel_runtime_exposes_default_and_explicit_trajectory_roots(tmp_path):
    plugin_dir = tmp_path / "kernels"
    kernel_dir = plugin_dir / "root-probe"
    kernel_dir.mkdir(parents=True)
    (kernel_dir / "kernel.py").write_text(
        "from xskill.kernels import BaseKernel, KernelMetadata, KernelRunResult\n"
        "class RootProbe(BaseKernel):\n"
        "    metadata = KernelMetadata(id='root-probe', name='Root Probe', "
        "version='1', description='test', triggers=('manual',))\n"
        "    def run(self, context):\n"
        "        return KernelRunResult(metrics={\n"
        "            'trajectory_root': str(context.trajectory_root),\n"
        "            'resource_paths': [str(x.path) for x in context.trajectories.iter()],\n"
        "        })\n"
        "KERNEL_CLASS = RootProbe\n",
        encoding="utf-8",
    )
    xskill_home = tmp_path / "home"
    catalog = KernelCatalog(plugin_dir=plugin_dir, xskill_home=xskill_home)

    def run_with_root(root=None):
        runtime = KernelRuntime(
            active_kernel="root-probe",
            catalog=catalog,
            skill_dir=tmp_path / "skills",
            registry_db_path=tmp_path / "registry.db",
            evaluation_store=KernelEvaluationStore(tmp_path / "runs.db"),
            trajectory_root=root,
        )
        return runtime.run_active(
            trigger="manual",
            native_runner=lambda _request: pytest.fail("unexpected native kernel"),
        )[1]

    default = run_with_root()
    assert default.metrics["trajectory_root"] == str(
        (xskill_home / "team_trajectories" / "clients").resolve()
    )

    explicit_root = tmp_path / "explicit-trajectories"
    nested = explicit_root / "train" / "project-a"
    nested.mkdir(parents=True)
    trajectory = nested / "traj_manual.md"
    trajectory.write_text("## User\n\nmanual\n", encoding="utf-8")
    explicit = run_with_root(explicit_root)
    assert explicit.metrics["trajectory_root"] == str(explicit_root.resolve())
    assert explicit.metrics["resource_paths"] == [str(trajectory.resolve())]


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

    skill_name = first.submitted_skills[0]
    skill_path = skill_dir / skill_name
    main_commit = canary.main_sha(skill_path)
    assert main_commit
    canary.append_ux_score(
        skill_path,
        traj_id="traj_feedback",
        skill_name=skill_name,
        side="main",
        commit_sha=main_commit,
        score=0.8,
        reasons="helpful",
    )
    record_canary_decision(
        skill=skill_name,
        action="promoted",
        main_avg=0.7,
        staging_avg=0.8,
        main_samples=5,
        staging_samples=5,
        age_days=1.0,
        main_sha=main_commit,
        staging_sha="candidate-sha",
        db_path=registry_db,
    )
    exported = store.export_report(
        kernel_id="rule-based-demo",
        skill_dir=skill_dir,
        registry_db_path=registry_db,
    )
    assert exported["summary"]["runs"] == 2
    assert exported["run_window"] == {
        "runs_limit": 500,
        "runs_returned": 2,
        "summary_limit": 500,
        "order": "started_at_desc",
    }
    assert exported["runs"][0]["kernel_id"] == "rule-based-demo"
    assert exported["skills"][0]["ux_events"][0]["score"] == 0.8
    assert exported["canary_decisions"][0]["action"] == "promoted"


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


def test_documented_demo_kernel_is_discoverable_runnable_and_idempotent(
    tmp_path,
):
    plugin_dir = tmp_path / "kernels"
    shutil.copytree(
        Path(__file__).parents[1]
        / "examples" / "kernels" / "your-demo-algo-kernel",
        plugin_dir / "your-demo-algo-kernel",
    )
    trajectory_dir = tmp_path / "trajectories"
    trajectory_dir.mkdir()
    (trajectory_dir / "traj_demo.md").write_text(
        "## User\n\nExercise the documented demo algorithm kernel.\n",
        encoding="utf-8",
    )
    (trajectory_dir / "traj_demo.md.meta").write_text(
        '{"kernel_demo": true, "success": true}',
        encoding="utf-8",
    )
    registry_db = tmp_path / "registry.db"
    register_dir(trajectory_dir, db_path=registry_db)
    store = KernelEvaluationStore(tmp_path / "kernel_runs.db")
    runtime = KernelRuntime(
        active_kernel="your-demo-algo-kernel",
        catalog=KernelCatalog(
            plugin_dir=plugin_dir,
            xskill_home=tmp_path,
        ),
        skill_dir=tmp_path / "skills",
        registry_db_path=registry_db,
        evaluation_store=store,
    )

    def native_must_not_run(_request):
        raise AssertionError("demo kernel must run through the public contract")

    descriptor, first = runtime.run_active(native_runner=native_must_not_run)
    assert descriptor.id == "your-demo-algo-kernel"
    assert len(first.processed_trajectory_ids) == 1
    assert len(first.submitted_skills) == 1
    assert descriptor.config_path.is_file()
    assert (descriptor.workspace / "processed.json").is_file()

    _, second = runtime.run_active(native_runner=native_must_not_run)
    assert second.processed_trajectory_ids == ()
    assert second.submitted_skills == ()
    assert len(store.list_runs(limit=10)) == 2
