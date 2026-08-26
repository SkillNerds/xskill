"""Static contracts for the risk-axis CI topology."""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import yaml


_REPO_ROOT = Path(__file__).resolve().parents[1]
_CI_PATH = _REPO_ROOT / ".github" / "workflows" / "ci.yml"
_WORKFLOW_PATHS = tuple(
    sorted((_REPO_ROOT / ".github" / "workflows").glob("*.yml"))
)
_OFFICIAL_ACTION_MAJORS = {
    "actions/checkout": "v7",
    "actions/setup-node": "v7",
    "actions/setup-python": "v7",
    "actions/upload-artifact": "v7",
}
_OFFICIAL_ACTION_COUNTS = Counter(
    {
        "actions/checkout": 13,
        "actions/setup-python": 12,
        "actions/upload-artifact": 2,
        "actions/setup-node": 1,
    }
)


def _jobs() -> dict:
    return yaml.safe_load(_CI_PATH.read_text(encoding="utf-8"))["jobs"]


def _run_steps(job: dict) -> list[str]:
    return [str(step["run"]) for step in job["steps"] if "run" in step]


def test_official_javascript_actions_use_node24_majors() -> None:
    counts: Counter[str] = Counter()

    for workflow_path in _WORKFLOW_PATHS:
        workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
        for job in workflow["jobs"].values():
            for step in job.get("steps", []):
                uses = str(step.get("uses", ""))
                action, separator, version = uses.partition("@")
                if action not in _OFFICIAL_ACTION_MAJORS:
                    continue
                assert separator and version == _OFFICIAL_ACTION_MAJORS[action], (
                    f"{workflow_path.name}: expected {action}"
                    f"@{_OFFICIAL_ACTION_MAJORS[action]}, got {uses}"
                )
                counts[action] += 1

    assert counts == _OFFICIAL_ACTION_COUNTS


def test_ut_matrix_covers_each_supported_risk_axis_once() -> None:
    matrix = _jobs()["ut-it"]["strategy"]["matrix"]["include"]
    pairs = {(entry["os"], entry["python-version"]) for entry in matrix}

    assert pairs == {
        ("ubuntu-latest", "3.9"),
        ("ubuntu-latest", "3.10"),
        ("ubuntu-latest", "3.11"),
        ("ubuntu-latest", "3.12"),
        ("macos-latest", "3.11"),
        ("windows-latest", "3.11"),
    }


def test_regular_jobs_do_not_duplicate_bdd_or_hide_windows_failures() -> None:
    job = _jobs()["ut-it"]
    test_commands = [command for command in _run_steps(job) if "pytest " in command]

    assert len(test_commands) == 2
    for command in test_commands:
        assert "--ignore=tests/bdd" in command
        assert "--durations=25" in command
        assert "--reruns" not in command


def test_smoke_runs_all_contracts_once_per_platform_without_agent_clis() -> None:
    job = _jobs()["smoke-e2e"]
    matrix = job["strategy"]["matrix"]
    commands = "\n".join(_run_steps(job))

    assert "needs" not in job
    assert matrix == {"os": ["ubuntu-latest", "macos-latest", "windows-latest"]}
    assert "pytest tests/e2e/test_smoke.py -v --durations=25" in commands
    assert " -k " not in commands
    assert "@openai/codex" not in commands
    assert "opencode-ai" not in commands


def test_team_lifecycle_job_runs_connect_and_team_cs_e2e_together() -> None:
    job = _jobs()["connect-lifecycle-e2e"]
    test_commands = [command for command in _run_steps(job) if "pytest " in command]

    assert "if" not in job
    assert len(test_commands) == 1
    command = test_commands[0]
    assert "tests/e2e/test_connect_lifecycle_e2e.py" in command
    assert "tests/e2e/test_team_cs_e2e.py" in command


def test_live_agent_trigger_scope_and_cli_versions_are_explicit() -> None:
    job = _jobs()["live-agent-e2e"]
    matrix = job["strategy"]["matrix"]
    os_expression = matrix["os"]
    commands = "\n".join(_run_steps(job))

    assert "needs" not in job
    assert matrix["agent"] == ["codex", "opencode"]
    assert "schedule" in os_expression
    assert "workflow_dispatch" in os_expression
    assert os_expression.count("ubuntu-latest") == 2
    assert "macos-latest" in os_expression
    assert "windows-latest" in os_expression
    assert "@openai/codex@latest" in commands
    assert "@openai/codex@${PINNED_CODEX_VERSION}" in commands
    assert "opencode-ai@latest" in commands
    assert "opencode-ai@${PINNED_OPENCODE_VERSION}" in commands
    assert "schedule" in str(job["continue-on-error"])
    assert "workflow_dispatch" in str(job["continue-on-error"])
