"""内部子进程 worker(**非用户 CLI**)。

agent-worker 是常驻子进程；``recommend-heavy``（含画像刷新 + Milvus 对账 +
脏用户推荐预计算）是定时短命子进程。web 进程的
``IntervalSubprocessScheduler`` 用
``[sys.executable, "-m", "xskill._workers", <kind>]`` 启动它们，重计算与 web
事件循环保持 GIL 隔离。

这些是**内部管道**,刻意不注册进 ``xskill`` 用户 CLI(``cli.build_parser``)——用户
``xskill --help`` 看不到它们。调度器直接调本模块的 SDK 函数，或经
``python -m xskill._workers`` 入口。
"""
from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

logger = logging.getLogger("xskill._workers")


def _install_stop_signal_handlers(stop_event):
    """在 worker 主线程把 TERM/INT 转成可等待事件；返回原 handler。"""
    import signal
    import threading

    if threading.current_thread() is not threading.main_thread():
        return {}
    previous = {}

    def request_stop(_signum, _frame):
        stop_event.set()

    for signum in (signal.SIGTERM, signal.SIGINT):
        previous[signum] = signal.getsignal(signum)
        signal.signal(signum, request_stop)
    return previous


def _restore_signal_handlers(previous) -> None:
    """恢复 ``_install_stop_signal_handlers`` 保存的 handler。"""
    import signal

    for signum, handler in previous.items():
        signal.signal(signum, handler)


def run_agent_worker_forever(
    *,
    server: bool = False,
    home: str | None = None,
    stop_event=None,
    status_interval: float = 5.0,
) -> int:
    """构造四池 agent worker 并常驻运行，直到收到 TERM/INT。

    ``DirectoryWatcher.start()`` 的扫描线程每个 poll 都继续提交和收割
    任务，Future 跨轮保留；LLM 长尾任务不会阻止后续扫描。
    ``stop_event`` 仅供测试和嵌入调用注入；生产由信号 handler 设置内部事件。
    """
    import threading

    from xskill.config import (
        XSKILL_HOME,
        get_registry_db_path,
        get_skill_dir,
        kernel_config,
        load_config,
    )
    from xskill.pipeline.watcher_factory import (
        build_watcher,
        ingest_detected_ecosystems_once,
    )
    from xskill.utils.status_file import (
        AGENT_WORKER_STATUS_FILE,
        WATCHER_STATUS_FILE,
        write_status_file,
    )

    if status_interval <= 0:
        raise ValueError("status_interval 必须 > 0")

    config = load_config()
    home_root = Path(home).expanduser().resolve() if home else Path.home()
    status_path = XSKILL_HOME / WATCHER_STATUS_FILE
    worker_status_path = XSKILL_HOME / AGENT_WORKER_STATUS_FILE
    event = stop_event if stop_event is not None else threading.Event()
    previous_handlers = _install_stop_signal_handlers(event)
    watcher = None
    ok = False
    error = None
    try:
        skill_dir = get_skill_dir(
            config,
            xskill_home=XSKILL_HOME,
        ).expanduser().resolve()
        registry_db_path = get_registry_db_path(
            xskill_home=XSKILL_HOME,
        ).expanduser().resolve()
        install_history_path = (
            XSKILL_HOME / "install_history.jsonl"
        ).expanduser().resolve()

        on_poll_hook = None
        if not server:
            def ingest_on_poll() -> None:
                ingest_detected_ecosystems_once(
                    config,
                    home_root,
                    skill_dir,
                    registry_db_path=registry_db_path,
                    install_history_path=install_history_path,
                    excluded_ecosystems={"claude_code"},
                )

            on_poll_hook = ingest_on_poll

        selected = kernel_config(config, xskill_home=XSKILL_HOME)
        watcher = build_watcher(
            config,
            xskill_home=XSKILL_HOME,
            config_path=XSKILL_HOME / "config.yaml",
            db_path=registry_db_path,
            skill_dir=skill_dir,
            home_root=home_root,
            server_mode=server,
            on_poll_hook=on_poll_hook,
            native_distill=(selected["active"] == "native"),
        )
        watcher.start()
        write_status_file(status_path, watcher.stats, ok=True)
        write_status_file(
            worker_status_path,
            getattr(watcher, "agent_worker_status", watcher.stats),
            ok=True,
        )

        while not event.wait(status_interval):
            if not watcher.is_running:
                raise RuntimeError("watcher thread exited unexpectedly")
            write_status_file(status_path, watcher.stats, ok=True)
            write_status_file(
                worker_status_path,
                getattr(watcher, "agent_worker_status", watcher.stats),
                ok=True,
            )

        ok = True
        return 0
    except Exception as exc:  # noqa: BLE001 — 常驻 worker 顶层边界
        error = str(exc)
        logger.exception("persistent agent worker failed")
        return 1
    finally:
        if watcher is not None:
            from xskill.utils.shutdown import request_shutdown

            request_shutdown()
            watcher.stop()
        write_status_file(
            status_path,
            watcher.stats if watcher is not None else {},
            ok=ok,
            error=error,
        )
        write_status_file(
            worker_status_path,
            (
                getattr(watcher, "agent_worker_status", watcher.stats)
                if watcher is not None
                else {}
            ),
            ok=ok,
            error=error,
        )
        _restore_signal_handlers(previous_handlers)


