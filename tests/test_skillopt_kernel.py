"""Tests for the example SkillOpt orchestration kernel."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import MappingProxyType, SimpleNamespace

import pytest


pytest.importorskip("skillopt_sleep")

_KERNEL_PATH = (
    Path(__file__).parents[1] / "examples" / "kernels" / "skillopt" / "kernel.py"
)
_SPEC = importlib.util.spec_from_file_location("xskill_example_skillopt", _KERNEL_PATH)
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)


def _skill_md(name: str, description: str = "Focused test procedures.") -> str:
    return (
        "---\n"
        f"name: {name}\n"
        f"description: {description}\n"
        "---\n\n"
        f"# {name}\n\nUse evidence-backed procedures.\n"
    )


def _write_state(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")


def test_router_tools_support_overlap_and_pending_seed(tmp_path):
    state_path = tmp_path / "state.json"
    seed_root = tmp_path / "new-seeds"
    _write_state(state_path, {
        "phase": "router",
        "valid_trajectory_ids": ["root:a", "root:b"],
        "existing_skills": ["existing-a", "existing-b"],
        "pending_skills": [],
        "new_skills": {},
        "associations": {},
        "max_targets": 8,
        "new_seed_root": str(seed_root),
    })
    store = _MODULE._KernelState(state_path)

    store.associate_skill(
        ["existing-a", "existing-b"],
        ["root:a"],
        "the same incident exercises both focused skills",
    )
    store.newskill(
        "focused-new",
        "Focused test procedures.",
        _skill_md("focused-new"),
        ["root:b"],
        {"references/checks.md": "# Checks\n"},
    )

    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["associations"]["existing-a"] == ["root:a"]
    assert state["associations"]["existing-b"] == ["root:a"]
    assert state["associations"]["focused-new"] == ["root:b"]
    assert (seed_root / "focused-new" / "SKILL.md").is_file()
    assert (seed_root / "focused-new" / "references" / "checks.md").is_file()


def test_router_tools_enforce_target_limit_and_known_sources(tmp_path):
    state_path = tmp_path / "state.json"
    _write_state(state_path, {
        "phase": "router",
        "valid_trajectory_ids": ["root:a"],
        "existing_skills": ["one", "two"],
        "pending_skills": [],
        "new_skills": {},
        "associations": {},
        "max_targets": 1,
        "new_seed_root": str(tmp_path / "seeds"),
    })
    store = _MODULE._KernelState(state_path)

    with pytest.raises(ValueError, match="target limit"):
        store.associate_skill(["one", "two"], ["root:a"], "both")
    with pytest.raises(ValueError, match="unknown trajectory"):
        store.associate_skill(["one"], ["root:missing"], "unknown")


def test_task_bank_has_source_safe_stable_splits(tmp_path):
    state_path = tmp_path / "state.json"
    bank = tmp_path / "bank"
    _write_state(state_path, {
        "phase": "tasks",
        "target": "focused",
        "valid_trajectory_ids": ["root:a", "root:b", "root:c"],
        "target_trajectory_ids": ["root:a", "root:b", "root:c"],
        "task_bank_root": str(bank),
    })
    store = _MODULE._KernelState(state_path)
    for task_id, sources in (
        ("a-1", ["root:a"]),
        ("a-2", ["root:a", "root:c"]),
        ("b-1", ["root:b"]),
    ):
        store.upsert_task(
            "focused",
            task_id,
            f"intent {task_id}",
            "context",
            "exact",
            f"answer-{task_id}",
            {},
            sources,
        )

    first = _MODULE._load_task_records(
        bank, "focused", project=tmp_path,
        validation_fraction=0.34, seed=42,
    )
    second = _MODULE._load_task_records(
        bank, "focused", project=tmp_path,
        validation_fraction=0.34, seed=42,
    )
    splits = {task.id: task.split for task in first}
    assert splits["a-1"] == splits["a-2"]
    assert splits["a-1"] != splits["b-1"]
    assert {task.id: task.split for task in second} == splits
    assert {task.split for task in first} == {"train", "val"}


def test_task_split_excludes_bridge_when_single_source_groups_are_available(tmp_path):
    bank = tmp_path / "bank" / "focused" / "tasks"
    bank.mkdir(parents=True)
    for task_id, sources in (
        ("a", ["root:a"]),
        ("b", ["root:b"]),
        ("bridge", ["root:a", "root:b"]),
    ):
        (bank / f"{task_id}.json").write_text(json.dumps({
            "id": task_id,
            "intent": task_id,
            "context_excerpt": "",
            "reference_kind": "exact",
            "reference": task_id,
            "judge": {},
            "source_trajectory_ids": sources,
            "status": "active",
        }))

    tasks = _MODULE._load_task_records(
        tmp_path / "bank", "focused", project=tmp_path,
        validation_fraction=0.5, seed=42,
    )

    assert {task.id for task in tasks} == {"a", "b"}
    assert {task.split for task in tasks} == {"train", "val"}


def test_task_tool_rejects_uncheckable_rule(tmp_path):
    state_path = tmp_path / "state.json"
    _write_state(state_path, {
        "phase": "tasks",
        "target": "focused",
        "valid_trajectory_ids": ["root:a"],
        "target_trajectory_ids": ["root:a"],
        "task_bank_root": str(tmp_path / "bank"),
    })
    store = _MODULE._KernelState(state_path)

    with pytest.raises(ValueError, match="unsupported rule check"):
        store.upsert_task(
            "focused", "bad", "intent", "", "rule", "",
            {"kind": "rule", "checks": [{"op": "run_arbitrary_code"}]},
            ["root:a"],
        )


def test_router_dynamic_tools_are_phase_scoped_and_dispatch(tmp_path):
    state_path = tmp_path / "router.json"
    _write_state(state_path, {
        "phase": "router",
        "valid_trajectory_ids": ["root:a"],
        "existing_skills": ["focused"],
        "pending_skills": [],
        "new_skills": {},
        "associations": {},
        "max_targets": 8,
        "new_seed_root": str(tmp_path / "seeds"),
    })

    tools = _MODULE._agent_tool_specs("router")
    result = _MODULE._dispatch_agent_tool(
        _MODULE._KernelState(state_path),
        "router",
        "associate_skill",
        {
            "skill_names": ["focused"],
            "trajectory_ids": ["root:a"],
            "rationale": "the trajectory directly exercises this skill",
        },
    )

    assert [tool["name"] for tool in tools] == ["newskill", "associate_skill"]
    assert result["targets"] == ["focused"]
    assert json.loads(state_path.read_text())["associations"] == {
        "focused": ["root:a"],
    }

    with pytest.raises(ValueError, match="not available"):
        _MODULE._dispatch_agent_tool(
            _MODULE._KernelState(state_path),
            "router",
            "upsert_task",
            {},
        )


def test_task_dynamic_tools_are_phase_scoped_and_dispatch(tmp_path):
    state_path = tmp_path / "focused.json"
    bank = tmp_path / "bank"
    _write_state(state_path, {
        "phase": "tasks",
        "target": "focused",
        "valid_trajectory_ids": ["root:a"],
        "target_trajectory_ids": ["root:a"],
        "task_bank_root": str(bank),
    })

    tools = _MODULE._agent_tool_specs("tasks")
    result = _MODULE._dispatch_agent_tool(
        _MODULE._KernelState(state_path),
        "tasks",
        "upsert_task",
        {
            "skill_name": "focused",
            "task_id": "check-outcome",
            "intent": "Check the recorded outcome.",
            "context_excerpt": "The run completed.",
            "reference_kind": "rule",
            "reference": "",
            "judge": {
                "kind": "rule",
                "checks": [{"op": "contains", "value": "completed"}],
            },
            "source_trajectory_ids": ["root:a"],
        },
    )

    assert [tool["name"] for tool in tools] == ["upsert_task", "retire_task"]
    assert result["status"] == "active"
    task = json.loads(
        (bank / "focused" / "tasks" / "check-outcome.json").read_text()
    )
    assert task["intent"] == "Check the recorded outcome."
    assert task["judge"]["checks"][0]["op"] == "contains"


def test_sleep_cycle_injects_backend_for_pypi_020_api(monkeypatch):
    sentinel_backend = object()
    original_resolver = object()
    observed = {}

    def old_run_sleep_cycle(config, *, seed_tasks=None, dry_run=False):
        observed["backend"] = _MODULE.sleep_cycle.get_backend("ignored")
        observed["tasks"] = seed_tasks
        observed["dry_run"] = dry_run
        return "outcome"

    monkeypatch.setattr(
        _MODULE.sleep_cycle, "run_sleep_cycle", old_run_sleep_cycle,
    )
    monkeypatch.setattr(
        _MODULE.sleep_cycle, "get_backend", original_resolver,
    )

    outcome = _MODULE._invoke_sleep_cycle(
        object(), tasks=["task"], backend=sentinel_backend,
    )

    assert outcome == "outcome"
    assert observed == {
        "backend": sentinel_backend,
        "tasks": ["task"],
        "dry_run": False,
    }
    assert _MODULE.sleep_cycle.get_backend is original_resolver


def test_real_sampler_is_balanced_redacted_and_reproducible(tmp_path):
    source = tmp_path / "server" / "clients"
    for client in ("client-a", "client-b"):
        for index in range(3):
            trajectory = source / client / "sessions" / f"traj_{index}.md"
            trajectory.parent.mkdir(parents=True, exist_ok=True)
            trajectory.write_text(
                f"## User\ncase {index}\napi_key=super-secret-{index}\n",
                encoding="utf-8",
            )
            trajectory.with_suffix(".json").write_text(
                json.dumps({"case": index, "token": f"secret-{index}"}),
                encoding="utf-8",
            )

    first = _MODULE._sample_server_trajectories(
        source, tmp_path / "sample-a", per_client=2, seed=42,
        min_bytes=0, max_bytes=10_000,
    )
    second = _MODULE._sample_server_trajectories(
        source, tmp_path / "sample-b", per_client=2, seed=42,
        min_bytes=0, max_bytes=10_000,
    )

    assert first["client_counts"] == {"client-a": 2, "client-b": 2}
    assert [item["path"] for item in first["trajectories"]] == [
        item["path"] for item in second["trajectories"]
    ]
    sampled_text = next((tmp_path / "sample-a" / "clients").rglob("traj_*.md")).read_text()
    assert "super-secret" not in sampled_text
    assert "[REDACTED]" in sampled_text
    assert first["trajectories"][0]["source_sha256"]
    assert first["trajectories"][0]["sample_sha256"]


def test_bridge_rejects_legacy_single_target_config(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text("skill_name: old-target\n", encoding="utf-8")

    with pytest.raises(ValueError, match="legacy single-target"):
        _MODULE.SkillOptKernel._load_config(config)


def test_copied_agent_toml_supplies_role_instructions(tmp_path):
    _MODULE._copy_agent_definitions(tmp_path)

    import tomllib

    generated = tomllib.loads(
        (tmp_path / ".codex" / "agents" / "skill-router.toml").read_text()
    )
    assert generated["name"] == "skill-router"
    assert "model" not in generated
    assert "routing phase" in _MODULE._agent_instructions(tmp_path, "skill-router")


def test_codex_agent_uses_app_server_dynamic_tools_without_action_files(
    monkeypatch,
    tmp_path,
):
    workspace = tmp_path / "agent"
    workspace.mkdir()
    state_path = tmp_path / "router.json"
    _write_state(state_path, {
        "phase": "router",
        "valid_trajectory_ids": ["root:a"],
        "existing_skills": ["focused"],
        "pending_skills": [],
        "new_skills": {},
        "associations": {},
        "max_targets": 8,
        "new_seed_root": str(tmp_path / "seeds"),
    })
    capture_path = tmp_path / "capture.json"
    fake_codex = tmp_path / "fake-codex"
    fake_codex.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "seen = {'argv': sys.argv[1:]}\n"
        "def send(value):\n"
        "    sys.stdout.write(json.dumps(value) + '\\n')\n"
        "    sys.stdout.flush()\n"
        "for line in sys.stdin:\n"
        "    message = json.loads(line)\n"
        "    method = message.get('method')\n"
        "    if method == 'initialize':\n"
        "        seen['initialize'] = message['params']\n"
        "        send({'id': message['id'], 'result': {'userAgent': 'fake'}})\n"
        "    elif method == 'initialized':\n"
        "        continue\n"
        "    elif method == 'thread/start':\n"
        "        seen['thread'] = message['params']\n"
        "        send({'id': message['id'], 'result': {\n"
        "            'thread': {'id': 'thread-1'},\n"
        "        }})\n"
        "    elif method == 'turn/start':\n"
        "        seen['turn'] = message['params']\n"
        "        send({'id': message['id'], 'result': {\n"
        "            'turn': {'id': 'turn-1', 'status': 'inProgress'},\n"
        "        }})\n"
        "        send({'method': 'item/tool/call', 'id': 40, 'params': {\n"
        "            'threadId': 'thread-1',\n"
        "            'turnId': 'turn-1',\n"
        "            'callId': 'call-1',\n"
        "            'namespace': None,\n"
        "            'tool': 'associate_skill',\n"
        "            'arguments': {\n"
        "                'skill_names': ['focused'],\n"
        "                'trajectory_ids': ['root:a'],\n"
        "                'rationale': 'direct evidence',\n"
        "            },\n"
        "        }})\n"
        "    elif message.get('id') == 40:\n"
        "        seen['tool_response'] = message\n"
        "        Path(os.environ['CAPTURE_PATH']).write_text(\n"
        "            json.dumps(seen), encoding='utf-8')\n"
        "        send({'method': 'turn/completed', 'params': {\n"
        "            'threadId': 'thread-1',\n"
        "            'turn': {'id': 'turn-1', 'status': 'completed'},\n"
        "        }})\n",
        encoding="utf-8",
    )
    fake_codex.chmod(0o755)
    monkeypatch.setenv("CAPTURE_PATH", str(capture_path))

    config = _MODULE.SkillOptKernel._load_config(tmp_path / "missing.yaml")
    config["codex_path"] = str(fake_codex)
    config["codex_provider"] = ""

    _MODULE._run_codex_agent(
        config,
        workspace=workspace,
        state_path=state_path,
        phase="router",
    )

    captured = json.loads(capture_path.read_text())
    arguments = captured["argv"]
    assert arguments[:2] == ["app-server", "--stdio"]
    assert captured["initialize"]["capabilities"]["experimentalApi"] is True
    assert captured["thread"]["sandbox"] == "read-only"
    assert captured["thread"]["approvalPolicy"] == "never"
    assert captured["thread"]["ephemeral"] is True
    assert "routing phase" in captured["thread"]["developerInstructions"]
    assert [
        tool["name"] for tool in captured["thread"]["dynamicTools"]
    ] == ["newskill", "associate_skill"]
    assert "successful tool call" in captured["turn"]["input"][0]["text"]
    assert captured["tool_response"]["result"]["success"] is True
    assert json.loads(state_path.read_text())["associations"] == {
        "focused": ["root:a"],
    }
    assert "--output-schema" not in arguments
    assert not any("mcp_servers." in value for value in arguments)
    assert not (workspace / "agent-actions.json").exists()
    assert not (workspace / "codex-output-schema.json").exists()


def test_run_result_stays_provider_neutral(monkeypatch, tmp_path):
    from xskill.kernels import KernelRunResult, TrajectoryResource

    trajectory_root = tmp_path / "trajectories"
    trajectory_root.mkdir()
    resources = []
    for index in range(2):
        path = trajectory_root / f"traj_{index}.md"
        path.write_text(f"## User\nrequest {index}\n", encoding="utf-8")
        resources.append(TrajectoryResource(
            id=f"root:traj_{index}.md",
            trajectory_id=f"traj_{index}",
            path=path,
            watch_dir_id=0,
            watch_dir=trajectory_root,
            label="fixture",
            ecosystem="test",
            status=None,
            metadata=MappingProxyType({}),
        ))

    class _Trajectories:
        def iter(self):
            return iter(resources)

    def fake_agent(config, *, workspace, state_path, phase, target=""):
        store = _MODULE._KernelState(state_path)
        state = json.loads(state_path.read_text(encoding="utf-8"))
        if phase == "router":
            store.newskill(
                "focused",
                "Focused test procedures.",
                _skill_md("focused"),
                state["valid_trajectory_ids"],
            )
            return
        for index, source_id in enumerate(state["valid_trajectory_ids"]):
            store.upsert_task(
                target, f"task-{index}", f"intent {index}", "context",
                "exact", f"answer-{index}", {}, [source_id],
            )

    monkeypatch.setattr(_MODULE, "_run_codex_agent", fake_agent)
    monkeypatch.setattr(
        _MODULE,
        "_run_skillopt_target",
        lambda *args, **kwargs: {
            "status": "rejected", "accepted": False, "gate_action": "reject",
        },
    )
    context = SimpleNamespace(
        config_path=tmp_path / "missing-config.yaml",
        trajectories=_Trajectories(),
        invocation=SimpleNamespace(changed_trajectory_ids=()),
        skills=SimpleNamespace(list=lambda: []),
        workspace=tmp_path / "workspace",
        run_id="run-1",
        trajectory_root=trajectory_root,
    )

    result = _MODULE.SkillOptKernel().run(context)

    assert isinstance(result, KernelRunResult)
    assert result.processed_trajectory_ids == ("root:traj_0.md", "root:traj_1.md")
    assert result.submitted_skills == ()
    assert result.metrics == {}
    assert result.notes == ""
    report = json.loads((context.workspace / "reports" / "run-1.json").read_text())
    assert report["targets"]["focused"]["status"] == "rejected"
