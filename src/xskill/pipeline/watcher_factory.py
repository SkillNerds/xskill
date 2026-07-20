"""watcher 构造 + 生态一次性入库,供 web startup(已移除)与短命 ``sweep --once``
子进程共用——construct 逻辑收敛到一处,source 唯一。

watcher 拆成短命子进程后,原 web startup 里的 ``_ensure_ingesters_for_detected_
ecosystems`` daemon-hook 闭包(每 poll 起常驻 ingester 线程)迁到本模块的
``ingest_detected_ecosystems_once``:每次 sweep 对每个检测到的生态调
``ingester.run_once()``(单轮桥接即返回),不起常驻线程。
"""
from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger("xskill.pipeline.watcher_factory")


def build_watcher(config: dict, *, home_root: Path | None = None,
                  xskill_home: Path | None = None,
                  config_path: Path | None = None,
                  db_path: Path | None = None,
                  skill_dir: Path | None = None,
                  logs_dir: Path | None = None,
                  spill_root: Path | None = None,
                  server_mode: bool = False, on_poll_hook=None):
    """构造 ``DirectoryWatcher``(读 watcher 段 + 造 llm/embed 客户端)。

    与原 web startup 的构造收敛到一处。``on_poll_hook`` 给短命 sweep 传 None
    (生态一次性入库由 ``ingest_detected_ecosystems_once`` 显式调,不走 poll hook)。
    """
    from xskill.config import (
        XSKILL_HOME,
        get_registry_db_path,
        get_skill_dir,
    )
    from xskill.pipeline.runner import DirectoryWatcher
    from xskill.usage import UsageLedger, load_price_table
    from xskill.utils.llm import create_embed_client, create_llm_client

    state_root = (
        Path(xskill_home) if xskill_home is not None else XSKILL_HOME
    ).expanduser().resolve()
    resolved_db_path = (
        Path(db_path)
        if db_path is not None
        else get_registry_db_path(xskill_home=state_root)
    ).expanduser().resolve()
    resolved_skill_dir = (
        Path(skill_dir)
        if skill_dir is not None
        else get_skill_dir(config, xskill_home=state_root)
    ).expanduser().resolve()
    resolved_config_path = (
        Path(config_path)
        if config_path is not None
        else state_root / "config.yaml"
    ).expanduser().resolve()
    resolved_logs_dir = (
        Path(logs_dir)
        if logs_dir is not None
        else state_root / "logs"
    ).expanduser().resolve()
    resolved_spill_root = (
        Path(spill_root)
        if spill_root is not None
        else state_root / "tmp" / "spill"
    ).expanduser().resolve()
    usage_ledger = UsageLedger(
        load_price_table(
            config.get("pricing"),
            cache_path=state_root / "cache" / "model_prices.json",
        ),
        db_path=resolved_db_path,
    )
    watcher_cfg = config.get("watcher", {})
    return DirectoryWatcher(
        llm=create_llm_client(config, usage_ledger=usage_ledger),
        embed_client=create_embed_client(
            config, usage_ledger=usage_ledger
        ),
        config=config,
        skill_dir=resolved_skill_dir,
        poll_interval=float(watcher_cfg.get("poll_interval", 30)),
        max_concurrent=int(watcher_cfg.get("max_concurrent", 30)),
        cluster_batch_size=int(watcher_cfg.get("cluster_batch_size", 8)),
        server_mode=server_mode,
        home_root=home_root,
        xskill_home=state_root,
        config_path=resolved_config_path,
        db_path=resolved_db_path,
        logs_dir=resolved_logs_dir,
        spill_root=resolved_spill_root,
        usage_ledger=usage_ledger,
        on_poll_hook=on_poll_hook,
    )