def _trajectory_snapshot(reader) -> dict[str, tuple[int, int, tuple[str, ...]]]:
    """Return feed fingerprints for trajectories whose atom split view is ready."""
    snapshot: dict[str, tuple[int, int, tuple[str, ...]]] = {}
    for resource in reader.iter():
        if resource.atom_split_status != "ready":
            continue
        try:
            stat = resource.path.stat()
        except OSError:
            continue
        snapshot[resource.id] = (
            stat.st_mtime_ns,
            stat.st_size,
            tuple(atom.atom_id for atom in resource.atoms),
        )
    return snapshot


def run_kernel_host(
    *,
    server: bool = False,
    stop_event=None,
    max_cycles: int | None = None,
) -> int:
    """Keep the selected external kernel alive and invoke it periodically."""
    from xskill.config import (
        CONFIG_PATH,
        XSKILL_HOME,
        get_kernel_evaluation_db_path,
        get_registry_db_path,
        get_skill_dir,
        get_team_trajectories_dir,
        kernel_config,
        load_config,
    )
    from xskill.kernels.base import KernelRunResult
    from xskill.kernels.catalog import KernelCatalog
    from xskill.kernels.context import TrajectoryReader
    from xskill.kernels.runtime import (
        KernelEvaluationStore,
        KernelRuntime,
        kernel_environment,
    )
    from xskill.utils.shutdown import SHUTTING_DOWN

    kernel_log = logging.getLogger("xskill.kernel.host")
    stop = stop_event or SHUTTING_DOWN
    runtime = None
    runtime_key: tuple[str, Path] | None = None
    previous_snapshot: dict[str, tuple[int, int, tuple[str, ...]]] = {}
    first_run = True
    next_run_at = time.monotonic()
    completed_cycles = 0
    interval = 1.0

    while not stop.is_set():
        config = load_config()
        selected = kernel_config(config, xskill_home=XSKILL_HOME)
        active = selected["active"]
        if active == "native":
            runtime = None
            runtime_key = None
            previous_snapshot = {}
            first_run = True
            if stop.wait(1.0):
                break
            continue

        selected_key = (active, selected["plugin_dir"])
        if runtime is None or selected_key != runtime_key:
            with kernel_environment(config):
                catalog = KernelCatalog(
                    plugin_dir=selected["plugin_dir"],
                    xskill_home=XSKILL_HOME,
                )
            runtime = KernelRuntime(
                active_kernel=active,
                catalog=catalog,
                skill_dir=get_skill_dir(
                    config, xskill_home=XSKILL_HOME,
                ).expanduser().resolve(),
                registry_db_path=get_registry_db_path(
                    xskill_home=XSKILL_HOME,
                ).expanduser().resolve(),
                evaluation_store=KernelEvaluationStore(
                    get_kernel_evaluation_db_path(xskill_home=XSKILL_HOME)
                ),
                trajectory_root=(get_team_trajectories_dir() / "clients"),
                xskill_config=config,
                xskill_config_path=CONFIG_PATH,
            )
            interval = runtime.external_run_interval()
            runtime_key = selected_key
            previous_snapshot = {}
            first_run = True
            next_run_at = time.monotonic()
            kernel_log.info(
                "external kernel host selected %s (interval %.1fs, server=%s)",
                active,
                interval,
                server,
            )

        remaining = next_run_at - time.monotonic()
        if remaining > 0:
            if stop.wait(min(remaining, 1.0)):
                break
            continue

        assert runtime is not None
        reader = TrajectoryReader(
            runtime.registry_db_path,
            root=runtime.trajectory_root,
        )
        current_snapshot = _trajectory_snapshot(reader)
        changed = tuple(sorted(
            resource_id
            for resource_id, fingerprint in current_snapshot.items()
            if first_run or previous_snapshot.get(resource_id) != fingerprint
        ))

        try:
            runtime.run_active(
                trigger="scheduled",
                dataset_id="live",
                changed_trajectory_ids=changed,
                full_rebuild=first_run,
                native_runner=lambda _invocation: KernelRunResult(),
            )
        except Exception:  # noqa: BLE001 - persistent process run boundary
            kernel_log.exception("external kernel %s run failed", active)
        else:
            kernel_log.info(
                "external kernel %s run finished (changed=%d, full_rebuild=%s)",
                active,
                len(changed),
                first_run,
            )
            previous_snapshot = current_snapshot
            first_run = False
        completed_cycles += 1
        if max_cycles is not None and completed_cycles >= max_cycles:
            return 0
        next_run_at = time.monotonic() + interval

    return 0


