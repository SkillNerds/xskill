"""team /sync 的缓存响应和后台画像刷新接线。"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, wait
import json
import pickle
import subprocess
import threading
from pathlib import Path

import numpy as np
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from xskill.recommend.engine import SkillRecommendEngine
from xskill.team.server import api as server_api
from xskill.team.server.client_registry import ClientRegistry
from xskill.team.server.profile_refresh import ProfileRefreshService
from xskill.team.server.skill_manifest import set_recommend_engine


def _git(args, cwd):
    subprocess.run(
        ["git"] + args, cwd=str(cwd), capture_output=True, text=True, check=True,
    )


def _make_main_skill(parent: Path, name: str):
    directory = parent / name
    directory.mkdir(parents=True)
    _git(["init", "-q"], directory)
    _git(["checkout", "-q", "-b", "main"], directory)
    _git(["config", "user.email", "t@t"], directory)
    _git(["config", "user.name", "t"], directory)
    (directory / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {name} desc\n"
        "metadata:\n  version: 1\n---\n"
        f"# {name}\n",
        encoding="utf-8",
    )
    _git(["add", "."], directory)
    _git(["commit", "-q", "-m", "v1"], directory)


class GateEmbed:
    model = "fake-v1"

    def __init__(self, dim=4):
        self.dim = dim
        self.block = False
        self.started = threading.Event()
        self.release = threading.Event()
        self._lock = threading.Lock()
        self.calls = 0
        self.items = 0
        self.active = 0
        self.max_active = 0

    def encode(self, text):
        vector = np.zeros(self.dim, dtype=float)
        for index, char in enumerate(text):
            vector[index % self.dim] += ord(char) % 97
        return vector

    def encode_batch(self, texts):
        texts = list(texts)
        with self._lock:
            self.calls += 1
            self.items += len(texts)
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.started.set()
        try:
            if self.block:
                assert self.release.wait(10), "test did not release embedding gate"
            return np.stack([self.encode(text) for text in texts])
        finally:
            with self._lock:
                self.active -= 1


def _write_atom(root: Path, traj_id: str, atom_id: str, *, summary, used_skills):
    tasks = root / traj_id / "tasks"
    tasks.mkdir(parents=True, exist_ok=True)
    (tasks / f"{atom_id}.json").write_text(json.dumps({
        "atom_id": atom_id,
        "traj_id": traj_id,
        "offset_start": 1,
        "offset_end": 2,
        "intent": "i",
        "summary": summary,
        "used_skills": used_skills,
        "tags": [],
    }), encoding="utf-8")


@pytest.fixture
def team_runtime(tmp_path):
    skill_dir = tmp_path / "skill"
    _make_main_skill(skill_dir, "s0")
    with open(skill_dir / ".skill_index.pkl", "wb") as file:
        pickle.dump({
            "skill_names": ["s0"],
            "embeddings": np.eye(1, 4),
            "atom_feats": np.zeros((1, 4)),
            "atom_feat_present": [False],
            "schema_version": 2,
        }, file)
    traj_root = tmp_path / "traj"
    registry = ClientRegistry(tmp_path / "clients.db")
    embed = GateEmbed(dim=4)
    engine = SkillRecommendEngine(
        config={
            "recommend": {"quality_ratio": 0.8, "staging_need": 3},
            "canary": {"total_samples": 3, "min_samples": 3},
        },
        skill_dir=skill_dir,
        traj_root=traj_root,
        embed_client=embed,
        profile_db=tmp_path / "profile.db",
        client_registry=registry,
    )
    # 本组测试只验画像刷新,不测散点物化——关掉散点子系统,避免事件触发派发线程/
    # 进程池的异步副作用（#106 的散点路径由 test_scatter_materialize 专门覆盖）。
    service = ProfileRefreshService(engine, workers=2, queue_size=64,
                                    scatter_materialize=False)
    set_recommend_engine(engine)
    server_api.init_team_context(
        join_token="tok",
        client_registry=registry,
        skill_dir=skill_dir,
        traj_root=traj_root,
        register_dir=lambda _path, _label: None,
        profile_refresh_service=service,
    )
    app = FastAPI()
    app.include_router(server_api.router)
    client = TestClient(app)
    response = client.post(
        "/api/v1/team/register", json={"token": "tok", "user_name": "u1"},
    )
    client_id = response.json()["client_id"]
    atom_root = (
        traj_root / "clients" / registry.dir_name_for(client_id) / "sessions"
    )
    _write_atom(
        atom_root,
        "traj_1",
        "atom_traj_1_0001",
        summary="fix django migration",
        used_skills=["s0"],
    )
    yield engine, embed, service, client, client_id, registry, traj_root
    server_api.clear_team_context(profile_refresh_shutdown_timeout=2)
    set_recommend_engine(None)


def _headers(client_id: str) -> dict[str, str]:
    return {"X-Xskill-Token": "tok", "X-Xskill-Client": client_id}


# 等待后台画像刷新收尾的统一上限。这些断言验证的是「刷新最终会完成」，
# 不是「须在多少秒内完成」——「sync 先返回、刷新在后台」的性质由响应先到
# / 版本号未变等断言单独保障。上限从 2 秒放宽到 10 秒：Windows CI 运行器
# 慢且负载波动大（约为 Linux 的六分之一速度），2 秒是压线值，2026-08-19
# 同日在三个不同分支的矩阵上偶发超时（zero_slots 用例，禁用分发时 sync
# 立即返回，后台刷新几乎没有提前量，窗口最紧）。快路径下等待立即返回，
# 放宽上限不增加正常轮次的耗时。
IDLE_WAIT_SECONDS = 10


def test_sync_returns_before_uncached_embedding_finishes(team_runtime):
    engine, embed, service, client, client_id, _registry, _traj_root = team_runtime
    embed.block = True

    response = client.get("/api/v1/team/sync", headers=_headers(client_id))

    assert response.status_code == 200
    assert set(response.json()) == {
        "slots", "server_time", "server_slots", "take_n",
    }
    assert embed.started.wait(2)
    assert engine.profile_store.load(client_id) is None
    assert service.metrics["running"] == 1
    embed.release.set()
    assert service.wait_idle(timeout=IDLE_WAIT_SECONDS)
    assert engine.profile_store.load(client_id)["feature_tensor"] is not None


def test_zero_slots_skips_preference_reads_but_still_refreshes_profile(
    team_runtime, monkeypatch,
):
    """禁用分发时 sync 不做无效控制面查询，后台画像语义保持不变。"""
    engine, embed, service, client, client_id, _registry, _traj_root = team_runtime
    from xskill.pipeline import registry as pipeline_registry

    preference_calls: list[str] = []
    monkeypatch.setattr(
        pipeline_registry,
        "effective_prefs",
        lambda _user: preference_calls.append("prefs") or {},
    )
    monkeypatch.setattr(
        pipeline_registry,
        "retired_skills",
        lambda: preference_calls.append("retired") or set(),
    )
    # 停止分发改由 live config 现取(热生效),不再是 _ctx 快照
    from xskill.api import app as app_mod
    monkeypatch.setattr(app_mod, "_config", {"team": {"server": {"skill_slots": 0}}})

    response = client.get("/api/v1/team/sync", headers=_headers(client_id))

    assert response.status_code == 200
    assert response.json()["slots"] == []
    assert preference_calls == []
    assert embed.started.wait(2)
    assert service.wait_idle(timeout=IDLE_WAIT_SECONDS)
    assert engine.profile_store.load(client_id) is not None


def test_sync_uses_old_profile_then_refreshes_in_background(team_runtime):
    engine, embed, service, client, client_id, _registry, traj_root = team_runtime
    engine.update_user_interest(
        __import__(
            "xskill.recommend.client_interest", fromlist=["ClientInterest"],
        ).ClientInterest(client_id),
    )
    old_revision = engine.profile_store.get_revision(client_id)["source_revision"]
    _write_atom(
        engine._client_store_root(client_id),
        "traj_2",
        "atom_traj_2_0001",
        summary="tune nginx",
        used_skills=["s0"],
    )
    embed.block = True

    response = client.get("/api/v1/team/sync", headers=_headers(client_id))

    assert response.status_code == 200
    assert embed.started.wait(2)
    assert engine.profile_store.get_revision(client_id)["source_revision"] == old_revision
    embed.release.set()
    assert service.wait_idle(timeout=IDLE_WAIT_SECONDS)
    assert engine.profile_store.get_revision(client_id)["source_revision"] != old_revision


def test_sync_builds_manifest_before_request_and_request_failure_is_best_effort(
    team_runtime, monkeypatch,
):
    _engine, _embed, service, client, client_id, _registry, _traj_root = team_runtime
    events: list[str] = []

    class Response:
        def model_dump(self):
            return {"slots": [], "server_time": 1.0}

    def build_manifest(**_kwargs):
        events.append("manifest")
        return Response()

    def failing_request(_client_id):
        events.append("request")
        raise RuntimeError("queue unavailable")

    monkeypatch.setattr(server_api, "build_manifest", build_manifest)
    monkeypatch.setattr(service, "request", failing_request)

    response = client.get("/api/v1/team/sync", headers=_headers(client_id))

    assert response.status_code == 200
    assert events == ["manifest", "request"]


def test_sync_cold_cache_does_not_scan_atoms_when_refresh_service_missing(
    team_runtime, monkeypatch,
):
    engine, _embed, service, client, client_id, _registry, _traj_root = team_runtime
    assert service.stop(timeout=2)
    server_api._ctx.profile_refresh_service = None

    def forbidden_atom_scan(_client_id):
        raise AssertionError("/sync must not read atoms")

    monkeypatch.setattr(engine, "_user_atoms", forbidden_atom_scan)
    response = client.get("/api/v1/team/sync", headers=_headers(client_id))
    assert response.status_code == 200


def test_repeated_sync_for_same_client_is_coalesced(team_runtime):
    _engine, embed, service, client, client_id, _registry, _traj_root = team_runtime
    embed.block = True
    first = client.get("/api/v1/team/sync", headers=_headers(client_id))
    assert first.status_code == 200
    assert embed.started.wait(2)

    second = client.get("/api/v1/team/sync", headers=_headers(client_id))

    assert second.status_code == 200
    assert embed.calls == 1
    assert service.metrics["coalesced"] == 1
    embed.release.set()
    assert service.wait_idle(timeout=IDLE_WAIT_SECONDS)


def test_thirty_clients_return_while_embedding_is_bounded(tmp_path, monkeypatch):
    # 本用例只验证 /sync 和画像 worker；控制面偏好的 SQLite 迁移
    # 已有独立测试，避免 30 个线程在此共用用户主目录数据库。
    from xskill.pipeline import registry as pipeline_registry
    monkeypatch.setattr(
        pipeline_registry, "effective_prefs",
        lambda _user_key: {"pinned": [], "blocked": set()},
    )
    monkeypatch.setattr(pipeline_registry, "retired_skills", lambda: set())
    skill_dir = tmp_path / "skill"
    _make_main_skill(skill_dir, "s0")
    with open(skill_dir / ".skill_index.pkl", "wb") as file:
        pickle.dump({
            "skill_names": ["s0"],
            "embeddings": np.eye(1, 4),
            "atom_feats": np.zeros((1, 4)),
            "atom_feat_present": [False],
            "schema_version": 2,
        }, file)
    traj_root = tmp_path / "traj"
    registry = ClientRegistry(tmp_path / "clients.db")
    embed = GateEmbed(dim=4)
    embed.block = True
    engine = SkillRecommendEngine(
        config={"recommend": {}, "canary": {"min_samples": 3}},
        skill_dir=skill_dir,
        traj_root=traj_root,
        embed_client=embed,
        profile_db=tmp_path / "profile.db",
        client_registry=registry,
    )
    service = ProfileRefreshService(engine, workers=4, queue_size=64,
                                    scatter_materialize=False)
    set_recommend_engine(engine)
    server_api.init_team_context(
        join_token="tok",
        client_registry=registry,
        skill_dir=skill_dir,
        traj_root=traj_root,
        register_dir=lambda _path, _label: None,
        profile_refresh_service=service,
    )
    app = FastAPI()
    app.include_router(server_api.router)
    client = TestClient(app)
    client_ids = []
    for index in range(30):
        response = client.post(
            "/api/v1/team/register",
            json={"token": "tok", "user_name": f"user-{index}"},
        )
        client_id = response.json()["client_id"]
        client_ids.append(client_id)
        _write_atom(
            engine._client_store_root(client_id),
            f"traj_{index}",
            f"atom_traj_{index}_0001",
            summary=f"summary {index}",
            used_skills=["s0"],
        )

    executor = ThreadPoolExecutor(max_workers=30)
    futures = [
        executor.submit(
            server_api.team_sync,
            x_xskill_token="tok",
            x_xskill_client=client_id,
            x_xskill_version=None,
        )
        for client_id in client_ids
    ]
    try:
        done, not_done = wait(futures, timeout=5)
        assert not not_done, f"{len(not_done)} sync calls did not return"
        responses = [future.result() for future in done]
        assert all(
            set(response) == {"slots", "server_time", "server_slots", "take_n"}
            for response in responses
        )
        assert embed.started.wait(2)
        assert service.metrics["running"] <= 4
        assert embed.max_active <= 4
        assert service.metrics["queued"] + service.metrics["running"] == 30

        embed.release.set()
        assert service.wait_idle(timeout=IDLE_WAIT_SECONDS)
        assert embed.items == 30
        assert service.metrics["embed_items"] == 30
        assert service.metrics["failed"] == 0
        assert all(engine.profile_store.load(client_id) is not None
                   for client_id in client_ids)
    finally:
        embed.release.set()
        for future in futures:
            future.cancel()
        executor.shutdown(wait=True, cancel_futures=True)
        server_api.clear_team_context(profile_refresh_shutdown_timeout=2)
        set_recommend_engine(None)


def test_context_clear_stops_workers_and_removes_references(team_runtime):
    _engine, _embed, service, _client, _client_id, _registry, _traj_root = team_runtime
    assert server_api.clear_team_context(profile_refresh_shutdown_timeout=2)
    assert all(not thread.is_alive() for thread in service._threads)
    assert server_api._ctx.client_registry is None
    assert server_api._ctx.profile_refresh_service is None


def test_stats_exposes_profile_refresh_status(tmp_path, monkeypatch):
    """画像拆为短命子进程后,/stats 的 profile_refresh 读子进程落盘的状态文件。"""
    from xskill.api import app as app_module
    from xskill import config as xconfig
    from xskill.pipeline import registry as pipeline_registry
    from xskill.utils.status_file import PROFILE_STATUS_FILE, write_status_file

    monkeypatch.setattr(app_module, "_config", {
        "team": {"server": {}},
        "dashboard": {"enabled": False},
    })
    monkeypatch.setattr(app_module, "_skill_dir", tmp_path / "skill")
    monkeypatch.setattr(xconfig, "XSKILL_HOME", tmp_path)

    def _empty_summary():
        return {}

    monkeypatch.setattr(pipeline_registry, "usage_summary", _empty_summary)
    monkeypatch.setattr(pipeline_registry, "model_share", _empty_summary)
    write_status_file(
        tmp_path / PROFILE_STATUS_FILE, {"clients": 3, "completed": 2}, ok=True)

    response = TestClient(app_module.create_app()).get("/api/v1/stats")

    assert response.status_code == 200
    body = response.json()["profile_refresh"]
    assert body["ok"] is True
    assert body["stats"] == {"clients": 3, "completed": 2}
