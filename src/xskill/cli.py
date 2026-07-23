#!/usr/bin/env python3
"""
cli.py — xskill 紧凑 CLI
═══════════════════════════════════════════════════════
主要子命令：
    xskill distill --kernel <id> --trajectory-dir <path> --output <path>
    xskill serve [--host] [--port]
    xskill registry add|remove|list <path>
    xskill search <关键词...> [--top-k]

所有筛选/格式化交给 shell（grep/awk）。状态/配置全在 ~/.xskill/。
"""

from __future__ import annotations

import argparse
import logging
import re
import sys

from xskill import __version__
from xskill._sqlite_connect import connect_with_lock
from xskill.config import set_overrides
from xskill.ecosystems import SQLITE_SPEC_BY_ECO

logger = logging.getLogger("xskill.cli")


# ═══════════════════════════════════════════════════════════════
# 子命令
# ═══════════════════════════════════════════════════════════════

def cmd_distill(args) -> int:
    """Turn one local trajectory directory into Skills with a kernel."""
    import json
    from pathlib import Path

    import yaml

    from xskill.config import CONFIG_PATH, XSKILL_HOME
    from xskill.kernels.distillation import (
        render_distillation_table,
        run_offline_distillation,
    )

    plugin_dir = Path(args.plugin_dir).expanduser().resolve() if args.plugin_dir else None
    if plugin_dir is None and CONFIG_PATH.is_file():
        raw = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
        kernel_section = raw.get("kernel", {}) if isinstance(raw, dict) else {}
        configured = (
            (
                kernel_section.get("kernels_path")
                or kernel_section.get("plugin_dir")
            )
            if isinstance(kernel_section, dict) else None
        )
        if configured:
            candidate = Path(str(configured)).expanduser()
            plugin_dir = (
                candidate if candidate.is_absolute()
                else (XSKILL_HOME / candidate)
            ).resolve()
    plugin_dir = plugin_dir or (XSKILL_HOME / "kernels").resolve()
    try:
        report = run_offline_distillation(
            kernel_id=args.kernel_id,
            trajectory_dir=Path(args.trajectory_dir),
            plugin_dir=plugin_dir,
            xskill_home=XSKILL_HOME,
            output_dir=Path(args.output).expanduser(),
            json_output=args.json,
            no_progress=args.no_progress,
        )
    except Exception as exc:  # noqa: BLE001 - CLI renders provider failures
        if args.json:
            print(json.dumps({
                "status": "error",
                "error": f"{type(exc).__name__}: {exc}",
            }, ensure_ascii=False))
        else:
            print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(
            report.as_dict(include_artifact_dir=True),
            ensure_ascii=False,
        ))
    else:
        print(render_distillation_table(report))
    return 0

def cmd_serve(args, xskill) -> int:
    # --home 用于 debug 模式：生态扫描只看该目录下的 .claude/，不碰真实
    # $HOME。要求顶层 --debug 同时打开，避免生产环境误用。
    home_root = None
    if args.home:
        if not args.debug:
            print("error: --home 仅在 --debug 模式下生效；加 --debug 或去掉 --home",
                  file=sys.stderr)
            return 2
        from pathlib import Path
        home_root = Path(args.home).expanduser().resolve()
        if not home_root.is_dir():
            print(f"error: --home 目录不存在: {home_root}", file=sys.stderr)
            return 2
    from xskill.runtime import read_status, write_running
    # ── 单实例守卫：已有活 daemon 时拒绝启动 ──
    # 双 daemon 会抢同一 registry（rebuild 后旧 daemon 可能用旧模型抢先处理）。
    # read_status 已校验 pid 存活，陈旧运行态文件不会误拦。--force 强行接管。
    status = read_status()
    if status.get("running") and not args.force:
        print(
            f"✗ 已有 xskill daemon 在运行（pid {status.get('pid')}, "
            f"端口 {status.get('port')}）。",
            file=sys.stderr,
        )
        print(
            "  双 daemon 会抢同一 registry，导致换模型 rebuild 被旧 daemon 抢去用旧"
            "模型处理。\n  先停掉它再起；确认要强行接管可加 --force。",
            file=sys.stderr,
        )
        return 2
    write_running(port=args.port, mode="server" if args.server else "standalone")
    xskill.serve(host=args.host, port=args.port, home_root=home_root,
                 server_mode=args.server)
    return 0


def cmd_registry(args, xskill) -> int:
    action = args.registry_action
    if action == "add":
        wd = xskill.registry.add(args.path, label=args.label or "")
        print(f"Registered: {wd.path}  id={wd.id}  label={wd.label!r}")
        return 0
    if action == "remove":
        ok = xskill.registry.remove(args.path)
        print("Removed." if ok else "Not found.")
        return 0 if ok else 1
    if action == "list":
        dirs = xskill.registry.list()
        if not dirs:
            print("(no registered directories)")
            return 0
        # 列序: id  ecosystem  traj  indexed  label  path
        # ecosystem 是来源标签：``manual`` = 用户手动注册；其他如
        # ``claude_code`` = daemon 启动时自动 detect 出来的生态目录。
        # 同时用 codex / opencode 等其他工具时一眼能区分来源。
        # 表头与数据行都用 \t 分隔；解析方只取含 ecosystem 名的数据行即可。
        print("ID\tECOSYSTEM\tTRAJ\tINDEXED\tLABEL\tPATH")
        for w in dirs:
            print(
                f"{w.id}\t{w.ecosystem}\t{w.traj_count}\t{w.indexed_count}\t"
                f"{w.label or '-'}\t{w.path}"
            )
        return 0
    return 1


def _standalone_watch_dir_count() -> int:
    """轻量读 registry.db 里 watch_dirs 行数（不建表、不走 facade/config）。

    用于判断本机是否有 standalone/server 数据。库文件或表不存在都视作 0
    ——这是"尚未初始化"的正常状态，不是错误，故显式查表而非吞异常。
    """
    import sqlite3
    from xskill.config import get_registry_db_path
    db = get_registry_db_path()
    if not db.is_file():
        return 0
    conn = connect_with_lock(sqlite3.connect, str(db))
    try:
        has_table = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='watch_dirs'"
        ).fetchone()
        if not has_table:
            return 0
        return int(conn.execute("SELECT COUNT(*) FROM watch_dirs").fetchone()[0])
    finally:
        conn.close()


