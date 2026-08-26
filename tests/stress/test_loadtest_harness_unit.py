"""Fast regression tests for the release stress-test harness itself."""
from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import threading
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
HARNESS_PATH = REPO_ROOT / "scripts" / "loadtest_300_control_plane.py"
SPEC = importlib.util.spec_from_file_location("xskill_loadtest_harness", HARNESS_PATH)
assert SPEC is not None and SPEC.loader is not None
HARNESS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(HARNESS)


class _Response:
    status_code = 200
    text = "{}"

    def json(self):
        return {"slots": []}


class _BlockedSyncClient:
    def __init__(self, release: threading.Event):
        self.release = release

    async def get(self, _path, **_kwargs):
        while not self.release.is_set():
            await asyncio.sleep(0.001)
        return _Response()


class _AlreadySaturatedState:
    def __init__(self, release: threading.Event):
        self.embed_release = release

    def set_embed_phase(self, _phase: str, *, released: bool) -> None:
        if released:
            self.embed_release.set()
        else:
            self.embed_release.clear()

    def snapshot(self):
        return {
            "embedding": {
                "request_count": 1,
                "completed_requests": 0,
                "input_item_count": 1,
                "active": 1,
                "max_active": 1,
                "requests_by_phase": {"cold": 1},
                "items_by_phase": {"cold": 1},
                "unique_inputs": 1,
                "duplicate_input_calls": 0,
                "latency": HARNESS._latency_summary([]),
            },
        }


class _GatedState:
    def __init__(self):
        self.embed_release = threading.Event()
        self.phase = "unset"

    def set_embed_phase(self, phase: str, *, released: bool) -> None:
        self.phase = phase
        if released:
            self.embed_release.set()
        else:
            self.embed_release.clear()

    def snapshot(self):
        request_count = 0 if self.phase == "unset" else 1
        return {
            "embedding": {
                "request_count": request_count,
                "completed_requests": 0,
                "input_item_count": request_count,
                "active": request_count,
                "max_active": request_count,
                "requests_by_phase": ({self.phase: 1} if request_count else {}),
                "items_by_phase": ({self.phase: 1} if request_count else {}),
                "unique_inputs": request_count,
                "duplicate_input_calls": 0,
                "latency": HARNESS._latency_summary([]),
            },
        }


class _JsonResponse:
    status_code = 200
    text = "{}"

    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class _ProfileClient:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = 0

    async def get(self, _path, **_kwargs):
        self.calls += 1
        response = next(self.responses)
        if isinstance(response, BaseException):
            raise response
        return _JsonResponse({"profile_refresh": response})


class _WatcherClient:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = 0

    async def get(self, _path, **_kwargs):
        self.calls += 1
        response = next(self.responses)
        if isinstance(response, BaseException):
            raise response
        return _JsonResponse(response)


def _watcher_round(
    ended_at: float, *, skills_edited: int, errors: int = 0,
) -> dict:
    return {
        "ok": True,
        "error": None,
        "ended_at": ended_at,
        "stats": {
            "polls": 1,
            "new_trajs": 0,
            "atoms_extracted": 0,
            "indexed": 0,
            "atoms_clustered": 0,
            "skills_edited": skills_edited,
            "scores": 0,
            "errors": errors,
            "retries": 0,
            "scans": 1,
            "last_scan": ended_at - 0.1,
            "running": False,
            "paused": False,
        },
    }


def _tool_names(response: dict) -> list[str]:
    return [
        call["function"]["name"]
        for call in response["choices"][0]["message"].get("tool_calls", [])
    ]


