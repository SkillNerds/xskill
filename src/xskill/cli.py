#!/usr/bin/env python3
"""
cli.py — xskill 紧凑 CLI
═══════════════════════════════════════════════════════
仅 5 个子命令（无 --no-watch / --no-ui / --skill-dir / --llm-* 这类散 flag）：
    xskill serve [--host] [--port]
    xskill registry add|remove|list <path>
    xskill search <关键词...> [--top-k]
    xskill download <skill-id>

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
    """一站式引导：把 xskill 使用指南 skill 装进各 agent 生态 + 连上 team server。

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
        if not (skill_source / "SKILL.md").is_file():
            print(f"warning: 捆绑的 xskill skill 缺失（{skill_source}），跳过装 skill",
                  file=sys.stderr)
        else:
            installed_ecosystems = []
            for detection in detect_known_ecosystems(home_root=target_root):
                install_fn = installer_by_eco.get(detection["ecosystem"])
                if install_fn is None:
                    continue
                try:
                    install_fn(skill_source, target_root=target_root, side="main")
                    installed_ecosystems.append(detection["ecosystem"])
                except Exception as install_error:  # noqa: BLE001
                    print(f"warning: 装到 {detection['ecosystem']} 失败：{install_error}",
                          file=sys.stderr)
            if installed_ecosystems:
                print(f"已把 xskill 使用指南装进 {'/'.join(installed_ecosystems)} 的 "
                      f"skill 目录，在对应 agent 里可直接 /xskill 查用法。")
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

    from xskill.config import (
        XSKILL_HOME, get_team_server_state_path, resolve_team_client_skill_dir,
    )
    client_skill = resolve_team_client_skill_dir(XSKILL_HOME / "skill")
    if get_team_server_state_path().is_file() and client_skill != XSKILL_HOME / "skill":
        print(
            "warning: 本机已是 team server。client 工作副本放到 "
            f"{client_skill}，不会清理 server 自有仓 {XSKILL_HOME / 'skill'}",
            file=sys.stderr,
        )

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
        XSKILL_HOME,
        get_team_client_cursor_path,
        get_team_client_history_path,
        resolve_team_client_skill_dir,
    )
    from xskill.team.client.daemon import TeamClient

    http = httpx.Client(base_url=state.server_url, timeout=30.0,
                        trust_env=use_proxy)
    client = TeamClient(
        state=state, http=http,
        skill_dir=resolve_team_client_skill_dir(XSKILL_HOME / "skill"),
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


def _emit_skillhub_search_meta_warnings(
    search_meta: dict, *, has_results: bool,
) -> None:
    """根据 skill_hub/search 的 meta 向 stderr 提示空库 / BM25 降级。"""
    if search_meta.get("corpus_empty") and not has_results:
        _write_search_output(
            "warning: skillhub 暂无可搜索的 skill（库为空）",
            to_stderr=True,
        )
    if search_meta.get("degraded_to_bm25"):
        _write_search_output(
            "warning: 语义检索不可用，已降级为 BM25 关键词搜索",
            to_stderr=True,
        )


_DOWNLOAD_AGENT_OPTIONS = (
    ("claude_code", "claude-code", "Claude Code"),
    ("codex", "codex", "Codex"),
    ("nga3", "nga3", "CodeAgent3 / NGA3"),
    ("opencode", "opencode", "OpenCode"),
    ("ngagent", "ngagent", "NGAgent"),
    ("openclaw", "openclaw", "OpenClaw"),
    ("cursor", "cursor", "Cursor"),
    ("trae", "trae", "Trae"),
)
_DOWNLOAD_AGENT_ALIASES = {
    "claude-code": "claude_code",
    "claude_code": "claude_code",
    "cc": "claude_code",
    "codex": "codex",
    "nga3": "nga3",
    "codeagent3": "nga3",
    "code-agent3": "nga3",
    "opencode": "opencode",
    "open-code": "opencode",
    "ngagent": "ngagent",
    "ng-agent": "ngagent",
    "openclaw": "openclaw",
    "open-claw": "openclaw",
    "cursor": "cursor",
    "trae": "trae",
}
_DOWNLOAD_AGENT_CHOICES = tuple(_DOWNLOAD_AGENT_ALIASES)
_DOWNLOAD_AGENT_LABELS = {
    ecosystem: label
    for ecosystem, _cli_name, label in _DOWNLOAD_AGENT_OPTIONS
}


def _source_label(row: dict) -> str:
    source = str(row.get("source") or "")
    source_path = str(row.get("source_path") or "")
    path_parts = source_path.replace("\\", "/").split("/")
    if source == "repo":
        return "XSkill 自蒸馏生成"
    if source.startswith("上传者:"):
        return f"{source}（用户上传）"
    if len(path_parts) >= 2 and path_parts[0] == "user_skill_hub":
        return f"用户上传（{path_parts[1]}）"
    return "SkillHub"


def _match_summary(row: dict) -> str:
    match = row.get("match")
    match_parts: list[str] = []
    if isinstance(match, dict) and match.get("bm25_rank") is not None:
        match_parts.append(f"关键词排名 #{match['bm25_rank']}")
    if isinstance(match, dict) and match.get("semantic_rank") is not None:
        match_parts.append(f"语义排名 #{match['semantic_rank']}")
    if row.get("ux_avg") is not None:
        match_parts.append(f"ux {row['ux_avg']}")
    return "    ".join(match_parts)


def _render_search_metadata_results(results: list[dict], query: str) -> None:
    """精简渲染只读搜索结果，绝不展示本机路径或 harness 安装信息。"""
    output_lines = [
        f"搜索：{query}",
        f"找到 {len(results)} 个 skill",
        "=" * 64,
    ]
    for index, row in enumerate(results, start=1):
        if index > 1:
            output_lines.append("-" * 64)
        display_name = row.get("display_name") or row.get("name")
        output_lines.append(
            f"[{index}/{len(results)}] {display_name or row['skill_id']}"
        )
        output_lines.append(f"ID：{row['skill_id']}")
        description = " ".join(str(row.get("description") or "").split())
        if len(description) > 180:
            description = f"{description[:177].rstrip()}..."
        output_lines.append(f"描述：{description or '（无描述）'}")
        output_lines.append(f"来源：{_source_label(row)}")
        match_summary = _match_summary(row)
        if match_summary:
            output_lines.append(f"匹配：{match_summary}")
        output_lines.append(f"下载：xskill download {row['skill_id']}")
    output_lines.append("=" * 64)
    _write_search_output("\n".join(output_lines))


def _render_search_results(
    results: list[dict], query: str, *, heading: str = "搜索",
) -> None:
    """渲染已经下载/安装的结果，不重新探测本机生态。"""
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
        f"{heading}：{query}",
        f"找到 {len(results)} 个 skill",
        "=" * 64,
    ]
    successful_installations = 0
    for index, row in enumerate(results, start=1):
        if index > 1:
            output_lines.append("-" * 64)
        display_name = row.get("display_name") or row.get("name")
        output_lines.append(
            f"[{index}/{len(results)}] {display_name or row['skill_id']}"
        )
        output_lines.append(f"ID：{row['skill_id']}")

        description = " ".join(str(row.get("description") or "").split())
        if len(description) > 180:
            description = f"{description[:177].rstrip()}..."
        output_lines.append(f"描述：{description or '（无描述）'}")

        source_path = str(row.get("source_path") or "")
        output_lines.append(f"来源: {_source_label(row)}")
        if source_path:
            output_lines.append(f"  {source_path}")

        match_summary = _match_summary(row)
        if match_summary:
            output_lines.append(f"匹配：{match_summary}")

        installation_records = row.get("installations")
        if not isinstance(installation_records, list):
            installation_records = []
        local_path = row.get("path")
        if local_path:
            output_lines.append(f"本地：{local_path}")
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
        f"完成：{len(results)} 个 skill，"
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