def _build_claude_code_ingester(*, home: str | None = None):
    """构造轻量 CC ingester；未检测到 Claude Code 时返回 ``None``。"""
    from xskill.config import (
        XSKILL_HOME,
        get_registry_db_path,
        get_skill_dir,
        load_config,
    )
    from xskill.ecosystems import (
        CCSessionIngester,
        detect_known_ecosystems,
        ensure_claude_code_install,
        install_all_to_claude_code,
    )
    from xskill.ecosystems._history import InstallHistory
    from xskill.pipeline.registry import register_dir

    config = load_config()
    home_root = Path(home).expanduser().resolve() if home else Path.home()
    skill_dir = get_skill_dir(
        config,
        xskill_home=XSKILL_HOME,
    ).expanduser().resolve()
    registry_db_path = get_registry_db_path(
        xskill_home=XSKILL_HOME,
    ).expanduser().resolve()
    detections = detect_known_ecosystems(home_root=home_root)
    claude_code_detection = next(
        (
            detection
            for detection in detections
            if detection["ecosystem"] == "claude_code"
        ),
        None,
    )
    if claude_code_detection is None:
        return None
    bridge_path = Path(claude_code_detection["bridge"])
    bridge_path.mkdir(parents=True, exist_ok=True)
    register_dir(
        bridge_path,
        label="claude_code sessions",
        ecosystem="claude_code",
        db_path=registry_db_path,
    )
    skill_paths = [
        skill_path
        for skill_path in sorted(skill_dir.iterdir())
        if skill_path.is_dir()
        and (skill_path / "SKILL.md").is_file()
    ]
    staging_names = {
        skill_path.name
        for skill_path in skill_paths
        if (
            skill_path.parent
            / ".canary"
            / skill_path.name
            / "SKILL.md"
        ).is_file()
    }
    install_all_to_claude_code(
        skill_dir,
        target_root=home_root,
        names=[
            skill_path.name
            for skill_path in skill_paths
            if skill_path.name not in staging_names
        ],
    )
    install_history = InstallHistory(
        XSKILL_HOME / "install_history.jsonl"
    )
    for skill_path in skill_paths:
        if skill_path.name in staging_names:
            ensure_claude_code_install(
                install_history,
                skill_path,
                target_root=home_root,
            )
    return CCSessionIngester(
        target_traj_dir=bridge_path,
        home_root=home_root,
        poll_interval=1.0,
        skill_dir=skill_dir,
        target_root=home_root,
        history_path=install_history.path,
        assignments_path=skill_dir / "session_assignments.jsonl",
        registry_db_path=registry_db_path,
    )