def test_mock_skill_edit_follows_current_three_request_protocol() -> None:
    initial = HARNESS._tool_call_response({"messages": [{
        "role": "user",
        "content": "目标 skill 目录: /tmp/skills/example-skill\n"
                   "目标 SKILL.md 路径: /tmp/skills/example-skill/SKILL.md",
    }]}, 1)
    assert _tool_names(initial) == ["write_file"]

    after_write = HARNESS._tool_call_response({"messages": [{
        "role": "tool", "content": "wrote: /tmp/skills/example-skill/SKILL.md",
    }]}, 2)
    assert _tool_names(after_write) == ["commit_baby"]
    arguments = json.loads(
        after_write["choices"][0]["message"]["tool_calls"][0]
        ["function"]["arguments"]
    )
    assert set(arguments) == {"skill_name", "message"}

    after_checkpoint = HARNESS._tool_call_response({"messages": [{
        "role": "tool", "content": "Created baby checkpoint abc1234.",
    }]}, 3)
    message = after_checkpoint["choices"][0]["message"]
    assert message["content"] == "Mock SkillEdit complete."
    assert "tool_calls" not in message


def test_gated_wave_releases_embedding_before_gather(monkeypatch, tmp_path) -> None:
    """A synchronous-embedding regression must fail, not deadlock the harness."""
    state = _GatedState()
    client = _BlockedSyncClient(state.embed_release)
    wave = {}
    checkpoints = []
    artifact = tmp_path / "result.json"

    def checkpoint() -> None:
        checkpoints.append(dict(wave))
        HARNESS._write_result_snapshot({"waves": {"blocked_sync": wave}}, artifact)

    async def no_probes(*_args, **_kwargs):
        return []

    monkeypatch.setattr(HARNESS, "_control_plane_probes", no_probes)

    async def scenario():
        return await HARNESS.run_sync_wave(
            client,
            phase="blocked_sync",
            client_rows=[{"client_id": "client-1"}],
            join_token="token",
            state=state,
            server_pid=-1,
            gated_embedding=True,
            wave=wave,
            checkpoint=checkpoint,
            sync_max_s=0.02,
        )

    result = asyncio.run(asyncio.wait_for(scenario(), timeout=0.5))
    assert state.embed_release.is_set()
    assert result["all_sync_completed_before_embedding_release"] is False
    assert result["saturation"]["pending_sync_requests"] == 1
    assert result["sync"]["statuses"] == {"200": 1}
    assert len(checkpoints) >= 3
    persisted = json.loads(artifact.read_text(encoding="utf-8"))
    assert persisted["waves"]["blocked_sync"]["sync"]["statuses"] == {"200": 1}


def test_gated_wave_accepts_embedding_already_active(monkeypatch) -> None:
    release = threading.Event()
    state = _AlreadySaturatedState(release)
    client = _BlockedSyncClient(release)
    checkpoint_records = []

    async def no_probes(*_args, **_kwargs):
        return []

    monkeypatch.setattr(HARNESS, "_control_plane_probes", no_probes)

    result = asyncio.run(HARNESS.run_sync_wave(
        client,
        phase="cold",
        client_rows=[{"client_id": "client-1"}],
        join_token="token",
        state=state,
        server_pid=-1,
        gated_embedding=True,
        wave={},
        checkpoint=checkpoint_records.clear,
        sync_max_s=0.02,
    ))

    assert result["saturation"]["embedding_snapshot"]["active"] == 1
    assert result["sync"]["statuses"] == {"200": 1}


def test_profile_wait_requires_new_nested_idle_round(monkeypatch) -> None:
    monkeypatch.setattr(HARNESS, "PROFILE_REFRESH_POLL_INTERVAL_S", 0)
    client = _ProfileClient([
        {
            "ok": True, "error": None, "ended_at": 10.0,
            "stats": {
                "queued": 0, "running": 0, "failed": 0, "embed_items": 0,
            },
        },
        {
            "ok": True, "error": None, "ended_at": 11.0,
            "stats": {
                "profile_rc": 0, "vector_upserted": 1,
                "vector_deleted": 0, "recommends": 1,
            },
        },
    ])

    metrics = asyncio.run(HARNESS._wait_profile_idle(
        client, after_ended_at=10.0, timeout=0.1,
    ))

    assert client.calls == 2
    assert metrics == {
        "profile_rc": 0, "vector_upserted": 1,
        "vector_deleted": 0, "recommends": 1,
    }