def _normalize_download_agents(raw_agents: list[str]) -> list[str]:
    normalized: list[str] = []
    for raw_agent in raw_agents:
        canonical = _DOWNLOAD_AGENT_ALIASES.get(
            str(raw_agent).strip().lower(),
        )
        if canonical is None:
            raise ValueError(f"unsupported agent: {raw_agent}")
        if canonical not in normalized:
            normalized.append(canonical)
    return normalized


def _detected_download_agents() -> list[str]:
    from pathlib import Path
    from xskill.ecosystems import detect_known_ecosystems

    detected = {
        str(record.get("ecosystem") or "")
        for record in detect_known_ecosystems(home_root=Path.home())
        if isinstance(record, dict)
    }
    return [
        ecosystem for ecosystem, _cli_name, _label
        in _DOWNLOAD_AGENT_OPTIONS
        if ecosystem in detected
    ]


def _stdin_is_interactive() -> bool:
    return bool(getattr(sys.stdin, "isatty", lambda: False)())


def _prompt_download_agents(candidates: list[str]) -> list[str] | None:
    """用无依赖的编号多选询问 harness；None 表示用户取消。"""
    lines = ["选择要安装到的 harness（可多选）："]
    for index, ecosystem in enumerate(candidates, start=1):
        lines.append(
            f"  {index}. {_DOWNLOAD_AGENT_LABELS[ecosystem]}"
        )
    lines.append("输入编号（逗号/空格分隔）；直接回车全选；q 取消。")
    _write_search_output("\n".join(lines), to_stderr=True)
    while True:
        sys.stderr.write("> ")
        sys.stderr.flush()
        raw_selection = sys.stdin.readline()
        if raw_selection == "":
            return None
        selection = raw_selection.strip().lower()
        if selection in {"q", "quit", "cancel"}:
            return None
        if not selection:
            return list(candidates)
        tokens = [
            token for token in re.split(r"[\s,]+", selection) if token
        ]
        try:
            indexes = [int(token) for token in tokens]
        except ValueError:
            indexes = []
        if (
            indexes and all(1 <= index <= len(candidates) for index in indexes)
        ):
            return list(dict.fromkeys(
                candidates[index - 1] for index in indexes
            ))
        _write_search_output(
            f"请输入 1-{len(candidates)} 的编号，或 q 取消。",
            to_stderr=True,
        )