def cmd_registry_list_client() -> int:
    """team 客户端模式的 ``registry list``。

    瘦客户端不写 ``watch_dirs`` / ``trajectories`` 表（那是 standalone/server
    的存储），它靠实时 ``detect_known_ecosystems`` 采集 + client SQLite 状态
    记上传进度。所以这里**现算**视图：每个探测到的生态显示

        ECOSYSTEM  COLLECTED  UPLOADED  SOURCE

    - COLLECTED = 该生态 bridge 目录下 ``traj_*.json`` 数（已镜像采集的轨迹）
    - UPLOADED  = 上述轨迹里已记入 client_state.db（已上传 server）的数
    - SOURCE    = 用户真实的原生目录（如 ~/.claude/projects），非内部 bridge

    不依赖 config.yaml / XSkill 门面——纯客户端机器也能直接看。
    """
    from pathlib import Path
    from xskill.config import (
        XSKILL_HOME, get_team_client_state_path, get_team_client_cursor_path,
        get_team_client_state_db_path,
    )
    from xskill.ecosystems import detect_known_ecosystems
    from xskill.team.client.state import load_client_state
    from xskill.team.client.upload_state import TrajectoryUploadStateStore

    home = XSKILL_HOME.parent  # 与 XSKILL_HOME 同源,避免 home 解析漂移
    # 上传状态按 server 分目录（方案 A）——先读连接状态拿 server_url 才能定位。
    # 没连过 server（无 state）则没有任何上传状态，uploaded 全 0。
    uploaded_ids: set[str] = set()
    state_path = get_team_client_state_path()
    if state_path.is_file():
        state = load_client_state(state_path)
        store = TrajectoryUploadStateStore(
            db_path=get_team_client_state_db_path(state.server_url),
            legacy_cursor_path=get_team_client_cursor_path(state.server_url),
            home_root=home,
        )
        uploaded_ids = store.uploaded_trajectory_ids()

    dets = detect_known_ecosystems(home_root=home)
    if not dets:
        print("(no agent ecosystems detected)")
        return 0
    print("ECOSYSTEM\tCOLLECTED\tUPLOADED\tSOURCE")
    for det in dets:
        bridge = Path(det["bridge"])
        bridge_ids = (
            {p.stem for p in bridge.glob("traj_*.json")}
            if bridge.is_dir() else set()
        )
        collected = len(bridge_ids)
        uploaded = len(bridge_ids & uploaded_ids)
        print(f"{det['ecosystem']}\t{collected}\t{uploaded}\t{det['source']}")
    return 0


def cmd_init(args) -> int:
    """一站式引导：安装 XSkill Skills 并连接 team server。

    交互式（默认）逐项询问缺失的 server/token/工号；带齐 flag 且 ``--yes`` 可无头执行。
    """
    from pathlib import Path

    interactive = not args.yes
    target_root = None
    if args.target_root:
        target_root = Path(args.target_root).expanduser().resolve()

    if not args.no_skill:
        from importlib.resources import files
        from xskill.ecosystems import (
            detect_known_ecosystems,
            install_to_claude_code, install_to_codex, install_to_cursor,
            install_to_nga3, install_to_ngagent, install_to_openclaw,
            install_to_opencode, install_to_trae,
        )
        installer_by_eco = {
            "claude_code": install_to_claude_code,
            "codex": install_to_codex,
            "nga3": install_to_nga3,
            "opencode": install_to_opencode,
            "ngagent": install_to_ngagent,
            "openclaw": install_to_openclaw,
            "cursor": install_to_cursor,
            "trae": install_to_trae,
        }
        skill_source = Path(str(files("xskill") / "data" / "skill" / "xskill"))
        valid_sources = [skill_source] if (skill_source / "SKILL.md").is_file() else []
        if not valid_sources:
            print(
                f"warning: 捆绑的 xskill skill 缺失（{skill_source}），跳过",
                file=sys.stderr,
            )

        detections = detect_known_ecosystems(home_root=target_root)
        installed: list[tuple[str, str]] = []
        for detection in detections:
            ecosystem = detection["ecosystem"]
            install_fn = installer_by_eco.get(ecosystem)
            if install_fn is None:
                continue
            for skill_source in valid_sources:
                try:
                    install_fn(skill_source, target_root=target_root, side="main")
                    installed.append((skill_source.name, ecosystem))
                except Exception as install_error:  # noqa: BLE001
                    print(
                        f"warning: {skill_source.name} 装到 {ecosystem} 失败："
                        f"{install_error}",
                        file=sys.stderr,
                    )
        if installed:
            ecosystems = list(dict.fromkeys(item[1] for item in installed))
            skill_names = list(dict.fromkeys(item[0] for item in installed))
            commands = "、".join(f"/{name}" for name in skill_names)
            print(
                f"已把 {','.join(skill_names)} 装进 {'/'.join(ecosystems)} 的 "
                f"skill 目录，在对应 agent 里可直接使用 {commands}。"
            )
        elif detections:
            print("没有可安装的捆绑 Skill，已跳过安装。")
        else:
            print("未检测到已知 agent 生态（claude_code/codex/opencode/cursor/… "
                  "均未发现），跳过装 skill。")

    if args.skills_only:
        return 0

    from xskill.team.client.service import read_daemon_state
    daemon_state = read_daemon_state()
    if daemon_state.get("running"):
        current_server = "?"
        try:
            from xskill.config import get_team_client_state_path
            from xskill.team.client.state import load_client_state
            current_server = load_client_state(get_team_client_state_path()).server_url
        except Exception:  # noqa: BLE001
            logger.debug("读取当前 team server 地址失败", exc_info=True)
        print(f"检测到常驻连接正在运行：server={current_server}  "
              f"pid={daemon_state.get('pid')}  backend={daemon_state.get('backend')}")
        should_stop = args.force
        if not args.force:
            if not interactive:
                print("已保留现有连接（加 --force 可停掉重新配置）。")
                return 0
            should_stop = input("停掉并重新配置？[y/N] ").strip().lower() in ("y", "yes")
            if not should_stop:
                print("保留现有连接，未改动。")
                return 0
        from xskill.team.client.service import (
            ServiceError, clear_daemon_state, get_backend,
        )
        try:
            get_backend().stop()
        except ServiceError as stop_error:
            print(f"warning: 停止旧常驻失败：{stop_error}", file=sys.stderr)
        clear_daemon_state()

    address = args.address
    if not address and interactive:
        address = input("server 地址 (host:port): ").strip()
    if not address:
        print("error: 缺少 server 地址（位置参数或交互输入）", file=sys.stderr)
        return 2
    token = args.token
    if not token and interactive:
        token = input("join token (server 启动时打印的 token): ").strip()
    if not token:
        print("error: 缺少 --token（首次连接必填）", file=sys.stderr)
        return 2
    name = args.name
    if not name and interactive:
        name = input("工号/用户 ID (推荐填，直接回车留空): ").strip() or None

    connect_args = argparse.Namespace(
        address=address, token=token, label=args.label, name=name,
        use_proxy=args.use_proxy, foreground=args.foreground,
        no_auto_update=args.no_auto_update,
    )
    exit_code = cmd_connect(connect_args)
    if exit_code == 0:
        print("\n后续：`xskill status` 看状态 · `xskill search <词>` 搜技能 · "
              "`xskill update`／`pip install -U xskill` 升级 · `xskill stop` 停。")
    return exit_code


def cmd_connect(args) -> int:
    """team 瘦客户端：连上 server。

    ``xskill connect <host:port> --token <t>``  首次握手 + 落盘连接信息
    ``xskill connect``                          复用已存连接
    ``xskill connect ... --foreground``          前台阻塞跑守护循环

    默认（非 --foreground）：完成握手 / 校验连接信息后，把常驻循环交给操作系统的
    守护设施（Windows「计划任务」；Linux/WSL 优先 systemd user）在
    后台拉起，命令随即返回——用户不必一直开着终端。``--foreground`` 才是真正阻塞的
    轮询循环，也是后台任务实际 execute 的形态（见 team.client.service）。
    """
    from xskill.config import get_team_client_state_path
    from xskill.team.client.state import load_client_state

    state_path = get_team_client_state_path()

    if args.address:
        state = _connect_handshake(args, state_path)
        if state is None:
            return 1 if args.token else 2
    else:
        try:
            state = load_client_state(state_path)
        except FileNotFoundError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1

    from xskill.team.client.service import ServiceError, get_backend
    backend = get_backend()

    # 前台模式，或本平台还没有原生常驻后端：直接阻塞跑守护循环。
    if args.foreground or not backend.supported:
        print(f"reconnecting: client_id={state.client_id}  server={state.server_url}")
        _run_team_client_forever(state, use_proxy=args.use_proxy,
                                 auto_update=not args.no_auto_update)
        return 0

    # 默认（有原生后端的平台，如 Windows）：交给操作系统守护设施后台拉起。
    try:
        st = backend.install_and_start()
    except ServiceError as e:
        print(f"error: 后台常驻启动失败：{e}", file=sys.stderr)
        print("  可先用 `xskill connect --foreground` 在前台验证连接是否正常。",
              file=sys.stderr)
        return 1
    print(f"background task started: {st.get('task_name') or st.get('backend')}"
          f"  (pid={st.get('pid')})")
    print("  用 `xskill status` 查看，`xskill stop` 停止。")
    return 0


