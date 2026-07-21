"""内部短命子进程 worker(**非用户 CLI**)。

watcher / 画像重计算拆成短命子进程:web 进程的 ``IntervalSubprocessScheduler`` 用
``[sys.executable, "-m", "xskill._workers", <kind>]`` spawn 一个全新解释器进程,跑一轮
即退,GIL 与 web 事件循环彻底隔离。

这些是**内部管道**,刻意不注册进 ``xskill`` 用户 CLI(``cli.build_parser``)——用户
``xskill --help`` 看不到它们。调度器直接调本模块的 SDK 函数(``run_sweep_once`` /
``run_profile_refresh_once``),或经 ``python -m xskill._workers`` 入口。
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

logger = logging.getLogger("xskill._workers")


def run_sweep_once(*, server: bool = False, home: str | None = None) -> int:
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
    p_ingest = sub.add_parser("ecosystem-ingest")
    p_ingest.add_argument("--home", default=None)
    p_ingest.add_argument("--loop", action="store_true")
    p_ingest.add_argument("--interval", type=float, default=0.5)
    sub.add_parser("profile-refresh")
    args = parser.parse_args(argv)

    configure_logging(get_logs_dir(), debug=False, quiet=False, stdout=True)
    if args.kind == "sweep":
        return run_sweep_once(server=args.server, home=args.home)
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
