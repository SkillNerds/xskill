"""SkillHub/dashboard 并发扫描不得饿死 ASGI 事件循环。"""
from __future__ import annotations

import asyncio
import math
import threading
import time
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from xskill.dashboard.metrics import DashboardMetrics
from xskill.pipeline.atom import AtomTask, AtomTaskStore
from xskill.pipeline.registry import get_connection
from xskill.recommend.skillhub import SkillHub


def _write_skill(root: Path, index: int) -> str:
    name = f"hub-skill-{index:03d}"
    skill_dir = root / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        f"name: {name}\n"
        f"description: Load-test helper {index:03d} for skillhub scanning\n"
        "---\n\n"
        f"# {name}\n\n" + ("fixture body\n" * 32),
        encoding="utf-8",
    )
    return name


def _write_atom(store: AtomTaskStore, index: int) -> None:
    store.save(AtomTask(
        atom_id=f"atom_load_{index:04d}",
        traj_id=f"traj_load_{index:04d}",
        offset_start=1,
        offset_end=2,
        intent="load test",
        summary="dashboard tag fixture",
        tags=["dashboard", f"bucket-{index % 10}"],
        used_skills=[],
    ))


async def _sample_health(client: httpx.AsyncClient, duration: float) -> list[float]:
    latencies: list[float] = []
    deadline = time.monotonic() + duration
    while time.monotonic() < deadline:
        started = time.monotonic()
        response = await client.get("/api/v1/health")
        latencies.append(time.monotonic() - started)
        assert response.status_code == 200
        await asyncio.sleep(0.01)
    return latencies


def _percentile(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, math.ceil(percentile * len(ordered)) - 1)
    return ordered[index]


@pytest.mark.stress
@pytest.mark.timeout(30)
def test_skillhub_and_dashboard_load_keeps_health_responsive(
        tmp_path, monkeypatch) -> None:
    hub_dir = tmp_path / "skillhub"
    hub_dir.mkdir()
    names = [_write_skill(hub_dir, index) for index in range(300)]

    # 旧 rglob 会进入点目录并递归这些无关层级；新扫描必须在 .git 处剪枝。
    hidden_root = hub_dir / ".git" / "objects" / "deep" / "nested" / "tree"
    for index in range(200):
        hidden = hidden_root / f"object-{index:03d}"
        hidden.mkdir(parents=True)
        (hidden / "SKILL.md").write_text("ignored", encoding="utf-8")

    atom_root = tmp_path / "atoms"
    store = AtomTaskStore(atom_root)
    for index in range(300):
        _write_atom(store, index)
    db = tmp_path / "registry.db"
    conn = get_connection(db)
    conn.execute(
        "INSERT INTO watch_dirs(path,label,ecosystem) VALUES(?,?,?)",
        (str(atom_root), "load-user", "team_client"),
    )
    conn.commit()
    conn.close()

    hub = SkillHub(enabled=True, hub_dir=hub_dir, embed_client=None)
    metrics = DashboardMetrics(db_path=db)
    scan_counts = {"skillhub": 0, "atoms": 0}
    count_lock = threading.Lock()
    original_hub_scan = hub._scan_entries
    original_all_atoms = AtomTaskStore.all_atoms

    def counted_hub_scan():
        with count_lock:
            scan_counts["skillhub"] += 1
        return original_hub_scan()

    def counted_all_atoms(self):
        with count_lock:
            scan_counts["atoms"] += 1
        yield from original_all_atoms(self)

    monkeypatch.setattr(hub, "_scan_entries", counted_hub_scan)
    monkeypatch.setattr(AtomTaskStore, "all_atoms", counted_all_atoms)

    app = FastAPI()

    @app.get("/api/v1/health")
    async def health():
        return {"status": "ok"}

    @app.get("/api/v1/team/sync")
    def sync():
        return {"hits": sum(hub.entry(name) is not None for name in names[:100])}

    @app.get("/api/v1/dashboard/tags")
    def dashboard_tags():
        return {"tags": metrics.tag_cloud()}

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://stress",
        ) as client:
            health_task = asyncio.create_task(_sample_health(client, 1.0))
            await asyncio.sleep(0)
            requests = [
                client.get("/api/v1/team/sync") for _ in range(30)
            ] + [
                client.get("/api/v1/dashboard/tags") for _ in range(10)
            ]
            responses = await asyncio.wait_for(
                asyncio.gather(*requests), timeout=15,
            )
            latencies = await health_task
            return responses, latencies

    responses, health_latencies = asyncio.run(scenario())

    assert all(response.status_code == 200 for response in responses)
    assert all(response.json().get("hits") == 100 for response in responses[:30])
    assert scan_counts == {"skillhub": 1, "atoms": 1}
    assert len(health_latencies) >= 20
    assert _percentile(health_latencies, 0.99) < 0.5