def run_ecosystem_ingest_once(*, home: str | None = None) -> int:
    """仅桥接 Claude Code 已完成 session 并轮转 side，不运行生产管线。"""
    try:
        ingester = _build_claude_code_ingester(home=home)
        if ingester is not None:
            ingester.run_once()
        return 0
    except Exception:  # noqa: BLE001 — 顶层任务边界，必须落日志并报失败
        logger.exception("ecosystem ingest once failed")
        return 1


def run_ecosystem_ingest_loop(
    *,
    home: str | None = None,
    interval: float = 0.5,
) -> int:
    """常驻轻量进程：保留 seen 索引，watcher 忙时仍逐 session 轮转。"""
    import threading

    if interval <= 0:
        raise ValueError("ecosystem ingest interval must be positive")
    wait_event = threading.Event()
    ingester = None
    while True:
        try:
            if ingester is None:
                ingester = _build_claude_code_ingester(home=home)
            if ingester is not None:
                ingester.run_once()
        except Exception:  # noqa: BLE001 — 常驻任务边界，记录后下一轮重建
            logger.exception("ecosystem ingest loop iteration failed")
            ingester = None
        wait_event.wait(interval)


def run_profile_refresh_once(*, engine=None) -> int:
    """只消费持久化脏用户；首次/版本变化/低频对账才批量标记全部 client。"""
    from xskill.config import (
        XSKILL_HOME,
        load_config,
        profile_refresh_config,
    )
    from xskill.pipeline.registry import get_registry_db_path
    from xskill.recommend.profile_dirty import (
        PROFILE_ALGORITHM_VERSION,
        clear_profile_dirty,
        list_dirty_profiles,
        reconcile_profile_dirty,
    )
    from xskill.team.server.engine_factory import build_recommend_engine
    from xskill.team.server.profile_refresh import ProfileRefreshService
    from xskill.utils.status_file import PROFILE_STATUS_FILE, write_status_file

    config = load_config()
    pr_cfg = profile_refresh_config(config)
    status_path = XSKILL_HOME / PROFILE_STATUS_FILE
    service = None
    try:
        if engine is None:
            engine = build_recommend_engine(config)
        client_rows = engine.client_registry.list()
        key_to_client: dict[str, str] = {}
        profile_keys: list[str] = []
        for row in client_rows:
            client_id = row["client_id"]
            key_to_client[client_id] = client_id
            try:
                profile_key = engine.client_registry.dir_name_for(client_id)
            except Exception:  # pylint: disable=broad-exception-caught
                profile_key = client_id
            key_to_client[profile_key] = client_id
            profile_keys.append(profile_key)
        registry_db = get_registry_db_path()
        model = getattr(getattr(engine, "embed_client", None), "model", "") or ""
        input_fingerprint = f"{PROFILE_ALGORITHM_VERSION}:{model}"
        reconcile_reason = reconcile_profile_dirty(
            profile_keys,
            input_fingerprint=input_fingerprint,
            db_path=registry_db,
        )
        dirty_rows = list_dirty_profiles(db_path=registry_db)
        dirty_by_client: dict[str, list[dict]] = {}
        stale_rows: list[dict] = []
        for row in dirty_rows:
            client_id = key_to_client.get(row["user_key"])
            if client_id:
                dirty_by_client.setdefault(client_id, []).append(row)
            else:
                stale_rows.append(row)
        for row in stale_rows:
            clear_profile_dirty(
                row["user_key"], row["generation"], db_path=registry_db,
            )

        def _finish(client_id: str, succeeded: bool) -> None:
            if not succeeded:
                return
            for dirty in dirty_by_client.get(client_id, []):
                clear_profile_dirty(
                    dirty["user_key"], dirty["generation"], db_path=registry_db,
                )

        # 批量任务:settle_delay=0 立即算(settle 是给在线 sync 突发让路用的,批量无此需求)。
        service = ProfileRefreshService(
            engine, workers=pr_cfg["workers"], queue_size=pr_cfg["queue_size"],
            settle_delay=0, autostart=True, on_processed=_finish,
        )
        requested = 0
        for client_id in dirty_by_client:
            if service.request(client_id):
                requested += 1
        service.wait_idle()
        metrics = dict(service.metrics)
        metrics["clients"] = len(client_rows)
        metrics["requested_clients"] = requested
        metrics["dirty_rows"] = len(dirty_rows)
        metrics["stale_rows"] = len(stale_rows)
        metrics["reconcile_reason"] = reconcile_reason
        write_status_file(status_path, metrics, ok=True)
        return 0
    except Exception as exc:  # noqa: BLE001 — 顶层任务边界,落状态文件+日志后报错
        logger.exception("profile refresh once failed")
        write_status_file(status_path, {}, ok=False, error=str(exc))
        return 1
    finally:
        if service is not None:
            service.stop(timeout=pr_cfg["shutdown_timeout"])