def _select_download_agents(args) -> tuple[list[str] | None, int]:
    """解析 download 的自动/交互选择；返回 (agents, rc)。"""
    try:
        explicit_agents = _normalize_download_agents(
            list(getattr(args, "agent", None) or [])
        )
    except ValueError as agent_error:
        _write_search_output(
            f"error: {agent_error}", to_stderr=True,
        )
        return None, 2

    if bool(getattr(args, "yes", False)):
        selected = explicit_agents or _detected_download_agents()
        if not selected:
            _write_search_output(
                "warning: 未检测到可安装的 harness；skill 将仅持久下载，"
                "也可用 --agent <name> -y 显式安装。",
                to_stderr=True,
            )
        return selected, 0

    if not _stdin_is_interactive():
        _write_search_output(
            "error: 当前不是交互终端；请使用可重复的 "
            "--agent <name> 并加 -y，或仅加 -y 自动选择已检测 harness。",
            to_stderr=True,
        )
        return None, 2
    candidates = explicit_agents or _detected_download_agents()
    if not candidates:
        _write_search_output(
            "warning: 未检测到可安装的 harness；skill 将仅持久下载，"
            "也可用 --agent <name> -y 显式安装。",
            to_stderr=True,
        )
        return [], 0
    selected = _prompt_download_agents(candidates)
    if selected is None:
        _write_search_output("已取消下载。", to_stderr=True)
        return None, 0
    return selected, 0


def cmd_search_hub(args, http=None, headers=None) -> int:
    """`xskill search <query>` —— 默认只搜元信息。

    结果由 BM25 关键词与语义向量混合检索自产 skill 与 SkillHub；语义服务
    不可用时退化为 BM25。下载由 ``xskill download <skill-id>`` 显式执行。
    ``--download`` 兼容旧 search：把全部命中放进 10 槽 LRU 并自动安装到
    已检测 harness。
    ``http``/``headers`` 参数仅测试注入用。
    """
    import json as _json
    import httpx

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
        payload = resp.json()
        results = payload.get("results", [])
        search_meta = payload.get("meta") or {}
        _emit_skillhub_search_meta_warnings(search_meta, has_results=bool(results))
        if not results:
            if args.json:
                _write_search_output("[]")
            else:
                if search_meta.get("corpus_empty"):
                    _write_search_output("skillhub 暂无可搜索的 skill")
                else:
                    _write_search_output("skillhub 无匹配 skill")
            return 0
        if getattr(args, "download", False):
            from xskill.config import XSKILL_HOME
            from xskill.team.client.search_slots import SearchSlots

            slots = SearchSlots(xskill_home=XSKILL_HOME)
            installed = []
            for result in results:
                bundle = http.get(
                    f"/api/v1/team/skill/{result['skill_id']}/bundle",
                    headers=headers,
                )
                if bundle.status_code != 200:
                    _write_search_output(
                        f"warning: 拉取 {result['skill_id']} 失败 "
                        f"HTTP {bundle.status_code}",
                        to_stderr=True,
                    )
                    continue
                slot_result = slots.install(
                    result, bundle.content, query=query,
                    return_details=True,
                )
                local_path = slot_result["cache_path"]
                installed_entry = dict(result)
                installed_entry["name"] = (
                    result.get("display_name") or result["skill_id"]
                )
                installed_entry["path"] = str(local_path)
                installed_entry["cache_path"] = str(local_path)
                installed_entry["installations"] = [
                    dict(record)
                    for record in slot_result["installations"]
                ]
                installed.append(installed_entry)
    except (httpx.HTTPError, OSError) as network_error:
        _write_search_output(
            f"error: 无法连接 team server（{type(network_error).__name__}），"
            "server 可能未响应，请检查网络或联系管理员",
            to_stderr=True,
        )
        return 1
    output_results = (
        installed if getattr(args, "download", False) else results
    )
    if args.json:
        _write_search_output(_json.dumps(
            output_results, ensure_ascii=True, indent=2,
        ))
        return 0
    if getattr(args, "download", False):
        _render_search_results(output_results, query)
    else:
        _render_search_metadata_results(output_results, query)
    return 0