def _connect_handshake(args, state_path):
    """带 address 的首次/重连握手：register → 落盘 state。返回 ClientState 或 None。"""
    import socket as _socket
    from xskill.team.client.state import (
        ClientState, load_client_state, save_client_state,
    )
    from xskill.team.client.daemon import register_with_server_full

    if not args.token:
        print("error: 首次 connect 必须带 --token（server 启动时打印的 join token）",
              file=sys.stderr)
        return None
    server_url = args.address
    if not server_url.startswith("http"):
        server_url = f"http://{server_url}"
    # 带参 connect 也尽量保身份不漂移：本地 state 文件若存在就读出已有 client_id，
    # 作为 ``claimed_client_id`` 一起发给 server——server 按 (claimed/fingerprint/
    # new) 三级判定续用。state 不在 → existing_client_id=None，让 server 按指纹回查。
    existing_client_id: str | None = None
    if state_path.is_file():
        try:
            existing_client_id = load_client_state(state_path).client_id
        except Exception:
            # state 文件损坏不阻断重连——按"无本地身份"处理，让 server 走指纹回查
            # 或新发。损坏的 state 接下来会被新的 save 覆盖。
            existing_client_id = None
    import httpx
    # 默认 trust_env=False：team server 是已知、可直连的内网主机，绕开公司代理
    # （SWG）才是正确语义——经代理常因代理出口连不上 server 而 504。--use-proxy 时
    # 恢复读取系统/环境代理（含 Windows 注册表代理）。
    http = httpx.Client(base_url=server_url, timeout=30.0,
                        trust_env=args.use_proxy)
    try:
        reg = register_with_server_full(
            http, token=args.token,
            label=args.label or _socket.gethostname(),
            hostname=_socket.gethostname(),
            existing_client_id=existing_client_id,
            user_name=args.name or None,
        )
        client_id = reg["client_id"]
    except Exception as e:
        print(f"error: 注册失败: {e}", file=sys.stderr)
        return None
    state = ClientState(server_url=server_url, client_id=client_id,
                        join_token=args.token)
    save_client_state(state, state_path)
    name_hint = f"  (--name={args.name})" if args.name else ""
    print(f"connected: client_id={client_id}  server={server_url}{name_hint}")
    # P2-2.2(Q2a):server 为命名用户发放 dashboard 登录 token,这里打印一次。
    # token 幂等(重连拿到同一个),丢了重新 connect 即可再看到。
    dash_token = reg.get("dashboard_token")
    if dash_token:
        print(f"dashboard 登录: 用户名 {args.name} + token {dash_token}"
              f"  (server 看板地址 {server_url}/)")
    return state


def _run_team_client_forever(state, *, use_proxy: bool,
                             auto_update: bool = True) -> None:
    """构造 TeamClient 并阻塞跑守护循环。"""
    import httpx
    from xskill.config import (
        XSKILL_HOME, get_team_client_cursor_path, get_team_client_history_path,
    )
    from xskill.team.client.daemon import TeamClient

    http = httpx.Client(base_url=state.server_url, timeout=30.0,
                        trust_env=use_proxy)
    client = TeamClient(
        state=state, http=http,
        skill_dir=XSKILL_HOME / "skill",
        cursor_path=get_team_client_cursor_path(state.server_url),
        history_path=get_team_client_history_path(state.server_url),
        auto_update=auto_update,
        use_proxy=use_proxy,
    )
    client.run_forever()   # 阻塞


def _print_connect_status(st: dict, as_json: bool) -> None:
    """渲染 `xskill status` 输出。"""
    import json as _json
    if as_json:
        printable = {k: v for k, v in st.items() if k != "schtasks_query"}
        print(_json.dumps(printable, ensure_ascii=False, indent=2))
        return
    running = st.get("running")
    mark = "● running" if running else ("○ stopped" if st.get("installed")
                                        else "— not installed")
    print(f"connect daemon: {mark}")
    if st.get("task_name"):
        print(f"  task     : {st['task_name']} ({st.get('backend')})")
    elif st.get("unit_name"):
        print(f"  service  : {st['unit_name']} ({st.get('backend')})")
    elif st.get("backend"):
        print(f"  backend  : {st['backend']}")
    if st.get("platform"):
        print(f"  platform : {st['platform']}")
    if st.get("method"):
        print(f"  method   : {st['method']}")
    if st.get("pid"):
        print(f"  pid      : {st['pid']}")
    if st.get("log_path"):
        print(f"  log      : {st['log_path']}")
    if st.get("server_url"):
        print(f"  server   : {st['server_url']}")
    if st.get("client_id"):
        print(f"  client_id: {st['client_id']}")
    if st.get("warning"):
        print(f"  warning  : {st['warning']}")


def cmd_start(args) -> int:
    """安装并启动 connect 常驻任务（未 connect 过则提示先 connect）。"""
    from xskill.config import get_team_client_state_path
    from xskill.team.client.service import ServiceError, get_backend
    if not get_team_client_state_path().is_file():
        print("error: 尚未连接过 server。先跑一次：\n"
              "  xskill connect <host:port> --token <token>",
              file=sys.stderr)
        return 2
    try:
        st = get_backend().install_and_start()
    except ServiceError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    _print_connect_status(st, as_json=getattr(args, "json", False))
    return 0


def cmd_update(args) -> int:
    """立即检查 PyPI 是否有新版 xskill；PyPI 通道失败时走 team server wheel 回退。"""
    from packaging.version import Version
    from xskill.config import get_team_client_state_path
    from xskill.team.client.updater import (
        AutoUpdater, _current_version, _latest_pypi_version, _restart,
    )
    current = _current_version("xskill")
    if not current:
        print("error: 无法读取当前版本", file=sys.stderr)
        return 1
    try:
        current_version = Version(current)
    except Exception:
        current_version = None

    # 已 connect 过 team server 时带上连接信息，PyPI 失败可回退 server wheel。
    server_kwargs: dict = {}
    client_state_path = get_team_client_state_path()
    if client_state_path.is_file():
        from xskill.team.client.state import load_client_state
        try:
            client_state = load_client_state(client_state_path)
            server_kwargs = {
                "server_url": client_state.server_url,
                "client_id": client_state.client_id,
                "join_token": client_state.join_token,
            }
        except Exception as state_error:
            # 连接信息坏了只影响回退通道，不该挡住 PyPI 主路径。
            print(f"warning: 读取 team 连接信息失败，禁用 server 回退（{state_error}）",
                  file=sys.stderr)
    updater = AutoUpdater(**server_kwargs, use_proxy=args.use_proxy)

    print(f"当前版本: {current}")
    print("正在查询 PyPI...")
    latest = _latest_pypi_version("xskill")
    if not latest:
        print("查询 PyPI 失败，尝试 team server 通道...")
        if current_version is not None and updater._check_server_fallback(
            current, current_version, reason="pypi_query_failed", restart=False,
        ):
            print("已通过 team server wheel 升级完成")
            return 0
        print("error: 查询 PyPI 失败且 server 通道不可用，请检查网络",
              file=sys.stderr)
        return 1
    try:
        if current_version is not None and Version(latest) <= current_version:
            if updater._check_server_fallback(
                current, current_version, reason="pypi_not_ahead", restart=False,
            ):
                print("已通过 team server wheel 升级完成")
                return 0
            print(f"已是最新版本 ({current})")
            return 0
    except Exception:
        logger.warning("PyPI 返回的版本号无法比较：%s", latest, exc_info=True)
    print(f"发现新版本: {latest}，开始升级...")
    if not updater._install(latest):
        print("pip 升级失败，尝试 team server 通道...")
        if current_version is not None and updater._check_server_fallback(
            current, current_version, reason="pypi_install_failed", restart=False,
        ):
            print("已通过 team server wheel 升级完成")
            return 0
        print("error: 升级失败，请检查 pip 配置和日志", file=sys.stderr)
        return 1
    print(f"升级到 {latest} 成功，正在重启...")
    _restart()
    return 0  # 不会到达这里


