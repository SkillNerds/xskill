#!/usr/bin/env python3.11
"""Run the real 300-skill/300-client control-plane stress scenario.

Only the OpenAI-compatible LLM and embedding backends are mocked.  The test
uses a real uvicorn process, real team routes, the real watcher, Agno tool
calls, git repositories, SQLite stores, and profile refresh workers.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import math
import os
import re
import site
import socket
import sqlite3
import statistics
import subprocess
import sys
import threading
import time
from collections import Counter
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request


REPO_ROOT = Path(__file__).resolve().parents[1]
ORIGINAL_USER_BASE = site.USER_BASE
PROFILE_REFRESH_INTERVAL_S = 1.0
PROFILE_REFRESH_POLL_INTERVAL_S = 0.2
WATCHER_COUNT_FIELDS = (
    "polls", "new_trajs", "atoms_extracted", "indexed", "atoms_clustered",
    "skills_edited", "scores", "errors", "retries", "scans",
)
PROFILE_REFRESH_COUNT_FIELDS = ("queued", "running", "failed", "embed_items")
RECOMMEND_HEAVY_COUNT_FIELDS = (
    "profile_rc", "vector_upserted", "vector_deleted", "recommends",
)
logger = logging.getLogger("xskill.loadtest.control_plane")


class StatusContractError(RuntimeError):
    """A periodic worker status endpoint returned an invalid contract."""


def _write_result_snapshot(result: dict[str, Any], result_path: Path) -> None:
    """Atomically persist the evidence collected so far.

    The pytest wrapper may have to terminate the process group when a regression
    blocks the harness.  Checkpointing each phase keeps the last complete wave
    and the current partial wave available in CI artifacts.
    """
    temporary = result_path.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    temporary.replace(result_path)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, math.ceil(percentile * len(ordered)) - 1))
    return ordered[index]


def _latency_summary(values: list[float]) -> dict[str, float | int | None]:
    return {
        "count": len(values),
        "min_s": min(values) if values else None,
        "p50_s": _percentile(values, 0.50),
        "p95_s": _percentile(values, 0.95),
        "p99_s": _percentile(values, 0.99),
        "max_s": max(values) if values else None,
        "mean_s": statistics.fmean(values) if values else None,
    }


def _embedding_items(raw: Any) -> list[str]:
    if isinstance(raw, list):
        return [str(item) for item in raw]
    return [str(raw)]


class MockState:
    """Thread-safe counters and gates for both mock backend APIs."""

    def __init__(self, *, llm_delay: float, embed_delay: float):
        self.llm_delay = llm_delay
        self.embed_delay = embed_delay
        self.lock = threading.Lock()
        self.llm_release = threading.Event()
        self.embed_release = threading.Event()
        self.embed_phase = "cold"

        self.llm_active = 0
        self.llm_max_active = 0
        self.llm_started = 0
        self.llm_completed = 0
        self.llm_initial = 0
        self.llm_followup = 0
        self.llm_durations: list[float] = []

        self.embed_active = 0
        self.embed_max_active = 0
        self.embed_started = 0
        self.embed_completed = 0
        self.embed_durations: list[float] = []
        self.embed_requests: list[dict[str, Any]] = []

    def set_embed_phase(self, phase: str, *, released: bool) -> None:
        with self.lock:
            self.embed_phase = phase
        if released:
            self.embed_release.set()
        else:
            self.embed_release.clear()

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            requests_by_phase = Counter(r["phase"] for r in self.embed_requests)
            items_by_phase: Counter[str] = Counter()
            all_items: list[str] = []
            for request in self.embed_requests:
                items = request["items"]
                items_by_phase[request["phase"]] += len(items)
                all_items.extend(items)
            return {
                "llm": {
                    "started": self.llm_started,
                    "completed": self.llm_completed,
                    "active": self.llm_active,
                    "max_active": self.llm_max_active,
                    "initial_requests": self.llm_initial,
                    "followup_requests": self.llm_followup,
                    "latency": _latency_summary(list(self.llm_durations)),
                },
                "embedding": {
                    "request_count": self.embed_started,
                    "completed_requests": self.embed_completed,
                    "input_item_count": len(all_items),
                    "active": self.embed_active,
                    "max_active": self.embed_max_active,
                    "requests_by_phase": dict(requests_by_phase),
                    "items_by_phase": dict(items_by_phase),
                    "unique_inputs": len(set(all_items)),
                    "duplicate_input_calls": len(all_items) - len(set(all_items)),
                    "latency": _latency_summary(list(self.embed_durations)),
                },
            }


def _message_text(messages: list[dict[str, Any]]) -> str:
    pieces: list[str] = []
    for message in messages:
        content = message.get("content")
        if isinstance(content, str):
            pieces.append(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    pieces.append(part["text"])
    return "\n".join(pieces)


def _skill_from_messages(messages: list[dict[str, Any]]) -> tuple[str, str]:
    text = _message_text(messages)
    directory_match = re.findall(r"目标 skill 目录:\s*(\S+)", text)
    path_match = re.findall(r"目标 SKILL\.md 路径:\s*(\S+)", text)
    if directory_match:
        skill_dir = directory_match[-1].rstrip("/\\")
        skill_name = Path(skill_dir).name
    else:
        skill_name = "unknown-skill"
    skill_path = path_match[-1] if path_match else str(Path(skill_name) / "SKILL.md")
    return skill_name, skill_path


def _tool_call_response(body: dict[str, Any], request_number: int) -> dict[str, Any]:
    messages = body.get("messages") or []
    model = body.get("model", "mock-tool-model")
    tool_results = [
        str(message.get("content") or "")
        for message in messages
        if isinstance(message, dict) and message.get("role") == "tool"
    ]
    if any("Created baby checkpoint" in result for result in tool_results):
        return {
            "id": f"chatcmpl-final-{request_number}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": "Mock SkillEdit complete."},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 80, "completion_tokens": 8, "total_tokens": 88},
        }

    skill_name, skill_path = _skill_from_messages(messages)
    content = (
        "---\n"
        f"name: {skill_name}\n"
        f"description: Use this skill for controlled load-test task {skill_name}.\n"
        "compatibility: Mock stress-test fixture only.\n"
        "metadata:\n"
        "  version: 1\n"
        f"  source_atoms: [atom_{skill_name}_0001]\n"
        "---\n\n"
        f"# {skill_name}\n\n"
        f"Mock-generated content for {skill_name}.\n"
    )
    if any("wrote:" in result for result in tool_results):
        tool_calls = [{
            "id": f"call-commit-{request_number}",
            "type": "function",
            "function": {
                "name": "commit_baby",
                "arguments": json.dumps({
                    "skill_name": skill_name,
                    "message": f"mock load test: checkpoint {skill_name}",
                }),
            },
        }]
    else:
        tool_calls = [{
            "id": f"call-write-{request_number}",
            "type": "function",
            "function": {
                "name": "write_file",
                "arguments": json.dumps({"path": skill_path, "content": content}),
            },
        }]
    return {
        "id": f"chatcmpl-tool-{request_number}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": None, "tool_calls": tool_calls},
            "finish_reason": "tool_calls",
        }],
        "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
    }


def build_mock_app(state: MockState) -> FastAPI:
    app = FastAPI()

    @app.get("/health")
    async def health():
        return {"ok": True}

    @app.post("/chat/completions")
    async def chat_completions(request: Request):
        body = await request.json()
        started_at = time.monotonic()
        messages = body.get("messages") or []
        followup = any(m.get("role") == "tool" for m in messages if isinstance(m, dict))
        with state.lock:
            state.llm_started += 1
            request_number = state.llm_started
            state.llm_active += 1
            state.llm_max_active = max(state.llm_max_active, state.llm_active)
            if followup:
                state.llm_followup += 1
            else:
                state.llm_initial += 1
        try:
            while not state.llm_release.is_set() or time.monotonic() - started_at < state.llm_delay:
                await asyncio.sleep(0.02)
            return _tool_call_response(body, request_number)
        finally:
            with state.lock:
                state.llm_active -= 1
                state.llm_completed += 1
                state.llm_durations.append(time.monotonic() - started_at)

    @app.post("/embeddings")
    async def embeddings(request: Request):
        body = await request.json()
        started_at = time.monotonic()
        items = _embedding_items(body.get("input", ""))
        with state.lock:
            state.embed_started += 1
            request_number = state.embed_started
            phase = state.embed_phase
            state.embed_active += 1
            state.embed_max_active = max(state.embed_max_active, state.embed_active)
            state.embed_requests.append({
                "n": request_number,
                "phase": phase,
                "items": items,
                "item_sha256": [hashlib.sha256(item.encode()).hexdigest() for item in items],
            })
        try:
            while not state.embed_release.is_set() or time.monotonic() - started_at < state.embed_delay:
                await asyncio.sleep(0.02)
            data = []
            for index, item in enumerate(items):
                seed = sum(ord(ch) for ch in item[:128])
                vector = [((seed + i * 17) % 101) / 101.0 for i in range(8)]
                data.append({"object": "embedding", "index": index, "embedding": vector})
            return {
                "object": "list",
                "model": body.get("model", "mock-embed"),
                "data": data,
                "usage": {"prompt_tokens": 8 * len(items), "total_tokens": 8 * len(items)},
            }
        finally:
            with state.lock:
                state.embed_active -= 1
                state.embed_completed += 1
                state.embed_durations.append(time.monotonic() - started_at)

    @app.get("/metrics")
    async def metrics():
        return state.snapshot()

    return app


class MockServer:
    def __init__(self, state: MockState):
        import uvicorn

        self.port = _free_port()
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.server = uvicorn.Server(uvicorn.Config(
            build_mock_app(state), host="127.0.0.1", port=self.port,
            log_level="warning", loop="asyncio",
        ))
        self.thread = threading.Thread(target=self.server.run, daemon=True, name="mock-backend")

    def start(self) -> None:
        self.thread.start()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if self.server.started:
                return
            time.sleep(0.05)
        raise RuntimeError("mock backend did not start")

    def stop(self) -> bool:
        if self.thread.ident is None:
            return True
        self.server.should_exit = True
        self.thread.join(timeout=10)
        return not self.thread.is_alive()


def _write_atom(root: Path, *, traj_id: str, atom_id: str, summary: str) -> None:
    tasks = root / traj_id / "tasks"
    tasks.mkdir(parents=True, exist_ok=True)
    payload = {
        "atom_id": atom_id,
        "traj_id": traj_id,
        "offset_start": 1,
        "offset_end": 2,
        "intent": "mock load test",
        "summary": summary,
        "used_skills": [],
        "tags": ["load-test"],
        "ux_score": 8,
    }
    (tasks / f"{atom_id}.json").write_text(json.dumps(payload), encoding="utf-8")


def prepare_home(
    run_dir: Path,
    mock_base_url: str,
    *,
    skills: int,
    clients: int,
    max_concurrent: int,
    thread_pool_tokens: int,
    team_sync_workers: int,
    profile_refresh_workers: int,
    profile_refresh_queue_size: int,
    profile_refresh_shutdown_timeout: float,
    skill_slots: int,
) -> dict[str, Any]:
    server_home = run_dir / "server_home"
    xhome = server_home / ".xskill"
    skill_root = xhome / "skill"
    traj_root = xhome / "team_trajectories"
    shared_store = xhome / "loadtest_atom_store"
    for path in (skill_root, traj_root, shared_store):
        path.mkdir(parents=True, exist_ok=True)

    os.environ["HOME"] = str(server_home)
    if str(REPO_ROOT / "src") not in sys.path:
        sys.path.insert(0, str(REPO_ROOT / "src"))

    import yaml
    from xskill.pipeline.registry import register_dir
    from xskill.skill import candidates as candidate_store
    from xskill.skill.git import init_skill_repo_on_baby
    from xskill.team.server.client_registry import ClientRegistry

    started = time.monotonic()
    for index in range(skills):
        slug = f"load-skill-{index:03d}"
        skill_dir = skill_root / slug
        init_skill_repo_on_baby(str(skill_dir), name=slug, description="stub")
        data, _ = candidate_store.add_atom_contribution(
            {"candidates": []}, f"atom_{slug}_0001", 10,
        )
        candidate_store.save_candidates(skill_dir, data)
        _write_atom(
            shared_store,
            traj_id=f"traj_{slug}",
            atom_id=f"atom_{slug}_0001",
            summary=f"SkillEdit evidence for {slug}",
        )
    register_dir(shared_store, label="loadtest-skill-atoms")

    registry = ClientRegistry(xhome / "team_clients.db")
    client_rows: list[dict[str, str]] = []
    for index in range(clients):
        user_name = f"load-user-{index:03d}"
        client_id = registry.register(
            label="loadtest", hostname=f"load-host-{index:03d}", user_name=user_name,
        )
        client_root = traj_root / "clients" / user_name / "sessions"
        _write_atom(
            client_root,
            traj_id=f"traj_client_{index:03d}",
            atom_id=f"atom_client_{index:03d}_0001",
            summary=f"cold summary client {index:03d}",
        )
        client_rows.append({
            "index": str(index),
            "client_id": client_id,
            "user_name": user_name,
            "root": str(client_root),
        })

    join_token = hashlib.sha256(f"{run_dir}-join-token".encode()).hexdigest()[:32]
    (xhome / "team_server.json").write_text(
        json.dumps({"join_token": join_token}), encoding="utf-8",
    )
    (xhome / "team_server.json").chmod(0o600)
    (xhome / "COLD_START").write_text(
        json.dumps({"trajectory_ids": [], "created_at": time.time()}), encoding="utf-8",
    )

    config = {
        "skill_dir": str(skill_root),
        "llm": {
            "base_url": mock_base_url,
            "model": "mock-tool-model",
            "api_key": "mock-key",
            "max_context": 200000,
            "request_timeout": 120,
            "connect_timeout": 5,
            "client_max_retries": 0,
            "max_retries": 1,
            "rate_limit": {
                "rpm": 60000,
                "request_burst": max_concurrent,
                "max_inflight": max_concurrent,
            },
        },
        "embedding": {
            "base_url": mock_base_url,
            "model": "mock-embed-model",
            "api_key": "mock-key",
            "dim": 8,
            "api": "openai",
            "rate_limit": {"max_inflight": profile_refresh_workers},
        },
        "skill_opt": {"enabled": False},
        "canary": {
            "enabled": True,
            "probability": 0.2,
            "min_samples": 5,
            "total_samples": 20,
            "max_days_hold": 14,
            "rotate_interval": 300,
            "jam_threshold": 50,
        },
        "watcher": {
            "poll_interval": 0.5,
        },
        "agent_worker": {"pools": {
            "split": {"workers": max_concurrent, "llm_weight": 6},
            "cluster": {
                "workers": max_concurrent,
                "batch_size": 8,
                "llm_weight": 3,
            },
            "edit": {"workers": max_concurrent, "llm_weight": 1},
            "embed": {"workers": profile_refresh_workers},
        }},
        "server": {
            "thread_pool_tokens": thread_pool_tokens,
            "team_sync_workers": team_sync_workers,
            "profile_refresh_workers": profile_refresh_workers,
            "profile_refresh_queue_size": profile_refresh_queue_size,
            "profile_refresh_shutdown_timeout": profile_refresh_shutdown_timeout,
            "profile_refresh_interval": PROFILE_REFRESH_INTERVAL_S,
        },
        "team": {
            "server": {
                "traj_root": str(traj_root),
                "skill_slots": skill_slots,
                "ranked_slots": min(80, skill_slots),
                "allow_anonymous_user": True,
            },
        },
        "recommend": {
            "quality_ratio": 0.8,
            "cluster_centers": 5,
            "last_n_atoms": 5,
        },
        "dashboard": {
            "enabled": True,
            "public": True,
            "password": "",
            "admins": [],
            "admin_password": "",
        },
    }
    (xhome / "config.yaml").write_text(
        yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding="utf-8",
    )
    return {
        "server_home": str(server_home),
        "xhome": str(xhome),
        "skill_root": str(skill_root),
        "traj_root": str(traj_root),
        "join_token": join_token,
        "clients": client_rows,
        "setup_s": time.monotonic() - started,
    }


def _process_threads(pid: int) -> int:
    try:
        text = Path(f"/proc/{pid}/status").read_text(encoding="utf-8")
        match = re.search(r"^Threads:\s+(\d+)", text, re.MULTILINE)
        return int(match.group(1)) if match else -1
    except OSError:
        return -1


class ThreadMonitor:
    def __init__(self, pid: int):
        self.pid = pid
        self._stop = threading.Event()
        self.samples: list[dict[str, float | int]] = []
        self.thread = threading.Thread(target=self._run, daemon=True, name="loadtest-thread-monitor")

    def start(self) -> None:
        self.thread.start()

    def _run(self) -> None:
        while not self._stop.wait(0.5):
            count = _process_threads(self.pid)
            if count >= 0:
                self.samples.append({"at_s": time.monotonic(), "count": count})

    def snapshot(self, stage: str) -> dict[str, Any]:
        return {"stage": stage, "at_s": time.monotonic(), "count": _process_threads(self.pid)}

    def stop(self) -> dict[str, Any]:
        self._stop.set()
        self.thread.join(timeout=2)
        counts = [int(sample["count"]) for sample in self.samples]
        return {
            "sample_count": len(counts),
            "peak": max(counts) if counts else _process_threads(self.pid),
            "samples": self.samples,
        }


async def _wait_for(predicate, *, timeout: float, description: str, interval: float = 0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        await asyncio.sleep(interval)
    raise TimeoutError(f"timeout waiting for {description}")


async def _probe(
    client,
    method: str,
    path: str,
    *,
    headers: dict[str, str] | None = None,
    timeout: float = 2.0,
) -> dict[str, Any]:
    started = time.monotonic()
    try:
        response = await client.request(method, path, headers=headers, timeout=timeout)
        return {
            "path": path,
            "status": response.status_code,
            "elapsed_s": time.monotonic() - started,
            "timed_out": False,
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "path": path,
            "status": None,
            "elapsed_s": time.monotonic() - started,
            "timed_out": "Timeout" in type(exc).__name__ or "timed out" in str(exc).lower(),
            "error_type": type(exc).__name__,
        }


async def _control_plane_probes(client, *, auth_headers: dict[str, str]) -> list[dict[str, Any]]:
    specs = [
        ("GET", "/", None),
        ("GET", "/api/v1/health", None),
        ("GET", "/api/v1/status", None),
        ("GET", "/api/v1/watcher/status", None),
        ("GET", "/api/v1/stats", None),
        ("GET", "/api/v1/registry/dirs", None),
        ("GET", "/api/v1/dashboard/overview", None),
        ("GET", "/api/v1/dashboard/skills", None),
        ("GET", "/api/v1/team/sync", auth_headers),
    ]
    return await asyncio.gather(*[
        _probe(client, method, path, headers=headers)
        for method, path, headers in specs
    ])


async def _sync_one(client, headers: dict[str, str], index: int) -> dict[str, Any]:
    started = time.monotonic()
    try:
        response = await client.get("/api/v1/team/sync", headers=headers, timeout=10)
        payload = response.json() if response.status_code == 200 else None
        return {
            "index": index,
            "status": response.status_code,
            "elapsed_s": time.monotonic() - started,
            "error": None if response.status_code == 200 else response.text[:300],
            "slot_count": len(payload.get("slots", [])) if payload else None,
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "index": index,
            "status": None,
            "elapsed_s": time.monotonic() - started,
            "error": f"{type(exc).__name__}: {exc}",
        }


def _wave_summary(results: list[dict[str, Any]]) -> dict[str, Any]:
    statuses = Counter(str(r["status"]) for r in results)
    errors = [r for r in results if r["status"] != 200]
    return {
        "requests": len(results),
        "statuses": dict(statuses),
        "errors": errors[:20],
        "slot_counts": dict(Counter(str(r.get("slot_count")) for r in results)),
        "latency": _latency_summary([float(r["elapsed_s"]) for r in results]),
    }


async def _profile_metrics(client) -> dict[str, Any] | None:
    response = await client.get("/api/v1/stats", timeout=3)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise StatusContractError("/api/v1/stats response is not an object")
    profile_status = payload.get("profile_refresh")
    if profile_status is not None and not isinstance(profile_status, dict):
        raise StatusContractError("profile_refresh status is not an object")
    if profile_status is not None:
        if not isinstance(profile_status.get("ok"), bool):
            raise StatusContractError("profile_refresh ok is not boolean")
        _profile_round_ended_at(profile_status)
        stats = profile_status.get("stats")
        if not isinstance(stats, dict):
            raise StatusContractError("profile_refresh stats is not an object")
        if profile_status["ok"]:
            _profile_status_kind(stats)
    return profile_status


def _profile_status_kind(stats: dict[str, Any]) -> str:
    if any(key in stats for key in RECOMMEND_HEAVY_COUNT_FIELDS):
        kind = "recommend_heavy"
        required_fields = RECOMMEND_HEAVY_COUNT_FIELDS
    elif any(key in stats for key in PROFILE_REFRESH_COUNT_FIELDS):
        kind = "profile_refresh"
        required_fields = PROFILE_REFRESH_COUNT_FIELDS
    else:
        raise StatusContractError("profile_refresh stats has no recognized schema")
    for key in required_fields:
        value = stats.get(key)
        if (
            not isinstance(value, int)
            or isinstance(value, bool)
            or value < 0
        ):
            raise StatusContractError(
                f"profile_refresh {key} is not a non-negative integer"
            )
    if kind == "recommend_heavy" and stats["profile_rc"] != 0:
        raise StatusContractError(
            "profile_refresh profile_rc is nonzero for a successful round"
        )
    return kind


def _profile_round_ended_at(profile_status: dict[str, Any] | None) -> float | None:
    if profile_status is None:
        return None
    ended_at = profile_status.get("ended_at")
    if (not isinstance(ended_at, (int, float)) or isinstance(ended_at, bool)):
        raise StatusContractError("profile_refresh ended_at is not numeric")
    return float(ended_at)


def _safe_profile_status(
    profile_status: dict[str, Any] | None, poll_error_type: str | None,
) -> dict[str, Any]:
    stats = profile_status.get("stats") if isinstance(profile_status, dict) else None
    ended_at = (
        profile_status.get("ended_at")
        if isinstance(profile_status, dict) else None
    )
    safe_stats = {}
    if isinstance(stats, dict):
        safe_stats = {
            key: stats[key]
            for key in (*PROFILE_REFRESH_COUNT_FIELDS, *RECOMMEND_HEAVY_COUNT_FIELDS)
            if isinstance(stats.get(key), int) and not isinstance(stats[key], bool)
        }
    return {
        "ok": profile_status.get("ok") if isinstance(profile_status, dict) else None,
        "ended_at": (
            ended_at
            if isinstance(ended_at, (int, float)) and not isinstance(ended_at, bool)
            else None
        ),
        "stats": safe_stats,
        "error_present": bool(
            profile_status.get("error") if isinstance(profile_status, dict) else None
        ),
        "poll_error_type": poll_error_type,
    }


async def _wait_profile_idle(
    client, *, after_ended_at: float | None, timeout: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last_status: dict[str, Any] | None = None
    last_poll_error_type: str | None = None
    while time.monotonic() < deadline:
        try:
            last_status = await _profile_metrics(client)
        except StatusContractError:
            raise
        except Exception as exc:  # noqa: BLE001 — 轮询边界,记录后继续到有界超时
            poll_error_type = type(exc).__name__
            if poll_error_type != last_poll_error_type:
                logger.warning(
                    "profile refresh status poll failed: %s", poll_error_type,
                )
            last_poll_error_type = poll_error_type
            await asyncio.sleep(PROFILE_REFRESH_POLL_INTERVAL_S)
            continue
        last_poll_error_type = None
        if last_status is not None:
            if last_status["ok"] is False:
                safe_status = _safe_profile_status(last_status, last_poll_error_type)
                raise RuntimeError(
                    f"profile refresh reported failure; status={safe_status}"
                )
            current_ended_at = _profile_round_ended_at(last_status)
            stats = last_status["stats"]
            is_new_round = (
                after_ended_at is None or current_ended_at > after_ended_at
            )
            # recommend-heavy first writes an intermediate profile-refresh
            # status, then overwrites it after vector reconcile + precompute.
            # Only the combined status proves the scheduled round completed.
            if is_new_round and _profile_status_kind(stats) == "recommend_heavy":
                return stats
        await asyncio.sleep(PROFILE_REFRESH_POLL_INTERVAL_S)
    safe_status = _safe_profile_status(last_status, last_poll_error_type)
    raise TimeoutError(
        f"profile refresh did not finish a new idle round; last_status={safe_status}"
    )


async def _watcher_status(client) -> dict[str, Any] | None:
    response = await client.get("/api/v1/watcher/status", timeout=3)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise StatusContractError("watcher status response is not an object")
    if payload.get("running") is False and "message" in payload:
        return None
    if not isinstance(payload.get("stats"), dict):
        raise StatusContractError("watcher status stats is not an object")
    if not isinstance(payload.get("ok"), bool):
        raise StatusContractError("watcher status ok is not boolean")
    _profile_round_ended_at(payload)
    if payload["ok"]:
        stats = payload["stats"]
        for key in WATCHER_COUNT_FIELDS:
            value = stats.get(key)
            if (
                not isinstance(value, int)
                or isinstance(value, bool)
                or value < 0
            ):
                raise StatusContractError(
                    f"watcher {key} is not a non-negative integer"
                )
        if stats["errors"] != 0:
            raise StatusContractError("watcher errors is not zero")
        if payload.get("error") is not None:
            raise StatusContractError(
                "watcher successful status contains an error"
            )
        if not isinstance(stats.get("running"), bool):
            raise StatusContractError("watcher running is not boolean")
        if not isinstance(stats.get("paused"), bool):
            raise StatusContractError("watcher paused is not boolean")
        last_scan = stats.get("last_scan")
        if (
            not isinstance(last_scan, (int, float))
            or isinstance(last_scan, bool)
            or last_scan < 0
        ):
            raise StatusContractError(
                "watcher last_scan is not a non-negative number"
            )
    return payload


def _safe_watcher_status(
    watcher_status: dict[str, Any] | None, poll_error_type: str | None,
) -> dict[str, Any]:
    stats = watcher_status.get("stats") if isinstance(watcher_status, dict) else None
    ended_at = (
        watcher_status.get("ended_at")
        if isinstance(watcher_status, dict) else None
    )
    safe_stats = {}
    if isinstance(stats, dict):
        safe_stats = {
            key: value for key, value in stats.items()
            if isinstance(value, (int, float, bool)) or value is None
        }
    return {
        "ok": watcher_status.get("ok") if isinstance(watcher_status, dict) else None,
        "ended_at": (
            ended_at
            if isinstance(ended_at, (int, float)) and not isinstance(ended_at, bool)
            else None
        ),
        "stats": safe_stats,
        "error_present": bool(
            watcher_status.get("error") if isinstance(watcher_status, dict) else None
        ),
        "poll_error_type": poll_error_type,
    }


async def _wait_watcher_target(
    client, *, after_ended_at: float | None, expected_skills: int, timeout: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    observed_ended_at = after_ended_at
    last_status: dict[str, Any] | None = None
    last_poll_error_type: str | None = None
    evidence = {"skills_edited": 0, "errors": 0, "rounds": []}
    while time.monotonic() < deadline:
        try:
            last_status = await _watcher_status(client)
        except StatusContractError:
            raise
        except Exception as exc:  # noqa: BLE001 — 轮询边界,记录后继续到有界超时
            poll_error_type = type(exc).__name__
            if poll_error_type != last_poll_error_type:
                logger.warning("watcher status poll failed: %s", poll_error_type)
            last_poll_error_type = poll_error_type
            await asyncio.sleep(PROFILE_REFRESH_POLL_INTERVAL_S)
            continue
        last_poll_error_type = None
        if last_status is not None:
            if not isinstance(last_status.get("ok"), bool):
                raise StatusContractError("watcher status ok is not boolean")
            if last_status["ok"] is False:
                safe_status = _safe_watcher_status(
                    last_status, last_poll_error_type,
                )
                raise RuntimeError(f"watcher round reported failure; status={safe_status}")
            current_ended_at = _profile_round_ended_at(last_status)
            if observed_ended_at is None or current_ended_at > observed_ended_at:
                safe_status = _safe_watcher_status(
                    last_status, last_poll_error_type,
                )
                skills_edited = safe_status["stats"].get("skills_edited")
                round_errors = safe_status["stats"].get("errors")
                if not isinstance(skills_edited, int) or isinstance(skills_edited, bool):
                    raise StatusContractError(
                        "watcher skills_edited is not an integer"
                    )
                if not isinstance(round_errors, int) or isinstance(round_errors, bool):
                    raise StatusContractError("watcher errors is not an integer")
                if round_errors != 0:
                    raise RuntimeError(
                        f"watcher round reported errors; status={safe_status}"
                    )
                evidence["rounds"].append(safe_status)
                evidence["skills_edited"] += skills_edited
                evidence["errors"] += round_errors
                observed_ended_at = current_ended_at
                if evidence["skills_edited"] >= expected_skills:
                    return evidence
        await asyncio.sleep(PROFILE_REFRESH_POLL_INTERVAL_S)
    safe_status = _safe_watcher_status(last_status, last_poll_error_type)
    raise TimeoutError(
        "watcher did not observe enough edited skills in new rounds; "
        f"observed={evidence['skills_edited']} expected={expected_skills} "
        f"last_status={safe_status}"
    )


async def run_sync_wave(
    client,
    *,
    phase: str,
    client_rows: list[dict[str, str]],
    join_token: str,
    state: MockState,
    server_pid: int,
    gated_embedding: bool,
    wave: dict[str, Any],
    checkpoint,
    sync_max_s: float = 7.0,
) -> dict[str, Any]:
    embedding_before = state.snapshot()["embedding"]
    baseline_threads = _process_threads(server_pid)
    wave.update({
        "phase": phase,
        "embedding_before": embedding_before,
        "embedding_at_sync_completion": None,
        "saturation": None,
        "all_sync_completed_before_embedding_release": False,
        "probes": [],
        "sync": _wave_summary([]),
    })
    checkpoint()
    state.set_embed_phase(phase, released=not gated_embedding)
    headers = [
        {"X-Xskill-Token": join_token, "X-Xskill-Client": row["client_id"]}
        for row in client_rows
    ]
    tasks = [asyncio.create_task(_sync_one(client, hdr, index)) for index, hdr in enumerate(headers)]
    started_at = time.monotonic()
    completed_before_release = False
    phase_error: Exception | None = None
    try:
        if gated_embedding:
            additional_starts = max(
                0, min(1, len(client_rows)) - int(embedding_before["active"]),
            )
            target_started = (
                int(embedding_before["request_count"]) + additional_starts
            )
            await _wait_for(
                lambda: state.snapshot()["embedding"]["request_count"] >= target_started,
                timeout=60,
                description=f"{phase} embedding requests to reach {target_started}",
            )
            remaining_sla = max(0.0, sync_max_s - (time.monotonic() - started_at))
            _, pending = await asyncio.wait(tasks, timeout=remaining_sla)
            completed_before_release = not pending
            wave["saturation"] = {
                "baseline_process_threads": baseline_threads,
                "observed_process_threads": _process_threads(server_pid),
                "embedding_snapshot": state.snapshot()["embedding"],
                "pending_sync_requests": len(pending),
                "all_sync_completed_before_embedding_release": completed_before_release,
            }
            wave["all_sync_completed_before_embedding_release"] = completed_before_release
            wave["probes"] = await _control_plane_probes(
                client, auth_headers=headers[0],
            )
            checkpoint()
    except Exception as exc:  # noqa: BLE001
        # Release the backend below and still collect every sync result.  This
        # is what prevents a synchronous-embedding regression from deadlocking
        # the test before it can write useful evidence.
        phase_error = exc
        wave["error"] = f"{type(exc).__name__}: {exc}"
        checkpoint()
    finally:
        if gated_embedding:
            state.embed_release.set()

    results = await asyncio.gather(*tasks, return_exceptions=True)
    normalized_results: list[dict[str, Any]] = []
    for index, item in enumerate(results):
        if isinstance(item, BaseException):
            normalized_results.append({
                "index": index,
                "status": None,
                "elapsed_s": time.monotonic() - started_at,
                "error": f"{type(item).__name__}: {item}",
            })
        else:
            normalized_results.append(item)

    if not gated_embedding:
        completed_before_release = True
        try:
            wave["probes"] = await _control_plane_probes(
                client, auth_headers=headers[0],
            )
        except Exception as exc:  # defensive: _probe normally captures errors
            phase_error = phase_error or exc
            wave["error"] = f"{type(exc).__name__}: {exc}"

    wave.update({
        "embedding_at_sync_completion": state.snapshot()["embedding"],
        "all_sync_completed_before_embedding_release": completed_before_release,
        "sync": _wave_summary(normalized_results),
    })
    checkpoint()
    if phase_error is not None:
        # The caller records profile/shutdown diagnostics in its finally block.
        raise phase_error
    return wave


def _seed_delta_atoms(
    client_rows: list[dict[str, str]], *, registry_db: Path,
) -> None:
    from xskill.recommend.profile_dirty import mark_profile_dirty_for_store

    for row in client_rows:
        index = int(row["index"])
        _write_atom(
            Path(row["root"]),
            traj_id=f"traj_client_{index:03d}",
            atom_id=f"atom_client_{index:03d}_0002",
            summary=f"delta summary client {index:03d}",
        )
        mark_profile_dirty_for_store(
            row["root"], reason="loadtest_delta", db_path=registry_db,
        )


def _skill_convergence(skill_root: Path, expected: int) -> dict[str, Any]:
    if not skill_root.is_dir():
        return {
            "expected": expected, "skill_dirs": 0, "main_count": 0,
            "candidates_empty": 0, "cross_contamination_count": expected,
        }
    skill_dirs = [p for p in skill_root.iterdir() if p.is_dir() and not p.name.startswith(".")]
    main_count = 0
    candidates_empty = 0
    cross_contamination = 0
    for skill_dir in skill_dirs:
        if (skill_dir / ".git" / "refs" / "heads" / "main").is_file():
            main_count += 1
        candidate_path = skill_dir / ".candidates.yml"
        if candidate_path.is_file() and "candidates: []" in candidate_path.read_text(encoding="utf-8"):
            candidates_empty += 1
        skill_md = skill_dir / "SKILL.md"
        if skill_md.is_file():
            body = skill_md.read_text(encoding="utf-8")
            own_marker = f"Mock-generated content for {skill_dir.name}."
            if own_marker not in body and "placeholder" not in body:
                cross_contamination += 1
    return {
        "expected": expected,
        "skill_dirs": len(skill_dirs),
        "main_count": main_count,
        "candidates_empty": candidates_empty,
        "cross_contamination_count": cross_contamination,
    }


def _expected_profile_revisions(
    client_rows: list[dict[str, str]],
) -> dict[str, str]:
    revisions: dict[str, str] = {}
    for row in client_rows:
        atoms: list[dict[str, Any]] = []
        for atom_path in Path(row["root"]).glob("*/tasks/*.json"):
            payload = json.loads(atom_path.read_text(encoding="utf-8"))
            atoms.append({
                "atom_id": payload.get("atom_id"),
                "summary": payload.get("summary") or "",
                "used_skills": sorted(payload.get("used_skills") or []),
                "ux_score": payload.get("ux_score"),
                "tags": sorted(payload.get("tags") or []),
            })
        encoded = json.dumps(
            sorted(atoms, key=lambda item: item["atom_id"]),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        revisions[row["client_id"]] = hashlib.sha256(encoded).hexdigest()
    return revisions


def _profile_convergence(
    xhome: Path,
    expected: int,
    client_rows: list[dict[str, str]],
) -> dict[str, Any]:
    db_path = xhome / "team_profile.db"
    if not db_path.is_file():
        return {
            "expected": expected, "rows": 0, "revision_rows": 0,
            "revision_matches": 0, "embed_model_rows": 0,
            "point_meta_items": 0,
        }
    try:
        conn = sqlite3.connect(str(db_path), timeout=5)
        try:
            rows = conn.execute(
                "SELECT user_id, source_revision, embed_model, point_meta"
                " FROM client_interest",
            ).fetchall()
        finally:
            conn.close()
        expected_revisions = _expected_profile_revisions(client_rows)
    except Exception as exc:  # noqa: BLE001
        return {
            "expected": expected,
            "rows": 0,
            "revision_rows": 0,
            "revision_matches": 0,
            "embed_model_rows": 0,
            "point_meta_items": 0,
            "error": f"{type(exc).__name__}: {exc}",
        }
    point_meta_items = 0
    for _, _, _, raw_meta in rows:
        try:
            point_meta_items += len(json.loads(raw_meta or "[]"))
        except (TypeError, json.JSONDecodeError):
            pass
    return {
        "expected": expected,
        "rows": len(rows),
        "revision_rows": sum(bool(row[1]) for row in rows),
        "revision_matches": sum(
            expected_revisions.get(row[0]) == row[1] for row in rows
        ),
        "embed_model_rows": sum(row[2] == "mock-embed-model" for row in rows),
        "point_meta_items": point_meta_items,
    }


def _health_sync(base_url: str) -> bool:
    import httpx
    try:
        return httpx.get(f"{base_url}/api/v1/health", timeout=0.5).status_code == 200
    except Exception:  # noqa: BLE001
        return False


async def run_scenario(
    args: argparse.Namespace,
    run_dir: Path,
    result_path: Path,
    state: MockState,
    prepared: dict[str, Any],
    result: dict[str, Any],
) -> None:
    import httpx

    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    server_log_path = run_dir / "xskill-server.log"
    server_log = server_log_path.open("wb")
    env = os.environ.copy()
    env["HOME"] = prepared["server_home"]
    env["PYTHONUSERBASE"] = ORIGINAL_USER_BASE
    env["PYTHONPATH"] = str(REPO_ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONUNBUFFERED"] = "1"
    process = subprocess.Popen(
        [sys.executable, "-m", "xskill.cli", "serve", "--server", "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(REPO_ROOT), env=env, stdout=server_log, stderr=subprocess.STDOUT,
    )
    result["server"] = {
        "pid": process.pid,
        "base_url": base_url,
        "log": str(server_log_path),
        "shutdown": {"return_code": None, "forced_kill": False, "clean": False},
    }
    monitor = ThreadMonitor(process.pid)
    monitor.start()
    thread_snapshots: list[dict[str, Any]] = []
    result["threads"] = {
        "snapshots": thread_snapshots,
        "pre": None,
        "final": None,
        "sample_count": 0,
        "peak": None,
        "samples": [],
    }
    result["waves"] = {}
    result["profile_metrics"] = {}
    def checkpoint() -> None:
        _write_result_snapshot(result, result_path)

    checkpoint()

    limits = httpx.Limits(
        max_connections=max(args.clients + 50, 400),
        max_keepalive_connections=100,
    )
    async with httpx.AsyncClient(base_url=base_url, limits=limits) as client:
        try:
            await _wait_for(
                lambda: process.poll() is not None or _health_sync(base_url),
                timeout=60,
                description="xskill server startup",
                interval=0.2,
            )
            if process.poll() is not None:
                raise RuntimeError(f"xskill server exited early with {process.returncode}")

            await _wait_for(
                lambda: state.snapshot()["llm"]["initial_requests"] >= min(
                    args.skills, args.max_concurrent,
                ),
                timeout=90,
                description="SkillEdit workers to enter mock LLM",
            )
            thread_snapshots.append(monitor.snapshot("pre_load"))
            result["pre_load"] = {
                "process_threads": _process_threads(process.pid),
                "mock": state.snapshot(),
                "profile_refresh": await _profile_metrics(client),
            }
            result["threads"]["pre"] = result["pre_load"]["process_threads"]
            checkpoint()

            cold_wave: dict[str, Any] = {}
            result["waves"]["cold"] = cold_wave
            cold_status_before = await _profile_metrics(client)
            cold_ended_at_before = _profile_round_ended_at(cold_status_before)
            cold_wave["profile_ended_at_before"] = cold_ended_at_before
            watcher_status_before = await _watcher_status(client)
            watcher_ended_at_before = _profile_round_ended_at(
                watcher_status_before
            )
            cold_wave["watcher_ended_at_before"] = watcher_ended_at_before
            await run_sync_wave(
                client,
                phase="cold",
                client_rows=prepared["clients"],
                join_token=prepared["join_token"],
                state=state,
                server_pid=process.pid,
                gated_embedding=True,
                wave=cold_wave,
                checkpoint=checkpoint,
            )
            thread_snapshots.append(monitor.snapshot("cold_sync_complete"))
            state.llm_release.set()
            watcher_evidence = await _wait_watcher_target(
                client,
                after_ended_at=watcher_ended_at_before,
                expected_skills=args.skills,
                timeout=args.convergence_timeout,
            )
            cold_wave["watcher_rounds"] = watcher_evidence["rounds"]
            result["watcher_evidence"] = watcher_evidence
            cold_metrics = await _wait_profile_idle(
                client, after_ended_at=cold_ended_at_before,
                timeout=args.convergence_timeout,
            )
            cold_wave["profile_idle_metrics"] = cold_metrics
            cold_wave["embedding_after_idle"] = state.snapshot()["embedding"]
            result["profile_metrics"]["after_cold"] = cold_metrics
            thread_snapshots.append(monitor.snapshot("cold_profiles_idle"))
            checkpoint()

            cached_wave: dict[str, Any] = {}
            result["waves"]["cache_hit"] = cached_wave
            cached_status_before = await _profile_metrics(client)
            cached_ended_at_before = _profile_round_ended_at(cached_status_before)
            cached_wave["profile_ended_at_before"] = cached_ended_at_before
            await run_sync_wave(
                client,
                phase="cache_hit",
                client_rows=prepared["clients"],
                join_token=prepared["join_token"],
                state=state,
                server_pid=process.pid,
                gated_embedding=False,
                wave=cached_wave,
                checkpoint=checkpoint,
            )
            cached_metrics = await _wait_profile_idle(
                client, after_ended_at=cached_ended_at_before,
                timeout=args.convergence_timeout,
            )
            cached_wave["profile_idle_metrics"] = cached_metrics
            cached_wave["embedding_after_idle"] = state.snapshot()["embedding"]
            result["profile_metrics"]["after_cache_hit"] = cached_metrics
            thread_snapshots.append(monitor.snapshot("cache_profiles_idle"))
            checkpoint()

            delta_wave: dict[str, Any] = {}
            result["waves"]["one_new_atom"] = delta_wave
            delta_status_before = await _profile_metrics(client)
            delta_ended_at_before = _profile_round_ended_at(delta_status_before)
            delta_wave["profile_ended_at_before"] = delta_ended_at_before
            # 先关 embedding gate 再写增量，避免 1s 周期恰好在基线与写入之间
            # 启动并完成，导致本波变化被上一轮提前消费。
            state.set_embed_phase("one_new_atom", released=False)
            _seed_delta_atoms(
                prepared["clients"],
                registry_db=Path(prepared["xhome"]) / "registry.db",
            )
            await run_sync_wave(
                client,
                phase="one_new_atom",
                client_rows=prepared["clients"],
                join_token=prepared["join_token"],
                state=state,
                server_pid=process.pid,
                gated_embedding=True,
                wave=delta_wave,
                checkpoint=checkpoint,
            )
            delta_metrics = await _wait_profile_idle(
                client, after_ended_at=delta_ended_at_before,
                timeout=args.convergence_timeout,
            )
            delta_wave["profile_idle_metrics"] = delta_metrics
            delta_wave["embedding_after_idle"] = state.snapshot()["embedding"]
            result["profile_metrics"]["after_one_new_atom"] = delta_metrics
            thread_snapshots.append(monitor.snapshot("delta_profiles_idle"))
            checkpoint()

            skill_root = Path(prepared["skill_root"])
            await _wait_for(
                lambda: _skill_convergence(skill_root, args.skills)["main_count"] >= args.skills,
                timeout=args.convergence_timeout,
                description="all SkillEdit tasks to graduate",
                interval=0.5,
            )
            # /watcher/status 只保存最近一轮。成功证据已在 cold 波期间按 ended_at
            # 捕获并累计，最终空轮不得覆盖；这里仅保留最新状态供诊断。
            latest_watcher_status = await _watcher_status(client)
            if latest_watcher_status is None or not latest_watcher_status["ok"]:
                safe_status = _safe_watcher_status(latest_watcher_status, None)
                raise RuntimeError(
                    f"final watcher status is not successful: {safe_status}"
                )
            result["watcher_final_state"] = _safe_watcher_status(
                latest_watcher_status, None,
            )
            result["final_probes"] = await _control_plane_probes(
                client,
                auth_headers={
                    "X-Xskill-Token": prepared["join_token"],
                    "X-Xskill-Client": prepared["clients"][0]["client_id"],
                },
            )
            # delta 已等待到一个严格校验的新空闲轮；此后没有画像业务写入。
            # 不为“最终态”人为再等一轮空任务，避免慢子进程清理造成假失败。
            result["profile_metrics"]["final_idle"] = delta_metrics
            result["skill_convergence"] = _skill_convergence(skill_root, args.skills)
            result["profile_convergence"] = _profile_convergence(
                Path(prepared["xhome"]), args.clients, prepared["clients"],
            )
            result["cold_start_signal_exists"] = (
                Path(prepared["xhome"]) / "COLD_START"
            ).exists()
            result["watcher_final"] = await _probe(
                client, "GET", "/api/v1/watcher/status", timeout=3,
            )
            thread_snapshots.append(monitor.snapshot("final_convergence"))
            checkpoint()
        finally:
            state.llm_release.set()
            state.embed_release.set()
            result["mock"] = state.snapshot()
            result["profile_metrics"]["before_shutdown"] = result[
                "profile_metrics"
            ].get("final_idle")
            if not result.get("final_probes"):
                result["final_probes"] = await _control_plane_probes(
                    client,
                    auth_headers={
                        "X-Xskill-Token": prepared["join_token"],
                        "X-Xskill-Client": prepared["clients"][0]["client_id"],
                    },
                )
            result.setdefault(
                "skill_convergence",
                _skill_convergence(Path(prepared["skill_root"]), args.skills),
            )
            result.setdefault(
                "profile_convergence",
                _profile_convergence(
                    Path(prepared["xhome"]), args.clients, prepared["clients"],
                ),
            )
            result.setdefault(
                "cold_start_signal_exists",
                (Path(prepared["xhome"]) / "COLD_START").exists(),
            )
            if "watcher_final_state" not in result:
                try:
                    latest_watcher_status = await _watcher_status(client)
                    if (
                        latest_watcher_status is None
                        or not latest_watcher_status["ok"]
                    ):
                        safe_status = _safe_watcher_status(
                            latest_watcher_status, None,
                        )
                        raise RuntimeError(
                            f"final watcher status is not successful: {safe_status}"
                        )
                    result["watcher_final_state"] = _safe_watcher_status(
                        latest_watcher_status, None,
                    )
                except Exception as exc:  # noqa: BLE001 — 诊断边界,记录安全类型
                    error_type = type(exc).__name__
                    logger.warning(
                        "final watcher status poll failed: %s", error_type,
                    )
                    result["watcher_final_state"] = {
                        "ok": None,
                        "ended_at": None,
                        "stats": {},
                        "error_present": False,
                        "poll_error_type": error_type,
                    }
            thread_snapshots.append(monitor.snapshot("before_shutdown"))
            result["threads"]["final"] = thread_snapshots[-1]["count"]
            result["threads"].update(monitor.stop())
            checkpoint()
            forced_kill = False
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    forced_kill = True
                    process.kill()
                    process.wait(timeout=10)
            result["server"]["shutdown"] = {
                "return_code": process.returncode,
                "forced_kill": forced_kill,
                "clean": not forced_kill,
            }
            server_log.close()
            log_text = server_log_path.read_text(encoding="utf-8", errors="replace")
            result["diagnostics"] = {
                "database_locked_count": log_text.lower().count("database is locked"),
                "traceback_count": log_text.count("Traceback (most recent call last)"),
            }
            checkpoint()


def _all_probes(result: dict[str, Any]) -> list[dict[str, Any]]:
    probes: list[dict[str, Any]] = []
    for wave in result.get("waves", {}).values():
        probes.extend(wave.get("probes", []))
    probes.extend(result.get("final_probes", []))
    return probes


def validate_result(result: dict[str, Any]) -> list[str]:
    """Return every failed acceptance condition instead of stopping at the first."""
    failures: list[str] = []
    config = result["config"]
    clients = int(config["clients"])
    skills = int(config["skills"])
    workers = int(config["profile_refresh_workers"])
    expected_slots = min(skills, int(config["skill_slots"]))

    for phase in ("cold", "cache_hit", "one_new_atom"):
        wave = result.get("waves", {}).get(phase)
        if not wave:
            failures.append(f"missing wave {phase}")
            continue
        if wave.get("error"):
            failures.append(f"{phase}: wave error={wave['error']}")
        sync = wave["sync"]
        if sync["requests"] != clients or sync["statuses"] != {"200": clients}:
            failures.append(f"{phase}: sync statuses={sync['statuses']}")
        slot_counts = sync.get("slot_counts") or {}
        try:
            observed_slot_counts = {
                int(count): int(requests)
                for count, requests in slot_counts.items()
            }
        except (TypeError, ValueError):
            observed_slot_counts = {-1: 0}
        if (
            sum(observed_slot_counts.values()) != clients
            or any(count < 0 or count > expected_slots
                   for count in observed_slot_counts)
        ):
            failures.append(f"{phase}: sync slot counts={slot_counts}")
        latency = sync["latency"]
        # 300 个请求同刻到达且 watcher 正在落 300 个 Git 仓时，实测尾延迟
        # 会包含单进程事件循环调度；6 秒 p95 / 7 秒最大值仍能区分正常
        # 退化与基线中的超时、连接失败和线程池饥饿。
        if latency["p95_s"] is None or float(latency["p95_s"]) >= 6:
            failures.append(f"{phase}: sync p95={latency['p95_s']}")
        if latency["max_s"] is None or float(latency["max_s"]) >= 7:
            failures.append(f"{phase}: sync max={latency['max_s']}")
        if phase != "cache_hit" and not wave.get("all_sync_completed_before_embedding_release"):
            failures.append(f"{phase}: sync remained blocked by embedding gate")
        completed = wave.get("profile_idle_metrics", {})
        if completed.get("profile_rc") != 0:
            failures.append(f"{phase}: recommend-heavy did not complete: {completed}")

    probes = _all_probes(result)
    for probe in probes:
        if probe.get("status") != 200:
            failures.append(f"probe failed: {probe}")
        if (
            probe.get("path") == "/"
            and float(probe.get("elapsed_s", 99)) >= 1.5
        ):
            failures.append(f"dashboard index probe too slow: {probe}")
        if (
            probe.get("path") in {
                "/api/v1/dashboard/overview", "/api/v1/dashboard/skills",
            }
            and float(probe.get("elapsed_s", 99)) >= 2.5
        ):
            failures.append(f"dashboard data probe too slow: {probe}")

    mock = result.get("mock", {})
    llm = mock.get("llm", {})
    if (
        llm.get("initial_requests") != skills
        or llm.get("followup_requests") != 2 * skills
    ):
        failures.append(f"LLM request split incorrect: {llm}")
    if llm.get("started") != 3 * skills or llm.get("completed") != 3 * skills:
        failures.append(f"LLM completion count incorrect: {llm}")
    if int(llm.get("max_active", workers + 1)) > int(config["llm_max_inflight"]):
        failures.append(f"LLM active limit exceeded: {llm.get('max_active')}")

    embedding = mock.get("embedding", {})
    items_by_phase = embedding.get("items_by_phase", {})
    expected_items = {
        "cold": skills + clients,
        "cache_hit": 0,
        "one_new_atom": clients,
    }
    actual_items = {phase: int(items_by_phase.get(phase, 0)) for phase in expected_items}
    if actual_items != expected_items:
        failures.append(f"embedding phase items incorrect: {actual_items}")
    requests_by_phase = embedding.get("requests_by_phase", {})
    actual_requests = {
        phase: int(requests_by_phase.get(phase, 0)) for phase in expected_items
    }
    if actual_requests != expected_items:
        failures.append(f"embedding phase requests incorrect: {actual_requests}")
    if embedding.get("input_item_count") != skills + 2 * clients:
        failures.append(f"embedding item count incorrect: {embedding.get('input_item_count')}")
    if embedding.get("request_count") != skills + 2 * clients:
        failures.append(f"embedding request count incorrect: {embedding.get('request_count')}")
    if embedding.get("unique_inputs") != skills + 2 * clients:
        failures.append(f"embedding unique inputs incorrect: {embedding.get('unique_inputs')}")
    if embedding.get("duplicate_input_calls") != 0:
        failures.append(f"embedding duplicate inputs: {embedding.get('duplicate_input_calls')}")
    if int(embedding.get("max_active", workers + 1)) > workers:
        failures.append(f"embedding active limit exceeded: {embedding.get('max_active')}")

    final_metrics = result.get("profile_metrics", {}).get("final_idle", {})
    if (
        not isinstance(final_metrics, dict)
        or type(final_metrics.get("profile_rc")) is not int
        or final_metrics["profile_rc"] != 0
    ):
        failures.append(f"recommend-heavy final state is invalid: {final_metrics}")
    profile_rounds = []
    for round_name in ("after_cold", "after_cache_hit", "after_one_new_atom"):
        metrics = result.get("profile_metrics", {}).get(round_name)
        if not isinstance(metrics, dict):
            failures.append(f"profile refresh round missing: {round_name}")
            continue
        invalid_fields = [
            field_name
            for field_name in RECOMMEND_HEAVY_COUNT_FIELDS
            if type(metrics.get(field_name)) is not int
        ]
        if invalid_fields:
            failures.append(
                f"profile refresh round {round_name} invalid fields={invalid_fields}"
            )
            continue
        profile_rounds.append(metrics)
    if len(profile_rounds) == 3 and any(
        metrics["profile_rc"] != 0 for metrics in profile_rounds
    ):
        failures.append(f"profile refresh failures: {profile_rounds}")

    skill = result.get("skill_convergence", {})
    if (
        skill.get("skill_dirs") != skills
        or skill.get("main_count") != skills
        or skill.get("candidates_empty") != skills
        or skill.get("cross_contamination_count") != 0
    ):
        failures.append(f"skill convergence failed: {skill}")
    profile = result.get("profile_convergence", {})
    if (
        profile.get("rows") != clients
        or profile.get("revision_rows") != clients
        or profile.get("revision_matches") != clients
        or profile.get("embed_model_rows") != clients
        or profile.get("point_meta_items") != 2 * clients
    ):
        failures.append(f"profile convergence failed: {profile}")
    watcher_evidence = result.get("watcher_evidence", {})
    watcher_skills_edited = watcher_evidence.get("skills_edited")
    watcher_errors = watcher_evidence.get("errors")
    watcher_rounds = watcher_evidence.get("rounds")
    if (
        not isinstance(watcher_skills_edited, int)
        or isinstance(watcher_skills_edited, bool)
        or watcher_skills_edited < skills
    ):
        failures.append(
            f"watcher did not harvest every SkillEdit result: {watcher_evidence}"
        )
    if (
        not isinstance(watcher_errors, int)
        or isinstance(watcher_errors, bool)
        or watcher_errors != 0
    ):
        failures.append(f"watcher reported errors: {watcher_evidence}")
    if not isinstance(watcher_rounds, list) or not watcher_rounds:
        failures.append(f"watcher round evidence missing: {watcher_evidence}")
    else:
        for round_status in watcher_rounds:
            round_stats = (
                round_status.get("stats")
                if isinstance(round_status, dict) else None
            )
            round_errors = (
                round_stats.get("errors")
                if isinstance(round_stats, dict) else None
            )
            if (
                not isinstance(round_errors, int)
                or isinstance(round_errors, bool)
                or round_errors != 0
            ):
                failures.append(
                    f"watcher round reported invalid errors: {round_status}"
                )
    final_watcher_status = result.get("watcher_final_state")
    if not isinstance(final_watcher_status, dict):
        failures.append(f"final watcher status missing: {final_watcher_status}")
    else:
        final_watcher_stats = final_watcher_status.get("stats")
        invalid_final_fields = []
        if not isinstance(final_watcher_stats, dict):
            invalid_final_fields.append("stats")
        else:
            invalid_final_fields.extend(
                key
                for key in WATCHER_COUNT_FIELDS
                if (
                    not isinstance(final_watcher_stats.get(key), int)
                    or isinstance(final_watcher_stats.get(key), bool)
                    or final_watcher_stats[key] < 0
                )
            )
            if not isinstance(final_watcher_stats.get("running"), bool):
                invalid_final_fields.append("running")
            if not isinstance(final_watcher_stats.get("paused"), bool):
                invalid_final_fields.append("paused")
            final_last_scan = final_watcher_stats.get("last_scan")
            if (
                not isinstance(final_last_scan, (int, float))
                or isinstance(final_last_scan, bool)
                or final_last_scan < 0
            ):
                invalid_final_fields.append("last_scan")
        if final_watcher_status.get("ok") is not True:
            invalid_final_fields.append("ok")
        final_ended_at = final_watcher_status.get("ended_at")
        if (
            not isinstance(final_ended_at, (int, float))
            or isinstance(final_ended_at, bool)
            or final_ended_at < 0
        ):
            invalid_final_fields.append("ended_at")
        if final_watcher_status.get("error_present") is not False:
            invalid_final_fields.append("error_present")
        if final_watcher_status.get("poll_error_type") is not None:
            invalid_final_fields.append("poll_error_type")
        if (
            isinstance(final_watcher_stats, dict)
            and final_watcher_stats.get("errors") != 0
        ):
            invalid_final_fields.append("errors")
        if invalid_final_fields:
            failures.append(
                "final watcher status contract failed: "
                f"fields={sorted(set(invalid_final_fields))}"
            )
    if result.get("cold_start_signal_exists"):
        failures.append("COLD_START signal still exists")
    if result.get("diagnostics", {}).get("database_locked_count") != 0:
        failures.append(
            f"database is locked count={result['diagnostics']['database_locked_count']}",
        )
    traceback_count = result.get("diagnostics", {}).get("traceback_count")
    if (
        not isinstance(traceback_count, int)
        or isinstance(traceback_count, bool)
        or traceback_count < 0
        or traceback_count != 0
    ):
        failures.append(f"unexpected traceback count={traceback_count!r}")
    if not result.get("server", {}).get("shutdown", {}).get("clean"):
        failures.append(f"server shutdown was not clean: {result.get('server', {}).get('shutdown')}")
    return failures


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--skills", type=int, default=300)
    parser.add_argument("--clients", type=int, default=300)
    parser.add_argument("--max-concurrent", type=int, default=30)
    parser.add_argument("--thread-pool-tokens", type=int, default=80)
    parser.add_argument("--team-sync-workers", type=int, default=32)
    parser.add_argument("--profile-refresh-workers", type=int, default=30)
    parser.add_argument("--profile-refresh-queue-size", type=int, default=1024)
    parser.add_argument("--profile-refresh-shutdown-timeout", type=float, default=10.0)
    parser.add_argument("--llm-delay", type=float, default=12.0)
    parser.add_argument("--embed-delay", type=float, default=23.0)
    parser.add_argument("--skill-slots", type=int, default=100)
    parser.add_argument("--convergence-timeout", type=float, default=900.0)
    parser.add_argument(
        "--artifact-root", type=Path, default=Path("/home/admin/xskill-loadtest-results"),
    )
    args = parser.parse_args()
    if args.skills < 1:
        parser.error("--skills must be >= 1")
    if args.clients < 1:
        parser.error("--clients must be >= 1")
    if args.max_concurrent < 1:
        parser.error("--max-concurrent must be >= 1")
    if args.team_sync_workers < 1:
        parser.error("--team-sync-workers must be >= 1")
    if args.skill_slots < 0:
        parser.error("--skill-slots must be >= 0")
    if args.profile_refresh_queue_size < args.clients:
        parser.error("--profile-refresh-queue-size must be >= --clients")
    if args.profile_refresh_workers < 1:
        parser.error("--profile-refresh-workers must be >= 1")
    return args


def main() -> int:
    args = parse_args()
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    run_dir = args.artifact_root / (
        f"run-{timestamp}-{os.getpid()}-s{args.skills}-c{args.clients}"
    )
    run_dir.mkdir(parents=True, exist_ok=False)
    result_path = run_dir / "result.json"
    state = MockState(llm_delay=args.llm_delay, embed_delay=args.embed_delay)
    mock = MockServer(state)
    started = time.monotonic()
    result: dict[str, Any] = {
        "run_dir": str(run_dir),
        "result_path": str(result_path),
        "repo_root": str(REPO_ROOT),
        "commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, text=True,
        ).strip(),
        "config": {
            "skills": args.skills,
            "clients": args.clients,
            "llm_max_inflight": args.max_concurrent,
            "thread_pool_tokens": args.thread_pool_tokens,
            "team_sync_workers": args.team_sync_workers,
            "skill_slots": args.skill_slots,
            "profile_refresh_workers": args.profile_refresh_workers,
            "profile_refresh_queue_size": args.profile_refresh_queue_size,
            "profile_refresh_shutdown_timeout_s": args.profile_refresh_shutdown_timeout,
            "profile_refresh_interval_s": PROFILE_REFRESH_INTERVAL_S,
            "llm_delay_s": args.llm_delay,
            "embedding_delay_s": args.embed_delay,
        },
        "setup": None,
        "server": {
            "pid": None,
            "base_url": None,
            "log": None,
            "shutdown": {
                "return_code": None,
                "forced_kill": False,
                "clean": False,
                "not_started": True,
            },
        },
        "waves": {},
        "final_probes": [],
        "profile_metrics": {},
        "threads": {
            "snapshots": [],
            "pre": None,
            "final": None,
            "sample_count": 0,
            "peak": None,
            "samples": [],
        },
    }
    _write_result_snapshot(result, result_path)
    return_code = 1
    try:
        mock.start()
        prepared = prepare_home(
            run_dir,
            mock.base_url,
            skills=args.skills,
            clients=args.clients,
            max_concurrent=args.max_concurrent,
            thread_pool_tokens=args.thread_pool_tokens,
            team_sync_workers=args.team_sync_workers,
            profile_refresh_workers=args.profile_refresh_workers,
            profile_refresh_queue_size=args.profile_refresh_queue_size,
            profile_refresh_shutdown_timeout=args.profile_refresh_shutdown_timeout,
            skill_slots=args.skill_slots,
        )
        result["setup"] = {
            "server_home": prepared["server_home"],
            "skill_root": prepared["skill_root"],
            "traj_root": prepared["traj_root"],
            "setup_s": prepared["setup_s"],
        }
        _write_result_snapshot(result, result_path)
        asyncio.run(run_scenario(
            args, run_dir, result_path, state, prepared, result,
        ))
    except Exception as exc:  # noqa: BLE001
        result["fatal_error"] = f"{type(exc).__name__}: {exc}"
    finally:
        state.llm_release.set()
        state.embed_release.set()
        result["mock"] = state.snapshot()
        result["mock_shutdown_clean"] = mock.stop()
        result["total_elapsed_s"] = time.monotonic() - started
        try:
            result["validation_failures"] = validate_result(result)
        except Exception as exc:  # noqa: BLE001
            result["validation_failures"] = [f"validation error: {type(exc).__name__}: {exc}"]
        if result.get("fatal_error"):
            result["validation_failures"].insert(0, result["fatal_error"])
        if not result["mock_shutdown_clean"]:
            result["validation_failures"].append("mock backend did not shut down cleanly")
        result["success"] = not result["validation_failures"]
        return_code = 0 if result["success"] else 1
        _write_result_snapshot(result, result_path)
        print(json.dumps({
            "success": result["success"],
            "run_dir": str(run_dir),
            "result": str(result_path),
            "fatal_error": result.get("fatal_error"),
            "validation_failures": result["validation_failures"],
            "elapsed_s": result["total_elapsed_s"],
        }, ensure_ascii=False), flush=True)
    return return_code


if __name__ == "__main__":
    raise SystemExit(main())