def cmd_search(args) -> int:
    """`xskill search` 部署模式自适应入口（#201）。

    解析顺序：``--team`` / ``--local`` 显式覆盖 > team client 状态文件
    （已 connect → SkillHub 路径）> 本地技能库路径。standalone 用户不再
    被 "未连接 team server" 直接挡在门外（#46 的前向修复）。
    """
    if getattr(args, "team", False):
        return cmd_search_hub(args)
    if getattr(args, "local", False):
        return _cmd_search_local(args)
    from xskill.runtime import role
    if role() == "client":
        return cmd_search_hub(args)
    return _cmd_search_local(args)


def _cmd_search_local(args, *, post=None) -> int:
    """本地技能库搜索：优先本机 daemon 语义检索，不可用时回退 BM25。

    降级必须对用户可见（stderr 警告），不静默空结果也不 traceback——
    与 server 端 skill_hub「语义不可用退化 BM25」同一契约。
    ``post`` 参数仅测试注入用。
    """
    import json as _json

    from xskill.runtime import read_status

    query = " ".join(args.terms).strip()
    if getattr(args, "download", False):
        _write_search_output(
            "warning: --download 仅 team 模式有效，本地技能已在本机，忽略",
            to_stderr=True,
        )

    st = read_status()
    hits: list[dict] = []
    semantic_ok = False
    if st.get("running") and st.get("port"):
        if post is None:
            import httpx

            def post(url, **kw):
                return httpx.post(url, trust_env=False, timeout=15.0, **kw)
        try:
            resp = post(
                f"http://127.0.0.1:{st['port']}/api/v1/skills/search",
                json={"query": query, "top_k": args.top_k},
            )
            if resp.status_code == 200:
                data = resp.json()
                # 兼容历史空索引 dict 包络（#46 Bug A 的形状）：统一成 list
                if isinstance(data, dict):
                    data = data.get("results", [])
                if isinstance(data, list):
                    semantic_ok = True
                    hits = [h for h in data if isinstance(h, dict)]
        except Exception as search_error:  # noqa: BLE001 — 降级路径必须兜住
            logger.debug("local semantic search failed: %s", search_error)

    used_bm25 = False
    if not hits:
        if not st.get("running"):
            _write_search_output(
                "⚠ 本地 daemon 未运行（先 `xskill serve`），"
                "本次搜索回退 BM25 关键词检索",
                to_stderr=True,
            )
        elif not semantic_ok:
            _write_search_output(
                "⚠ 本地语义搜索不可用（embedding 未配置或索引缺失），"
                "本次搜索回退 BM25 关键词检索",
                to_stderr=True,
            )
        bm25_hits = _local_bm25_hits(query, top_k=args.top_k)
        if bm25_hits and semantic_ok:
            _write_search_output(
                "⚠ 语义索引无命中（索引未建或 embedding 未配置），"
                "以下为 BM25 关键词匹配结果",
                to_stderr=True,
            )
        hits = bm25_hits
        used_bm25 = True

    if getattr(args, "json", False):
        _write_search_output(_json.dumps(hits, ensure_ascii=True, indent=2))
        return 0
    if not hits:
        _write_search_output("本地技能库无匹配 skill")
        return 0
    _render_local_search_results(hits, query, keyword_only=used_bm25)
    return 0