def cmd_dashboard(args) -> int:
    """向 server 要一条免密登录链接并打印：点开即以自己的身份进入看板。"""
    del args  # CLI handler signature compatibility.
    import json
    import urllib.error
    import urllib.request
    from xskill.config import get_team_client_state_path
    from xskill.team.client.state import load_client_state

    state_path = get_team_client_state_path()
    if not state_path.is_file():
        from xskill.runtime import read_status
        status = read_status()
        if status.get("running"):
            print(f"本机看板: http://127.0.0.1:{status.get('port')}/")
            return 0
        print("error: 未连接 team server，本机也没有运行中的 serve。\n"
              "  先跑：xskill connect <host:port> --token <t> --name <你的名字>",
              file=sys.stderr)
        return 1
    state = load_client_state(state_path)
    server_base = state.server_url.rstrip("/")
    request = urllib.request.Request(
        f"{server_base}/api/v1/team/dashboard_link",
        method="POST",
        headers={
            "X-Xskill-Token": state.join_token,
            "X-Xskill-Client": state.client_id,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            link_info = json.loads(
                response.read().decode("utf-8", errors="strict")
            )
    except urllib.error.HTTPError as http_error:
        detail = http_error.read().decode("utf-8", "replace")[:300]
        if http_error.code == 404:
            print("error: server 版本过旧，不支持免密链接（需 ≥0.6.14），"
                  "请管理员先升级 server", file=sys.stderr)
        else:
            print(f"error: server 拒绝签发登录链接（HTTP {http_error.code}）: "
                  f"{detail}", file=sys.stderr)
        return 1
    except Exception as network_error:
        print(f"error: 连不上 server: {network_error}", file=sys.stderr)
        return 1
    print(f"身份: {link_info['user']}")
    print("免密登录链接（10 分钟内有效，仅可用一次）:")
    print(f"  {server_base}{link_info['path']}")
    print("  （打不开时：需 server 看板允许远程访问，见 dashboard.public 配置）")
    return 0


def cmd_stop(args) -> int:
    """停止并撤销 connect 常驻任务。"""
    from xskill.team.client.service import ServiceError, get_backend
    try:
        st = get_backend().stop()
    except ServiceError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    if getattr(args, "json", False):
        _print_connect_status(st, as_json=True)
        return 0
    if st.get("warning"):
        print(f"warning: {st['warning']}", file=sys.stderr)
    print("stopped.")
    return 0


def cmd_status(args) -> int:
    """汇报 connect 常驻任务状态。"""
    from xskill.team.client.service import ServiceError, get_backend
    try:
        st = get_backend().status()
    except ServiceError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    _print_connect_status(st, as_json=getattr(args, "json", False))
    return 0


def cmd_stats(args) -> int:
    """token/成本统计。直接读 registry(~/.xskill/registry.db)。

    模型分布的 unknown 兜底标签复用 config 的 ``dashboard.default_model``——与看板
    口径一致，让"没记到模型名"的存量轨迹在 stats 里也归到指定模型而非 unknown。
    经 ``dashboard_attribution_defaults`` 读取：只看 dashboard 段、不校验
    llm/embedding key，config.yaml 缺失则退 'unknown'，瘦客户端无 config 也能用。
    纯展示——不改库里真实值、不影响 canary（灰度走 runner 里另一条默认 unknown 的
    路径，与此互不串）。
    """
    import json as _json
    import threading
    from xskill.config import dashboard_attribution_defaults
    from xskill.pipeline.registry import model_share, usage_summary
    from xskill.runtime import read_status
    from xskill.usage import render_stats

    unknown_model = dashboard_attribution_defaults()["model"]

    def _emit() -> None:
        s = usage_summary()
        st = read_status()
        ms = model_share(unknown_label=unknown_model)
        if args.json:
            print(_json.dumps({"status": st, "cost": s, "models": ms},
                              ensure_ascii=False, indent=2))
        else:
            print(render_stats(s, status=st, models=ms))

    if args.watch and not args.json:
        refresh_waiter = threading.Event()
        try:
            while True:
                print("\033[2J\033[H", end="")  # 清屏 + 光标归位
                _emit()
                refresh_waiter.wait(2)
        except KeyboardInterrupt:
            return 0
    _emit()
    return 0


def _team_client_http_and_headers():
    """瘦客户端命令共用：读连接 state，返回 (httpx client, 鉴权头)。

    未 connect 过返回 (None, None)（调用方打印引导后退出）。
    """
    from xskill.config import get_team_client_state_path
    from xskill.team.client.state import load_client_state

    state_path = get_team_client_state_path()
    if not state_path.is_file():
        print("error: 未连接 team server。先跑：\n"
              "  xskill connect <host:port> --token <t> --name <你的名字>",
              file=sys.stderr)
        return None, None
    state = load_client_state(state_path)
    import httpx
    http = httpx.Client(base_url=state.server_url, timeout=60.0, trust_env=False)
    headers = {"X-Xskill-Token": state.join_token,
               "X-Xskill-Client": state.client_id,
               "X-Xskill-Version": __version__}
    return http, headers


def _write_search_output(text: str, *, to_stderr: bool = False) -> None:
    """仅为 search 子命令做终端编码安全写入，不改变其他 CLI 输出。"""
    stream = sys.stderr if to_stderr else sys.stdout
    encoding = getattr(stream, "encoding", None) or "utf-8"
    safe_text = text.encode(
        encoding, errors="backslashreplace",
    ).decode(encoding, errors="strict")
    stream.write(safe_text)
    if not safe_text.endswith("\n"):
        stream.write("\n")


def _render_search_results(installed: list[dict], query: str) -> None:
    """以实际安装结果渲染人读搜索输出，不重新探测本机生态。"""
    harness_names = {
        "claude_code": "Claude Code",
        "codex": "Codex",
        "opencode": "OpenCode",
        "openclaw": "OpenClaw",
        "ngagent": "NGAgent",
        "nga3": "CodeAgent3 / NGA3",
        "cursor": "Cursor",
        "trae": "Trae",
    }
    output_lines = [
        f"搜索：{query}",
        f"找到 {len(installed)} 个 skill",
        "=" * 64,
    ]
    successful_installations = 0
    for index, row in enumerate(installed, start=1):
        if index > 1:
            output_lines.append("-" * 64)
        output_lines.append(f"[{index}/{len(installed)}] {row['name']}")
        output_lines.append(f"ID：{row['skill_id']}")

        description = " ".join(str(row.get("description") or "").split())
        if len(description) > 180:
            description = f"{description[:177].rstrip()}..."
        output_lines.append(f"描述：{description or '（无描述）'}")

        source = str(row.get("source") or "")
        source_path = str(row.get("source_path") or "")
        path_parts = source_path.replace("\\", "/").split("/")
        if source == "repo":
            source_label = "XSkill 自蒸馏生成"
        elif source.startswith("上传者:"):
            source_label = f"{source}（用户上传）"
        elif len(path_parts) >= 2 and path_parts[0] == "user_skill_hub":
            source_label = f"用户上传（{path_parts[1]}）"
        else:
            source_label = "SkillHub"
        output_lines.append(f"来源: {source_label}")
        if source_path:
            output_lines.append(f"  {source_path}")

        match = row.get("match")
        match_parts: list[str] = []
        if isinstance(match, dict) and match.get("bm25_rank") is not None:
            match_parts.append(f"关键词排名 #{match['bm25_rank']}")
        if isinstance(match, dict) and match.get("semantic_rank") is not None:
            match_parts.append(f"语义排名 #{match['semantic_rank']}")
        if row.get("ux_avg") is not None:
            match_parts.append(f"ux {row['ux_avg']}")
        if match_parts:
            output_lines.append(f"匹配：{'    '.join(match_parts)}")

        installation_records = row.get("installations")
        if not isinstance(installation_records, list):
            installation_records = []
        successful_groups: dict[tuple[str, str], list[str]] = {}
        failed_records: list[dict] = []
        for record in installation_records:
            if not isinstance(record, dict):
                continue
            ecosystem = str(record.get("ecosystem") or "")
            harness_name = harness_names.get(ecosystem, ecosystem)
            if record.get("status") == "installed":
                group_key = (
                    str(record.get("target") or ""),
                    str(record.get("mode") or ""),
                )
                names = successful_groups.setdefault(group_key, [])
                if harness_name and harness_name not in names:
                    names.append(harness_name)
                successful_installations += 1
            elif record.get("status") == "failed":
                failed_record = dict(record)
                failed_record["harness_name"] = harness_name
                failed_records.append(failed_record)
        if successful_groups or failed_records:
            output_lines.append("已安装到：")
        for (target, mode), names in successful_groups.items():
            output_lines.append(f"  [成功] {' / '.join(names)} [{mode}]")
            output_lines.append(f"    {target}")
        for record in failed_records:
            error_text = " ".join(str(record.get("error") or "").split())
            output_lines.append(
                f"  [失败] {record['harness_name']} 安装失败"
            )
            output_lines.append(
                f"    目标：{record.get('target') or '（未知）'}"
            )
            output_lines.append(
                f"    原因：{error_text or '安装器未提供错误信息'}"
            )
    output_lines.append("=" * 64)
    output_lines.append(
        f"完成：{len(installed)} 个 skill，"
        f"{successful_installations} 条 harness 安装记录"
    )
    _write_search_output("\n".join(output_lines))


def _safe_search_http_error(response) -> dict:
    """只从已知结构化错误中提取安全字段，绝不回显原始响应。"""
    canonical_messages = {
        "SKILL_HUB_SOURCE_UNAVAILABLE": "SkillHub 数据源暂时不可用",
        "SKILL_HUB_SEARCH_FAILED": "服务器执行 SkillHub 搜索时发生异常",
    }
    try:
        response_payload = response.json()
    except (TypeError, ValueError) as parse_error:
        error_type = (
            "TypeError" if isinstance(parse_error, TypeError) else "ValueError"
        )
        logging.getLogger("xskill.cli").warning(
            "search error JSON parse failed http_status=%s error_type=%s",
            int(response.status_code), error_type,
        )
        response_payload = {}
    if not isinstance(response_payload, dict):
        response_payload = {}
    raw_code = response_payload.get("code")
    code = raw_code if raw_code in canonical_messages else "HTTP_ERROR"
    message = canonical_messages.get(
        code, "服务器未提供可安全展示的结构化错误信息",
    )
    raw_request_id = response_payload.get("request_id")
    response_headers = getattr(response, "headers", {})
    header_request_id = (
        response_headers.get("X-Request-ID")
        if hasattr(response_headers, "get") else None
    )
    request_id = None
    if (
        isinstance(raw_request_id, str)
        and re.fullmatch(r"search-[0-9a-f]{16}", raw_request_id) is not None
        and header_request_id == raw_request_id
    ):
        request_id = raw_request_id
    safe_error = {
        "http_status": int(response.status_code),
        "code": code,
        "message": message[:200],
        "request_id": request_id,
    }
    if isinstance(response_payload.get("retryable"), bool):
        safe_error["retryable"] = response_payload["retryable"]
    return safe_error


def cmd_search_hub(args, http=None, headers=None) -> int:
    """`xskill search <query>` —— 搜 server skillhub，命中的拉到本地滚动槽位。

    结果由 BM25 关键词与语义向量混合检索 skillhub 目录（含 user_skill_hub 上传件），
    与推荐画像无关；语义服务不可用时自动退化为 BM25。每个命中 skill 下载解包到
    ``~/.xskill/search_skills/<skill_id>/``、
    打 ``.xskill_search.json`` 标记、装进本机生态；本地最多保留 10 个槽位，
    按最近命中滚动淘汰。``http``/``headers`` 参数仅测试注入用。
    """
    import json as _json
    import httpx
    from xskill.config import XSKILL_HOME
    from xskill.team.client.search_slots import SearchSlots

    if http is None:
        http, headers = _team_client_http_and_headers()
        if http is None:
            return 1
    query = " ".join(args.terms).strip()
    try:
        resp = http.get("/api/v1/team/skill_hub/search",
                        params={"query": query, "limit": args.top_k},
                        headers=headers)
        if resp.status_code == 404:
            _write_search_output(
                "error: server 版本过旧，不支持 skillhub 搜索（需 ≥0.6.17），"
                "请管理员先升级 server",
                to_stderr=True,
            )
            return 1
        if resp.status_code != 200:
            safe_error = _safe_search_http_error(resp)
            if args.json:
                _write_search_output(_json.dumps(
                    {"error": safe_error}, ensure_ascii=True, indent=2,
                ))
            else:
                error_lines = [
                    f"error: 搜索失败 HTTP {safe_error['http_status']}",
                    f"  原因: {safe_error['message']}",
                ]
                if safe_error["request_id"]:
                    error_lines.append(
                        f"  错误编号: {safe_error['request_id']}"
                    )
                _write_search_output(
                    "\n".join(error_lines), to_stderr=True,
                )
            return 1
        results = resp.json().get("results", [])
        if not results:
            _write_search_output("skillhub 无匹配 skill")
            return 0
        slots = SearchSlots(xskill_home=XSKILL_HOME)
        installed = []
        for result in results:
            bundle = http.get(f"/api/v1/team/skill/{result['skill_id']}/bundle",
                              headers=headers)
            if bundle.status_code != 200:
                _write_search_output(
                    f"warning: 拉取 {result['skill_id']} 失败 "
                    f"HTTP {bundle.status_code}",
                    to_stderr=True,
                )
                continue
            slot_result = slots.install(
                result, bundle.content, query=query, return_details=True,
            )
            local_path = slot_result["cache_path"]
            # 原样透传 server 返回的所有字段，再补本机安装信息
            installed_entry = dict(result)
            installed_entry["name"] = result["display_name"]
            installed_entry["path"] = str(local_path)
            installed_entry["cache_path"] = str(local_path)
            installed_entry["installations"] = [
                dict(record) for record in slot_result["installations"]
            ]
            installed.append(installed_entry)
    except (httpx.HTTPError, OSError) as network_error:
        _write_search_output(
            f"error: 无法连接 team server（{type(network_error).__name__}），"
            "server 可能未响应，请检查网络或联系管理员",
            to_stderr=True,
        )
        return 1
    if args.json:
        _write_search_output(_json.dumps(
            installed, ensure_ascii=True, indent=2,
        ))
        return 0
    _render_search_results(installed, query)
    return 0


def cmd_upload(args, http=None, headers=None) -> int:
    """`xskill upload <dir>` —— 打包 skill 文件夹上传到 server 的 user skillhub。

    server 落盘到 ``<skillhub>/user_skill_hub/<用户目录>/<skill名>/``，之后
    团队成员可用 `xskill search` 搜到。``http``/``headers`` 仅测试注入用。
    """
    import io as _io
    import json as _json
    import zipfile as _zipfile
    import httpx
    from pathlib import Path
    from xskill.skill.frontmatter import FrontmatterError, parse_strict

    skill_dir = Path(args.path).expanduser().resolve()
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.is_file():
        print(f"error: {skill_dir} 下没有 SKILL.md，不是合法 skill 目录",
              file=sys.stderr)
        return 2
    try:
        frontmatter, _body = parse_strict(skill_md.read_text(encoding="utf-8"))
    except (FrontmatterError, UnicodeDecodeError) as bad_skill:
        print(f"error: SKILL.md 校验失败: {bad_skill}", file=sys.stderr)
        return 2

    buffer = _io.BytesIO()
    with _zipfile.ZipFile(buffer, "w", compression=_zipfile.ZIP_DEFLATED) as zf:
        for file_path in sorted(skill_dir.rglob("*")):
            rel_parts = file_path.relative_to(skill_dir).parts
            if any(part in (".git", "__pycache__") or part.startswith(".xskill_")
                   for part in rel_parts):
                continue
            if file_path.is_file():
                zf.write(file_path, file_path.relative_to(skill_dir).as_posix())
    payload = buffer.getvalue()
    if len(payload) > 20 * 1024 * 1024:
        print("error: 打包后超过 20MB，请清理 skill 目录里的大文件",
              file=sys.stderr)
        return 2

    if http is None:
        http, headers = _team_client_http_and_headers()
        if http is None:
            return 1
    archive_name = f"{frontmatter['name']}.zip"
    try:
        resp = http.post("/api/v1/team/skill_hub/upload",
                         files={"file": (archive_name, payload, "application/zip")},
                         headers=headers)
        if resp.status_code == 404:
            print("error: server 版本过旧，不支持 skill 上传（需 ≥0.6.17），"
                  "请管理员先升级 server", file=sys.stderr)
            return 1
        if resp.status_code != 200:
            print(f"error: 上传失败 HTTP {resp.status_code}: {resp.text[:300]}",
                  file=sys.stderr)
            return 1
        stored = resp.json()
    except (httpx.HTTPError, OSError) as network_error:
        print(f"error: 无法连接 team server（{type(network_error).__name__}: "
              f"{network_error}），server 可能未响应，请检查网络或联系管理员",
              file=sys.stderr)
        return 1
    if args.json:
        print(_json.dumps(stored, ensure_ascii=False, indent=2))
        return 0
    print(f"uploaded: {stored['display_name']}  ({stored['skill_id']})")
    print(f"  server 路径: {stored['stored_path']}")
    print("  团队成员现在可以: xskill search "
          f"{stored['display_name']}")
    return 0


def cmd_read(args, xskill) -> int:
    """`xskill read <PATH> --eco ngagent` —— 批量把 db 文件桥接入库。"""
    del xskill  # CLI handler signature compatibility.
    from xskill.pipeline.db_ingest import read_db_files
    try:
        summary = read_db_files(
            args.path,
            eco=args.eco,
            register=not args.no_register,
            recursive=args.recursive,
        )
    except (FileNotFoundError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    print(
        f"read: {len(summary['db_files'])} db 文件 → 桥接 {summary['bridged']} "
        f"条轨迹到 {summary['target_dir']}"
    )
    if not args.no_register:
        print("已注册为 watch_dir —— 启动 `xskill serve` 后将自动拆分入库。")
    return 0


def cmd_rebuild(args, _xskill) -> int:
    """`xskill rebuild [--force]` —— 用现有原始轨迹重跑蒸馏。

    默认：删除已拆 atom + index.pkl、轨迹状态翻回 discovered，让运行中的 watcher
    从头重拆重聚（删 atom 是真正触发重拆的动作——splitter 续接点取自 atom 文件，
    不读 DB offset）。``--force``：额外先清空 skill 仓、看板派生埋点和安装历史。

    换模型护栏：rebuild 的重跑是交给**正在运行的 daemon**，而 daemon 的模型是
    启动时缓存的（改 config 不重启不生效）。若 daemon 在跑且其模型 ≠ 当前 config
    模型 → 默认拒绝并提示先重启 serve，否则会静默用旧模型重生成（`--ignore-
    model-mismatch` 可强行用当前运行的模型重跑）。
    """
    from xskill.config import XSKILL_HOME
    from xskill.pipeline.registry import reset_trajectories
    from xskill.runtime import config_models, read_status

    # ── 换模型护栏（先于任何清仓/重置）──
    status = read_status()
    if status.get("running") and not args.ignore_model_mismatch:
        daemon_model = status.get("llm_model")
        config_model = config_models().get("llm_model")
        if daemon_model != config_model:
            print(
                f"✗ 运行中的 daemon 在用模型 {daemon_model!r}，但 config.yaml "
                f"现在是 {config_model!r}。",
                file=sys.stderr,
            )
            print(
                "  daemon 的模型是启动时缓存的——直接 rebuild 会用旧模型重生成。\n"
                "  换模型请先干净重启：停掉 serve（确认进程真退了）→ 重新 "
                "`xskill serve` → 再 rebuild。\n"
                "  确认就是要用当前运行的模型重跑，可加 --ignore-model-mismatch。",
                file=sys.stderr,
            )
            return 2

    if args.force:
        from xskill.config import get_skill_dir
        from xskill.pipeline.registry import clear_rebuild_derived_state
        from xskill.skill.repo import SkillRepo
        skill_count = SkillRepo(get_skill_dir()).wipe_all_skills()
        print(f"--force: 清空 skill 仓（删 {skill_count} 个 skill）")
        deleted_counts = clear_rebuild_derived_state()
        print(
            "--force: 清空看板派生数据（"
            f"recommendation_log={deleted_counts['recommendation_log']}, "
            f"atom_adoption={deleted_counts['atom_adoption']}, "
            f"canary_decision={deleted_counts['canary_decision']}, "
            f"skill_trigger_eval={deleted_counts['skill_trigger_eval']}）"
        )
        install_history_path = XSKILL_HOME / "install_history.jsonl"
        if install_history_path.is_file():
            install_history_path.unlink()
            print("--force: 删除安装历史 install_history.jsonl")
        else:
            print("--force: 安装历史为空")

    reset_trajectory_ids = reset_trajectories(eco=args.eco, traj_id=args.traj)
    print(
        f"rebuild: 重置 {len(reset_trajectory_ids)} 条轨迹"
        "（已删 atom + index.pkl，将从头重拆）"
    )

    from xskill.pipeline.cold_start import ColdStartSignal
    cold_start_signal = ColdStartSignal(XSKILL_HOME)
    cold_start_signal.create(reset_trajectory_ids)
    print(
        "cold-start: 已写入本批轨迹快照信号，watcher 会在这批轨迹处理完成后 flush "
        f"({cold_start_signal.file_path})"
    )

    if read_status().get("running"):
        print("watcher 运行中 —— 30s 内将自动重跑这些轨迹。")
    else:
        print("⚠ 未检测到运行中的 daemon —— 请 `xskill serve` 启动后才会重跑。")
    return 0


# ═══════════════════════════════════════════════════════════════
# argparse
# ═══════════════════════════════════════════════════════════════

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="xskill",
        description="xskill — distill reusable Skills from AI Agent trajectories",
    )
    # -v / --version 唯一从 xskill.__version__ 读取，而 __version__ 在
    # src/xskill/__init__.py 里只 import 自 setuptools_scm 写出的 _version.py
    # —— 即 git tag 是单一真源，不在任何代码里硬编。
    p.add_argument(
        "-v", "--version",
        action="version",
        version=f"xskill {__version__}",
    )
    p.add_argument("--debug", action="store_true", help="verbose logging")
    p.add_argument("--quiet", action="store_true", help="quiet mode")
    sub = p.add_subparsers(dest="command")

    p_distill = sub.add_parser(
        "distill",
        help="用算法内核离线消化指定目录中的轨迹并产出 Skills",
    )
    p_distill.add_argument(
        "--kernel", dest="kernel_id", required=True,
        help="要运行的算法内核 ID",
    )
    p_distill.add_argument(
        "--trajectory-dir", required=True,
        help="包含 traj_*.md 的轨迹目录",
    )
    p_distill.add_argument(
        "--output", required=True,
        help="本次运行的产物输出目录；目录必须不存在",
    )
    p_distill.add_argument(
        "--plugin-dir", default=None,
        help=(
            "kernel 根目录；省略时读取 config.kernel.plugin_dir，"
            "否则使用 ~/.xskill/kernels"
        ),
    )
    p_distill.add_argument("--json", action="store_true", help="只输出稳定 JSON")
    p_distill.add_argument(
        "--no-progress", action="store_true", help="关闭 tqdm 阶段进度",
    )

    p_serve = sub.add_parser("serve", help="Start daemon (FastAPI + watcher)")
    p_serve.add_argument("--host", default="0.0.0.0")
    p_serve.add_argument("--port", type=int, default=8000)
    p_serve.add_argument(
        "--home", type=str, default=None,
        help="[debug only] 把生态扫描的 home 指向此目录，只看该目录下的 "
             ".claude/projects/*.jsonl + 装 skill 到 .claude/skills/。"
             "必须同时 --debug。用于隔离调试 (e.g. /tmp/xskill-test-home)。",
    )
    p_serve.add_argument(
        "--server", action="store_true",
        help="team server 模式：收 client 上传轨迹、跑全部 agent、"
             "提供 /api/v1/team/* 同步接口。不加则 standalone（仅本机）。",
    )
    p_serve.add_argument(
        "--force", action="store_true",
        help="已有 daemon 在跑时强行接管（默认拒绝启动，防双 daemon 抢 registry）",
    )

    p_reg = sub.add_parser("registry", help="Manage watched directories")
    p_reg.add_argument("registry_action", choices=["add", "remove", "list"])
    p_reg.add_argument("path", nargs="?", type=str,
                       help="directory path (for add/remove)")
    p_reg.add_argument("--label", type=str, default="",
                       help="human-friendly label (for add)")

    p_search = sub.add_parser(
        "search",
        help="搜 team server 的 skillhub 并把命中 skill 拉到本地槽位",
    )
    p_search.add_argument(
        "terms", nargs="+", metavar="QUERY",
        help="搜索词（可多个，拼成一个 skillhub 查询）",
    )
    p_search.add_argument("--top-k", "-k", type=int, default=5,
                          help="返回条数（skillhub 搜索最多 10）")
    p_search.add_argument("--json", action="store_true", help="机读 JSON 输出")

    p_upload = sub.add_parser(
        "upload", help="打包一个 skill 文件夹上传到 team server 的 user skillhub",
    )
    p_upload.add_argument("path", type=str, help="包含 SKILL.md 的 skill 目录")
    p_upload.add_argument("--json", action="store_true", help="机读 JSON 输出")

    p_init = sub.add_parser(
        "init",
        help="一站式引导：装 xskill 使用指南 skill 到各 agent + 连上 team server",
    )
    p_init.add_argument("address", nargs="?", default=None,
                        help="server 地址 host:port（交互模式留空会询问）")
    p_init.add_argument("--token", default=None,
                        help="join token（server 启动时打印；交互模式留空会询问）")
    p_init.add_argument("--name", default=None, metavar="EMPLOYEE_ID",
                        help="工号/用户 ID（推荐填，跨设备保持身份一致）")
    p_init.add_argument("--label", default="",
                        help="本 client 可读标签（默认主机名）")
    p_init.add_argument("--use-proxy", action="store_true",
                        help="经系统/环境代理连 server（默认直连，绕开公司 SWG 代理）")
    p_init.add_argument("--foreground", action="store_true",
                        help="前台阻塞跑守护循环（默认交给操作系统后台常驻）")
    p_init.add_argument("--no-auto-update", action="store_true", dest="no_auto_update",
                        help="禁用自动更新检查")
    p_init.add_argument("--skills-only", action="store_true", dest="skills_only",
                        help="只装 xskill skill，不配置连接")
    p_init.add_argument("--no-skill", action="store_true", dest="no_skill",
                        help="只配置连接，不装 xskill skill")
    p_init.add_argument("--force", action="store_true",
                        help="已有常驻连接时停掉并重新配置")
    p_init.add_argument("-y", "--yes", action="store_true",
                        help="非交互：缺必填项直接报错，不询问")
    p_init.add_argument("--target-root", default=None,
                        help="[测试/隔离] 安装与探测的 HOME 根（默认真实 HOME）")

    p_conn = sub.add_parser(
        "connect", help="Join a team server as a thin client",
    )
    p_conn.add_argument(
        "address", nargs="?", default=None,
        help="server 地址 host:port。省略则复用已存连接（~/.xskill/team_client.json）。",
    )
    p_conn.add_argument("--token", default=None,
                        help="join token（server 启动 `xskill serve --server` 时打印）")
    p_conn.add_argument("--label", default="",
                        help="本 client 的可读标签（默认主机名）")
    p_conn.add_argument(
        "--use-proxy", action="store_true",
        help="经系统/环境代理连 server（默认直连，绕开公司 SWG 代理）。"
             "仅当本机唯一出网路径是代理、且代理能到 server 时才需要。",
    )
    p_conn.add_argument(
        "--name", default=None, metavar="EMPLOYEE_ID",
        help="工号 / 用户 ID（推荐必填）。server 用它派生确定性 client_id——"
             "同一工号在不同设备或重装后身份保持一致，推荐算法也能跨设备积累。"
             "server 若设置了 allow_anonymous: false，则不带 --name 会被拒绝（403）。",
    )
    p_conn.add_argument(
        "--foreground", action="store_true",
        help="前台阻塞运行守护循环（默认交给操作系统守护设施后台常驻）。"
             "常驻任务内部 execute 的就是这个形态；调试时也可手动用。",
    )
    p_conn.add_argument(
        "--no-auto-update", action="store_true", dest="no_auto_update",
        help="禁用自动更新检查（默认每小时查一次 PyPI，有新版则升级重启）。",
    )

    p_start = sub.add_parser(
        "start", help="把 connect 装成后台常驻（开机自启 + 崩溃自愈）",
    )
    p_start.add_argument("--json", action="store_true", help="机读 JSON 输出")

    p_stop = sub.add_parser("stop", help="停止并撤销 connect 常驻任务")
    p_stop.add_argument("--json", action="store_true", help="机读 JSON 输出")

    p_status = sub.add_parser("status", help="查看 connect 常驻任务状态")

    p_update = sub.add_parser("update", help="立即检查 PyPI 新版并升级（有新版则重启）")
    p_update.add_argument(
        "--use-proxy", action="store_true",
        help="server wheel 回退经系统/环境代理拉取（默认直连，绕开公司 SWG 代理）。",
    )
    p_status.add_argument("--json", action="store_true", help="机读 JSON 输出")

    sub.add_parser(
        "dashboard",
        help="打印免密登录链接，点击即以自己的身份进入 server 看板",
    )

    p_stats = sub.add_parser(
        "stats", help="Show token usage & estimated cost (Issue #43)",
    )
    p_stats.add_argument("--json", action="store_true", help="机读 JSON 输出")
    p_stats.add_argument("--watch", action="store_true",
                         help="htop 式整屏刷新（每 2s）")

    p_read = sub.add_parser(
        "read", help="批量从指定位置读取 db 文件并入库（ngagent/opencode）",
    )
    p_read.add_argument("path", type=str,
                        help="db 文件，或包含 db 文件的目录")
    p_read.add_argument("--eco", default="ngagent",
                        choices=sorted(SQLITE_SPEC_BY_ECO),
                        help="db 所属生态（默认 ngagent）")
    p_read.add_argument("--recursive", "-r", action="store_true",
                        help="目录模式下递归查找 *.db")
    p_read.add_argument("--no-register", action="store_true",
                        help="只桥接不注册 watch_dir（一般不用）")

    p_rebuild = sub.add_parser(
        "rebuild", help="用现有原始轨迹重跑蒸馏（换强模型重生成 skill）",
    )
    p_rebuild.add_argument(
        "--force", action="store_true",
        help="先清空 skill 仓 + 已拆原子再全量重跑（删除重建）",
    )
    p_rebuild.add_argument("--eco", default=None,
                           help="只重跑某生态的轨迹（默认全部）")
    p_rebuild.add_argument("--traj", default=None,
                           help="只重跑某条轨迹 id（调试用）")
    p_rebuild.add_argument(
        "--ignore-model-mismatch", action="store_true",
        help="跳过'daemon 模型≠config 模型'护栏，用当前运行的模型重跑",
    )

    return p


def _setup_logging(debug: bool, quiet: bool, *, command: str = "") -> None:
    """配置 logging。

    - ``serve``：用 ``log_setup.configure_logging`` 拆 component 到独立文件
      （~/.xskill/logs/xskill.<component>.log）+ stdout 简略输出，方便
      tail -f 单独跟某条流水。
    - 其他短命令（``search`` / ``registry``）：保留旧 basicConfig，stdout
      only，不创建文件 handler——这些命令几秒就退，没必要落日志。
    """
    if command in ("serve", "connect"):
        # serve / connect 都是长跑守护，用 file-split 模式落文件日志
        from xskill.config import get_logs_dir
        from xskill.utils.logging import configure_logging
        configure_logging(get_logs_dir(), debug=debug, quiet=quiet, stdout=True)
        return

    # 老 basicConfig 路径（短命令）
    if debug:
        level, fmt = logging.DEBUG, "%(asctime)s [%(name)s] %(levelname)s %(message)s"
    elif quiet:
        level, fmt = logging.WARNING, "%(message)s"
    else:
        level, fmt = logging.INFO, "%(asctime)s [%(name)s] %(message)s"
    logging.basicConfig(level=level, format=fmt, datefmt="%H:%M:%S")
    for noisy in ("httpx", "httpcore", "openai", "xskill.utils.llm", "agno"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


# ═══════════════════════════════════════════════════════════════
# main
# ═══════════════════════════════════════════════════════════════

def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        return 1

    set_overrides(debug=args.debug, quiet=args.quiet)
    _setup_logging(args.debug, args.quiet, command=args.command)

    if args.command == "registry" and args.registry_action in ("add", "remove"):
        if not args.path:
            parser.error(f"path is required for 'registry {args.registry_action}'")

    # init 一站式引导：装 skill + connect，同样是瘦客户端侧，不碰 config.yaml。
    if args.command == "init":
        return cmd_init(args)

    # connect 是瘦客户端：不读 config.yaml / 不需要 llm.api_key / 不构造 XSkill 门面
    if args.command == "connect":
        return cmd_connect(args)

    # start/stop/status 管理 connect 常驻任务——同样是瘦客户端侧，不碰 config.yaml。
    if args.command == "start":
        return cmd_start(args)
    if args.command == "stop":
        return cmd_stop(args)
    if args.command == "status":
        return cmd_status(args)
    if args.command == "update":
        return cmd_update(args)
    if args.command == "dashboard":
        return cmd_dashboard(args)

    # skillhub 搜索/上传是瘦客户端侧（走 team server），不碰 config.yaml。
    if args.command == "search":
        return cmd_search_hub(args)
    if args.command == "upload":
        return cmd_upload(args)

    # stats 只读 registry，不需要 config.yaml / llm.api_key / facade
    if args.command == "stats":
        return cmd_stats(args)

    # distill 是离线隔离命令：只读 kernel 选择路径，不校验平台 LLM/embedding
    # key，也不构造 XSkill facade，更不要求先启动 serve。
    if args.command == "distill":
        return cmd_distill(args)

    # read / rebuild 只动 registry + 文件，不需要 llm.api_key / facade——
    # 重跑由运行中的 watcher 完成，本命令只做"重置/桥接"。
    if args.command == "read":
        return cmd_read(args, None)
    if args.command == "rebuild":
        return cmd_rebuild(args, None)

    # team 客户端的 `registry list`：本机是 client（有 team_client.json）且没有
    # standalone 数据（watch_dirs 为空）时，改走现算视图。放在 config/facade
    # 之前——纯客户端没 config.yaml 也能直接看。standalone/server 机（watch_dirs
    # 非空）走原路，不受影响（哪怕本机也存了 team_client.json）。
    if args.command == "registry" and args.registry_action == "list":
        from xskill.config import get_team_client_state_path
        if (get_team_client_state_path().is_file()
                and _standalone_watch_dir_count() == 0):
            return cmd_registry_list_client()

    # 首次运行 auto-init：serve / registry 都需要 config.yaml。
    # 不存在就写一份模板并要求用户填 key 后重跑——比直接抛 traceback 友好。
    from xskill.config import CONFIG_PATH, ensure_config_exists
    if not ensure_config_exists():
        print(
            f"\n  Created a config template at {CONFIG_PATH}\n"
            f"  Edit it — fill in llm.api_key and embedding.api_key — "
            f"then run `xskill {args.command}` again.\n",
            file=sys.stderr,
        )
        return 0

    from xskill import XSkill
    xskill = XSkill()

    handler = {
        "serve":    cmd_serve,
        "registry": cmd_registry,
    }.get(args.command)
    return handler(args, xskill) if handler else (parser.print_help() or 1)


if __name__ == "__main__":
    sys.exit(main() or 0)
