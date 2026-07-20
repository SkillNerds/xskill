#!/usr/bin/env python3.11
"""Release-gating smoke/stress harness for skill_hub 混合检索 (embed.md A5) + 面板工况门.

A real uvicorn xskill server drives a skillhub corpus, distractor files and deep
junk directories; only the OpenAI-compatible LLM/embedding backend is mocked.
Scale defaults are the release acceptance scale; local debugging passes small
``--skills``/``--concurrency`` etc.  The mock backend, latency summary helpers and
process/thread probes are imported from the 300x300 control-plane harness.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import site
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import loadtest_300_control_plane as control_plane_harness


REPO_ROOT = Path(__file__).resolve().parents[1]
ORIGINAL_USER_BASE = site.USER_BASE

# 同一中文词先做冷并发 single-flight 探测；随后 8 个 ASCII 词测 max_embed
# 峰值，再用全部 20 个中英文词做四波重复负载。
PEAK_QUERY_TERMS = (
    "python", "docker", "kubernetes", "postgres",
    "redis", "terraform", "prometheus", "grafana",
)
HOT_QUERY_TERMS = (
    "数据库", "缓存", "网络", "安全", "部署", "监控",
    "机器学习", "搜索", "备份", "日志", "流水线", "告警",
)
ALL_QUERY_TERMS = PEAK_QUERY_TERMS + HOT_QUERY_TERMS
WARM_QUERY_TERM = "自动化"
SNAPSHOT_REFRESH_WAIT_S = 5.1
DEFAULT_PROFILE_REFRESH_INTERVAL_S = 600.0


def _dead_port_base_url() -> str:
    """一个没人监听的本地端口，用于场景 B 制造 embed 连接被拒。"""
    return f"http://127.0.0.1:{control_plane_harness._free_port()}"


def prepare_search_home(run_dir: Path, mock_base_url: str, args: argparse.Namespace) -> dict[str, Any]:
    """建 skillhub 语料 + 干扰文件 + 深垃圾目录 + client，写 config，并预热 corpus 向量。"""
    from xskill.recommend.skillhub import SkillHub
    from xskill.team.server.client_registry import ClientRegistry
    from xskill.utils.llm import create_embed_client

    server_home = run_dir / "server_home"
    xhome = server_home / ".xskill"
    skillhub_dir = xhome / "skillhub_skills"
    empty_skill_dir = xhome / "skill"
    traj_root = xhome / "team_trajectories"
    for path in (skillhub_dir, empty_skill_dir, traj_root):
        path.mkdir(parents=True, exist_ok=True)

    os.environ["HOME"] = str(server_home)
    if str(REPO_ROOT / "src") not in sys.path:
        sys.path.insert(0, str(REPO_ROOT / "src"))

    started_at = time.monotonic()
    descriptions: list[str] = []
    for skill_index in range(args.skills):
        peak_term = PEAK_QUERY_TERMS[skill_index % len(PEAK_QUERY_TERMS)]
        hot_term = HOT_QUERY_TERMS[skill_index % len(HOT_QUERY_TERMS)]
        secondary_hot = HOT_QUERY_TERMS[(skill_index + 3) % len(HOT_QUERY_TERMS)]
        description = (
            f"{peak_term} 与 {hot_term} 集成技能：用于{secondary_hot}和{peak_term}的自动化，"
            "覆盖数据库缓存与网络安全监控的最佳实践。"
        )
        descriptions.append(description)
        skill_dir = skillhub_dir / f"hub-skill-{skill_index:04d}"
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text(
            f"---\nname: hub-skill-{skill_index:04d}\ndescription: {description}\n---\n\n"
            f"# hub-skill-{skill_index:04d}\n\n三方 skill 冒烟语料。\n",
            encoding="utf-8",
        )

    distractor_root = skillhub_dir / "_distractors"
    for distractor_index in range(args.distractors):
        bucket = distractor_root / f"bucket-{distractor_index // 100:03d}"
        bucket.mkdir(parents=True, exist_ok=True)
        (bucket / f"note-{distractor_index:05d}.txt").write_text("distractor", encoding="utf-8")

    deep_dir = skillhub_dir / "_deep"
    for depth in range(args.junk_depth):
        deep_dir = deep_dir / f"level-{depth}"
        deep_dir.mkdir(parents=True, exist_ok=True)
        (deep_dir / f"junk-{depth}.txt").write_text("junk", encoding="utf-8")

    registry = ClientRegistry(xhome / "team_clients.db")
    client_rows: list[dict[str, str]] = []
    for client_index in range(args.clients):
        user_name = f"search-user-{client_index:03d}"
        client_id = registry.register(
            label="search-smoke", hostname=f"search-host-{client_index:03d}", user_name=user_name,
        )
        dashboard_token = registry.ensure_dashboard_token(client_id)
        session_root = traj_root / "clients" / user_name / "sessions"
        control_plane_harness._write_atom(
            session_root,
            traj_id=f"traj_search_client_{client_index:03d}",
            atom_id=f"atom_search_client_{client_index:03d}_0001",
            summary=f"cold summary search client {client_index:03d}",
        )
        client_rows.append({
            "index": str(client_index), "client_id": client_id,
            "user_name": user_name, "dashboard_token": dashboard_token,
        })

    join_token = control_plane_harness.hashlib.sha256(f"{run_dir}-join-token".encode()).hexdigest()[:32]
    (xhome / "team_server.json").write_text(json.dumps({"join_token": join_token}), encoding="utf-8")
    (xhome / "team_server.json").chmod(0o600)
    (xhome / "COLD_START").write_text(
        json.dumps({"trajectory_ids": [], "created_at": time.time()}), encoding="utf-8",
    )

    config = {
        "skill_dir": str(empty_skill_dir),
        "llm": {
            "base_url": mock_base_url, "model": "mock-tool-model", "api_key": "mock-key",
            "max_context": 200000, "request_timeout": 120, "connect_timeout": 5,
            "client_max_retries": 0, "max_retries": 1,
        },
        "embedding": {
            "base_url": mock_base_url, "model": "mock-embed-model", "api_key": "mock-key",
            "dim": 8, "api": "openai",
            "max_embed": args.max_embed_low, "search_timeout_s": args.search_timeout_s,
        },
        "skillhub": {"enabled": True, "dir": str(skillhub_dir)},
        "skill_opt": {"enabled": False},
        "watcher": {"poll_interval": 0.5, "max_concurrent": 8, "cluster_batch_size": 8},
        "server": {
            "thread_pool_tokens": 80, "team_sync_workers": args.team_sync_workers,
            "profile_refresh_workers": 8, "profile_refresh_queue_size": 1024,
            "profile_refresh_shutdown_timeout": 10.0,
            "profile_refresh_interval": DEFAULT_PROFILE_REFRESH_INTERVAL_S,
        },
        "team": {"server": {
            "traj_root": str(traj_root), "skill_slots": 100, "ranked_slots": 80,
            "allow_anonymous_user": True,
        }},
        "recommend": {"quality_ratio": 0.8, "cluster_centers": 5, "last_n_atoms": 5},
        "dashboard": {
            "enabled": True, "public": True, "password": "",
            "admins": [], "admin_password": "",
        },
    }

    embed_client = create_embed_client(config)
    warm_hub = SkillHub.from_config(config, embed_client)
    warm_hub.index()

    return {
        "server_home": str(server_home), "xhome": str(xhome),
        "skillhub_dir": str(skillhub_dir), "traj_root": str(traj_root),
        "join_token": join_token, "clients": client_rows,
        "config": config, "descriptions": len(descriptions),
        "setup_s": time.monotonic() - started_at,
    }


def write_config(
    xhome: Path, config: dict[str, Any], *, max_embed: int,
    embed_base_url: str, profile_refresh_interval: float,
) -> None:
    import yaml
    config["embedding"]["max_embed"] = max_embed
    config["embedding"]["base_url"] = embed_base_url
    config["server"]["profile_refresh_interval"] = profile_refresh_interval
    (xhome / "config.yaml").write_text(
        yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding="utf-8",
    )


def launch_server(
    run_dir: Path, prepared: dict[str, Any], *, label: str, port: int,
    max_embed: int, embed_base_url: str, profile_refresh_interval: float,
) -> dict[str, Any]:
    """写好 config 后真起一个 uvicorn xskill server 子进程，返回进程与日志句柄。"""
    write_config(
        Path(prepared["xhome"]), prepared["config"],
        max_embed=max_embed, embed_base_url=embed_base_url,
        profile_refresh_interval=profile_refresh_interval,
    )
    log_path = run_dir / f"xskill-server-{label}.log"
    log_file = log_path.open("wb")
    env = os.environ.copy()
    env["HOME"] = prepared["server_home"]
    env["PYTHONUSERBASE"] = ORIGINAL_USER_BASE
    env["PYTHONPATH"] = str(REPO_ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONUNBUFFERED"] = "1"
    process = subprocess.Popen(
        [sys.executable, "-m", "xskill.cli", "serve", "--server", "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(REPO_ROOT), env=env, stdout=log_file, stderr=subprocess.STDOUT,
    )
    return {"process": process, "log_path": log_path, "log_file": log_file, "port": port, "label": label}


def terminate_server(server: dict[str, Any]) -> dict[str, Any]:
    """关停 server 子进程并统计降级/堆栈证据。"""
    process = server["process"]
    forced_kill = False
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            forced_kill = True
            process.kill()
            process.wait(timeout=10)
    server["log_file"].close()
    log_text = server["log_path"].read_text(encoding="utf-8", errors="replace")
    return {
        "return_code": process.returncode, "forced_kill": forced_kill, "clean": not forced_kill,
        "traceback_count": log_text.count("Traceback (most recent call last)"),
        "degraded_log_count": log_text.lower().count("bm25") + log_text.lower().count("降级"),
    }


def _client_headers(prepared: dict[str, Any]) -> list[dict[str, str]]:
    return [
        {"X-Xskill-Token": prepared["join_token"], "X-Xskill-Client": row["client_id"]}
        for row in prepared["clients"]
    ]


async def search_once(client, headers: dict[str, str], query: str, *, limit: int = 5) -> dict[str, Any]:
    started_at = time.monotonic()
    try:
        response = await client.get(
            "/api/v1/team/skill_hub/search",
            params={"query": query, "limit": limit}, headers=headers, timeout=10,
        )
        payload = response.json() if response.status_code == 200 else None
        return {
            "query": query, "status": response.status_code,
            "elapsed_s": time.monotonic() - started_at,
            "result_count": len(payload.get("results", [])) if payload else None,
            "error": None if response.status_code == 200 else response.text[:200],
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "query": query, "status": None, "elapsed_s": time.monotonic() - started_at,
            "result_count": None, "error": f"{type(exc).__name__}: {exc}",
        }


async def run_search_wave(client, headers: dict[str, str], queries: list[str]) -> list[dict[str, Any]]:
    return await asyncio.gather(*[search_once(client, headers, query) for query in queries])


async def sample_health(client, samples: list[dict[str, Any]], stop_event: asyncio.Event, *, interval: float = 0.05) -> None:
    """事件循环滞后探针：连续采样 /api/v1/health 的往返耗时。"""
    while not stop_event.is_set():
        started_at = time.monotonic()
        try:
            response = await client.get("/api/v1/health", timeout=5)
            samples.append({"status": response.status_code, "elapsed_s": time.monotonic() - started_at})
        except Exception as exc:  # noqa: BLE001
            samples.append({"status": None, "elapsed_s": time.monotonic() - started_at, "error": str(exc)})
        await asyncio.sleep(interval)


async def warmup_sync_and_wait_idle(client, headers_list: list[dict[str, str]]) -> list[dict[str, Any]]:
    """先串行烧热共享 manifest，再让每个 client /sync，并等画像刷新落空闲。"""
    profile_status_before = await control_plane_harness._profile_metrics(client)
    profile_ended_at_before = control_plane_harness._profile_round_ended_at(
        profile_status_before
    )
    # 首次 build_manifest 会填 SkillRecommendEngine 的 skillhub 索引缓存。若直接
    # 让所有 client 同时走这条冷路径，测到的是初始化竞争而非稳态 sync 工况。
    first = await control_plane_harness._sync_one(client, headers_list[0], 0)
    if first["status"] != 200:
        raise RuntimeError(f"manifest warmup sync failed: {first}")
    results = await asyncio.gather(*[
        control_plane_harness._sync_one(client, headers, index)
        for index, headers in enumerate(headers_list)
    ])
    failures = [item for item in results if item["status"] != 200]
    if failures:
        raise RuntimeError(f"client warmup sync failed: {failures[:5]}")
    await control_plane_harness._wait_profile_idle(
        client, after_ended_at=profile_ended_at_before, timeout=120,
    )
    return [first, *results]


def _search_stats(search_results: list[dict[str, Any]]) -> dict[str, Any]:
    latencies = [float(item["elapsed_s"]) for item in search_results]
    failures = [item for item in search_results if item["status"] != 200]
    return {
        "count": len(search_results), "failures": failures[:20], "failure_count": len(failures),
        "latency": control_plane_harness._latency_summary(latencies),
        "max_elapsed_s": max(latencies) if latencies else None,
    }


def _health_stats(samples: list[dict[str, Any]]) -> dict[str, Any]:
    latencies = [float(sample["elapsed_s"]) for sample in samples]
    non_200 = [sample for sample in samples if sample["status"] != 200]
    return {
        "count": len(samples), "non_200_count": len(non_200),
        "p99_s": control_plane_harness._percentile(latencies, 0.99),
        "max_s": max(latencies) if latencies else None,
    }


def _sync_stats(results: list[dict[str, Any]]) -> dict[str, Any]:
    latencies = [float(item["elapsed_s"]) for item in results]
    failures = [item for item in results if item["status"] != 200]
    return {
        "count": len(results), "failure_count": len(failures), "failures": failures[:20],
        "latency": control_plane_harness._latency_summary(latencies),
    }


def _panel_stats(results: list[dict[str, Any]]) -> dict[str, Any]:
    failures = [item for item in results if item.get("status") != 200]
    return {
        "count": len(results), "failure_count": len(failures), "failures": failures[:20],
        "paths": sorted({str(item.get("path")) for item in results}),
    }


async def _run_scenario_a_round(
    client, prepared: dict[str, Any], state, server_pid: int,
    *, max_embed: int, args: argparse.Namespace,
) -> dict[str, Any]:
    """常态混合负载单轮：峰值探测 burst + 中文吞吐波 + 并发 /sync + 面板轮询 + health 采样。"""
    headers_list = _client_headers(prepared)
    await warmup_sync_and_wait_idle(client, headers_list)
    warm_search = await search_once(client, headers_list[0], WARM_QUERY_TERM)
    if warm_search["status"] != 200:
        raise RuntimeError(f"search cache warmup failed: {warm_search}")
    with state.lock:
        state.embed_max_active = 0
    window_embed_start = state.snapshot()["embedding"]["request_count"]

    # 冷 single-flight 与 max_embed 峰值是配置诊断，不计入场景 A 的缓存热 SLA。
    duplicate_query = HOT_QUERY_TERMS[0]
    duplicate_embed_start = state.snapshot()["embedding"]["request_count"]
    duplicate_results = await run_search_wave(
        client, headers_list[0], [duplicate_query] * args.concurrency,
    )
    duplicate_cold_embed_calls = (
        state.snapshot()["embedding"]["request_count"] - duplicate_embed_start
    )
    # duplicate burst 已完全收敛，清零峰值后再独立验证 max_embed=1/4。
    with state.lock:
        state.embed_max_active = 0
    burst_results = await run_search_wave(client, headers_list[0], list(PEAK_QUERY_TERMS))
    observed_peak_embed = state.snapshot()["embedding"]["max_active"]

    # 场景 A 的 SLA 是缓存热负载。峰值探测在 max_embed=1/4 时会让部分并发查询
    # 立即走 BM25，因此这里逐个预热全部查询词，确保计时波次不再夹带 200ms embed。
    warm_results: list[dict[str, Any]] = []
    for query_term in ALL_QUERY_TERMS:
        warm_result = await search_once(client, headers_list[0], query_term)
        warm_results.append(warm_result)
        if warm_result["status"] != 200:
            raise RuntimeError(f"steady search cache warmup failed: {warm_result}")
    # 预热本身可能耗时接近扫描快照的 5s TTL。先明确跨过 TTL，再在计时窗外
    # 完成一次刷新，确保随后测量的确是新鲜且缓存热的稳态负载。
    await asyncio.sleep(SNAPSHOT_REFRESH_WAIT_S)
    snapshot_refresh = await search_once(client, headers_list[0], WARM_QUERY_TERM)
    if snapshot_refresh["status"] != 200:
        raise RuntimeError(f"steady snapshot refresh failed: {snapshot_refresh}")
    post_refresh_warm_results: list[dict[str, Any]] = []
    for query_term in ALL_QUERY_TERMS:
        warm_result = await search_once(client, headers_list[0], query_term)
        post_refresh_warm_results.append(warm_result)
        if warm_result["status"] != 200:
            raise RuntimeError(f"post-refresh search warmup failed: {warm_result}")

    health_samples: list[dict[str, Any]] = []
    sync_results: list[dict[str, Any]] = []
    panel_results: list[dict[str, Any]] = []
    stop_background = asyncio.Event()
    health_task = asyncio.create_task(sample_health(client, health_samples, stop_background))

    async def sync_wave() -> None:
        sync_results.extend(await asyncio.gather(*[
            control_plane_harness._sync_one(client, headers, index)
            for index, headers in enumerate(headers_list)
        ]))

    async def panel_poll() -> None:
        while not stop_background.is_set():
            panel_results.extend(await asyncio.gather(
                control_plane_harness._probe(
                    client, "GET", "/api/v1/dashboard/tags", timeout=5,
                ),
                control_plane_harness._probe(
                    client, "GET", "/api/v1/dashboard/my/manifest", timeout=5,
                ),
            ))
            await asyncio.sleep(0.05)

    sync_task = asyncio.create_task(sync_wave())
    panel_task = asyncio.create_task(panel_poll())

    pre_wave_embed = state.snapshot()["embedding"]["request_count"]
    baseline_threads = control_plane_harness._process_threads(server_pid)
    wave_results: list[dict[str, Any]] = []
    peak_threads = baseline_threads
    for _wave_index in range(args.waves):
        queries = [ALL_QUERY_TERMS[position % len(ALL_QUERY_TERMS)] for position in range(args.concurrency)]
        wave_results.extend(await run_search_wave(client, headers_list[0], queries))
        peak_threads = max(peak_threads, control_plane_harness._process_threads(server_pid))
    post_wave_embed = state.snapshot()["embedding"]["request_count"]

    stop_background.set()
    for task in (health_task, panel_task):
        try:
            await asyncio.wait_for(task, timeout=15)
        except Exception:  # noqa: BLE001
            task.cancel()
    await asyncio.wait_for(sync_task, timeout=30)

    return {
        "max_embed": max_embed,
        "search": _search_stats(wave_results),
        "diagnostic_duplicate": _search_stats(duplicate_results),
        "diagnostic_peak": _search_stats(burst_results),
        "diagnostic_warmup": _search_stats(warm_results),
        "diagnostic_snapshot_refresh": snapshot_refresh,
        "diagnostic_post_refresh_warmup": _search_stats(post_refresh_warm_results),
        "health": _health_stats(health_samples),
        "sync": _sync_stats(sync_results),
        "panel": _panel_stats(panel_results),
        "duplicate_cold_embed_calls": duplicate_cold_embed_calls,
        "observed_peak_embed": observed_peak_embed,
        "window_embed_calls": post_wave_embed - window_embed_start,
        "throughput_wave_embed_calls": post_wave_embed - pre_wave_embed,
        "distinct_query_terms": len(ALL_QUERY_TERMS),
        "baseline_threads": baseline_threads, "peak_threads": peak_threads,
    }


async def run_all_scenarios(
    args: argparse.Namespace, run_dir: Path, result_path: Path, state, prepared: dict[str, Any], result: dict[str, Any],
) -> None:
    import httpx

    result["scenarios"] = {}

    def checkpoint() -> None:
        control_plane_harness._write_result_snapshot(result, result_path)

    async def open_ready_server(server: dict[str, Any]) -> Any:
        base_url = f"http://127.0.0.1:{server['port']}"
        limits = httpx.Limits(max_connections=max(args.concurrency + args.clients + 50, 200), max_keepalive_connections=100)
        client = httpx.AsyncClient(base_url=base_url, limits=limits)
        await control_plane_harness._wait_for(
            lambda: server["process"].poll() is not None or control_plane_harness._health_sync(base_url),
            timeout=60, description=f"{server['label']} server startup", interval=0.2,
        )
        if server["process"].poll() is not None:
            raise RuntimeError(f"{server['label']} server exited early with {server['process'].returncode}")
        dashboard_user = prepared["clients"][0]
        login = await client.post(
            "/api/v1/dashboard/login",
            json={
                "user_name": dashboard_user["user_name"],
                "secret": dashboard_user["dashboard_token"],
            },
            timeout=5,
        )
        if login.status_code != 200:
            await client.aclose()
            raise RuntimeError(
                f"{server['label']} dashboard login failed: "
                f"status={login.status_code} body={login.text[:200]}"
            )
        return client

    # 场景 A：两轮 max_embed（1 再 4），观测峰值并发都要贴合配置
    for round_max_embed in (args.max_embed_low, args.max_embed_high):
        state.embed_release.set()
        state.embed_delay = args.embed_delay_s
        server = launch_server(
            run_dir, prepared, label=f"a-embed-{round_max_embed}",
            port=control_plane_harness._free_port(), max_embed=round_max_embed,
            embed_base_url=state_mock_base_url(prepared),
            profile_refresh_interval=(
                control_plane_harness.PROFILE_REFRESH_INTERVAL_S
            ),
        )
        client = await open_ready_server(server)
        try:
            round_result = await _run_scenario_a_round(
                client, prepared, state, server["process"].pid, max_embed=round_max_embed, args=args,
            )
        finally:
            await client.aclose()
            round_result_shutdown = terminate_server(server)
        round_result["shutdown"] = round_result_shutdown
        result["scenarios"][f"scenario_a_max_embed_{round_max_embed}"] = round_result
        checkpoint()

    # 场景 B：embed 宕机（死端口拒连）→ 全 BM25 降级
    state.embed_release.set()
    server = launch_server(
        run_dir, prepared, label="b-embed-down", port=control_plane_harness._free_port(),
        max_embed=args.max_embed_high, embed_base_url=_dead_port_base_url(),
        profile_refresh_interval=DEFAULT_PROFILE_REFRESH_INTERVAL_S,
    )
    client = await open_ready_server(server)
    try:
        headers_list = _client_headers(prepared)
        threads_before_warmup = control_plane_harness._process_threads(server["process"].pid)
        # 先串行填共享 manifest 缓存，避免后面的线程探测混入首次建索引。
        manifest_warmup = await control_plane_harness._probe(
            client, "GET", "/api/v1/dashboard/my/manifest", timeout=10,
        )
        if manifest_warmup["status"] != 200:
            raise RuntimeError(f"scenario B manifest warmup failed: {manifest_warmup}")
        # 用比正式波更宽的拒连请求把 anyio worker 和固定大小 query executor
        # 烧热；后续以这个稳态为基线，线程数增加一条就拦截。
        warmup_width = max(args.concurrency * 2, args.concurrency + 16)
        warmup_queries = [
            ALL_QUERY_TERMS[position % len(ALL_QUERY_TERMS)]
            for position in range(warmup_width)
        ]
        thread_warmup_results: list[dict[str, Any]] = []
        warmup_thread_counts: list[int] = []
        for _warmup_wave in range(3):
            thread_warmup_results.extend(
                await run_search_wave(client, headers_list[0], warmup_queries)
            )
            warmup_thread_counts.append(
                control_plane_harness._process_threads(server["process"].pid)
            )
        # watcher 和控制面固定 worker 都可能延迟启动；跨过四个 0.5s poll
        # 周期后再取稳态基线，避免把正常初始化误报成持续增长。
        await asyncio.sleep(2.1)
        steady_queries = [
            ALL_QUERY_TERMS[position % len(ALL_QUERY_TERMS)]
            for position in range(args.concurrency)
        ]
        thread_stabilization_results: list[dict[str, Any]] = []
        for _stabilization_wave in range(4):
            thread_stabilization_results.extend(
                await run_search_wave(client, headers_list[0], steady_queries)
            )
            warmup_thread_counts.append(
                control_plane_harness._process_threads(server["process"].pid)
            )
        await asyncio.sleep(0.1)
        await asyncio.sleep(SNAPSHOT_REFRESH_WAIT_S)
        snapshot_refresh = await search_once(
            client, headers_list[0], WARM_QUERY_TERM,
        )
        if snapshot_refresh["status"] != 200:
            raise RuntimeError(f"scenario B snapshot refresh failed: {snapshot_refresh}")
        post_refresh_warm_results: list[dict[str, Any]] = []
        for query_term in ALL_QUERY_TERMS:
            warm_result = await search_once(client, headers_list[0], query_term)
            post_refresh_warm_results.append(warm_result)
            if warm_result["status"] != 200:
                raise RuntimeError(
                    f"scenario B post-refresh warmup failed: {warm_result}"
                )
        threads_after_warmup = control_plane_harness._process_threads(server["process"].pid)
        warmup_thread_counts.append(threads_after_warmup)
        baseline_threads = max(warmup_thread_counts)
        down_results: list[dict[str, Any]] = []
        peak_threads = baseline_threads
        for _wave_index in range(max(args.waves, 4)):
            queries = [ALL_QUERY_TERMS[position % len(ALL_QUERY_TERMS)] for position in range(args.concurrency)]
            down_results.extend(await run_search_wave(client, headers_list[0], queries))
            peak_threads = max(peak_threads, control_plane_harness._process_threads(server["process"].pid))
    finally:
        await client.aclose()
        shutdown = terminate_server(server)
    result["scenarios"]["scenario_b_embed_down"] = {
        "search": _search_stats(down_results),
        "thread_warmup": _search_stats(thread_warmup_results),
        "thread_stabilization": _search_stats(thread_stabilization_results),
        "diagnostic_snapshot_refresh": snapshot_refresh,
        "diagnostic_post_refresh_warmup": _search_stats(post_refresh_warm_results),
        "threads_before_warmup": threads_before_warmup,
        "threads_after_warmup": threads_after_warmup,
        "warmup_thread_counts": warmup_thread_counts,
        "baseline_threads": baseline_threads, "peak_threads": peak_threads,
        "shutdown": shutdown,
    }
    checkpoint()

    # 场景 C：冷启动，首查冷扫描 <3s，次查命中 <200ms
    state.embed_release.set()
    state.embed_delay = args.embed_delay_s
    server = launch_server(
        run_dir, prepared, label="c-cold", port=control_plane_harness._free_port(),
        max_embed=args.max_embed_high, embed_base_url=state_mock_base_url(prepared),
        profile_refresh_interval=DEFAULT_PROFILE_REFRESH_INTERVAL_S,
    )
    client = await open_ready_server(server)
    try:
        headers_list = _client_headers(prepared)
        cold_query = HOT_QUERY_TERMS[0]
        first_search = await search_once(client, headers_list[0], cold_query)
        second_search = await search_once(client, headers_list[0], cold_query)
    finally:
        await client.aclose()
        shutdown = terminate_server(server)
    result["scenarios"]["scenario_c_cold_start"] = {
        "first_search": first_search, "second_search": second_search, "shutdown": shutdown,
    }
    checkpoint()

    # 面板工况门：真数据 + 并发 /sync + 面板重端点轮询，全程 health 采事件循环滞后
    state.embed_release.set()
    state.embed_delay = args.embed_delay_s
    server = launch_server(
        run_dir, prepared, label="panel", port=control_plane_harness._free_port(),
        max_embed=args.max_embed_high, embed_base_url=state_mock_base_url(prepared),
        profile_refresh_interval=(
            control_plane_harness.PROFILE_REFRESH_INTERVAL_S
        ),
    )
    client = await open_ready_server(server)
    try:
        headers_list = _client_headers(prepared)
        await warmup_sync_and_wait_idle(client, headers_list)
        scatter_user = prepared["clients"][0]["user_name"]
        health_samples: list[dict[str, Any]] = []
        sync_results: list[dict[str, Any]] = []
        stop_background = asyncio.Event()
        health_task = asyncio.create_task(sample_health(client, health_samples, stop_background))
        sync_task = asyncio.ensure_future(asyncio.gather(*[
            control_plane_harness._sync_one(client, headers, index)
            for index, headers in enumerate(headers_list)
        ]))
        panel_probe_results: list[dict[str, Any]] = []
        panel_deadline = time.monotonic() + args.panel_duration_s
        while time.monotonic() < panel_deadline:
            panel_probe_results.extend(await asyncio.gather(
                control_plane_harness._probe(
                    client, "GET", "/api/v1/dashboard/tags", timeout=5,
                ),
                control_plane_harness._probe(
                    client, "GET", "/api/v1/dashboard/my/manifest", timeout=5,
                ),
                control_plane_harness._probe(
                    client, "GET", "/api/v1/dashboard/overview", timeout=5,
                ),
                control_plane_harness._probe(
                    client, "GET", "/api/v1/dashboard/skills", timeout=5,
                ),
                control_plane_harness._probe(
                    client, "GET",
                    f"/api/v1/dashboard/user/{scatter_user}/scatter",
                    timeout=5,
                ),
            ))
            await asyncio.sleep(0.05)
        sync_results.extend(await asyncio.wait_for(sync_task, timeout=30))
        stop_background.set()
        try:
            await asyncio.wait_for(health_task, timeout=15)
        except Exception:  # noqa: BLE001
            health_task.cancel()
    finally:
        await client.aclose()
        shutdown = terminate_server(server)
    panel_paths = {
        "/api/v1/dashboard/tags", "/api/v1/dashboard/my/manifest",
        "/api/v1/dashboard/overview", "/api/v1/dashboard/skills",
    }
    result["scenarios"]["panel_gate"] = {
        "health": _health_stats(health_samples),
        "sync": _sync_stats(sync_results),
        "panel": _panel_stats(panel_probe_results),
        "panel_probe_count": len(panel_probe_results),
        "panel_core_failures": [
            probe for probe in panel_probe_results
            if probe.get("path") in panel_paths and probe.get("status") != 200
        ][:20],
        "panel_scatter_no_response": [
            probe for probe in panel_probe_results
            if probe.get("path", "").endswith("/scatter") and probe.get("status") is None
        ][:20],
        "shutdown": shutdown,
    }
    checkpoint()


def state_mock_base_url(prepared: dict[str, Any]) -> str:
    return prepared["config"]["llm"]["base_url"]


def validate_result(result: dict[str, Any], args: argparse.Namespace) -> list[str]:
    """收集全部未达标断言（一次跑完看全，不在第一条停）。"""
    failures: list[str] = []
    scenarios = result.get("scenarios", {})

    for round_max_embed in (args.max_embed_low, args.max_embed_high):
        name = f"scenario_a_max_embed_{round_max_embed}"
        wave = scenarios.get(name)
        if not wave:
            failures.append(f"{name}: missing")
            continue
        search = wave["search"]
        if search["failure_count"] != 0:
            failures.append(f"{name}: search failures={search['failure_count']}")
        p95 = search["latency"]["p95_s"]
        p99 = search["latency"]["p99_s"]
        if p95 is None or float(p95) >= 0.5:
            failures.append(f"{name}: search p95={p95}")
        if p99 is None or float(p99) >= 1.0:
            failures.append(f"{name}: search p99={p99}")
        if search["max_elapsed_s"] is None or float(search["max_elapsed_s"]) >= 2.0:
            failures.append(f"{name}: search max_elapsed={search['max_elapsed_s']} (rglob 上界)")
        health = wave["health"]
        if health["p99_s"] is None or float(health["p99_s"]) >= 0.3:
            failures.append(f"{name}: health p99={health['p99_s']}")
        if health["max_s"] is None or float(health["max_s"]) >= 1.0:
            failures.append(f"{name}: health max={health['max_s']}")
        if int(health["non_200_count"]) != 0:
            failures.append(f"{name}: health non-200={health['non_200_count']}")
        sync = wave["sync"]
        if sync["count"] < args.clients or sync["failure_count"] != 0:
            failures.append(
                f"{name}: concurrent sync count={sync['count']} failures={sync['failure_count']}"
            )
        panel = wave["panel"]
        required_panel_paths = {
            "/api/v1/dashboard/tags", "/api/v1/dashboard/my/manifest",
        }
        if panel["failure_count"] != 0 or not required_panel_paths.issubset(panel["paths"]):
            failures.append(f"{name}: panel probes={panel}")
        if int(wave["window_embed_calls"]) > int(wave["distinct_query_terms"]):
            failures.append(f"{name}: window embeds={wave['window_embed_calls']} > distinct terms")
        if int(wave["throughput_wave_embed_calls"]) != 0:
            failures.append(
                f"{name}: cache-hot wave embeds={wave['throughput_wave_embed_calls']} != 0"
            )
        if int(wave["duplicate_cold_embed_calls"]) != 1:
            failures.append(
                f"{name}: same-query cold burst embeds={wave['duplicate_cold_embed_calls']} != 1"
            )
        observed_peak = int(wave["observed_peak_embed"])
        if observed_peak > round_max_embed:
            failures.append(f"{name}: peak embed {observed_peak} > max_embed {round_max_embed}")
        if observed_peak != round_max_embed:
            failures.append(f"{name}: peak embed {observed_peak} != configured {round_max_embed}")
        if not wave["shutdown"]["clean"]:
            failures.append(f"{name}: server shutdown not clean")
        if int(wave["shutdown"]["traceback_count"]) != 0:
            failures.append(f"{name}: server traceback_count={wave['shutdown']['traceback_count']}")

    scenario_b = scenarios.get("scenario_b_embed_down")
    if not scenario_b:
        failures.append("scenario_b_embed_down: missing")
    else:
        search = scenario_b["search"]
        if scenario_b["thread_warmup"]["failure_count"] != 0:
            failures.append(
                f"scenario_b: thread warmup failures="
                f"{scenario_b['thread_warmup']['failure_count']}"
            )
        if scenario_b["thread_stabilization"]["failure_count"] != 0:
            failures.append(
                f"scenario_b: thread stabilization failures="
                f"{scenario_b['thread_stabilization']['failure_count']}"
            )
        if search["failure_count"] != 0:
            failures.append(f"scenario_b: search failures={search['failure_count']}")
        p95 = search["latency"]["p95_s"]
        # 这里是 50 并发、embed 完全不可用时的端到端 HTTP 尾延迟；缓存
        # 命中的串行诊断仍由 scenario C 的 200ms 门槛约束。降级并发门槛
        # 留 300ms，避免把事件循环与网络调度抖动误判成 BM25 计算回归。
        if p95 is None or float(p95) >= 0.3:
            failures.append(f"scenario_b: BM25 p95={p95}")
        thread_growth = int(scenario_b["peak_threads"]) - int(scenario_b["baseline_threads"])
        if thread_growth > 0:
            failures.append(f"scenario_b: thread growth={thread_growth}")
        if int(scenario_b["shutdown"]["traceback_count"]) != 0:
            failures.append(f"scenario_b: server traceback_count={scenario_b['shutdown']['traceback_count']}")
        if int(scenario_b["shutdown"]["degraded_log_count"]) < 1:
            failures.append("scenario_b: server log missing BM25 degradation record")

    scenario_c = scenarios.get("scenario_c_cold_start")
    if not scenario_c:
        failures.append("scenario_c_cold_start: missing")
    else:
        first_search = scenario_c["first_search"]
        second_search = scenario_c["second_search"]
        if first_search["status"] != 200:
            failures.append(f"scenario_c: first search status={first_search['status']}")
        elif float(first_search["elapsed_s"]) >= 3.0:
            failures.append(f"scenario_c: cold first search={first_search['elapsed_s']}")
        if second_search["status"] != 200:
            failures.append(f"scenario_c: second search status={second_search['status']}")
        elif float(second_search["elapsed_s"]) >= 0.2:
            failures.append(f"scenario_c: warm second search={second_search['elapsed_s']}")

    panel = scenarios.get("panel_gate")
    if not panel:
        failures.append("panel_gate: missing")
    else:
        health = panel["health"]
        if health["count"] < 1:
            failures.append("panel_gate: no health samples")
        if int(health["non_200_count"]) != 0:
            failures.append(f"panel_gate: health non-200={health['non_200_count']}")
        if health["p99_s"] is None or float(health["p99_s"]) >= 0.5:
            failures.append(f"panel_gate: health p99={health['p99_s']}")
        if health["max_s"] is None or float(health["max_s"]) >= 2.0:
            failures.append(f"panel_gate: health max={health['max_s']} (事故形态 5s)")
        if panel["panel_core_failures"]:
            failures.append(f"panel_gate: core panel endpoint failures={panel['panel_core_failures']}")
        if panel["panel_scatter_no_response"]:
            failures.append(f"panel_gate: scatter connection failures={panel['panel_scatter_no_response']}")
        required_panel_paths = {
            "/api/v1/dashboard/tags", "/api/v1/dashboard/my/manifest",
        }
        if (
            panel["panel"]["failure_count"] != 0
            or not required_panel_paths.issubset(panel["panel"]["paths"])
        ):
            failures.append(f"panel_gate: panel probes={panel['panel']}")
        sync = panel["sync"]
        if sync["count"] < args.clients or sync["failure_count"] != 0:
            failures.append(
                f"panel_gate: sync count={sync['count']} failures={sync['failure_count']}"
            )
        if int(panel["shutdown"]["traceback_count"]) != 0:
            failures.append(f"panel_gate: server traceback_count={panel['shutdown']['traceback_count']}")
    return failures


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--skills", type=int, default=500)
    parser.add_argument("--clients", type=int, default=30)
    parser.add_argument("--concurrency", type=int, default=50)
    parser.add_argument("--waves", type=int, default=4)
    parser.add_argument("--distractors", type=int, default=5000)
    parser.add_argument("--junk-depth", type=int, default=8)
    parser.add_argument("--max-embed-low", type=int, default=1)
    parser.add_argument("--max-embed-high", type=int, default=4)
    parser.add_argument("--search-timeout-s", type=float, default=3.0)
    parser.add_argument("--embed-delay-s", type=float, default=0.2)
    parser.add_argument("--panel-duration-s", type=float, default=20.0)
    # GitHub hosted runners have few CPU cores.  Keep manifest calculation
    # serialized in this mixed-load gate so 30 concurrent /sync requests test
    # queue isolation instead of letting CPU-bound Python workers starve search.
    parser.add_argument("--team-sync-workers", type=int, default=1)
    parser.add_argument("--artifact-root", type=Path, default=Path("/home/admin/xskill-loadtest-results"))
    args = parser.parse_args()
    if (args.skills < 1 or args.clients < 1 or args.concurrency < 1
            or args.team_sync_workers < 1):
        parser.error("--skills/--clients/--concurrency/--team-sync-workers must be >= 1")
    if args.max_embed_high <= args.max_embed_low:
        parser.error("--max-embed-high must exceed --max-embed-low")
    return args


def main() -> int:
    args = parse_args()
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    run_dir = args.artifact_root / f"run-{timestamp}-{os.getpid()}-s{args.skills}"
    run_dir.mkdir(parents=True, exist_ok=False)
    result_path = run_dir / "result.json"
    state = control_plane_harness.MockState(llm_delay=0.0, embed_delay=args.embed_delay_s)
    state.llm_release.set()
    state.embed_release.set()
    mock = control_plane_harness.MockServer(state)
    started_at = time.monotonic()
    result: dict[str, Any] = {
        "run_dir": str(run_dir), "result_path": str(result_path), "repo_root": str(REPO_ROOT),
        "config": {
            "skills": args.skills, "clients": args.clients, "concurrency": args.concurrency,
            "waves": args.waves, "distractors": args.distractors, "junk_depth": args.junk_depth,
            "max_embed_low": args.max_embed_low, "max_embed_high": args.max_embed_high,
            "search_timeout_s": args.search_timeout_s, "embed_delay_s": args.embed_delay_s,
            "team_sync_workers": args.team_sync_workers,
            "profile_refresh_required_interval_s":
                control_plane_harness.PROFILE_REFRESH_INTERVAL_S,
            "profile_refresh_default_interval_s":
                DEFAULT_PROFILE_REFRESH_INTERVAL_S,
        },
        "scenarios": {},
    }
    control_plane_harness._write_result_snapshot(result, result_path)
    return_code = 1
    try:
        mock.start()
        prepared = prepare_search_home(run_dir, mock.base_url, args)
        result["setup"] = {"setup_s": prepared["setup_s"], "descriptions": prepared["descriptions"]}
        control_plane_harness._write_result_snapshot(result, result_path)
        asyncio.run(run_all_scenarios(args, run_dir, result_path, state, prepared, result))
    except Exception as exc:  # noqa: BLE001
        result["fatal_error"] = f"{type(exc).__name__}: {exc}"
    finally:
        state.llm_release.set()
        state.embed_release.set()
        result["mock"] = state.snapshot()
        result["mock_shutdown_clean"] = mock.stop()
        result["total_elapsed_s"] = time.monotonic() - started_at
        try:
            result["validation_failures"] = validate_result(result, args)
        except Exception as exc:  # noqa: BLE001
            result["validation_failures"] = [f"validation error: {type(exc).__name__}: {exc}"]
        if result.get("fatal_error"):
            result["validation_failures"].insert(0, result["fatal_error"])
        if not result["mock_shutdown_clean"]:
            result["validation_failures"].append("mock backend did not shut down cleanly")
        result["success"] = not result["validation_failures"]
        return_code = 0 if result["success"] else 1
        control_plane_harness._write_result_snapshot(result, result_path)
        print(json.dumps({
            "success": result["success"], "run_dir": str(run_dir), "result": str(result_path),
            "fatal_error": result.get("fatal_error"), "validation_failures": result["validation_failures"],
            "elapsed_s": result["total_elapsed_s"],
        }, ensure_ascii=False), flush=True)
    return return_code


if __name__ == "__main__":
    raise SystemExit(main())