def _local_bm25_hits(query: str, *, top_k: int) -> list[dict]:
    """对本地 skill 的 name+description+tags 做 BM25 关键词检索。

    分词复用 skillhub 的 ``_tokenize``（ASCII 切词 + 中文 bigram），打分
    复用 skillhub 的 Lucene 风格公式 ``log(1 + (N-df+0.5)/(df+0.5))``
    （k1=1.2, b=0.75）——IDF 恒正，本地小语料（几个 skill）不会像
    BM25Okapi 那样把全部分数压成 0。"""
    import math

    from xskill.config import XSKILL_HOME, get_skill_dir
    from xskill.recommend.skillhub import BM25_B, BM25_K1, _tokenize
    from xskill.skill.skill import _load_skill

    try:
        skill_dir = get_skill_dir()
    except FileNotFoundError:
        # 首装还没有 config.yaml：关键词回退不应依赖配置，用默认 skill 目录
        skill_dir = XSKILL_HOME / "skill"
    entries: list[dict] = []
    if skill_dir.exists():
        for d in sorted(skill_dir.iterdir()):
            if not d.is_dir() or d.name.startswith("."):
                continue
            fm, _body, _path = _load_skill(d)
            if not fm:
                continue
            meta = fm.get("metadata", {}) or {}
            description = (fm.get("description") or "").strip()
            tags = [str(t) for t in (meta.get("tags", []) or [])]
            entries.append({
                "skill_name": d.name,
                "description": description,
                "tags": tags,
                "version": int(meta.get("version", 0) or 0),
                "text": " ".join([d.name, description, *tags]),
            })

    query_tokens = set(_tokenize(query))
    if not entries or not query_tokens:
        return []
    corpus = [_tokenize(e["text"]) for e in entries]
    doc_count = len(corpus)
    avg_doc_len = sum(len(doc) for doc in corpus) / doc_count
    if avg_doc_len == 0:
        return []
    doc_freq = {
        token: sum(1 for doc in corpus if token in doc)
        for token in query_tokens
    }
    scores: list[float] = []
    for doc in corpus:
        score = 0.0
        for token in query_tokens:
            tf = doc.count(token)
            if tf == 0 or doc_freq[token] == 0:
                continue
            idf = math.log(
                1 + (doc_count - doc_freq[token] + 0.5)
                / (doc_freq[token] + 0.5)
            )
            denominator = tf + BM25_K1 * (
                1 - BM25_B + BM25_B * len(doc) / avg_doc_len
            )
            score += idf * tf * (BM25_K1 + 1) / denominator
        scores.append(score)
    ranked = sorted(
        zip(entries, scores), key=lambda pair: pair[1], reverse=True,
    )
    out: list[dict] = []
    for entry, score in ranked[:top_k]:
        if score <= 0:
            continue
        hit = {k: v for k, v in entry.items() if k != "text"}
        hit["bm25_score"] = float(score)
        out.append(hit)
    return out


def _render_local_search_results(
    hits: list[dict], query: str, *, keyword_only: bool,
) -> None:
    """渲染本地技能库搜索结果（本机 skill，无下载步骤）。"""
    mode_label = "BM25 关键词" if keyword_only else "语义"
    output_lines = [
        f"搜索：{query}（本地技能库，{mode_label}）",
        f"找到 {len(hits)} 个 skill",
        "=" * 64,
    ]
    for index, row in enumerate(hits, start=1):
        if index > 1:
            output_lines.append("-" * 64)
        output_lines.append(
            f"[{index}/{len(hits)}] {row.get('skill_name') or '(unnamed)'}"
        )
        description = " ".join(str(row.get("description") or "").split())
        if len(description) > 180:
            description = f"{description[:177].rstrip()}..."
        output_lines.append(f"描述：{description or '（无描述）'}")
        if row.get("similarity") is not None:
            output_lines.append(f"匹配：语义相似度 {row['similarity']:.4f}")
        elif row.get("bm25_score") is not None:
            output_lines.append(f"匹配：BM25 {row['bm25_score']:.2f}")
    output_lines.append("=" * 64)
    _write_search_output("\n".join(output_lines))


def cmd_download(args, http=None, headers=None) -> int:
    """`xskill download <skill-id>` —— 显式下载并持久安装一个搜索结果。"""
    import json as _json
    import zipfile
    import httpx
    from xskill.config import XSKILL_HOME
    from xskill.team.client.search_slots import DownloadedSkills

    skill_id = str(args.skill_id).strip()
    if (
        not skill_id or skill_id in {".", ".."}
        or "/" in skill_id or "\\" in skill_id or "\x00" in skill_id
    ):
        _write_search_output("error: 非法 skill ID", to_stderr=True)
        return 2
    if http is None:
        http, headers = _team_client_http_and_headers()
        if http is None:
            return 1
    selected_agents, selection_rc = _select_download_agents(args)
    if selected_agents is None:
        return selection_rc
    try:
        metadata_response = http.get(
            f"/api/v1/team/skill_hub/entry/{skill_id}",
            headers=headers,
        )
        if metadata_response.status_code != 200:
            _write_search_output(
                f"error: 找不到可下载的 skill（HTTP "
                f"{metadata_response.status_code}）",
                to_stderr=True,
            )
            return 1
        result = metadata_response.json().get("result")
        if not isinstance(result, dict):
            _write_search_output(
                "error: server 返回了无效的 skill 元信息",
                to_stderr=True,
            )
            return 1
        bundle = http.get(
            f"/api/v1/team/skill/{skill_id}/bundle",
            headers=headers,
        )
        if bundle.status_code != 200:
            _write_search_output(
                f"error: 下载 skill 失败（HTTP {bundle.status_code}）",
                to_stderr=True,
            )
            return 1
        manager = DownloadedSkills(xskill_home=XSKILL_HOME)
        installed = manager.install(
            result, bundle.content, ecosystems=selected_agents,
            return_details=True,
        )
        output = dict(result)
        output["name"] = result.get("display_name") or skill_id
        output["path"] = str(installed["path"])
        output["installations"] = [
            dict(record) for record in installed["installations"]
        ]
    except (
        httpx.HTTPError, OSError, RuntimeError, ValueError, zipfile.BadZipFile,
    ) as download_error:
        _write_search_output(
            f"error: 下载失败（{type(download_error).__name__}）",
            to_stderr=True,
        )
        return 1
    if args.json:
        _write_search_output(_json.dumps(
            output, ensure_ascii=True, indent=2,
        ))
    else:
        _render_search_results([output], skill_id, heading="下载")
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