def test_profile_wait_fails_immediately_without_exposing_worker_error() -> None:
    client = _ProfileClient([{
        "ok": False,
        "error": "api_key=do-not-expose",
        "ended_at": 11.0,
        "stats": {},
    }])

    try:
        asyncio.run(HARNESS._wait_profile_idle(
            client, after_ended_at=10.0, timeout=0.1,
        ))
    except RuntimeError as exc:
        message = str(exc)
    else:
        raise AssertionError("failed profile refresh status must raise")

    assert client.calls == 1
    assert "do-not-expose" not in message
    assert "'error_present': True" in message


def test_profile_poll_exception_is_logged_and_reported_safely(
    monkeypatch, caplog,
) -> None:
    monkeypatch.setattr(HARNESS, "PROFILE_REFRESH_POLL_INTERVAL_S", 0)

    class _FailingProfileClient:
        async def get(self, _path, **_kwargs):
            raise OSError("api_key=do-not-expose")

    with caplog.at_level(logging.WARNING):
        try:
            asyncio.run(HARNESS._wait_profile_idle(
                _FailingProfileClient(), after_ended_at=10.0, timeout=0.001,
            ))
        except TimeoutError as exc:
            message = str(exc)
        else:
            raise AssertionError("profile refresh polling must time out")

    assert "OSError" in message
    assert "do-not-expose" not in message
    assert "profile refresh status poll failed: OSError" in caplog.text
    assert "do-not-expose" not in caplog.text


