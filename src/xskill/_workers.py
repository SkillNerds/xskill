"""内部 worker 子进程入口（**非用户 CLI**）。

watcher / 画像重计算拆成短命子进程:web 进程的 ``IntervalSubprocessScheduler`` 用
``[sys.executable, "-m", "xskill._workers", <kind>]`` spawn 一个全新解释器进程,跑一轮
即退,GIL 与 web 事件循环彻底隔离。外部 Kernel 则由 ``kernel-host`` 常驻子进程复用
Kernel 实例并自行按 ``run_interval`` 驱动。

这些是**内部管道**,刻意不注册进 ``xskill`` 用户 CLI(``cli.build_parser``)——用户
``xskill --help`` 看不到它们。调度器直接调本模块的 SDK 函数(``run_sweep_once`` /
``run_profile_refresh_once``),或经 ``python -m xskill._workers`` 入口。
"""
from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

logger = logging.getLogger("xskill._workers")


def run_sweep_once(
    *,
    server: bool = False,
    home: str | None = None,
    native_only: bool = False,
) -> int:
    """跑一轮 watcher sweep(采集→拆分→聚类→灰度)即退,状态落 watcher_status.json。

    非 server 先对本机各生态一次性入库(ingest_detected_ecosystems_once),再
    ``build_watcher`` + ``run_once_and_drain``(一轮 = daemon 一个 poll)。多阶段流水线
    靠调度器反复 spawn 逐轮推进。team_server 模式跳过本机生态采集。
    """
    from xskill.config import (
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
    from xskill.kernels.runtime import KernelEvaluationStore, KernelRuntime
    from xskill.pipeline.watcher_factory import (
        build_watcher,
        ingest_detected_ecosystems_once,
    )
    from xskill.utils.status_file import WATCHER_STATUS_FILE, write_status_file

    config = load_config()
    home_root = Path(home).expanduser().resolve() if home else Path.home()
    status_path = XSKILL_HOME / WATCHER_STATUS_FILE
    watcher = None
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
        if not server:
            ingest_detected_ecosystems_once(
                config,
                home_root,
                skill_dir,
                registry_db_path=registry_db_path,
                install_history_path=install_history_path,
            )
        selected = kernel_config(config, xskill_home=XSKILL_HOME)
        if native_only and selected["active"] != "native":
            logger.info(
                "native sweep skipped while external kernel %s is selected",
                selected["active"],
            )
            return 0
        catalog = KernelCatalog(
            plugin_dir=selected["plugin_dir"],
            xskill_home=XSKILL_HOME,
        )
        runtime = KernelRuntime(
            active_kernel=selected["active"],
            catalog=catalog,
            skill_dir=skill_dir,
            registry_db_path=registry_db_path,
            evaluation_store=KernelEvaluationStore(
                get_kernel_evaluation_db_path(xskill_home=XSKILL_HOME)
            ),
            trajectory_root=(
                get_team_trajectories_dir() / "clients"
                if server else None
            ),
        )

        def run_native(_request):
            nonlocal watcher
            watcher = build_watcher(
                config,
                xskill_home=XSKILL_HOME,
                config_path=XSKILL_HOME / "config.yaml",
                db_path=registry_db_path,
                skill_dir=skill_dir,
                home_root=home_root,
                server_mode=server,
            )
            watcher.run_once_and_drain()
            return KernelRunResult(metrics=dict(watcher.stats))

        descriptor, result = runtime.run_active(
            trigger="scheduled",
            native_runner=run_native,
        )
        status_stats = (
            watcher.stats
            if watcher is not None
            else {
                "kernel_id": descriptor.id,
                "processed_trajectories": len(result.processed_trajectory_ids),
                "submitted_skills": len(result.submitted_skills),
                **dict(result.metrics),
            }
        )
        write_status_file(status_path, status_stats, ok=True)
        return 0
    except Exception as exc:  # noqa: BLE001 — 顶层任务边界,落状态文件+日志后报错
        logger.exception("sweep once failed")
        write_status_file(
            status_path,
            watcher.stats if watcher is not None else {},
            ok=False,
            error=str(exc),
        )
        return 1


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
    previous_snapshot: dict[str, tuple[int, int]] = {}
    first_run = True
    next_run_at = time.monotonic()
    completed_cycles = 0

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
    """常驻轻量进程：保留 seen 索引，重 sweep 堵塞时仍逐 session 轮转。"""
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


def run_profile_refresh_once() -> int:
    """遍历所有 client 重算画像落库即退,状态落 profile_refresh_status.json。

    复用 ProfileRefreshService(短命形态:批量提交所有 client → wait_idle → stop),
    含散点物化子系统,进程退出即销毁,不留常驻线程。``update_user_interest`` 自带
    revision 未变早退,冷启动全量跑一遍也只对变化的 client 真算增量批量 embedding。
    """
    from xskill.config import XSKILL_HOME, load_config, profile_refresh_config
    from xskill.team.server.engine_factory import build_recommend_engine
    from xskill.team.server.profile_refresh import ProfileRefreshService
    from xskill.utils.status_file import PROFILE_STATUS_FILE, write_status_file

    config = load_config()
    pr_cfg = profile_refresh_config(config)
    status_path = XSKILL_HOME / PROFILE_STATUS_FILE
    service = None
    try:
        engine = build_recommend_engine(config)
        client_ids = [row["client_id"] for row in engine.client_registry.list()]
        # 批量任务:settle_delay=0 立即算(settle 是给在线 sync 突发让路用的,批量无此需求)。
        service = ProfileRefreshService(
            engine, workers=pr_cfg["workers"], queue_size=pr_cfg["queue_size"],
            settle_delay=0, autostart=True,
        )
        for client_id in client_ids:
            service.request(client_id)
        service.wait_idle()
        metrics = dict(service.metrics)
        metrics["clients"] = len(client_ids)
        write_status_file(status_path, metrics, ok=True)
        return 0
    except Exception as exc:  # noqa: BLE001 — 顶层任务边界,落状态文件+日志后报错
        logger.exception("profile refresh once failed")
        write_status_file(status_path, {}, ok=False, error=str(exc))
        return 1
    finally:
        if service is not None:
            service.stop(timeout=pr_cfg["shutdown_timeout"])


def main(argv: list[str] | None = None) -> int:
    """``python -m xskill._workers <kind>`` 入口(供调度器 spawn)。"""
    import argparse

    from xskill.config import get_logs_dir
    from xskill.utils.logging import configure_logging

    parser = argparse.ArgumentParser(prog="xskill._workers")
    sub = parser.add_subparsers(dest="kind", required=True)
    p_sweep = sub.add_parser("sweep")
    p_sweep.add_argument("--server", action="store_true")
    p_sweep.add_argument("--home", default=None)
    p_sweep.add_argument("--native-only", action="store_true")
    p_kernel = sub.add_parser("kernel-host")
    p_kernel.add_argument("--server", action="store_true")
    p_ingest = sub.add_parser("ecosystem-ingest")
    p_ingest.add_argument("--home", default=None)
    p_ingest.add_argument("--loop", action="store_true")
    p_ingest.add_argument("--interval", type=float, default=0.5)
    sub.add_parser("profile-refresh")
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
    if args.kind == "sweep":
        return run_sweep_once(
            server=args.server,
            home=args.home,
            native_only=args.native_only,
        )
    if args.kind == "kernel-host":
        return run_kernel_host(server=args.server)
    if args.kind == "ecosystem-ingest":
        if args.loop:
            return run_ecosystem_ingest_loop(
                home=args.home,
                interval=args.interval,
            )
        return run_ecosystem_ingest_once(home=args.home)
    return run_profile_refresh_once()


if __name__ == "__main__":
    sys.exit(main())