def _parse_sse_block(block: str) -> dict | None:
    import json as _json
    for line in block.splitlines():
        if line.startswith("data:"):
            payload = line[5:].strip()
            if not payload:
                continue
            try:
                parsed = _json.loads(payload)
            except _json.JSONDecodeError:
                return None
            if isinstance(parsed, dict):
                return parsed
    return None


def cmd_generate(args, http=None, headers=None) -> int:
    """`xskill generate "指令"` —— 在 team server 上即时生成或改写 skill。"""
    import httpx

    instruction = " ".join(args.instruction).strip()
    if not instruction:
        print("error: instruction 不能为空", file=sys.stderr)
        return 2
    names = [
        part.strip()
        for part in str(getattr(args, "name", "") or "").split(",")
        if part.strip()
    ]
    if http is None:
        http, headers = _team_client_http_and_headers()
        if http is None:
            return 1
    try:
        resp = http.post(
            "/api/v1/team/generate",
            json={"instruction": instruction, "names": names},
            headers=headers,
        )
        if resp.status_code == 404:
            print(
                "error: server 版本过旧，不支持 generate，请管理员先升级 server",
                file=sys.stderr,
            )
            return 1
        if resp.status_code != 200:
            print(
                f"error: generate 提交失败 HTTP {resp.status_code}: "
                f"{resp.text[:300]}",
                file=sys.stderr,
            )
            return 1
        job_id = resp.json().get("job_id")
        if not job_id:
            print("error: server 未返回 job_id", file=sys.stderr)
            return 1
        print(f"generate job {job_id}", flush=True)
        stream_timeout = httpx.Timeout(None)
        with httpx.Client(
            base_url=str(http.base_url),
            timeout=stream_timeout,
            trust_env=False,
        ) as stream_http:
            with stream_http.stream(
                "GET",
                f"/api/v1/team/generate/{job_id}/events",
                headers=headers,
            ) as stream:
                if stream.status_code != 200:
                    print(
                        f"error: 无法读取 generate 日志 HTTP {stream.status_code}",
                        file=sys.stderr,
                    )
                    return 1
                buffer = ""
                final = None
                for text in stream.iter_text():
                    buffer += text
                    while "\n\n" in buffer:
                        block, buffer = buffer.split("\n\n", 1)
                        event = _parse_sse_block(block)
                        if event is None:
                            continue
                        if event.get("type") == "log":
                            chunk = event.get("chunk") or ""
                            if chunk:
                                sys.stdout.write(chunk)
                                sys.stdout.flush()
                        elif event.get("type") == "ping":
                            status = event.get("status") or ""
                            if status == "queued":
                                print("仍在排队，等待席位…", flush=True)
                            else:
                                print("仍在执行…", flush=True)
                        elif event.get("type") == "done":
                            final = event
                if final is None and buffer.strip():
                    final = _parse_sse_block(buffer)
    except (httpx.HTTPError, OSError) as network_error:
        print(
            f"error: 无法连接 team server（{type(network_error).__name__}），"
            "server 可能未响应，请检查网络或联系管理员",
            file=sys.stderr,
        )
        return 1
    if not final:
        print("error: generate 结束但没有收到完成事件", file=sys.stderr)
        return 1
    if not final.get("ok"):
        err = final.get("error") or "generate 失败"
        print(f"error: {err}", file=sys.stderr)
        return 1
    skills = final.get("skill_names") or []
    pinned = final.get("pinned") or []
    if skills:
        print("generate 完成: " + "、".join(skills))
    else:
        print("generate 完成")
    if pinned:
        print("已钉到发起人推荐列表: " + "、".join(pinned))
    return 0


def _is_thin_team_client() -> bool:
    """已 connect 且本机没有 standalone/server 的 watch_dirs → 瘦客户端。"""
    from xskill.config import get_team_client_state_path
    return (
        get_team_client_state_path().is_file()
        and _standalone_watch_dir_count() == 0
    )