def run_recommend_heavy_once() -> int:
    """合并重活：画像刷新 → Milvus 对账 → 脏用户推荐预计算。

    替代原先仅跑 profile-refresh 的短命子进程；Web /sync 只读
    ``client_recommend_slots``，不再请求内 ``get_skill_for_client``。
    """
    from xskill.config import XSKILL_HOME, load_config, recommend_heavy_config
    from xskill.recommend.heavy_worker import run_recommend_heavy_once as _heavy_tick
    from xskill.team.server.engine_factory import build_recommend_engine
    from xskill.utils.status_file import PROFILE_STATUS_FILE, write_status_file

    status_path = XSKILL_HOME / PROFILE_STATUS_FILE
    try:
        config = load_config()
        heavy_cfg = recommend_heavy_config(config)
        engine = build_recommend_engine(config)
        profile_rc = run_profile_refresh_once(engine=engine)
        heavy = _heavy_tick(
            engine=engine,
            vector_sync_batch_limit=heavy_cfg["batch_limit"],
            memory_budget_mb=heavy_cfg["memory_budget_mb"],
        )
        vector_stats = heavy.get("vector", {})
        metrics = {
            "profile_rc": profile_rc,
            "vector_upserted": vector_stats.get("upserted", 0),
            "vector_deleted": vector_stats.get("deleted", 0),
            "vector_skipped": vector_stats.get("skipped", 0),
            "vector_mode": vector_stats.get("mode", ""),
            "vector_reason": vector_stats.get("reason", ""),
            # 全量对账（bootstrap/model_changed/periodic/ephemeral）还剩多少
            # catalog_key 没追平；0 表示这一轮之后已经追平，不是「查询侧还没
            # 收敛」——full 模式下该字段随每轮增量收敛，供看板/日志观察进度。
            "vector_remaining": vector_stats.get("remaining"),
            # 只在这一轮真播种了全量对账时才有值；平时纯增量运转不需要
            # 每轮都数一遍 catalog 有多少行。
            "vector_total_indexable": vector_stats.get("total_indexable"),
            "vector_budget_aborted": vector_stats.get("budget_aborted", False),
            "recommends": heavy.get("recommends", 0),
            "recommends_deferred": heavy.get("recommends_deferred", False),
            "index_kind": heavy.get("index_kind", ""),
            "rss_peak_mb": heavy.get("rss_peak_mb", 0.0),
        }
        write_status_file(status_path, metrics, ok=profile_rc == 0)
        return 0 if profile_rc == 0 else profile_rc
    except Exception as exc:  # noqa: BLE001 — 顶层任务边界
        logger.exception("recommend heavy once failed")
        write_status_file(status_path, {}, ok=False, error=str(exc))
        return 1