def ingest_detected_ecosystems_once(config: dict, home_root: Path,
                                    skill_dir: Path, *,
                                    registry_db_path: Path,
                                    install_history_path: Path) -> None:
    """检测本机各生态 → 装 skill → 对每个生态一次性桥接其 session。

    替代常驻 ingester 线程:每个生态调 ``ingester.run_once()``(单轮即返回)。整体
    catch:自动检测失败只 warn,不影响 sweep 主流程(与原 daemon-hook 一致)。
    ``team_server`` 模式由调用方(_workers.run_sweep_once)判定后不调本函数——纯 server 不采集本机
    本地轨迹。
    """
    from xskill.canary import CanaryConfig
    from xskill.ecosystems import (
        CCSessionIngester, JsonlIngester, SqliteIngester, TraeIngester,
        CODEX_SPEC, CURSOR_SPEC, NGA3_SPEC, NGAGENT_SPEC, OPENCLAW_SPEC,
        OPENCODE_SPEC,
        detect_known_ecosystems,
        ensure_claude_code_install,
        install_all_to_claude_code,
        install_all_to_codex, install_all_to_cursor,
        install_all_to_nga3, install_all_to_ngagent, install_all_to_opencode,
        install_all_to_openclaw, install_all_to_trae,
        make_openclaw_canary_flip_hook,
    )
    from xskill.ecosystems._history import InstallHistory
    from xskill.pipeline.registry import register_dir

    install_history = InstallHistory(install_history_path)
    poll_interval = float(config.get("watcher", {}).get("poll_interval", 10))

    try:
        detections = detect_known_ecosystems(home_root=home_root)
    except Exception:
        logger.warning("ecosystem auto-detect failed", exc_info=True)
        return

    for det in detections:
        eco = det["ecosystem"]
        bridge: Path = det["bridge"]
        bridge.mkdir(parents=True, exist_ok=True)
        register_dir(
            bridge,
            label=f"{eco} sessions",
            ecosystem=eco,
            db_path=registry_db_path,
        )

        if eco == "claude_code":
            try:
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
                main_names = [
                    skill_path.name
                    for skill_path in skill_paths
                    if skill_path.name not in staging_names
                ]
                install_all_to_claude_code(
                    skill_dir,
                    target_root=home_root,
                    names=main_names,
                )
                ensured_count = 0
                for skill_path in skill_paths:
                    if skill_path.name not in staging_names:
                        continue
                    ensure_claude_code_install(
                        install_history,
                        skill_path,
                        target_root=home_root,
                    )
                    ensured_count += 1
                logger.info(
                    "Claude Code installations ensured without side reset: %d",
                    ensured_count,
                )
            except Exception as exc:
                logger.warning("ensure_claude_code_install failed", exc_info=True)
                install_history.record_fail(skill="<startup_all>", agent="claude_code",
                                            reason=str(exc)[:200])
            CCSessionIngester(
                target_traj_dir=bridge, home_root=home_root,
                poll_interval=poll_interval, skill_dir=skill_dir,
                target_root=home_root, history_path=install_history_path,
                assignments_path=skill_dir / "session_assignments.jsonl",
                registry_db_path=registry_db_path,
            ).run_once()

        elif eco == "codex":
            try:
                install_all_to_codex(skill_dir, target_root=home_root)
            except Exception as exc:
                logger.warning("install_all_to_codex failed", exc_info=True)
                install_history.record_fail(skill="<startup_all>", agent="codex",
                                            reason=str(exc)[:200])
            JsonlIngester(CODEX_SPEC, target_traj_dir=bridge, home_root=home_root,
                          poll_interval=poll_interval,
                          registry_db_path=registry_db_path).run_once()

        elif eco == "nga3":
            try:
                installed = install_all_to_nga3(skill_dir, target_root=home_root)
                for dest in installed:
                    install_history.record(skill=dest.parent.name, side="main", sha="")
            except Exception as exc:
                logger.warning("install_all_to_nga3 failed", exc_info=True)
                install_history.record_fail(skill="<startup_all>", agent="nga3",
                                            reason=str(exc)[:200])
            JsonlIngester(NGA3_SPEC, target_traj_dir=bridge, home_root=home_root,
                          poll_interval=poll_interval,
                          registry_db_path=registry_db_path).run_once()

        elif eco == "opencode":
            try:
                install_all_to_opencode(skill_dir, target_root=home_root)
            except Exception as exc:
                logger.warning("install_all_to_opencode failed", exc_info=True)
                install_history.record_fail(skill="<startup_all>", agent="opencode",
                                            reason=str(exc)[:200])
            SqliteIngester(target_traj_dir=bridge, home_root=home_root,
                           spec=OPENCODE_SPEC, poll_interval=poll_interval).run_once()

        elif eco == "ngagent":
            try:
                install_all_to_ngagent(skill_dir, target_root=home_root)
            except Exception as exc:
                logger.warning("install_all_to_ngagent failed", exc_info=True)
                install_history.record_fail(skill="<startup_all>", agent="ngagent",
                                            reason=str(exc)[:200])
            SqliteIngester(target_traj_dir=bridge, home_root=home_root,
                           spec=NGAGENT_SPEC, poll_interval=poll_interval).run_once()

        elif eco == "openclaw":
            try:
                installed = install_all_to_openclaw(skill_dir, target_root=home_root)
                for dest in installed:
                    install_history.record(skill=dest.parent.name, side="main", sha="")
            except Exception as exc:
                logger.warning("install_all_to_openclaw failed", exc_info=True)
                install_history.record_fail(skill="<startup_all>", agent="openclaw",
                                            reason=str(exc)[:200])
            canary_cfg = CanaryConfig.from_dict(config.get("canary", {}))
            flip_hook = make_openclaw_canary_flip_hook(
                skill_dir=skill_dir, target_root=home_root,
                history=install_history, probability=canary_cfg.probability,
            )
            JsonlIngester(OPENCLAW_SPEC, target_traj_dir=bridge, home_root=home_root,
                          poll_interval=poll_interval,
                          on_new_sessions=flip_hook,
                          registry_db_path=registry_db_path).run_once()

        elif eco == "cursor":
            try:
                installed = install_all_to_cursor(skill_dir, target_root=home_root)
                for dest in installed:
                    install_history.record(skill=dest.parent.name, side="main", sha="")
            except Exception as exc:
                logger.warning("install_all_to_cursor failed", exc_info=True)
                install_history.record_fail(skill="<startup_all>", agent="cursor",
                                            reason=str(exc)[:200])
            JsonlIngester(CURSOR_SPEC, target_traj_dir=bridge, home_root=home_root,
                          poll_interval=poll_interval,
                          registry_db_path=registry_db_path).run_once()

        elif eco == "trae":
            try:
                installed = install_all_to_trae(skill_dir, target_root=home_root)
                for dest in installed:
                    install_history.record(skill=dest.parent.name, side="main", sha="")
            except Exception as exc:
                logger.warning("install_all_to_trae failed", exc_info=True)
                install_history.record_fail(skill="<startup_all>", agent="trae",
                                            reason=str(exc)[:200])
            TraeIngester(target_traj_dir=bridge, home_root=home_root,
                         poll_interval=poll_interval).run_once()

        logger.info("ecosystem %s ingested once: source=%s bridge=%s",
                    eco, det.get("source"), bridge)