def _local_import_skill_dir():
    from xskill.config import CONFIG_PATH, XSKILL_HOME, get_skill_dir
    if CONFIG_PATH.is_file():
        try:
            return get_skill_dir()
        except Exception:
            pass
    return XSKILL_HOME / "skill"


def _print_import_result(imported, *, json_mode: bool) -> None:
    import json as _json
    if json_mode:
        print(_json.dumps({
            "name": imported.name,
            "sha": imported.sha,
            "existed": imported.existed,
            "baby_overwritten": imported.baby_overwritten,
            "staging_kept": imported.staging_kept,
            "main_round_scores_cleared": imported.main_round_scores_cleared,
            "stash_path": imported.stash_path,
            "warnings": imported.warnings,
        }, ensure_ascii=False, indent=2))
        return
    verb = "更新" if imported.existed else "纳入"
    print(f"imported: {imported.name}  ({verb} 主干 {imported.sha[:8]})")
    if imported.baby_overwritten:
        print("  原来停在预备分支 baby 的草稿已被这次导入覆盖")
    if imported.staging_kept:
        print("  灰度分支 staging 仍在，继续和新主干对比"
              f"（清了当前轮主干体验分 {imported.main_round_scores_cleared} 条）")
    if imported.stash_path:
        print(f"  未提交内容已拷到 {imported.stash_path}，本机已换成这次导入的版本")
    for warning in imported.warnings:
        print(f"warning: {warning}", file=sys.stderr)


def cmd_import(args, http=None, headers=None) -> int:
    """`xskill import <路径>` —— 把已有技能目录纳入自有仓，不是 upload。"""
    from pathlib import Path
    from xskill.config import XSKILL_HOME
    from xskill.skill.importer import discover_import_sources, import_skill_path

    source = Path(args.path).expanduser()
    try:
        sources = discover_import_sources(source)
    except FileNotFoundError as missing:
        print(f"error: {missing}", file=sys.stderr)
        return 2

    if _is_thin_team_client():
        return _cmd_import_team(
            args, sources, http=http, headers=headers,
        )

    home = Path.home()
    skill_dir = _local_import_skill_dir()
    try:
        results = import_skill_path(
            skill_dir, source, install=True, home_root=home,
            stash_home=XSKILL_HOME,
        )
    except (FileNotFoundError, ValueError, RuntimeError) as failed:
        print(f"error: {failed}", file=sys.stderr)
        return 1
    for imported in results:
        _print_import_result(imported, json_mode=args.json)
    return 0


