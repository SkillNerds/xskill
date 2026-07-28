"""Persistent external-kernel process scheduling tests."""

from __future__ import annotations

from xskill.kernels.runtime import KernelEvaluationStore
from xskill.pipeline.atom import AtomTask, AtomTaskStore
from xskill.pipeline.registry import (
    discover_trajectories,
    register_dir,
    update_traj_status,
)


def _mark_ready(trajectory_path, *, registry_db):
    watch_dir_id = register_dir(
        trajectory_path.parent,
        label="client-a",
        db_path=registry_db,
    )
    discover_trajectories(
        watch_dir_id, trajectory_path.parent, db_path=registry_db,
    )
    update_traj_status(
        watch_dir_id,
        trajectory_path.name,
        status="split_done",
        db_path=registry_db,
    )
    AtomTaskStore(root=trajectory_path.parent).save(AtomTask(
        atom_id="atom_traj_one_0001",
        traj_id=trajectory_path.stem,
        offset_start=1,
        offset_end=3,
        intent="intent",
        summary="summary",
        raw_segment="first",
    ))
    return watch_dir_id


def test_kernel_host_reuses_process_and_reports_changed_trajectories(
    tmp_path, monkeypatch,
):
    from xskill import config as config_module
    from xskill._workers import run_kernel_host

    xskill_home = tmp_path / "xskill-home"
    plugin_dir = xskill_home / "kernels"
    kernel_dir = plugin_dir / "host-probe"
    kernel_dir.mkdir(parents=True)
    (kernel_dir / "kernel.py").write_text(
        "from xskill.kernels import BaseKernel, KernelMetadata, KernelRunResult\n"
        "class HostProbe(BaseKernel):\n"
        "    metadata = KernelMetadata(id='host-probe', name='Host Probe', "
        "version='1', description='test', triggers=('scheduled',))\n"
        "    def __init__(self): self.calls = 0\n"
        "    def run(self, context, run_interval=0.01):\n"
        "        self.calls += 1\n"
        "        return KernelRunResult(metrics={\n"
        "            'calls': self.calls,\n"
        "            'changed': list(context.invocation.changed_trajectory_ids),\n"
        "            'full_rebuild': context.invocation.full_rebuild,\n"
        "        })\n"
        "KERNEL_CLASS = HostProbe\n",
        encoding="utf-8",
    )
    config_path = xskill_home / "config.yaml"
    config_path.write_text(
        f"skill_dir: {xskill_home / 'skill'}\n"
        "kernel:\n"
        "  kernel_id: host-probe\n"
        f"  kernels_path: {plugin_dir}\n"
        "llm:\n"
        "  base_url: https://llm.invalid/v1\n"
        "  model: llm-test\n"
        "  api_key: test-only\n"
        "embedding:\n"
        "  base_url: https://embed.invalid/v1\n"
        "  model: embed-test\n"
        "  api_key: test-only\n",
        encoding="utf-8",
    )
    trajectory = (
        xskill_home / "team_trajectories" / "clients"
        / "client-a" / "traj_one.md"
    )
    trajectory.parent.mkdir(parents=True)
    trajectory.write_text("## User\n\nfirst\n", encoding="utf-8")
    registry_db = xskill_home / "registry.db"
    watch_dir_id = _mark_ready(trajectory, registry_db=registry_db)

    monkeypatch.setattr(config_module, "XSKILL_HOME", xskill_home)
    monkeypatch.setattr(config_module, "CONFIG_PATH", config_path)
    monkeypatch.setattr(config_module, "_config", {})
    monkeypatch.setattr(
        config_module,
        "get_registry_db_path",
        lambda **kwargs: registry_db,
    )

    class MutatingStopEvent:
        mutated = False

        @staticmethod
        def is_set():
            return False

        def wait(self, timeout):
            if timeout < 1.0 and not self.mutated:
                trajectory.write_text(
                    "## User\n\nfirst trajectory changed\n",
                    encoding="utf-8",
                )
                AtomTaskStore(root=trajectory.parent).save(AtomTask(
                    atom_id="atom_traj_one_0002",
                    traj_id=trajectory.stem,
                    offset_start=3,
                    offset_end=5,
                    intent="intent",
                    summary="summary",
                    raw_segment="first trajectory changed",
                ))
                self.mutated = True
            return False

    assert run_kernel_host(
        server=True,
        stop_event=MutatingStopEvent(),
        max_cycles=2,
    ) == 0

    runs = KernelEvaluationStore(
        xskill_home / "kernel_runs.db"
    ).list_runs(limit=10)
    assert len(runs) == 2
    runs_by_call = {run["metrics"]["calls"]: run for run in runs}
    resource_id = f"{watch_dir_id}:traj_one.md"
    assert runs_by_call[1]["metrics"] == {
        "calls": 1,
        "changed": [resource_id],
        "full_rebuild": True,
    }
    assert runs_by_call[2]["metrics"] == {
        "calls": 2,
        "changed": [resource_id],
        "full_rebuild": False,
    }