def test_successful_profile_poll_clears_previous_poll_error(monkeypatch) -> None:
    monkeypatch.setattr(HARNESS, "PROFILE_REFRESH_POLL_INTERVAL_S", 0)

    class _RecoveringProfileClient:
        calls = 0

        async def get(self, _path, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                raise OSError("transient")
            return _JsonResponse({"profile_refresh": {
                "ok": True, "error": None, "ended_at": 10.0,
                "stats": {
                    "queued": 0, "running": 0, "failed": 0,
                    "embed_items": 0,
                },
            }})

    with pytest.raises(TimeoutError) as error_info:
        asyncio.run(HARNESS._wait_profile_idle(
            _RecoveringProfileClient(),
            after_ended_at=10.0,
            timeout=0.001,
        ))

    assert "'poll_error_type': None" in str(error_info.value)


@pytest.mark.parametrize(
    ("profile_status", "expected_message"),
    [
        (
            {
                "ok": True, "error": None, "ended_at": 11.0,
                "stats": {
                    "profile_rc": 0, "vector_upserted": 0,
                    "vector_deleted": 0,
                },
            },
            "recommends",
        ),
        (
            {
                "ok": True, "error": None, "ended_at": 11.0,
                "stats": {
                    "queued": 0, "running": 0, "failed": True,
                    "embed_items": 0,
                },
            },
            "failed",
        ),
        (
            {
                "ok": True, "error": None, "ended_at": "late",
                "stats": {
                    "queued": 0, "running": 0, "failed": 0,
                    "embed_items": 0,
                },
            },
            "ended_at",
        ),
        (
            {
                "error": None, "ended_at": 11.0,
                "stats": {
                    "queued": 0, "running": 0, "failed": 0,
                    "embed_items": 0,
                },
            },
            "ok",
        ),
    ],
)
def test_profile_status_contract_rejects_missing_or_invalid_fields(
    profile_status, expected_message,
) -> None:
    with pytest.raises(HARNESS.StatusContractError, match=expected_message):
        asyncio.run(HARNESS._wait_profile_idle(
            _ProfileClient([profile_status]),
            after_ended_at=10.0,
            timeout=0.1,
        ))


def test_watcher_target_accumulates_only_new_nested_rounds(monkeypatch) -> None:
    monkeypatch.setattr(HARNESS, "PROFILE_REFRESH_POLL_INTERVAL_S", 0)
    client = _WatcherClient([
        _watcher_round(10.0, skills_edited=99),
        _watcher_round(11.0, skills_edited=2),
        _watcher_round(12.0, skills_edited=3),
        _watcher_round(13.0, skills_edited=0),
    ])

    evidence = asyncio.run(HARNESS._wait_watcher_target(
        client, after_ended_at=10.0, expected_skills=5, timeout=0.1,
    ))

    assert evidence["skills_edited"] == 5
    assert evidence["errors"] == 0
    assert [item["ended_at"] for item in evidence["rounds"]] == [11.0, 12.0]
    assert client.calls == 3


def test_watcher_failure_is_immediate_and_safe() -> None:
    client = _WatcherClient([{
        "ok": False,
        "error": "api_key=do-not-expose",
        "ended_at": 11.0,
        "stats": {"skills_edited": 0},
    }])

    try:
        asyncio.run(HARNESS._wait_watcher_target(
            client, after_ended_at=10.0, expected_skills=1, timeout=0.1,
        ))
    except RuntimeError as exc:
        message = str(exc)
    else:
        raise AssertionError("failed watcher status must raise")

    assert client.calls == 1
    assert "do-not-expose" not in message
    assert "'error_present': True" in message


def test_watcher_nonzero_errors_fail_the_gate() -> None:
    client = _WatcherClient([
        _watcher_round(11.0, skills_edited=1, errors=7),
    ])

    with pytest.raises(HARNESS.StatusContractError, match="watcher errors"):
        asyncio.run(HARNESS._wait_watcher_target(
            client, after_ended_at=10.0, expected_skills=1, timeout=0.1,
        ))


@pytest.mark.parametrize(
    ("field_name", "invalid_value"),
    [
        ("polls", None),
        ("skills_edited", True),
        ("scans", -1),
        ("running", 0),
        ("last_scan", "late"),
    ],
)
def test_successful_watcher_status_requires_complete_strict_stats(
    field_name, invalid_value,
) -> None:
    watcher_status = _watcher_round(11.0, skills_edited=1)
    watcher_status["stats"][field_name] = invalid_value

    with pytest.raises(HARNESS.StatusContractError, match=field_name):
        asyncio.run(HARNESS._watcher_status(_WatcherClient([watcher_status])))


def test_status_artifact_does_not_contain_raw_secret(tmp_path) -> None:
    secret = "sk-artifactsecret0123456789"
    raw_failure = {
        "ok": False, "error": secret, "ended_at": 11.0, "stats": {},
    }
    safe_status = HARNESS._safe_watcher_status(raw_failure, None)

    class _SecretProbeResponse:
        status_code = 500
        text = f'{{"error":"{secret}"}}'

    class _SecretProbeClient:
        async def request(self, *_args, **_kwargs):
            return _SecretProbeResponse()

    probe = asyncio.run(HARNESS._probe(
        _SecretProbeClient(), "GET", "/api/v1/watcher/status",
    ))
    result_path = tmp_path / "result.json"
    HARNESS._write_result_snapshot(
        {"watcher_final_state": safe_status, "probes": [probe]},
        result_path,
    )
    artifact_text = result_path.read_text(encoding="utf-8")

    assert secret not in artifact_text
    assert "body_prefix" not in artifact_text
    assert "error_present" in artifact_text


def test_watcher_poll_exception_is_not_silent(monkeypatch, caplog) -> None:
    monkeypatch.setattr(HARNESS, "PROFILE_REFRESH_POLL_INTERVAL_S", 0)

    class _FailingWatcherClient:
        async def get(self, _path, **_kwargs):
            raise OSError("api_key=do-not-expose")

    with caplog.at_level(logging.WARNING):
        try:
            asyncio.run(HARNESS._wait_watcher_target(
                _FailingWatcherClient(),
                after_ended_at=10.0,
                expected_skills=1,
                timeout=0.001,
            ))
        except TimeoutError as exc:
            message = str(exc)
        else:
            raise AssertionError("watcher polling must time out")

    assert "OSError" in message
    assert "do-not-expose" not in message
    assert "watcher status poll failed: OSError" in caplog.text
    assert "do-not-expose" not in caplog.text