def _cmd_import_team(args, sources, *, http=None, headers=None) -> int:
    import httpx
    from pathlib import Path
    from xskill.config import (
        XSKILL_HOME,
        get_team_client_history_path,
        get_team_client_state_path,
    )
    from xskill.skill.importer import (
        HARNESS_IMPORT_WARNING,
        ImportResult,
        is_harness_skill_path,
        maybe_stash_overwrite_dir,
        pack_import_zip,
    )
    from xskill.team.client.import_follow import follow_imported_skill
    from xskill.team.client.state import load_client_state

    if http is None:
        http, headers = _team_client_http_and_headers()
        if http is None:
            return 1
        http.timeout = 120.0

    skill_dir = XSKILL_HOME / "skill"
    home = Path.home()
    state = load_client_state(get_team_client_state_path())
    history_path = get_team_client_history_path(state.server_url)

    for source in sources:
        name = source.name
        if is_harness_skill_path(source, home_root=home):
            print(f"warning: {HARNESS_IMPORT_WARNING}", file=sys.stderr)
        stash = maybe_stash_overwrite_dir(
            source, name, home_root=XSKILL_HOME,
        )
        working = skill_dir / name
        if working.exists() and working.resolve() != source.resolve():
            extra = maybe_stash_overwrite_dir(
                working, name, home_root=XSKILL_HOME,
            )
            stash = stash or extra
        payload = pack_import_zip(source, include_git=True)
        if len(payload) > 50 * 1024 * 1024:
            print(f"error: {name} 打包后超过 50MB", file=sys.stderr)
            return 2
        try:
            resp = http.post(
                "/api/v1/team/skills/import",
                files={"file": (f"{name}.zip", payload, "application/zip")},
                data={"name": name},
                headers=headers,
            )
        except (httpx.HTTPError, OSError) as network_error:
            print(f"error: 无法连接 team server（{type(network_error).__name__}: "
                  f"{network_error}）", file=sys.stderr)
            return 1
        if resp.status_code == 404:
            print("error: server 版本过旧，不支持技能纳入（需含 /skills/import）",
                  file=sys.stderr)
            return 1
        if resp.status_code != 200:
            print(f"error: 纳入失败 HTTP {resp.status_code}: {resp.text[:300]}",
                  file=sys.stderr)
            return 1
        body = resp.json()
        imported = ImportResult(
            name=body.get("name") or name,
            existed=bool(body.get("existed")),
            sha=str(body.get("sha") or ""),
            baby_overwritten=bool(body.get("baby_overwritten")),
            staging_kept=bool(body.get("staging_kept")),
            main_round_scores_cleared=int(
                body.get("main_round_scores_cleared") or 0
            ),
            stash_path=str(stash) if stash else "",
        )
        if is_harness_skill_path(source, home_root=home):
            imported.warnings.append(HARNESS_IMPORT_WARNING)
        try:
            follow_imported_skill(
                http=http,
                headers=headers,
                skill_dir=skill_dir,
                name=imported.name,
                sha=imported.sha,
                home_root=home,
                history_path=history_path,
            )
        except RuntimeError as follow_error:
            print(f"error: 本机跟上这次主干失败: {follow_error}", file=sys.stderr)
            return 1
        _print_import_result(imported, json_mode=args.json)
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
        from xskill.config import get_registry_db_path, get_skill_dir
        from xskill.pipeline.registry import clear_rebuild_derived_state
        from xskill.skill.repo import SkillRepo
        skill_count = SkillRepo(get_skill_dir()).wipe_all_skills(
            db_path=get_registry_db_path(),
        )
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
        help="搜索 skill（自动适配：已连 team 走 SkillHub，否则本地技能库）",
    )
    p_search.add_argument(
        "terms", nargs="+", metavar="QUERY",
        help="搜索词（可多个，拼成一个查询）",
    )
    p_search.add_argument("--top-k", "-k", type=int, default=5,
                          help="返回条数（skillhub 搜索最多 10）")
    p_search.add_argument(
        "--download", action="store_true",
        help="兼容旧 search：下载命中到 10 槽 LRU 并安装到已检测 harness",
    )
    p_search.add_argument("--json", action="store_true", help="机读 JSON 输出")
    p_search_mode = p_search.add_mutually_exclusive_group()
    p_search_mode.add_argument(
        "--team", action="store_true",
        help="强制走 team SkillHub 搜索（需先 xskill connect）",
    )
    p_search_mode.add_argument(
        "--local", action="store_true",
        help="强制搜本地技能库（daemon 语义检索，不可用回退 BM25）",
    )

    p_download = sub.add_parser(
        "download", help="按 search 返回的 skill ID 显式下载并持久安装",
    )
    p_download.add_argument("skill_id", help="xskill search 返回的 skill ID")
    p_download.add_argument(
        "--agent", action="append", choices=_DOWNLOAD_AGENT_CHOICES,
        default=[], metavar="AGENT",
        help="安装目标，可重复（如 claude-code、codex、cursor）",
    )
    p_download.add_argument(
        "-y", "--yes", action="store_true",
        help="不询问；未指定 --agent 时自动选择已检测 harness",
    )
    p_download.add_argument(
        "--json", action="store_true", help="机读 JSON 输出",
    )

    p_upload = sub.add_parser(
        "upload", help="打包一个 skill 文件夹上传到 team server 的 user skillhub",
    )
    p_upload.add_argument("path", type=str, help="包含 SKILL.md 的 skill 目录")
    p_upload.add_argument("--json", action="store_true", help="机读 JSON 输出")

    p_gen = sub.add_parser(
        "generate",
        help="按指令在 team server 上即时生成或改写 skill（直接提交主干）",
    )
    p_gen.add_argument(
        "instruction", nargs="+", metavar="PROMPT",
        help="给生成代理的定向指令",
    )
    p_gen.add_argument(
        "--name", default="",
        help="优先阅读的工号，逗号分隔；不传则提示词里写可看全量",
    )

    p_import = sub.add_parser(
        "import", help="把已有技能目录纳入自有仓（不是 upload）",
    )
    p_import.add_argument("path", type=str, help="技能目录，或含多个技能的父目录")
    p_import.add_argument("--json", action="store_true", help="机读 JSON 输出")

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

    # skillhub 搜索/下载/上传是瘦客户端侧（走 team server），不碰 config.yaml。
    if args.command == "search":
        return cmd_search(args)
    if args.command == "download":
        return cmd_download(args)
    if args.command == "upload":
        return cmd_upload(args)
    if args.command == "generate":
        return cmd_generate(args)
    if args.command == "import":
        return cmd_import(args)

    # stats 只读 registry，不需要 config.yaml / llm.api_key / facade
    if args.command == "stats":
        return cmd_stats(args)

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