def run_ux_scores_sync_once() -> int:
    """一轮扫 skill_dir：UX jsonl + candidates pending 等盘→库投影。

    入口名仍为 ``ux-scores-sync``（配置键不变）；新投影挂
    ``skill_dir_sync``，禁止再开独立全量扫盘 worker。
    """
    from xskill.config import get_skill_dir
    from xskill.pipeline.skill_dir_sync import sync_skill_disk_projections

    skill_dir = get_skill_dir()
    stats = sync_skill_disk_projections(skill_dir)
    ux = stats["ux"]
    pending = stats["pending"]
    logger.info(
        "skill_dir sync done ux(skills=%s lines=%s inserted=%s) "
        "pending(skills=%s synced=%s skipped=%s orphans=%s)",
        ux["skills"], ux["lines"], ux["inserted"],
        pending["skills"], pending["synced"], pending["skipped"],
        pending["orphans"],
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    """``python -m xskill._workers <kind>`` 入口(供调度器 spawn)。"""
    import argparse

    from xskill.config import get_logs_dir
    from xskill.utils.logging import configure_logging

    parser = argparse.ArgumentParser(prog="xskill._workers")
    sub = parser.add_subparsers(dest="kind", required=True)
    p_worker = sub.add_parser("agent-worker")
    p_worker.add_argument("--server", action="store_true")
    p_worker.add_argument("--home", default=None)
    p_kernel = sub.add_parser("kernel-host")
    p_kernel.add_argument("--server", action="store_true")
    p_ingest = sub.add_parser("ecosystem-ingest")
    p_ingest.add_argument("--home", default=None)
    p_ingest.add_argument("--loop", action="store_true")
    p_ingest.add_argument("--interval", type=float, default=0.5)
    sub.add_parser("profile-refresh")
    sub.add_parser("recommend-heavy")
    sub.add_parser("ux-scores-sync")
    args = parser.parse_args(argv)

    configure_logging(
        get_logs_dir(),
        debug=False,
        quiet=False,
        # kernel-host 的 stdout 已被调度器追加进 xskill.kernel.log；再开
        # StreamHandler 会让 xskill.kernel.openearth 进度日志写两遍。
        stdout=args.kind != "kernel-host",
    )
    if args.kind == "kernel-host":
        for stream in (sys.stdout, sys.stderr):
            reconfigure = getattr(stream, "reconfigure", None)
            if reconfigure is not None:
                try:
                    reconfigure(line_buffering=True)
                except (OSError, ValueError):
                    pass
        return run_kernel_host(server=args.server)
    if args.kind == "agent-worker":
        return run_agent_worker_forever(server=args.server, home=args.home)
    if args.kind == "ecosystem-ingest":
        if args.loop:
            return run_ecosystem_ingest_loop(
                home=args.home,
                interval=args.interval,
            )
        return run_ecosystem_ingest_once(home=args.home)
    if args.kind == "ux-scores-sync":
        return run_ux_scores_sync_once()
    if args.kind in ("recommend-heavy", "profile-refresh"):
        # profile-refresh 入口保留兼容，实际走合并重活（画像+向量+推荐）。
        return run_recommend_heavy_once()
    return run_recommend_heavy_once()


if __name__ == "__main__":
    sys.exit(main())
