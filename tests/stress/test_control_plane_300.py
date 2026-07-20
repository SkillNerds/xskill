"""Release-gating 300x300 control-plane stress test.

The defaults are the release acceptance scale and real backend delays.  Local
debugging may set the documented ``XSKILL_STRESS_*`` environment variables;
CI and release workflows intentionally do not override them.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
HARNESS = REPO_ROOT / "scripts" / "loadtest_300_control_plane.py"
WATCHER_COUNT_FIELDS = (
    "polls", "new_trajs", "atoms_extracted", "indexed", "atoms_clustered",
    "skills_edited", "scores", "errors", "retries", "in_flight",
)


def _env_int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


def _env_float(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


def _artifact_message(root: Path, process: subprocess.CompletedProcess[str] | None) -> str:
    results = sorted(root.glob("run-*/result.json"))
    details = [f"artifact_root={root}", f"result_files={results}"]
    if process is not None:
        details.extend([
            f"returncode={process.returncode}",
            f"stdout_tail={process.stdout[-4000:]}",
            f"stderr_tail={process.stderr[-4000:]}",
        ])
    return "\n".join(details)


def _signal_process_group(pid: int, sig: signal.Signals) -> None:
    try:
        os.killpg(pid, sig)
    except ProcessLookupError:
        pass


@pytest.mark.stress
def test_control_plane_300(tmp_path: Path) -> None:
    skills = _env_int("XSKILL_STRESS_SKILLS", 300)
    clients = _env_int("XSKILL_STRESS_CLIENTS", 300)
    concurrency = _env_int("XSKILL_STRESS_CONCURRENCY", 30)
    profile_workers = _env_int("XSKILL_STRESS_PROFILE_WORKERS", 30)
    llm_delay = _env_float("XSKILL_STRESS_LLM_DELAY", 12.0)
    embed_delay = _env_float("XSKILL_STRESS_EMBED_DELAY", 23.0)
    timeout_s = _env_float("XSKILL_STRESS_TIMEOUT", 1200.0)
    convergence_timeout = _env_float("XSKILL_STRESS_CONVERGENCE_TIMEOUT", 900.0)
    queue_size = max(clients, _env_int("XSKILL_STRESS_QUEUE_SIZE", 1024))

    command = [
        sys.executable,
        str(HARNESS),
        "--skills", str(skills),
        "--clients", str(clients),
        "--max-concurrent", str(concurrency),
        "--profile-refresh-workers", str(profile_workers),
        "--profile-refresh-queue-size", str(queue_size),
        "--llm-delay", str(llm_delay),
        "--embed-delay", str(embed_delay),
        "--convergence-timeout", str(convergence_timeout),
        "--artifact-root", str(tmp_path),
    ]
    process: subprocess.CompletedProcess[str] | None = None
    runner = subprocess.Popen(
        command,
        cwd=REPO_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        stdout, stderr = runner.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired as exc:
        _signal_process_group(runner.pid, signal.SIGTERM)
        try:
            stdout, stderr = runner.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            _signal_process_group(runner.pid, signal.SIGKILL)
            stdout, stderr = runner.communicate(timeout=10)
        pytest.fail(
            f"stress harness timed out after {timeout_s}s\n"
            f"stdout_tail={(stdout or exc.stdout or '')[-4000:]}\n"
            f"stderr_tail={(stderr or exc.stderr or '')[-4000:]}\n"
            f"{_artifact_message(tmp_path, None)}",
        )
    finally:
        # The harness normally reaps its uvicorn child.  Since both processes
        # share this dedicated group, this also cleans up a child left behind
        # by an early harness crash.
        _signal_process_group(runner.pid, signal.SIGTERM)
    process = subprocess.CompletedProcess(command, runner.returncode, stdout, stderr)

    result_files = sorted(tmp_path.glob("run-*/result.json"))
    assert len(result_files) == 1, _artifact_message(tmp_path, process)
    result_path = result_files[0]
    result = json.loads(result_path.read_text(encoding="utf-8"))
    artifact = f"stress artifact: {result_path}"
    assert process.returncode == 0, _artifact_message(tmp_path, process)
    assert result["success"] is True, (
        f"validation_failures={result.get('validation_failures')}\n"
        f"result={result_path}\n{_artifact_message(tmp_path, process)}"
    )

    for phase in ("cold", "cache_hit", "one_new_atom"):
        sync = result["waves"][phase]["sync"]
        assert sync["requests"] == clients, artifact
        assert sync["statuses"] == {"200": clients}, artifact
        assert sync["latency"]["p95_s"] < 6, artifact
        assert sync["latency"]["max_s"] < 7, artifact

    probes = [
        probe
        for wave in result["waves"].values()
        for probe in wave["probes"]
    ] + result["final_probes"]
    assert probes, artifact
    assert all(probe["status"] == 200 for probe in probes), artifact
    dashboard = [probe for probe in probes if probe["path"].startswith("/api/v1/dashboard/")]
    assert len(dashboard) >= 6, artifact
    assert all(probe["elapsed_s"] < 2.5 for probe in dashboard), artifact
    index_probes = [probe for probe in probes if probe["path"] == "/"]
    assert index_probes, artifact
    assert all(probe["elapsed_s"] < 1.5 for probe in index_probes), artifact
    assert any(probe["path"] == "/api/v1/status" for probe in probes), artifact

    assert result["diagnostics"]["database_locked_count"] == 0, artifact
    assert type(result["diagnostics"]["traceback_count"]) is int, artifact
    assert result["diagnostics"]["traceback_count"] == 0, artifact
    llm = result["mock"]["llm"]
    assert llm["started"] == 2 * skills, artifact
    assert llm["completed"] == 2 * skills, artifact
    assert llm["initial_requests"] == skills, artifact
    assert llm["followup_requests"] == skills, artifact
    assert llm["max_active"] <= concurrency, artifact

    embedding = result["mock"]["embedding"]
    assert embedding["requests_by_phase"].get("cold", 0) == clients, artifact
    assert embedding["requests_by_phase"].get("cache_hit", 0) == 0, artifact
    assert embedding["requests_by_phase"].get("one_new_atom", 0) == clients, artifact
    assert embedding["items_by_phase"].get("cold", 0) == clients, artifact
    assert embedding["items_by_phase"].get("cache_hit", 0) == 0, artifact
    assert embedding["items_by_phase"].get("one_new_atom", 0) == clients, artifact
    assert embedding["request_count"] == 2 * clients, artifact
    assert embedding["input_item_count"] == 2 * clients, artifact
    assert embedding["unique_inputs"] == 2 * clients, artifact
    assert embedding["duplicate_input_calls"] == 0, artifact
    assert embedding["max_active"] <= profile_workers, artifact

    profile_metrics = result["profile_metrics"]["final_idle"]
    assert profile_metrics["queued"] == 0, artifact
    assert profile_metrics["running"] == 0, artifact
    profile_rounds = [
        result["profile_metrics"][key]
        for key in ("after_cold", "after_cache_hit", "after_one_new_atom")
    ]
    assert sum(metrics["failed"] for metrics in profile_rounds) == 0, artifact
    assert profile_rounds[0]["embed_items"] in (0, clients), artifact
    assert profile_rounds[1]["embed_items"] == 0, artifact
    assert profile_rounds[2]["embed_items"] == clients, artifact
    assert result["profile_convergence"]["rows"] == clients, artifact
    assert result["profile_convergence"]["revision_rows"] == clients, artifact
    assert result["profile_convergence"]["revision_matches"] == clients, artifact

    skills_final = result["skill_convergence"]
    assert skills_final["skill_dirs"] == skills, artifact
    assert skills_final["main_count"] == skills, artifact
    assert skills_final["candidates_empty"] == skills, artifact
    assert skills_final["cross_contamination_count"] == 0, artifact
    assert result["cold_start_signal_exists"] is False, artifact
    assert type(result["watcher_evidence"]["skills_edited"]) is int, artifact
    assert result["watcher_evidence"]["skills_edited"] >= skills, artifact
    assert type(result["watcher_evidence"]["errors"]) is int, artifact
    assert result["watcher_evidence"]["errors"] == 0, artifact
    assert result["watcher_evidence"]["rounds"], artifact
    assert all(
        type(round_status["stats"]["errors"]) is int
        and round_status["stats"]["errors"] == 0
        for round_status in result["watcher_evidence"]["rounds"]
    ), artifact
    final_watcher = result["watcher_final_state"]
    assert final_watcher["ok"] is True, artifact
    assert type(final_watcher["ended_at"]) in (int, float), artifact
    assert final_watcher["ended_at"] >= 0, artifact
    assert final_watcher["error_present"] is False, artifact
    assert final_watcher["poll_error_type"] is None, artifact
    for field_name in WATCHER_COUNT_FIELDS:
        assert type(final_watcher["stats"][field_name]) is int, artifact
        assert final_watcher["stats"][field_name] >= 0, artifact
    assert final_watcher["stats"]["errors"] == 0, artifact
    assert type(final_watcher["stats"]["running"]) is bool, artifact
    assert type(final_watcher["stats"]["paused"]) is bool, artifact
    assert result["server"]["shutdown"]["clean"] is True, artifact
    assert result["server"]["shutdown"]["forced_kill"] is False, artifact
